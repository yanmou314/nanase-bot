import asyncio
import time

import pytest
from conftest import FinishedException, Message, MessageEvent, MessageSegment

from helpers import load_plugin

owstats = load_plugin("owstats")


class _FakeMatcher:
    def __init__(self):
        self.block = False


def _relay(bot, ev):
    """带 matcher stub 的 _relay_result 调用（handler 需要 Matcher 注入）。"""
    return owstats._relay_result(bot, ev, _FakeMatcher())


@pytest.fixture(autouse=True)
def _reset_relay_state():
    """隔离查询冷却与中继队列状态，测试互不影响。"""
    def _clear():
        owstats._last_query.clear()
        owstats._task_queue.clear()
        owstats._task_current = None
        owstats._task_seq = 0
        owstats._orphan_image_times.clear()
        owstats._frozen_tasks.clear()
        owstats._claiming_task = None
        owstats._claim_sent_at = 0.0
        owstats._discarded_until = 0.0
        owstats._flush_tasks.clear()
        owstats._dispatch_lock = None
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
    asyncio.run(_relay(bot, ev))
    assert owstats._task_current is not None
    assert owstats._task_current.get("first_segs")
    assert bot.sent_group == []


def test_relay_second_image_combines_and_forwards():
    bot = _relay_bot()
    owstats._task_current = _make_task()
    asyncio.run(_relay(bot, _relay_event(Message([MessageSegment.text("正文")]))))
    asyncio.run(_relay(bot, _relay_event(Message([MessageSegment.image("http://x/1.png")]))))
    assert owstats._task_current is None
    assert len(bot.sent_group) == 1
    body = str(bot.sent_group[0]["message"])
    assert "正文" in body and "1.png" in body


def test_relay_second_text_aborts_as_error():
    bot = _relay_bot()
    owstats._task_current = _make_task()
    asyncio.run(_relay(bot, _relay_event(Message([MessageSegment.text("正文")]))))
    asyncio.run(_relay(bot, _relay_event(Message([MessageSegment.text("查询失败：ID不存在")]))))
    assert owstats._task_current is None
    assert len(bot.sent_group) == 1
    assert "查询失败" in str(bot.sent_group[0]["message"])


def test_relay_first_image_without_orphan_treated_as_single():
    bot = _relay_bot()
    owstats._task_current = _make_task()
    asyncio.run(_relay(bot, _relay_event(Message([MessageSegment.image("http://x/only.png")]))))
    assert owstats._task_current is None
    assert len(bot.sent_group) == 1
    assert "only.png" in str(bot.sent_group[0]["message"])


def test_relay_first_image_with_orphan_dropped_as_stale():
    owstats._note_missing_image()  # 模拟上一任务缺图片收尾
    bot = _relay_bot()
    owstats._task_current = _make_task()
    asyncio.run(_relay(bot, _relay_event(Message([MessageSegment.image("http://x/old.png")]))))
    assert owstats._task_current is not None
    assert not owstats._task_current.get("first_segs")
    assert bot.sent_group == []
    # 随后真正的文本+图片仍能正常归集转发
    asyncio.run(_relay(bot, _relay_event(Message([MessageSegment.text("正文")]))))
    asyncio.run(_relay(bot, _relay_event(Message([MessageSegment.image("http://x/new.png")]))))
    assert owstats._task_current is None
    assert len(bot.sent_group) == 1
    assert "new.png" in str(bot.sent_group[0]["message"])


def test_relay_orphan_expires_after_ttl(monkeypatch):
    owstats._note_missing_image()
    monkeypatch.setattr(owstats, "_ORPHAN_IMAGE_TTL", -1)  # 立即过期
    bot = _relay_bot()
    owstats._task_current = _make_task()
    asyncio.run(_relay(bot, _relay_event(Message([MessageSegment.image("http://x/only.png")]))))
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
    assert "已丢弃" in str(msg3) and "在途" in str(msg3)
    assert owstats._task_current is None


def test_relay_progress_still_ignored():
    bot = _relay_bot()
    owstats._task_current = _make_task()
    asyncio.run(_relay(bot, _relay_event(Message([MessageSegment.text("正在生成，请稍候")]))))
    assert owstats._task_current is not None
    assert not owstats._task_current.get("first_segs")
    assert bot.sent_group == []


def test_relay_future_resolves_combined():
    async def _go():
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        owstats._task_current = _make_task(future=fut, group_id=None, user_id="")
        bot = _relay_bot()
        await _relay(bot, _relay_event(Message([MessageSegment.text("正文")])))
        assert not fut.done()
        await _relay(bot, _relay_event(Message([MessageSegment.image("http://x/1.png")])))
        assert fut.done()
        segs, has_image = fut.result()
        assert has_image is True
        assert len(segs) == 2
    asyncio.run(_go())


