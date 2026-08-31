"""bnet_verify 入群验证插件测试。"""

import asyncio

import pytest
from conftest import FinishedException, GroupRequestEvent, Message

from helpers import load_plugin

bnet_verify = load_plugin("bnet_verify")

OWNER_ID = 10000  # conftest 固定的 QQBOT_OWNER


class FakeBot:
    def __init__(self, role="admin"):
        self.role = role
        self.self_id = 10086
        self.group_requests = []
        self.sent_private = []

    async def set_group_add_request(self, flag, sub_type, approve, reason=""):
        self.group_requests.append(
            {"flag": flag, "sub_type": sub_type, "approve": approve, "reason": reason}
        )

    async def send_private_msg(self, user_id, message):
        self.sent_private.append({"user_id": user_id, "message": message})

    async def get_group_member_info(self, group_id, user_id):
        return {"role": self.role}


def _group_request(flag="f1", gid=888, uid=222, sub="add", comment=""):
    return GroupRequestEvent(
        flag=flag, sub_type=sub, group_id=gid, user_id=uid, comment=comment,
    )


@pytest.fixture(autouse=True)
def _state_file(tmp_path, monkeypatch):
    monkeypatch.setattr(bnet_verify, "STATE_FILE", str(tmp_path / "groups.json"))


def _enable(gid=888):
    state = bnet_verify._load_state()
    groups = state.get("groups") or []
    if str(gid) not in groups:
        groups.append(str(gid))
        state["groups"] = groups
        asyncio.run(bnet_verify._save_state(state))


def _run(fn, *args):
    """调用 handler，吞掉 finish() 抛出的 FinishedException。"""
    try:
        return asyncio.run(fn(*args))
    except FinishedException as exc:
        return exc.message


def _patch_query(monkeypatch, status, error=None):
    async def fake_query(tag):
        return {"status": status, "error": error}
    monkeypatch.setattr(bnet_verify, "query_overwatch_profile", fake_query)


# ---------------- clean_join_answer：附言即ID ----------------

def test_clean_join_answer():
    assert bnet_verify.clean_join_answer("Player#12345") == "Player#12345"
    assert bnet_verify.clean_join_answer("  Player＃12345  ") == "Player#12345"  # 全角#+首尾空白
    assert bnet_verify.clean_join_answer("中文昵称#12345") == "中文昵称#12345"
    assert bnet_verify.clean_join_answer("   ") == ""
    assert bnet_verify.clean_join_answer("") == ""


# ---------------- 核验主流程 ----------------

def test_join_found_approves_binds_and_schedules_card(monkeypatch):
    _enable(888)
    _patch_query(monkeypatch, "found")
    binds, cards = [], []
    monkeypatch.setattr(
        bnet_verify, "_auto_bind", lambda uid, tag: binds.append((uid, tag)) or True
    )
    monkeypatch.setattr(
        bnet_verify, "_schedule_card",
        lambda bot, gid, uid, tag: cards.append((gid, uid, tag)),
    )
    bot = FakeBot()
    ev = _group_request(flag="f1", gid=888, uid=222, comment="Player#12345")
    asyncio.run(bnet_verify._handle_group_join(bot, ev))

    assert bot.group_requests == [
        {"flag": "f1", "sub_type": "add", "approve": True, "reason": ""}
    ]
    assert binds == [(222, "Player#12345")]  # 自动绑定
    assert cards == [(888, 222, "Player#12345")]  # 群名片改为ID
    assert len(bot.sent_private) == 1  # 通过也通知主人
    notice = str(bot.sent_private[0]["message"])
    assert "Player#12345" in notice and "已自动绑定ID" in notice and "群名片" in notice


def test_join_notice_wraps_user_content_as_text(monkeypatch):
    """附言是申请人可控内容：必须整体包在 text 段里，不得解析出 CQ 码段。"""
    _enable(888)
    _patch_query(monkeypatch, "error", error="upstream_down")
    bot = FakeBot()
    ev = _group_request(
        flag="fc", gid=888, uid=888, comment="[CQ:at,qq=all] Player#12345"
    )
    asyncio.run(bnet_verify._handle_group_join(bot, ev))

    msg = bot.sent_private[0]["message"]
    assert all(getattr(seg, "type", None) == "text" for seg in msg.segments)
    assert "[CQ:at,qq=all]" in str(msg)  # 原文按纯文本展示


