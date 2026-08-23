import asyncio
import json
import time

import pytest
from conftest import Bot, GroupDecreaseNoticeEvent, GroupIncreaseNoticeEvent

from helpers import load_plugin

group_leave = load_plugin("group_leave")


class GroupBot(Bot):
    """带群 API 的假 bot：记录群消息，可配置成员/群列表。"""

    def __init__(self):
        super().__init__()
        self.sent_group = []
        self.group_list = []
        self.member_lists = {}  # gid -> 成员列表（缺省时 API 报错）
        self.member_infos = {}  # (gid, uid) -> 群成员信息
        self.strangers = {}  # uid -> 昵称

    async def send_group_msg(self, group_id=None, message=None, **kw):
        self.sent_group.append({"group_id": group_id, "message": message})

    async def get_group_list(self, **kw):
        return self.group_list

    async def get_group_member_list(self, group_id=None, **kw):
        if group_id not in self.member_lists:
            raise RuntimeError("api error")
        return self.member_lists[group_id]

    async def get_group_member_info(self, group_id=None, user_id=None, **kw):
        info = self.member_infos.get((group_id, user_id))
        if info is None:
            raise RuntimeError("no such member")
        return info

    async def get_stranger_info(self, user_id=None, **kw):
        return {"nickname": self.strangers.get(user_id, f"路人{user_id}")}


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """状态文件指向临时目录；入群记录/已导入群快照隔离，测后还原。"""
    monkeypatch.setattr(group_leave, "STATE_FILE", str(tmp_path / "join_state.json"))
    saved_join = dict(group_leave._join_ts)
    saved_imported = set(group_leave._imported_groups)
    group_leave._join_ts.clear()
    group_leave._imported_groups.clear()
    yield
    group_leave._join_ts.clear()
    group_leave._join_ts.update(saved_join)
    group_leave._imported_groups.clear()
    group_leave._imported_groups.update(saved_imported)


# ---------------- _format_duration 各档位 ----------------

@pytest.mark.parametrize("seconds,expected", [
    (0, "0 秒"),
    (59, "59 秒"),
    (125, "2 分 5 秒"),
    (3599, "59 分 59 秒"),
    (3600, "1 小时 0 分钟"),
    (3661, "1 小时 1 分钟"),
    (86399, "23 小时 59 分钟"),
    (86400, "1 天 0 小时"),
    (90061, "1 天 1 小时"),
])
def test_format_duration_tiers(seconds, expected):
    assert group_leave._format_duration(seconds) == expected


# ---------------- 入群记录与一次性落盘 ----------------

def test_import_existing_members_persists_once(monkeypatch):
    mod = group_leave
    bot = GroupBot()
    bot.group_list = [{"group_id": 1}, {"group_id": 2}]
    bot.member_lists = {
        # 机器人自己（10000）不记录
        1: [{"user_id": 11, "join_time": 1000}, {"user_id": 10000, "join_time": 900}],
        # join_time 无效回退当前时间；缺失 join_time 同样回退
        2: [{"user_id": 21, "join_time": "bad"}, {"user_id": 22}],
    }
    saves = []

    async def fake_save():
        saves.append(1)

    monkeypatch.setattr(mod, "_save_state_async", fake_save)
    asyncio.run(mod._import_existing_members(bot))

    assert len(saves) == 1  # 所有群导完只落盘一次
    assert mod._join_ts[(1, 11)] == 1000
    assert (1, 10000) not in mod._join_ts
    assert mod._join_ts[(2, 21)] > time.time() - 60
    assert (2, 22) in mod._join_ts
    assert mod._imported_groups == {1, 2}

    # 二次触发：已导入过的群不再重复拉成员列表（重复拉取才是昂贵操作；
    # 连接末尾的整体落盘按设计每次连接至多一次，不构成额外回归点）
    async def strict(group_id=None, **kw):
        raise AssertionError(f"不应再次拉取群 {group_id} 成员列表")

    bot.get_group_member_list = strict
    asyncio.run(mod._import_existing_members(bot))
    assert mod._join_ts[(1, 11)] == 1000  # 记录不受二次导入影响


