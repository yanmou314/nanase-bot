"""idle_kick 单测：时间解析、群号剥离、踢人名单筛选。"""
from datetime import datetime
from zoneinfo import ZoneInfo

from helpers import load_plugin

ik = load_plugin("idle_kick")
_SH = ZoneInfo("Asia/Shanghai")


def test_split_gid_token():
    assert ik.split_gid_token("123456789 2026-09-01") == ("123456789", "2026-09-01")
    assert ik.split_gid_token("g:123456789 30天") == ("123456789", "30天")
    assert ik.split_gid_token("群:123456 清除") == ("123456", "清除")
    assert ik.split_gid_token("#123456789 确认") == ("123456789", "确认")
    assert ik.split_gid_token("2026-09-01") == (None, "2026-09-01")
    assert ik.split_gid_token("30天") == (None, "30天")
    assert ik.split_gid_token("确认") == (None, "确认")
    assert ik.split_gid_token("+10001") == (None, "+10001")  # 4位以下不是群号
    assert ik.split_gid_token("123456789") == ("123456789", "")
    assert ik.split_gid_token("") == (None, "")


def test_parse_cutoff_clear_and_relative_and_abs():
    now = datetime(2026, 9, 19, 12, 0, 0)
    assert ik.parse_cutoff("清除", now=now) == ("clear", None)
    kind, dt = ik.parse_cutoff("30天", now=now)
    assert kind == "ok" and dt == datetime(2026, 8, 20, 12, 0, 0)
    kind, dt = ik.parse_cutoff("2026-09-01")
    assert kind == "ok" and dt == datetime(2026, 9, 1, 0, 0, 0)
    assert ik.parse_cutoff("昨天")[0] == "error"


def test_parse_run_confirm():
    assert ik.parse_run_confirm("确认") == (None, True)
    assert ik.parse_run_confirm("123456789 确认") == ("123456789", True)
    assert ik.parse_run_confirm("123456789") == ("123456789", False)
    assert ik.parse_run_confirm("") == (None, False)
    assert ik.parse_run_confirm("123456789 y") == ("123456789", True)


def test_parse_protect_args():
    assert ik.parse_protect_args("123456789 +10002") == ("123456789", "+10002")
    assert ik.parse_protect_args("+10002") == (None, "+10002")
    assert ik.parse_protect_args("123456789") == ("123456789", "")


class _Ev:
    def __init__(self, group_id=None):
        if group_id is not None:
            self.group_id = group_id


def test_resolve_target_gid():
    # 群内省略群号
    gid, rest, err = ik.resolve_target_gid(_Ev(111), "2026-09-01")
    assert (gid, rest, err) == ("111", "2026-09-01", None)
    # 群内指定其他群
    gid, rest, err = ik.resolve_target_gid(_Ev(111), "222222 30天")
    assert (gid, rest, err) == ("222222", "30天", None)
    # 私聊指定群号
    gid, rest, err = ik.resolve_target_gid(_Ev(None), "333333 清除")
    assert (gid, rest, err) == ("333333", "清除", None)
    # 私聊未指定群号
    gid, rest, err = ik.resolve_target_gid(_Ev(None), "30天")
    assert gid is None and err


def _m(uid, last, role="member", card="", nick=""):
    return {"user_id": uid, "last_sent_time": last, "role": role, "card": card, "nickname": nick}


def test_select_kick_targets():
    cutoff = ik.cutoff_ts(datetime(2026, 9, 1, 0, 0, 0))
    ts_ok = ik.cutoff_ts(datetime(2026, 9, 10))
    ts_old = ik.cutoff_ts(datetime(2026, 8, 1))
    members = [
        _m(1, ts_old, card="甲"),
        _m(2, ts_ok, card="乙"),
        _m(3, ts_old, role="admin"),
        _m(4, ts_old, role="owner"),
        _m(5, 0, card="从不说话"),
        _m(6, ts_old, card="保护"),
        _m(7, ts_old, card="机器人"),
        _m(8, ts_old, card="主人"),
    ]
    targets = ik.select_kick_targets(members, cutoff, protect={"6"}, bot_id="7", owner="8")
    assert [t["user_id"] for t in targets] == [5, 1]


def test_member_name():
    assert ik.member_name(_m(1, 0, card="卡", nick="昵")) == "卡"
    assert ik.member_name(_m(1, 0, nick="昵")) == "昵"
    assert ik.member_name(_m(9, 0)) == "9"


def test_parse_cutoff_yyyymmdd():
    kind, dt = ik.parse_cutoff("20260901")
    assert kind == "ok" and dt == datetime(2026, 9, 1, 0, 0, 0)
    kind, dt = ik.parse_cutoff("20260901 12:30")
    assert kind == "ok" and dt == datetime(2026, 9, 1, 12, 30, 0)


def test_safe_last_sent_and_sort_not_crash():
    assert ik.safe_last_sent({"last_sent_time": "abc"}) == 0.0
    assert ik.safe_last_sent({}) == 0.0
    assert ik.safe_last_sent({"last_sent_time": 12}) == 12.0
    cutoff = ik.cutoff_ts(datetime(2026, 9, 1))
    members = [_m(1, "bad"), _m(2, ik.cutoff_ts(datetime(2026, 8, 1))), _m(3, 0)]
    targets = ik.select_kick_targets(members, cutoff, protect=set())
    # 排序不得因非法 last_sent 抛错；非法与 0 都排在前面
    assert [t["user_id"] for t in targets][0] in (1, 3)


def test_mass_wipe_guard():
    old_ts = ik.cutoff_ts(datetime(2020, 1, 1))
    # 全 0 且数量够 → 拦截
    targets = [_m(i, 0) for i in range(1, 6)]
    members = targets + [_m(99, old_ts, role="admin")]
    reason = ik.mass_wipe_guard(targets, members)
    assert reason and "全为 0" in reason
    # 少量正常目标 → 放行
    ok_targets = [_m(1, old_ts), _m(2, old_ts)]
    assert ik.mass_wipe_guard(ok_targets, ok_targets + [_m(9, 9, role="owner")]) is None
    # 七成以上 → 拦截（有非零 last_sent，不走全 0 分支）
    many = [_m(i, old_ts if i % 2 else 0) for i in range(1, 21)]
    ordinary = many  # 全是 member
    reason = ik.mass_wipe_guard(many, ordinary + [_m(99, 999, role="owner")])
    assert reason and ("七成" in reason or "异常" in reason)
