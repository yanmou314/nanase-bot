import asyncio
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

from helpers import load_plugin

holiday = load_plugin("holiday_countdown")

TZ = holiday.TIMEZONE


def _dt(y, m, d, hh=10):
    return datetime(y, m, d, hh, 0, tzinfo=TZ)


def test_is_workday():
    assert holiday._is_workday(date(2026, 8, 14)) is True   # 周五
    assert holiday._is_workday(date(2026, 8, 17)) is True   # 周一
    assert holiday._is_workday(date(2026, 8, 15)) is False  # 周六
    assert holiday._is_workday(date(2026, 8, 16)) is False  # 周日
    assert holiday._is_workday(date(2026, 10, 1)) is False  # 国庆节(周四)
    assert holiday._is_workday(date(2026, 10, 2)) is False  # 国庆假期第 2 天
    assert holiday._is_workday(date(2026, 10, 7)) is False  # 国庆假期最后一天
    assert holiday._is_workday(date(2026, 10, 8)) is True   # 假期后首个工作日
    assert holiday._is_workday(date(2026, 2, 16)) is False  # 春节假期（除夕）
    assert holiday._is_workday(date(2026, 2, 23)) is False  # 春节假期最后一天
    assert holiday._is_workday(date(2026, 1, 4)) is True    # 元旦调休上班的周日
    assert holiday._is_workday(date(2026, 2, 14)) is True   # 春节调休上班的周六
    assert holiday._is_workday(date(2026, 10, 10)) is True  # 国庆调休上班的周六
    assert holiday._is_workday(date(2026, 4, 4)) is False   # 清明假期首日
    assert holiday._is_workday(date(2026, 4, 6)) is False   # 清明假期末日


def test_next_workday_start_skips_weekend():
    # 周六下午 → 下周一 08:00
    target = holiday._next_workday_start(_dt(2026, 8, 15, 15))
    assert target.date() == date(2026, 8, 17)
    assert target.time() == datetime.strptime("08:00", "%H:%M").time()
    assert target.tzinfo is not None


def test_next_workday_start_skips_holiday_interval():
    # 国庆当天 → 跳过整个假期到 10-08
    target = holiday._next_workday_start(_dt(2026, 10, 1, 10))
    assert target.date() == date(2026, 10, 8)


def test_next_workday_start_over_spring_festival():
    # 春节假期 2/15-2/23 → 2/24（周二）
    target = holiday._next_workday_start(_dt(2026, 2, 16, 10))
    assert target.date() == date(2026, 2, 24)


def test_work_target_on_workday_is_offwork():
    _, title, detail, remaining = holiday._work_target(_dt(2026, 8, 14, 10))
    assert title == "下班倒计时"
    assert "今天" in detail
    assert "已下班" not in remaining


def test_work_target_after_offwork_is_next_work():
    # 冬季作息 17:00 下班，18:00 应显示下一次上班倒计时而不是“还剩 已下班”
    target, title, _, _ = holiday._work_target(_dt(2026, 8, 14, 18))
    assert title == "上班倒计时"
    assert target.date() == date(2026, 8, 17)  # 周五下班后 → 下周一


def test_work_target_on_weekend_is_next_work():
    target, title, detail, remaining = holiday._work_target(_dt(2026, 8, 15, 10))
    assert title == "上班倒计时"
    assert "8月17日" in detail
    assert target.date() == date(2026, 8, 17)


def test_work_target_on_holiday():
    target, title, _, _ = holiday._work_target(_dt(2026, 10, 1, 10))
    assert title == "上班倒计时"
    # 区间表：整个国庆假期 10-01 ~ 10-07 都放假
    assert target.date() == date(2026, 10, 8)


def test_next_holiday_skips_passed_interval():
    # 国庆假期中（10-03），下一个节假日应是 2027 元旦
    name, day, _, _ = holiday._next_holiday(_dt(2026, 10, 3, 10))
    assert name == "元旦"
    assert day == date(2027, 1, 1)


def test_build_message_contains_sections():
    msg = holiday._build_message(_dt(2026, 8, 16, 9))
    assert "每日倒计时" in msg
    assert "下一个周末" in msg
    assert "下一个节假日" in msg
    assert "上班倒计时" in msg  # 周日
    assert "已下班" not in msg
    msg2 = holiday._build_message(_dt(2026, 8, 14, 9))
    assert "下班倒计时" in msg2


