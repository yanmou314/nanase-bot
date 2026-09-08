import math
import os
import random
import time
import uuid
from collections import Counter

from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFont

from common import FONTS

CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")

# 字体候选链：首选站酷字体（风格强），缺字时自动换用全字库的 Noto CJK，最后用 PIL 默认字体
FONT_CANDIDATES = (
    "/usr/share/fonts/custom/ZCOOLQingKeHuangYou-Regular.ttf",
    FONTS.get("bold"),
    FONTS.get("noto_reg"),
    FONTS.get("noto_bold"),
)
PALETTE = ["#5858B8", "#6868C8", "#8898C8", "#88A050", "#C83838", "#A84848",
           "#B89838", "#E0B850", "#C8D898", "#E09098"]

_cmap_cache: dict[str, set[str]] = {}
_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def _coverage(path: str) -> set[str]:
    """字体的可用字符集（cmap），按路径缓存。

    映射到 .notdef（glyph 0）的码点视为缺字——有些字体虽在 cmap 里登记了码点，
    实际字形是空的，渲染出来就是方框/空白。
    """
    chars = _cmap_cache.get(path)
    if chars is None:
        try:
            with TTFont(path, fontNumber=0, lazy=True) as f:
                glyph_order = f.getGlyphOrder()
                notdef = glyph_order[0] if glyph_order else None
                glyf = f["glyf"] if "glyf" in f else None
                chars = set()
                for table in f["cmap"].tables:
                    if table.isUnicode():
                        for cp, gname in table.cmap.items():
                            if gname == notdef:
                                continue
                            # 无轮廓的空字形（部分字体给未知字配的是空框）同样视为缺字
                            if glyf is not None and glyf[gname].numberOfContours == 0:
                                continue
                            chars.add(chr(cp))
        except Exception:
            chars = set()
        _cmap_cache[path] = chars
    return chars


def _font(size: int, text: str = "") -> ImageFont.FreeTypeFont:
    """按 (字体路径, 字号) 缓存；text 非空时优先选能完整覆盖其字形的字体，
    避免站酷字体缺字渲染成方框。"""
    for path in FONT_CANDIDATES:
        if path and text and text not in ("\u3000", " "):
            cov = _coverage(path)
            if cov and not set(text) <= cov:
                continue
        key = (path or "", size)
        f = _font_cache.get(key)
        if f is None:
            try:
                f = ImageFont.truetype(path, size)
            except (OSError, TypeError):
                continue
            _font_cache[key] = f
        if f is not None:
            return f
    return ImageFont.load_default()


def _overlap(a, b) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _spiral_positions(cx: float, cy: float, max_r: float, step: float = 3.0):
    ring = 0
    while ring <= max_r:
        n = max(8, int(2 * math.pi * ring / step))
        for i in range(n):
            angle = 2 * math.pi * i / n
            yield cx + ring * math.cos(angle), cy + ring * math.sin(angle) * 0.85
        ring += step


# 逐级缩小的字号比例：放不下就缩小重试，保证面板尽量铺满而不是丢弃词
_SIZE_STEPS = (1.0, 0.85, 0.7, 0.55, 0.45)
_MIN_SIZE = 16
_PAD = 2  # 词位图自带的外边距，越紧留白越少
_FULL_ENTRIES = 60  # 铺满面板的参考词条数：达到该数时字号系数为 1，原样渲染


def _size_boost(n_entries: int) -> float:
    """词条不足时的字号放大系数：小群消息少、词条不够 60 个时按缺口整体放大。

    缺口越大放得越大，上限 2 倍；词条充足（>=60）或为空时返回 1.0 原样不动。
    排版自带的逐级缩小会兜住放得过大的词，保证不丢词。
    """
    if n_entries <= 0 or n_entries >= _FULL_ENTRIES:
        return 1.0
    return min(2.0, (_FULL_ENTRIES / n_entries) ** 0.7)


