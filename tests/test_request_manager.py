import asyncio
import json
import time

import pytest
from conftest import (
    Bot,
    FinishedException,
    GroupMessageEvent,
    GroupRequestEvent,
    Message,
    MessageEvent,
    MessageSegment,
)

from helpers import load_plugin

request_manager = load_plugin("request_manager")

OWNER_ID = 10000  # conftest 固定的 QQBOT_OWNER


@pytest.fixture(autouse=True)
def _isolate_pending():
    request_manager._pending.clear()
    yield
    request_manager._pending.clear()


class ApproveBot(Bot):
    """记录审批/查询 API 调用的假 bot；可注入 API 失败。"""

    def __init__(self):
        super().__init__()
        self.group_requests = []  # set_group_add_request 调用记录
        self.friend_requests = []  # set_friend_add_request 调用记录
        self.fail_group_api = False
        self.msgs_by_id = {}  # message_id -> 引用消息 raw_message

    async def set_group_add_request(self, flag=None, sub_type=None, approve=None, **kw):
        if self.fail_group_api:
            raise RuntimeError("network down")
        self.group_requests.append({"flag": flag, "sub_type": sub_type, "approve": approve})

    async def set_friend_add_request(self, flag=None, approve=None, remark=None, **kw):
        self.friend_requests.append({"flag": flag, "approve": approve, "remark": remark})

    async def get_msg(self, message_id=None, **kw):
        raw = self.msgs_by_id.get(int(message_id))
        if raw is None:
            raise RuntimeError("message not found")
        return {"message_id": message_id, "raw_message": raw}


def _group_request(flag="f1", gid=111, uid=222, sub="add", comment=""):
    return GroupRequestEvent(
        flag=flag, sub_type=sub, group_id=gid, user_id=uid, comment=comment,
    )


def _pending_group(flag, gid, uid=222, sub="add"):
    return {
        "kind": "group", "flag": flag, "sub_type": sub,
        "group_id": gid, "user_id": uid, "ts": time.time(),
    }


def _pending_friend(flag, uid):
    return {"kind": "friend", "flag": flag, "user_id": uid, "ts": time.time()}


def _owner_group_msg(gid, text="同意"):
    return GroupMessageEvent(plain=text, user_id=OWNER_ID, group_id=gid, message=[])


def _owner_private_msg(text="同意", reply_to=None):
    segs = []
    if reply_to is not None:
        segs.append(MessageSegment.reply(reply_to))
    segs.append(MessageSegment.text(text))
    return MessageEvent(
        plain=text, user_id=OWNER_ID, message_type="private", message=Message(segs),
    )


def test_save_and_load_keywords(monkeypatch, tmp_path):
    mod = request_manager
    f = tmp_path / "auto_approve.json"
    monkeypatch.setattr(mod, "CONFIG_FILE", str(f))
    saved = mod._save_keywords(123, ["老玩家", "老玩家", "内部"], merge=True)
    assert saved == ["老玩家", "内部"]
    assert mod._load_keywords(123) == ["老玩家", "内部"]
    assert mod._load_keywords(456) == []


def test_save_keywords_merge_keeps_existing(monkeypatch, tmp_path):
    mod = request_manager
    f = tmp_path / "auto_approve.json"
    monkeypatch.setattr(mod, "CONFIG_FILE", str(f))
    mod._save_keywords(111, ["a"])
    merged = mod._save_keywords(111, ["b", "a"], merge=True)
    assert merged == ["a", "b"]


def test_save_keywords_replace(monkeypatch, tmp_path):
    mod = request_manager
    f = tmp_path / "auto_approve.json"
    monkeypatch.setattr(mod, "CONFIG_FILE", str(f))
    mod._save_keywords(111, ["a", "b"])
    replaced = mod._save_keywords(111, ["c"], merge=False)
    assert replaced == ["c"]
    # 文件内容为合法 JSON 且写法原子（无 .tmp 残留）
    data = json.loads(f.read_text(encoding="utf-8"))
    assert data == {"111": ["c"]}
    assert not (tmp_path / "auto_approve.json.tmp").exists()


def test_load_keywords_bad_file(monkeypatch, tmp_path):
    mod = request_manager
    f = tmp_path / "auto_approve.json"
    f.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(mod, "CONFIG_FILE", str(f))
    assert mod._load_keywords(1) == []


def test_purge_pending_ttl():
    mod = request_manager
    mod._pending.clear()
    mod._pending["flag_old"] = {"ts": time.time() - mod._PENDING_TTL - 1}
    mod._pending["flag_new"] = {"ts": time.time()}
    mod._purge_pending()
    assert "flag_old" not in mod._pending
    assert "flag_new" in mod._pending
    mod._pending.clear()


def test_owner_decision_regex_patterns():
    # 引用通知文本解析用的两个正则，直接验证模式行为
    import re
    quoted_friend = "🔔 好友申请\n👤 申请人：123456（08-16 12:00）"
    quoted_group = "🔔 有人申请进群\n🏘 群号：654321\n👤 申请人：123456（08-16 12:00）"
    m = re.search(r"申请人[:：](\d+)", quoted_friend)
    assert m and m.group(1) == "123456"
    g = re.search(r"群号[:：](\d+)", quoted_group)
    assert g and g.group(1) == "654321"


