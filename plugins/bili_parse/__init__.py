"""B站视频链接解析：群里出现 B站链接 / BV号 / av号 时，自动回复封面图与视频信息。

只调用 B站查询 API 解析元信息，不下载视频。卡片整体渲染为一张图片
（weasyprint HTML→PNG，B站粉主题模板）；渲染失败时回退"封面+文本"卡片。
同群 30 秒冷却 + 同视频 10 分钟去重防刷屏，信息与渲染结果各缓存 30 分钟。
"""
import base64
import html as html_mod
import logging
import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from nonebot import on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageSegment

from common import get_http_client, render_html_to_png_async

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

_GROUP_COOLDOWN = 30.0        # 同群两次解析的最小间隔
_DUP_WINDOW = 10 * 60.0       # 同群同一视频的去重窗口
_CACHE_TTL = 30 * 60.0        # 视频信息/渲染卡片缓存
_MAX_COVER_BYTES = 8 * 1024 * 1024  # 封面大小上限，防异常大图耗内存

_group_last: dict[str, float] = {}                # group_id -> 上次解析时间
_recent: dict[tuple[str, str], float] = {}        # (group_id, vid) -> 上次解析时间
_info_cache: dict[str, tuple[float, dict]] = {}   # vid -> (过期时间, 信息)
_img_cache: dict[str, tuple[float, str]] = {}     # bvid -> (过期时间, 渲染图路径)


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


# ---------------- 图片卡片（HTML 模板 → weasyprint 渲染） ----------------

_CSS = """
* { margin: 0; padding: 0; box-sizing: border-box; }
body { width: 900px; font-family: "Noto Sans CJK SC", sans-serif;
       background: #ffffff; color: #18191c; }
.cover { position: relative; width: 900px; height: 506px; overflow: hidden;
         border-radius: 18px 18px 0 0; background: #ffe4ee; }
.cover img { width: 900px; height: 506px; object-fit: cover; }
.no-cover { width: 900px; height: 150px; border-radius: 18px 18px 0 0;
            background: linear-gradient(135deg, #fb7299 0%, #ff9db8 60%, #ffc6d9 100%); }
.badge-duration { position: absolute; right: 14px; bottom: 12px;
                  background: rgba(0,0,0,0.65); color: #fff; font-size: 16px;
                  padding: 3px 10px; border-radius: 6px; }
.badge-multi { position: absolute; left: 14px; top: 12px;
               background: #fb7299; color: #fff; font-size: 15px;
               padding: 3px 10px; border-radius: 6px; }
.body { padding: 26px 30px 24px; }
.title { font-size: 30px; font-weight: 700; line-height: 1.45; overflow: hidden; }
.meta { margin-top: 14px; font-size: 17px; color: #61666d; }
.meta .owner { color: #fb7299; font-weight: 700; }
.meta .sep { color: #e3e5e7; margin: 0 10px; }
.stats { margin-top: 20px; padding-top: 18px; border-top: 1px solid #f0f1f2;
         display: flex; justify-content: space-between; }
.stat { width: 118px; text-align: center; }
.stat .num { font-size: 21px; font-weight: 700; color: #18191c; }
.stat .label { margin-top: 4px; font-size: 13px; color: #9499a0; }
.desc { margin-top: 18px; background: #f6f7f8; border-radius: 10px;
        padding: 12px 16px; font-size: 15px; color: #61666d;
        line-height: 1.55; overflow: hidden; }
.footer { margin-top: 18px; display: flex; justify-content: space-between;
          align-items: center; font-size: 14px; color: #9499a0; }
.footer .brand { color: #fb7299; font-weight: 700; }
.footer .link { color: #9499a0; }
"""