def test_join_approve_api_failure_is_swallowed(monkeypatch):
    """flag 失效（申请人撤回等）导致审批 API 失败时：不绑定、不改卡、不崩。"""
    _enable(888)
    _patch_query(monkeypatch, "found")
    binds, cards = [], []
    monkeypatch.setattr(
        bnet_verify, "_auto_bind", lambda uid, tag: binds.append((uid, tag))
    )
    monkeypatch.setattr(
        bnet_verify, "_schedule_card", lambda *a: cards.append(a)
    )

    class FailBot(FakeBot):
        async def set_group_add_request(self, flag, sub_type, approve, reason=""):
            raise RuntimeError("flag expired")

    bot = FailBot()
    ev = _group_request(flag="f9", gid=888, uid=777, comment="Player#12345")
    asyncio.run(bnet_verify._handle_group_join(bot, ev))

    assert binds == [] and cards == [] and bot.sent_private == []


def test_auto_bind_uses_owstats(monkeypatch):
    import sys
    import types

    calls = []
    fake = types.ModuleType("plugins.owstats")
    fake._bind = lambda uid, tag: calls.append((uid, tag))
    monkeypatch.setitem(sys.modules, "plugins.owstats", fake)
    assert bnet_verify._auto_bind(222, "Player#12345") is True
    assert calls == [("222", "Player#12345")]


def test_auto_bind_missing_owstats(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "plugins.owstats", None)  # import → ImportError
    assert bnet_verify._auto_bind(222, "Player#12345") is False


def test_set_card_with_retry(monkeypatch):
    monkeypatch.setattr(bnet_verify, "CARD_RETRY_DELAYS", (0,))

    calls = []

    class CardBot(FakeBot):
        async def set_group_card(self, group_id, user_id, card):
            calls.append((group_id, user_id, card))

    asyncio.run(bnet_verify._set_card_with_retry(CardBot(), 888, 222, "Player#12345"))
    assert calls == [(888, 222, "Player#12345")]


def test_set_card_with_retry_exhausts_without_raise(monkeypatch):
    monkeypatch.setattr(bnet_verify, "CARD_RETRY_DELAYS", (0, 0))

    calls = []

    class CardFailBot(FakeBot):
        async def set_group_card(self, group_id, user_id, card):
            calls.append((group_id, user_id, card))
            raise RuntimeError("member not joined yet")

    asyncio.run(bnet_verify._set_card_with_retry(CardFailBot(), 888, 222, "Player#12345"))
    assert len(calls) == 2  # 重试后放弃，不向调用方抛异常


def test_join_not_found_rejects_with_reason(monkeypatch):
    _enable(888)
    _patch_query(monkeypatch, "not_found")
    bot = FakeBot()
    ev = _group_request(flag="f2", gid=888, uid=333, comment="Wrong#1234")
    asyncio.run(bnet_verify._handle_group_join(bot, ev))

    assert len(bot.group_requests) == 1
    call = bot.group_requests[0]
    assert call["approve"] is False
    assert "战网ID不正确" in call["reason"] and "Wrong#1234" in call["reason"]


def test_join_plain_text_treated_as_id(monkeypatch):
    """不做提取：附言整体作为ID送查询，查不到按"战网ID不正确"拒绝。"""
    _enable(888)
    seen = []

    async def fake_query(tag):
        seen.append(tag)
        return {"status": "not_found"}

    monkeypatch.setattr(bnet_verify, "query_overwatch_profile", fake_query)
    bot = FakeBot()
    ev = _group_request(flag="f3", gid=888, uid=444, comment="我想进群看看")
    asyncio.run(bnet_verify._handle_group_join(bot, ev))

    assert seen == ["我想进群看看"]
    call = bot.group_requests[0]
    assert call["approve"] is False
    assert "战网ID不正确" in call["reason"] and "我想进群看看" in call["reason"]


def test_join_empty_answer_rejects_without_query(monkeypatch):
    _enable(888)
    seen = []

    async def fake_query(tag):
        seen.append(tag)
        return {"status": "found"}

    monkeypatch.setattr(bnet_verify, "query_overwatch_profile", fake_query)
    bot = FakeBot()
    ev = _group_request(flag="f3b", gid=888, uid=445, comment="   ")
    asyncio.run(bnet_verify._handle_group_join(bot, ev))

    assert seen == []  # 空ID不发查询
    assert bot.group_requests[0]["approve"] is False
    assert "不能为空" in bot.group_requests[0]["reason"]


