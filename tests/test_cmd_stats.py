import asyncio
import re
from types import SimpleNamespace

from conftest import GroupMessageEvent
from helpers import load_plugin

cmd_stats = load_plugin("cmd_stats")


class CommandRule:
    pass


def _matcher(is_command=True):
    call = CommandRule() if is_command else object()
    checker = SimpleNamespace(call=call)
    return SimpleNamespace(rule=SimpleNamespace(checkers=[checker]))


def test_only_successfully_run_command_is_recorded(monkeypatch):
    calls = []

    async def fake_write_command(group_id, user_id, command):
        calls.append((group_id, user_id, command))

    monkeypatch.setattr(cmd_stats, "db_write_command", fake_write_command)
    event = GroupMessageEvent(group_id=123, user_id=456)
    state = {"_prefix": {"raw_command": ".帮助"}}

    asyncio.run(cmd_stats._record_successful_command(_matcher(), event, state))
    # 同一事件命中多个响应器时只计一次
    asyncio.run(cmd_stats._record_successful_command(_matcher(), event, state))
    # 非命令响应器和运行异常都不计入
    asyncio.run(cmd_stats._record_successful_command(_matcher(False), event, {"_prefix": {"raw_command": ".未知"}}))
    asyncio.run(
        cmd_stats._record_successful_command(
            _matcher(), event, {"_prefix": {"raw_command": ".失败"}}, exception=RuntimeError()
        )
    )

    assert calls == [(123, 456, ".帮助")]


def test_collect_uses_success_command_table(monkeypatch):
    queries = []

    async def fake_exec(sql, params=()):
        queries.append((sql, params))
        if "COUNT(*), COUNT(DISTINCT" in sql:
            return [(3, 2, 2)]
        if "SELECT command" in sql:
            return [(".帮助", 2, 1), (".rp", 1, 2)]
        return [(456, 2, 123, 1), (789, 1, 456, 3)]

    monkeypatch.setattr(cmd_stats, "exec", fake_exec)
    data = asyncio.run(cmd_stats._collect("2026-08-19"))

    assert data["total"] == 3
    assert data["cmds"] == [(".帮助", 2), (".rp", 1)]
    assert all("command_usages" in sql for sql, _ in queries)
    assert all("text LIKE" not in sql for sql, _ in queries)


def test_render_allocates_space_for_all_top_rows(monkeypatch):
    captured = {}

    def fake_render(html, prefix, cache_dir, max_age):
        captured["html"] = html
        return "stats.png"

    monkeypatch.setattr(cmd_stats, "gradient_background", lambda w, h: "data:image/png;base64,xx")
    monkeypatch.setattr(cmd_stats, "render_html_to_png", fake_render)
    data = {
        "total": 3,
        "groups": 1,
        "users": 2,
        "cmds": [(".帮助", 2), (".rp", 1)],
        "users_named": [("Alice", 2)],
    }

    assert cmd_stats._render("2026年8月19日", data) == "stats.png"
    html = captured["html"]
    assert html.count('class="row"') == 3
    assert re.search(r"@page \{ size: 900px 650px;", html)