# ---------------- 绘制超时：冻结 → 自动领取 → 覆盖重发 ----------------

_DRAWING_TEXT = (
    "@nanase <@32196B7A6081B25F0A8970B5EFF8C33B> 仍在绘制中，"
    "但预计会超过 QQ 官方 5 分钟回复时限。图片准备好后请再次 @机器人领取，缓存 24 小时。"
)


def test_drawing_re_matches_notice_but_not_progress():
    assert owstats._DRAWING_RE.search(_DRAWING_TEXT)
    assert owstats._DRAWING_RE.search("仍在绘制中，请稍后")
    assert not owstats._DRAWING_RE.search("正在生成，请稍候")
    assert not owstats._DRAWING_RE.search("Yanmou 的今日总结")


def test_drawing_notice_freezes_task_and_notifies_user():
    bot = _relay_bot()
    owstats._task_current = _make_task()
    asyncio.run(_relay(bot, _relay_event(Message([MessageSegment.text(_DRAWING_TEXT)]))))
    assert owstats._task_current is None
    assert len(owstats._frozen_tasks) == 1
    frozen = owstats._frozen_tasks[0]
    assert frozen.get("claim_at", 0) > frozen.get("frozen_at", 0)
    # 通知原用户等待领取
    assert len(bot.sent_group) == 1
    body = str(bot.sent_group[0]["message"])
    assert "5 分钟" in body or "5分钟" in body
    assert "领取" in body


def test_drawing_notice_as_second_msg_also_freezes():
    bot = _relay_bot()
    owstats._task_current = _make_task()
    asyncio.run(_relay(bot, _relay_event(Message([MessageSegment.text("标题正文")]))))
    asyncio.run(_relay(bot, _relay_event(Message([MessageSegment.text(_DRAWING_TEXT)]))))
    assert owstats._task_current is None
    assert len(owstats._frozen_tasks) == 1
    assert not owstats._frozen_tasks[0].get("first_segs")


def test_freeze_dispatches_next_queued_task():
    class _Bot:
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

    bot = _Bot()
    owstats._task_current = _make_task(seq=1)
    nxt = _make_task(seq=2, kind="strength", tag="B#2")
    owstats._task_queue.append(nxt)

    def _fake_get_bot():
        return bot

    orig = owstats.get_bot
    owstats.get_bot = _fake_get_bot
    try:
        asyncio.run(_relay(bot, _relay_event(Message([MessageSegment.text(_DRAWING_TEXT)]))))
    finally:
        owstats.get_bot = orig
    assert len(owstats._frozen_tasks) == 1
    assert owstats._task_current is not None
    assert owstats._task_current.get("seq") == 2
    # 冻结通知 + 派发下一个查询
    kinds = [str(x["message"]) for x in bot.sent_group]
    assert any("领取" in k for k in kinds)
    assert any("快速强度指数" in k for k in kinds)


def test_send_claim_at_after_delay():
    class _Bot:
        def __init__(self):
            self.sent_group = []

        async def send_group_msg(self, group_id=None, message=None, **kwargs):
            self.sent_group.append({"group_id": group_id, "message": message})
            return {"message_id": 1}

    bot = _Bot()
    frozen = _make_task(seq=7)
    frozen["claim_at"] = 0.0  # 已到期
    owstats._frozen_tasks.append(frozen)
    # 在途任务会被领取覆盖，应先放回队首
    owstats._task_current = _make_task(seq=8, kind="strength", tag="B#2")

    async def _go():
        assert await owstats._send_claim_at(bot) is True
    asyncio.run(_go())
    assert owstats._claiming_task is frozen
    assert len(bot.sent_group) == 1
    assert bot.sent_group[0]["group_id"] == owstats.RELAY_GROUP_ID
    assert owstats._task_current is None
    assert owstats._task_queue and owstats._task_queue[0].get("seq") == 8


def test_dispatch_blocked_while_claiming():
    class _Bot:
        def __init__(self):
            self.sent_group = []

        async def send_group_msg(self, group_id=None, message=None, **kwargs):
            self.sent_group.append({"group_id": group_id, "message": message})
            return {"message_id": 1}

    bot = _Bot()
    owstats._claiming_task = _make_task(seq=1)
    owstats._task_queue.append(_make_task(seq=2, kind="strength", tag="B#2"))

    def _fake_get_bot():
        return bot

    orig = owstats.get_bot
    owstats.get_bot = _fake_get_bot
    try:
        asyncio.run(owstats._dispatch_next())
    finally:
        owstats.get_bot = orig
    assert owstats._task_current is None
    assert len(owstats._task_queue) == 1
    assert bot.sent_group == []


