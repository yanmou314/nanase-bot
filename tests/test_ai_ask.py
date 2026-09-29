"""`.ai` 多供应商问答插件测试：注入包裹、空输入、限额、冷却、引导、故障转移、.aikey 管理。"""
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


def _fake_news(key="test-key", model="glm-4-flash"):
    return types.SimpleNamespace(get_glm_config=lambda: (key, model))


def _chain(names=("智谱",)):
    """构造非空供应商链（供应商调用已被 mock，字段只需可区分）。"""
    return [(n, f"https://api.{n}.test/v1", "test-model", "key-" + n, None) for n in names]


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    ai_ask._cooldowns.clear()
    monkeypatch.setattr(ai_ask, "_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setattr(ai_ask, "_CONFIG_FILE", str(tmp_path / "config.json"))
    yield
    ai_ask._cooldowns.clear()


def test_ai_ask_usage_on_empty(monkeypatch):
    called = []

    async def boom(*a, **k):
        called.append(1)

    monkeypatch.setattr(ai_ask, "_ask_any", boom)
    with pytest.raises(FinishedException):
        asyncio.run(ai_ask.ai_cmd.handlers[0](_ev(".ai")))
    assert "用法" in str(ai_ask.ai_cmd.finished[-1])
    assert not called  # 空输入不得发起任何 AI 调用


def test_ai_ask_success_wraps_in_text_segment(monkeypatch):
    async def fake_ask(q):
        assert q == "今天天气怎么样"
        return "今天是晴天！", "智谱"

    monkeypatch.setattr(ai_ask, "_provider_chain", lambda: _chain())
    monkeypatch.setattr(ai_ask, "_ask_any", fake_ask)
    with pytest.raises(FinishedException):
        asyncio.run(ai_ask.ai_cmd.handlers[0](_ev(".ai 今天天气怎么样")))
    msg = ai_ask.ai_cmd.finished[-1]
    # 防 CQ 注入：必须以 text 段发送，且内容完整
    assert isinstance(msg, MessageSegment) and msg.type == "text"
    assert "今天是晴天！" in str(msg)


def test_ai_ask_injection_stays_literal_text(monkeypatch):
    async def evil_ask(q):
        return "[CQ:image,file=evil.jpg]", "智谱"

    monkeypatch.setattr(ai_ask, "_provider_chain", lambda: _chain())
    monkeypatch.setattr(ai_ask, "_ask_any", evil_ask)
    with pytest.raises(FinishedException):
        asyncio.run(ai_ask.ai_cmd.handlers[0](_ev(".ai 你好")))
    msg = ai_ask.ai_cmd.finished[-1]
    assert msg.type == "text"  # 不允许被解析成真实 CQ 码
    assert str(msg) == "[CQ:image,file=evil.jpg]"


def test_ai_ask_long_answer_clipped(monkeypatch):
    async def long_ask(q):
        return "长" * 3000, "智谱"

    monkeypatch.setattr(ai_ask, "_provider_chain", lambda: _chain())
    monkeypatch.setattr(ai_ask, "_ask_any", long_ask)
    with pytest.raises(FinishedException):
        asyncio.run(ai_ask.ai_cmd.handlers[0](_ev(".ai 写长文")))
    text = str(ai_ask.ai_cmd.finished[-1])
    assert len(text) == ai_ask._CLIP_LEN + len("\n…（回答过长已截断）")


def test_ai_ask_daily_limit_and_owner_exempt(monkeypatch):
    async def fake_ask(q):
        return "ok", "智谱"

    monkeypatch.setattr(ai_ask, "_provider_chain", lambda: _chain())
    monkeypatch.setattr(ai_ask, "_ask_any", fake_ask)
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

    async def flaky_ask(q):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("boom")
        return "第二次成功", "智谱"

    monkeypatch.setattr(ai_ask, "_provider_chain", lambda: _chain())
    monkeypatch.setattr(ai_ask, "_ask_any", flaky_ask)
    handler = ai_ask.ai_cmd.handlers[0]
    # 第一次失败：回滚冷却并提示不可用
    with pytest.raises(FinishedException):
        asyncio.run(handler(_ev(".ai 问题")))
    assert "暂时不可用" in str(ai_ask.ai_cmd.finished[-1])
    # 失败后冷却已回滚：可立即重试并成功
    with pytest.raises(FinishedException):
        asyncio.run(handler(_ev(".ai 问题")))
    assert "第二次成功" in str(ai_ask.ai_cmd.finished[-1])


def test_ai_ask_no_provider_guides_setting(monkeypatch):
    monkeypatch.setattr(ai_ask, "_news_mod", lambda: None)  # 晨报不可用
    with pytest.raises(FinishedException):
        asyncio.run(ai_ask.ai_cmd.handlers[0](_ev(".ai 问题")))
    text = str(ai_ask.ai_cmd.finished[-1])
    assert "新闻key" in text and "aikey" in text  # 引导配置任一供应商


def test_ai_ask_failover_to_second_provider(monkeypatch, tmp_path):
    calls = []

    async def fake_chat(name, url, model, key, messages, proxy):
        calls.append(name)
        if name == "智谱":
            raise RuntimeError("GLM down")
        assert proxy, "OpenRouter 必须走代理"
        return f"答案来自{name}"

    monkeypatch.setattr(ai_ask, "_news_mod", lambda: _fake_news())
    monkeypatch.setattr(ai_ask, "_chat_completion", fake_chat)
    ai_ask.save_json_state(ai_ask._CONFIG_FILE,
                           {"openrouter_key": "sk-or-test"}, ai_ask._config_lock)

    with pytest.raises(FinishedException):
        asyncio.run(ai_ask.ai_cmd.handlers[0](_ev(".ai 问题")))
    assert calls == ["智谱", "OpenRouter"]  # 第一个失败自动切换第二个
    assert "答案来自OpenRouter" in str(ai_ask.ai_cmd.finished[-1])


def test_aikey_set_and_status_masked(monkeypatch):
    handler = ai_ask.aikey_cmd.handlers[0]
    with pytest.raises(FinishedException):
        asyncio.run(handler(_ev(".aikey openrouter sk-or-v1-abcdef1234567890", uid=OWNER_QQ)))
    saved = str(ai_ask.aikey_cmd.finished[-1])
    assert "已保存" in saved and "abcdef1234567890" not in saved  # 回执不回显完整 key
    cfg = ai_ask.load_json_state(ai_ask._CONFIG_FILE, ai_ask._config_lock)
    assert cfg["openrouter_key"] == "sk-or-v1-abcdef1234567890"  # 完整 key 已落盘
    # 状态一览：key 脱敏展示
    with pytest.raises(FinishedException):
        asyncio.run(handler(_ev(".aikey", uid=OWNER_QQ)))
    status = str(ai_ask.aikey_cmd.finished[-1])
    assert "sk-or-***7890" in status


def test_aikey_rejects_non_owner():
    with pytest.raises(FinishedException):
        asyncio.run(ai_ask.aikey_cmd.handlers[0](_ev(".aikey openrouter k", uid=20001)))
    assert "仅机器人主人" in str(ai_ask.aikey_cmd.finished[-1])
