import asyncio
import os
import time

from conftest import MessageEvent

import common as common  # BOT_ROOT 已由 conftest 加入 sys.path


def test_json_state_roundtrip(tmp_path):
    p = str(tmp_path / "state.json")
    data = {"groups": [1, 2, 3], "note": "中文"}
    common.save_json_state(p, data)
    assert common.load_json_state(p) == data


def test_load_json_state_missing_or_corrupt(tmp_path):
    assert common.load_json_state(str(tmp_path / "nope.json")) == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert common.load_json_state(str(bad)) == {}
    non_dict = tmp_path / "list.json"
    non_dict.write_text("[1,2]", encoding="utf-8")
    assert common.load_json_state(str(non_dict)) == {}


def test_save_json_state_no_tmp_leftover(tmp_path):
    p = str(tmp_path / "state.json")
    common.save_json_state(p, {"a": 1})
    assert not os.path.exists(p + ".tmp")


def test_save_json_state_async_roundtrip(tmp_path):
    p = str(tmp_path / "state.json")
    asyncio.run(common.save_json_state_async(p, {"a": 1, "b": "中文"}))
    assert common.load_json_state(p) == {"a": 1, "b": "中文"}
    assert not os.path.exists(p + ".tmp")


def test_get_member_name_caches_and_falls_back():
    class _Bot:
        calls = 0

        async def get_group_member_info(self, group_id=None, user_id=None):
            type(self).calls += 1
            return {"card": "", "nickname": "小明"}

    bot = _Bot()
    assert asyncio.run(common.get_member_name(bot, 1, 2)) == "小明"
    assert asyncio.run(common.get_member_name(bot, 1, 2)) == "小明"  # 命中缓存
    assert _Bot.calls == 1

    class _Broken:
        async def get_group_member_info(self, **kw):
            raise RuntimeError("api down")

    # 查询失败回退 QQ 号字符串（与原 chat_stats/cmd_stats 实现一致）
    assert asyncio.run(common.get_member_name(_Broken(), 1, 3)) == "3"


def test_parse_tag():
    assert common.parse_tag("ab-cd") == "ab#cd"
    assert common.parse_tag("abc#123") == "abc#123"
    assert common.parse_tag("abc") == ""
    assert common.parse_tag("abc 123") == ""  # 空格被删除而非替换为 #


def test_is_owner(monkeypatch):
    monkeypatch.setattr(common, "OWNER", "10001")
    assert common.is_owner(MessageEvent(user_id=10001)) is True
    assert common.is_owner(MessageEvent(user_id=10002)) is False


def test_cleanup_cache_removes_only_stale(tmp_path):
    fresh = tmp_path / "fresh.png"
    stale = tmp_path / "stale.png"
    fresh.write_bytes(b"x")
    stale.write_bytes(b"y")
    old = time.time() - 7200
    os.utime(stale, (old, old))
    removed = common.cleanup_cache(str(tmp_path), max_age=3600)
    assert removed == 1
    assert fresh.exists()
    assert not stale.exists()


def test_cleanup_cache_missing_dir(tmp_path):
    assert common.cleanup_cache(str(tmp_path / "void"), max_age=1) == 0
