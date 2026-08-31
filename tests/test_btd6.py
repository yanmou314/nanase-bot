import asyncio
import logging
import os
import time

import pytest
from conftest import FinishedException, GroupMessageEvent, MessageSegment

from helpers import load_plugin
import sys

# 移除缓存的旧模块，确保测试反映最新的插件代码
sys.modules.pop("plugin_btd6", None)
btd6 = load_plugin("btd6")

NOW = 1_787_000_000_000  # 固定"当前时间"（毫秒）
DAY = 86_400_000


def _clear_all_caches():
    btd6._cache.clear()
    btd6._stale.clear()
    btd6._stale_at.clear()
    btd6._stale_served.clear()
    btd6._cache_sizes.clear()
    btd6._lb_next_cache.clear()
    btd6._refresh_fail_counts.clear()
    btd6._refreshing.clear()
    btd6._refresh_tasks.clear()
    btd6._asset_mem.clear()
    btd6._game_mem.clear()
    btd6._ui_mem.clear()
    btd6._odyssey_thumb_mem.clear()
    btd6._cooldowns.clear()


@pytest.fixture(autouse=True)
def _clear_cache():
    _clear_all_caches()
    yield
    _clear_all_caches()


@pytest.fixture(autouse=True)
def _no_render(monkeypatch):
    """默认禁用图片渲染，处理器统一走文本兜底，测试不依赖 weasyprint 是否安装。"""

    async def broken(prefix, html_fn):
        raise RuntimeError("renderer disabled in tests")

    monkeypatch.setattr(btd6.cards, "_render_card", broken)


def _ev(text: str) -> GroupMessageEvent:
    return GroupMessageEvent(plain=text, user_id=1, group_id=100, message=[])


RACE_ACTIVE = {
    "id": "r1", "name": "Test Race ", "start": NOW - DAY,
    "end": NOW + 2 * DAY + 3_600_000, "totalScores": 36745,
    "leaderboard": "https://lb.test/race", "metadata": "https://meta.test/race",
}
BOSS_UPCOMING = {
    "id": "b1", "name": "Phayze30", "bossType": "phayze",
    "start": NOW + 3 * DAY, "end": NOW + 10 * DAY,
    "totalScores_standard": 9278, "totalScores_elite": 3884,
    "normalScoringType": "GameTime", "eliteScoringType": "LeastTiers",
    "leaderboard_standard_players_1": "https://lb.test/boss/std",
    "leaderboard_elite_players_1": "https://lb.test/boss/elite",
    "metadataStandard": "https://meta.test/boss/std", "metadataElite": "https://meta.test/boss/elite",
}
CT_ACTIVE = {
    "id": "c1", "start": NOW - 3_600_000, "end": NOW + 5 * DAY,
    "totalScores_player": 13671, "totalScores_team": 4484,
    "leaderboard_player": "https://lb.test/ct/player", "leaderboard_team": "https://lb.test/ct/team",
}


# ---------------- 时间与状态 ----------------

def test_fmt_remaining():
    assert btd6.fmt_remaining(2 * DAY + 3 * 3_600_000) == "2天3小时"
    assert btd6.fmt_remaining(5 * 3_600_000 + 30 * 60_000) == "5小时30分"
    assert btd6.fmt_remaining(90 * 60_000) == "1小时30分"
    assert btd6.fmt_remaining(-1000) == "0分钟"


def test_event_status_line_states():
    active = {"start": NOW - 60_000, "end": NOW + 90_000}
    assert "剩余" in btd6.event_status_line(active, NOW) and "结束" in btd6.event_status_line(active, NOW)
    future = {"start": NOW + 3_600_000, "end": NOW + 2 * 3_600_000}
    assert "后开始" in btd6.event_status_line(future, NOW)
    past = {"start": NOW - 7200_000, "end": NOW - 3600_000}
    assert "已于" in btd6.event_status_line(past, NOW)


def test_pick_active_next_fallback():
    items = [RACE_ACTIVE]
    assert btd6.pick_active(items, NOW) is RACE_ACTIVE
    assert btd6.pick_active(items, NOW + 99 * DAY) is None
    upcoming = [dict(RACE_ACTIVE, start=NOW + 5 * DAY, end=NOW + 6 * DAY)]
    assert btd6.pick_next(upcoming, NOW) is upcoming[0]
    assert btd6.fallback_latest(upcoming) is upcoming[0]
    assert btd6.fallback_latest([]) is None
    assert btd6.pick_next([], NOW) is None


# ---------------- 分数格式化 ----------------

def test_fmt_score_game_time():
    assert btd6.fmt_score("GameTime", 118300) == "1:58.300"


def test_fmt_score_least_cash_and_tiers():
    assert btd6.fmt_score("LeastCash", 12345) == "$12,345"
    assert btd6.fmt_score("LeastTiers", 7.0) == "7"


def test_fmt_score_raw_and_invalid():
    assert btd6.fmt_score(None, 12345) == "12,345"
    assert btd6.fmt_score("", 0) == "0"
    assert btd6.fmt_score("GameTime", "abc") == "abc"


# ---------------- 活动总览 ----------------

def test_build_overview_sections():
    text = btd6.build_overview([RACE_ACTIVE], [BOSS_UPCOMING], [CT_ACTIVE], NOW)
    assert "🎮 BTD6 当前活动" in text
    assert "「Test Race」" in text and "36,745" in text
    assert "「Phayze30」" in text and "（幻影）" in text
    assert "后开始" in text  # Boss 未开始 → 显示预告倒计时
    assert "标准模式 最快用时 · 精英模式 最少升级" in text
    assert "个人 13,671 · 战队 4,484" in text
    # 外部内容不应出现 CQ 码字符
    assert "[" not in text.replace("「", "").replace("」", "")


def test_race_overview_empty():
    # 空数据场景由 build_overview 输出"暂无"；单场文本统一经 _single_event_text
    lines = btd6._single_event_text(RACE_ACTIVE, "race", NOW)
    assert "「Test Race」" in lines[0] and "36,745" in lines[2]
    ended = dict(RACE_ACTIVE, start=NOW - 9 * DAY, end=NOW - 8 * DAY)
    assert "已于" in "\n".join(btd6._single_event_text(ended, "race", NOW))


def test_single_event_text_rush_branch():
    """Boss Rush 不再落入 CT 分支：输出名称 + 起止时间线。"""
    rush = {"id": "rush1", "name": "A Boss Rush Event", "type": "bossRush",
            "start": NOW - DAY, "end": NOW + DAY}
    lines = btd6._single_event_text(rush, "rush", NOW)
    joined = "\n".join(lines)
    assert "Boss 竞速冲刺" in joined
    assert "剩余" in joined and "结束" in joined
    assert "争夺领土" not in joined
    # 未知名称回退为 Boss Rush 前缀 + 原名
    custom = dict(rush, name="Custom Rush Name")
    assert "Boss Rush「Custom Rush Name」" in "\n".join(btd6._single_event_text(custom, "rush", NOW))
    # 总览分类含 rush 时不再显示成 CT
    text = btd6.build_overview([RACE_ACTIVE], [], [], NOW, [], [rush])
    assert "争夺领土（CT）" not in text
    assert "Boss 竞速冲刺" in text


def test_classify_overview_ended_not_truncated():
    """分类层不再截断已结束列表（截断职责移至展示层 ENDED_SHOW）。"""
    races = [dict(RACE_ACTIVE, id=f"r{i}", start=NOW - (i + 2) * DAY, end=NOW - (i + 1) * DAY)
             for i in range(12)]
    _, _, ended = btd6._classify_overview_events(races, [], [], NOW)
    assert len(ended) == 12
    text = btd6.build_overview(races, [], [], NOW)
    assert f"最近 {btd6.ENDED_SHOW} 场" in text


# ---------------- 规则渲染 ----------------

META = {
    "name": "Test Race", "map": "ThreeMinesAround",
    "difficulty": "Medium", "mode": "Reverse",
    "startingCash": 650, "lives": 200, "startRound": 1, "endRound": 80,
    "maxTowers": 9999, "maxParagons": 0,
    "disableMK": True, "disablePowers": False,
    "_bloonModifiers": {
        "speedMultiplier": 1.5, "moabSpeedMultiplier": 1,
        "healthMultipliers": {"bloons": 1, "moabs": 2, "boss": 1},
        "allCamo": False, "allRegen": False,
    },
    "_towers": [
        {"tower": "ChosenPrimaryHero", "max": 0},
        {"tower": "Alchemist", "max": 1},
        {"tower": "Dart Monkey", "max": 0},
        {"tower": "Monkey Village", "max": -1, "path1NumBlockedTiers": 3},
        {"tower": "Etienne", "max": 99, "isHero": True},
        {"tower": "Quincy", "max": 0, "isHero": True},
    ],
}


def test_format_rules_full():
    text = btd6.format_rules(META, "🏁 竞赛")
    assert "竞赛「Test Race」规则" in text
    assert "地图：ThreeMinesAround｜难度：中等｜模式：反向" in text
    assert "初始资金 650｜❤️ 生命 200｜回合 1–80" in text
    assert "塔位上限 无限制｜禁止 Paragon" in text
    assert "禁用：猴子知识" in text
    assert "气球速度 ×1.5" in text and "MOAB血量 ×2" in text
    assert "塔禁用" not in text  # 整塔禁用的猴子直接不显示
    assert "飞镖猴" not in text  # 被禁塔名不得出现在任何行
    assert "塔限购：炼金术士×1" in text
    assert "路径限制：猴村（路1禁3层）" in text
    assert "英雄限定：艾蒂安" in text
    assert "ChosenPrimaryHero" not in text  # 内部占位符不外显
    assert "昆西" not in text  # 被禁英雄不进限定名单


