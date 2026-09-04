"""共享工具模块：常量、公共函数，所有插件统一引用。

同步阻塞函数（save_image / cleanup_cache / render_html_to_png）禁止直接在
事件循环内调用，请使用对应的 *_async 封装或在 asyncio.to_thread 中执行。
"""
import asyncio
import json
import logging
import os
import subprocess
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime

from nonebot.adapters.onebot.v11 import Message, MessageEvent, MessageSegment

_logger = logging.getLogger("qqbot.common")

OWNER = os.getenv("QQBOT_OWNER", "REPLACE_WITH_OWNER_QQ")

# 测试群：与主人同权，群内任意成员视为 owner（仅用于帮助菜单等管理命令的测试）
TEST_PRIVILEGED_GROUPS = {864213945}

FONTS = {
    "bold": "/usr/share/fonts/custom/ZCOOLKuaiLe-Regular.ttf",
    "noto_bold": "/usr/share/fonts/custom/noto/NotoSansCJK-Bold.ttc",
    "noto_reg": "/usr/share/fonts/custom/noto/NotoSansCJK-Regular.ttc",
}

# 渲染（weasyprint/Pillow）全局串行信号量：2 核 1.6G 的小机器上并发渲染
# 容易打爆内存，所有重渲染都应经由它串行化。
RENDER_SEM = asyncio.Semaphore(1)

_NULL_LOCK = threading.RLock()

# 同一缓存目录的 TTL 清理节流：避免每次保存/渲染都全目录扫描
_CLEANUP_INTERVAL = 60.0
_last_cleanup: dict[str, float] = {}


def is_owner(event: MessageEvent) -> bool:
    if str(event.user_id) == OWNER:
        return True
    gid = getattr(event, "group_id", None)
    try:
        return int(gid) in TEST_PRIVILEGED_GROUPS if gid is not None else False
    except (TypeError, ValueError):
        return False


def at_prefix(event: MessageEvent) -> Message:
    if hasattr(event, "group_id"):
        return Message(MessageSegment.at(event.user_id))
    return Message()


_EXT_BY_TYPE = {
    "jpeg": ".jpg",
    "jpg": ".jpg",
    "png": ".png",
    "webp": ".webp",
    "gif": ".gif",
}


def save_image(data: bytes, content_type: str, prefix: str, cache_dir: str) -> str:
    """保存图片到缓存目录并返回路径。【同步阻塞】事件循环内请用 save_image_async。"""
    base = (content_type or "").split(";")[0].strip().lower()
    ext = _EXT_BY_TYPE.get(base, ".png")
    _cleanup_cache_throttled(cache_dir, max_age=24 * 60 * 60)
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(
        cache_dir, f"{prefix}_{int(datetime.now().timestamp() * 1000)}{uuid.uuid4().hex[:6]}{ext}"
    )
    with open(path, "wb") as f:
        f.write(data)
    return path


async def save_image_async(data: bytes, content_type: str, prefix: str, cache_dir: str) -> str:
    """save_image 的异步封装（to_thread），事件循环内请使用本函数。"""
    return await asyncio.to_thread(save_image, data, content_type, prefix, cache_dir)


def parse_tag(arg: str) -> str:
    tag = arg.replace(" ", "").replace("-", "#")
    return tag if "#" in tag else ""


def now_str(fmt: str = "%m-%d %H:%M") -> str:
    return datetime.now().strftime(fmt)


