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

FONT_BOLD = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
FONT_REG = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
FONT_FUN = "/usr/share/fonts/custom/ZCOOLKuaiLe-Regular.ttf"

DARK = (60, 55, 80)
GRAY = (150, 145, 165)
ACCENT = (244, 114, 182)
BLUE = (64, 120, 220)

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
    MARGIN = 52
    top, bottom = (255, 241, 248), (233, 245, 255)

    f_title = ImageFont.truetype(FONT_FUN, 48)
    f_date = ImageFont.truetype(FONT_REG, 24)
    f_num = ImageFont.truetype(FONT_BOLD, 26)
    f_item = ImageFont.truetype(FONT_REG, 26)
    f_foot = ImageFont.truetype(FONT_REG, 22)

    items = news[:12]
    preview = Image.new("RGB", (W, 10))
    pdraw = ImageDraw.Draw(preview)
    row_h = 44
    heights = []
    for i, item in enumerate(items, 1):
        lines = _wrap_text(pdraw, item, f_item, W - MARGIN * 2 - 64)
        heights.append(len(lines) * 40 + 8)

    title_h = 120
    list_h = sum(heights)
    foot_h = 70
    H = title_h + list_h + foot_h

    img = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        draw.line([(0, y), (W, y)],
                  fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))

    import random
    rnd = random.Random(11)
    pastel = ["#FFD3E0", "#C9F0FF", "#FFF3C4", "#D8F3DC", "#E7D9FF", "#FFE8D6"]
    for _ in range(60):
        x, y, r = rnd.randint(0, W), rnd.randint(0, title_h + 30), rnd.randint(6, 24)
        draw.ellipse([x - r, y - r, x + r, y + r], fill=pastel[rnd.randint(0, 5)] + f"{rnd.randint(35, 80):02X}")

    draw.text((MARGIN, 40), "今日新闻速览", font=f_title, fill=DARK)
    draw.text((MARGIN, 100),
              f"{day.year}年{day.month}月{day.day}日 · {len(items)} 条 · 来源：{source}",
              font=f_date, fill=GRAY)

    y = title_h + 8
    palette = ["#F15BB5", "#FCA311", "#4ECDC4", "#45B7D1", "#9B5DE5", "#00BBF9",
               "#FF6B6B", "#5FD068", "#FF8C42", "#FEE440", "#96CEB4", "#FFC93C"]
    for i, item in enumerate(items, 1):
        color = palette[(i - 1) % len(palette)]
        box_h = heights[i - 1]
        _round_rect(draw, [MARGIN, y, W - MARGIN, y + box_h], 14, (255, 255, 255, 200))
        num_w = 30
        cx = MARGIN + 26
        cy = y + box_h / 2
        draw.ellipse([cx - 13, cy - 13, cx + 13, cy + 13], fill=color)
        draw.text((cx, cy), str(i), font=f_num, fill=(255, 255, 255), anchor="mm")
        lines = _wrap_text(draw, item, f_item, W - MARGIN * 2 - 64)
        ty = y + (box_h - len(lines) * 38) / 2 + 8
        for ln in lines:
            draw.text((MARGIN + 48, ty), ln, font=f_item, fill=DARK)
            ty += 38
        y += box_h + 8

    draw.line([(MARGIN, H - 46), (W - MARGIN, H - 46)], fill=(210, 205, 225), width=2)
    draw.text((MARGIN, H - 40), f"60秒读懂世界 · {time.strftime('%H:%M')}", font=f_foot, fill=GRAY)

    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"news_{int(time.time() * 1000)}.png")
    img.save(path, "PNG")
    return path


def _round_rect(draw: ImageDraw.ImageDraw, box, radius: int, fill):
    x0, y0, x1, y1 = box
    r = min(radius, (x1 - x0) // 2, (y1 - y0) // 2)
    draw.rounded_rectangle([x0, y0, x1, y1], radius=r, fill=fill)


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
