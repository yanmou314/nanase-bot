import asyncio
import json
import logging
import os
import threading
import time
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import httpx
from nonebot import get_bot, get_driver, on_command
from nonebot.adapters.onebot.v11 import MessageEvent, MessageSegment
from nonebot_plugin_apscheduler import scheduler
from PIL import Image, ImageDraw, ImageFont

from common import cleanup_cache, is_owner

_logger = logging.getLogger(__name__)
_SH = ZoneInfo("Asia/Shanghai")

STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")
CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
_LOCK = threading.Lock()
_client_lock = threading.Lock()

NEWS_API = "https://60s.viki.moe/v2/60s"
BAIDU_API = "https://top.baidu.com/api/board?platform=wise&tab=realtime"

FONT_REG = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"

WHITE = (255, 255, 255)
BLACK = (40, 40, 40)
GRAY = (120, 120, 120)

news_on_cmd = on_command("新闻开启", priority=5, block=True)
news_off_cmd = on_command("新闻关闭", priority=5, block=True)
news_test_cmd = on_command("新闻测试", priority=5, block=True)
news_status_cmd = on_command("新闻状态", priority=5, block=True)

_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        with _client_lock:
            if _http_client is None or _http_client.is_closed:
                _http_client = httpx.AsyncClient(timeout=20)
    return _http_client


@get_driver().on_shutdown
async def _close_http_client() -> None:
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()


def _load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    # 旧格式 {"group_id": "xxx"} 自动迁移到多群格式
    if "groups" not in data and data.get("group_id"):
        data["groups"] = [str(data["group_id"])]
        data.pop("group_id", None)
        _save_state(data)
    if not isinstance(data.get("groups"), list):
        data["groups"] = []
    return data


