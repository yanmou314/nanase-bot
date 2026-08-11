"""每日发送下一个周末和法定节假日倒计时。"""
from __future__ import annotations

import json
import os
import threading
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from nonebot import get_bot, on_command
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent
from nonebot_plugin_apscheduler import scheduler

from common import is_owner


TIMEZONE = ZoneInfo("Asia/Shanghai")
STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")
STATE_LOCK = threading.RLock()
WEEKDAYS = ("一", "二", "三", "四", "五", "六", "日")

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


def _build_message(now: datetime | None = None) -> str:
    now = now or _now()
    weekend_day, weekend_at = _next_weekend(now)
    holiday_name, holiday_day, holiday_at = _next_holiday(now)
    return (
        "⏳ 每日倒计时\n"
        f"今天是 {now.year}年{now.month}月{now.day}日 {now:%H:%M}\n\n"
        f"🏖️ 下一个周末：{_format_date(weekend_day)} 00:00\n"
        f"还剩 {_remaining(weekend_at, now)}\n\n"
        f"🎉 下一个节假日：{holiday_name} · {_format_date(holiday_day)}\n"
        f"还剩 {_remaining(holiday_at, now)}\n\n"
        "注：节假日按放假起始日计算，日期表会随官方安排更新。"
    )


@countdown_cmd.handle()
async def countdown(event: MessageEvent):
    await countdown_cmd.finish(_build_message())


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
    await test_cmd.finish(_build_message())


@scheduler.scheduled_job("cron", hour=17, minute=0, id="daily_holiday_countdown", timezone="Asia/Shanghai")
async def daily_holiday_countdown_job():
    groups = _enabled_groups()
    if not groups:
        return
    message = _build_message()
    bot = get_bot()
    for group_id in groups:
        try:
            await bot.send_group_msg(group_id=group_id, message=message)
        except Exception:
            continue
