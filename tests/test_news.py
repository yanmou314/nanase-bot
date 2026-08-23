import random
from datetime import date

from helpers import load_plugin

news = load_plugin("news")


def test_lunar_line_solar_term():
    # 2026-08-23 为农历七月十一、处暑节气
    line = news._lunar_line(date(2026, 8, 23))
    assert "七月十一" in line
    assert "处暑" in line


def test_lunar_line_starts_with_lunar_date():
    line = news._lunar_line(date(2026, 8, 20))
    assert line.startswith("农历")


def test_build_greeting_weekend():
    random.seed(1)
    g = news._build_greeting(date(2026, 8, 23), quote="若心如朝阳，所见皆朝霞。")
    assert g.startswith("早安。若心如朝阳，所见皆朝霞。")
    assert "今天是" not in g  # 日期在消息首行另行展示
    assert any(w in g for w in news._WEEKEND_WISHES)
    assert any(c in g for c in news._CLOSINGS)


def test_build_greeting_weekday():
    random.seed(1)
    g = news._build_greeting(date(2026, 8, 24), quote="心有暖阳。")
    assert g.startswith("早安。心有暖阳。")
    assert any(w in g for w in news._WEEKDAY_WISHES)


def test_build_greeting_legal_holiday():
    random.seed(1)
    g = news._build_greeting(date(2026, 10, 1), quote="", festival="国庆节", legal=True)
    assert any(w in g for w in news._HOLIDAY_WISHES)


def test_build_greeting_normal_festival_uses_weekday_wish():
    # 情人节 2026-02-14 恰为周六 → 周末祝愿；普通节日不占用假日文案
    random.seed(1)
    g = news._build_greeting(date(2026, 2, 14), quote="", festival="情人节", legal=False)
    assert any(w in g for w in news._WEEKEND_WISHES)


def test_build_greeting_fallback_quote():
    random.seed(2)
    g = news._build_greeting(date(2026, 8, 23), quote="")
    assert any(q in g for q in news._FALLBACK_QUOTES)


def test_parse_hitokoto():
    assert news._parse_hitokoto({"hitokoto": " 你好 "}) == "你好"
    assert news._parse_hitokoto({}) == ""


def test_greeting_messages_context():
    req = news._greeting_messages(date(2026, 8, 23), "农历七月十一 · 节气 处暑", "")[1]["content"]
    assert "2026年8月23日" in req
    assert "星期日" in req
    assert "处暑" in req
    assert "周末" in req
    assert "以「早安。」开头" in req  # 问候不再自带日期前缀


def test_greeting_messages_holiday_priority():
    req = news._greeting_messages(date(2026, 10, 1), "农历八月廿一", "国庆节", True)[1]["content"]
    assert "国庆节" in req
    assert "法定假日" in req


def test_greeting_messages_festival_context():
    req = news._greeting_messages(date(2026, 2, 14), "农历正月廿七", "情人节", False)[1]["content"]
    assert "情人节" in req


def test_lunar_info_detects_festivals():
    cases = [
        (date(2026, 2, 14), "情人节", False),   # 公历节日（cnlunar）
        (date(2026, 6, 21), "父亲节", False),   # 规则型周日节日（cnlunar）
        (date(2026, 8, 19), "七夕", False),     # 农历七月初七（本地补充）
        (date(2026, 11, 26), "感恩节", False),  # 11 月第 4 个周四（本地补充）
        (date(2026, 10, 31), "万圣节", False),  # 公历固定（本地补充）
        (date(2026, 10, 1), "国庆节", True),    # 法定假日
        (date(2027, 2, 5), "除夕", False),      # 腊月廿九且次日春节（本地补充）
    ]
    for day, festival, legal in cases:
        _, got_festival, got_legal = news._lunar_info(day)
        assert got_festival == festival, f"{day}: {got_festival!r} != {festival!r}"
        assert got_legal is legal


def test_lunar_info_no_festival_on_plain_day():
    _, festival, legal = news._lunar_info(date(2026, 8, 23))
    assert festival == ""
    assert legal is False


def test_sanitize_greeting():
    assert news._sanitize_greeting("  「你好呀。」\n第二行") == "你好呀。"
    assert news._sanitize_greeting("“早安”") == "早安"
    assert news._sanitize_greeting("早安。") == "早安。"
    assert news._sanitize_greeting("") == ""
    assert news._sanitize_greeting("   ") == ""