def test_daily_monkey_grid_hides_banned_towers():
    """每日挑战卡：max=0（整塔禁用）的猴子直接不显示，可用塔正常带角标。"""
    meta = {"_towers": [
        {"tower": "DartMonkey", "max": 0},   # 禁用 → 不显示
        {"tower": "Alchemist", "max": 1},    # 限购 → 正常显示
    ]}
    html = btd6._daily_monkey_grid(meta)
    assert "飞镖猴" not in html
    assert "禁用" not in html
    assert "炼金术士" in html


def test_bloon_mod_lines_default_silent():
    assert btd6.bloon_mod_lines({"speedMultiplier": 1, "healthMultipliers": {}}) == []
    mods = {"speedMultiplier": 2, "allCamo": True, "allRegen": True}
    joined = "；".join(btd6.bloon_mod_lines(mods))
    assert "气球速度 ×2" in joined and "全体隐身" in joined and "全体再生" in joined


def test_tower_cn_handles_api_camel_case():
    assert btd6.tower_cn("BananaFarm") == "香蕉农场"
    assert btd6.tower_cn("Banana Farm") == "香蕉农场"
    assert btd6.tower_cn("Ezili") == "伊兹莉"
    assert btd6.tower_cn("UnknownTower") == "UnknownTower"


def test_tower_limit_lines_caps_long_lists():
    towers = [{"tower": f"T{i}", "max": 1} for i in range(10)]
    lines = btd6.tower_limit_lines(towers)
    assert len(lines) == 1 and "…等10项" in lines[0]
    # 整塔禁用（max=0）的猴子直接不显示
    banned_only = [{"tower": f"T{i}", "max": 0} for i in range(10)]
    assert btd6.tower_limit_lines(banned_only) == []


# ---------------- 参数解析 ----------------

def test_parse_kind():
    assert btd6.parse_kind(["竞赛", "15"]) == "race"
    assert btd6.parse_kind(["RACE"]) == "race"
    assert btd6.parse_kind(["boss"]) == "boss"
    assert btd6.parse_kind(["领土", "战队"]) == "ct"
    assert btd6.parse_kind(["abc"]) is None
    assert btd6.parse_kind([]) is None


def test_parse_variant():
    assert btd6.parse_variant(["精英"], default="standard") == "elite"
    assert btd6.parse_variant(["Elite"], default="standard") == "elite"
    assert btd6.parse_variant(["战队"], default="player") == "team"
    assert btd6.parse_variant(["zzz"], default="standard") == "standard"


def test_parse_rows():
    assert btd6.parse_rows(["15"]) == 15
    assert btd6.parse_rows(["999"]) == btd6.MAX_ROWS
    assert btd6.parse_rows(["0"]) == 1
    assert btd6.parse_rows(["abc"]) == btd6.DEFAULT_ROWS


# ---------------- fetch 缓存 ----------------

def test_fetch_body_uses_cache(monkeypatch):
    def _boom(t):
        raise AssertionError("缓存命中时不应发起请求")

    monkeypatch.setattr(btd6.nkapi, "get_http_client", _boom)
    url = "https://data.ninjakiwi.com/btd6/races"
    btd6._cache_put(url, {"ok": 1})
    assert asyncio.run(btd6.fetch_body(url)) == {"ok": 1}


def test_fetch_body_serves_stale_and_marks(monkeypatch):
    """缓存过期时返回 stale 旧数据，并标记供 _stale_warn 提示；刷新成功后标记清除。"""
    url = btd6.URL_RACES
    btd6._stale[url] = {"old": 1}
    btd6._stale_at[url] = time.monotonic() - 100

    async def fake_refresh(u):
        btd6._cache_put(u, {"new": 2})

    monkeypatch.setattr(btd6.nkapi, "_refresh_url", fake_refresh)

    async def main():
        body = await btd6.fetch_body(url)
        await asyncio.sleep(0.01)  # 让后台刷新任务执行
        return body

    assert asyncio.run(main()) == {"old": 1}
    assert 0 <= btd6._stale_age(url) < 60
    # 后台刷新成功写入新数据 → 过期标记清除、缓存更新
    assert btd6._cache_get(url) == {"new": 2}
    assert url not in btd6._stale_served


def test_stale_age_and_warn():
    url = btd6.URL_RACES
    assert btd6._stale_age(url) is None  # 从未写入
    btd6._cache_put(url, {"x": 1})
    assert 0 <= btd6._stale_age(url) < 60
    assert btd6._stale_warn(url) == ""
    btd6._stale_at[url] = time.monotonic() - 25 * 3600  # 模拟 24h+ 未刷新
    assert btd6._stale_warn(url) == ""  # 未实际以 stale 响应时不提示
    btd6._stale_served.add(url)
    assert "24+" in btd6._stale_warn(url)
    assert btd6._stale_warn(btd6.URL_BOSSES) == ""  # 其他 URL 不受影响


def test_json_cache_byte_budget(monkeypatch):
    """JSON body 缓存有总字节上限，超限从最旧淘汰。"""
    monkeypatch.setattr(btd6.nkapi, "MAX_JSON_MEM_BYTES", 1000)
    for i in range(10):
        btd6._cache_put(f"https://data.ninjakiwi.com/x{i}", {"pad": "a" * 400})
    total = sum(btd6._cache_sizes.values())
    assert total <= 1000
    assert len(btd6._cache) < 10


def test_safe_logs_warning(caplog):
    async def boom():
        raise RuntimeError("boom")

    async def main():
        return await btd6._safe(boom(), "unit")

    with caplog.at_level(logging.WARNING):
        assert asyncio.run(main()) is None
    assert any("btd6 optional call failed" in r.message and "[unit]" in r.message
               for r in caplog.records)


def test_refresh_url_failure_counted(monkeypatch, caplog):
    async def broken(url, timeout):
        raise RuntimeError("down")

    monkeypatch.setattr(btd6.nkapi, "_http_get", broken)
    url = btd6.URL_RACES
    before = btd6._refresh_fail_counts.get(url, 0)
    with caplog.at_level(logging.WARNING):
        asyncio.run(btd6._refresh_url(url))
    assert btd6._refresh_fail_counts[url] == before + 1
    assert any("后台刷新失败" in r.message for r in caplog.records)


def test_rushgen_constants_validation(caplog):
    """rushdata.json 结构校验：关键字段缺失时 warning。"""
    rg = btd6.rushgen
    with caplog.at_level(logging.WARNING):
        rg._validate_constants({})
    assert any("缺少关键字段" in r.message for r in caplog.records)
    with caplog.at_level(logging.WARNING):
        rg._validate_constants({"bossRush": {"StageScores": []}})
    assert any("RandomSettings" in r.message for r in caplog.records)
    # 真实数据文件无告警
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        rg._validate_constants(rg.load_constants())
    assert not caplog.records


# ---------------- 处理器 ----------------

def _fake_fetch_factory(bodies_by_url, calls=None):
    async def fake_fetch(url):
        if calls is not None:
            calls.append(url)
        return bodies_by_url[url]

    return fake_fetch


def test_handler_help(monkeypatch):
    with pytest.raises(FinishedException):
        asyncio.run(btd6.help_cmd.handlers[0](_ev(".btd6")))
    assert str(btd6.help_cmd.finished[-1]) == btd6.HELP_TEXT  # 渲染被禁用 → 文本兜底


def test_handler_events_overview(monkeypatch):
    calls = []
    bodies = {btd6.URL_RACES: [RACE_ACTIVE], btd6.URL_BOSSES: [BOSS_UPCOMING], btd6.URL_CT: [CT_ACTIVE]}
    monkeypatch.setattr(btd6.nkapi, "fetch_body", _fake_fetch_factory(bodies, calls))
    with pytest.raises(FinishedException):
        asyncio.run(btd6.events_cmd.handlers[0](_ev(".btd6活动")))
    seg = btd6.events_cmd.finished[-1]
    assert isinstance(seg, MessageSegment) and seg.type == "text"
    text = str(seg)
    assert "Test Race" in text and "Phayze30" in text and "争夺领土" in text
    assert {btd6.URL_RACES, btd6.URL_BOSSES, btd6.URL_CT} <= set(calls)


def test_handler_events_failure(monkeypatch):
    async def broken(url):
        raise RuntimeError("down")

    monkeypatch.setattr(btd6.nkapi, "fetch_body", broken)
    with pytest.raises(FinishedException):
        asyncio.run(btd6.events_cmd.handlers[0](_ev(".btd6活动")))
    assert "获取 BTD6 活动信息失败" in str(btd6.events_cmd.finished[-1])


def test_handler_leaderboard_race(monkeypatch):
    entries = [
        {"displayName": "ISAB", "score": 118300},
        {"displayName": "<b>注入</b>", "score": 119017},
        {"displayName": "third", "score": 120000},
    ]
    bodies = {
        btd6.URL_RACES: [RACE_ACTIVE],
        RACE_ACTIVE["leaderboard"]: entries,
    }
    monkeypatch.setattr(btd6.nkapi, "fetch_body", _fake_fetch_factory(bodies))
    with pytest.raises(FinishedException):
        asyncio.run(btd6.lb_cmd.handlers[0](_ev(".btd6排行 竞赛")))
    text = str(btd6.lb_cmd.finished[-1])
    assert "🥇 ISAB — 1:58.300" in text
    assert "🥈 <b>注入</b> — 1:59.017" in text
    assert "third" in text  # 默认前50，3条全部展示
    # 毫秒级分差在格式化后仍可分辨（118300 vs 119017）


