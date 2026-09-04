"""bnet_verify 入群验证插件测试。"""

import asyncio
import time

import pytest
from conftest import FinishedException, GroupRequestEvent, Message, MessageSegment

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
    monkeypatch.setattr(bnet_verify, "_VERIFY_FILE", str(tmp_path / "verify_state.json"))
    bnet_verify._verify_pending.clear()
    bnet_verify._verify_active.clear()
    yield
    bnet_verify._verify_pending.clear()
    bnet_verify._verify_active.clear()


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


def _patch_relay(monkeypatch, has_image, segs=None):
    """桩掉中继查询，返回记录查询目标的列表。"""
    seen = []

    async def fake_relay(tag, timeout=180, active_key="", group_id=None):
        seen.append(tag)
        return (has_image, list(segs or []))

    monkeypatch.setattr(bnet_verify, "_relay_verify_profile", fake_relay)
    return seen


# ---------------- clean_join_answer：附言提取战网ID ----------------

def test_clean_join_answer():
    assert bnet_verify.clean_join_answer("Player#12345") == "Player#12345"
    assert bnet_verify.clean_join_answer("  Player＃12345  ") == "Player#12345"  # 全角#+首尾空白
    assert bnet_verify.clean_join_answer("中文昵称#12345") == "中文昵称#12345"
    assert bnet_verify.clean_join_answer("   ") == ""
    assert bnet_verify.clean_join_answer("") == ""


def test_clean_join_answer_extracts_from_question_comment():
    # 附言常连带验证问题：优先取"答案："后的 ID
    assert bnet_verify.clean_join_answer("问题：请填写你的战网ID\n答案：Yanmou#51293") == "Yanmou#51293"
    # 无"答案："标记时取最后一个冒号之后
    assert bnet_verify.clean_join_answer("请填写战网ID：Yanmou#51293") == "Yanmou#51293"


# ---------------- 核验主流程（中继查图：有图自动通过，无图转人工） ----------------

def test_join_found_approves_binds_and_schedules_card(monkeypatch):
    _enable(888)
    seen = _patch_relay(monkeypatch, True)
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

    assert seen == ["Player#12345"]  # 附言提取出的 ID 送中继查询
    assert bot.group_requests == [
        {"flag": "f1", "sub_type": "add", "approve": True, "reason": ""}
    ]
    assert binds == [(222, "Player#12345")]  # 自动绑定
    assert cards == [(888, 222, "Player#12345")]  # 群名片改为ID
    assert len(bot.sent_private) == 1  # 通过也通知主人
    notice = str(bot.sent_private[0]["message"])
    assert "Player#12345" in notice and "已自动绑定ID" in notice and "群名片" in notice


def test_join_no_image_keeps_pending_and_notice_wraps_user_content(monkeypatch):
    """中继无图片结果（超时/上游异常）：不动审批，转人工并通知主人；
    附言是申请人可控内容：必须整体包在 text 段里，不得解析出 CQ 码段。"""
    _enable(888)
    _patch_relay(monkeypatch, False, segs=[MessageSegment.text("查询失败：upstream_down")])
    bot = FakeBot()
    ev = _group_request(
        flag="fc", gid=888, uid=888, comment="[CQ:at,qq=all] Player#12345"
    )
    asyncio.run(bnet_verify._handle_group_join(bot, ev))

    assert bot.group_requests == []  # 既不通过也不拒绝，保持待人工
    rec = bnet_verify.list_verify_pending().get("888") or {}
    assert rec.get("tag") == "Player#12345"
    assert rec.get("user_id") == 888  # 记录必须带 QQ：.同意 依赖它改名片+绑定
    assert len(bot.sent_private) == 2
    assert "待人工审批" in str(bot.sent_private[0]["message"])
    assert "查询失败" in str(bot.sent_private[1]["message"])  # 中继返回内容转给主人
    msg = bot.sent_private[0]["message"]
    assert all(getattr(seg, "type", None) == "text" for seg in msg.segments)
    assert "[CQ:at,qq=all]" in str(msg)  # 原文按纯文本展示


def test_join_approve_api_failure_is_swallowed(monkeypatch):
    """flag 失效（申请人撤回等）导致审批 API 失败时：不绑定、不改卡、不崩。"""
    _enable(888)
    _patch_relay(monkeypatch, True)
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


def test_join_unrecognized_answer_rejects_without_relay(monkeypatch):
    """附言里提不出战网ID（无 名字#数字）：直接拒绝，不发起中继查询。"""
    _enable(888)
    seen = _patch_relay(monkeypatch, True)
    bot = FakeBot()
    ev = _group_request(flag="f3", gid=888, uid=444, comment="我想进群看看")
    asyncio.run(bnet_verify._handle_group_join(bot, ev))

    assert seen == []  # 不发查询
    call = bot.group_requests[0]
    assert call["approve"] is False
    assert "未能识别战网ID" in call["reason"]