def test_second_image_not_stolen_by_pending_frozen():
    """冻结待领取时，在途任务的第二条图仍应正常归集，不能被冻结抢走。"""
    bot = _relay_bot()
    frozen = _make_task(seq=1, user_id="111", group_id=10001)
    frozen["claim_at"] = time.monotonic() + 1000  # 还没到领取时间
    owstats._frozen_tasks.append(frozen)
    current = _make_task(seq=2, user_id="222", group_id=10002)
    owstats._task_current = current

    asyncio.run(_relay(bot, _relay_event(Message([MessageSegment.text("正文")]))))
    assert current.get("first_segs")
    asyncio.run(_relay(bot, _relay_event(Message([MessageSegment.image("http://x/ok.png")]))))
    # 正常完成当前任务，冻结仍挂着
    assert owstats._task_current is None
    assert len(owstats._frozen_tasks) == 1
    delivered = [s for s in bot.sent_group if s["group_id"] == 10002]
    assert delivered and "ok.png" in str(delivered[0]["message"])
    assert "正文" in str(delivered[0]["message"])
    # 不应把图发给冻结用户
    assert not any(s["group_id"] == 10001 for s in bot.sent_group)


def test_first_image_without_claim_goes_to_frozen_and_requeues():
    """未发领取 @，但对方把缓存图贴在了新查询后：首条图仍给冻结用户。"""
    bot = _relay_bot()
    frozen = _make_task(seq=1, user_id="111", group_id=10001)
    frozen["t0"] = time.monotonic() - 400
    owstats._frozen_tasks.append(frozen)
    current = _make_task(seq=2, user_id="222", group_id=10002, kind="strength", tag="B#2")
    owstats._task_current = current

    asyncio.run(_relay(bot, _relay_event(Message([MessageSegment.image("http://x/cached.png")]))))
    delivered = [s for s in bot.sent_group if s["group_id"] == 10001]
    assert delivered and "cached.png" in str(delivered[0]["message"])
    assert owstats._task_current is None
    assert owstats._task_queue and owstats._task_queue[0].get("seq") == 2
    assert not owstats._frozen_tasks


def test_send_claim_at_respects_delay():
    bot = _relay_bot()
    frozen = _make_task()
    frozen["claim_at"] = time.monotonic() + 1000
    owstats._frozen_tasks.append(frozen)

    async def _go():
        assert await owstats._send_claim_at(bot) is False
    asyncio.run(_go())
    assert owstats._claiming_task is None


def test_first_image_delivers_to_frozen_and_requeues_current():
    bot = _relay_bot()
    frozen = _make_task(seq=1, user_id="111", group_id=10001)
    frozen["t0"] = time.monotonic() - 400
    owstats._frozen_tasks.append(frozen)
    # 模拟已在领取中
    owstats._claiming_task = frozen
    owstats._frozen_tasks.remove(frozen)
    # 在途另一用户查询（会被覆盖）
    current = _make_task(seq=2, user_id="222", group_id=10002, kind="strength", tag="B#2")
    owstats._task_current = current

    asyncio.run(_relay(bot, _relay_event(Message([MessageSegment.image("http://x/done.png")]))))
    # 冻结用户收到图片
    delivered = [s for s in bot.sent_group if s["group_id"] == 10001]
    assert delivered and "done.png" in str(delivered[0]["message"])
    assert "绘制完成" in str(delivered[0]["message"])
    # 在途任务被覆盖 → 重新入队，不在途
    assert owstats._task_current is None
    assert len(owstats._task_queue) == 1
    assert owstats._task_queue[0].get("seq") == 2
    assert owstats._claiming_task is None


def test_first_image_without_frozen_keeps_existing_orphan_behavior():
    owstats._note_missing_image()
    bot = _relay_bot()
    owstats._task_current = _make_task()
    asyncio.run(_relay(bot, _relay_event(Message([MessageSegment.image("http://x/old.png")]))))
    assert owstats._task_current is not None
    assert bot.sent_group == []


def test_claim_timeout_fails_frozen_task():
    bot = _relay_bot()
    frozen = _make_task(seq=3, user_id="333", group_id=10003)
    frozen["t0"] = time.monotonic() - 600
    owstats._claiming_task = frozen
    owstats._claim_sent_at = time.monotonic() - 9999

    def _fake_get_bot():
        return bot

    orig = owstats.get_bot
    owstats.get_bot = _fake_get_bot
    try:
        asyncio.run(owstats._fail_claim_or_stale())
    finally:
        owstats.get_bot = orig
    assert owstats._claiming_task is None
    assert any("领取超时" in str(s["message"]) for s in bot.sent_group)




