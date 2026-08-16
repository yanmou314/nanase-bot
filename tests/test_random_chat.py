import json

import pytest

from helpers import load_plugin

random_chat = load_plugin("random_chat")


def test_record_buffer_format_and_maxlen():
    mod = random_chat
    for i in range(30):
        mod._record(111, f"u{i}", f"msg{i}")
    buf = mod._buffers[111]
    assert len(buf) == mod.BUFFER_SIZE
    assert buf[-1] == "u29: msg29"
    assert buf[0] == "u10: msg10"


def test_record_evicts_beyond_max_groups(monkeypatch):
    mod = random_chat
    monkeypatch.setattr(mod, "MAX_GROUPS", 2)
    mod._buffers.clear()
    mod._record(1, "a", "x")
    mod._record(2, "b", "x")
    mod._record(3, "c", "x")
    assert 1 not in mod._buffers  # 最久未插话的被淘汰
    assert set(mod._buffers) == {2, 3}


def test_load_state_probability_clamp(monkeypatch, tmp_path):
    mod = random_chat
    f = tmp_path / "state.json"
    f.write_text(json.dumps({"enabled_groups": ["123", "bad", 456], "probability": 0.5}), encoding="utf-8")
    monkeypatch.setattr(mod, "STATE_FILE", str(f))
    mod._load_state()
    assert mod._state["probability"] == 0.2
    assert mod._state["enabled_groups"] == [123, 456]


def test_load_state_garbage_probability(monkeypatch, tmp_path):
    mod = random_chat
    f = tmp_path / "state.json"
    f.write_text(json.dumps({"probability": "not-a-number"}), encoding="utf-8")
    monkeypatch.setattr(mod, "STATE_FILE", str(f))
    mod._state["probability"] = 0.05  # 无效值应保持调用前的设置不变
    mod._load_state()
    assert mod._state["probability"] == 0.05


def _fake_auto_chat(reply):
    class _Mod:
        SYSTEM = "PERSONA"

        @staticmethod
        async def chat_completion(messages, max_tokens=300, timeout=30):
            return reply(messages, max_tokens)

    return _Mod()


def test_generate_reply_strips_quotes(monkeypatch):
    import asyncio

    mod = random_chat
    monkeypatch.setattr(mod, "_auto_chat_mod", lambda: _fake_auto_chat(lambda m, t: '"えへへ"'))
    mod._buffers[999] = __import__("collections").deque(["a: 1", "b: 2"], maxlen=20)
    assert asyncio.run(mod._generate_reply(999)) == "えへへ"


def test_generate_reply_skip_is_silence(monkeypatch):
    import asyncio

    mod = random_chat
    monkeypatch.setattr(mod, "_auto_chat_mod", lambda: _fake_auto_chat(lambda m, t: "[SKIP]"))
    mod._buffers[999] = __import__("collections").deque(["a: 1"], maxlen=20)
    assert asyncio.run(mod._generate_reply(999)) == ""


def test_generate_reply_length_cap(monkeypatch):
    import asyncio

    mod = random_chat
    monkeypatch.setattr(mod, "_auto_chat_mod", lambda: _fake_auto_chat(lambda m, t: "あ" * 500))
    mod._buffers[999] = __import__("collections").deque(["a: 1"], maxlen=20)
    assert len(asyncio.run(mod._generate_reply(999))) == 200


def test_generate_reply_no_module_raises(monkeypatch):
    import asyncio

    mod = random_chat
    monkeypatch.setattr(mod, "_auto_chat_mod", lambda: None)
    with pytest.raises(RuntimeError):
        asyncio.run(mod._generate_reply(1))


def test_generate_reply_empty_buffer(monkeypatch):
    import asyncio

    mod = random_chat
    monkeypatch.setattr(mod, "_auto_chat_mod", lambda: _fake_auto_chat(lambda m, t: "x"))
    mod._buffers.pop(1234, None)
    assert asyncio.run(mod._generate_reply(1234)) == ""