def test_join_upstream_error_leaves_pending_and_notifies(monkeypatch):
    _enable(888)
    _patch_query(monkeypatch, "error", error="too_many_requests")
    bot = FakeBot()
    ev = _group_request(flag="f4", gid=888, uid=555, comment="Player#12345")
    asyncio.run(bnet_verify._handle_group_join(bot, ev))

    assert bot.group_requests == []  # 异常时不动审批，保持待人工
    assert len(bot.sent_private) == 1
    assert "待人工处理" in str(bot.sent_private[0]["message"])


def test_join_unmanaged_group_ignored(monkeypatch):
    _patch_query(monkeypatch, "found")
    bot = FakeBot()
    ev = _group_request(flag="f5", gid=999, uid=666, comment="Player#12345")  # 999 未开启
    asyncio.run(bnet_verify._handle_group_join(bot, ev))

    assert bot.group_requests == []
    assert bot.sent_private == []


def test_query_maps_responses(monkeypatch):
    class _Resp:
        def __init__(self, payload, status_code=200):
            self._payload = payload
            self.status_code = status_code

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, payload, status_code=200):
            self._payload = payload
            self._status = status_code

        async def post(self, url, json=None, timeout=None):
            return _Resp(self._payload, self._status)

    # ok → found
    monkeypatch.setattr(
        bnet_verify, "get_http_client",
        lambda t: _Client({"ok": True, "resolved": {"full_id": "Player#12345"}}),
    )
    assert asyncio.run(bnet_verify.query_overwatch_profile("Player#12345"))["status"] == "found"
    # bnet_not_found → not_found
    monkeypatch.setattr(
        bnet_verify, "get_http_client",
        lambda t: _Client({"ok": False, "error": "bnet_not_found"}, 400),
    )
    assert asyncio.run(bnet_verify.query_overwatch_profile("Nobody#0000"))["status"] == "not_found"
    # 其他错误码 → error
    monkeypatch.setattr(
        bnet_verify, "get_http_client",
        lambda t: _Client({"ok": False, "error": "upstream_boom"}, 500),
    )
    result = asyncio.run(bnet_verify.query_overwatch_profile("Player#12345"))
    assert result["status"] == "error" and result["error"] == "upstream_boom"


# ---------------- 主人开关 ----------------

def _owner_event(gid=888):
    return GroupRequestEvent(
        flag="", sub_type="add", group_id=gid, user_id=OWNER_ID, comment="",
    )


def test_enable_disable_status_flow(monkeypatch):
    bot = FakeBot()
    ev = _owner_event(888)

    _run(bnet_verify.enable_verify, bot, ev, Message(""))
    assert bnet_verify.is_managed_group(888) is True
    assert "已开启群 888" in bnet_verify.enable_cmd.finished[-1]
    assert "警告" not in bnet_verify.enable_cmd.finished[-1]  # bot 是 admin

    _run(bnet_verify.enable_verify, bot, ev, Message(""))  # 重复开启
    assert "本来就是开着" in bnet_verify.enable_cmd.finished[-1]

    _run(bnet_verify.verify_status, ev)
    assert "888" in bnet_verify.verify_cmd.finished[-1]

    _run(bnet_verify.disable_verify, bot, ev, Message(""))
    assert bnet_verify.is_managed_group(888) is False
    assert "已关闭群 888" in bnet_verify.disable_cmd.finished[-1]

    _run(bnet_verify.disable_verify, bot, ev, Message(""))  # 重复关闭
    assert "本来就没开" in bnet_verify.disable_cmd.finished[-1]


def test_enable_warns_when_bot_not_admin(monkeypatch):
    bot = FakeBot(role="member")
    _run(bnet_verify.enable_verify, bot, _owner_event(777), Message(""))
    assert "已开启群 777" in bnet_verify.enable_cmd.finished[-1]
    assert "不是该群管理员" in bnet_verify.enable_cmd.finished[-1]


def test_owner_commands_reject_non_owner():
    bot = FakeBot()
    msg = _run(bnet_verify.enable_verify, bot, _group_request(gid=888, uid=222), Message(""))
    assert msg == "只有主人可以操作入群验证开关"
    msg = _run(bnet_verify.disable_verify, bot, _group_request(gid=888, uid=222), Message(""))
    assert msg == "只有主人可以操作入群验证开关"
    msg = _run(bnet_verify.verify_status, _group_request(gid=888, uid=222))
    assert msg == "只有主人可以查看入群验证状态"
    assert bnet_verify.is_managed_group(888) is False