# ---------------- _auto_approve：关键字命中/未命中 ----------------

def test_auto_approve_case_insensitive_hit(monkeypatch, tmp_path):
    mod = request_manager
    monkeypatch.setattr(mod, "CONFIG_FILE", str(tmp_path / "auto_approve.json"))
    mod._save_keywords(111, ["OldPlayer"])

    bot = ApproveBot()
    ev = _group_request(flag="f1", gid=111, uid=222, comment="i am an OLDPLAYER here")
    assert asyncio.run(mod._auto_approve(bot, ev, ev.comment)) is True

    assert bot.group_requests == [
        {"flag": "f1", "sub_type": "add", "approve": True},
    ]
    assert len(bot.sent_private) == 1  # 命中通知私发给 owner
    notice = str(bot.sent_private[0]["message"])
    assert bot.sent_private[0]["user_id"] == OWNER_ID
    assert "自动通过" in notice and "111" in notice and "OldPlayer" in notice


def test_auto_approve_miss_falls_through_to_manual(monkeypatch, tmp_path):
    mod = request_manager
    monkeypatch.setattr(mod, "CONFIG_FILE", str(tmp_path / "auto_approve.json"))
    mod._save_keywords(111, ["暗号"])

    bot = ApproveBot()
    ev = _group_request(flag="f1", gid=111, uid=222, comment="随便看看")
    assert asyncio.run(mod._auto_approve(bot, ev, ev.comment)) is False

    assert bot.group_requests == []  # 未命中不调用审批 API
    assert bot.sent_private == []  # 也不发命中通知（等待人工）


def test_auto_approve_no_keywords_for_group(monkeypatch, tmp_path):
    mod = request_manager
    monkeypatch.setattr(mod, "CONFIG_FILE", str(tmp_path / "auto_approve.json"))
    mod._save_keywords(222, ["暗号"])  # 只给别的群配置

    bot = ApproveBot()
    ev = _group_request(flag="f1", gid=111, uid=222, comment="暗号")
    assert asyncio.run(mod._auto_approve(bot, ev, ev.comment)) is False
    assert bot.group_requests == [] and bot.sent_private == []


# ---------------- _process_decision：群内路径 ----------------

def test_group_decision_only_handles_same_group():
    mod = request_manager
    mod._pending["fA"] = _pending_group("fA", 111, uid=222)
    mod._pending["fB"] = _pending_group("fB", 222, uid=333, sub="invite")

    # 群 222 里"同意"：只处理本群的 fB，不动其他群的 fA
    bot = ApproveBot()
    asyncio.run(mod._process_decision(bot, _owner_group_msg(222), True))
    assert [r["flag"] for r in bot.group_requests] == ["fB"]
    assert bot.group_requests[0]["sub_type"] == "invite"  # 真实 sub_type 透传
    assert "fB" not in mod._pending and "fA" in mod._pending

    # 其他群（没有本群申请）"同意"：不处理任何申请
    bot2 = ApproveBot()
    asyncio.run(mod._process_decision(bot2, _owner_group_msg(999), True))
    assert bot2.group_requests == [] and bot2.friend_requests == []
    assert "fA" in mod._pending
    assert "当前群" in str(bot2.sent_private[-1]["message"])


def test_group_decision_api_failure_restores_pending():
    # 上次审计 #6 回归守护：审批 API 失败时申请必须放回 _pending 供重试
    mod = request_manager
    mod._pending["fA"] = _pending_group("fA", 111, uid=222)
    bot = ApproveBot()
    bot.fail_group_api = True

    asyncio.run(mod._process_decision(bot, _owner_group_msg(111), True))
    assert "fA" in mod._pending
    assert "处理失败" in str(bot.sent_private[-1]["message"])

    # API 恢复后重试同一条"同意"可以成功
    bot.fail_group_api = False
    asyncio.run(mod._process_decision(bot, _owner_group_msg(111), True))
    assert "fA" not in mod._pending
    assert bot.group_requests[-1] == {"flag": "fA", "sub_type": "add", "approve": True}
    assert "已通过进群申请" in str(bot.sent_private[-1]["message"])


# ---------------- _process_decision：私聊引用路径 ----------------

def test_private_decision_reply_resolves_friend_request():
    mod = request_manager
    bot = ApproveBot()
    bot.msgs_by_id[55] = "🔔 好友申请\n👤 申请人：888（08-16 12:00）"
    mod._pending["fF"] = _pending_friend("fF", 888)
    mod._pending["fG"] = _pending_group("fG", 111, uid=999)

    asyncio.run(mod._process_decision(bot, _owner_private_msg("同意", reply_to=55), True))
    assert [r["flag"] for r in bot.friend_requests] == ["fF"]
    assert bot.friend_requests[0]["approve"] is True
    assert "fF" not in mod._pending and "fG" in mod._pending  # 不误伤另一条
    assert "已通过好友申请" in str(bot.sent_private[-1]["message"])