def _save_state(data: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def _get_groups() -> list[str]:
    with _LOCK:
        state = _load_state()
        return [str(g) for g in (state.get("groups") or []) if str(g)]


def _add_group(gid: str) -> None:
    with _LOCK:
        state = _load_state()
        groups = {str(g) for g in (state.get("groups") or [])}
        groups.add(gid)
        state["groups"] = sorted(groups)
        _save_state(state)


def _remove_group(gid: str) -> None:
    with _LOCK:
        state = _load_state()
        groups = {str(g) for g in (state.get("groups") or [])}
        groups.discard(gid)
        state["groups"] = sorted(groups)
        _save_state(state)


async def _fetch_60s(day: date) -> list[str]:
    client = _get_http_client()
    r = await client.get(NEWS_API, params={"date": day.isoformat()}, timeout=20)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 200:
        raise RuntimeError(data.get("message", "unknown"))
    news = (data.get("data") or {}).get("news") or []
    return [str(x) for x in news]


async def _fetch_baidu() -> list[str]:
    """百度热搜实时榜：条目位于 cards[].content[] 直接层级，字段名为 word。"""
    client = _get_http_client()
    r = await client.get(BAIDU_API, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    r.raise_for_status()
    data = r.json()
    items = []
    for card in data.get("data", {}).get("cards", []):
        for item in card.get("content", []):
            if not isinstance(item, dict):
                continue
            word = item.get("word") or item.get("query")
            if word:
                items.append(str(word))
    return items[:10]


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list:
    lines = []
    for seg in text.split("\n"):
        cur = ""
        for ch in seg:
            if draw.textlength(cur + ch, font=font) > max_width:
                lines.append(cur)
                cur = ch
            else:
                cur += ch
        lines.append(cur)
    return lines


def _render_news_image(day: date, news: list[str], source: str) -> str:
    W = 880
    MARGIN = 40
    try:
        font_title = ImageFont.truetype(FONT_REG, 40)
        font_item = ImageFont.truetype(FONT_REG, 28)
        font_foot = ImageFont.truetype(FONT_REG, 22)
    except OSError:
        # 字体缺失时用默认字体，避免整个任务崩溃
        font_title = ImageFont.load_default()
        font_item = ImageFont.load_default()
        font_foot = ImageFont.load_default()

    items = news[:12]
    preview = Image.new("RGB", (W, 10))
    pdraw = ImageDraw.Draw(preview)
    heights = []
    content_max = W - MARGIN - 44 - 20  # 与正文实际绘制起点 (MARGIN+44) 对齐
    for item in items:
        lines = _wrap_text(pdraw, item, font_item, content_max)
        heights.append(len(lines) * 40 + 10)

    H = 110 + sum(heights) + 50
    img = Image.new("RGB", (W, H), WHITE)
    draw = ImageDraw.Draw(img)

    draw.text((MARGIN, 34), f"{day.month}月{day.day}日 新闻速览", font=font_title, fill=BLACK)
    draw.line([(MARGIN, 92), (W - MARGIN, 92)], fill=(220, 220, 220), width=2)

    y = 104
    for i, item in enumerate(items, 1):
        lines = _wrap_text(draw, item, font_item, content_max)
        draw.text((MARGIN, y + 5), f"{i}.", font=font_item, fill=BLACK)
        ty = y
        for ln in lines:
            draw.text((MARGIN + 44, ty), ln, font=font_item, fill=BLACK)
            ty += 40
        y += len(lines) * 40 + 10
        if i < len(items):
            draw.line([(MARGIN, y - 5), (W - MARGIN, y - 5)], fill=(220, 220, 220), width=2)

    draw.line([(MARGIN, H - 40), (W - MARGIN, H - 40)], fill=(220, 220, 220), width=2)
    draw.text((MARGIN, H - 34), f"来源：{source} · {time.strftime('%H:%M', time.localtime())}", font=font_foot, fill=GRAY)

    os.makedirs(CACHE_DIR, exist_ok=True)
    cleanup_cache(CACHE_DIR, max_age=3 * 24 * 60 * 60)
    path = os.path.join(CACHE_DIR, f"news_{int(time.time() * 1000)}.png")
    img.save(path, "PNG")
    return path


async def _send_news(day: date) -> None:
    groups = _get_groups()
    if not groups:
        return
    try:
        news = await _fetch_60s(day)
        source = "60秒读懂世界"
        if not news:
            raise RuntimeError("60s 返回空列表")
    except Exception:
        try:
            news = await _fetch_baidu()
            source = "百度热搜"
        except Exception:
            _logger.exception("新闻源全部获取失败")
            return
    if not news:
        _logger.warning("新闻源返回为空")
        return
    try:
        path = await asyncio.to_thread(_render_news_image, day, news, source)
    except Exception:
        _logger.exception("新闻图片渲染失败")
        return
    try:
        bot = get_bot()
    except Exception:
        _logger.exception("获取 bot 失败")
        return
    for gid in groups:
        try:
            await bot.send_group_msg(group_id=int(gid), message=MessageSegment.image("file://" + path))
        except Exception:
            _logger.exception("新闻发送到群 %s 失败", gid)


@scheduler.scheduled_job("cron", hour=5, minute=30, id="daily_news", timezone="Asia/Shanghai")
async def daily_news_job():
    await _send_news(date.today() - timedelta(days=1))


@news_on_cmd.handle()
async def news_on(event: MessageEvent):
    if not is_owner(event):
        await news_on_cmd.finish("❌ 你没有权限使用此功能")
    if not hasattr(event, "group_id"):
        await news_on_cmd.finish("请在有机器人的群里开启此功能")
    _add_group(str(event.group_id))
    await news_on_cmd.finish(f"✅ 本群已开启每日新闻推送\n每天 5:30 自动发送前一天新闻总结到此群")


@news_off_cmd.handle()
async def news_off(event: MessageEvent):
    if not is_owner(event):
        await news_off_cmd.finish("❌ 你没有权限使用此功能")
    if not hasattr(event, "group_id"):
        await news_off_cmd.finish("请在有机器人的群里关闭此功能")
    _remove_group(str(event.group_id))
    await news_off_cmd.finish("✅ 本群已关闭每日新闻推送")


@news_test_cmd.handle()
async def news_test(event: MessageEvent):
    if not is_owner(event):
        await news_test_cmd.finish("❌ 你没有权限使用此功能")
    if not hasattr(event, "group_id"):
        await news_test_cmd.finish("请在群里使用此命令")
    await news_test_cmd.send("⏳ 正在生成新闻图片...")
    try:
        news = await _fetch_60s(date.today() - timedelta(days=1))
        source = "60秒读懂世界"
    except Exception:
        try:
            news = await _fetch_baidu()
            source = "百度热搜"
        except Exception:
            await news_test_cmd.finish("新闻源获取失败，请稍后再试")
    if not news:
        await news_test_cmd.finish("新闻源返回为空，请稍后再试")
    path = await asyncio.to_thread(_render_news_image, date.today() - timedelta(days=1), news, source)
    await news_test_cmd.finish(MessageSegment.image("file://" + path))


@news_status_cmd.handle()
async def news_status(event: MessageEvent):
    if not is_owner(event):
        await news_status_cmd.finish("❌ 你没有权限使用此功能")
    groups = _get_groups()
    if groups:
        await news_status_cmd.finish(f"📰 每日新闻推送已开启于 {len(groups)} 个群（每天 5:30 发送）：\n{'、'.join(groups)}")
    await news_status_cmd.finish("📰 每日新闻推送：未开启")