def test_claim_send_failure_opens_discard_window():
    """领取 @ 发送失败：在途任务已 requeue，必须开隔离窗防止旧回复串台。"""
    class _Bot:
        async def send_group_msg(self, **kwargs):
            raise RuntimeError("send fail")
    bot = _Bot()
    frozen = _make_task(seq=1)
    frozen["claim_at"] = 0.0
    owstats._frozen_tasks.append(frozen)
    owstats._task_current = _make_task(seq=2, kind="strength", tag="B#2")

    async def _go():
        assert await owstats._send_claim_at(bot) is False
    asyncio.run(_go())
    assert owstats._claiming_task is None
    assert owstats._task_current is None
    assert owstats._task_queue and owstats._task_queue[0].get("seq") == 2
    assert owstats._in_discard_window()


def test_dispatch_blocked_during_discard_window():
    """超时/重置打开隔离窗后，不得立刻派发下一任务，避免其回复被当迟到消息丢弃。"""
    class _Bot:
        def __init__(self):
            self.sent_group = []
        async def send_group_msg(self, group_id=None, message=None, **kwargs):
            self.sent_group.append({"group_id": group_id, "message": message})
            return {"message_id": 1}
    bot = _Bot()
    owstats._open_discard_window(30)
    owstats._task_queue.append(_make_task(seq=5, kind="strength", tag="B#2"))
    orig = owstats.get_bot
    owstats.get_bot = lambda: bot
    try:
        asyncio.run(owstats._dispatch_next())
    finally:
        owstats.get_bot = orig
    assert owstats._task_current is None
    assert len(owstats._task_queue) == 1
    assert bot.sent_group == []
    # 窗结束后可正常派发
    owstats._close_discard_window()
    owstats.get_bot = lambda: bot
    try:
        asyncio.run(owstats._dispatch_next())
    finally:
        owstats.get_bot = orig
    assert owstats._task_current is not None
    assert owstats._task_current.get("seq") == 5
    assert len(bot.sent_group) == 1


def test_ow_status_shows_frozen():
    import time as _time
    ev_owner = MessageEvent(user_id=10000, group_id=888)
    frozen = _make_task(seq=9)
    frozen["claim_at"] = _time.monotonic() + 120
    owstats._frozen_tasks.append(frozen)
    msg = _run(owstats.ow_status, ev_owner)
    assert "冻结" in str(msg) and "#9" in str(msg)


# ---------------- 审查修复回归：T6-T12 ----------------

def test_late_text_after_timeout_does_not_fill_next_first_segs():
    """超时后 45s 隔离窗：旧任务迟到文本不得成为下一任务 first_segs。"""
    bot = _relay_bot()
    # 模拟 sweep 超时收尾
    owstats._task_current = _make_task(seq=1)
    owstats._task_current = None
    owstats._note_missing_image()
    owstats._open_discard_window()
    # 下一任务已在途
    nxt = _make_task(seq=2)
    owstats._task_current = nxt
    asyncio.run(_relay(bot, _relay_event(Message([MessageSegment.text("迟到的旧战报正文")]))))
    assert not nxt.get("first_segs")
    assert owstats._task_current is nxt
    assert bot.sent_group == []


def test_late_image_during_discard_window_dropped_without_claim():
    bot = _relay_bot()
    owstats._open_discard_window()
    nxt = _make_task(seq=2)
    owstats._task_current = nxt
    asyncio.run(_relay(bot, _relay_event(Message([MessageSegment.image("http://x/late.png")]))))
    assert owstats._task_current is nxt
    assert not nxt.get("first_segs")
    assert bot.sent_group == []


def test_discard_window_allows_expected_claim_image():
    bot = _relay_bot()
    owstats._open_discard_window()
    frozen = _make_task(seq=1, user_id="111", group_id=10001)
    frozen["t0"] = time.monotonic() - 400
    owstats._claiming_task = frozen
    asyncio.run(_relay(bot, _relay_event(Message([MessageSegment.image("http://x/claim.png")]))))
    delivered = [s for s in bot.sent_group if s["group_id"] == 10001]
    assert delivered and "claim.png" in str(delivered[0]["message"])
    assert owstats._claiming_task is None


