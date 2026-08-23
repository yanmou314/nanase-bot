import asyncio
import time

import pytest
from conftest import Message, MessageEvent, MessageSegment

from helpers import load_plugin

owstats = load_plugin("owstats")


@pytest.fixture(autouse=True)
def _reset_query_state():
    yield
    owstats._last_query.clear()
    owstats._warmup_state["busy"] = False
    owstats._warmup_state["deadline"] = 0.0


def test_summary_timeouts_cover_cold_cache():
    """上游 5 请求/秒限速下冷缓存总结仅排队就可能 20-30 秒，预算必须盖住冷查询。"""
    assert owstats.SUMMARY_TIMEOUTS["today"] >= 60
    assert owstats.SUMMARY_TIMEOUTS["yesterday"] >= 60
    assert owstats.SUMMARY_TIMEOUTS["week"] >= 120
    # 档位随范围递增：本周数据量最大
    assert owstats.SUMMARY_TIMEOUTS["week"] > owstats.SUMMARY_TIMEOUTS["today"]


def test_warmup_player_reports_skip(monkeypatch):
    async def fake_post(path, payload, timeout=90.0):
        return {"ok": False, "error": "summary_busy", "message": "busy"}

    monkeypatch.setattr(owstats, "_post_json", fake_post)
    assert asyncio.run(owstats._warmup_player("Test#1234")) is False


def test_warmup_player_success(monkeypatch):
    async def fake_post(path, payload, timeout=90.0):
        assert path == "/api/v2/dashen-summary/warmup"
        assert payload["bnet_id"] == "Test#1234"
        assert payload["detail_matches"] == owstats.WARMUP_DETAIL_MATCHES
        return {"ok": True, "match_count": 12, "detail_count": 12, "weather_count": 80}

    monkeypatch.setattr(owstats, "_post_json", fake_post)
    assert asyncio.run(owstats._warmup_player("Test#1234")) is True


def test_friendly_error_bnet_not_found_is_chinese_with_id():
    data = {
        "ok": False,
        "error": "bnet_not_found",
        "message": "Could not resolve customerToken from bnet_id: NoSuchPlayer#99999",
        "details": {"query": "NoSuchPlayer#99999"},
    }
    text = owstats._friendly_error(data)
    assert "NoSuchPlayer#99999" in text
    assert "没找到" in text
    # 不再把英文上游报错原样透传给用户
    assert "Could not resolve" not in text


def test_friendly_error_bnet_not_found_without_details():
    text = owstats._friendly_error({"error": "bnet_not_found", "message": "x"})
    assert "没找到" in text
    assert "该 ID" in text


def test_friendly_error_other_codes_are_chinese():
    assert "缺少查询" in owstats._friendly_error({"error": "missing_target", "message": ""})
    assert "稍等" in owstats._friendly_error({"error": "too_many_requests", "message": "Too many requests."})
    assert "稍后再试" in owstats._friendly_error({"error": "summary_busy", "message": ""})
    assert "对局记录" in owstats._friendly_error({"error": "summary_empty", "message": ""})


def test_friendly_error_unknown_code_falls_back():
    assert owstats._friendly_error({"error": "weird_code", "message": ""}) == "查询失败：weird_code"
    assert owstats._friendly_error({}) == "未知错误"


def test_friendly_error_summary_empty_matches_scope():
    week_text = owstats._friendly_error({"error": "summary_empty", "message": ""}, scope="week")
    assert "7 天" in week_text and "24 小时" not in week_text
    yesterday_text = owstats._friendly_error({"error": "summary_empty", "message": ""}, scope="yesterday")
    assert "昨日" in yesterday_text
    assert "24 小时" in owstats._friendly_error({"error": "summary_empty", "message": ""}, scope="today")


def _msg(text):
    return Message([MessageSegment.text(text)])


def test_resolve_tag_flags_invalid_explicit_input():
    ev = MessageEvent(user_id=1)
    # 显式输入但没带 #数字：必须标记为无效，而不是静默回退查自己
    assert owstats._resolve_tag(_msg("张三"), ev) == ("", True)
    # 合法 ID 正常解析
    assert owstats._resolve_tag(_msg("Yanmou#51293"), ev) == ("Yanmou#51293", False)


def test_resolve_tag_falls_back_to_binding(monkeypatch):
    ev = MessageEvent(user_id=42)
    monkeypatch.setattr(owstats, "_get_bound", lambda uid: "Bound#111")
    assert owstats._resolve_tag(_msg("  "), ev) == ("Bound#111", False)
    assert owstats._resolve_tag(_msg("Yanmou#51293 extra"), ev) == ("Yanmou#51293", False)


def test_query_cooldown_blocks_second_call_within_window():
    assert owstats._check_cooldown("u1") == 0.0  # 首次放行并记账
    remain = owstats._check_cooldown("u1")
    assert 0 < remain <= owstats._QUERY_COOLDOWN
    assert owstats._check_cooldown("u2") == 0.0  # 不同用户互不影响


def test_warmup_busy_notice_idle_returns_empty():
    assert owstats._warmup_busy_notice() == ""


def test_warmup_busy_notice_reports_minutes_and_seconds():
    owstats._warmup_state["busy"] = True
    owstats._warmup_state["deadline"] = time.time() + 130
    text = owstats._warmup_busy_notice()
    assert "预热" in text and "分钟" in text
    owstats._warmup_state["deadline"] = time.time() + 8
    assert "秒" in owstats._warmup_busy_notice()


def test_warmup_busy_notice_expires_automatically():
    # deadline 已过：即使 busy 标志因异常没被清掉，也不会永久"正忙"
    owstats._warmup_state["busy"] = True
    owstats._warmup_state["deadline"] = time.time() - 5
    assert owstats._warmup_busy_notice() == ""
    assert owstats._warmup_remaining() == 0.0