def cleanup_cache(cache_dir: str, max_age: int = 3600) -> int:
    """删除缓存目录中超过 max_age 的文件。【同步阻塞】返回删除数。"""
    if not os.path.isdir(cache_dir):
        return 0
    now = time.time()
    count = 0
    for name in os.listdir(cache_dir):
        path = os.path.join(cache_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:  # 文件可能被并发删除
            continue
        if now - mtime > max_age:
            try:
                os.remove(path)
                count += 1
            except OSError:
                pass
    return count


def _cleanup_cache_throttled(cache_dir: str, max_age: int) -> None:
    """TTL 清理节流版：同一目录至多每 _CLEANUP_INTERVAL 秒扫一次。"""
    now = time.monotonic()
    last = _last_cleanup.get(cache_dir, 0.0)
    if now - last < _CLEANUP_INTERVAL:
        return
    _last_cleanup[cache_dir] = now
    cleanup_cache(cache_dir, max_age=max_age)


def run_in_thread(func, *args, **kwargs):
    return asyncio.to_thread(func, *args, **kwargs)


# ---------------- JSON 状态文件读写（统一模式：RLock + tmp + fsync + os.replace） ----------------

def load_json_state(path: str, lock=None) -> dict:
    """读取 JSON 状态文件；缺失/非对象返回 {}；损坏/不可读时先备份留档再返回 {}。"""
    with (lock or _NULL_LOCK):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError:
            _backup_corrupt(path)
            return {}
        except OSError:
            # 与 corrupt 备份防护对等：文件存在但读不了（权限/IO 错误）时先留档，
            # 否则后续 save 会把真实状态覆盖为空且无从排查
            _logger.warning("状态文件读取失败: %s", path, exc_info=True)
            _backup_unreadable(path)
            return {}


def _backup_unreadable(path: str) -> None:
    """不可读的状态文件在文件仍存在时先留档（.unreadable-<ts>）再放弃。"""
    import shutil

    if not os.path.isfile(path):
        return
    backup = f"{path}.unreadable-{int(time.time())}"
    try:
        shutil.copy2(path, backup)
        _logger.warning("状态文件不可读，已备份为 %s", backup)
    except OSError:
        _logger.warning("状态文件不可读且备份失败: %s", path, exc_info=True)


def _backup_corrupt(path: str) -> None:
    """损坏的状态文件先留档再放弃，避免一次 save 把原内容静默清空后无从排查。"""
    import shutil

    backup = f"{path}.corrupt-{int(time.time())}"
    try:
        shutil.copy2(path, backup)
        _logger.warning("状态文件损坏，已备份为 %s", backup)
    except OSError:
        _logger.warning("状态文件损坏且备份失败: %s", path, exc_info=True)


def save_json_state(path: str, data: dict, lock=None) -> None:
    """原子写 JSON 状态文件（tmp + fsync + os.replace），锁由调用方提供或使用公共锁。【同步阻塞】"""
    with (lock or _NULL_LOCK):
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        # 权限沿用目标文件现状（防止手工收紧到 600 的密钥类文件在重写后掉回 644）；
        # 新建文件一律 600——状态文件普遍含 QQ 号/群号，部分还含 API key 或数据库 DSN
        try:
            mode = os.stat(path).st_mode & 0o777
        except OSError:
            mode = 0o600
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, path)


async def save_json_state_async(path: str, data: dict, lock=None) -> None:
    """save_json_state 的异步封装（to_thread），事件循环内请使用本函数。

    传自定义锁时调用方必须在 await 之前已释放该锁：to_thread 的工作线程会重新
    申请同一把锁，协程持锁跨 await 会死锁。正确姿势是先在锁内完成读改写，
    锁外再 await 本函数落盘。
    """
    return await asyncio.to_thread(save_json_state, path, data, lock)


# ---------------- 群成员昵称（带 TTL 的 LRU，跨插件共享单例） ----------------

_NAME_TTL = 300.0  # 昵称缓存有效期（秒），沿用 chat_stats/cmd_stats 原实现
_NAME_CACHE_MAX = 10000  # 简单 LRU 上限，防止长期运行内存增长
_member_name_cache: OrderedDict = OrderedDict()
_member_name_ts: dict = {}
_member_name_lock = threading.Lock()  # 缓存本体跨事件循环/线程共享，统一加锁


async def get_member_name(bot, group_id: int, user_id: int) -> str:
    """取群成员群名片/昵称，失败回退 QQ 号字符串；结果带 TTL 缓存（插件间共享）。

    单次查询 10 秒超时：NapCat 卡死时不致挂死调用方（日报等批量拉取场景）。
    """
    key = (group_id, user_id)
    now = time.time()
    with _member_name_lock:
        cached = _member_name_cache.get(key)
        if cached and now - _member_name_ts.get(key, 0) < _NAME_TTL:
            _member_name_cache.move_to_end(key)
            return cached
    try:
        info = await asyncio.wait_for(
            bot.get_group_member_info(group_id=group_id, user_id=user_id), 10
        )
        name = info.get("card") or info.get("nickname") or str(user_id)
    except Exception:
        name = str(user_id)
    with _member_name_lock:
        _member_name_cache[key] = name
        _member_name_ts[key] = now
        _member_name_cache.move_to_end(key)
        while len(_member_name_cache) > _NAME_CACHE_MAX:
            old = _member_name_cache.popitem(last=False)
            _member_name_ts.pop(old[0], None)
    return name


