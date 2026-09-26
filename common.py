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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from nonebot.adapters.onebot.v11 import Message, MessageEvent, MessageSegment

_logger = logging.getLogger("qqbot.common")

OWNER = os.getenv("QQBOT_OWNER", "REPLACE_WITH_OWNER_QQ")


def _parse_id_set(raw: str | None) -> set[int]:
    """解析逗号分隔的整数集合；非法项静默跳过。"""
    result: set[int] = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.add(int(part))
        except ValueError:
            _logger.warning("忽略非法 id 项: %r", part)
    return result


# 测试群：仅当显式配置 QQBOT_TEST_PRIVILEGED_GROUPS 时才启用。
# 默认空集——生产路径下不再有整群 owner 提权（含历史硬编码 864213945）。
# 可选 QQBOT_TEST_OWNER_UIDS：非空时要求调用者 uid 同时命中白名单。
TEST_PRIVILEGED_GROUPS = _parse_id_set(os.getenv("QQBOT_TEST_PRIVILEGED_GROUPS"))
TEST_OWNER_UIDS = _parse_id_set(os.getenv("QQBOT_TEST_OWNER_UIDS"))


def _first_existing(*candidates: str) -> str | None:
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


_NOTO_BOLD_CANDIDATES = (
    "/usr/share/fonts/custom/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
)
_NOTO_REG_CANDIDATES = (
    "/usr/share/fonts/custom/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)

FONTS = {
    "bold": "/usr/share/fonts/custom/ZCOOLKuaiLe-Regular.ttf",
    # 候选链：custom/noto → opentype/noto；都不存在时保留首候选，交由调用方报错
    "noto_bold": _first_existing(*_NOTO_BOLD_CANDIDATES) or _NOTO_BOLD_CANDIDATES[0],
    "noto_reg": _first_existing(*_NOTO_REG_CANDIDATES) or _NOTO_REG_CANDIDATES[0],
}

# 渲染（weasyprint/Pillow）全局串行信号量：2 核 1.6G 的小机器上并发渲染
# 容易打爆内存，所有重渲染都应经由它串行化。
RENDER_SEM = asyncio.Semaphore(1)

# 阻塞渲染专用线程池：避免 weasyprint/PIL 长任务占满默认 executor；
# max_workers=2 给 wait_for 超时后残留的 worker 留一个接续槽，同时保证有界。
_RENDER_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="qqbot-render")

_NULL_LOCK = threading.RLock()

# 同一缓存目录的 TTL 清理节流：避免每次保存/渲染都全目录扫描
_CLEANUP_INTERVAL = 60.0
_last_cleanup: dict[str, float] = {}


def is_owner(event: MessageEvent) -> bool:
    """判断是否具备主人权限。

    真实 OWNER 恒为 True；否则仅当事件所在群在 TEST_PRIVILEGED_GROUPS 且
    （TEST_OWNER_UIDS 为空，或 event.user_id 命中该白名单）时才 True。
    """
    if str(event.user_id) == OWNER:
        return True
    gid = getattr(event, "group_id", None)
    if gid is None:
        return False
    try:
        gid_int = int(gid)
    except (TypeError, ValueError):
        return False
    if gid_int not in TEST_PRIVILEGED_GROUPS:
        return False
    if not TEST_OWNER_UIDS:
        return True
    try:
        return int(event.user_id) in TEST_OWNER_UIDS
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


def parse_tag(arg: str) -> str:
    """解析战网/游戏标签：名字#数字。拒绝空白与控制符，防止注入下游指令通道。"""
    tag = arg.replace("-", "#").strip()
    # 拒绝空白与控制字符，避免 foo#123\t/cmd 这类注入
    if any(ch.isspace() or ord(ch) < 32 for ch in tag):
        return ""
    # 此处已无空格可控（isspace 分支先行返回），无需再 replace
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


