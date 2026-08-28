import asyncio
import os

import pytest
from conftest import FinishedException, GroupMessageEvent, MessageSegment

from helpers import load_plugin
import sys

# 移除缓存的旧模块，确保测试反映最新的插件代码
sys.modules.pop("plugin_btd6", None)
btd6 = load_plugin("btd6")

NOW = 1_787_000_000_000  # 固定"当前时间"（毫秒）
DAY = 86_400_000


@pytest.fixture(autouse=True)
def _clear_cache():
    btd6._cache.clear()
    btd6._stale.clear()
    btd6._asset_mem.clear()
    btd6._game_mem.clear()
    btd6._cooldowns.clear()
    yield
    btd6._cache.clear()
    btd6._stale.clear()
    btd6._asset_mem.clear()
    btd6._game_mem.clear()
    btd6._cooldowns.clear()


@pytest.fixture(autouse=True)
def _no_render(monkeypatch):
    """默认禁用图片渲染，处理器统一走文本兜底，测试不依赖 weasyprint 是否安装。"""

    async def broken(prefix, html_fn):
        raise RuntimeError("renderer disabled in tests")

    monkeypatch.setattr(btd6, "_render_card", broken)


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
    assert btd6._race_overview([], NOW) == ["🏁 每周竞赛：暂无数据"]
    ended = dict(RACE_ACTIVE, start=NOW - 9 * DAY, end=NOW - 8 * DAY)
    lines = btd6._race_overview([ended], NOW)
    assert "已于" in "\n".join(lines)


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
    assert "塔禁用：飞镖猴" in text
    assert "塔限购：炼金术士×1" in text
    assert "路径限制：猴村（路1禁3层）" in text
    assert "英雄限定：艾蒂安" in text
    assert "ChosenPrimaryHero" not in text  # 内部占位符不外显
    assert "昆西" not in text  # 被禁英雄不进限定名单


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
    towers = [{"tower": f"T{i}", "max": 0} for i in range(10)]
    lines = btd6.tower_limit_lines(towers)
    assert len(lines) == 1 and "…等10项" in lines[0]


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

    monkeypatch.setattr(btd6, "get_http_client", _boom)
    url = "https://data.ninjakiwi.com/btd6/races"
    btd6._cache_put(url, {"ok": 1})
    assert asyncio.run(btd6.fetch_body(url)) == {"ok": 1}


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
    monkeypatch.setattr(btd6, "fetch_body", _fake_fetch_factory(bodies, calls))
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

    monkeypatch.setattr(btd6, "fetch_body", broken)
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
    monkeypatch.setattr(btd6, "fetch_body", _fake_fetch_factory(bodies))
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
    monkeypatch.setattr(btd6, "fetch_body", _fake_fetch_factory(bodies))
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
    monkeypatch.setattr(btd6, "fetch_body", _fake_fetch_factory(bodies))
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
    monkeypatch.setattr(btd6, "fetch_body", _fake_fetch_factory(bodies, calls))
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

    monkeypatch.setattr(btd6, "fetch_body", broken)
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
    monkeypatch.setattr(btd6, "fetch_body", _fake_fetch_factory(bodies))
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

    monkeypatch.setattr(btd6, "fetch_body", fake_fetch)
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
    monkeypatch.setattr(btd6, "GAME_ASSET_DIR", str(tmp_path))
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
    monkeypatch.setattr(btd6, "fetch_body", _fake_fetch_factory(bodies))
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

    monkeypatch.setattr(btd6, "_render_card", fake_render)
    bodies = {
        btd6.URL_RACES: [RACE_ACTIVE],
        btd6.URL_BOSSES: [BOSS_UPCOMING],
        btd6.URL_CT: [CT_ACTIVE],
    }
    monkeypatch.setattr(btd6, "fetch_body", _fake_fetch_factory(bodies))
    with pytest.raises(FinishedException):
        asyncio.run(btd6.events_cmd.handlers[0](_ev(".btd6活动")))
    seg = btd6.events_cmd.finished[-1]
    assert isinstance(seg, MessageSegment) and seg.type == "image"
    assert str(card) in seg.data["file"]


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
    monkeypatch.setattr(btd6, "GAME_ASSET_DIR", str(gdir))
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
    monkeypatch.setattr(btd6, "GAME_ASSET_DIR", str(gdir))
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
    monkeypatch.setattr(btd6, "HISTORY_FILE", str(tmp_path / "history.json"))
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
    monkeypatch.setattr(btd6, "CACHE_DIR", str(tmp_path))

    def fake_render(html, prefix, cache_dir, max_age, dpi):
        calls.append(html[:10])
        p = os.path.join(cache_dir, f"raw_{len(calls)}.png")
        with open(p, "wb") as f:
            f.write(b"x")
        return p

    monkeypatch.setattr(btd6, "render_html_to_png", fake_render)
    html = "<html>aaa</html>"
    p1 = btd6._render_card_sync("t", html)
    p2 = btd6._render_card_sync("t", html)  # 同内容命中缓存，不再渲染
    assert p1 == p2 and len(calls) == 1
    p3 = btd6._render_card_sync("t", "<html>bbb</html>")
    assert p3 != p1 and len(calls) == 2