# ---------------- 图片渲染（weasyprint → PDF → pdftoppm → PNG） ----------------

def render_html_to_png(html: str, prefix: str, cache_dir: str, max_age: int = 24 * 60 * 60,
                        dpi: int = 144) -> str:
    """HTML 渲染为 PNG 的通用管线；资源加载仅允许 data: URL，阻止外部请求（防 SSRF）。

    【同步阻塞，几秒级】禁止直接在事件循环内调用，请使用 render_html_to_png_async。
    """
    from weasyprint import HTML, default_url_fetcher

    def _local_only_fetcher(url, timeout=10, *args, **kwargs):
        if url.startswith("data:"):
            return default_url_fetcher(url, timeout, *args, **kwargs)
        raise ValueError(f"blocked external url: {url}")

    os.makedirs(cache_dir, exist_ok=True)
    _cleanup_cache_throttled(cache_dir, max_age=max_age)
    stamp = f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    tmp_pdf = os.path.join(cache_dir, f"{prefix}_{stamp}.pdf")
    path = os.path.join(cache_dir, f"{prefix}_{stamp}.png")
    try:
        HTML(string=html, url_fetcher=_local_only_fetcher).write_pdf(tmp_pdf)
        try:
            subprocess.run(
                ["pdftoppm", "-png", "-r", str(dpi), "-singlefile", tmp_pdf, path[:-4]],
                check=True, capture_output=True, timeout=60,
            )
        except subprocess.TimeoutExpired as exc:
            # 不加超时会让挂死的 pdftoppm 永久占住 RENDER_SEM(1)，全站渲染瘫痪
            raise RuntimeError("pdftoppm render timed out after 60s") from exc
    finally:
        if os.path.exists(tmp_pdf):
            os.remove(tmp_pdf)
    return path


# 渲染整体超时（weasyprint 无内建超时；wait_for 无法强杀线程，但能释放
# RENDER_SEM 让后续渲染继续，与 pdftoppm 的 60s 防护对齐，防止挂死渲染
# 永久占住全站唯一的渲染槽）
RENDER_TOTAL_TIMEOUT = 180


async def render_html_to_png_async(html: str, prefix: str, cache_dir: str,
                                    max_age: int = 24 * 60 * 60, dpi: int = 144) -> str:
    """render_html_to_png 的异步封装：经 RENDER_SEM 全局串行化后在线程池执行。"""
    async with RENDER_SEM:
        return await asyncio.wait_for(
            asyncio.to_thread(render_html_to_png, html, prefix, cache_dir, max_age, dpi),
            timeout=RENDER_TOTAL_TIMEOUT,
        )


def gradient_background(w: int, h: int, top=(249, 248, 250), bottom=(243, 241, 246)) -> str:
    """生成竖向渐变背景的 data: URL（供 HTML 卡片使用）。【同步阻塞】"""
    import base64
    import io
    from PIL import Image

    strip = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / h
        strip.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    img = strip.resize((w, h))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


# ---------------- httpx 客户端单例 ----------------

_http_clients: dict[float, object] = {}
# 并发首调同一 timeout 时败者的客户端无法放回注册表，记入孤儿表由关停统一关闭
_orphan_clients: list = []


def get_http_client(timeout: float = 30.0):
    """按超时参数缓存的 httpx.AsyncClient 单例。"""
    import httpx

    client = _http_clients.get(timeout)
    if client is None or client.is_closed:
        new_client = httpx.AsyncClient(timeout=timeout)
        existing = _http_clients.setdefault(timeout, new_client)
        if existing is new_client:
            client = new_client
        else:
            _orphan_clients.append(new_client)
            client = existing
    return client


async def close_http_clients() -> None:
    """关闭全部公共 httpx 客户端（幂等，可被多个插件重复注册调用）。"""
    for client in list(_http_clients.values()) + _orphan_clients:
        if not client.is_closed:
            await client.aclose()
    _http_clients.clear()
    _orphan_clients.clear()


# 关闭职责收敛到 common 自身，不再依赖个别插件恰好注册了关闭钩子
try:
    from nonebot import get_driver

    get_driver().on_shutdown(close_http_clients)
except Exception:  # 未初始化（如被测试 stub 导入）时跳过
    pass