def _page_height(head_h: int, title_h: int, has_desc: bool) -> int:
    """按分区高度累加：封面 + 标题 + meta + 数据行 + 简介块 + 页脚 + 内边距。"""
    h = head_h + 26 + title_h      # 顶部内边距 + 标题
    h += 14 + 25                    # meta（上间距 + 行高）
    h += 20 + 18 + 43               # stats（上间距 + 分隔线内边距 + 行高）
    if has_desc:
        h += 18 + 47                # 简介（两行以内）
    h += 18 + 21 + 24               # 页脚（上间距 + 行高 + 底部内边距）
    return h


def _build_html(info: dict, cover_b64: str | None) -> tuple[str, int]:
    """构建卡片 HTML；返回 (html, 页面总高度px)。文本先截断保证高度确定。"""
    esc = html_mod.escape
    title = info["title"] or "未知标题"
    if len(title) > 56:  # 标题最多两行，每行约 28 字
        title = title[:56] + "…"
    title_h = 44 if len(title) <= 28 else 88
    desc = _fmt_desc(info.get("desc", ""), limit=80)

    multi = f'<div class="badge-multi">全{info["videos"]}P</div>' \
        if info.get("videos", 1) > 1 else ""
    if cover_b64:
        cover_html = (
            f'<div class="cover"><img src="{cover_b64}">{multi}'
            f'<div class="badge-duration">{_fmt_duration(info["duration"])}</div></div>'
        )
        head_h = 506
    else:
        cover_html = f'<div class="no-cover">{multi}</div>'
        head_h = 150

    owner = esc(info["owner"]) or "未知UP主"
    tname = esc(info.get("tname", ""))
    date = datetime.fromtimestamp(info["pubdate"], _SH).strftime("%Y-%m-%d") \
        if info["pubdate"] else "未知"
    meta = f'<span class="owner">{owner}</span>'
    if tname:
        meta += f'<span class="sep">|</span>{tname}'
    meta += f'<span class="sep">|</span>发布于 {date}'

    stats = [
        ("播放", info["view"]), ("弹幕", info["danmaku"]), ("点赞", info["like"]),
        ("投币", info["coin"]), ("收藏", info["favorite"]),
        ("评论", info["reply"]), ("分享", info["share"]),
    ]
    stats_html = "".join(
        f'<div class="stat"><div class="num">{_fmt_count(v)}</div>'
        f'<div class="label">{k}</div></div>'
        for k, v in stats
    )

    desc_html = f'<div class="desc">简介：{esc(desc)}</div>' if desc else ""
    bvid = esc(info["bvid"])
    height = _page_height(head_h, title_h, bool(desc))

    body = f"""
<div class="body">
  <div class="title">{esc(title)}</div>
  <div class="meta">{meta}</div>
  <div class="stats">{stats_html}</div>
  {desc_html}
  <div class="footer">
    <span class="brand">哔哩哔哩 bilibili.com</span>
    <span class="link">{bvid}</span>
  </div>
</div>"""

    page = (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        f"<style>{_CSS}@page {{ size: 900px {height}px; margin: 0; }}</style>"
        "</head><body>" + cover_html + body + "</body></html>"
    )
    return page, height


async def _fetch_cover_b64(pic_url: str) -> str | None:
    """下载封面并转为 data: URL 内嵌（weasyprint 只允许 data: 资源，防 SSRF）。"""
    if not pic_url:
        return None
    try:
        client = get_http_client(10.0)
        resp = await client.get(pic_url, headers=_HEADERS)
        data = resp.content
        if len(data) > _MAX_COVER_BYTES:
            _logger.warning("封面图异常大(%d bytes)已跳过: %s", len(data), pic_url)
            return None
        mime = (resp.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
        b64 = base64.b64encode(data).decode()
        return f"data:{mime};base64,{b64}"
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
    cover_b64 = await _fetch_cover_b64(info.get("pic", ""))
    html_text, _ = _build_html(info, cover_b64)
    try:
        path = await render_html_to_png_async(html_text, "bili", CACHE_DIR, max_age=2 * 60 * 60)
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
