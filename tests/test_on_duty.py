"""on_duty 上号组队榜插件测试：带点严格匹配 / 按群开关 / 开队 / 加入 / 查询 / 下班 / 24h 超时。"""

import asyncio
import sys
import types

import pytest
from conftest import FinishedException, GroupMessageEvent, Message, MessageEvent, MessageSegment

from helpers import load_plugin

plugin = load_plugin("on_duty")

OWNER_ID = 10000  # conftest 固定的 QQBOT_OWNER

# owstats 绑定表桩（测试自定塞入；默认为空 → 名单回退群名片）
_bound_names: dict = {}


def _ev(text, uid=111, gid=888):
    """单文本段消息（严格匹配的正常触发形态）。"""
    return GroupMessageEvent(plain=text, user_id=uid, group_id=gid,
                             message=[MessageSegment.text(text)])


def _raw_msg(*segments):
    return GroupMessageEvent(plain=".上号2=3", user_id=1, group_id=888, message=list(segments))


def _run(fn, *args):
    """调用 handler，吞掉 finish() 抛出的 FinishedException。"""
    try:
        return asyncio.run(fn(*args))
    except FinishedException as exc:
        return exc.message


class FakeBot:
    """get_group_member_info 桩：返回群名片/昵称（card 为空回退 nickname）。"""

    def __init__(self, members=None, fail=False):
        self.members = members or {}
        self.fail = fail

    async def get_group_member_info(self, group_id, user_id):
        if self.fail:
            raise RuntimeError("api down")
        info = self.members.get(str(user_id))
        if info is None:
            return {"card": "", "nickname": f"用户{user_id}"}
        return info