def test_private_decision_reply_resolves_group_request():
    mod = request_manager
    bot = ApproveBot()
    bot.msgs_by_id[66] = "🔔 有人申请进群\n🏘 群号：111\n👤 申请人：999（08-16 12:00）"
    mod._pending["fF"] = _pending_friend("fF", 888)
    mod._pending["fG"] = _pending_group("fG", 111, uid=999)

    asyncio.run(mod._process_decision(bot, _owner_private_msg("拒绝", reply_to=66), False))
    # 好友正则先命中 999 但无好友申请匹配 → 回退群号正则命中 fG
    assert bot.friend_requests == []
    assert [r["flag"] for r in bot.group_requests] == ["fG"]
    assert bot.group_requests[0]["approve"] is False
    assert "fG" not in mod._pending and "fF" in mod._pending
    assert "已拒绝进群申请" in str(bot.sent_private[-1]["message"])


def test_private_decision_reply_no_match_keeps_pending():
    mod = request_manager
    bot = ApproveBot()
    bot.msgs_by_id[77] = "🔔 好友申请\n👤 申请人：777（08-16 12:00）"  # 777 没有待处理申请
    mod._pending["fF"] = _pending_friend("fF", 888)
    mod._pending["fG"] = _pending_group("fG", 111, uid=999)

    asyncio.run(mod._process_decision(bot, _owner_private_msg("同意", reply_to=77), True))
    assert bot.friend_requests == [] and bot.group_requests == []
    assert set(mod._pending) == {"fF", "fG"}  # 匹配不到：两条都不处理
    assert "引用" in str(bot.sent_private[-1]["message"])


# ---------------- 并发/重复处理（pop(key, None) 守护） ----------------

class _RacyPendingDict(dict):
    """模拟并发窗口：items() 视图被消费的同时，目标条目已被另一路处理掉。"""

    def items(self):
        snap = dict(dict.items(self))
        if snap:
            dict.pop(self, next(iter(snap)), None)
        return snap.items()


def test_duplicate_decision_reports_already_processed(monkeypatch):
    mod = request_manager
    racy = _RacyPendingDict()
    racy["fA"] = _pending_group("fA", 111, uid=222)
    monkeypatch.setattr(mod, "_pending", racy)

    bot = ApproveBot()
    asyncio.run(mod._process_decision(bot, _owner_group_msg(111), True))
    # 候选选择与 pop 之间申请被并发处理：提示"已被处理"，绝不重复调用审批 API
    assert bot.group_requests == []
    assert "已被处理" in str(bot.sent_private[-1]["message"])


# ---------------- 群内配置命令：结果私发 ----------------

def test_group_config_command_result_sent_privately(monkeypatch, tmp_path):
    mod = request_manager
    f = tmp_path / "auto_approve.json"
    monkeypatch.setattr(mod, "CONFIG_FILE", str(f))

    bot = ApproveBot()
    handler = mod.auto_on_cmd.handlers[0]
    ev = GroupMessageEvent(plain=".自动通过 暗号ABC", user_id=OWNER_ID, group_id=111, message=[])
    arg = Message(MessageSegment.text("暗号ABC"))

    with pytest.raises(FinishedException) as exc:
        asyncio.run(handler(bot, ev, arg))

    # 群内 matcher 只回简短确认，不含关键字明文
    assert exc.value.message == "✅ 配置结果已私发给你"
    assert mod.auto_on_cmd.finished[-1] == "✅ 配置结果已私发给你"
    assert "暗号ABC" not in str(mod.auto_on_cmd.finished[-1])

    # 完整结果（含关键字）走私发
    assert len(bot.sent_private) == 1
    assert bot.sent_private[0]["user_id"] == OWNER_ID
    private_text = str(bot.sent_private[0]["message"])
    assert "暗号ABC" in private_text and "111" in private_text

    # 配置已持久化
    assert mod._load_keywords(111) == ["暗号ABC"]


def test_private_config_command_finishes_with_full_text(monkeypatch, tmp_path):
    mod = request_manager
    f = tmp_path / "auto_approve.json"
    monkeypatch.setattr(mod, "CONFIG_FILE", str(f))

    bot = ApproveBot()
    handler = mod.auto_on_cmd.handlers[0]
    ev = MessageEvent(plain=".自动通过 111 暗号XYZ", user_id=OWNER_ID,
                      message_type="private", message=[])
    arg = Message(MessageSegment.text("111 暗号XYZ"))

    with pytest.raises(FinishedException) as exc:
        asyncio.run(handler(bot, ev, arg))

    # 私聊里直接 finish 完整结果（本就只有 owner 可见），无需私发
    assert "暗号XYZ" in str(exc.value.message) and "111" in str(exc.value.message)
    assert bot.sent_private == []
    assert mod._load_keywords(111) == ["暗号XYZ"]