def test_handler_leaderboard_race_page(monkeypatch):
    entries_p1 = [{"displayName": f"p1_{i}", "score": 100000 + i} for i in range(50)]
    entries_p2 = [{"displayName": f"p2_{i}", "score": 200000 + i} for i in range(5)]
    bodies = {
        btd6.URL_RACES: [RACE_ACTIVE],
        RACE_ACTIVE["leaderboard"]: entries_p1,
        RACE_ACTIVE["leaderboard"] + "?page=2": entries_p2,
    }
    monkeypatch.setattr(btd6.nkapi, "fetch_body", _fake_fetch_factory(bodies))
    with pytest.raises(FinishedException):
        asyncio.run(btd6.lb_cmd.handlers[0](_ev(".btd6排行 竞赛 P2")))
    text = str(btd6.lb_cmd.finished[-1])
    assert "第2页" in text
    assert "p2_0" in text
    assert "p1_0" not in text


def test_handler_leaderboard_rank_returns_player(monkeypatch):
    pid2 = "b" * 40
    entries = [
        {"displayName": "first", "score": 118300, "profile": "https://data.ninjakiwi.com/btd6/users/" + "a" * 40},
        {"displayName": "second", "score": 119017, "profile": "https://data.ninjakiwi.com/btd6/users/" + pid2},
        {"displayName": "third", "score": 120000, "profile": "https://data.ninjakiwi.com/btd6/users/" + "c" * 40},
    ]
    player_body = {
        "displayName": "second", "rank": 99, "veteranRank": 10, "followers": 123,
        "mostExperiencedMonkey": "DartMonkey", "highestRound": 100, "achievements": 10,
        "bloonsPopped": {"bloonsPopped": 1000}, "gameplay": {"highestRoundCHIMPS": 10},
    }
    bodies = {
        btd6.URL_RACES: [RACE_ACTIVE],
        RACE_ACTIVE["leaderboard"]: entries,
        btd6.URL_USERS + pid2: player_body,
    }
    monkeypatch.setattr(btd6.nkapi, "fetch_body", _fake_fetch_factory(bodies))
    with pytest.raises(FinishedException):
        asyncio.run(btd6.lb_cmd.handlers[0](_ev(".btd6排行 竞赛 2")))
    text = str(btd6.lb_cmd.finished[-1])
    assert "第 2 名" in text
    assert "second" in text


def test_parse_lb_page_and_rank():
    assert btd6.parse_lb_page(["P2"]) == 2
    assert btd6.parse_lb_page(["p", "2"]) == 2
    assert btd6.parse_lb_page(["p2"]) == 2
    assert btd6.parse_lb_page(["竞赛", "P3"]) == 3
    assert btd6.parse_lb_page(["竞速"]) is None
    assert btd6.parse_lb_rank(["7"]) == 7
    assert btd6.parse_lb_rank(["P2"]) is None
    assert btd6.parse_lb_rank(["p", "2"]) is None
    assert btd6.parse_lb_rank(["竞速", "7"]) == 7


def test_handler_leaderboard_boss_elite(monkeypatch):
    calls = []
    bodies = {
        btd6.URL_BOSSES: [BOSS_UPCOMING],
        BOSS_UPCOMING["leaderboard_elite_players_1"]: [{"displayName": "top1", "score": 42}],
    }
    monkeypatch.setattr(btd6.nkapi, "fetch_body", _fake_fetch_factory(bodies, calls))
    with pytest.raises(FinishedException):
        asyncio.run(btd6.lb_cmd.handlers[0](_ev(".btd6排行 boss 精英")))
    assert BOSS_UPCOMING["leaderboard_elite_players_1"] in calls
    text = str(btd6.lb_cmd.finished[-1])
    assert "精英排行榜（最少升级）" in text and "🥇 top1 — 42" in text


def test_handler_leaderboard_usage_when_no_kind(monkeypatch):
    with pytest.raises(FinishedException):
        asyncio.run(btd6.lb_cmd.handlers[0](_ev(".btd6排行")))
    assert btd6.lb_cmd.finished[-1] == btd6.LB_USAGE


def test_handler_leaderboard_failure(monkeypatch):
    async def broken(url):
        raise RuntimeError("down")

    monkeypatch.setattr(btd6.nkapi, "fetch_body", broken)
    with pytest.raises(FinishedException):
        asyncio.run(btd6.lb_cmd.handlers[0](_ev(".btd6排行 竞赛")))
    assert "获取 BTD6 排行榜失败" in str(btd6.lb_cmd.finished[-1])


def test_handler_rules_race_and_boss(monkeypatch):
    boss_meta = dict(META, name="Phayze30")
    bodies = {
        btd6.URL_RACES: [RACE_ACTIVE],
        RACE_ACTIVE["metadata"]: META,
        btd6.URL_BOSSES: [BOSS_UPCOMING],
        BOSS_UPCOMING["metadataElite"]: boss_meta,
    }
    monkeypatch.setattr(btd6.nkapi, "fetch_body", _fake_fetch_factory(bodies))
    with pytest.raises(FinishedException):
        asyncio.run(btd6.rules_cmd.handlers[0](_ev(".btd6竞速")))
    assert "竞赛「Test Race」规则" in str(btd6.rules_cmd.finished[-1])
    btd6._cooldowns.clear()
    with pytest.raises(FinishedException):
        asyncio.run(btd6.rules_cmd.handlers[0](_ev(".btd6竞速 boss 精英")))
    assert "Boss·精英「Phayze30」规则" in str(btd6.rules_cmd.finished[-1])


def test_handler_rules_ct_does_not_query_boss(monkeypatch):
    calls = []

    async def fake_fetch(url):
        calls.append(url)
        if url == btd6.URL_CT:
            return [CT_ACTIVE]
        if url == btd6.URL_BOSSES:
            raise AssertionError("CT 规则不应查询 Boss 接口")
        raise AssertionError(f"不应请求 {url}")

    monkeypatch.setattr(btd6.nkapi, "fetch_body", fake_fetch)
    with pytest.raises(FinishedException):
        asyncio.run(btd6.rules_cmd.handlers[0](_ev(".btd6竞速 领土")))
    text = str(btd6.rules_cmd.finished[-1])
    assert "领土暂无通用规则数据" in text
    assert btd6.URL_BOSSES not in calls


def test_validate_url_allows_nk_and_rejects_other_hosts():
    assert btd6._validate_url("https://data.ninjakiwi.com/btd6/races")
    assert btd6._validate_url("https://static-api.nkstatic.com/image.webp")
    for url in ("http://data.ninjakiwi.com/x", "https://example.com/x",
                "https://data.ninjakiwi.com:443/x", "https://user@data.ninjakiwi.com/x"):
        with pytest.raises(ValueError):
            btd6._validate_url(url)


def test_tower_icon_rejects_path_like_names(monkeypatch, tmp_path):
    monkeypatch.setattr(btd6.assets, "GAME_ASSET_DIR", str(tmp_path))
    btd6._game_mem.clear()
    assert btd6._tower_icon("../secret", True) == ""
    assert btd6._tower_icon("<x>", False) == ""


def test_player_html_escapes_profile_fields():
    p = {"displayName": "ok", "rank": "<b>1</b>", "veteranRank": "<i>2</i>",
         "followers": "<script>", "mostExperiencedMonkey": "<x>"}
    html = btd6.player_html({"p": p})
    assert "&lt;b&gt;1&lt;/b&gt;" in html
    assert "&lt;i&gt;2&lt;/i&gt;" in html
    assert "&lt;script&gt;" in html
    assert "<script>" not in html


def test_cooldown_is_scoped_and_rejects_repeated_request():
    event = _ev(".btd6")
    assert btd6._cooldown_remaining(event, "help", "default") == 0
    assert btd6._cooldown_remaining(event, "help", "default") >= 1
    assert btd6._cooldown_remaining(event, "events", "default") == 0


def test_help_html_lists_all_commands():
    html = btd6.help_html()
    for cmd in (".btd6活动", ".btd6竞速", ".btd6排行", ".btd6每日", ".btd6远征",
                ".btd6玩家", ".btd6地图"):
        assert cmd in html
    assert "活动" in html and "排行与档案" in html
    assert "chip" in html and "hdesc" in html


def test_handler_maps(monkeypatch):
    items = [
        {"name": f"Map{i}", "createdAt": 1787000000000 + i} for i in range(5)
    ]
    bodies = {btd6.URL_MAP_FILTER.format("trending"): items}
    monkeypatch.setattr(btd6.nkapi, "fetch_body", _fake_fetch_factory(bodies))
    with pytest.raises(FinishedException):
        asyncio.run(btd6.maps_cmd.handlers[0](_ev(".btd6地图 热门 3")))
    text = str(btd6.maps_cmd.finished[-1])
    assert "自制地图 · 热门 Top3" in text
    assert "Map0" in text and "Map2" in text and "Map3" not in text


# ---------------- 图片卡片 ----------------