def test_cooldown_rejects_second_query_within_10s():
    ev = MessageEvent(user_id=777, group_id=888)
    arg = _msg("Yanmou#51293")
    first = _run(owstats.match_report, ev, arg)
    assert first is None or "频繁" not in str(first)
    second = _run(owstats.match_report, ev, arg)
    assert "频繁" in str(second)
    # 只入队第一次
    assert len(owstats._task_queue) == 1


def test_ow_reset_resolves_unfinished_futures():
    async def _go():
        loop = asyncio.get_running_loop()
        fut_cur = loop.create_future()
        fut_claim = loop.create_future()
        fut_frozen = loop.create_future()
        owstats._task_current = _make_task(seq=1, future=fut_cur, group_id=None, user_id="")
        owstats._claiming_task = _make_task(seq=2, future=fut_claim, group_id=None, user_id="")
        owstats._frozen_tasks.append(_make_task(seq=3, future=fut_frozen, group_id=None, user_id=""))
        ev = MessageEvent(user_id=10000, group_id=888)
        try:
            await owstats.ow_reset(ev)
        except FinishedException:
            pass
        for fut in (fut_cur, fut_claim, fut_frozen):
            assert fut.done()
            assert fut.result() == (None, False)
        assert owstats._task_current is None
        assert owstats._claiming_task is None
        assert not owstats._frozen_tasks
        assert owstats._in_discard_window()
    asyncio.run(_go())


def test_dispatch_double_call_only_one_send():
    async def _go():
        bot = _relay_bot()
        sent = []

        async def send_group_msg(group_id=None, message=None, **kwargs):
            sent.append({"group_id": group_id, "message": message})
            await asyncio.sleep(0.05)  # 让出事件循环，模拟并发第二个 dispatch
            return {"message_id": 1}

        bot.send_group_msg = send_group_msg
        owstats._task_queue.append(_make_task(seq=1, kind="matchrep", tag="A#1"))
        owstats._task_queue.append(_make_task(seq=2, kind="strength", tag="B#2"))
        orig = owstats.get_bot
        owstats.get_bot = lambda: bot
        try:
            await asyncio.gather(owstats._dispatch_next(), owstats._dispatch_next())
        finally:
            owstats.get_bot = orig
        assert len(sent) == 1
        assert owstats._task_current is not None
        assert owstats._task_current.get("seq") == 1
        assert len(owstats._task_queue) == 1
        assert owstats._task_queue[0].get("seq") == 2
    asyncio.run(_go())


def test_dispatch_send_failure_clears_current_and_requeues():
    async def _go():
        bot = _relay_bot()

        async def send_group_msg(group_id=None, message=None, **kwargs):
            if group_id == owstats.RELAY_GROUP_ID:
                raise RuntimeError("relay down")
            bot.sent_group.append({"group_id": group_id, "message": message})
            return {"message_id": 1}

        bot.send_group_msg = send_group_msg
        owstats._task_queue.append(_make_task(seq=1, group_id=10001, user_id="111"))
        orig = owstats.get_bot
        owstats.get_bot = lambda: bot
        try:
            await owstats._dispatch_next()
        finally:
            owstats.get_bot = orig
        assert owstats._task_current is None
        assert owstats._task_queue and owstats._task_queue[0].get("seq") == 1
        assert any("派发失败" in str(s["message"]) for s in bot.sent_group)
    asyncio.run(_go())


def test_relay_sets_matcher_block_for_relay_bot():
    bot = _relay_bot()
    m = _FakeMatcher()
    owstats._task_current = _make_task()
    asyncio.run(owstats._relay_result(bot, _relay_event(Message([MessageSegment.text("正文")])), m))
    assert m.block is True


def test_claim_send_opens_discard_window():
    bot = _relay_bot()
    frozen = _make_task(seq=7)
    frozen["claim_at"] = 0.0
    owstats._frozen_tasks.append(frozen)

    async def _go():
        assert await owstats._send_claim_at(bot) is True
    asyncio.run(_go())
    assert owstats._in_discard_window()
    assert owstats._claiming_task is frozen


def test_flush_tasks_hold_strong_ref():
    async def _go():
        bot = _relay_bot()
        owstats._task_current = _make_task(seq=50)
        await _relay(bot, _relay_event(Message([MessageSegment.text("只有一条文本")])))
        assert owstats._task_current.get("first_segs")
        assert len(owstats._flush_tasks) == 1
        # 清理：取消未完成的 sleep 任务
        for t in list(owstats._flush_tasks):
            t.cancel()
        await asyncio.gather(*owstats._flush_tasks, return_exceptions=True)
        assert len(owstats._flush_tasks) == 0
    asyncio.run(_go())
