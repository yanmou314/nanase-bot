import json
import os
import threading
import time
from datetime import date, timedelta

import httpx
from nonebot import get_bot, on_command
from nonebot.adapters.onebot.v11 import MessageEvent, MessageSegment
from nonebot_plugin_apscheduler import scheduler
from PIL import Image, ImageDraw, ImageFont

from common import is_owner

STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")
CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
_LOCK = threading.Lock()

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


def _load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(data: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def _get_group() -> str:
    with _LOCK:
        return _load_state().get("group_id", "")


def _set_group(gid: str) -> None:
    with _LOCK:
        state = _load_state()
        state["group_id"] = gid
        _save_state(state)


def _clear_group() -> None:
    with _LOCK:
        state = _load_state()
        state.pop("group_id", None)
        _save_state(state)


async def _fetch_60s(day: date) -> list[str]:
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(NEWS_API, params={"date": day.isoformat()})
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 200:
            raise RuntimeError(data.get("message", "unknown"))
        news = (data.get("data") or {}).get("news") or []
        return [str(x) for x in news]


async def _fetch_baidu() -> list[str]:
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "Mozilla/5.0"}) as client:
        r = await client.get(BAIDU_API)
        r.raise_for_status()
        data = r.json()
        items = []
        for card in data.get("data", {}).get("cards", []):
            for content in card.get("content", []):
                for word in content.get("content", []):
                    if word.get("query"):
                        items.append(word["query"])
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
    font_title = ImageFont.truetype(FONT_REG, 40)
    font_item = ImageFont.truetype(FONT_REG, 28)
    font_foot = ImageFont.truetype(FONT_REG, 22)

    items = news[:12]
    preview = Image.new("RGB", (W, 10))
    pdraw = ImageDraw.Draw(preview)
    heights = []
    for item in items:
        lines = _wrap_text(pdraw, item, font_item, W - MARGIN * 2 - 20)
        heights.append(len(lines) * 40 + 10)

    H = 110 + sum(heights) + 50
    img = Image.new("RGB", (W, H), WHITE)
    draw = ImageDraw.Draw(img)

    draw.text((MARGIN, 34), f"{day.month}月{day.day}日 新闻速览", font=font_title, fill=BLACK)
    draw.line([(MARGIN, 92), (W - MARGIN, 92)], fill=(220, 220, 220), width=2)

    y = 104
    for i, item in enumerate(items, 1):
        lines = _wrap_text(draw, item, font_item, W - MARGIN * 2 - 20)
        draw.text((MARGIN, y + 5), f"{i}.", font=font_item, fill=BLACK)
        ty = y
        for ln in lines:
            draw.text((MARGIN + 44, ty), ln, font=font_item, fill=BLACK)
            ty += 40
        y += len(lines) * 40 + 10
        if i < len(items):
            draw.line([(MARGIN, y - 5), (W - MARGIN, y - 5)], fill=(220, 220, 220), width=2)

    draw.line([(MARGIN, H - 40), (W - MARGIN, H - 40)], fill=(220, 220, 220), width=2)
    draw.text((MARGIN, H - 34), f"来源：{source} · {time.strftime('%H:%M')}", font=font_foot, fill=GRAY)

    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"news_{int(time.time() * 1000)}.png")
    img.save(path, "PNG")
    return path


async def _send_news(day: date) -> None:
    group_id = _get_group()
    if not group_id:
        return
    try:
        news = await _fetch_60s(day)
        source = "60秒读懂世界"
    except Exception:
        try:
            news = await _fetch_baidu()
            source = "百度热搜"
        except Exception:
            return
    if not news:
        return
    path = await __import__("asyncio").to_thread(_render_news_image, day, news, source)
    bot = get_bot()
    await bot.send_group_msg(group_id=int(group_id), message=MessageSegment.image("file://" + path))


@scheduler.scheduled_job("cron", hour=9, minute=0, id="daily_news", timezone="Asia/Shanghai")
async def daily_news_job():
    await _send_news(date.today() - timedelta(days=1))


@news_on_cmd.handle()
async def news_on(event: MessageEvent):
    if not is_owner(event):
        await news_on_cmd.finish("❌ 你没有权限使用此功能")
    if not hasattr(event, "group_id"):
        await news_on_cmd.finish("请在有机器人的群里开启此功能")
    _set_group(str(event.group_id))
    await news_on_cmd.finish(f"✅ 每日新闻已开启\n每天 9:00 自动发送前一天新闻总结到此群（本群 {event.group_id}）")


@news_off_cmd.handle()
async def news_off(event: MessageEvent):
    if not is_owner(event):
        await news_off_cmd.finish("❌ 你没有权限使用此功能")
    _clear_group()
    await news_off_cmd.finish("✅ 每日新闻已关闭")


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
    path = await __import__("asyncio").to_thread(_render_news_image, date.today() - timedelta(days=1), news, source)
    await news_test_cmd.finish(MessageSegment.image("file://" + path))


@news_status_cmd.handle()
async def news_status(event: MessageEvent):
    if not is_owner(event):
        await news_status_cmd.finish("❌ 你没有权限使用此功能")
    gid = _get_group()
    if gid:
        await news_status_cmd.finish(f"📰 每日新闻：已开启（群 {gid}，每天 9:00 发送）")
    await news_status_cmd.finish("📰 每日新闻：未开启")
