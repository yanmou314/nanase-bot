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
