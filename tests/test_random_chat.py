import asyncio
import json
import time

import pytest
from conftest import GroupMessageEvent

from helpers import load_plugin

random_chat = load_plugin("random_chat")


@pytest.fixture(autouse=True)
def _clear_module_dicts():
    """模块级缓冲/辅助字典在用例间完全隔离（前一轮测试遗留会互相干扰）。"""
    for d in (random_chat._buffers, random_chat._last_seen,
              random_chat._last_interject, random_chat._inflight):
        d.clear()
    yield
    for d in (random_chat._buffers, random_chat._last_seen,
              random_chat._last_interject, random_chat._inflight):
        d.clear()


def test_record_buffer_format_and_maxlen():
    mod = random_chat
    for i in range(30):
        mod._record(111, f"u{i}", f"msg{i}")
    buf = mod._buffers[111]
    assert len(buf) == mod.BUFFER_SIZE
    assert buf[-1] == "u29: msg29"
    assert buf[0] == "u10: msg10"


def test_context_is_shared_by_group_members():
    mod = random_chat
    mod._buffers.clear()
    mod._record(222, "alice", "来自 alice")
    mod._record(222, "bob", "来自 bob")
    mod._record(333, "other", "另一个群")

    assert list(mod._buffers[222]) == ["alice: 来自 alice", "bob: 来自 bob"]
    assert list(mod._buffers[333]) == ["other: 另一个群"]
    assert mod._buffers[222].maxlen == mod.GROUP_CONTEXT_SIZE == 20


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


# ---------------- watch handler：60 秒插话间隔 ----------------

class _WatchBot:
    def __init__(self):
        self.sent_group = []

    async def send_group_msg(self, group_id=None, message=None, **kw):
        self.sent_group.append({"group_id": group_id, "message": message})


class _Sender:
    def __init__(self, nickname):
        self.card = None
        self.nickname = nickname


def _group_ev(gid, text, uid=555):
    return GroupMessageEvent(
        plain=text, user_id=uid, group_id=gid,
        sender=_Sender(f"u{uid}"), message=[],
    )


def test_interject_interval_blocks_within_60s(monkeypatch):
    mod = random_chat
    handler = mod.watcher.handlers[0]
    monkeypatch.setattr(mod, "_enabled_set", {111})
    monkeypatch.setattr(mod, "_state", {"enabled_groups": [111], "probability": 1.0})
    monkeypatch.setattr(mod.random, "random", lambda: 0.0)  # 概率必中

    generated = []

    async def fake_gen(gid):
        generated.append(gid)
        return "插话内容"

    monkeypatch.setattr(mod, "_generate_reply", fake_gen)

    # 缓冲攒够 MIN_BUFFER 条
    for i in range(mod.MIN_BUFFER):
        mod._record(111, f"u{i}", f"msg{i}")

    bot = _WatchBot()
    # 上次插话 30 秒前：间隔不足 60s，直接放弃，不生成也不发送
    mod._last_interject[111] = time.time() - 30
    asyncio.run(handler(bot, _group_ev(111, "大家好")))
    assert generated == []
    assert bot.sent_group == []
    assert mod._last_interject[111] < time.time() - 20  # 未被刷新

    # 上次插话超过 60s：允许触发
    mod._last_interject[111] = time.time() - mod.MIN_INTERJECT_INTERVAL - 60
    asyncio.run(handler(bot, _group_ev(111, "大家好呀")))
    assert generated == [111]
    assert len(bot.sent_group) == 1
    assert bot.sent_group[0]["group_id"] == 111
    assert str(bot.sent_group[0]["message"]) == "插话内容"
    assert mod._last_interject[111] > time.time() - 10  # 发送成功后刷新
