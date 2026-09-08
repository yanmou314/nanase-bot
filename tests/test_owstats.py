import asyncio

import pytest
from conftest import FinishedException, Message, MessageEvent, MessageSegment

from helpers import load_plugin

owstats = load_plugin("owstats")


@pytest.fixture(autouse=True)
def _reset_relay_state():
    """隔离查询冷却与中继队列状态，测试互不影响。"""
    def _clear():
        owstats._last_query.clear()
        owstats._task_queue.clear()
        owstats._task_current = None
        owstats._task_seq = 0
        owstats._orphan_image_times.clear()
    _clear()
    yield
    _clear()


def _msg(text):
    return Message([MessageSegment.text(text)])


def _run(fn, *args):
    """调用 handler，吞掉 finish() 抛出的 FinishedException。"""
    try:
        return asyncio.run(fn(*args))
    except FinishedException as exc:
        return exc.message


# ---------------- 中继模式：任务标签与进度识别 ----------------

def test_task_kind_label_maps_all_kinds():
    assert owstats._task_kind_label("matchrep") == "战报"
    assert owstats._task_kind_label("rankhist") == "段位"
    assert owstats._task_kind_label("strength") == "强度"
    assert owstats._task_kind_label("summary") == "总结"
    assert owstats._task_kind_label("verify") == "verify"  # 未知类型原样返回


def test_progress_re_matches_progress_but_not_result():
    # 对方机器人的纯文本进度提示（不含图片）：忽略，不消费任务
    for text in ("正在生成，请稍候", "排队中，前面还有 2 个任务", "查询中…"):
        assert owstats._PROGRESS_RE.search(text)
    # 普通文本结果不会误判为进度提示
    assert not owstats._PROGRESS_RE.search("Yanmou#51293 的战报")
    assert not owstats._PROGRESS_RE.search("")


# ---------------- 目标解析与冷却 ----------------

def test_resolve_tag_flags_invalid_explicit_input():
    ev = MessageEvent(user_id=1)
    # 显式输入但没带 #数字：必须标记为无效，而不是静默回退查自己
    assert owstats._resolve_tag(_msg("张三"), ev) == ("", True)
    # 合法 ID 正常解析
    assert owstats._resolve_tag(_msg("Yanmou#51293"), ev) == ("Yanmou#51293", False)


def test_resolve_tag_falls_back_to_binding(monkeypatch):
    ev = MessageEvent(user_id=42)
    monkeypatch.setattr(owstats, "_get_bound", lambda uid: "Bound#111")
    assert owstats._resolve_tag(_msg("  "), ev) == ("Bound#111", False)
    assert owstats._resolve_tag(_msg("Yanmou#51293 extra"), ev) == ("Yanmou#51293", False)


def test_query_cooldown_blocks_second_call_within_window():
    assert owstats._check_cooldown("u1") == 0.0  # 首次放行并记账
    remain = owstats._check_cooldown("u1")
    assert 0 < remain <= owstats._QUERY_COOLDOWN
    assert owstats._check_cooldown("u2") == 0.0  # 不同用户互不影响


# ---------------- 维护开关 ----------------

def test_maintenance_toggle_and_file_state(tmp_path, monkeypatch):
    monkeypatch.setattr(owstats, "MAINTENANCE_FILE", str(tmp_path / "maintenance.json"))
    assert owstats._is_maintenance() is False  # 缺文件 → 不维护
    ev = MessageEvent(user_id=10000, group_id=888)  # conftest 固定 QQBOT_OWNER=10000
    _run(owstats.maintenance_toggle, ev, Message([MessageSegment.text("开启")]))
    assert owstats._is_maintenance() is True
    _run(owstats.maintenance_toggle, ev, Message([MessageSegment.text("关闭")]))
    assert owstats._is_maintenance() is False


def test_maintenance_toggle_rejects_non_owner():
    ev = MessageEvent(user_id=222, group_id=888)
    msg = _run(owstats.maintenance_toggle, ev, Message([MessageSegment.text("开启")]))
    assert "仅Bot主人" in str(msg)


# ---------------- 两段归集：文本+图片 / 报错中断 / 延迟图片丢弃 ----------------

def _relay_bot():
    class _FakeBot:
        def __init__(self):
            self.self_id = "10000"
            self.sent_group = []
            self.sent_private = []

        async def send_group_msg(self, group_id=None, message=None, **kwargs):
            self.sent_group.append({"group_id": group_id, "message": message})
            return {"message_id": 1}

        async def send_private_msg(self, user_id=None, message=None, **kwargs):
            self.sent_private.append({"user_id": user_id, "message": message})
            return {"message_id": 1}

    return _FakeBot()


def _relay_event(message):
    from conftest import GroupMessageEvent
    return GroupMessageEvent(
        group_id=owstats.RELAY_GROUP_ID,
        user_id=int(owstats.RELAY_BOT_QQ),
        message=message,
    )


