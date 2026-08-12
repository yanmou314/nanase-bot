import math
import os
import random
import time
from collections import Counter

from PIL import Image, ImageDraw, ImageFont

CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")

FONT = "/usr/share/fonts/custom/ZCOOLQingKeHuangYou-Regular.ttf"
PALETTE = ["#5858B8", "#6868C8", "#8898C8", "#88A050", "#C83838", "#A84848",
           "#B89838", "#E0B850", "#C8D898", "#E09098"]


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
        font = ImageFont.truetype(FONT, size)
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
            font = ImageFont.truetype(FONT, 30)
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

    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"words_{int(time.time() * 1000)}.png")
    img.save(path, "PNG")
    return path