# ---------------- 业务共享常量 ----------------
# OW 任务中继群与查询机器人 QQ：owstats/auto_chat/ow_patch/repeater/random_chat 多处使用。
# 换中继群只需改这里（此前字面量散落 6 处文件）。
RELAY_GROUP_ID = 864213945
RELAY_BOT_QQ = 3889045090


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
        # 权限沿用目标文件现状（防止手工收紧到 600 的密钥类文件在重写后掉回 644）；
        # 新建一律 600，历史松权限（644 等）趁机收紧——状态文件普遍含 QQ 号/群号，
        # 部分还含 API key 或数据库 DSN。os.open 直接按目标权限建 tmp，消除
        # 「先按 umask 权限落盘、写完才 chmod」的世界可读窗口。
        try:
            mode = os.stat(path).st_mode & 0o777
        except OSError:
            mode = 0o600
        if mode & 0o077:
            mode = 0o600
        fd = None
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                fd = None  # 所有权移交 fdopen
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.chmod(tmp, mode)
            except OSError:
                pass
            os.replace(tmp, path)
        except BaseException:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.remove(tmp)  # 异常路径清理残留 tmp，不留半截文件
            except OSError:
                pass
            raise
        # 目录 fsync：掉电/崩溃后确保 rename 本身已落盘，状态不回退到旧版本
        try:
            dirfd = os.open(os.path.dirname(path) or ".", getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
        except OSError:
            pass


async def save_json_state_async(path: str, data: dict, lock=None) -> None:
    """save_json_state 的异步封装（to_thread），事件循环内请使用本函数。

    传自定义锁时调用方必须在 await 之前已释放该锁：to_thread 的工作线程会重新
    申请同一把锁，协程持锁跨 await 会死锁。正确姿势是先在锁内完成读改写，
    锁外再 await 本函数落盘。
    """
    return await asyncio.to_thread(save_json_state, path, data, lock)


# ---------------- 群成员昵称（带 TTL 的 LRU，跨插件共享单例） ----------------

_NAME_TTL = 300.0  # 昵称缓存有效期（秒），沿用 chat_stats/cmd_stats 原实现
_NAME_FAIL_TTL = 30.0  # NapCat 失败负缓存：短 TTL，避免 API 恢复后仍长期显示 QQ 号
_NAME_CACHE_MAX = 10000  # 简单 LRU 上限，防止长期运行内存增长
_member_name_cache: OrderedDict = OrderedDict()
_member_name_ts: dict = {}
_member_name_lock = threading.Lock()  # 缓存本体跨事件循环/线程共享，统一加锁


async def get_member_name(bot, group_id: int, user_id: int) -> str:
    """取群成员群名片/昵称，失败回退 QQ 号字符串；结果带 TTL 缓存（插件间共享）。

    单次查询 10 秒超时：NapCat 卡死时不致挂死调用方（日报等批量拉取场景）。
    失败结果仅缓存 _NAME_FAIL_TTL（默认 30s），不写入 300s 负缓存。
    """
    key = (group_id, user_id)
    now = time.time()
    with _member_name_lock:
        cached = _member_name_cache.get(key)
        if cached is not None and now - _member_name_ts.get(key, 0) < _NAME_TTL:
            _member_name_cache.move_to_end(key)
            return cached
    failed = False
    try:
        info = await asyncio.wait_for(
            bot.get_group_member_info(group_id=group_id, user_id=user_id), 10
        )
        name = info.get("card") or info.get("nickname") or str(user_id)
    except Exception:
        name = str(user_id)
        failed = True
    with _member_name_lock:
        _member_name_cache[key] = name
        # 失败条目与成功条目共用 _member_name_ts，但读缓存时按失败短 TTL 过期
        _member_name_ts[key] = now if not failed else now - (_NAME_TTL - _NAME_FAIL_TTL)
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
    """render_html_to_png 的异步封装：经 RENDER_SEM 全局串行化后在专用渲染线程池执行。

    使用专用 ThreadPoolExecutor（max_workers=2）而非默认 executor，避免 weasyprint/PIL
    阻塞任务挤占 asyncio 默认线程池；wait_for 超时后 worker 可能仍在跑，但池有界。
    """
    async with RENDER_SEM:
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(
                _RENDER_EXECUTOR, render_html_to_png, html, prefix, cache_dir, max_age, dpi
            ),
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
    """按超时参数缓存的 httpx.AsyncClient 单例。

    若池内实例已 is_closed，先 pop 再注册新客户端，避免 setdefault 一直
    挂着关闭实例、把新客户端永久丢进孤儿表。
    """
    import httpx

    client = _http_clients.get(timeout)
    if client is not None and client.is_closed:
        _http_clients.pop(timeout, None)
        client = None
    if client is None:
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
