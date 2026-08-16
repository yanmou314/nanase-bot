"""共享工具模块：常量、公共函数，所有插件统一引用。"""
import json
import os
import subprocess
import threading
import time
import asyncio
from datetime import datetime

from nonebot.adapters.onebot.v11 import Message, MessageEvent, MessageSegment

OWNER = os.getenv("QQBOT_OWNER", "REPLACE_WITH_OWNER_QQ")

FONTS = {
    "bold": "/usr/share/fonts/custom/ZCOOLKuaiLe-Regular.ttf",
    "noto_bold": "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "noto_reg": "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
}

_NULL_LOCK = threading.RLock()


def is_owner(event: MessageEvent) -> bool:
    return str(event.user_id) == OWNER


def at_prefix(event: MessageEvent) -> Message:
    if hasattr(event, "group_id"):
        return Message(MessageSegment.at(event.user_id))
    return Message()


def save_image(data: bytes, content_type: str, prefix: str, cache_dir: str) -> str:
    ext = ".jpg" if "jpeg" in (content_type or "") else ".png"
    cleanup_cache(cache_dir, max_age=24 * 60 * 60)
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"{prefix}_{int(datetime.now().timestamp() * 1000)}{ext}")
    with open(path, "wb") as f:
        f.write(data)
    return path


def parse_tag(arg: str) -> str:
    tag = arg.replace(" ", "").replace("-", "#")
    return tag if "#" in tag else ""


def now_str(fmt: str = "%m-%d %H:%M") -> str:
    return datetime.now().strftime(fmt)


def cleanup_cache(cache_dir: str, max_age: int = 3600) -> int:
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


def run_in_thread(func, *args, **kwargs):
    return asyncio.to_thread(func, *args, **kwargs)


# ---------------- JSON 状态文件读写（统一模式：RLock + tmp + os.replace） ----------------

def load_json_state(path: str, lock=None) -> dict:
    """读取 JSON 状态文件；缺失/损坏/非对象一律返回 {}，不抛异常。lock 为 RLock 或 None。"""
    with (lock or _NULL_LOCK):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}


def save_json_state(path: str, data: dict, lock=None) -> None:
    """原子写 JSON 状态文件（tmp + os.replace），锁由调用方提供或使用公共锁。lock 为 RLock 或 None。"""
    with (lock or _NULL_LOCK):
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)


# ---------------- 图片渲染（weasyprint → PDF → pdftoppm → PNG） ----------------

def render_html_to_png(html: str, prefix: str, cache_dir: str, max_age: int = 24 * 60 * 60) -> str:
    """HTML 渲染为 PNG 的通用管线；资源加载仅允许 data: URL，阻止外部请求（防 SSRF）。"""
    from weasyprint import HTML, default_url_fetcher

    def _local_only_fetcher(url, timeout=10, *args, **kwargs):
        if url.startswith("data:"):
            return default_url_fetcher(url, timeout, *args, **kwargs)
        raise ValueError(f"blocked external url: {url}")

    os.makedirs(cache_dir, exist_ok=True)
    cleanup_cache(cache_dir, max_age=max_age)
    stamp = int(time.time() * 1000)
    tmp_pdf = os.path.join(cache_dir, f"{prefix}_{stamp}.pdf")
    path = os.path.join(cache_dir, f"{prefix}_{stamp}.png")
    try:
        HTML(string=html, url_fetcher=_local_only_fetcher).write_pdf(tmp_pdf)
        subprocess.run(
            ["pdftoppm", "-png", "-r", "192", "-singlefile", tmp_pdf, path[:-4]],
            check=True, capture_output=True,
        )
    finally:
        if os.path.exists(tmp_pdf):
            os.remove(tmp_pdf)
    return path


def gradient_background(w: int, h: int, top=(249, 248, 250), bottom=(243, 241, 246)) -> str:
    """生成竖向渐变背景的 data: URL（供 HTML 卡片使用）。"""
    import base64
    import io
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / h
        d.line([(0, y), (w, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


# ---------------- httpx 客户端单例 ----------------

_http_clients: dict[float, object] = {}


def get_http_client(timeout: float = 30.0):
    """按超时参数缓存的 httpx.AsyncClient 单例。"""
    import httpx

    client = _http_clients.get(timeout)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(timeout=timeout)
        _http_clients[timeout] = client
    return client


async def close_http_clients() -> None:
    """关闭全部公共 httpx 客户端（幂等，可被多个插件重复注册调用）。"""
    for client in list(_http_clients.values()):
        if not client.is_closed:
            await client.aclose()
    _http_clients.clear()