def test_import_does_not_overwrite_recorded_join(monkeypatch):
    mod = group_leave
    mod._join_ts[(1, 11)] = 555.0  # 进群通知已写入的记录
    bot = GroupBot()
    asyncio.run(mod._import_group_members(bot, 1))  # get_group_member_list 失败直接返回
    assert mod._join_ts[(1, 11)] == 555.0
    assert 1 not in mod._imported_groups  # 失败的群不计为已导入，下次连接可重试


def test_welcome_handler_records_join_and_sends():
    mod = group_leave
    bot = GroupBot()
    bot.strangers = {777: "新同学"}
    ev = GroupIncreaseNoticeEvent(group_id=111, user_id=777, sub_type="approve", self_id=10000)

    asyncio.run(mod.handle_welcome(bot, ev))

    assert len(bot.sent_group) == 1
    sent = bot.sent_group[0]
    assert sent["group_id"] == 111
    msg = sent["message"]
    assert msg.segments[0].type == "at" and msg.segments[0].data["qq"] == 777
    text = str(msg)
    assert "欢迎" in text and "新同学" in text
    assert any(w in text for w in mod.WELCOME_MESSAGES)

    # 入群时间已记录并落盘
    assert (111, 777) in mod._join_ts
    with open(mod.STATE_FILE, encoding="utf-8") as f:
        data = json.load(f)
    assert "111:777" in data


def test_welcome_handler_skips_bot_itself():
    mod = group_leave
    bot = GroupBot()
    ev = GroupIncreaseNoticeEvent(group_id=111, user_id=10000, self_id=10000)  # 机器人自己进群
    asyncio.run(mod.handle_welcome(bot, ev))
    assert bot.sent_group == []
    assert (111, 10000) not in mod._join_ts


# ---------------- 退群播报组装 ----------------

def test_leave_broadcast_with_duration():
    mod = group_leave
    bot = GroupBot()
    bot.strangers = {888: "老朋友"}
    mod._join_ts[(111, 888)] = time.time() - 7200  # 待了 2 小时

    ev = GroupDecreaseNoticeEvent(group_id=111, user_id=888, sub_type="leave", operator_id=1)
    asyncio.run(mod.handle(bot, ev))

    assert len(bot.sent_group) == 1
    sent = bot.sent_group[0]
    assert sent["group_id"] == 111
    text = str(sent["message"])
    assert "退群了" in text and "老朋友" in text and "888" in text
    assert "在群里待了" in text and "2 小时" in text
    assert any(m in text for m in mod.MESSAGES)
    assert (111, 888) not in mod._join_ts  # 播报后弹出记录


def test_leave_broadcast_without_record_omits_duration():
    mod = group_leave
    bot = GroupBot()
    ev = GroupDecreaseNoticeEvent(group_id=111, user_id=999, sub_type="leave")
    asyncio.run(mod.handle(bot, ev))
    text = str(bot.sent_group[0]["message"])
    assert "退群了" in text
    assert "在群里待了" not in text


def test_kick_broadcast_names_operator():
    mod = group_leave
    bot = GroupBot()
    bot.strangers = {888: "捣蛋鬼"}
    bot.member_infos[(111, 1)] = {"card": "管理员甲", "nickname": "甲"}
    mod._join_ts[(111, 888)] = time.time() - 125  # 2 分 5 秒

    ev = GroupDecreaseNoticeEvent(group_id=111, user_id=888, sub_type="kick", operator_id=1)
    asyncio.run(mod.handle(bot, ev))

    text = str(bot.sent_group[0]["message"])
    assert "被 管理员甲 移出了群" in text
    assert "捣蛋鬼" in text
    assert "2 分 5 秒" in text


def test_unknown_sub_type_is_ignored():
    mod = group_leave
    bot = GroupBot()
    mod._join_ts[(111, 888)] = time.time()
    ev = GroupDecreaseNoticeEvent(group_id=111, user_id=888, sub_type="kick_me")
    asyncio.run(mod.handle(bot, ev))
    assert bot.sent_group == []
    assert (111, 888) in mod._join_ts  # 未知 sub_type 不丢逗留时长数据
