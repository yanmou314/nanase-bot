"""每日发送下一个周末和法定节假日倒计时。"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time as system_time
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from nonebot import get_bot, get_driver, on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent, MessageSegment
from nonebot_plugin_apscheduler import scheduler
from PIL import Image, ImageDraw, ImageFont

from common import FONTS, cleanup_cache, is_owner, load_json_state, save_json_state


_logger = logging.getLogger(__name__)
TIMEZONE = ZoneInfo("Asia/Shanghai")
STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")
CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
STATE_LOCK = threading.RLock()
WEEKDAYS = ("一", "二", "三", "四", "五", "六", "日")
CARD_WIDTH = 1080
CARD_HEIGHT = 840
WORK_START = time(8, 0)  # 上班时间（放假/周末时倒计时到此时间）
PUSH_TIME = time(17, 0)  # 每日推送时刻（与 daily_holiday_countdown_job 的 cron 保持一致）

# 放假区间表：(名称, 放假首日, 放假末日)。
# 2026 为国务院办公厅官方安排；2027-2030 为按近年惯例推算的基线
# （春节=除夕至初七、劳动节 5 天、国庆 7 天、其余 3 天连休），待官方通知发布后更新。
HOLIDAYS: dict[int, tuple[tuple[str, date, date], ...]] = {
    2026: (
        ("元旦", date(2026, 1, 1), date(2026, 1, 3)),
        ("春节", date(2026, 2, 15), date(2026, 2, 23)),
        ("清明节", date(2026, 4, 4), date(2026, 4, 6)),
        ("劳动节", date(2026, 5, 1), date(2026, 5, 5)),
        ("端午节", date(2026, 6, 19), date(2026, 6, 21)),
        ("中秋节", date(2026, 9, 25), date(2026, 9, 27)),
        ("国庆节", date(2026, 10, 1), date(2026, 10, 7)),
    ),
    2027: (
        ("元旦", date(2027, 1, 1), date(2027, 1, 3)),
        ("春节", date(2027, 2, 5), date(2027, 2, 12)),
        ("清明节", date(2027, 4, 3), date(2027, 4, 5)),
        ("劳动节", date(2027, 5, 1), date(2027, 5, 5)),
        ("端午节", date(2027, 6, 9), date(2027, 6, 11)),
        ("中秋节", date(2027, 9, 15), date(2027, 9, 17)),
        ("国庆节", date(2027, 10, 1), date(2027, 10, 7)),
    ),
    2028: (
        ("元旦", date(2028, 1, 1), date(2028, 1, 3)),
        ("春节", date(2028, 1, 25), date(2028, 2, 1)),
        ("清明节", date(2028, 4, 4), date(2028, 4, 6)),
        ("劳动节", date(2028, 5, 1), date(2028, 5, 5)),
        ("端午节", date(2028, 5, 30), date(2028, 6, 1)),
        ("国庆节·中秋", date(2028, 10, 1), date(2028, 10, 7)),  # 中秋(10-3)落在国庆周内，合并连休
    ),
    2029: (
        ("元旦", date(2029, 1, 1), date(2029, 1, 3)),
        ("春节", date(2029, 2, 12), date(2029, 2, 19)),
        ("清明节", date(2029, 4, 4), date(2029, 4, 6)),
        ("劳动节", date(2029, 5, 1), date(2029, 5, 5)),
        ("端午节", date(2029, 6, 19), date(2029, 6, 21)),
        ("中秋节", date(2029, 9, 22), date(2029, 9, 24)),
        ("国庆节", date(2029, 10, 1), date(2029, 10, 7)),
    ),
    2030: (
        ("元旦", date(2030, 1, 1), date(2030, 1, 3)),
        ("春节", date(2030, 2, 2), date(2030, 2, 9)),
        ("清明节", date(2030, 4, 4), date(2030, 4, 6)),
        ("劳动节", date(2030, 5, 1), date(2030, 5, 5)),
        ("端午节", date(2030, 6, 3), date(2030, 6, 5)),
        ("中秋节", date(2030, 9, 12), date(2030, 9, 14)),
        ("国庆节", date(2030, 10, 1), date(2030, 10, 7)),
    ),
}

# 调休上班的周末（官方通知中标注“上班”的周六/周日），仅 2026 有官方数据
WORKDAY_OVERRIDES: dict[int, tuple[date, ...]] = {
    2026: (
        date(2026, 1, 4),   # 元旦调休
        date(2026, 2, 14),  # 春节调休
        date(2026, 2, 28),  # 春节调休
        date(2026, 5, 9),   # 劳动节调休
        date(2026, 9, 20),  # 国庆调休
        date(2026, 10, 10),  # 国庆调休
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
    data = load_json_state(STATE_FILE, STATE_LOCK)
    groups = data.get("groups", []) if isinstance(data.get("groups"), list) else []
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
    # 只更新 groups，保留同文件里的其他键（如 last_push_date）
    data = load_json_state(STATE_FILE, STATE_LOCK)
    data["groups"] = sorted(groups)
    save_json_state(STATE_FILE, data, STATE_LOCK)


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


def _last_push_date() -> str:
    """读取最近一次倒计时推送日期（YYYY-MM-DD，上海时区）；从未推送返回空串。"""
    with STATE_LOCK:
        return str(load_json_state(STATE_FILE, STATE_LOCK).get("last_push_date") or "")


def _mark_pushed(day: date) -> None:
    """记录当日倒计时已推送完成，用于防重复推送与启动补发判断。"""
    with STATE_LOCK:
        data = load_json_state(STATE_FILE, STATE_LOCK)
        data["last_push_date"] = day.isoformat()
        save_json_state(STATE_FILE, data, STATE_LOCK)


def _last_workday_before(value: date) -> date:
    day = value - timedelta(days=1)
    while not _is_workday(day):
        day -= timedelta(days=1)
    return day


def _offwork_on(day: date) -> datetime:
    late_period = date(day.year, 5, 1) <= day <= date(day.year, 10, 1)
    offwork_time = time(17, 30) if late_period else time(17, 0)
    return datetime.combine(day, offwork_time, tzinfo=TIMEZONE)


def _next_weekend(now: datetime) -> tuple[date, date, datetime]:
    days_until_saturday = (5 - now.weekday()) % 7
    weekend_day = now.date() + timedelta(days=days_until_saturday)
    start_day = _last_workday_before(weekend_day)
    target = _offwork_on(start_day)
    if target <= now:
        weekend_day += timedelta(days=7)
        start_day = _last_workday_before(weekend_day)
        target = _offwork_on(start_day)
    return weekend_day, start_day, target


_holidays_warned: set[int] = set()


def _holidays(year: int) -> tuple[tuple[str, date, date], ...]:
    if year in HOLIDAYS:
        return HOLIDAYS[year]
    if year not in _holidays_warned:
        _holidays_warned.add(year)
        _logger.warning("节假日表未覆盖 %s 年，降级为元旦/劳动节/国庆基线，请补充官方安排", year)
    return (
        ("元旦", date(year, 1, 1), date(year, 1, 3)),
        ("劳动节", date(year, 5, 1), date(year, 5, 5)),
        ("国庆节", date(year, 10, 1), date(year, 10, 7)),
    )


def _next_holiday(now: datetime) -> tuple[str, date, date, datetime]:
    for year in range(now.year, now.year + 4):
        for name, start_day_h, _end in sorted(_holidays(year), key=lambda item: item[1]):
            start_day = _last_workday_before(start_day_h)
            target = _offwork_on(start_day)
            if target > now:
                return name, start_day_h, start_day, target
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


def _is_workday(day: date) -> bool:
    if day in WORKDAY_OVERRIDES.get(day.year, ()):  # 调休上班的周末
        return True
    if day.weekday() >= 5:  # 周末
        return False
    for _, start, end in _holidays(day.year):
        if start <= day <= end:
            return False
    return True


def _next_workday_start(now: datetime) -> datetime:
    day = now.date() + timedelta(days=1)
    while not _is_workday(day):
        day += timedelta(days=1)
    return datetime.combine(day, WORK_START, tzinfo=TIMEZONE)


def _work_target(now: datetime) -> tuple[datetime, str, str, str]:
    """返回 (目标时间, 标题, 详情, 剩余文案)；放假/周末/下班后显示下一次上班倒计时。"""
    if _is_workday(now.date()):
        target = _offwork_on(now.date())
        if now < target:
            return target, "下班倒计时", f"今天 {target:%H:%M} 下班", _remaining(target, now)
    target = _next_workday_start(now)
    return target, "上班倒计时", f"下次上班 {_format_date(target.date())} {target:%H:%M}", _remaining(target, now)


def _build_message(now: datetime | None = None) -> str:
    now = now or _now()
    weekend_day, _, weekend_at = _next_weekend(now)
    holiday_name, holiday_day, _, holiday_at = _next_holiday(now)
    work_at, work_title, work_detail, work_remaining = _work_target(now)
    return (
        "⏳ 每日倒计时\n"
        f"今天是 {now.year}年{now.month}月{now.day}日 {now:%H:%M}\n\n"
        f"🏖️ 下一个周末：{_format_date(weekend_day)}\n"
        f"还剩 {_remaining(weekend_at, now)}\n\n"
        f"🎉 下一个节假日：{holiday_name} · {_format_date(holiday_day)}\n"
        f"还剩 {_remaining(holiday_at, now)}\n\n"
        f"💼 {work_title}：{work_detail}\n"
        f"还剩 {work_remaining}\n\n"
        "注：周末/节假日按最后一个工作日的下班时间起算，节假日日期表会随官方安排更新。"
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
    weekend_day, _, weekend_at = _next_weekend(now)
    holiday_name, holiday_day, _, holiday_at = _next_holiday(now)
    _, work_title, work_detail, work_remaining = _work_target(now)
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
        f"{_format_date(weekend_day)}",
        _remaining(weekend_at, now),
        weekend_color,
    )
    draw_panel(
        375,
        "HOLIDAY",
        f"下一个节假日 · {holiday_name}",
        f"{_format_date(holiday_day)}",
        _remaining(holiday_at, now),
        holiday_color,
    )
    draw_panel(
        595,
        "OFF WORK" if work_title == "下班倒计时" else "TO WORK",
        work_title,
        work_detail,
        work_remaining,
        offwork_color,
    )

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
        _logger.warning("倒计时卡片渲染失败，回退文本消息", exc_info=True)
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


_push_running = False  # 推送进行中标记：定时触发与启动补发恰好并发时只跑一路


async def _push_daily_countdown() -> bool:
    """推送当日倒计时到所有开启群；至少一个群送达才记录 last_push_date 并返回 True。"""
    global _push_running
    today = _now().date().isoformat()
    if _last_push_date() == today:
        return True  # 今日已推送（定时与补发共用此标记，防重复）
    if _push_running:  # 另一路正在推送本次倒计时，无需重复
        return False
    _push_running = True
    try:
        if _last_push_date() == today:  # 双重检查：进入前另一路可能刚好推完
            return True
        groups = _enabled_groups()
        if not groups:
            return False
        try:
            message = await _build_image_message()
        except Exception:
            _logger.exception("倒计时每日消息构建失败")
            return False
        try:
            bot = get_bot()
        except Exception:
            _logger.warning("倒计时推送时机器人未连接，本次跳过")
            return False
        # 并发发送：单个群失败不影响其他群
        results = await asyncio.gather(
            *(bot.send_group_msg(group_id=gid, message=message) for gid in groups),
            return_exceptions=True,
        )
        sent = 0
        for gid, result in zip(groups, results):
            if isinstance(result, BaseException):
                _logger.warning("倒计时推送到群 %s 失败", gid, exc_info=result)
            else:
                sent += 1
        if sent:
            _mark_pushed(_now().date())  # 有群成功送达才记录，全失败保留补发机会
        return bool(sent)
    finally:
        _push_running = False


@scheduler.scheduled_job("cron", hour=PUSH_TIME.hour, minute=PUSH_TIME.minute, id="daily_holiday_countdown", timezone="Asia/Shanghai")
async def daily_holiday_countdown_job():
    if _last_push_date() == _now().date().isoformat():
        return  # 今日已推送（如启动补发已执行过），防重复
    await _push_daily_countdown()


# 启动补发：APScheduler 用内存 jobstore，进程重启后错过的当日推送静默丢失，
# bot 连上后检查"已过推送时刻且今日未推送"则立即补发一次。
# 优先 on_bot_connect（此时 get_bot 可用）；无该钩子的环境（旧版 nonebot / 测试 stub）回退 on_startup。
_register_catchup = getattr(get_driver(), "on_bot_connect", get_driver().on_startup)


@_register_catchup
async def _countdown_catchup(bot: Bot) -> None:
    now = _now()
    if (now.hour, now.minute) < (PUSH_TIME.hour, PUSH_TIME.minute):
        return  # 还没到当日推送时刻，交给定时任务
    if _last_push_date() >= now.date().isoformat():
        return  # 今日已推送（last_push_date 为今天或更晚），不重复
    await _push_daily_countdown()
