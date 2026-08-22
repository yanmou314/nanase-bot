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