def test_handler_sends_image_when_render_ok(monkeypatch, tmp_path):
    card = tmp_path / "card.png"
    card.write_bytes(b"PNG")

    async def fake_render(prefix, html_fn):
        assert prefix
        return str(card)

    monkeypatch.setattr(btd6.cards, "_render_card", fake_render)
    bodies = {
        btd6.URL_RACES: [RACE_ACTIVE],
        btd6.URL_BOSSES: [BOSS_UPCOMING],
        btd6.URL_CT: [CT_ACTIVE],
    }
    monkeypatch.setattr(btd6.nkapi, "fetch_body", _fake_fetch_factory(bodies))
    with pytest.raises(FinishedException):
        asyncio.run(btd6.events_cmd.handlers[0](_ev(".btd6活动")))
    seg = btd6.events_cmd.finished[-1]
    assert isinstance(seg, MessageSegment) and seg.type == "image"
    # 处理器发送 Path(path).as_uri()：与本地 card 的 file URI 全等（跨平台，Windows 下不含反斜杠）
    assert seg.data["file"] == card.as_uri()


def test_overview_html_escapes_and_sections():
    evil_race = dict(RACE_ACTIVE, name="<img src=x onerror=1>")
    data = {"races": [evil_race], "bosses": [BOSS_UPCOMING], "cts": [CT_ACTIVE], "now": NOW}
    # Ensure the plugin is the latest version (helpers.load_plugin already reloads)
    import sys
    sys.modules.pop("plugin_btd6", None)
    import helpers
    helpers.load_plugin("btd6")
    html = btd6.overview_html(data)
    # Debug: check classification
    ongoing, upcoming, ended = btd6._classify_overview_events(data["races"], data["bosses"], data["cts"], data["now"])
    assert len(ongoing) > 0 or len(upcoming) > 0, f"No events classified: ongoing={len(ongoing)} upcoming={len(upcoming)} ended={len(ended)}"
    assert "&lt;img src=x" in html and "<img src=x" not in html
    # boss 名称：官方名 + 中文别名字段拼接渲染
    assert "Phayze30（幻影" in html
    # 状态以中文文案渲染：race 进行中 / boss 即将开始 / ct 已结束
    assert "进行中" in html and "即将开始" in html and "已结束" in html
    # 日期渲染为 YYYY-MM-DD
    assert "2026-" in html
    # 不含命令提示
    assert ".btd6规则" not in html and ".btd6排行" not in html


def test_leaderboard_html_rows_and_escape():
    col = {
        "head": "🏁 竞赛「<b>注入</b>」排行榜",
        "status": "⏱ 剩余 1小时",
        "entries": [(1, "ISAB", "1:58.300"), (2, "<script>", "$12")],
    }
    html = btd6.leaderboard_html(col)
    assert "ISAB" in html and "1:58.300" in html
    assert "&lt;script&gt;" in html and "<script>" not in html
    assert "1:58.300" in html


def test_leaderboard_html_empty_variant():
    col = {"empty": "⚠️ 该活动暂无此模式的排行榜"}
    html = btd6.leaderboard_html(col)
    assert "暂无此模式的排行榜" in html


def test_rules_html_grid_and_escape(monkeypatch, tmp_path):
    gdir = tmp_path / "game"
    gdir.mkdir()
    (gdir / "000-Alchemist.webp").write_bytes(b"w")
    (gdir / "000-MonkeyVillage.webp").write_bytes(b"w")
    (gdir / "QuincyPortrait.webp").write_bytes(b"w")
    (gdir / "CorvusPortrait.webp").write_bytes(b"w")
    monkeypatch.setattr(btd6.assets, "GAME_ASSET_DIR", str(gdir))
    btd6._game_mem.clear()
    meta = dict(META)
    meta["_towers"] = [
        {"tower": "DartMonkey", "max": 0},                       # 禁用 → 直接移除
        {"tower": "Alchemist", "max": 1},
        {"tower": "MonkeyVillage", "max": -1, "path1NumBlockedTiers": 3},
        {"tower": "Quincy", "max": 0, "isHero": True},            # 禁用英雄 → 移除
        {"tower": "Silas", "max": 99, "isHero": True},            # 皮肤英雄 → 基础立绘
        {"tower": "<x>", "max": 2},
    ]
    html = btd6.rules_html({
        "prefix": "🏁 竞赛", "meta": meta, "scoring_cn": "最快用时",
        "map_img": "", "side_img": "", "ev": dict(RACE_ACTIVE),
    })
    assert "猴子限制" in html and "mkgrid" in html
    assert "飞镖猴" not in html and "昆西" not in html  # 禁用的塔/英雄不出现
    assert "✕" not in html  # 不再使用打叉样式
    assert "×1" in html
    assert "2-5-5" in html  # 路1禁3层 → 可升到 2 层，其余 5
    assert "科沃斯·皮肤" in html
    assert "&lt;x&gt;" in html and "<x>" not in html  # 无立绘的塔退化为文字并转义
    assert "初始资金" in html and "最快用时" in html and "气球强化" in html
    assert "已结束" in html and "2026/8/" in html  # 活动时间与状态徽章（样例活动相对真实时钟已过期）


def test_path_max_txt():
    assert btd6._path_max_txt({}) == "5-5-5"
    assert btd6._path_max_txt({1: 2, 2: 3, 3: 2}) == "3-2-3"
    assert btd6._path_max_txt({2: 5}) == "5-0-5"


def test_tower_icon_mapping(monkeypatch, tmp_path):
    gdir = tmp_path / "game"
    gdir.mkdir()
    (gdir / "000-Wizard.webp").write_bytes(b"w")
    (gdir / "SaudaPortrait.webp").write_bytes(b"w")
    monkeypatch.setattr(btd6.assets, "GAME_ASSET_DIR", str(gdir))
    btd6._game_mem.clear()
    assert "data:image/webp" in btd6._tower_icon("WizardMonkey", False)  # 特例映射
    assert "data:image/webp" in btd6._tower_icon("Sauda", True)
    assert btd6._tower_icon("UnknownTower", False) == ""
    assert btd6._tower_icon("Nobody", True) == ""


# ---------------- 每日挑战 / 远征 / 玩家档案 ----------------

def test_fmt_cn_num():
    assert btd6.fmt_cn_num(1884842684) == "18.8亿"
    assert btd6.fmt_cn_num(9409863) == "941万"
    assert btd6.fmt_cn_num(5971) == "5,971"
    assert btd6.fmt_cn_num(0) == "0"
    assert btd6.fmt_cn_num(None) == "0"
    assert btd6.fmt_cn_num("abc") == "abc"


def test_extract_player_id():
    url = "https://data.ninjakiwi.com/btd6/users/9ce9468ad6c2ffad1c168e1f0e20e521cd0c1bedc813d03d"
    assert btd6._extract_player_id(url) == "9ce9468ad6c2ffad1c168e1f0e20e521cd0c1bedc813d03d"
    assert btd6._extract_player_id("9ce9468ad6c2ffad1c168e1f0e20e521cd0c1bedc813d03d") == \
        "9ce9468ad6c2ffad1c168e1f0e20e521cd0c1bedc813d03d"
    assert btd6._extract_player_id("") == ""
    assert btd6._extract_player_id("hello 12345") == ""


def test_daily_prefix():
    assert btd6._daily_prefix("Standard 2936: Shadow's Challenge", False) == "每日标准·第2936期"
    assert btd6._daily_prefix("Advanced 2923: X's Challenge", True) == "每日高级·第2923期"
    assert btd6._daily_prefix("Weird", False) == "每日标准"


def test_reward_txt():
    assert btd6._reward_txt(["MonkeyMoney:100", "Power:TechBot"]) == "猴币×100、力量·TechBot"
    assert btd6._reward_txt([]) == "无"


def test_odyssey_upgrade_caps():
    # 英雄固定满级，不显示升级上限
    assert btd6._odyssey_upgrade_caps({"tower": "Quincy", "max": 1, "isHero": True}) == ""
    assert btd6._odyssey_upgrade_caps(None) == ""
    # 由 path*NumBlockedTiers 计算三路开放层级：0/0/1 → 5-5-4
    assert btd6._odyssey_upgrade_caps({"path1NumBlockedTiers": 0, "path2NumBlockedTiers": 0,
                                       "path3NumBlockedTiers": 1}) == "5-5-4"
    # 全 0 → 5-5-5；异常值安全回退 5
    assert btd6._odyssey_upgrade_caps({"path1NumBlockedTiers": 2, "path2NumBlockedTiers": 3,
                                       "path3NumBlockedTiers": 4}) == "3-2-1"
    assert btd6._odyssey_upgrade_caps({"path1NumBlockedTiers": "x"}) == "5-5-5"


def test_odyssey_card_height():
    # 缺失 meta 回退 260（与 odyssey_diff_html 的空态高度一致）
    assert btd6._odyssey_card_height(None, 0) == 260
    meta = {"startingHealth": 150, "_availableTowers": [{"tower": "DartMonkey", "max": 1}],
            "_availablePowers": [{"power": "CashDrop", "max": 4}]}
    base = btd6._odyssey_card_height(meta, 1)
    # 地图越多卡片越高（每张 130px）
    assert btd6._odyssey_card_height(meta, 3) == base + 2 * 130
    # 极限模式徽章增加高度
    ext = dict(meta, isExtreme=True)
    assert btd6._odyssey_card_height(ext, 1) == base + 22


