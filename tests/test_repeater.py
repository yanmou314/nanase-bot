import time
from collections import deque

from conftest import GroupMessageEvent, MessageSegment

from helpers import load_plugin

repeater = load_plugin("repeater")


def test_text_hash_is_sha1():
    import hashlib
    assert repeater._text_hash("hello") == hashlib.sha1(b"hello").hexdigest()


def test_normalize_item_migrates_plain_text():
    item = repeater._normalize_item(["t", "你好"])
    assert item[0] == "t"
    assert item[1] == repeater._text_hash("你好")
    assert item[2] == ""


def test_normalize_item_keeps_hashed():
    h = "a" * 40
    item = repeater._normalize_item(["t", h])
    assert item == ("t", h, "")


def test_normalize_item_image_passthrough():
    item = repeater._normalize_item(["i", "filehash"])
    assert item == ("i", "filehash", "filehash")
    assert repeater._normalize_item(["i", "x", "x"]) == ("i", "x", "x")


def test_fingerprint_text():
    ev = GroupMessageEvent(plain="你好", message=[MessageSegment.text("你好")])
    fp = repeater._fingerprint(ev)
    assert fp is not None and fp[0] == "t"
    assert fp[1] == repeater._text_hash("你好")
    assert fp[2] == "你好"


def test_fingerprint_ignores_commands_and_mixed():
    cmd = GroupMessageEvent(plain=".签到", message=[MessageSegment.text(".签到")])
    assert repeater._fingerprint(cmd) is None
    mixed = GroupMessageEvent(plain="看图", message=[MessageSegment.text("看图"), MessageSegment.image("f")])
    assert repeater._fingerprint(mixed) is None


def test_fingerprint_image():
    ev = GroupMessageEvent(plain="", message=[MessageSegment.image("abc.jpg")])
    fp = repeater._fingerprint(ev)
    assert fp == ("i", "abc.jpg", "abc.jpg")


def test_prune_removes_stale_groups():
    mod = repeater
    old_ts = time.time() - 8 * 86400
    mod._track.clear(); mod._replied_ts.clear(); mod._replied_fp.clear()
    mod._track[1] = deque(maxlen=3); mod._replied_ts[1] = old_ts; mod._replied_fp[1] = ("t", "x", "y")
    mod._track[2] = deque(maxlen=3); mod._replied_ts[2] = time.time()
    mod._prune()
    assert 1 not in mod._track and 1 not in mod._replied_ts and 1 not in mod._replied_fp
    assert 2 in mod._track
    mod._track.clear(); mod._replied_ts.clear(); mod._replied_fp.clear()


def test_save_state_skips_none_fingerprints(monkeypatch, tmp_path):
    """回归：不可复读消息的指纹为 None，保存时必须跳过而不是崩溃。"""
    import asyncio
    import json as _json

    mod = repeater
    f = tmp_path / "repeater_state.json"
    monkeypatch.setattr(mod, "STATE_FILE", str(f))
    mod._track.clear(); mod._replied_ts.clear()
    mod._track[42] = deque([None, ("t", "a" * 40, "原文"), None], maxlen=3)
    mod._replied_ts[42] = time.time()

    asyncio.run(mod._save_on_shutdown())  # 不得抛异常
    data = _json.loads(f.read_text(encoding="utf-8"))
    assert data["track"] == {"42": [["t", "a" * 40]]}
    assert mod._replied_ts  # 未被破坏


def test_repeater_none_fingerprint_breaks_chain_without_crash():
    """回归：@、表情、混合消息的指纹为 None，进入队列后比较不得抛 TypeError。"""
    import asyncio

    mod = repeater
    mod._track.clear(); mod._replied_ts.clear(); mod._replied_fp.clear()

    sent = []

    class FakeBot:
        async def send_group_msg(self, **kw):
            sent.append(kw)

    async def feed(plain, message):
        ev = GroupMessageEvent(group_id=999, plain=plain, message=message)
        await mod.repeater(FakeBot(), ev)

    # 序列：哈哈 / 不可复读(→None) / 哈哈 / 哈哈 / 哈哈 / 哈哈
    # 旧代码在第 3 条消息时 deq=[t, None, t]，deq[1][:2] 直接 TypeError
    asyncio.run(feed("哈哈", [MessageSegment.text("哈哈")]))
    asyncio.run(feed("看图", [MessageSegment.text("看图"), MessageSegment.image("f")]))
    asyncio.run(feed("哈哈", [MessageSegment.text("哈哈")]))  # 旧代码在此崩溃
    asyncio.run(feed("哈哈", [MessageSegment.text("哈哈")]))
    asyncio.run(feed("哈哈", [MessageSegment.text("哈哈")]))  # 三连成立 → 复读一次
    asyncio.run(feed("哈哈", [MessageSegment.text("哈哈")]))  # 同一串复读不重复触发
    assert len(sent) == 1
    assert sent[0]["group_id"] == 999
    assert str(sent[0]["message"]) == "哈哈"
    mod._track.clear(); mod._replied_ts.clear(); mod._replied_fp.clear()
