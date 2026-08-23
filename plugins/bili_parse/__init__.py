"""B站视频链接解析：群里出现 B站链接 / BV号 / av号 时，自动回复封面图与视频信息。

只调用 B站查询 API 解析元信息，不下载视频；封面图以 URL 形式交给 NapCat
直接发送，服务器零下载带宽。同群 30 秒冷却 + 同视频 10 分钟去重防刷屏。
"""
import logging
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from nonebot import on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageSegment

from common import get_http_client

_logger = logging.getLogger(__name__)
_SH = ZoneInfo("Asia/Shanghai")

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
_CACHE_TTL = 30 * 60.0        # 视频信息缓存

_group_last: dict[str, float] = {}                # group_id -> 上次解析时间
_recent: dict[tuple[str, str], float] = {}        # (group_id, vid) -> 上次解析时间
_info_cache: dict[str, tuple[float, dict]] = {}   # vid -> (过期时间, 信息)


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
            "view": stat.get("view", 0),
            "danmaku": stat.get("danmaku", 0),
            "like": stat.get("like", 0),
            "coin": stat.get("coin", 0),
            "favorite": stat.get("favorite", 0),
            "reply": stat.get("reply", 0),
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


def _fmt_count(n: int) -> str:
    return f"{n / 10000:.1f}万" if n >= 10000 else str(n)


def _fmt_duration(seconds: int) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def build_card(info: dict) -> MessageSegment:
    """封面图 + 信息卡片（文本段包裹，标题/UP主名不会触发 CQ 码解析）。"""
    date = datetime.fromtimestamp(info["pubdate"], _SH).strftime("%Y-%m-%d") \
        if info["pubdate"] else "未知"
    text = (
        f"🎬 {info['title']}\n"
        f"👤 UP主：{info['owner']}\n"
        f"▶ 播放 {_fmt_count(info['view'])} · 弹幕 {_fmt_count(info['danmaku'])}"
        f" · 点赞 {_fmt_count(info['like'])}\n"
        f"🪙 投币 {_fmt_count(info['coin'])} · ⭐ 收藏 {_fmt_count(info['favorite'])}"
        f" · 💬 评论 {_fmt_count(info['reply'])}\n"
        f"⏱ {_fmt_duration(info['duration'])} · 📅 {date}\n"
        f"🔗 https://www.bilibili.com/video/{info['bvid']}"
    )
    parts = []
    if info["pic"]:
        parts.append(MessageSegment.image(info["pic"]))
    parts.append(MessageSegment.text(text))
    return sum(parts[1:], parts[0])


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
    await bili_matcher.send(build_card(info))
