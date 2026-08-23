import math
import os
import random
import time
from collections import Counter

from PIL import Image, ImageDraw, ImageFont

from common import FONTS

CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")

# 字体候选链：首选站酷字体，缺失时依次回退到 common 维护的其他字体，最后用 PIL 默认字体
FONT_CANDIDATES = (
    "/usr/share/fonts/custom/ZCOOLQingKeHuangYou-Regular.ttf",
    FONTS.get("bold"),
    FONTS.get("noto_reg"),
    FONTS.get("noto_bold"),
)
PALETTE = ["#5858B8", "#6868C8", "#8898C8", "#88A050", "#C83838", "#A84848",
           "#B89838", "#E0B850", "#C8D898", "#E09098"]

_font_cache: dict[int, ImageFont.FreeTypeFont] = {}


def _font(size: int) -> ImageFont.FreeTypeFont:
    """按字号缓存字体对象，避免每个词重复打开字体文件；全部缺失时回退默认字体。"""
    f = _font_cache.get(size)
    if f is None:
        for path in FONT_CANDIDATES:
            if not path:
                continue
            try:
                f = ImageFont.truetype(path, size)
                break
            except OSError:
                continue
        if f is None:
            f = ImageFont.load_default()
        _font_cache[size] = f
    return f


def _overlap(a, b) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _spiral_positions(cx: float, cy: float, max_r: float, step: float = 4.0):
    ring = 0
    while ring <= max_r:
        n = max(8, int(2 * math.pi * ring / step))
        for i in range(n):
            angle = 2 * math.pi * i / n
            yield cx + ring * math.cos(angle), cy + ring * math.sin(angle) * 0.85
        ring += step


def _render(counter: Counter, n: int, msg_count: int) -> str:
    W, H = 1200, 480
    img = Image.new("RGB", (W, H), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    words = counter.most_common(n)
    maxc = max(c for _, c in words) if words else 1
    entries = [(w, int(24 + 92 * ((c / maxc) ** 1.2))) for w, c in words]
    entries.sort(key=lambda e: -e[1])
    rnd = random.Random(7)

    placed = []
    cx, cy = W / 2, H / 2
    max_r = math.hypot(W, H) / 2

    for word, size in entries:
        font = _font(size)
        bbox = font.getbbox(word)
        ww, hh = bbox[2] - bbox[0], bbox[3] - bbox[1]
        color = rnd.choice(PALETTE)
        ok = False
        for x, y in _spiral_positions(cx, cy, max_r, step=4.0):
            box = (x - ww / 2 - 3, y - hh / 2 - 3, x + ww / 2 + 3, y + hh / 2 + 3)
            if box[0] < 6 or box[2] > W - 6 or box[1] < 6 or box[3] > H - 6:
                continue
            if any(_overlap(box, p[1]) for p in placed):
                continue
            placed.append((word, box, font, color))
            ok = True
            break
        if not ok and size > 30:
            font = _font(30)
            bbox = font.getbbox(word)
            ww, hh = bbox[2] - bbox[0], bbox[3] - bbox[1]
            for x, y in _spiral_positions(cx, cy, max_r, step=4.0):
                box = (x - ww / 2 - 3, y - hh / 2 - 3, x + ww / 2 + 3, y + hh / 2 + 3)
                if box[0] < 6 or box[2] > W - 6 or box[1] < 6 or box[3] > H - 6:
                    continue
                if any(_overlap(box, p[1]) for p in placed):
                    continue
                placed.append((word, box, font, color))
                break

    for word, box, font, color in placed:
        draw.text(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), word, font=font,
                  fill=color, anchor="mm")

    # 消息总数展示在左上角（msg_count 由调用方传入，在此消费）
    if msg_count:
        draw.text((14, 10), f"共 {msg_count} 条消息", font=_font(22),
                  fill=(165, 161, 171), anchor="lt")

    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"words_{int(time.time() * 1000)}.png")
    img.save(path, "PNG")
    return path
