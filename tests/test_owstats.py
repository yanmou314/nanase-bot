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