def test_join_empty_answer_rejects_without_relay(monkeypatch):
    _enable(888)
    seen = _patch_relay(monkeypatch, True)
    bot = FakeBot()
    ev = _group_request(flag="f3b", gid=888, uid=445, comment="   ")
    asyncio.run(bnet_verify._handle_group_join(bot, ev))

    assert seen == []  # 空ID不发查询
    assert bot.group_requests[0]["approve"] is False
    assert "未能识别战网ID" in bot.group_requests[0]["reason"]


def test_join_unmanaged_group_ignored(monkeypatch):
    seen = _patch_relay(monkeypatch, True)
    bot = FakeBot()
    ev = _group_request(flag="f5", gid=999, uid=666, comment="Player#12345")  # 999 未开启
    asyncio.run(bnet_verify._handle_group_join(bot, ev))

    assert seen == []
    assert bot.group_requests == []
    assert bot.sent_private == []


# ---------------- 中继查询与人工审批 ----------------

def test_relay_verify_profile_returns_image_flag(monkeypatch):
    import sys
    import types

    async def fake_submit(kind, tag, text=None, timeout=None):
        assert kind == "verify"
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(([MessageSegment.text("档案")], True))
        return fut

    fake = types.ModuleType("plugins.owstats")
    fake.submit_relay_task = fake_submit
    monkeypatch.setitem(sys.modules, "plugins.owstats", fake)
    has_image, segs = asyncio.run(
        bnet_verify._relay_verify_profile("Player#12345", active_key="777")
    )
    assert has_image is True and [str(s) for s in segs] == ["档案"]
    assert "777" not in bnet_verify._verify_active  # 用完即清


def test_relay_verify_profile_swallows_missing_owstats(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "plugins.owstats", None)  # import → ImportError
    assert asyncio.run(bnet_verify._relay_verify_profile("Player#12345")) == (False, [])


def test_approve_verify_by_qq_approves_and_clears(monkeypatch):
    binds, cards = [], []
    monkeypatch.setattr(
        bnet_verify, "_auto_bind", lambda uid, tag: binds.append((uid, tag)) or True
    )
    monkeypatch.setattr(
        bnet_verify, "_schedule_card", lambda bot, gid, uid, tag: cards.append((gid, uid, tag))
    )
    bnet_verify._verify_pending["555"] = {
        "flag": "f7", "sub_type": "add", "group_id": 888,
        "user_id": 555, "tag": "Player#12345", "ts": time.time(),
    }
    bot = FakeBot()
    handled, notice = asyncio.run(bnet_verify.approve_verify_by_qq(bot, "555"))
    assert handled is True
    assert bot.group_requests[0]["approve"] is True
    assert binds == [(555, "Player#12345")]
    assert cards == [(888, 555, "Player#12345")]
    assert "555" not in bnet_verify._verify_pending
    assert "已通过" in notice


def test_approve_verify_by_qq_legacy_record_without_user_id(monkeypatch):
    """旧格式记录（缺 user_id）：QQ 回退用记录键，绑定与改名片照常。"""
    binds, cards = [], []
    monkeypatch.setattr(
        bnet_verify, "_auto_bind", lambda uid, tag: binds.append((uid, tag)) or True
    )
    monkeypatch.setattr(
        bnet_verify, "_schedule_card", lambda bot, gid, uid, tag: cards.append((gid, uid, tag))
    )
    bnet_verify._verify_pending["556"] = {
        "flag": "f8", "sub_type": "add", "group_id": 888,
        "tag": "Old#0001", "ts": time.time(),
    }
    handled, notice = asyncio.run(bnet_verify.approve_verify_by_qq(FakeBot(), "556"))
    assert handled is True
    assert binds == [(556, "Old#0001")]
    assert cards == [(888, 556, "Old#0001")]
    assert "已通过" in notice and "缺少 QQ 号" not in notice


def test_relay_verify_tracks_active_state(monkeypatch):
    """中继等待期间登记 _verify_active（含群号），完成/超时后清除。"""
    import sys
    import types

    holder: dict = {}

    async def fake_submit(kind, tag, text=None, timeout=None):
        fut = asyncio.get_running_loop().create_future()
        holder["fut"] = fut
        return fut

    fake = types.ModuleType("plugins.owstats")
    fake.submit_relay_task = fake_submit
    monkeypatch.setitem(sys.modules, "plugins.owstats", fake)

    async def scenario():
        task = asyncio.ensure_future(
            bnet_verify._relay_verify_profile("Player#12345", active_key="777", group_id=888))
        await asyncio.sleep(0)  # 让 _relay_verify_profile 跑到登记 active
        assert bnet_verify._verify_active.get("777", {}).get("tag") == "Player#12345"
        assert bnet_verify._verify_active["777"].get("group_id") == 888
        holder["fut"].set_result(([MessageSegment.text("x")], True))
        return await task

    has_image, segs = asyncio.run(scenario())
    assert has_image is True and [str(s) for s in segs] == ["x"]
    assert "777" not in bnet_verify._verify_active  # 完成后清除


def test_approve_verify_by_qq_missing_record():
    handled, notice = asyncio.run(bnet_verify.approve_verify_by_qq(FakeBot(), "404"))
    assert handled is False
    assert "没有 QQ 404" in notice


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