@pytest.fixture(autouse=True)
def _state_file(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(plugin, "_ENABLED_CACHE", None)  # 隔离开群缓存，防测试间串扰


@pytest.fixture(autouse=True)
def _fake_owstats(monkeypatch):
    """桩掉 plugins.owstats：绑定表默认为空，个别用例向 _bound_names 塞数据。"""
    mod = types.ModuleType("plugins.owstats")
    mod._get_bound = lambda uid: _bound_names.get(str(uid), "")
    monkeypatch.setitem(sys.modules, "plugins.owstats", mod)
    _bound_names.clear()
    yield
    _bound_names.clear()


def _arg(text=""):
    return Message([MessageSegment.text(text)])


def _squad_rows(gid="888"):
    return plugin._squads_of(plugin._load_state(), gid)


# ---------------- 严格匹配（命令带点） ----------------

def test_match_create_strict():
    assert plugin._match_re(_ev(".上号2=3"), plugin.RE_CREATE) is not None
    assert plugin._match_re(_ev("  .上号2=3  "), plugin.RE_CREATE) is not None
    # 全角数字/＝/．归一化后等价
    assert plugin._match_re(_ev("．上号２＝３"), plugin.RE_CREATE) is not None
    # 缺点 / 带其他词 / 形态不对：不触发
    for text in ("上号2=3", "上号 2=3", ".上号2=3=4", ".上号2= 3", ".上号2=", ".上号=3",
                 "快.上号2=3", ".上号2=3 缺人", ".上号aa=bb"):
        assert plugin._match_re(_ev(text), plugin.RE_CREATE) is None, text


def test_match_join_strict():
    assert plugin._match_re(_ev(".加入1"), plugin.RE_JOIN) is not None
    assert plugin._match_re(_ev(".加入12"), plugin.RE_JOIN) is not None
    for text in ("加入1", ".加入", ".加入 1", ".加入abc", ".我要加入1", ".加入1号", ".加入123"):
        assert plugin._match_re(_ev(text), plugin.RE_JOIN) is None, text


def test_match_words_strict():
    assert plugin._match_word(_ev(".谁玩"), plugin.WORD_QUERY) is True
    assert plugin._match_word(_ev(".下班"), plugin.WORD_OFF) is True
    assert plugin._match_word(_ev(".上号"), plugin.WORD_ON_HINT) is True
    # 不带点不触发
    for text in ("谁玩", "下班", "上号"):
        assert plugin._match_word(_ev(text), plugin.WORD_QUERY) is False
    for text in (".谁玩啊", "大家.谁玩", ".下班了"):
        assert plugin._match_word(_ev(text), plugin.WORD_QUERY) is False


def test_match_rejects_mixed_segments():
    """@前缀 / 引用回复 / 图片混排：不算「只发命令」，不触发。"""
    assert plugin._match_word(
        _raw_msg(MessageSegment.at(10086), MessageSegment.text(".上号")), plugin.WORD_ON_HINT) is False
    assert plugin._match_re(
        _raw_msg(MessageSegment.reply(123), MessageSegment.text(".上号2=3")), plugin.RE_CREATE) is None
    assert plugin._match_re(
        _raw_msg(MessageSegment.text(".上号2=3"), MessageSegment.image("file://x.png")),
        plugin.RE_CREATE) is None


def test_match_ignores_private_chat():
    assert plugin._match_word(MessageEvent(user_id=1, plain=".上号"), plugin.WORD_ON_HINT) is False
    assert plugin._match_re(MessageEvent(user_id=1, plain=".上号2=3"), plugin.RE_CREATE) is None


# ---------------- 按群开关 ----------------

def test_rules_require_enabled_group():
    plugin._set_enabled("777", True)
    assert asyncio.run(plugin._rule_create(_ev(".上号2=3", gid=777))) is True
    assert asyncio.run(plugin._rule_join(_ev(".加入1", gid=777))) is True
    assert asyncio.run(plugin._rule_query(_ev(".谁玩", gid=777))) is True
    assert asyncio.run(plugin._rule_off(_ev(".下班", gid=777))) is True
    assert asyncio.run(plugin._rule_hint(_ev(".上号", gid=777))) is True
    # 未开启的群：词不触发（按普通消息放行给其他插件）
    assert asyncio.run(plugin._rule_create(_ev(".上号2=3", gid=999))) is False
    assert asyncio.run(plugin._rule_join(_ev(".加入1", gid=999))) is False
    assert asyncio.run(plugin._rule_query(_ev(".谁玩", gid=999))) is False


def test_enable_disable_status_flow():
    owner = _ev(".上号开启", uid=OWNER_ID, gid=888)
    msg = _run(plugin.enable_cmd.handlers[0], owner, _arg())
    assert "已开启群 888" in str(msg) and "本来" not in str(msg)
    assert plugin._is_enabled("888") is True

    msg = _run(plugin.enable_cmd.handlers[0], owner, _arg())
    assert "本来就是开着的" in str(msg)

    msg = _run(plugin.status_cmd.handlers[0], _ev("", uid=OWNER_ID, gid=888))
    assert "888" in str(msg)

    msg = _run(plugin.disable_cmd.handlers[0], _ev("", uid=OWNER_ID, gid=888), _arg())
    assert "已关闭群 888" in str(msg)
    assert plugin._is_enabled("888") is False

    msg = _run(plugin.disable_cmd.handlers[0], _ev("", uid=OWNER_ID, gid=888), _arg())
    assert "本来就没开" in str(msg)


def test_owner_commands_reject_non_owner():
    for fn in (plugin.enable_cmd, plugin.disable_cmd):
        msg = _run(fn.handlers[0], _ev("", uid=222, gid=888), _arg())
        assert "只有主人" in str(msg)
    msg = _run(plugin.status_cmd.handlers[0], _ev("", uid=222, gid=888))
    assert "只有主人" in str(msg)
    assert plugin._is_enabled("888") is False


# ---------------- 开队 ----------------

def test_create_squad_and_numbering():
    msg = _run(plugin.create_matcher.handlers[0], _ev(".上号2=3", uid=111))
    assert "1号队已创建" in str(msg) and "共5人" in str(msg)
    msg = _run(plugin.create_matcher.handlers[0], _ev(".上号1=1", uid=222))
    assert "2号队已创建" in str(msg)

    rows = _squad_rows()
    assert len(rows) == 2
    assert rows[0]["leader"] == "111" and rows[0]["capacity"] == 5 and rows[0]["reserved"] == 1
    assert rows[1]["leader"] == "222" and rows[1]["capacity"] == 2 and rows[1]["reserved"] == 0


def test_create_duplicate_leader_or_member_rejected():
    _run(plugin.create_matcher.handlers[0], _ev(".上号2=3", uid=111))
    _run(plugin.join_matcher.handlers[0], _ev(".加入1", uid=222))

    msg = _run(plugin.create_matcher.handlers[0], _ev(".上号1=1", uid=111))
    assert "你已创建 1号队" in str(msg)
    msg = _run(plugin.create_matcher.handlers[0], _ev(".上号1=1", uid=222))
    assert "你已在 1号队" in str(msg)
    assert len(_squad_rows()) == 1  # 没有新建


def test_create_size_validation():
    for text in (".上号0=3", ".上号2=0", ".上号21=20", ".上号30=30"):
        msg = _run(plugin.create_matcher.handlers[0], _ev(text, uid=111))
        assert "用法" in str(msg), text
    assert _squad_rows() == []


def test_plain_shanghao_gives_usage():
    msg = _run(plugin.hint_matcher.handlers[0], _ev(".上号"))
    assert "用法" in str(msg) and ".上号2=3" in str(msg)


def test_squad_cap_per_group():
    for i in range(plugin.MAX_SQUADS_PER_GROUP):
        _run(plugin.create_matcher.handlers[0], _ev(".上号1=1", uid=100 + i))
    msg = _run(plugin.create_matcher.handlers[0],
               _ev(".上号1=1", uid=999, gid=888))
    assert "小队太多" in str(msg)


def test_save_state_drops_legacy_groups_key():
    plugin._save_state({"groups": {"888": {"legacy": True}}, "enabled": ["888"], "squads": {}})
    assert "groups" not in plugin._load_state()  # 旧版单人榜遗留键不再落盘


# ---------------- 加入 ----------------

def test_join_flow_and_fill_display():
    _run(plugin.create_matcher.handlers[0], _ev(".上号2=3", uid=111))
    _bound_names["333"] = "绑定的名字#1"  # 加入回执不含名字，绑定名仅影响 .谁玩 显示

    msg = _run(plugin.join_matcher.handlers[0], _ev(".加入1", uid=333))
    assert "已加入 1号队（3/5）" in str(msg)
    msg = _run(plugin.join_matcher.handlers[0], _ev(".加入1", uid=444))
    assert "已加入 1号队（4/5）" in str(msg)

    rows = _squad_rows()
    assert [m["uid"] for m in rows[0]["members"]] == ["333", "444"]
    assert plugin._open(rows[0]) == 1


def test_join_errors():
    _run(plugin.create_matcher.handlers[0], _ev(".上号2=3", uid=111))

    msg = _run(plugin.join_matcher.handlers[0], _ev(".加入5", uid=333))
    assert "没有 5号队" in str(msg) and "共 1 支" in str(msg)

    _run(plugin.join_matcher.handlers[0], _ev(".加入1", uid=333))
    msg = _run(plugin.join_matcher.handlers[0], _ev(".加入1", uid=333))
    assert "你已在 1号队" in str(msg)
    msg = _run(plugin.join_matcher.handlers[0], _ev(".加入1", uid=111))
    assert "你已创建 1号队" in str(msg)


def test_join_full_squad_rejected():
    _run(plugin.create_matcher.handlers[0], _ev(".上号4=1", uid=111))  # 容量5：队长+3随行，缺1
    _run(plugin.join_matcher.handlers[0], _ev(".加入1", uid=222))
    msg = _run(plugin.join_matcher.handlers[0], _ev(".加入1", uid=333))
    assert "已满员（5/5）" in str(msg)
    rows = _squad_rows()
    assert [m["uid"] for m in rows[0]["members"]] == ["222"]  # 满员后不再收人


# ---------------- 谁玩 ----------------

def test_query_empty_board():
    q = _run(plugin.who_matcher.handlers[0], FakeBot(), _ev(".谁玩", uid=111))
    assert "没有人在玩" in str(q)


def test_query_lists_squads_with_names():
    _run(plugin.create_matcher.handlers[0], _ev(".上号2=3", uid=111))
    _run(plugin.create_matcher.handlers[0], _ev(".上号1=1", uid=222))
    _run(plugin.join_matcher.handlers[0], _ev(".加入1", uid=333))

    _bound_names["111"] = "绑定的名字#1"
    bot = FakeBot(members={"222": {"card": "老二卡", "nickname": "老二"},
                           "333": {"card": "", "nickname": "老三昵"}})
    q = _run(plugin.who_matcher.handlers[0], bot, _ev(".谁玩", uid=999))
    s = str(q)
    assert "当前小队 2 支" in s
    assert "【1号队】3/5" in s and "缺2" in s and "队长：绑定的名字#1" in s and "（随行1人）" in s
    assert "已加入：老三昵" in s  # 333 未绑定 → 群昵称；队长不出现在已加入行
    assert "空位 2 个，发「.加入1」" in s
    assert "【2号队】1/2" in s and "队长：老二卡" in s and "已加入：暂无" in s
    assert "[CQ:at" not in s  # 纯文本，不 @ 人


def test_query_marks_full_squad():
    _run(plugin.create_matcher.handlers[0], _ev(".上号4=1", uid=111))
    _run(plugin.join_matcher.handlers[0], _ev(".加入1", uid=222))
    q = _run(plugin.who_matcher.handlers[0], FakeBot(), _ev(".谁玩", uid=333))
    s = str(q)
    assert "5/5" in s and "已满员" in s and "空位" not in s


# ---------------- 下班 ----------------

def test_leave_leader_disbands_whole_squad():
    _run(plugin.create_matcher.handlers[0], _ev(".上号2=3", uid=111))
    _run(plugin.join_matcher.handlers[0], _ev(".加入1", uid=222))

    msg = _run(plugin.off_matcher.handlers[0], _ev(".下班", uid=111))
    assert "队长下班" in str(msg) and "1号队已解散" in str(msg)
    assert _squad_rows() == []  # 整队解散，队员记录一并消失

    q = _run(plugin.who_matcher.handlers[0], FakeBot(), _ev(".谁玩", uid=222))
    assert "没有人在玩" in str(q)


def test_leave_member_keeps_squad():
    _run(plugin.create_matcher.handlers[0], _ev(".上号2=3", uid=111))
    _run(plugin.join_matcher.handlers[0], _ev(".加入1", uid=222))

    msg = _run(plugin.off_matcher.handlers[0], _ev(".下班", uid=222))
    assert "已退出1号队" in str(msg)
    rows = _squad_rows()
    assert len(rows) == 1 and rows[0]["leader"] == "111"  # 队伍保留
    assert rows[0]["members"] == []

    q = _run(plugin.who_matcher.handlers[0], FakeBot(), _ev(".谁玩", uid=222))
    assert "1号队" in str(q)


def test_leave_without_register():
    msg = _run(plugin.off_matcher.handlers[0], _ev(".下班", uid=555))
    assert "还没在榜上" in str(msg)


# ---------------- 24 小时超时 ----------------

def test_squad_expires_after_24h(monkeypatch):
    _run(plugin.create_matcher.handlers[0], _ev(".上号2=3", uid=111))
    base = plugin._now()
    monkeypatch.setattr(plugin, "_now", lambda: base + plugin.MAX_SQUAD_LIFETIME + 1)

    q = _run(plugin.who_matcher.handlers[0], FakeBot(), _ev(".谁玩", uid=222))
    assert "没有人在玩" in str(q)
    assert _squad_rows() == []

    msg = _run(plugin.create_matcher.handlers[0], _ev(".上号2=3", uid=111))
    assert "1号队已创建" in str(msg)  # 超时后可重新开队


def test_join_refreshes_squad_timer(monkeypatch):
    _run(plugin.create_matcher.handlers[0], _ev(".上号2=3", uid=111))
    base = plugin._now()
    monkeypatch.setattr(plugin, "_now", lambda: base + 23 * 3600)  # 前进 23 小时
    _run(plugin.join_matcher.handlers[0], _ev(".加入1", uid=222))  # 加入重新计时

    monkeypatch.setattr(plugin, "_now", lambda: base + 25 * 3600)  # 距创建 25h，距加入 2h
    q = _run(plugin.who_matcher.handlers[0], FakeBot(), _ev(".谁玩", uid=333))
    assert "1号队" in str(q)  # 加入刷新计时，未被解散


# ---------------- 榜按群隔离 ----------------

def test_boards_are_per_group():
    _run(plugin.create_matcher.handlers[0], _ev(".上号2=3", uid=111, gid=888))
    q = _run(plugin.who_matcher.handlers[0], FakeBot(), _ev(".谁玩", uid=222, gid=999))
    assert "没有人在玩" in str(q)
    q = _run(plugin.who_matcher.handlers[0], FakeBot(), _ev(".谁玩", uid=222, gid=888))
    assert "1号队" in str(q)


# ---------------- 显示名回退链 ----------------

def test_member_name_binding_beats_group_card():
    _bound_names["111"] = "绑定名#9"
    name = asyncio.run(plugin._member_name(
        FakeBot(members={"111": {"card": "卡片名", "nickname": "昵称"}}), "888", "111"))
    assert name == "绑定名#9"


def test_member_name_card_then_nickname():
    name = asyncio.run(plugin._member_name(
        FakeBot(members={"111": {"card": "", "nickname": "纯昵称"}}), "888", "111"))
    assert name == "纯昵称"
    name = asyncio.run(plugin._member_name(
        FakeBot(members={"111": {"card": "群名片", "nickname": "昵称"}}), "888", "111"))
    assert name == "群名片"


def test_member_name_falls_back_to_uid_on_api_failure():
    name = asyncio.run(plugin._member_name(FakeBot(fail=True), "888", "111"))
    assert name == "111"


def test_member_name_missing_owstats_uses_group_card(monkeypatch):
    monkeypatch.setitem(sys.modules, "plugins.owstats", None)  # import → ImportError
    name = asyncio.run(plugin._member_name(
        FakeBot(members={"111": {"card": "卡片名", "nickname": "昵称"}}), "888", "111"))
    assert name == "卡片名"
