"""每日发送下一个周末和法定节假日倒计时。"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time as system_time
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from nonebot import get_bot, on_command
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent, MessageSegment
from nonebot_plugin_apscheduler import scheduler
from PIL import Image, ImageDraw, ImageFont

from common import FONTS, cleanup_cache, is_owner


TIMEZONE = ZoneInfo("Asia/Shanghai")
STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")
CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
STATE_LOCK = threading.RLock()
WEEKDAYS = ("一", "二", "三", "四", "五", "六", "日")
CARD_WIDTH = 1080
CARD_HEIGHT = 920

HOLIDAY_DATES: dict[int, tuple[tuple[str, date], ...]] = {
    2026: (
        ("元旦", date(2026, 1, 1)),
        ("春节", date(2026, 2, 15)),
        ("清明节", date(2026, 4, 4)),
        ("劳动节", date(2026, 5, 1)),
        ("端午节", date(2026, 6, 19)),
        ("中秋节", date(2026, 9, 25)),
        ("国庆节", date(2026, 10, 1)),
    ),
    2027: (
        ("元旦", date(2027, 1, 1)),
        ("春节", date(2027, 2, 5)),
        ("清明节", date(2027, 4, 3)),
        ("劳动节", date(2027, 5, 1)),
        ("端午节", date(2027, 6, 9)),
        ("中秋节", date(2027, 9, 15)),
        ("国庆节", date(2027, 10, 1)),
    ),
    2028: (
        ("元旦", date(2028, 1, 1)),
        ("春节", date(2028, 1, 25)),
        ("清明节", date(2028, 4, 4)),
        ("劳动节", date(2028, 5, 1)),
        ("端午节", date(2028, 5, 30)),
        ("中秋节", date(2028, 10, 3)),
        ("国庆节", date(2028, 10, 1)),
    ),
    2029: (
        ("元旦", date(2029, 1, 1)),
        ("春节", date(2029, 2, 12)),
        ("清明节", date(2029, 4, 4)),
        ("劳动节", date(2029, 5, 1)),
        ("端午节", date(2029, 6, 19)),
        ("中秋节", date(2029, 9, 22)),
        ("国庆节", date(2029, 10, 1)),
    ),
    2030: (
        ("元旦", date(2030, 1, 1)),
        ("春节", date(2030, 2, 2)),
        ("清明节", date(2030, 4, 4)),
        ("劳动节", date(2030, 5, 1)),
        ("端午节", date(2030, 6, 3)),
        ("中秋节", date(2030, 9, 12)),
        ("国庆节", date(2030, 10, 1)),
    ),
}

countdown_cmd = on_command("倒计时", aliases={"周末倒计时"}, priority=5, block=True)
enable_cmd = on_command("倒计时开启", priority=5, block=True)
disable_cmd = on_command("倒计时关闭", priority=5, block=True)
status_cmd = on_command("倒计时状态", priority=5, block=True)
test_cmd = on_command("倒计时测试", priority=5, block=True)


def _now() -> datetime:
    return datetime.now(TIMEZONE)


def _load_groups() -> set[int]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return set()
    groups = data.get("groups", []) if isinstance(data, dict) else []
    result = set()
    for group_id in groups:
        try:
            group_id = int(group_id)
        except (TypeError, ValueError):
            continue
        if group_id > 0:
            result.add(group_id)
    return result


def _save_groups(groups: set[int]) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    temporary = STATE_FILE + ".tmp"
    with open(temporary, "w", encoding="utf-8") as file:
        json.dump({"groups": sorted(groups)}, file, ensure_ascii=False, indent=2)
    os.replace(temporary, STATE_FILE)


def _enabled_groups() -> set[int]:
    with STATE_LOCK:
        return _load_groups()


def _change_group(group_id: int, enabled: bool) -> bool:
    with STATE_LOCK:
        groups = _load_groups()
        changed = (group_id not in groups) if enabled else (group_id in groups)
        if enabled:
            groups.add(group_id)
        else:
            groups.discard(group_id)
        if changed:
            _save_groups(groups)
        return changed


def _next_weekend(now: datetime) -> tuple[date, datetime]:
    days_until_saturday = (5 - now.weekday()) % 7
    weekend_day = now.date() + timedelta(days=days_until_saturday)
    target = datetime.combine(weekend_day, time.min, tzinfo=TIMEZONE)
    if target <= now:
        weekend_day += timedelta(days=7)
        target = datetime.combine(weekend_day, time.min, tzinfo=TIMEZONE)
    return weekend_day, target


def _holiday_dates(year: int) -> tuple[tuple[str, date], ...]:
    if year in HOLIDAY_DATES:
        return HOLIDAY_DATES[year]
    return (
        ("元旦", date(year, 1, 1)),
        ("劳动节", date(year, 5, 1)),
        ("国庆节", date(year, 10, 1)),
    )


def _next_holiday(now: datetime) -> tuple[str, date, datetime]:
    for year in range(now.year, now.year + 4):
        for name, holiday_day in sorted(_holiday_dates(year), key=lambda item: item[1]):
            target = datetime.combine(holiday_day, time.min, tzinfo=TIMEZONE)
            if target > now:
                return name, holiday_day, target
    raise RuntimeError("holiday calendar has no future date")


def _remaining(target: datetime, now: datetime) -> str:
    seconds = max(0, int((target - now).total_seconds()))
    minutes = (seconds + 59) // 60
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days}天{hours}小时{minutes}分钟"
    if hours:
        return f"{hours}小时{minutes}分钟"
    return f"{minutes}分钟"


def _format_date(value: date) -> str:
    return f"{value.month}月{value.day}日（周{WEEKDAYS[value.weekday()]}）"


def _offwork_target(now: datetime) -> tuple[datetime, str]:
    late_period = date(now.year, 5, 1) <= now.date() <= date(now.year, 10, 1)
    offwork_time = time(17, 30) if late_period else time(17, 0)
    return datetime.combine(now.date(), offwork_time, tzinfo=TIMEZONE), offwork_time.strftime("%H:%M")


def _remaining_or_done(target: datetime, now: datetime) -> str:
    if now >= target:
        return "已下班"
    return _remaining(target, now)


def _build_message(now: datetime | None = None) -> str:
    now = now or _now()
    weekend_day, weekend_at = _next_weekend(now)
    holiday_name, holiday_day, holiday_at = _next_holiday(now)
    offwork_at, offwork_label = _offwork_target(now)
    return (
        "⏳ 每日倒计时\n"
        f"今天是 {now.year}年{now.month}月{now.day}日 {now:%H:%M}\n\n"
        f"🏖️ 下一个周末：{_format_date(weekend_day)} 00:00\n"
        f"还剩 {_remaining(weekend_at, now)}\n\n"
        f"🎉 下一个节假日：{holiday_name} · {_format_date(holiday_day)}\n"
        f"还剩 {_remaining(holiday_at, now)}\n\n"
        f"💼 下班倒计时：今天 {offwork_label} 下班\n"
        f"还剩 {_remaining_or_done(offwork_at, now)}\n\n"
        "注：节假日按放假起始日计算，日期表会随官方安排更新。"
    )


def _font(size: int, bold: bool = False):
    key = "noto_bold" if bold else "noto_reg"
    try:
        return ImageFont.truetype(FONTS[key], size)
    except (OSError, TypeError, KeyError):
        return ImageFont.load_default()


def _right_text(draw: ImageDraw.ImageDraw, text: str, y: int, font, fill) -> None:
    box = draw.textbbox((0, 0), text, font=font)
    width = box[2] - box[0]
    draw.text((CARD_WIDTH - 64 - width, y), text, font=font, fill=fill)


def _render_card(now: datetime) -> str:
    weekend_day, weekend_at = _next_weekend(now)
    holiday_name, holiday_day, holiday_at = _next_holiday(now)
    offwork_at, offwork_label = _offwork_target(now)
    image = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT))
    draw = ImageDraw.Draw(image, "RGBA")
    top = (249, 247, 255)
    bottom = (232, 246, 255)
    for y in range(CARD_HEIGHT):
        ratio = y / max(1, CARD_HEIGHT - 1)
        color = tuple(int(top[i] + (bottom[i] - top[i]) * ratio) for i in range(3))
        draw.line([(0, y), (CARD_WIDTH, y)], fill=color)
    draw.ellipse([CARD_WIDTH - 180, -100, CARD_WIDTH + 80, 160], fill=(255, 220, 150, 90))
    draw.ellipse([-100, CARD_HEIGHT - 150, 180, CARD_HEIGHT + 130], fill=(183, 226, 255, 100))

    title_font = _font(48, bold=True)
    date_font = _font(24)
    label_font = _font(23, bold=True)
    name_font = _font(34, bold=True)
    detail_font = _font(25)
    remaining_font = _font(36, bold=True)
    footer_font = _font(19)
    dark = (42, 39, 55, 255)
    gray = (123, 118, 139, 255)
    weekend_color = (65, 155, 196, 255)
    holiday_color = (217, 140, 71, 255)
    offwork_color = (103, 157, 111, 255)

    draw.text((64, 42), "每日倒计时", font=title_font, fill=dark)
    draw.text((66, 105), f"{now.year}年{now.month}月{now.day}日 {now:%H:%M} · 中国标准时间", font=date_font, fill=gray)

    def draw_panel(top_y: int, label: str, name: str, detail: str, remaining: str, accent) -> None:
        draw.rounded_rectangle([48, top_y, CARD_WIDTH - 48, top_y + 190], radius=28, fill=(255, 255, 255, 232))
        draw.rounded_rectangle([48, top_y, 60, top_y + 190], radius=6, fill=accent)
        draw.text((82, top_y + 24), label, font=label_font, fill=accent)
        draw.text((82, top_y + 67), name, font=name_font, fill=dark)
        draw.text((82, top_y + 122), detail, font=detail_font, fill=gray)
        _right_text(draw, "还剩", top_y + 42, detail_font, gray)
        _right_text(draw, remaining, top_y + 77, remaining_font, accent)

    draw_panel(
        155,
        "WEEKEND",
        "下一个周末",
        f"{_format_date(weekend_day)} 00:00",
        _remaining(weekend_at, now),
        weekend_color,
    )
    draw_panel(
        375,
        "HOLIDAY",
        f"下一个节假日 · {holiday_name}",
        f"{_format_date(holiday_day)} 00:00",
        _remaining(holiday_at, now),
        holiday_color,
    )
    draw_panel(
        595,
        "OFF WORK",
        "下班倒计时",
        f"今天下班时间 {offwork_label}",
        _remaining_or_done(offwork_at, now),
        offwork_color,
    )
    footer = "5月1日—10月1日 17:30 下班 · 其余日期 17:00 下班 · 每天 17:00 自动发送"
    footer_box = draw.textbbox((0, 0), footer, font=footer_font)
    draw.text(((CARD_WIDTH - (footer_box[2] - footer_box[0])) / 2, 845), footer, font=footer_font, fill=gray)

    os.makedirs(CACHE_DIR, exist_ok=True)
    cleanup_cache(CACHE_DIR, max_age=3 * 24 * 60 * 60)
    path = os.path.join(CACHE_DIR, f"countdown_{int(system_time.time() * 1000)}.png")
    image.save(path, "PNG")
    return path


async def _build_image_message() -> MessageSegment | str:
    now = _now()
    try:
        path = await asyncio.to_thread(_render_card, now)
        return MessageSegment.image("file://" + path)
    except Exception:
        return _build_message(now)


@countdown_cmd.handle()
async def countdown(event: MessageEvent):
    await countdown_cmd.finish(await _build_image_message())


@enable_cmd.handle()
async def enable(event: MessageEvent):
    if not is_owner(event):
        await enable_cmd.finish("❌ 你没有权限使用此功能")
    if not isinstance(event, GroupMessageEvent):
        await enable_cmd.finish("请在需要接收推送的群里使用此命令")
    changed = _change_group(event.group_id, True)
    if changed:
        await enable_cmd.finish("✅ 本群已开启每日倒计时推送，每天 17:00 发送")
    await enable_cmd.finish("ℹ️ 本群已经开启每日倒计时推送")


@disable_cmd.handle()
async def disable(event: MessageEvent):
    if not is_owner(event):
        await disable_cmd.finish("❌ 你没有权限使用此功能")
    if not isinstance(event, GroupMessageEvent):
        await disable_cmd.finish("请在需要关闭推送的群里使用此命令")
    changed = _change_group(event.group_id, False)
    if changed:
        await disable_cmd.finish("✅ 本群已关闭每日倒计时推送")
    await disable_cmd.finish("ℹ️ 本群本来就没有开启每日倒计时推送")


@status_cmd.handle()
async def status(event: MessageEvent):
    if not is_owner(event):
        await status_cmd.finish("❌ 你没有权限使用此功能")
    groups = _enabled_groups()
    if isinstance(event, GroupMessageEvent):
        current = "已开启" if event.group_id in groups else "未开启"
        await status_cmd.finish(f"⏳ 本群倒计时推送：{current}\n当前共有 {len(groups)} 个群开启推送")
    await status_cmd.finish(f"⏳ 当前共有 {len(groups)} 个群开启每日 17:00 倒计时推送")


@test_cmd.handle()
async def test(event: MessageEvent):
    if not is_owner(event):
        await test_cmd.finish("❌ 你没有权限使用此功能")
    if not isinstance(event, GroupMessageEvent):
        await test_cmd.finish("请在群里使用此命令")
    await test_cmd.finish(await _build_image_message())


@scheduler.scheduled_job("cron", hour=17, minute=0, id="daily_holiday_countdown", timezone="Asia/Shanghai")
async def daily_holiday_countdown_job():
    groups = _enabled_groups()
    if not groups:
        return
    message = await _build_image_message()
    bot = get_bot()
    for group_id in groups:
        try:
            await bot.send_group_msg(group_id=group_id, message=message)
        except Exception:
            continue
