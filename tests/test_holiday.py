from datetime import date, datetime

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


def test_next_workday_start_skips_weekend():
    # 周六下午 → 下周一 08:00
    target = holiday._next_workday_start(_dt(2026, 8, 15, 15))
    assert target.date() == date(2026, 8, 17)
    assert target.time() == datetime.strptime("08:00", "%H:%M").time()
    assert target.tzinfo is not None


def test_work_target_on_workday_is_offwork():
    _, title, detail, remaining = holiday._work_target(_dt(2026, 8, 14, 10))
    assert title == "下班倒计时"
    assert "今天" in detail


def test_work_target_on_weekend_is_next_work():
    target, title, detail, remaining = holiday._work_target(_dt(2026, 8, 15, 10))
    assert title == "上班倒计时"
    assert "8月17日" in detail
    assert target.date() == date(2026, 8, 17)


def test_work_target_on_holiday():
    target, title, _, _ = holiday._work_target(_dt(2026, 10, 1, 10))
    assert title == "上班倒计时"
    # 数据表只收录节假日当天：10-02(周五)被视为工作日（已知限制，见审计报告）
    assert target.date() == date(2026, 10, 2)


def test_build_message_contains_sections():
    msg = holiday._build_message(_dt(2026, 8, 16, 9))
    assert "每日倒计时" in msg
    assert "下一个周末" in msg
    assert "下一个节假日" in msg
    assert "上班倒计时" in msg  # 周日
    msg2 = holiday._build_message(_dt(2026, 8, 14, 9))
    assert "下班倒计时" in msg2


def test_format_date_weekday_label():
    assert "周五" == holiday._format_date(date(2026, 8, 14)).split("（")[1].rstrip("）")
    assert "周日" == holiday._format_date(date(2026, 8, 16)).split("（")[1].rstrip("）")
