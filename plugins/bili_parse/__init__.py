"""B站视频链接解析：群里出现 B站链接 / BV号 / av号 时，自动回复封面图与视频信息。

只调用 B站查询 API 解析元信息，不下载视频。卡片用 PIL 整体渲染为一张图片
（B站粉主题；weasyprint 在本机解析 CJK 字体需约 10 秒，PIL 仅需数百毫秒）；
渲染失败时回退"封面+文本"卡片。同群 30 秒冷却 + 同视频 10 分钟去重防刷屏，
信息与渲染结果各缓存 30 分钟。
"""
import asyncio
import io
import logging
import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from nonebot import on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageSegment

from common import FONTS, RENDER_SEM, get_http_client, save_image

_logger = logging.getLogger(__name__)
_SH = ZoneInfo("Asia/Shanghai")
CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")

bili_matcher = on_message(priority=25, block=False)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.bilibili.com/",
}

# BV 号固定 1+10 位；av 号至少 5 位数字且前一个字符不能是字母数字（避免误伤普通文本）
_BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")
_AV_RE = re.compile(r"(?<![0-9A-Za-z])av(\d{5,})", re.IGNORECASE)
_B23_RE = re.compile(r"https?://b23\.tv/[0-9A-Za-z]+")
_CMD_PREFIXES = (".", "/", "。")

_CARD_W = 900
_COVER_H = 506
_GROUP_COOLDOWN = 30.0        # 同群两次解析的最小间隔
_DUP_WINDOW = 10 * 60.0       # 同群同一视频的去重窗口
_CACHE_TTL = 30 * 60.0        # 视频信息/渲染卡片缓存
_MAX_COVER_BYTES = 8 * 1024 * 1024  # 封面大小上限，防异常大图耗内存

# B站粉主题配色
_C_PINK = (251, 114, 153)     # #fb7299
_C_DARK = (24, 25, 28)        # #18191c
_C_GRAY = (97, 102, 109)      # #61666d
_C_LIGHT = (148, 153, 160)    # #9499a0
_C_LINE = (240, 241, 242)     # #f0f1f2
_C_BG_GRAY = (246, 247, 248)  # #f6f7f8

_group_last: dict[str, float] = {}                # group_id -> 上次解析时间
_recent: dict[tuple[str, str], float] = {}        # (group_id, vid) -> 上次解析时间
_info_cache: dict[str, tuple[float, dict]] = {}   # vid -> (过期时间, 信息)
_img_cache: dict[str, tuple[float, str]] = {}     # bvid -> (过期时间, 渲染图路径)
_font_cache: dict[tuple[str, int], object] = {}   # (字体名, 字号) -> ImageFont


def _font(key: str, size: int):
    """按 (字体, 字号) 缓存字体对象；生产用 Noto CJK，缺失环境回退默认字体。"""
    cache_key = (key, size)
    if cache_key in _font_cache:
        return _font_cache[cache_key]
    from PIL import ImageFont

    try:
        font = ImageFont.truetype(FONTS[key], size)
    except OSError:
        font = ImageFont.load_default(size)
    _font_cache[cache_key] = font
    return font


def extract_ids(*texts: str) -> list[str]:
    """从文本中提取去重后的视频 ID 列表（BV 原样，av 转小写号段）。"""
    found: list[str] = []
    for text in texts:
        found.extend(_BV_RE.findall(text or ""))
        found.extend(f"av{n}" for n in _AV_RE.findall(text or ""))
    return list(dict.fromkeys(found))


async def resolve_b23(url: str) -> str | None:
    """解析 b23.tv 短链的真实地址（跟随重定向后从最终 URL 提取视频 ID）。"""
    try:
        client = get_http_client(5.0)
        resp = await client.get(url, headers=_HEADERS, follow_redirects=True)
        ids = extract_ids(str(resp.url))
        return ids[0] if ids else None
    except httpx.HTTPError:
        _logger.warning("b23.tv 短链解析失败: %s", url)
        return None


