import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor

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


def test_get_member_name_failure_not_long_cached():
    """失败结果只用短 TTL，不得按 300s 负缓存锁死 QQ 号展示。"""
    key = (11, 22)
    class _Broken:
        calls = 0

        async def get_group_member_info(self, **kw):
            type(self).calls += 1
            raise RuntimeError("api down")

    assert asyncio.run(common.get_member_name(_Broken(), 11, 22)) == "22"
    # 短 TTL 内命中负缓存，不再打 API
    assert asyncio.run(common.get_member_name(_Broken(), 11, 22)) == "22"
    assert _Broken.calls == 1

    # 失败条目写入时 ts = fail_now - (_NAME_TTL - _NAME_FAIL_TTL)，有效窗口 30s。
    # 拨到使 now - ts >= _NAME_TTL 即可模拟短 TTL 已过期。
    with common._member_name_lock:
        common._member_name_ts[key] = time.time() - common._NAME_TTL - 1

    class _Ok:
        calls = 0

        async def get_group_member_info(self, **kw):
            type(self).calls += 1
            return {"card": "恢复", "nickname": ""}

    assert asyncio.run(common.get_member_name(_Ok(), 11, 22)) == "恢复"
    assert _Ok.calls == 1


def test_parse_tag():
    assert common.parse_tag("ab-cd") == "ab#cd"
    assert common.parse_tag("abc#123") == "abc#123"
    assert common.parse_tag("abc") == ""
    assert common.parse_tag("abc 123") == ""  # 空格被删除而非替换为 #


def test_is_owner(monkeypatch):
    monkeypatch.setattr(common, "OWNER", "10001")
    monkeypatch.setattr(common, "TEST_PRIVILEGED_GROUPS", set())
    monkeypatch.setattr(common, "TEST_OWNER_UIDS", set())
    assert common.is_owner(MessageEvent(user_id=10001)) is True
    assert common.is_owner(MessageEvent(user_id=10002)) is False


def test_is_owner_default_denies_legacy_test_group(monkeypatch):
    """默认配置下历史测试群 864213945 内任意成员都不是 owner。"""
    monkeypatch.setattr(common, "OWNER", "10001")
    monkeypatch.setattr(common, "TEST_PRIVILEGED_GROUPS", set())
    monkeypatch.setattr(common, "TEST_OWNER_UIDS", set())
    assert 864213945 not in common.TEST_PRIVILEGED_GROUPS
    assert common.is_owner(
        MessageEvent(user_id=10002, group_id=864213945)
    ) is False
    # 即便 user_id 是 OWNER，群不在特权集也仍因真 OWNER 为 True（与旧逻辑一致）
    assert common.is_owner(
        MessageEvent(user_id=10001, group_id=864213945)
    ) is True


def test_is_owner_privileged_group_requires_uid_allowlist(monkeypatch):
    monkeypatch.setattr(common, "OWNER", "10001")
    monkeypatch.setattr(common, "TEST_PRIVILEGED_GROUPS", {999})
    # 空白名单：整群放行（仅当特权群被显式配置时才会走到这里）
    monkeypatch.setattr(common, "TEST_OWNER_UIDS", set())
    assert common.is_owner(MessageEvent(user_id=555, group_id=999)) is True
    assert common.is_owner(MessageEvent(user_id=555, group_id=998)) is False

    # 非空白名单：仅命中 uid 才放行
    monkeypatch.setattr(common, "TEST_OWNER_UIDS", {555})
    assert common.is_owner(MessageEvent(user_id=555, group_id=999)) is True
    assert common.is_owner(MessageEvent(user_id=556, group_id=999)) is False


def test_parse_id_set():
    assert common._parse_id_set(None) == set()
    assert common._parse_id_set("") == set()
    assert common._parse_id_set("1, 2,3") == {1, 2, 3}
    assert common._parse_id_set("1,abc,3") == {1, 3}


def test_fonts_noto_candidate_chain(monkeypatch, tmp_path):
    """custom/noto 优先；缺失时回退 opentype/noto。"""
    custom = tmp_path / "custom" / "noto" / "NotoSansCJK-Bold.ttc"
    opentype = tmp_path / "opentype" / "noto" / "NotoSansCJK-Bold.ttc"
    custom.parent.mkdir(parents=True)
    opentype.parent.mkdir(parents=True)

    # 两者都在：选 custom
    custom.write_bytes(b"c")
    opentype.write_bytes(b"o")
    assert (
        common._first_existing(str(custom), str(opentype)) == str(custom)
    )

    # 只有 opentype：选 opentype
    custom.unlink()
    assert (
        common._first_existing(str(custom), str(opentype)) == str(opentype)
    )

    # 都没有：返回 None
    opentype.unlink()
    assert common._first_existing(str(custom), str(opentype)) is None


def test_fonts_noto_paths_are_strings():
    assert isinstance(common.FONTS["noto_bold"], str)
    assert isinstance(common.FONTS["noto_reg"], str)
    assert "NotoSansCJK-Bold" in common.FONTS["noto_bold"]
    assert "NotoSansCJK-Regular" in common.FONTS["noto_reg"]


def test_get_http_client_replaces_closed(monkeypatch):
    """池内 client 关闭后应被 pop 并注册新实例，而不是永久塞进 orphan。"""
    monkeypatch.setattr(common, "_http_clients", {})
    monkeypatch.setattr(common, "_orphan_clients", [])

    c1 = common.get_http_client(7.0)
    assert c1 is common._http_clients[7.0]

    # 模拟外部 close：客户端仍挂在池里但 is_closed
    class _Closed:
        is_closed = True

    common._http_clients[7.0] = _Closed()
    c2 = common.get_http_client(7.0)
    assert c2 is not None
    assert c2 is common._http_clients[7.0]
    assert not getattr(c2, "is_closed", True)
    # 替换路径不应把新 client 丢进 orphan
    assert c2 not in common._orphan_clients


def test_render_executor_bounded():
    assert isinstance(common._RENDER_EXECUTOR, ThreadPoolExecutor)
    assert common._RENDER_EXECUTOR._max_workers == 2
    assert common.RENDER_SEM is not None
    assert common.RENDER_TOTAL_TIMEOUT == 180


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