def test_merge_history_merges_by_id(monkeypatch, tmp_path):
    monkeypatch.setattr(btd6.push, "HISTORY_FILE", str(tmp_path / "history.json"))
    # 首次合并写入并按 start 降序
    assert btd6._merge_history("boss", [{"id": "a", "start": 2}, {"id": "b", "start": 1}]) is True
    hist = btd6._load_history()
    assert [x["id"] for x in hist["boss"]] == ["a", "b"]
    # 重复 id 不视为变更
    assert btd6._merge_history("boss", [{"id": "a", "start": 2}]) is False
    # 新增更近期数排到最前
    assert btd6._merge_history("boss", [{"id": "c", "start": 9}]) is True
    hist = btd6._load_history()
    assert [x["id"] for x in hist["boss"]] == ["c", "a", "b"]
    # 空列表不写入
    assert btd6._merge_history("ct", []) is False
    assert "ct" not in btd6._load_history()


def test_odyssey_html_renders_difficulties():
    diffs = {
        "easy": {
            "meta": {"startingHealth": 150, "_rewards": ["MonkeyMoney:100"],
                     "_availablePowers": [{"power": "CashDrop", "max": 4}],
                     "_availableTowers": [{"tower": "DartMonkey", "max": 1}]},
            "maps": [{"name": "Odyssey Map 1", "img": "data:image/jpg;base64,M1",
                      "difficulty": "Easy", "startingCash": 650}],
        },
        "medium": {"meta": None, "maps": []},
        "hard": {"meta": {"startingHealth": 100, "isExtreme": True, "_rewards": []},
                 "maps": []},
    }
    col = {"ev": dict(BOSS_UPCOMING, name="Sniper Target Practice",
                      description="打气球！"), "diffs": diffs}
    html = btd6.odyssey_diff_html(col, "easy", "简单")
    assert "Sniper Target Practice" in html and "打气球！" in html
    assert "简单" in html  # 难度标题渲染为「简单 / 标准」
    assert "猴币×100" not in html  # 奖励改为图标 + 数值
    assert "现金掉落" in html and "ody-power-tile" in html
    assert "Odyssey Map 1" in html and "M1" in html  # 地图缩略图
    html_hard = btd6.odyssey_diff_html(col, "hard", "困难")
    assert "极限模式" in html_hard
    html_med = btd6.odyssey_diff_html(col, "medium", "中等")
    assert "（该难度数据缺失）" in html_med


def test_player_html_renders_profile():
    p = {
        "displayName": "ISAB", "rank": 155, "veteranRank": 58, "achievements": 161,
        "mostExperiencedMonkey": "MonkeyVillage", "highestRound": 800, "followers": 5971,
        "bloonsPopped": {"bloonsPopped": 1884842684, "bossesPopped": 17154,
                         "goldenBloonsPopped": 500},
        "gameplay": {"gameCount": 100, "gamesWon": 90, "challengesCompleted": 821,
                     "cashEarned": 3197755568, "totalTrophiesEarned": 5,
                     "totalOdysseyStars": 7, "highestRoundCHIMPS": 500},
    }
    col = {"p": p, "banner": "data:image/png;base64,B", "avatar": "data:image/png;base64,A"}
    html = btd6.player_html(col)
    assert "ISAB" in html and "等级 155" in html and "老兵 58" in html
    assert "18.8亿" in html and "猴村" in html
    assert html.count("<img") == 2  # 横幅 + 头像


def test_player_text_fallback():
    p = {
        "displayName": "ISAB", "rank": 155, "veteranRank": 0, "achievements": 161,
        "mostExperiencedMonkey": "MonkeyVillage", "highestRound": 800, "followers": 5971,
        "bloonsPopped": {"bloonsPopped": 1884842684},
        "gameplay": {"highestRoundCHIMPS": 500},
    }
    text = btd6.player_text({"p": p})
    assert "ISAB" in text and "18.8亿" in text and "猴村" in text
    assert btd6.player_text({"empty": "未找到该玩家"}) == "未找到该玩家"


def test_odyssey_text_fallback():
    col = {"ev": dict(BOSS_UPCOMING, name="Skulls", description="骷髅主题"),
           "diffs": {"easy": {"meta": None, "maps": []},
                     "medium": {"meta": None, "maps": []},
                     "hard": {"meta": None, "maps": []}}}
    text = btd6.odyssey_text(col)
    assert "Skulls" in text and "骷髅主题" in text and "【简单】" in text
    assert btd6.odyssey_text({"empty": "🏰 当前没有远征活动"}) == "🏰 当前没有远征活动"


def test_maps_html_rows_and_escape():
    html = btd6.maps_html({
        "label": "最新",
        "entries": [(1, "Map<A>", "2026-08-25", "", 1234, 56)],
    })
    assert "自制地图 · 最新 Top1" in html
    assert "Map&lt;A&gt;" in html
    assert "2026-08-25" in html
    assert "游玩 1,234" in html and "点赞 56" in html
    assert "ody-map-empty" in html  # 无缩略图占位（游戏风格）


def test_maps_html_with_thumbnails():
    html = btd6.maps_html({
        "label": "热门",
        "entries": [(1, "PrettyMap", "2026-08-25", "data:image/jpg;base64,THUMB", 10, 2)],
    })
    assert "<img class='ody-map-img'" in html and "THUMB" in html


# ---------------- 渲染缓存 / 素材缓存 / 预热 ----------------