def _word_bitmap(word: str, font: ImageFont.FreeTypeFont, color: str) -> Image.Image:
    """把词渲染成透明位图（含 _PAD 边距），排版与旋转统一在此之上进行。"""
    bbox = font.getbbox(word)
    ww = max(1, bbox[2] - bbox[0])
    hh = max(1, bbox[3] - bbox[1])
    img = Image.new("RGBA", (ww + 2 * _PAD, hh + 2 * _PAD), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((_PAD - bbox[0], _PAD - bbox[1]), word, font=font, fill=color)
    return img


def _place(entries, W: int, H: int):
    """把 (词, 目标字号, 颜色, 首选竖排) 逐个螺旋排版。

    每个词先按首选方向、逐级缩小尝试；全部失败再换另一个方向重试一遍，
    竖排词占位为横排的宽高互换，能填进横向螺旋留出的纵向缝隙。
    返回 [(位图, box)]。
    """
    placed = []  # (bitmap, box)
    cx, cy = W / 2, H / 2
    max_r = math.hypot(W, H) / 2
    for word, size, color, vertical in entries:
        for vert in dict.fromkeys((vertical, not vertical)):
            for frac in _SIZE_STEPS:
                s = max(_MIN_SIZE, int(size * frac))
                font = _font(s, word)
                bmp = _word_bitmap(word, font, color)
                if vert:
                    bmp = bmp.rotate(90, expand=True)
                bw, bh = bmp.size
                ok = False
                for x, y in _spiral_positions(cx, cy, max_r, step=3.0):
                    box = (x - bw / 2, y - bh / 2, x + bw / 2, y + bh / 2)
                    if box[0] < _PAD or box[2] > W - _PAD or box[1] < _PAD or box[3] > H - _PAD:
                        continue
                    if any(_overlap(box, p[1]) for p in placed):
                        continue
                    placed.append((bmp, box))
                    ok = True
                    break
                if ok:
                    break
            if ok:
                break
    return placed


def _render(counter: Counter, n: int, msg_count: int, phrases: list[tuple[str, int]] | None = None) -> str:
    """渲染词云：常规词 + 语录片段混合排版，横竖排混排，铺满整个面板。

    counter: 词 -> 次数，次数越高字号越大；phrases: (从长句截取的短语, 重复次数)。
    """
    W, H = 1200, 480
    img = Image.new("RGB", (W, H), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    # 至少取 60 个词参与排版，宁可用小字号填满面板也不留大片空白
    words = counter.most_common(max(n, 60))
    maxc = max(c for _, c in words) if words else 1
    # 语录片段作为高优先级词条混排：字号排，
    # 保证面板尽量铺满而不是缩在中间一小块（词够多时系数为 1，原样不动）
    boost = _size_boost(len(words) + (len(phrases) if phrases else 0))
    rnd = random.Random(7)
    entries: list[tuple[str, int, str, bool]] = []
    for w, c in words:
        size = int((20 + 110 * ((c / maxc) ** 1.3)) * boost)
        entries.append((w, size, rnd.choice(PALETTE), rnd.random() < 0.22))
    # 语录片段作为高优先级词条混排：字号按次数归一化到 36~60（同样随词量放大）
    if phrases:
        qmax = max(c for _, c in phrases)
        for q, c in phrases:
            entries.append((q, int((36 + 24 * (c / qmax)) * boost),
                            rnd.choice(PALETTE), rnd.random() < 0.22))
    entries.sort(key=lambda e: -e[1])

    placed = _place(entries, W, H)
    for bmp, box in placed:
        img.paste(bmp, (int(box[0]), int(box[1])), bmp)

    if msg_count:
        draw.text((14, 10), f"共 {msg_count} 条消息", font=_font(22, f"共 {msg_count} 条消息"),
                  fill=(165, 161, 171), anchor="lt")

    os.makedirs(CACHE_DIR, exist_ok=True)
    # 命名风格与 common.render_html_to_png 一致：时间戳 + uuid 片段，避免并发渲染同名互覆
    path = os.path.join(
        CACHE_DIR, f"words_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}.png"
    )
    img.save(path, "PNG")
    return path
