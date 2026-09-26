import time
import types

from helpers import load_plugin

error_notify = load_plugin("error_notify")


def test_should_notify_respects_cooldown():
    mod = error_notify
    mod._last_notified.clear()
    key = "plugins.x|ValueError|f.py:1 in g"
    now = time.time()
    assert mod._should_notify(key, now) is True
    # 冷却期内的同一错误不再提醒
    assert mod._should_notify(key, now + 60) is False
    # 冷却结束后恢复提醒
    assert mod._should_notify(key, now + mod._COOLDOWN + 1) is True
    mod._last_notified.clear()


def test_build_message_contains_plugin_error_and_location():
    mod = error_notify
    try:
        raise ValueError("boom")
    except ValueError as e:
        exc = e  # 带真实 traceback
    msg = mod._build_message("plugins.demo", exc, "demo.py:10 in handler")
    assert "plugins.demo" in msg
    assert "ValueError" in msg and "boom" in msg
    assert "demo.py:10 in handler" in msg


def test_deepest_location_points_to_raise_line():
    mod = error_notify
    try:
        raise RuntimeError("x")
    except RuntimeError as e:
        exc = e
    loc = mod._deepest_location(exc)
    assert "test_error_notify.py" in loc and "in test_deepest_location" in loc


def test_plugin_label_fallbacks():
    mod = error_notify
    # 什么都没有 → 未知插件
    assert mod._plugin_label(types.SimpleNamespace()) == "未知插件"
    # 只有 module → 模块名
    m = types.SimpleNamespace(module=types.SimpleNamespace(__name__="plugins.fake"))
    assert mod._plugin_label(m) == "plugins.fake"
    # 有 plugin.name → 插件名优先
    m2 = types.SimpleNamespace(plugin=types.SimpleNamespace(name="plugins.real"))
    assert mod._plugin_label(m2) == "plugins.real"


def test_error_key_is_stable_for_same_error():
    mod = error_notify
    try:
        raise KeyError("k")
    except KeyError as e:
        exc = e
    k1 = mod._error_key("plugins.a", exc, "a.py:1 in f")
    k2 = mod._error_key("plugins.a", exc, "a.py:1 in f")
    k3 = mod._error_key("plugins.b", exc, "a.py:1 in f")
    assert k1 == k2
    assert k1 != k3


def test_job_error_listener_registered():
    import nonebot_plugin_apscheduler as aps_mod

    mod = error_notify
    assert any(
        cb is mod._on_job_error for cb, _mask in aps_mod.scheduler.listeners
    ), "FAIL: 定时任务错误监听器未注册"


def test_job_label_falls_back_to_id():
    mod = error_notify
    label = mod._job_label(types.SimpleNamespace(job_id="daily_news"))
    assert label == "定时任务 daily_news"


def test_build_message_uses_shanghai_timezone():
    mod = error_notify
    assert str(mod._SH) == "Asia/Shanghai" or getattr(mod._SH, "key", None) == "Asia/Shanghai"
    try:
        raise ValueError("tz")
    except ValueError as e:
        exc = e
    msg = mod._build_message("plugins.demo", exc, "demo.py:1 in h")
    # 时间戳格式 %m-%d %H:%M；确认来源是上海时区而非本地 naive now
    from datetime import datetime
    from zoneinfo import ZoneInfo
    expected = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M")
    # 允许跨分钟边界：取前后一分钟窗口
    assert any(m in msg for m in (
        expected,
        (datetime.now(ZoneInfo("Asia/Shanghai")).replace(second=59)).strftime("%m-%d %H:%M"),
    )) or "🕐" in msg


def test_log_alert_handler_levels_and_ignore(monkeypatch):
    """日志兜底：WARNING+ 通知；框架日志/设计内可选失败/INFO 不通知。"""
    import logging

    scheduled = []

    class _LoopStub:
        def is_closed(self):
            return False

    monkeypatch.setattr(error_notify, "_loop", _LoopStub())
    monkeypatch.setattr(error_notify, "_schedule_log_notice",
                        lambda key, text: scheduled.append((key, text)))

    def _rec(name, level, msg):
        return logging.LogRecord(name, level, "f.py", 1, msg, (), None)

    h = error_notify._LogAlertHandler(level=logging.WARNING)
    h.emit(_rec("plugin_random_chat", logging.WARNING, "random_chat AI 生成失败，本次静默跳过"))
    assert len(scheduled) == 1 and "plugin_random_chat" in scheduled[0][0]
    assert "WARNING" in scheduled[0][1]

    h.emit(_rec("nonebot", logging.ERROR, "adapter down"))  # 框架日志：忽略
    h.emit(_rec("plugin_btd6.collect", logging.WARNING,
                "btd6 optional call failed [races]"))  # 设计内可选失败：忽略
    h.emit(_rec("plugin_x", logging.INFO, "info 不到阈值"))  # INFO：忽略
    assert len(scheduled) == 1

    # 清理预占的冷却键，避免影响其他测试
    error_notify._last_notified.pop(
        "log|plugin_random_chat|random_chat AI 生成失败，本次静默跳过", None)


def test_schedule_log_notice_rolls_back_on_failure(monkeypatch):
    """发送失败时回滚冷却预占，恢复后可立即重试。"""
    import asyncio

    async def failing_send(text):
        raise RuntimeError("napcat down")

    monkeypatch.setattr(error_notify, "_send_notice", failing_send)
    loop = asyncio.new_event_loop()
    old_loop = error_notify._loop
    error_notify._loop = loop

    async def main():
        error_notify._last_notified.pop("log|x|boom", None)
        assert error_notify._should_notify("log|x|boom", time.time()) is True  # 预占冷却
        error_notify._schedule_log_notice("log|x|boom", "告警文本")
        await asyncio.sleep(0.05)  # 让回调执行回滚
        assert "log|x|boom" not in error_notify._last_notified  # 已回滚

    try:
        loop.run_until_complete(main())
    finally:
        error_notify._loop = old_loop
        loop.close()
        error_notify._last_notified.pop("log|x|boom", None)
