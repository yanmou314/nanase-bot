import asyncio
import json

import pytest
from conftest import MessageEvent

from helpers import load_plugin

auto_chat = load_plugin("auto_chat")


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


class _FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return _FakeResponse(self.payload)


def test_clean_msg_strips_cq_at():
    ev = MessageEvent(plain="[CQ:at,qq=1837204555] 你好呀")
    assert auto_chat._clean_msg(ev) == "你好呀"


def test_get_memory_maxlen():
    mod = auto_chat
    mem = mod._get_memory(("g1", "u1"))
    for i in range(30):
        mem.append({"role": "user", "content": str(i)})
    assert len(mem) == 20
    assert mem[-1]["content"] == "29"


def test_group_memory_key_is_shared_but_private_memory_isolated():
    mod = auto_chat
    assert mod._memory_key("123", "alice") == ("group", "123")
    assert mod._memory_key("123", "bob") == ("group", "123")
    assert mod._memory_key("0", "alice") == ("private", "alice")
    assert mod._memory_key("0", "bob") == ("private", "bob")


def test_ai_reply_uses_shared_group_memory(monkeypatch):
    mod = auto_chat
    calls = []

    async def fake_completion(messages, max_tokens=300, timeout=30):
        calls.append(messages)
        return "收到啦"

    monkeypatch.setattr(mod, "chat_completion", fake_completion)
    mod._memory.clear()
    mod._memory_last_seen.clear()

    asyncio.run(mod._ai_reply("key", "alice", "123", "第一句话"))
    asyncio.run(mod._ai_reply("key", "bob", "123", "第二句话"))

    assert any(m["content"] == "第一句话" for m in calls[1])
    assert ("group", "123") in mod._memory
    assert ("123", "alice") not in mod._memory


def test_get_memory_ttl_eviction():
    mod = auto_chat
    mod._get_memory(("g2", "u2"))  # 建立条目
    assert ("g2", "u2") in mod._memory
    mod._memory_last_seen[("g2", "u2")] -= mod._MEMORY_TTL + 10  # 置为过期
    mod._get_memory(("g3", "u3"))  # 触发 TTL 清理
    assert ("g2", "u2") not in mod._memory


@pytest.fixture(autouse=True)
def _restore_memory_state():
    yield
    auto_chat._memory.clear()
    auto_chat._memory_last_seen.clear()


def test_get_memory_capacity_eviction(monkeypatch):
    mod = auto_chat
    monkeypatch.setattr(mod, "_MEMORY_MAX_KEYS", 5)
    for i in range(5):
        mod._get_memory(("gx", f"u{i}"))
    assert len(mod._memory) == 5
    mod._get_memory(("gx", "new"))
    assert len(mod._memory) == 5
    assert ("gx", "new") in mod._memory
    assert ("gx", "u0") not in mod._memory  # 最久未访问的被淘汰


def test_load_key_missing_config(monkeypatch, tmp_path):
    mod = auto_chat
    monkeypatch.setattr(mod, "CFG_FILE", str(tmp_path / "nope.json"))
    monkeypatch.setattr(mod, "_cached_key_mtime", -1.0)
    assert mod._load_key() == ""


def test_load_key_reads_config(monkeypatch, tmp_path):
    mod = auto_chat
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"api_key": " sk-test "}), encoding="utf-8")
    monkeypatch.setattr(mod, "CFG_FILE", str(cfg))
    monkeypatch.setattr(mod, "_cached_key_mtime", -1.0)
    assert mod._load_key() == "sk-test"


def test_get_api_key_public_alias(monkeypatch, tmp_path):
    mod = auto_chat
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"api_key": "sk-pub"}), encoding="utf-8")
    monkeypatch.setattr(mod, "CFG_FILE", str(cfg))
    monkeypatch.setattr(mod, "_cached_key_mtime", -1.0)
    assert mod.get_api_key() == "sk-pub"


def test_chat_completion_no_key(monkeypatch, tmp_path):
    mod = auto_chat
    monkeypatch.setattr(mod, "CFG_FILE", str(tmp_path / "nope.json"))
    monkeypatch.setattr(mod, "_cached_key_mtime", -1.0)
    with pytest.raises(RuntimeError):
        asyncio.run(mod.chat_completion([{"role": "user", "content": "hi"}]))


def test_chat_completion_ok(monkeypatch):
    import asyncio

    mod = auto_chat
    client = _FakeClient({"choices": [{"message": {"content": "  你好哦  "}}]})
    monkeypatch.setattr(mod, "_load_key", lambda: "sk-1")
    monkeypatch.setattr(mod, "_get_http_client", lambda: client)

    reply = asyncio.run(mod.chat_completion([{"role": "user", "content": "hi"}], max_tokens=77))
    assert reply == "你好哦"
    (call,) = client.calls
    assert call["headers"]["Authorization"] == "Bearer sk-1"
    assert call["json"]["model"] == mod.MODEL
    assert call["json"]["max_tokens"] == 77
    assert call["json"]["thinking"] == {"type": "disabled"}
