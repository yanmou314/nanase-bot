"""`.ai` 免费问答插件测试：注入包裹、空输入、限额、冷却、未配置 key。"""
import asyncio
import sys
import types

import pytest
from conftest import FinishedException, MessageSegment
from helpers import load_plugin

# 移除缓存的旧模块，确保测试反映最新的插件代码
sys.modules.pop("plugin_ai_ask", None)
ai_ask = load_plugin("ai_ask")

OWNER_QQ = 10000  # conftest 固定的测试 owner


def _ev(text: str, uid: int = 20001):
    return types.SimpleNamespace(
        user_id=uid, get_plaintext=lambda: text, group_id=12345,
    )


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    ai_ask._cooldowns.clear()
    monkeypatch.setattr(ai_ask, "_USAGE_FILE", str(tmp_path / "usage.json"))
    yield
    ai_ask._cooldowns.clear()


def _fake_news(key="test-key", model="glm-4-flash"):
    return types.SimpleNamespace(get_glm_config=lambda: (key, model))


def test_ai_ask_usage_on_empty(monkeypatch):
    called = []

    async def boom(*a, **k):
        called.append(1)

    monkeypatch.setattr(ai_ask, "_ask_glm", boom)
    with pytest.raises(FinishedException):
        asyncio.run(ai_ask.ai_cmd.handlers[0](_ev(".ai")))
    assert "用法" in str(ai_ask.ai_cmd.finished[-1])
    assert not called  # 空输入不得发起任何 AI 调用


def test_ai_ask_success_wraps_in_text_segment(monkeypatch):
    async def fake_glm(model, key, messages):
        assert key == "test-key" and model == "glm-4-flash"
        assert messages[-1]["content"] == "今天天气怎么样"
        return "今天是晴天！"

    monkeypatch.setattr(ai_ask, "_news_mod", lambda: _fake_news())
    monkeypatch.setattr(ai_ask, "_ask_glm", fake_glm)
    with pytest.raises(FinishedException):
        asyncio.run(ai_ask.ai_cmd.handlers[0](_ev(".ai 今天天气怎么样")))
    msg = ai_ask.ai_cmd.finished[-1]
    # 防 CQ 注入：必须以 text 段发送，且内容完整
    assert isinstance(msg, MessageSegment) and msg.type == "text"
    assert "今天是晴天！" in str(msg)


def test_ai_ask_injection_stays_literal_text(monkeypatch):
    async def evil_glm(model, key, messages):
        return "[CQ:image,file=evil.jpg]"

    monkeypatch.setattr(ai_ask, "_news_mod", lambda: _fake_news())
    monkeypatch.setattr(ai_ask, "_ask_glm", evil_glm)
    with pytest.raises(FinishedException):
        asyncio.run(ai_ask.ai_cmd.handlers[0](_ev(".ai 你好")))
    msg = ai_ask.ai_cmd.finished[-1]
    assert msg.type == "text"  # 不允许被解析成真实 CQ 码
    assert str(msg) == "[CQ:image,file=evil.jpg]"


def test_ai_ask_long_answer_clipped(monkeypatch):
    async def long_glm(model, key, messages):
        return "长" * 3000

    monkeypatch.setattr(ai_ask, "_news_mod", lambda: _fake_news())
    monkeypatch.setattr(ai_ask, "_ask_glm", long_glm)
    with pytest.raises(FinishedException):
        asyncio.run(ai_ask.ai_cmd.handlers[0](_ev(".ai 写长文")))
    text = str(ai_ask.ai_cmd.finished[-1])
    assert len(text) == ai_ask._CLIP_LEN + len("\n…（回答过长已截断）")


def test_ai_ask_daily_limit_and_owner_exempt(monkeypatch, tmp_path):
    async def fake_glm(model, key, messages):
        return "ok"

    monkeypatch.setattr(ai_ask, "_news_mod", lambda: _fake_news())
    monkeypatch.setattr(ai_ask, "_ask_glm", fake_glm)
    monkeypatch.setattr(ai_ask, "_DAILY_LIMIT", 2)
    day = __import__("time").strftime("%Y%m%d")
    ai_ask._usage_incr("20001", day)
    ai_ask._usage_incr("20001", day)
    handler = ai_ask.ai_cmd.handlers[0]
    # 达限用户被拒
    with pytest.raises(FinishedException):
        asyncio.run(handler(_ev(".ai 问题", uid=20001)))
    assert "上限" in str(ai_ask.ai_cmd.finished[-1])
    # owner 豁免
    with pytest.raises(FinishedException):
        asyncio.run(handler(_ev(".ai 问题", uid=OWNER_QQ)))
    assert "ok" in str(ai_ask.ai_cmd.finished[-1])


def test_ai_ask_cooldown_then_release_on_failure(monkeypatch):
    state = {"n": 0}

    async def flaky_glm(model, key, messages):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("boom")
        return "第二次成功"

    monkeypatch.setattr(ai_ask, "_news_mod", lambda: _fake_news())
    monkeypatch.setattr(ai_ask, "_ask_glm", flaky_glm)
    handler = ai_ask.ai_cmd.handlers[0]
    # 第一次失败：回滚冷却并提示不可用
    with pytest.raises(FinishedException):
        asyncio.run(handler(_ev(".ai 问题")))
    assert "暂时不可用" in str(ai_ask.ai_cmd.finished[-1])
    # 失败后冷却已回滚：可立即重试并成功
    with pytest.raises(FinishedException):
        asyncio.run(handler(_ev(".ai 问题")))
    assert "第二次成功" in str(ai_ask.ai_cmd.finished[-1])


def test_ai_ask_no_key_guides_owner_setting(monkeypatch):
    monkeypatch.setattr(ai_ask, "_news_mod", lambda: _fake_news(key=""))
    with pytest.raises(FinishedException):
        asyncio.run(ai_ask.ai_cmd.handlers[0](_ev(".ai 问题")))
    assert "新闻key" in str(ai_ask.ai_cmd.finished[-1])