def test_format_date_weekday_label():
    assert "周五" == holiday._format_date(date(2026, 8, 14)).split("（")[1].rstrip("）")
    assert "周日" == holiday._format_date(date(2026, 8, 16)).split("（")[1].rstrip("）")


# ---------------- 部分失败定向补发（T15） ----------------


class _FakeBot:
    def __init__(self, fail_groups: set[int] | None = None):
        self.fail_groups = set(fail_groups or ())
        self.sent: list[int] = []

    async def send_group_msg(self, group_id: int, message=None):
        if group_id in self.fail_groups:
            raise RuntimeError(f"send failed: {group_id}")
        self.sent.append(group_id)
        return {}


def _setup_push_env(monkeypatch, groups: set[int], fail: set[int] | None = None):
    """隔离 STATE_FILE，注入假 bot / 消息构建，并重置推送进行中标记。"""
    tmp = tempfile.mkdtemp()
    state_file = str(Path(tmp) / "state.json")
    monkeypatch.setattr(holiday, "STATE_FILE", state_file)
    monkeypatch.setattr(holiday, "_push_running", False)
    monkeypatch.setattr(holiday, "_retry_day", "")
    # 直接写 groups，避免依赖锁内迁移路径
    from common import save_json_state
    save_json_state(state_file, {"groups": sorted(groups)}, holiday.STATE_LOCK)
    bot = _FakeBot(fail)
    monkeypatch.setattr(holiday, "get_bot", lambda: bot)
    monkeypatch.setattr(holiday, "_build_image_message", lambda: asyncio.sleep(0, result="msg"))
    # 避免真实 scheduler 注册一次性任务
    monkeypatch.setattr(holiday, "_schedule_failed_retry", lambda today: None)
    return bot, state_file


def test_partial_failure_marks_pushed_and_records_failed(monkeypatch):
    bot, state_file = _setup_push_env(monkeypatch, {1, 2, 3}, fail={2})
    ok = asyncio.run(holiday._push_daily_countdown())
    assert ok is True
    day, failed = holiday._push_status()
    assert day == holiday._now().date().isoformat()
    assert failed == [2]
    assert sorted(bot.sent) == [1, 3]


def test_catchup_same_day_only_retries_failed(monkeypatch):
    bot, state_file = _setup_push_env(monkeypatch, {1, 2, 3}, fail={2})
    asyncio.run(holiday._push_daily_countdown())
    assert sorted(bot.sent) == [1, 3]

    # 同日补发：只推失败群 2，成功群 1/3 不再重推
    bot.fail_groups = set()
    bot.sent.clear()
    # 模拟过了 17:00
    now = holiday._now().replace(hour=18, minute=0)
    monkeypatch.setattr(holiday, "_now", lambda: now)
    asyncio.run(holiday._countdown_catchup())
    assert bot.sent == [2]
    day, failed = holiday._push_status()
    assert day == now.date().isoformat()
    assert failed == []


def test_catchup_next_day_full_push(monkeypatch):
    bot, state_file = _setup_push_env(monkeypatch, {1, 2}, fail={2})
    # 先完成一次部分成功推送
    asyncio.run(holiday._push_daily_countdown())
    assert sorted(bot.sent) == [1]
    # 次日 18:00：应全量推送，而不是只补 failed
    tomorrow = holiday._now().replace(hour=18, minute=0) + timedelta(days=1)
    monkeypatch.setattr(holiday, "_now", lambda: tomorrow)
    bot.fail_groups = set()
    bot.sent.clear()
    asyncio.run(holiday._countdown_catchup())
    assert sorted(bot.sent) == [1, 2]
    day, failed = holiday._push_status()
    assert day == tomorrow.date().isoformat()
    assert failed == []


def test_all_success_does_not_record_failed(monkeypatch):
    bot, state_file = _setup_push_env(monkeypatch, {1, 2}, fail=set())
    ok = asyncio.run(holiday._push_daily_countdown())
    assert ok is True
    day, failed = holiday._push_status()
    assert day == holiday._now().date().isoformat()
    assert failed == []
    assert sorted(bot.sent) == [1, 2]
    # 再次推送应直接跳过
    bot.sent.clear()
    ok2 = asyncio.run(holiday._push_daily_countdown())
    assert ok2 is True
    assert bot.sent == []


def test_all_fail_does_not_mark_pushed(monkeypatch):
    bot, state_file = _setup_push_env(monkeypatch, {1, 2}, fail={1, 2})
    ok = asyncio.run(holiday._push_daily_countdown())
    assert ok is False
    day, failed = holiday._push_status()
    assert day == ""
    assert failed == []