async def fetch_info(vid: str) -> dict | None:
    """查询视频信息；返回 None 表示查询失败（调用方静默跳过即可）。"""
    now = time.time()
    cached = _info_cache.get(vid)
    if cached and cached[0] > now:
        return cached[1]
    params = {"bvid": vid[2:]} if vid.startswith("BV") else {"aid": vid[2:]}
    try:
        client = get_http_client(10.0)
        resp = await client.get(
            "https://api.bilibili.com/x/web-interface/view",
            params=params, headers=_HEADERS,
        )
        data = resp.json()
        if data.get("code") != 0 or not isinstance(data.get("data"), dict):
            _logger.warning("B站视频信息查询失败 vid=%s code=%s", vid, data.get("code"))
            return None
        d = data["data"]
        stat = d.get("stat") or {}
        info = {
            "bvid": d.get("bvid") or vid,
            "title": d.get("title") or "",
            "pic": (d.get("pic") or "").replace("http://", "https://"),
            "owner": (d.get("owner") or {}).get("name") or "",
            "tname": d.get("tname") or "",
            "videos": d.get("videos", 1) or 1,
            "desc": _clean_desc(d.get("desc")),
            "view": stat.get("view", 0),
            "danmaku": stat.get("danmaku", 0),
            "like": stat.get("like", 0),
            "coin": stat.get("coin", 0),
            "favorite": stat.get("favorite", 0),
            "reply": stat.get("reply", 0),
            "share": stat.get("share", 0),
            "duration": d.get("duration", 0),
            "pubdate": d.get("pubdate", 0),
        }
    except (httpx.HTTPError, ValueError, KeyError):
        _logger.warning("B站视频信息请求异常 vid=%s", vid, exc_info=True)
        return None
    # 缓存有界：超过 200 条时先清掉已过期项
    if len(_info_cache) > 200:
        expired = [k for k, (exp, _) in _info_cache.items() if exp <= now]
        for k in expired:
            _info_cache.pop(k, None)
    _info_cache[vid] = (now + _CACHE_TTL, info)
    return info


def _clean_desc(raw: str | None) -> str:
    """简介压成单行；过滤 "-" 之类的无意义占位内容。"""
    flat = " ".join((raw or "").split())
    return "" if flat in {"-", "——", "（本视频暂时没有简介）"} else flat


def _fmt_count(n: int) -> str:
    return f"{n / 10000:.1f}万" if n >= 10000 else str(n)