def _make_task(**kw):
    import time as _time
    task = {"seq": 99, "kind": "summary", "tag": "A#1",
            "group_id": 123456, "user_id": "999", "t0": _time.monotonic()}
    task.update(kw)
    return task


def test_relay_first_text_buffers_awaits_second():
    bot = _relay_bot()
    owstats._task_current = _make_task()
    ev = _relay_event(Message([MessageSegment.text("Yanmou 的今日总结")]))
    asyncio.run(owstats._relay_result(bot, ev))
    assert owstats._task_current is not None
    assert owstats._task_current.get("first_segs")
    assert bot.sent_group == []


def test_relay_second_image_combines_and_forwards():
    bot = _relay_bot()
    owstats._task_current = _make_task()
    asyncio.run(owstats._relay_result(bot, _relay_event(Message([MessageSegment.text("正文")]))))
    asyncio.run(owstats._relay_result(bot, _relay_event(Message([MessageSegment.image("http://x/1.png")]))))
    assert owstats._task_current is None
    assert len(bot.sent_group) == 1
    body = str(bot.sent_group[0]["message"])
    assert "正文" in body and "1.png" in body


def test_relay_second_text_aborts_as_error():
    bot = _relay_bot()
    owstats._task_current = _make_task()
    asyncio.run(owstats._relay_result(bot, _relay_event(Message([MessageSegment.text("正文")]))))
    asyncio.run(owstats._relay_result(bot, _relay_event(Message([MessageSegment.text("查询失败：ID不存在")]))))
    assert owstats._task_current is None
    assert len(bot.sent_group) == 1
    assert "查询失败" in str(bot.sent_group[0]["message"])


def test_relay_first_image_without_orphan_treated_as_single():
    bot = _relay_bot()
    owstats._task_current = _make_task()
    asyncio.run(owstats._relay_result(bot, _relay_event(Message([MessageSegment.image("http://x/only.png")]))))
    assert owstats._task_current is None
    assert len(bot.sent_group) == 1
    assert "only.png" in str(bot.sent_group[0]["message"])


def test_relay_first_image_with_orphan_dropped_as_stale():
    owstats._note_missing_image()  # 模拟上一任务缺图片收尾
    bot = _relay_bot()
    owstats._task_current = _make_task()
    asyncio.run(owstats._relay_result(bot, _relay_event(Message([MessageSegment.image("http://x/old.png")]))))
    assert owstats._task_current is not None
    assert not owstats._task_current.get("first_segs")
    assert bot.sent_group == []
    # 随后真正的文本+图片仍能正常归集转发
    asyncio.run(owstats._relay_result(bot, _relay_event(Message([MessageSegment.text("正文")]))))
    asyncio.run(owstats._relay_result(bot, _relay_event(Message([MessageSegment.image("http://x/new.png")]))))
    assert owstats._task_current is None
    assert len(bot.sent_group) == 1
    assert "new.png" in str(bot.sent_group[0]["message"])


def test_relay_orphan_expires_after_ttl(monkeypatch):
    owstats._note_missing_image()
    monkeypatch.setattr(owstats, "_ORPHAN_IMAGE_TTL", -1)  # 立即过期
    bot = _relay_bot()
    owstats._task_current = _make_task()
    asyncio.run(owstats._relay_result(bot, _relay_event(Message([MessageSegment.image("http://x/only.png")]))))
    assert owstats._task_current is None  # 过期后按单图成功处理
    assert len(bot.sent_group) == 1


def test_owstatus_and_reset_owner_only():
    ev_owner = MessageEvent(user_id=10000, group_id=888)
    msg = _run(owstats.ow_status, ev_owner)
    assert "无在途任务" in str(msg)
    ev_other = MessageEvent(user_id=222, group_id=888)
    msg2 = _run(owstats.ow_status, ev_other)
    assert "仅Bot主人" in str(msg2)
    owstats._task_current = _make_task()
    msg3 = _run(owstats.ow_reset, ev_owner)
    assert "已丢弃在途任务" in str(msg3)
    assert owstats._task_current is None


def test_relay_progress_still_ignored():
    bot = _relay_bot()
    owstats._task_current = _make_task()
    asyncio.run(owstats._relay_result(bot, _relay_event(Message([MessageSegment.text("正在生成，请稍候")]))))
    assert owstats._task_current is not None
    assert not owstats._task_current.get("first_segs")
    assert bot.sent_group == []


def test_relay_future_resolves_combined():
    async def _go():
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        owstats._task_current = _make_task(future=fut, group_id=None, user_id="")
        bot = _relay_bot()
        await owstats._relay_result(bot, _relay_event(Message([MessageSegment.text("正文")])))
        assert not fut.done()
        await owstats._relay_result(bot, _relay_event(Message([MessageSegment.image("http://x/1.png")])))
        assert fut.done()
        segs, has_image = fut.result()
        assert has_image is True
        assert len(segs) == 2
    asyncio.run(_go())