def test_render_card_sync_reuses_cached_png(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(btd6.assets, "CACHE_DIR", str(tmp_path))

    def fake_render(html, prefix, cache_dir, max_age, dpi):
        calls.append(html[:10])
        p = os.path.join(cache_dir, f"raw_{len(calls)}.png")
        with open(p, "wb") as f:
            f.write(b"x")
        return p

    monkeypatch.setattr(btd6.cards, "render_html_to_png", fake_render)
    html = "<html>aaa</html>"
    p1 = btd6._render_card_sync("t", html)
    p2 = btd6._render_card_sync("t", html)  # 同内容命中缓存，不再渲染
    assert p1 == p2 and len(calls) == 1
    p3 = btd6._render_card_sync("t", "<html>bbb</html>")
    assert p3 != p1 and len(calls) == 2


def test_render_card_sync_persists_stable_cards(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(btd6.assets, "CACHE_DIR", str(tmp_path))

    def fake_render(html, prefix, cache_dir, max_age, dpi):
        calls.append((html, cache_dir))
        p = os.path.join(cache_dir, f"raw_{len(calls)}.png")
        os.makedirs(cache_dir, exist_ok=True)
        with open(p, "wb") as f:
            f.write(b"x")
        return p

    monkeypatch.setattr(btd6.cards, "render_html_to_png", fake_render)
    p1 = btd6._render_card_sync("btd6rule", "<html>same</html>")
    p2 = btd6._render_card_sync("btd6rule", "<html>same</html>")
    assert p1 == p2 and len(calls) == 1
    assert os.path.dirname(p1) == os.path.join(str(tmp_path), "cards")
    assert os.path.dirname(calls[0][1]) == str(tmp_path)

    p3 = btd6._render_card_sync("btd6rule", "<html>changed</html>")
    assert p3 != p1 and len(calls) == 2
    assert os.path.exists(p1) and os.path.exists(p3)


def test_asset_data_url_caches_to_disk(monkeypatch, tmp_path):
    monkeypatch.setattr(btd6.assets, "ASSET_DIR", str(tmp_path))

    class _Resp:
        content = b"\x89PNG\r\n\x1a\ndata"
        headers = {"content-type": "image/png"}

        def raise_for_status(self):
            return None

    class _Client:
        async def get(self, url, **kw):
            return _Resp()

    monkeypatch.setattr(btd6.nkapi, "get_http_client", lambda t: _Client())
    url = "https://static-api.nkstatic.com/img.png"
    d1 = asyncio.run(btd6._asset_data_url(url))
    assert d1.startswith("data:image/png;base64,")
    btd6._asset_mem.clear()  # 清掉内存层，验证落盘缓存仍可命中
    d2 = asyncio.run(btd6._asset_data_url(url))
    assert d1 == d2
    # 第二次应命中磁盘缓存：换成会抛异常的客户端验证不再发请求
    def boom(t):
        raise AssertionError("disk cache hit expected")

    monkeypatch.setattr(btd6.nkapi, "get_http_client", boom)
    assert asyncio.run(btd6._asset_data_url(url)) == d1


def test_asset_data_url_empty_url():
    assert asyncio.run(btd6._asset_data_url("")) == ""


def test_asset_data_url_rejects_unverified_image_bytes(monkeypatch):
    class _Resp:
        content = b"not an image"
        headers = {"content-type": "image/svg+xml"}

        def raise_for_status(self):
            return None

    class _Client:
        async def get(self, url, **kw):
            return _Resp()

    monkeypatch.setattr(btd6.nkapi, "get_http_client", lambda t: _Client())
    assert asyncio.run(btd6._asset_data_url("https://static-api.nkstatic.com/not-image.svg")) == ""


def test_prune_cache_files_obeys_count_and_bytes(tmp_path):
    paths = [tmp_path / f"{i}.txt" for i in range(3)]
    for i, path in enumerate(paths):
        path.write_bytes(b"x" * 10)
        os.utime(path, (i + 1, i + 1))
    btd6._prune_cache_files(str(tmp_path), ".txt", max_files=2, max_bytes=100)
    assert not paths[0].exists()
    assert paths[1].exists() and paths[2].exists()


def test_overview_html_embeds_images():
    """总览卡片图标全部来自本地 UI 素材/占位符，collect 层不再预取远端配图字段。"""
    data = {
        "races": [RACE_ACTIVE], "bosses": [BOSS_UPCOMING], "cts": [CT_ACTIVE], "now": NOW,
    }
    html = btd6.overview_html(data)
    # 三段式结构：进行中/即将开始/已结束 段落齐备
    assert "进行中" in html and "即将开始" in html and "已结束" in html
    # 三行活动行（race 进行中 / boss 未开始 / ct 进行中），每行含图标+名称+日期+状态
    assert html.count("<div class='ev-row'>") == 3
    # 已删除的预取字段不再参与渲染（图标为本地 data URL 或 emoji 占位）
    assert "race_map_by_id" not in html and "boss_img_by_id" not in html


def test_rules_html_embeds_map_image():
    html = btd6.rules_html({
        "prefix": "🏁 竞赛", "meta": META, "map_img": "data:image/png;base64,MAP",
        "side_img": "", "scoring_cn": "最快用时",
    })
    assert "<img" in html and "MAP" in html


def test_prewarm_once_renders_active_leaderboards_only(monkeypatch):
    """瘦身后预热：仅归档 + 进行中竞赛/Boss 榜单；不再渲染总览/规则/每日卡。"""
    rendered = []

    async def fake_render(prefix, html_fn):
        rendered.append(prefix)
        return "/tmp/x.png"

    data = {
        "races": [RACE_ACTIVE], "bosses": [BOSS_UPCOMING], "cts": [CT_ACTIVE], "now": NOW,
    }

    async def fake_collect():
        return data

    lb = {"head": "h", "status": "s", "entries": [(1, "a", "1:00.000")]}

    async def fake_lb(kind, variant, rows):
        return lb

    async def fake_archive(data=None):
        return None

    monkeypatch.setattr(btd6.cards, "_render_card", fake_render)
    monkeypatch.setattr(btd6.collect, "collect_overview", fake_collect)
    monkeypatch.setattr(btd6.collect, "collect_leaderboard", fake_lb)
    monkeypatch.setattr(btd6.push, "_archive_events", fake_archive)

    asyncio.run(btd6._prewarm_once())
    assert rendered == ["btd6lb", "btd6lb", "btd6lb"]  # 竞赛榜 + Boss 标准榜 + CT 个人榜，无 btd6ov/btd6rule/每日
    assert btd6.push._prewarm_running is False

    # 并发保护：预热进行中再次触发直接返回
    rendered.clear()
    btd6.push._prewarm_running = True
    asyncio.run(btd6._prewarm_once())
    assert rendered == []
    btd6.push._prewarm_running = False


# ---------------- 2026-08-30 审查修复补充 ----------------

def test_fetch_leaderboard_paginated_no_duplicate_request(monkeypatch):
    """分页拉取：next 随信封缓存，同一 URL 不再"fetch_body 后又 _http_get"重复请求；
    TTL 内重复查询全部走缓存（含 next），不发起任何请求。"""
    http_calls = []

    def _mk(entries, nxt=None):
        class _Resp:
            content = b"{}"

            def raise_for_status(self):
                return None

            def json(self):
                return {"success": True, "body": entries, "next": nxt}

        return _Resp()

    envelopes = {
        "https://data.ninjakiwi.com/lb": _mk(
            [{"displayName": f"p1_{i}", "score": i} for i in range(50)],
            "https://data.ninjakiwi.com/lb?page=2"),
        "https://data.ninjakiwi.com/lb?page=2": _mk(
            [{"displayName": f"p2_{i}", "score": i} for i in range(5)]),
    }

    async def fake_http_get(url, timeout):
        http_calls.append(url)
        return envelopes[url]

    monkeypatch.setattr(btd6.nkapi, "_http_get", fake_http_get)
    entries = asyncio.run(btd6.fetch_leaderboard_paginated("https://data.ninjakiwi.com/lb", 60))
    assert len(entries) == 55
    assert [e["displayName"] for e in entries[50:]] == [f"p2_{i}" for i in range(5)]
    # 每个 URL 恰好一次信封请求（旧逻辑在第 1 页会请求两次）
    assert http_calls == ["https://data.ninjakiwi.com/lb", "https://data.ninjakiwi.com/lb?page=2"]
    assert btd6._cache_get("https://data.ninjakiwi.com/lb") is not None
    # 第二次查询：缓存（body + next）全命中，零请求
    entries2 = asyncio.run(btd6.fetch_leaderboard_paginated("https://data.ninjakiwi.com/lb", 60))
    assert len(entries2) == 55
    assert http_calls == ["https://data.ninjakiwi.com/lb", "https://data.ninjakiwi.com/lb?page=2"]


def test_fetch_leaderboard_paginated_next_unknown_falls_back(monkeypatch):
    """body 命中缓存但 next 未知（旧缓存条目）时，补一次信封请求读 next。"""
    http_calls = []

    class _Resp:
        content = b"{}"

        def raise_for_status(self):
            return None

        def json(self):
            return {"success": True, "body": [], "next": None}

    async def fake_http_get(url, timeout):
        http_calls.append(url)
        return _Resp()

    monkeypatch.setattr(btd6.nkapi, "_http_get", fake_http_get)
    btd6._cache_put("https://data.ninjakiwi.com/lb", [{"displayName": "a", "score": 1}])
    entries = asyncio.run(btd6.fetch_leaderboard_paginated("https://data.ninjakiwi.com/lb", 50))
    assert [e["displayName"] for e in entries] == ["a"]
    assert http_calls == ["https://data.ninjakiwi.com/lb"]
    assert "https://data.ninjakiwi.com/lb" in btd6._lb_next_cache


def test_fetch_rank_entry_direct_target_page(monkeypatch):
    """排名查询直接拉目标页，不从第 1 页逐页串行拉 20 页。"""
    calls = []
    entries_p1 = [{"displayName": f"p1_{i}", "score": 100000 + i} for i in range(50)]
    entries_p2 = [{"displayName": f"p2_{i}", "score": 200000 + i} for i in range(50)]
    bodies = {
        btd6.URL_RACES: [RACE_ACTIVE],
        RACE_ACTIVE["leaderboard"]: entries_p1,
        RACE_ACTIVE["leaderboard"] + "?page=2": entries_p2,
    }

    async def fake_fetch(url):
        calls.append(url)
        return bodies.get(url, [])

    monkeypatch.setattr(btd6.nkapi, "fetch_body", fake_fetch)
    # 第 2 页名次：仅拉活动列表 + 目标页各一次，不从第 1 页逐页串行拉
    entry = asyncio.run(btd6.fetch_rank_entry("race", "", 60))
    assert entry["displayName"] == "p2_9"
    assert calls == [btd6.URL_RACES, RACE_ACTIVE["leaderboard"] + "?page=2"]
    # 第 1 页常见场景行为不变
    calls.clear()
    entry = asyncio.run(btd6.fetch_rank_entry("race", "", 2))
    assert entry["displayName"] == "p1_1"
    assert calls == [btd6.URL_RACES, RACE_ACTIVE["leaderboard"]]
    # 榜单不足该名次 → None（由调用方报"未找到/超出可查范围"），且只请求了目标页
    calls.clear()
    assert asyncio.run(btd6.fetch_rank_entry("race", "", 500)) is None
    assert calls == [btd6.URL_RACES, RACE_ACTIVE["leaderboard"] + "?page=10"]


def test_fetch_rank_entry_beyond_max_page_returns_none(monkeypatch):
    """boss/ct 每页 25 人，排名 501-1000 超出 LB_MAX_PAGE=20 → 直接 None，不发任何请求。"""
    calls = []

    async def fake_fetch(url):
        calls.append(url)
        return []

    monkeypatch.setattr(btd6.nkapi, "fetch_body", fake_fetch)
    assert asyncio.run(btd6.fetch_rank_entry("boss", "standard", 600)) is None
    assert calls == []


def test_push_jobs_apscheduler_compat():
    """push.py 的全部 scheduled_job 装饰器参数必须与真实 APScheduler 兼容。

    APScheduler 3.11 的 scheduled_job 装饰器内部恒以 replace_existing=True 注册，
    显式传 replace_existing 会与内部关键字冲突（add_job() got multiple values），
    导致插件导入失败；conftest 的 scheduler stub 接受任意参数无法拦截，
    此处对真实签名做校验。
    """
    import re as _re
    import subprocess
    import sys as _sys
    from pathlib import Path as _Path

    # conftest 会 stub 掉 apscheduler，因此用干净的子进程取真实签名
    code = ("import inspect; from apscheduler.schedulers.asyncio import AsyncIOScheduler; "
            "print(','.join(inspect.signature(AsyncIOScheduler.scheduled_job).parameters))")
    proc = subprocess.run([_sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, f"无法获取 APScheduler 真实签名: {proc.stderr}"
    real_params = set(proc.stdout.strip().split(","))
    real_params |= {"hour", "minute", "second", "day", "week", "day_of_week",
                    "month", "year", "start_date", "end_date", "timezone", "jitter"}
    src = _Path(btd6.push.__file__).read_text(encoding="utf-8")
    src_nc = _re.sub(r"#.*", "", src)  # 去掉注释，避免误拦说明文字
    assert "replace_existing" not in src_nc, (
        "scheduled_job 装饰器不接受 replace_existing（3.11 内部恒为 True），"
        "需要幂等注册请改用 scheduler.add_job(..., replace_existing=True)")
    calls = _re.findall(r"@scheduler\.scheduled_job\((.*?)\)\n(?:async )?def",
                        src_nc, flags=_re.DOTALL)
    assert len(calls) >= 20, "未能从 push.py 解析出全部定时任务装饰器"
    for call in calls:
        kwargs = set(_re.findall(r"(\w+)\s*=", call)) - {"cron"}
        unknown = kwargs - real_params
        assert not unknown, f"scheduled_job 存在真实 APScheduler 不接受的参数: {unknown}"


def test_fetch_body_no_scores_available_is_empty_state(monkeypatch):
    """NK API 对暂无分数的榜单返回 success=false + "No Scores Available"，
    属空态而非故障：应返回空列表并写缓存（next=None 终止分页），不抛异常。"""
    _URL = "https://data.ninjakiwi.com/btd6/ct/testev/leaderboard/player"

    class _Resp:
        content = b'{"error": "No Scores Available", "success": false}'

        def raise_for_status(self):
            pass

        def json(self):
            return {"error": "No Scores Available", "success": False}

    async def fake_get(url, timeout):
        return _Resp()

    monkeypatch.setattr(btd6.nkapi, "_http_get", fake_get)
    body = asyncio.run(btd6.fetch_body(_URL))
    assert body == []
    assert _URL in btd6.nkapi._cache
    assert btd6.nkapi._lb_next_cache[_URL] is None
    # 缓存命中后重复取同样为空态，不再发请求
    assert asyncio.run(btd6.fetch_body(_URL)) == []


def test_push_daily_uses_real_prefix(monkeypatch, tmp_path):
    """每日推送：高级/标准各用真实期号前缀，不复用外层 Standard 的 label。"""
    monkeypatch.setattr(btd6.push, "BTD6_PUSH_STATE_FILE", str(tmp_path / "state.json"))
    sent = []

    class _Bot:
        async def send_group_msg(self, group_id=None, message=None, **kw):
            sent.append(str(message))

    async def fake_render(prefix, html_fn):
        # 必须是绝对路径：Windows 下 Path("/tmp/x.png") 非绝对，as_uri() 会抛 ValueError
        return str(tmp_path / "x.png")

    async def fake_collect(adv):
        return {"prefix": "每日高级·第2923期" if adv else "每日标准·第2936期", "meta": {}}

    monkeypatch.setattr(btd6.push, "get_bot", lambda: _Bot())
    monkeypatch.setattr(btd6.cards, "_render_card", fake_render)
    monkeypatch.setattr(btd6.collect, "collect_daily", fake_collect)

    asyncio.run(btd6._btd6_push_single(
        "daily", {"id": "d1", "name": "Standard 2936: X"}, "d1", "Standard 2936: X", {100}))
    assert any("每日挑战已刷新·每日标准·第2936期" in m for m in sent)
    assert any("每日挑战已刷新·每日高级·第2923期" in m for m in sent)
    for msg in sent:
        if "每日高级" in msg:
            assert "2936" not in msg  # 高级推送不得携带标准期号


def test_cooldown_release_and_ct_key():
    event = _ev(".btd6领土")
    # ct 命令使用独立冷却 key，不再与 events 共用
    assert btd6._cooldown_remaining(event, "ct", "default") == 0
    assert btd6._cooldown_remaining(event, "events", "default") == 0
    assert btd6._cooldown_remaining(event, "ct", "default") >= 1
    # 失败路径回滚后可立即重试
    btd6._release_cooldown(event, "ct")
    assert btd6._cooldown_remaining(event, "ct", "default") == 0


def test_translation_tables_unified():
    """两套中文翻译表已合并：驼峰与带空格键归一化命中，译名以总览主路径为准。"""
    assert btd6.tower_cn("MonkeyVillage") == "猴村" == btd6.tower_cn("Monkey Village")
    assert btd6.tower_cn("DartMonkey") == "飞镖猴" == btd6.tower_cn("Dart Monkey")
    assert btd6.tower_cn("Skywarden") == "天空守卫"
    assert btd6.tower_cn("GlueGunner") == "胶水枪手" == btd6.tower_cn("Glue Gunner")
    assert btd6.hero_cn("StrikerJones") == "琼斯"
    assert btd6.hero_cn("DanDMonke") == "丹迪猴"
    assert btd6.hero_cn("Captain Churchill") == "丘吉尔"
    assert btd6.boss_cn("Phayze") == "幻影" == btd6.boss_cn("phayze")
    assert btd6.boss_cn("Blastapopolous") == "爆裂魔炎"  # API 历史拼写变体
    # 旧 rush 侧重复表已删除
    for gone in ("_BOSS_CN", "_TOWER_CN", "_HERO_CN"):
        assert not hasattr(btd6, gone)


def test_rush_card_has_data_version_footer():
    """rush 卡片底部带数据版本小字。"""
    col = {"ev": {"id": "rush1", "name": "A Boss Rush Event", "start": NOW - DAY, "end": NOW + DAY},
           "hero": "",
           "diffs": {"default": {"meta": {"isExtreme": False}, "maps": [
               {"stage": 1, "name": "Island 1", "map": "Bloonarius", "map_name": "Logs",
                "map_img": "", "boss": "Bloonarius", "kills": 100,
                "rewards": [("猴币", "100")], "reward_text": "猴币 100",
                "towers": ["DartMonkey"], "removed": [], "relics": [], "new_relic": None,
                "img": "data:image/png;base64,BOSS"},
           ]}}}
    html = btd6._rush_diff_html(col)
    assert "数据版本: Constants v3.0.0 · rushgen" in html
    assert "膨胀气球神" in html  # 合并后的统一 Boss 译名（总览主路径）


# ---------------- 2026-08-30 复查修复回归 ----------------

def test_text_stale_note_appended_without_crash():
    """A1 回归：带 stale_note 的采集结果在三个文本出口不再抛 TypeError。"""
    note = "（数据已 24+ 小时未刷新，可能过期）"
    lb_col = {"head": "🏁 竞赛「T」排行榜", "status": "剩余 1小时",
              "entries": [(1, "a", "1:00.000")], "stale_note": note}
    text = btd6.leaderboard_text(lb_col)
    assert note in text and text.endswith(note)
    maps_col = {"label": "最新", "entries": [(1, "MapA", "2026-08-25")], "stale_note": note}
    assert note in btd6.maps_text(maps_col)
    p = {"displayName": "ISAB", "rank": 1, "bloonsPopped": {}, "gameplay": {}}
    assert note in btd6.player_text({"p": p, "stale_note": note})


def test_all_exports_resolvable():
    """B4 回归：__all__ 里的每个名字都必须真实存在于模块命名空间（防幽灵导出再犯）。"""
    assert set(btd6.__all__) <= set(dir(btd6))


def test_cache_budget_recovers_after_stale_hit_and_rewrite(monkeypatch):
    """B2 回归：条目 TTL 过期 + stale 命中 + 后台刷新写入新数据后，字节预算能回落。"""
    monkeypatch.setattr(btd6.nkapi, "MAX_JSON_MEM_BYTES", 10 ** 9)
    url = btd6.URL_RACES
    btd6._cache_put(url, {"old": "a" * 500})
    # 模拟 TTL 过期后 _cache_get 弹出
    _expiry, body = btd6._cache[url]
    btd6._cache[url] = (time.monotonic() - 1, body)
    assert btd6._cache_get(url) is None
    # 过期弹出同步清 _cache_sizes（死账不复存在），_stale 及伴随字典保留维持 SWR
    assert url not in btd6._cache_sizes
    assert url in btd6._stale and url in btd6._stale_at

    async def fake_refresh(u):
        btd6._cache_put(u, {"new": "b" * 500})

    monkeypatch.setattr(btd6.nkapi, "_refresh_url", fake_refresh)

    async def main():
        served = await btd6.fetch_body(url)  # stale 命中
        await asyncio.sleep(0.01)  # 等后台刷新任务写入新条目
        return served

    assert asyncio.run(main()) == {"old": "a" * 500}
    total = sum(btd6._cache_sizes.get(u, 0) for u in btd6._cache)
    assert total > 0 and total == btd6._cache_sizes[url]  # 预算只含新条目


def test_cache_eviction_cleans_side_dictionaries(monkeypatch):
    """B2 回归：预算淘汰 victim 时旁路字典（stale/next/stale_served/fail_counts）同步清理。"""
    url = "https://data.ninjakiwi.com/evict-old"
    url2 = "https://data.ninjakiwi.com/evict-new"
    size = btd6._body_size({"pad": "a" * 100})
    monkeypatch.setattr(btd6.nkapi, "MAX_JSON_MEM_BYTES", size + 10)
    btd6._cache_put(url, {"pad": "a" * 100})
    btd6._lb_next_cache[url] = url + "?page=2"
    btd6._stale_served.add(url)
    btd6._refresh_fail_counts[url] = 2
    btd6._cache_put(url2, {"pad": "b" * 100})  # 触发预算淘汰：最旧的 url 整体移除
    assert url not in btd6._cache and url not in btd6._stale
    assert url not in btd6._cache_sizes and url not in btd6._stale_at
    assert url not in btd6._lb_next_cache
    assert url not in btd6._stale_served
    assert url not in btd6._refresh_fail_counts
    assert url2 in btd6._cache  # 新条目仍在预算内


def test_handler_rules_boss_failure_releases_cooldown(monkeypatch):
    """C1：Boss 规则双版本全部失败时回滚冷却，允许立即重试。"""
    event = _ev(".btd6竞速 boss")

    async def broken(url):
        raise RuntimeError("down")

    monkeypatch.setattr(btd6.nkapi, "fetch_body", broken)
    with pytest.raises(FinishedException):
        asyncio.run(btd6.rules_cmd.handlers[0](event))
    # 处理器入口先加冷却、失败路径必须回滚：结束消息为失败文案而非限频文案
    assert "获取 BTD6 规则失败" in str(btd6.rules_cmd.finished[-1])
    assert not any(k.endswith(":rules") for k in btd6._cooldowns)


def test_handler_leaderboard_double_board_failure_releases_cooldown(monkeypatch):
    """C1：默认双榜全部失败时回滚冷却，允许立即重试。"""
    event = _ev(".btd6排行 boss")

    async def broken(url):
        raise RuntimeError("down")

    monkeypatch.setattr(btd6.nkapi, "fetch_body", broken)
    with pytest.raises(FinishedException):
        asyncio.run(btd6.lb_cmd.handlers[0](event))
    assert "获取 BTD6 排行榜失败" in str(btd6.lb_cmd.finished[-1])
    assert not any(k.endswith(":leaderboard") for k in btd6._cooldowns)


def test_collect_leaderboard_stale_page_triggers_warn(monkeypatch):
    """C3：分页第 2 页实际被 stale 服务时同样触发过期提示。"""
    # fetch_body 走真实校验：榜单 URL 必须是允许列表内的主机
    race = dict(RACE_ACTIVE, leaderboard="https://data.ninjakiwi.com/btd6/races/x/leaderboard")
    lb_url = race["leaderboard"]
    page2_url = lb_url + "?page=2"
    entries_p1 = [{"displayName": f"p1_{i}", "score": 100000 + i} for i in range(50)]
    entries_p2 = [{"displayName": f"p2_{i}", "score": 200000 + i} for i in range(5)]
    btd6._cache_put(btd6.URL_RACES, [race])
    btd6._cache_put(lb_url, entries_p1)
    btd6._stale[page2_url] = entries_p2
    btd6._stale_at[page2_url] = time.monotonic() - 25 * 3600
    btd6._lb_next_cache[lb_url] = page2_url
    btd6._lb_next_cache[page2_url] = None

    async def no_http(url, timeout):
        raise AssertionError("stale 命中时不应发起网络请求")

    async def fake_refresh(u):
        return None

    monkeypatch.setattr(btd6.nkapi, "_http_get", no_http)
    monkeypatch.setattr(btd6.nkapi, "_refresh_url", fake_refresh)

    async def main():
        col = await btd6.collect.collect_leaderboard("race", "", 60)
        await asyncio.sleep(0.01)  # 让后台刷新任务执行
        return col

    col = asyncio.run(main())
    assert len(col["entries"]) == 55
    assert col["stale_note"] == btd6.STALE_WARN_TEXT


def test_map_cn_translation_in_rules_text():
    """C7：MAP_CN 接入规则文本——有译名显示中文，无译名回退原始内部名。"""
    translated = dict(META, map="Logs")
    assert "地图：原木｜" in btd6.format_rules(translated, "🏁 竞赛")
    untranslated = dict(META, map="ThreeMinesAround")
    assert "地图：ThreeMinesAround｜" in btd6.format_rules(untranslated, "🏁 竞赛")
    # FLAT 归一化查找：带空格写法同样命中
    assert btd6.map_cn("TownCentre") == "城镇中心"
    assert btd6.map_cn("Town Centre") == "城镇中心"
    assert btd6.map_cn("UnknownMap") == "UnknownMap"


def test_rushgen_empty_stage_scores_warns(caplog):
    """C8：StageScores 必须为非空 list；AvailableBosses/RelicChances 为空同样告警（不抛异常）。"""
    rg = btd6.rushgen
    with caplog.at_level(logging.WARNING):
        rg._validate_constants({"bossRush": {"StageScores": [],
                                             "RandomSettings": {"TowerSettings": {}}}})
    assert any("StageScores" in r.message for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        rg._validate_constants({"bossRush": {"StageScores": [1, 2],
                                             "RandomSettings": {"TowerSettings": {},
                                                                "AvailableBosses": [],
                                                                "RelicChances": {}}}})
    assert any("AvailableBosses" in r.message for r in caplog.records)
    assert any("RelicChances" in r.message for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        rg._validate_constants({"bossRush": {"StageScores": [1], "StageRewards": [],
                                             "RandomSettings": {"TowerSettings": {}}},
                                "mapsInOrder": {}, "towersInOrder": [], "heroesInOrder": []})
    assert not caplog.records  # 正常结构无告警


def test_prewarm_odyssey_uses_html_fn_and_unified_h(monkeypatch):
    """C9：远征预热与 handle_odyssey 注入相同的 _unified_h，且 HTML 构建延迟到渲染线程。"""
    captured = {}

    async def fake_render(prefix, html_fn):
        captured["html_fn"] = html_fn
        return "x.png"

    diffs = {
        d: {"meta": {"startingHealth": 150, "_availableTowers": [{"tower": "DartMonkey", "max": 1}]},
            "maps": []}
        for d, _ in btd6._ODYSSEY_DIFFS
    }
    col = {"ev": dict(BOSS_UPCOMING), "diffs": diffs}

    async def fake_collect_overview():
        return {"races": [], "bosses": [], "cts": [],
                "odysseys": [dict(BOSS_UPCOMING, start=NOW - DAY, end=NOW + DAY)],
                "rush": [], "now": NOW}

    async def fake_collect_odyssey():
        return col

    async def fake_archive(data=None):
        return None

    monkeypatch.setattr(btd6.cards, "_render_card", fake_render)
    monkeypatch.setattr(btd6.collect, "collect_overview", fake_collect_overview)
    monkeypatch.setattr(btd6.collect, "collect_odyssey", fake_collect_odyssey)
    monkeypatch.setattr(btd6.push, "_archive_events", fake_archive)

    asyncio.run(btd6._prewarm_once())
    assert isinstance(col["diffs"]["easy"].get("_unified_h"), int)
    # html_fn 延迟构建：HTML 在渲染线程内才生成（不在协程内同步构建）
    assert captured["html_fn"]()


def test_prewarm_lb_hourly_only_ongoing(monkeypatch):
    """D2：每小时榜单对齐任务只渲染进行中活动（race+ct），未开始的 boss 跳过。"""
    rendered = []
    btd6.push._prewarm_running = False

    async def fake_render(prefix, html_fn):
        rendered.append(prefix)
        return "x.png"

    data = {"races": [RACE_ACTIVE], "bosses": [BOSS_UPCOMING], "cts": [CT_ACTIVE], "now": NOW}

    async def fake_collect():
        return data

    lb = {"head": "h", "status": "s", "entries": [(1, "a", "1:00.000")]}

    async def fake_lb(kind, variant, rows):
        return lb

    monkeypatch.setattr(btd6.cards, "_render_card", fake_render)
    monkeypatch.setattr(btd6.collect, "collect_overview", fake_collect)
    monkeypatch.setattr(btd6.collect, "collect_leaderboard", fake_lb)

    async def fake_daily(adv):
        return {"empty": "暂无每日挑战数据"}  # 每日卡预热：empty 时跳过，不渲染

    monkeypatch.setattr(btd6.collect, "collect_daily", fake_daily)

    asyncio.run(btd6.push.btd6_prewarm_lb_hourly_job())
    assert rendered == ["btd6lb", "btd6lb"]


def test_prewarm_daily_cards_renders_both(monkeypatch):
    """每小时预热含每日卡：标准+高级各渲一张；empty 的跳过；渲染失败不抛。"""
    rendered = []

    async def fake_collect(adv):
        if adv:
            return {"empty": "暂无每日挑战数据"}
        return {"prefix": "Standard 2936", "meta": {}, "map_img": "",
                "side_img": "", "stale_note": ""}

    async def fake_render(prefix, html_fn):
        rendered.append(prefix)
        return f"/tmp/{prefix}.png"

    monkeypatch.setattr(btd6.collect, "collect_daily", fake_collect)
    monkeypatch.setattr(btd6.cards, "_render_card", fake_render)
    done = asyncio.run(btd6.push._prewarm_daily_cards())
    assert rendered == ["btd6daily"]
    assert done == 1

    def boom(adv):
        raise RuntimeError("fetch failed")

    monkeypatch.setattr(btd6.collect, "collect_daily", boom)
    assert asyncio.run(btd6.push._prewarm_daily_cards()) == 0  # 异常吞掉不抛


def test_asset_data_url_dedupes_inflight(monkeypatch, tmp_path):
    """D1：同 URL 并发冷读共享同一下载任务，只发一次请求。"""
    monkeypatch.setattr(btd6.assets, "ASSET_DIR", str(tmp_path))
    calls = []

    class _Resp:
        content = b"\x89PNG\r\n\x1a\ndata"

        def raise_for_status(self):
            return None

    class _Client:
        async def get(self, url, **kw):
            calls.append(url)
            await asyncio.sleep(0.01)
            return _Resp()

    monkeypatch.setattr(btd6.nkapi, "get_http_client", lambda t: _Client())
    url = "https://static-api.nkstatic.com/dedup.png"
    btd6._asset_mem.clear()

    async def main():
        return await asyncio.gather(btd6._asset_data_url(url), btd6._asset_data_url(url))

    d1, d2 = asyncio.run(main())
    assert d1 == d2 and d1.startswith("data:image/png;base64,")
    assert calls == [url]