def _fmt_duration(seconds: int) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _fmt_desc(desc: str, limit: int = 60) -> str:
    """简介压成单行并截断；空简介返回空串。"""
    if not desc:
        return ""
    flat = " ".join(desc.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


# ---------------- 文本卡片（渲染失败的回退形态） ----------------

def build_card(info: dict) -> MessageSegment:
    """封面图 + 信息卡片（文本段包裹，标题/UP主名不会触发 CQ 码解析）。"""
    date = datetime.fromtimestamp(info["pubdate"], _SH).strftime("%Y-%m-%d") \
        if info["pubdate"] else "未知"
    multi = f"（全{info['videos']}P）" if info.get("videos", 1) > 1 else ""
    owner_line = f"👤 UP主：{info['owner']}"
    if info.get("tname"):
        owner_line += f" ｜ {info['tname']}"
    desc = _fmt_desc(info.get("desc", ""))
    lines = [
        f"🎬 {info['title']}{multi}",
        owner_line,
        f"▶ 播放 {_fmt_count(info['view'])} · 弹幕 {_fmt_count(info['danmaku'])}"
        f" · 点赞 {_fmt_count(info['like'])} · ↗ 分享 {_fmt_count(info['share'])}",
        f"🪙 投币 {_fmt_count(info['coin'])} · ⭐ 收藏 {_fmt_count(info['favorite'])}"
        f" · 💬 评论 {_fmt_count(info['reply'])}",
        f"⏱ {_fmt_duration(info['duration'])} · 📅 {date}",
    ]
    if desc:
        lines.append(f"📝 简介：{desc}")
    lines.append(f"🔗 https://www.bilibili.com/video/{info['bvid']}")
    text = "\n".join(lines)
    parts = []
    if info["pic"]:
        parts.append(MessageSegment.image(info["pic"]))
    parts.append(MessageSegment.text(text))
    return sum(parts[1:], parts[0])


# ---------------- 图片卡片（PIL 渲染） ----------------

def _wrap_text(text: str, font, max_width: int, max_lines: int) -> list[str]:
    """CJK 逐字贪心换行；超出行数时末行截断加省略号。"""
    lines: list[str] = []
    cur = ""
    for ch in text:
        if font.getlength(cur + ch) <= max_width:
            cur += ch
        else:
            lines.append(cur)
            cur = ch
            if len(lines) == max_lines:
                tail = lines[-1]
                while tail and font.getlength(tail + "…") > max_width:
                    tail = tail[:-1]
                lines[-1] = tail + "…"
                return lines
    if cur:
        lines.append(cur)
    return lines


def _page_height(head_h: int, title_h: int, has_desc: bool) -> int:
    """按分区高度累加：封面 + 标题 + meta + 数据行 + 简介块 + 页脚 + 内边距。"""
    h = head_h + 26 + title_h      # 顶部内边距 + 标题
    h += 14 + 25                    # meta（上间距 + 行高）
    h += 20 + 70                    # stats（上间距 + 分隔线内边距 + 数字/标签两行）
    if has_desc:
        h += 18 + 70                # 简介（两行以内，含内边距）
    h += 18 + 21 + 24               # 页脚（上间距 + 行高 + 底部内边距）
    return h


def _render_card(info: dict, cover_bytes: bytes | None):
    """渲染卡片为 PIL Image（同步阻塞，调用方须在 to_thread 中执行）。"""
    from PIL import Image, ImageDraw, ImageOps

    title_font = _font("noto_bold", 30)
    meta_font = _font("noto_reg", 17)
    num_font = _font("noto_bold", 21)
    label_font = _font("noto_reg", 13)
    desc_font = _font("noto_reg", 15)
    foot_font = _font("noto_reg", 14)

    title_lines = _wrap_text(info["title"] or "未知标题", title_font, _CARD_W - 60, 2)
    title_h = 44 * len(title_lines)
    desc = _fmt_desc(info.get("desc", ""), limit=80)
    date = datetime.fromtimestamp(info["pubdate"], _SH).strftime("%Y-%m-%d") \
        if info["pubdate"] else "未知"

    height = _page_height(_COVER_H, title_h, bool(desc))
    canvas = Image.new("RGB", (_CARD_W, height), (255, 255, 255))

    # 封面区（或无封面时的粉色渐变占位头）
    cover_img = None
    if cover_bytes:
        try:
            cover_img = ImageOps.fit(
                Image.open(io.BytesIO(cover_bytes)).convert("RGB"),
                (_CARD_W, _COVER_H), Image.Resampling.LANCZOS,
            )
        except Exception:
            _logger.warning("封面解码失败，使用占位头", exc_info=True)
    if cover_img is not None:
        mask = Image.new("L", (_CARD_W, _COVER_H), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [0, 0, _CARD_W - 1, _COVER_H + 18], radius=18, fill=255)
        canvas.paste(cover_img, (0, 0), mask)
    else:
        strip = Image.new("RGB", (1, _COVER_H))
        for y in range(_COVER_H):
            t = y / _COVER_H
            strip.putpixel((0, y), tuple(int(255 - (255 - c) * t) for c in _C_PINK))
        canvas.paste(strip.resize((_CARD_W, _COVER_H)), (0, 0))

    draw = ImageDraw.Draw(canvas)

    # 角标：多P（左上，粉底）与时长（右下，半透明黑底，仅真实封面时）
    if info.get("videos", 1) > 1:
        tag = f"全{info['videos']}P"
        tw = draw.textlength(tag, font=meta_font)
        draw.rounded_rectangle([14, 12, 14 + tw + 20, 46], radius=6, fill=_C_PINK)
        draw.text((24, 15), tag, font=meta_font, fill=(255, 255, 255))
    if cover_img is not None:
        dur = _fmt_duration(info["duration"])
        dw = draw.textlength(dur, font=meta_font)
        box = [_CARD_W - 24 - dw - 20, _COVER_H - 38, _CARD_W - 24, _COVER_H - 10]
        overlay = Image.new("RGBA", (_CARD_W, _COVER_H), (0, 0, 0, 0))
        ImageDraw.Draw(overlay).rounded_rectangle(box, radius=6, fill=(0, 0, 0, 165))
        canvas.paste(Image.alpha_composite(
            canvas.crop((0, 0, _CARD_W, _COVER_H)).convert("RGBA"),
            overlay).convert("RGB"), (0, 0))
        draw = ImageDraw.Draw(canvas)
        draw.text((box[0] + 10, box[1] + 3), dur, font=meta_font, fill=(255, 255, 255))

    # 正文
    y = _COVER_H + 26
    for line in title_lines:
        draw.text((30, y), line, font=title_font, fill=_C_DARK)
        y += 44
    y += 14
    owner = info["owner"] or "未知UP主"
    draw.text((30, y), owner, font=meta_font, fill=_C_PINK)
    x = 30 + draw.textlength(owner, font=meta_font) + 10
    for part in (info.get("tname", ""), f"发布于 {date}"):
        if not part:
            continue
        draw.text((x, y), "|", font=meta_font, fill=_C_LINE)
        x += draw.textlength("|", font=meta_font) + 10
        draw.text((x, y), part, font=meta_font, fill=_C_GRAY)
        x += draw.textlength(part, font=meta_font) + 10

    y += 25 + 20
    draw.line([(30, y), (_CARD_W - 30, y)], fill=_C_LINE, width=1)
    y += 18
    stats = [
        ("播放", info["view"]), ("弹幕", info["danmaku"]), ("点赞", info["like"]),
        ("投币", info["coin"]), ("收藏", info["favorite"]),
        ("评论", info["reply"]), ("分享", info["share"]),
    ]
    col_w = (_CARD_W - 60) / len(stats)
    for i, (label, val) in enumerate(stats):
        cx = 30 + col_w * (i + 0.5)
        num = _fmt_count(val)
        draw.text((cx - draw.textlength(num, font=num_font) / 2, y), num,
                  font=num_font, fill=_C_DARK)
        draw.text((cx - draw.textlength(label, font=label_font) / 2, y + 32), label,
                  font=label_font, fill=_C_LIGHT)
    y += 70

    if desc:
        y += 18
        draw.rounded_rectangle([30, y, _CARD_W - 30, y + 70], radius=10, fill=_C_BG_GRAY)
        for i, line in enumerate(_wrap_text(f"简介：{desc}", desc_font, _CARD_W - 92, 2)):
            draw.text((46, y + 12 + i * 23), line, font=desc_font, fill=_C_GRAY)
        y += 70

    y += 18
    draw.text((30, y), "哔哩哔哩 bilibili.com", font=foot_font, fill=_C_PINK)
    bvid = info["bvid"]
    draw.text((_CARD_W - 30 - draw.textlength(bvid, font=foot_font), y), bvid,
              font=foot_font, fill=_C_LIGHT)
    return canvas


def _render_and_save(info: dict, cover_bytes: bytes | None) -> str:
    """渲染卡片并落盘缓存目录，返回 PNG 路径。"""
    img = _render_card(info, cover_bytes)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return save_image(buf.getvalue(), "image/png", "bili", CACHE_DIR)


def _resized_cover_url(pic_url: str) -> str:
    """hdslb 封面追加 CDN 缩放参数（原图几百KB → 约30KB），加速下载与渲染。"""
    if not pic_url or "@" in pic_url:
        return pic_url
    return pic_url + "@900w_506h_1c.jpg"


async def _fetch_cover_bytes(pic_url: str) -> bytes | None:
    """下载（缩放后的）封面字节；失败或超限返回 None。"""
    if not pic_url:
        return None
    try:
        client = get_http_client(10.0)
        resp = await client.get(_resized_cover_url(pic_url), headers=_HEADERS)
        if len(resp.content) > _MAX_COVER_BYTES:
            _logger.warning("封面图异常大(%d bytes)已跳过: %s", len(resp.content), pic_url)
            return None
        return resp.content
    except httpx.HTTPError:
        _logger.warning("封面下载失败: %s", pic_url)
        return None


async def build_card_image(info: dict) -> str | None:
    """渲染图片卡片并返回 PNG 路径；渲染异常返回 None（调用方回退文本卡片）。"""
    bvid = info["bvid"]
    now = time.time()
    cached = _img_cache.get(bvid)
    if cached and cached[0] > now and os.path.exists(cached[1]):
        return cached[1]
    cover_bytes = await _fetch_cover_bytes(info.get("pic", ""))
    try:
        async with RENDER_SEM:  # 与其他 PIL/weasyprint 卡片渲染全局串行化
            path = await asyncio.to_thread(_render_and_save, info, cover_bytes)
    except Exception:
        _logger.warning("B站卡片渲染失败 bvid=%s", bvid, exc_info=True)
        return None
    if len(_img_cache) > 100:  # 渲染缓存有界
        expired = [k for k, (exp, _) in _img_cache.items() if exp <= now]
        for k in expired:
            _img_cache.pop(k, None)
    _img_cache[bvid] = (now + _CACHE_TTL, path)
    return path


def _prune_state(now: float) -> None:
    """冷却/去重字典有界清理，防长期运行膨胀。"""
    if len(_recent) > 1000:
        for key, ts in list(_recent.items()):
            if now - ts > _DUP_WINDOW:
                _recent.pop(key, None)
    if len(_group_last) > 1000:
        for gid, ts in list(_group_last.items()):
            if now - ts > _GROUP_COOLDOWN * 10:
                _group_last.pop(gid, None)


@bili_matcher.handle()
async def handle_bili_link(event: GroupMessageEvent):
    # 原文取纯文本 + 消息字符串（CQ 卡片 JSON 里的链接只出现在后者）
    text = event.get_plaintext()
    if text.lstrip().startswith(_CMD_PREFIXES):
        return  # 指令消息（如 .战报 BVxxx）不触发被动解析
    raw = f"{text}\n{event.message}"

    now = time.time()
    gid = str(event.group_id)
    if now - _group_last.get(gid, 0.0) < _GROUP_COOLDOWN:
        return

    ids = extract_ids(raw)
    for url in _B23_RE.findall(raw):
        resolved = await resolve_b23(url)
        if resolved and resolved not in ids:
            ids.append(resolved)
    if not ids:
        return

    # 跳过去重窗口内已解析过的视频，取第一个未解析的
    vid = next((v for v in ids if now - _recent.get((gid, v), 0.0) >= _DUP_WINDOW), None)
    if vid is None:
        return

    info = await fetch_info(vid)
    if info is None:
        return
    _group_last[gid] = now
    _recent[(gid, vid)] = now
    _prune_state(now)

    path = await build_card_image(info)
    if path:
        await bili_matcher.send(MessageSegment.image("file://" + path))
    else:
        await bili_matcher.send(build_card(info))  # 渲染失败回退文本卡片
