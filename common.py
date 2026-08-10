"""共享工具模块：常量、公共函数，所有插件统一引用。"""
import os
import time
import asyncio
from datetime import datetime

from nonebot.adapters.onebot.v11 import Message, MessageEvent, MessageSegment

OWNER = "REPLACE_WITH_OWNER_QQ"

FONTS = {
    "bold": "/usr/share/fonts/custom/ZCOOLKuaiLe-Regular.ttf",
    "noto_bold": "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "noto_reg": "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
}


def is_owner(event: MessageEvent) -> bool:
    return str(event.user_id) == OWNER


def at_prefix(event: MessageEvent) -> Message:
    if hasattr(event, "group_id"):
        return Message(MessageSegment.at(event.user_id))
    return Message()


def save_image(data: bytes, content_type: str, prefix: str, cache_dir: str) -> str:
    ext = ".jpg" if "jpeg" in (content_type or "") else ".png"
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
        if os.path.isfile(path) and now - os.path.getmtime(path) > max_age:
            try:
                os.remove(path)
                count += 1
            except OSError:
                pass
    return count


def run_in_thread(func, *args, **kwargs):
    return asyncio.to_thread(func, *args, **kwargs)