def test_render_card_sync_persists_stable_cards(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(btd6, "CACHE_DIR", str(tmp_path))

    def fake_render(html, prefix, cache_dir, max_age, dpi):
        calls.append((html, cache_dir))
        p = os.path.join(cache_dir, f"raw_{len(calls)}.png")
        os.makedirs(cache_dir, exist_ok=True)
        with open(p, "wb") as f:
            f.write(b"x")
        return p

    monkeypatch.setattr(btd6, "render_html_to_png", fake_render)
    p1 = btd6._render_card_sync("btd6rule", "<html>same</html>")
    p2 = btd6._render_card_sync("btd6rule", "<html>same</html>")
    assert p1 == p2 and len(calls) == 1
    assert os.path.dirname(p1) == os.path.join(str(tmp_path), "cards")
    assert os.path.dirname(calls[0][1]) == str(tmp_path)

    p3 = btd6._render_card_sync("btd6rule", "<html>changed</html>")
    assert p3 != p1 and len(calls) == 2
    assert os.path.exists(p1) and os.path.exists(p3)


def test_asset_data_url_caches_to_disk(monkeypatch, tmp_path):
    monkeypatch.setattr(btd6, "ASSET_DIR", str(tmp_path))

    class _Resp:
        content = b"\x89PNG\r\n\x1a\ndata"
        headers = {"content-type": "image/png"}

        def raise_for_status(self):
            return None

    class _Client:
        async def get(self, url, **kw):
            return _Resp()

    monkeypatch.setattr(btd6, "get_http_client", lambda t: _Client())
    url = "https://static-api.nkstatic.com/img.png"
    d1 = asyncio.run(btd6._asset_data_url(url))
    assert d1.startswith("data:image/png;base64,")
    btd6._asset_mem.clear()  # 清掉内存层，验证落盘缓存仍可命中
    d2 = asyncio.run(btd6._asset_data_url(url))
    assert d1 == d2
    # 第二次应命中磁盘缓存：换成会抛异常的客户端验证不再发请求
    def boom(t):
        raise AssertionError("disk cache hit expected")

    monkeypatch.setattr(btd6, "get_http_client", boom)
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

    monkeypatch.setattr(btd6, "get_http_client", lambda t: _Client())
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
    data = {
        "races": [RACE_ACTIVE], "bosses": [BOSS_UPCOMING], "cts": [CT_ACTIVE], "now": NOW,
        "race_map": "data:image/png;base64,AAA", "boss_img": "data:image/png;base64,BBB",
    }
    html = btd6.overview_html(data)
    # Race + Boss + CT (CT now has default ct-event.png) = 3 images
    assert html.count("<img") == 3
    assert "AAA" in html and "BBB" in html


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
        "race_map": "", "boss_img": "",
    }

    async def fake_collect():
        return data

    lb = {"head": "h", "status": "s", "entries": [(1, "a", "1:00.000")]}

    async def fake_lb(kind, variant, rows):
        return lb

    async def fake_archive(data=None):
        return None

    monkeypatch.setattr(btd6, "_render_card", fake_render)
    monkeypatch.setattr(btd6, "collect_overview", fake_collect)
    monkeypatch.setattr(btd6, "collect_leaderboard", fake_lb)
    monkeypatch.setattr(btd6, "_archive_events", fake_archive)

    asyncio.run(btd6._prewarm_once())
    assert rendered == ["btd6lb", "btd6lb", "btd6lb"]  # 竞赛榜 + Boss 标准榜 + CT 个人榜，无 btd6ov/btd6rule/每日
    assert btd6._prewarm_running is False

    # 并发保护：预热进行中再次触发直接返回
    rendered.clear()
    btd6._prewarm_running = True
    asyncio.run(btd6._prewarm_once())
    assert rendered == []
    btd6._prewarm_running = False
