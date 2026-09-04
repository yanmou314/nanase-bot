"""采集层：组合 nkapi 取数 + assets 素材，产出各卡片/文本共用的数据 dict。"""
import asyncio
import json
import logging
import os
import re
import time

from . import assets, i18n, instagen, nkapi, rushgen, util

_logger = logging.getLogger(__name__)


# ---------------- 取数层（供文本/卡片两路共用） ----------------


async def _safe(coro, tag: str = ""):
    """执行协程，任何异常返回 None（预热/可选素材失败不拖垮主流程），warning 记录堆栈。"""
    try:
        return await coro
    except Exception:
        _logger.warning("btd6 optional call failed%s", f" [{tag}]" if tag else "", exc_info=True)
        return None


async def collect_overview(now_ms: int | None = None) -> dict:
    now = now_ms if now_ms is not None else util.bucket_now()
    races, bosses, cts, odysseys, events_body = await asyncio.gather(
        nkapi.fetch_body(nkapi.URL_RACES), nkapi.fetch_body(nkapi.URL_BOSSES), nkapi.fetch_body(nkapi.URL_CT),
        _safe(nkapi.fetch_body(nkapi.URL_ODYSSEY)),
        _safe(nkapi.fetch_body(nkapi.URL_EVENTS)),
    )
    # odysseys 可能为 None（网络失败）或非列表
    if not isinstance(odysseys, list):
        odysseys = []
    # NK /btd6/events 一次性返回 race/boss/ct/odyssey/rush/socialseason/collectableEvent 等
    # 多种活动，按 type 字段拆分；rush/socials/collectables 三类同源，单次请求即可。
    rush = []
    socials = []
    collectables = []
    if isinstance(events_body, list):
        for ev in events_body:
            if not isinstance(ev, dict):
                continue
            t = str(ev.get("type") or "").strip()
            if t == "bossRush":
                rush.append(ev)
            elif t == "socialseason":
                socials.append(ev)
            elif t == "collectableEvent":
                collectables.append(ev)
    # 三段式总览（文本/卡片两路）只消费活动列表本身：配图图标全部来自本地 UI 素材，
    # 无逐活动远端配图预取（原 race_map/boss_img 预取块的唯一消费方
    # _overview_panel_html/_banner 已删除）。
    return {
        "races": races, "bosses": bosses, "cts": cts, "odysseys": odysseys,
        "rush": rush, "socials": socials, "collectables": collectables, "now": now,
        "stale_note": nkapi._stale_warn(nkapi.URL_RACES, nkapi.URL_BOSSES, nkapi.URL_CT, nkapi.URL_ODYSSEY, nkapi.URL_EVENTS),
    }


async def collect_leaderboard(kind: str, variant: str, rows: int) -> dict:
    """拉取并整理一个排行榜；无可展示活动时返回 {"empty": 文案}。"""
    now = util.bucket_now()
    scoring = ""
    img = ""
    touched: set[str] = set()  # 本次实际请求过的榜单页 URL（供 stale 判定覆盖第 2 页起）
    if kind == "race":
        races = await nkapi.fetch_body(nkapi.URL_RACES)
        ev = util.pick_active(races, now) or util.fallback_latest(races)
        if not ev:
            return {"empty": "当前没有竞赛活动"}
        lb_url = ev.get("leaderboard")
        if not lb_url:
            return {"empty": "该活动暂无排行榜"}
        entries = await nkapi.fetch_leaderboard_paginated(lb_url, rows, touched)
        head = f"竞赛「{(ev.get('name') or '').strip()}」排行榜（最快用时，越短越好）"
        scoring = "GameTime"
        stale_urls = (nkapi.URL_RACES, *touched)
        meta_url = ev.get("metadata")
        meta = await _safe(nkapi.fetch_body(meta_url), "race_meta") if meta_url else None
        if meta and meta.get("mapURL"):
            img = await _safe(assets._asset_data_url(meta["mapURL"]), "race_map") or ""
    elif kind == "boss":
        bosses = await nkapi.fetch_body(nkapi.URL_BOSSES)
        ev = util.pick_active(bosses, now) or util.fallback_latest(bosses)
        if not ev:
            return {"empty": "当前没有 Boss 活动"}
        elite = variant == "elite"
        url_key = "leaderboard_elite_players_1" if elite else "leaderboard_standard_players_1"
        url = ev.get(url_key)
        if not url:
            return {"empty": "该活动暂无此模式的排行榜"}
        entries = await nkapi.fetch_leaderboard_paginated(url, rows, touched)
        label = "精英" if elite else "标准"
        scoring = str(ev.get("eliteScoringType" if elite else "normalScoringType") or "")
        mode_cn = i18n.SCORING_CN.get(scoring, scoring or "?")
        head = f"Boss「{(ev.get('name') or '').strip()}」{label}排行榜（{mode_cn}）"
        stale_urls = (nkapi.URL_BOSSES, *touched)
        if ev.get("bossTypeURL"):
            img = await _safe(assets._asset_data_url(ev["bossTypeURL"]), "boss_img") or ""
    elif kind == "rush":
        # Boss Rush 暂无公开排行榜接口（events 仅提供摘要），返回提示
        try:
            events = await nkapi.fetch_body(nkapi.URL_EVENTS)
            rush_list = [e for e in events if isinstance(e, dict) and e.get("type") == "bossRush"]
        except Exception:
            rush_list = []
        ev = util.pick_active(rush_list, now) or util.fallback_latest(rush_list)
        if not ev:
            return {"empty": "当前没有 Boss Rush 活动"}
        return {"empty": "Boss Rush 排行榜暂未开放（NK 仅在 /btd6/events 提供摘要，详细榜单待开放）"}
    else:  # ct
        cts = await nkapi.fetch_body(nkapi.URL_CT)
        ev = util.pick_active(cts, now) or util.fallback_latest(cts)
        if not ev:
            return {"empty": "当前没有争夺领土活动"}
        team = variant == "team"
        url_key = "leaderboard_team" if team else "leaderboard_player"
        url = ev.get(url_key)
        if not url:
            return {"empty": "该活动暂无此榜的排行榜"}
        entries = await nkapi.fetch_leaderboard_paginated(url, rows, touched)
        label = "战队" if team else "个人"
        head = f"争夺领土 {label}排行榜（领土积分，越高越好）"
        stale_urls = (nkapi.URL_CT, *touched)

    top = list(entries or [])[:rows]
    rows_out = [
        (i, str(e.get("displayName") or "?").strip(), util.fmt_score(scoring, e.get("score")))
        for i, e in enumerate(top, 1)
    ]
    return {
        "head": head, "status": util.event_status_line(ev, now), "entries": rows_out,
        "img": img, "stale_note": nkapi._stale_warn(*stale_urls),
    }


async def collect_rules(kind: str, variant: str) -> dict:
    now = util.bucket_now()
    if kind == "race":
        races = await nkapi.fetch_body(nkapi.URL_RACES)
        ev = util.pick_active(races, now) or util.fallback_latest(races)
        if not ev:
            return {"empty": "当前没有竞赛活动"}
        meta_url = ev.get("metadata")
        if not meta_url:
            return {"empty": "该活动暂无规则数据"}
        meta = await nkapi.fetch_body(meta_url)
        if not isinstance(meta, dict):
            return {"empty": "该活动暂无规则数据"}
        prefix = "竞赛"
        scoring_raw = "GameTime"
        scoring_cn = "最快用时"
        side_img = ""
        stale_urls = (nkapi.URL_RACES, meta_url)
    elif kind == "boss":
        bosses = await nkapi.fetch_body(nkapi.URL_BOSSES)
        ev = util.pick_active(bosses, now) or util.fallback_latest(bosses)
        if not ev:
            return {"empty": "当前没有 Boss 活动"}
        elite = variant != "standard"
        meta_url = ev.get("metadataElite") if elite else ev.get("metadataStandard")
        if not meta_url:
            return {"empty": "该活动暂无此模式的规则数据"}
        meta = await nkapi.fetch_body(meta_url)
        if not isinstance(meta, dict):
            return {"empty": "该活动暂无此模式的规则数据"}
        label = "精英" if elite else "标准"
        prefix = f"Boss·{label}"
        scoring_raw = str(ev.get("eliteScoringType" if elite else "normalScoringType") or "")
        scoring_cn = i18n.SCORING_CN.get(scoring_raw, scoring_raw or "?")
        side_img = await _safe(assets._asset_data_url(ev.get("bossTypeURL")), "boss_img") if ev.get("bossTypeURL") else ""
        stale_urls = (nkapi.URL_BOSSES, meta_url)
    elif kind == "ct":
        return {"empty": "争夺领土暂无通用规则数据，请使用 .btd6活动 或 .btd6排行 领土 查询"}
    else:
        return {"empty": "暂不支持该活动类型的规则查询"}
    map_img = await _safe(assets._asset_data_url(meta.get("mapURL")), "map_img") if meta.get("mapURL") else ""
    return {
        "prefix": prefix, "meta": meta, "map_img": map_img or "",
        "side_img": side_img or "", "scoring_raw": scoring_raw,
        "scoring_cn": scoring_cn, "ev": ev,
        "stale_note": nkapi._stale_warn(*stale_urls),
    }


# ---------------- 自制地图 ----------------

MAP_FILTERS = {
    "最新": "newest", "newest": "newest",
    "热门": "trending", "trending": "trending",
    "点赞": "mostLiked", "mostliked": "mostLiked", "mostLiked": "mostLiked",
}
FILTER_LABEL = {"newest": "最新", "trending": "热门", "mostLiked": "最多点赞"}


async def collect_maps(filt: str, rows: int) -> dict:
    items = await nkapi.fetch_body(nkapi.URL_MAP_FILTER.format(filt))
    if not isinstance(items, list):  # 非 list 响应按空数据处理，走既有空态文案
        items = []
    top = list(items or [])[:rows]

    async def detail(m: dict) -> tuple[str, int, int]:
        meta = await _safe(nkapi.fetch_body(m["metadata"]), "map_meta") if m.get("metadata") else None
        if not meta:
            return "", 0, 0
        img = await _safe(assets._asset_data_url(meta.get("mapURL")), "map_img") if meta.get("mapURL") else ""
        return img or "", int(meta.get("plays") or 0), int(meta.get("upvotes") or 0)

    details = await asyncio.gather(*(detail(m) for m in top)) if top else []
    entries = [
        (i, str(m.get("name") or "?").strip(), util.fmt_date(m.get("createdAt")),
         img, plays, upvotes)
        for i, (m, (img, plays, upvotes)) in enumerate(zip(top, details, strict=True), 1)
    ]
    return {"label": FILTER_LABEL[filt], "entries": entries,
            "stale_note": nkapi._stale_warn(nkapi.URL_MAP_FILTER.format(filt))}


def _daily_prefix(label: str, advanced: bool) -> str:
    """'Standard 2936: Shadow's Challenge' → '每日标准·第2936期'。"""
    issue = str(label or "").split(":")[0].replace("Advanced", "").replace("Standard", "").strip()
    kind = "每日高级" if advanced else "每日标准"
    return f"{kind}·第{issue}期" if issue.isdigit() else kind


async def _challenge_map_img(meta: dict, tag: str) -> str:
    """挑战类卡片地图图：优先开放 API 的 mapURL（按钮图），缺失/下载失败回退本地素材。"""
    raw_map_url = str(meta.get("mapURL") or "").strip()
    map_img = ""
    if raw_map_url:
        map_img = await _safe(assets._asset_data_url(raw_map_url), tag) or ""
    if not map_img:
        fallback_map = str(meta.get("map") or "").strip()
        if fallback_map:
            map_img = await _safe(assets._game_asset_data_url(f"MapSelect{fallback_map}Button.webp"), tag + "_fallback") or ""
            if not map_img:
                map_img = await _safe(assets._asset_data_url(f"MapSelect{fallback_map}Button.png"), tag + "_fallback2") or ""
    return map_img or ""


async def collect_daily(advanced: bool) -> dict:
    items = await nkapi.fetch_body(nkapi.URL_DAILY)
    if not isinstance(items, list):  # 非 list 响应按空数据处理，走既有失败文案
        items = []
    want = "Advanced" if advanced else "Standard"
    ev = next((x for x in items if str(x.get("name") or "").startswith(want)), None)
    if not ev:
        return {"empty": "暂无每日挑战数据"}
    meta = await nkapi.fetch_body(ev["metadata"])
    return {
        "prefix": _daily_prefix(str(ev.get("name") or ""), advanced),
        "meta": meta, "map_img": await _challenge_map_img(meta, "daily_map"),
        "side_img": "", "scoring_cn": "固定种子",
        "stale_note": nkapi._stale_warn(nkapi.URL_DAILY, ev.get("metadata") or ""),
    }


def _coop_pick(items: list, now_ms: int) -> dict | None:
    """Coop 挑战选期（与 BTD6 API Explorer 口径一致）：name 以 coop 开头的条目里，
    只考虑 createdAt ≤ 当前的（未来排期的条目元数据未开放，信封返回 error），取最新一期。"""
    created = [x for x in items
               if isinstance(x, dict) and str(x.get("name") or "").startswith("coop")
               and int(x.get("createdAt") or 0) <= now_ms]
    if not created:
        return None
    return max(created, key=lambda x: int(x.get("createdAt") or 0))


async def collect_daily_coop() -> dict:
    """Co-op 挑战：与每日挑战共用 /btd6/challenges/filter/daily 接口（name 以 "coop - " 开头）。

    NK 不为 coop 提供起止时间与期号，前缀用"每日Coop"（rules_html 按"每日"前缀
    走每日系渲染：日历徽章 + 全塔总览网格）；展示名取 metadata.name（已无 coop 前缀）。
    """
    items = await nkapi.fetch_body(nkapi.URL_DAILY)
    if not isinstance(items, list):  # 非 list 响应按空数据处理，走既有失败文案
        items = []
    ev = _coop_pick(items, int(time.time() * 1000))
    if not ev:
        return {"empty": "暂无 Co-op 挑战数据"}
    meta = await nkapi.fetch_body(ev["metadata"])
    return {
        "prefix": "每日Coop", "meta": meta,
        "map_img": await _challenge_map_img(meta, "coop_map"),
        "side_img": "", "scoring_cn": "固定种子", "kind_label": "Co-op 挑战",
        "stale_note": nkapi._stale_warn(nkapi.URL_DAILY, ev.get("metadata") or ""),
    }


# 当前远征的完成奖杯数：游戏内活动页显示，开放 API 的 _rewards 未包含，逐期转录
_ODYSSEY_TROPHY = {"mt7bsc6c": 15}


async def collect_odyssey() -> dict:
    now = util.bucket_now()
    items = await nkapi.fetch_body(nkapi.URL_ODYSSEY)
    ev = util.pick_active(items, now) or util.pick_next(items, now) or util.fallback_latest(items)
    if not ev:
        return {"empty": "当前没有远征活动"}

    async def collect_diff(d: str) -> tuple[str, dict]:
        url = ev.get(f"metadata_{d}")
        meta = await _safe(nkapi.fetch_body(url)) if url else None
        if meta is not None:
            # 游戏内活动页的奖励含完成奖杯，开放 API 未返回——按期转录补齐
            trophy = _ODYSSEY_TROPHY.get(str(ev.get("id") or ""))
            if trophy:
                rewards = [r for r in (meta.get("_rewards") or [])
                           if not str(r).startswith("Trophy:")]
                rewards.append(f"Trophy:{trophy}")
                # 浅拷贝后再注入：meta 与 nkapi 缓存共享同一对象，原地改写会污染缓存
                meta = dict(meta)
                meta["_rewards"] = rewards
        maps = []
        maps_url = (meta or {}).get("maps")
        if maps_url:
            mres = await _safe(nkapi.fetch_body(maps_url))
            if isinstance(mres, dict):
                maps = mres.get("body") or []
            elif isinstance(mres, list):
                maps = mres

        async def map_entry(mp: dict) -> dict:
            map_url = mp.get("mapURL")
            source_img = await _safe(assets._asset_data_url(map_url)) if map_url else ""
            # PIL 缩放/编码是重 CPU 同步操作，放线程池避免卡住事件循环
            img = await asyncio.to_thread(assets._odyssey_thumbnail_data_url, map_url, source_img or "")
            # 保留逐岛规则字段，图片卡片需要用它显示回合、难度、模式和强化状态。
            return {
                "name": str(mp.get("name") or "?").strip(),
                "map": str(mp.get("map") or "").strip(),
                "img": img or "",
                "difficulty": str(mp.get("difficulty") or "").strip(),
                "mode": str(mp.get("mode") or "").strip(),
                "startingCash": int(mp.get("startingCash") or 0),
                "startRound": int(mp.get("startRound") or 0),
                "endRound": int(mp.get("endRound") or 0),
                "lives": int(mp.get("lives") or 0),
                "maxLives": int(mp.get("maxLives") or 0),
                "maxTowers": int(mp.get("maxTowers") or 0),
                "maxParagons": int(mp.get("maxParagons") or 0),
                "roundSets": mp.get("roundSets") or [],
                "_bloonModifiers": mp.get("_bloonModifiers") or {},
                "disableMK": bool(mp.get("disableMK")),
                "disablePowers": bool(mp.get("disablePowers")),
                "disableInstas": bool(mp.get("disableInstas")),
                "disableSelling": bool(mp.get("disableSelling")),
                "noContinues": bool(mp.get("noContinues")),
                "disableDoubleCash": bool(mp.get("disableDoubleCash")),
            }

        entries = await asyncio.gather(*(map_entry(mp) for mp in maps[:5]))
        return d, {"meta": meta, "maps": entries}

    collected = await asyncio.gather(*(collect_diff(d) for d, _label in i18n._ODYSSEY_DIFFS))
    return {"ev": ev, "diffs": dict(collected), "stale_note": nkapi._stale_warn(nkapi.URL_ODYSSEY)}


_PLAYER_ID_RE = re.compile(r"[0-9a-f]{40,}")
_OAK_RE = re.compile(r"oak_[0-9a-fA-F]{8,}")


def _extract_player_id(arg: str) -> str:
    m = _PLAYER_ID_RE.search(arg or "")
    return m.group(0) if m else ""


def _extract_oak(arg: str) -> str:
    m = _OAK_RE.search(arg or "")
    return m.group(0) if m else ""


async def collect_player(pid: str) -> dict:
    body = await nkapi.fetch_body(nkapi.URL_USERS + pid)
    if not isinstance(body, dict) or not body.get("displayName"):
        return {"empty": "未找到该玩家，请确认 ID 是否正确"}
    banner = await _safe(assets._asset_data_url(body.get("bannerURL")), "player_banner") if body.get("bannerURL") else ""
    avatar = await _safe(assets._asset_data_url(body.get("avatarURL")), "player_avatar") if body.get("avatarURL") else ""
    return {"p": body, "banner": banner or "", "avatar": avatar or "",
            "stale_note": nkapi._stale_warn(nkapi.URL_USERS + pid)}


async def collect_player_oak(oak: str) -> dict:
    """OAK 查询：公开档案 + 完整存档（网站 profile&stats 视角）。
    注意：OAK 只出现在发往 NK 的 URL 里，绝不进卡片/文本/日志。"""
    try:
        public = await nkapi.fetch_body(nkapi.URL_USERS + oak)
    except Exception:
        return {"empty": "OAK 无效或已过期，请在游戏内重新生成后再试"}
    if not isinstance(public, dict) or not public.get("displayName"):
        return {"empty": "OAK 无效或已过期，请在游戏内重新生成后再试"}
    try:
        save_body = await nkapi.fetch_body(nkapi.URL_SAVE + oak)
    except Exception:
        save_body = {}
    if not isinstance(save_body, dict):
        save_body = {}
    banner = await _safe(assets._asset_data_url(public.get("bannerURL")), "player_banner") if public.get("bannerURL") else ""
    avatar = await _safe(assets._asset_data_url(public.get("avatarURL")), "player_avatar") if public.get("avatarURL") else ""
    return {"p": public, "save": save_body, "banner": banner or "", "avatar": avatar or "",
            "stale_note": nkapi._stale_warn(nkapi.URL_USERS + oak)}


_SITE_DATA = None


def site_data() -> dict:
    """站点裁剪常量（随游戏版本更新，用 build_sitedata.py 重建）。"""
    global _SITE_DATA
    if _SITE_DATA is None:
        try:
            with open(os.path.join(os.path.dirname(__file__), "site_data.json"), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        try:
            data["rankTable"] = {int(k): int(v) for k, v in (data.get("rankTable") or {}).items()}
        except (AttributeError, TypeError, ValueError):
            data["rankTable"] = {}
        _SITE_DATA = data
    return _SITE_DATA


_UPGRADE_EXCLUDES = frozenset({
    "Camo Bananas", "Adaptive Workers", "Regrow Bananas",
    "Banana Intelligence Bureau", "Monkey Banker", "Banana Financier",
    "Mini Stormcaller", "High Impact", "Rapid Recharge", "Beacon of Legends",
    "Bewildering Storm", "Piercing Wind",
})
_BOSS_MEDAL_ORDER = ["Diamondback", "Blastapopoulos", "Phayze", "Dreadbloon",
                     "Vortex", "Lych", "Bloonarius"]
_MEDAL_TIERS9 = ["Bronze", "Silver", "DoubleSilver", "GoldSilver", "DoubleGold",
                 "GoldDiamond", "BlueDiamond", "RedDiamond", "BlackDiamond"]
_CT_LOCAL_TIERS = ["Bronze", "Silver", "DoubleGold", "GoldDiamond",
                   "BlueDiamond", "RedDiamond", "BlackDiamond"]
_CT_GLOBAL_TIERS = ["Bronze", "Silver", "DoubleSilver", "GoldSilver",
                    "DoubleGold", "GoldDiamond", "BlueDiamond"]
_SEASON_MEDALS = ["Champion", "Tier4", "Tier3", "Tier2", "Tier1",
                  "Ultimate1", "Ultimate2", "Ultimate3"]


def _hero_skins_total(save: dict, SD: dict) -> int:
    """复刻网站 generateHeroesSkinsUnlocked 的 totalSkins 口径。"""
    heroes = SD.get("heroes") or {}
    t = []
    for _hero, skins in heroes.items():
        t += [x for x in (skins or []) if x not in heroes]
    us = (save or {}).get("unlockedSkins") or {}
    for e in (SD.get("iapHeroSkins") or []) + (SD.get("hiddenHeroes") or []):
        if e in t and not us.get(e):
            t.remove(e)
    return len(t)


def _chimps_count(save: dict) -> int:
    """复刻网站 CHIMPS 口径：各图 Hard.single.Clicks 完成数 + coop Clicks 完成数。"""
    n = 0
    for m in ((save or {}).get("mapProgress") or {}).values():
        if not isinstance(m, dict):
            continue
        try:
            diffs = m.get("difficulty") or {}
            hard = diffs.get("Hard") or {}
            single = hard.get("single") or {}
            c = single.get("Clicks")
            if isinstance(c, dict) and c.get("completed"):
                n += 1
            for content in diffs.values():
                if not isinstance(content, dict):
                    continue
                coop = content.get("coop") or {}
                if not isinstance(coop, dict):
                    continue
                cc = coop.get("Clicks")
                if isinstance(cc, dict) and cc.get("completed"):
                    n += 1
                    break
        except AttributeError:
            continue
    return n


def _trophy_count(save: dict, SD: dict) -> int:
    """复刻网站 TrophyStore 口径（含 keyFixes 兼容）。"""
    owned = (save or {}).get("trophyStoreItems") or {}
    fixes = SD.get("trophyKeyFixes") or {}
    n = 0
    for e in SD.get("trophyCatalog") or []:
        if owned.get(e) == 1:
            n += 1
            continue
        f = fixes.get(e)
        if f and owned.get(f):
            n += 1
    return n


def _extras_unlocked(save: dict) -> dict:
    """复刻网站 generateExtrasUnlocked。"""
    save = save or {}
    claimed = save.get("achievementsClaimed") or []
    ex = {}
    if save.get("unlockedBigBloons") or "Big Bloons" in claimed:
        ex["Big Bloons"] = save.get("bigBloonsActive")
    if save.get("unlockedSmallBloons") or "Small Bloons" in claimed:
        ex["Small Bloons"] = save.get("smallBloonsActive")
    if save.get("seenBigTowers") or "Chunky Monkeys" in claimed:
        ex["Big Monkey Towers"] = save.get("bigTowersActive")
    if save.get("unlockedSmallTowers") or "GoldenTicket" in claimed:
        ex["Small Monkey Towers"] = save.get("smallTowersActive")
    if save.get("unlockedSmallBosses") or "25 to Life" in claimed:
        ex["Small Bosses"] = save.get("smallBossesActive")
    return ex


def _int0(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def profile_quick_stats(save: dict, public: dict) -> list:
    """网站 profile overview 左列 [(图标相对路径, 文案)]，含 0 跳过（复刻网站规则）。"""
    SD = site_data()
    save = save or {}
    public = public or {}
    stats = public.get("stats") or {}
    gameplay = public.get("gameplay") or {}
    rows = []

    def add(icon, text, head):
        if head == 0:
            return
        rows.append((icon, text))

    tw = save.get("unlockedTowers") or {}
    towers = sum(1 for v in tw.values() if v)
    towers_total = len(SD.get("towers") or []) or 26
    add("UI/MaxMonkeysIcon.webp", f"{towers}/{towers_total} 塔已解锁", towers)

    au = save.get("acquiredUpgrades") or {}
    ups = sum(1 for k, v in au.items() if v and k not in _UPGRADE_EXCLUDES
              and ("Paragon" not in k or k == "Sentry Paragon"))
    add("UI/UpgradeIcon.webp", f"{ups}/{15 * towers_total} 升级已解锁", ups)

    pg = sum(1 for k, v in au.items() if v and "Paragon" in k and k != "Sentry Paragon")
    pg_total = len(SD.get("paragons") or []) or 13
    add("UI/ParagonPip.webp", f"{pg}/{pg_total} 帕拉贡已解锁", pg)

    uh = save.get("unlockedHeroes") or {}
    heroes = sum(1 for k, v in uh.items() if v and k != "Sheriff")
    heroes_total = len(SD.get("heroes") or {}) or 18
    add("UI/AllHeroesIcon.webp", f"{heroes}/{heroes_total} 英雄已解锁", heroes)

    us = save.get("unlockedSkins") or {}
    hero_names = set((SD.get("heroes") or {}).keys())
    skins = sum(1 for k, v in us.items() if v and k not in hero_names and k != "Sheriff")
    add("UI/TopHatSprite.webp",
        f"{skins}/{_hero_skins_total(save, SD)} 英雄皮肤已解锁", skins)

    abil = stats.get("abilitiesActivatedByName") or {}
    abils = SD.get("abilities") or []
    n_abil = sum(1 for k in abil if k in abils)
    add("UI/RapidShotIcon.webp", f"{n_abil} 独特技能已使用", n_abil)

    ak = save.get("acquiredKnowledge") or {}
    know = sum(1 for v in ak.values() if v)
    know_total = int(SD.get("knowledgeTotal") or 134)
    add("UI/KnowledgeIcon.webp", f"{know}/{know_total} 知识点已解锁", know)

    mp = save.get("mapProgress") or {}
    n_maps = sum(1 for v in mp.values() if v)
    add("UI/StartRoundIconSmall.webp", f"{n_maps} 地图已游玩", n_maps)

    n_chimps = _chimps_count(save)
    add("MedalIcon/MedalImpoppableRuby.webp", f"{n_chimps} CHIMPS 奖章", n_chimps)

    pw = save.get("powers") or {}
    n_powers = sum(int(v.get("quantity") or 0) if isinstance(v, dict) else 0
                   for v in pw.values())
    add("UI/PowerContainer.webp", f"{n_powers} 能量已收集", n_powers)

    pwp = save.get("powersPro") or {}
    n_pro = sum(1 for v in pwp.values()
                if isinstance(v, dict) and int(v.get("unlockedTier") or 0) > 0)
    add("UI/PowersProContainer.webp", f"{n_pro} 专业能量已解锁", n_pro)

    it_ = save.get("instaTowers") or {}
    avail = 0
    for entries in it_.values():
        if isinstance(entries, dict):
            for x in entries.values():
                try:
                    avail += int(x)
                except (TypeError, ValueError):
                    pass
    n_insta = avail + _int0(gameplay.get("instaMonkeysUsed"))
    add("UI/InstaIcon.webp", f"{n_insta} 速生猴已收集", n_insta)

    ach_total = int(SD.get("achievementsTotal") or 162)
    n_ach = len(save.get("achievementsClaimed") or [])
    add("AchievementIcon/AchievementsIcon.webp", f"{n_ach}/{ach_total} 成就已达成", n_ach)

    n_ts = _trophy_count(save, SD)
    add("UI/LimitedRunIcon.webp", f"{n_ts} 奖杯商店物品已收集", n_ts)

    n_q = sum(1 for q in (save.get("quests") or [])
              if isinstance(q, dict) and q.get("complete"))
    add("UI/QuestIcon.webp", f"{n_q} 任务完成", n_q)

    n_ex = len(_extras_unlocked(save))
    add("UI/SmallBloonsModeIcon.webp", f"{n_ex} 额外内容已解锁", n_ex)
    return rows


def profile_medals(public: dict) -> list:
    """网站 currency&medals 奖章格 [(图标相对路径, 数量)]，去零，保持网站顺序。"""
    public = public or {}
    gameplay = public.get("gameplay") or {}
    mm = (site_data().get("medalMap") or {})
    out = []

    def add(icon, v):
        try:
            n = int(v or 0)
        except (TypeError, ValueError):
            n = 0
        if n:
            out.append((icon, n))

    single = public.get("_medalsSinglePlayer") or {}
    multi = public.get("_medalsMultiplayer") or {}
    for mode, medal in mm.items():
        add(f"MedalIcon/Medal{medal}.webp", single.get(mode))
    for mode, medal in mm.items():
        add(f"MedalIcon/MedalCoop{medal}.webp", multi.get(mode))
    bn, be = public.get("bossBadgesNormal") or {}, public.get("bossBadgesElite") or {}
    for b in _BOSS_MEDAL_ORDER:
        add(f"MedalIcon/{b}EliteBadge.webp", be.get(b))
        add(f"MedalIcon/{b}Badge.webp", bn.get(b))
    race = public.get("_medalsRace") or {}
    for t in _MEDAL_TIERS9:
        add(f"MedalIcon/MedalEvent{t}Medal.webp", race.get(t))
    add("MedalIcon/OdysseyStarIcon.webp", gameplay.get("totalOdysseyStars"))
    bev, ebev = public.get("_medalsBoss") or {}, public.get("_medalsBossElite") or {}
    for t in _MEDAL_TIERS9:
        add(f"MedalIcon/BossMedalEvent{t}Medal.webp", bev.get(t))
    for t in _MEDAL_TIERS9:
        add(f"MedalIcon/EliteBossMedalEvent{t}Medal.webp", ebev.get(t))
    ctl, ctg = public.get("_medalsCTLocal") or {}, public.get("_medalsCTGlobal") or {}
    for t in _CT_LOCAL_TIERS:
        add(f"MedalIcon/CtLocalPlayer{t}Medal.webp", ctl.get(t))
    for t in _CT_GLOBAL_TIERS:
        add(f"MedalIcon/CtGlobalPlayer{t}Medal.webp", ctg.get(t))
    seasons = public.get("seasonBadges") or {}
    for s in _SEASON_MEDALS:
        entry = seasons.get("Season" + s) or {}
        add(f"MedalIcon/SocialSeasons{s}Medal.webp",
            entry.get("count") if isinstance(entry, dict) else entry)
    return out


def profile_tops(public: dict) -> dict:
    """网站 overview Top 区：英雄/塔/帕拉贡/技能 Top 排行（已排序，未截断）。"""
    SD = site_data()
    public = public or {}
    stats = public.get("stats") or {}
    heroes_order = set((SD.get("heroes") or {}).keys())
    towers_order = set(SD.get("towers") or [])
    paragons_order = set(SD.get("paragons") or [])
    heroes = sorted(
        ((k, int(v)) for k, v in (public.get("heroesPlaced") or {}).items()
         if k in heroes_order and int(v or 0) > 0),
        key=lambda kv: kv[1], reverse=True)
    towers = sorted(
        ((k, int(v)) for k, v in (public.get("towersPlaced") or {}).items()
         if k in towers_order and int(v or 0) > 0),
        key=lambda kv: kv[1], reverse=True)
    paragons = sorted(
        ((k, int(v)) for k, v in (stats.get("paragonsPurchasedByName") or {}).items()
         if k in paragons_order and int(v or 0) > 0),
        key=lambda kv: kv[1], reverse=True)
    abil_full = SD.get("abilitiesFull") or {}
    merged = {}
    for k, v in (stats.get("abilitiesActivatedByName") or {}).items():
        try:
            n = int(v or 0)
        except (TypeError, ValueError):
            continue
        if n <= 0:
            continue
        info = abil_full.get(k)
        if not info or not info.get("icon"):
            continue
        old = info.get("oldId")
        if old:
            try:
                n += int((stats.get("abilitiesActivatedByName") or {}).get(old) or 0)
            except (TypeError, ValueError):
                pass
        merged[k] = (info.get("displayName") or k, info.get("icon"), n)
    abilities = sorted(merged.values(), key=lambda t: t[2], reverse=True)
    return {"heroes": heroes, "towers": towers, "paragons": paragons,
            "abilities": abilities}


def profile_rogue(save: dict) -> list:
    """网站 Rogue Legends Stats（无游荡数据返回空列表）。"""
    save = save or {}
    if "rogueLegends" not in save:
        return []
    rg = save.get("rogueLegends") or {}
    rgs = save.get("rogueLegendsStats") or {}
    wins = ((rgs.get("winsByTileType") or {}) if isinstance(rgs.get("winsByTileType"), dict) else {})

    def _g(d, k):
        try:
            return int((d or {}).get(k) or 0)
        except (TypeError, ValueError):
            return 0

    arts = save.get("rogueUnlockedStarterArtifacts") or {}
    return [
        ("地盘占领", _g(rg, "tilesCaptured")),
        ("战役胜利", _g(rg, "bossesDefeated")),
        ("普通神器", _g(rg, "commonArtifactsCollected")),
        ("稀有神器", _g(rg, "rareArtifactsCollected")),
        ("传说神器", _g(rg, "legendaryArtifactsCollected")),
        ("已提取神器", len(arts) if isinstance(arts, dict) else 0),
        ("气球遭遇胜利", _g(wins, "pathStandardGame")),
        ("小游戏胜利", _g(wins, "miniGame")),
        ("小首领胜利", _g(wins, "miniBoss")),
        ("普通增益", _g(rg, "commonBoostsCollected")),
        ("稀有增益", _g(rg, "rareBoostsCollected")),
        ("传说增益", _g(rg, "legendaryBoostsCollected")),
    ]


def profile_frontier(save: dict) -> list:
    """网站 Frontier Legends Stats（无边境数据返回空列表）。"""
    save = save or {}
    if "frontierLegends" not in save:
        return []
    fl = save.get("frontierLegends") or {}
    fls = save.get("frontierLegendsStats") or {}
    basic = (fls.get("basicStats") or {}) if isinstance(fls.get("basicStats"), dict) else {}
    spec = (basic.get("frontierSpecialBloons") or {}) if isinstance(basic.get("frontierSpecialBloons"), dict) else {}

    def _g(d, k):
        try:
            return int((d or {}).get(k) or 0)
        except (TypeError, ValueError):
            return 0

    try:
        stamina = int(float(fls.get("spendStaminaAcrossAll") or 0))
    except (TypeError, ValueError):
        stamina = 0
    return [
        ("快枪获胜", _g(fl, "winQuickdrawAcrossAll")),
        ("快枪连胜", _g(fl, "captureBountyBloonsInARow")),
        ("总移动距离", _g(fl, "walkTotalAcrossAll")),
        ("雇佣猴子总数", _g(fl, "hireTotalAcrossAll")),
        ("雇佣传奇猴子", _g(fl, "hireLegendsAcrossAll")),
        ("商店花费", _g(fl, "spendGoldInGeneralStore")),
        ("体力消耗", stamina),
        ("炸药气球击破", _g(spec, "DynamiteBloon")),
        ("光环气球击破", _g(spec, "AuraBloon")),
        ("头目气球击破", _g(spec, "RingleaderBloon")),
        ("玻璃气球击破", _g(spec, "GlassBloon")),
        ("复仇气球击破", _g(spec, "RetributionBloon")),
        ("钻石气球击破", _g(spec, "DiamondBloon")),
    ]


def profile_rank_info(save: dict):
    """复刻网站 generateRankInfo：(等级, 本级经验, 本级目标)，满级后两项为 None。"""
    xp = _int0((save or {}).get("xp"))
    if xp >= 180000000:
        return (155, None, None)
    table = (site_data().get("rankTable") or {})
    prev = 0
    for a in sorted(table):
        if xp < table[a]:
            return (a - 1, xp - prev, table[a] - prev)
        prev = table[a]
    return (155, None, None)


def profile_veteran_info(save: dict):
    """复刻网站 generateVeteranRankInfo：每级固定 2000 万。(等级, 本级经验, 本级目标)"""
    a = _int0((save or {}).get("veteranXp"))
    rank = 1
    while a >= 20000000:
        a -= 20000000
        rank += 1
    return (rank, a, 20000000)


async def fetch_leaderboard_page(start_url: str, page: int) -> list:
    """拉取指定页的排行榜（单页，不做分页累积）。"""
    url = nkapi.lb_page_url(start_url, page)
    body = await nkapi.fetch_body(url)
    return body if isinstance(body, list) else []


async def collect_leaderboard_page(kind: str, variant: str, page: int) -> dict:
    """拉取并整理指定页的排行榜；页码越界返回 empty。"""
    if kind == "rush":
        # Boss Rush 暂无公开排行榜接口，显式返回空榜，避免落入 CT 分支取到错误数据
        return {"empty": "Boss Rush 排行榜暂未开放（NK 仅在 /btd6/events 提供摘要，详细榜单待开放）"}
    now = util.bucket_now()
    scoring = ""
    img = ""
    page = max(1, min(page, nkapi.LB_MAX_PAGE))
    size = nkapi.lb_page_size(kind)
    if kind == "race":
        races = await nkapi.fetch_body(nkapi.URL_RACES)
        ev = util.pick_active(races, now) or util.fallback_latest(races)
        if not ev:
            return {"empty": "当前没有竞赛活动"}
        start_url = ev.get("leaderboard") or ""
        if not start_url:
            return {"empty": "该活动暂无排行榜"}
        page_url = nkapi.lb_page_url(start_url, page)
        entries = await fetch_leaderboard_page(start_url, page)
        head = f"竞赛「{(ev.get('name') or '').strip()}」排行榜 第{page}页（{size}人/页）"
        scoring = "GameTime"
        stale_urls = (nkapi.URL_RACES, page_url)
        meta = await _safe(nkapi.fetch_body(ev["metadata"]), "race_meta") if ev.get("metadata") else None
        if meta and meta.get("mapURL"):
            img = await _safe(assets._asset_data_url(meta["mapURL"]), "race_map") or ""
    elif kind == "boss":
        bosses = await nkapi.fetch_body(nkapi.URL_BOSSES)
        ev = util.pick_active(bosses, now) or util.fallback_latest(bosses)
        if not ev:
            return {"empty": "当前没有 Boss 活动"}
        elite = variant == "elite"
        url_key = "leaderboard_elite_players_1" if elite else "leaderboard_standard_players_1"
        start_url = ev.get(url_key) or ""
        if not start_url:
            return {"empty": "该活动暂无此模式的排行榜"}
        page_url = nkapi.lb_page_url(start_url, page)
        entries = await fetch_leaderboard_page(start_url, page)
        label = "精英" if elite else "标准"
        scoring = str(ev.get("eliteScoringType" if elite else "normalScoringType") or "")
        mode_cn = i18n.SCORING_CN.get(scoring, scoring or "?")
        head = f"Boss「{(ev.get('name') or '').strip()}」{label}排行榜 第{page}页（{size}人/页，{mode_cn}）"
        stale_urls = (nkapi.URL_BOSSES, page_url)
        if ev.get("bossTypeURL"):
            img = await _safe(assets._asset_data_url(ev["bossTypeURL"]), "boss_img") or ""
    else:  # ct
        cts = await nkapi.fetch_body(nkapi.URL_CT)
        ev = util.pick_active(cts, now) or util.fallback_latest(cts)
        if not ev:
            return {"empty": "当前没有争夺领土活动"}
        team = variant == "team"
        url_key = "leaderboard_team" if team else "leaderboard_player"
        start_url = ev.get(url_key) or ""
        if not start_url:
            return {"empty": "该活动暂无此榜的排行榜"}
        page_url = nkapi.lb_page_url(start_url, page)
        entries = await fetch_leaderboard_page(start_url, page)
        label = "战队" if team else "个人"
        head = f"争夺领土 {label}排行榜 第{page}页（{size}人/页）"
        stale_urls = (nkapi.URL_CT, page_url)
    if not entries:
        return {"empty": f"第{page}页暂无数据（排行榜可能不足 {(page-1)*size+1} 人）"}
    start_rank = (page - 1) * size + 1
    rows_out = [
        (start_rank + i, str(e.get("displayName") or "?").strip(), util.fmt_score(scoring, e.get("score")))
        for i, e in enumerate(entries)
    ]
    return {
        "head": head, "status": util.event_status_line(ev, now), "entries": rows_out,
        "img": img, "page": page, "stale_note": nkapi._stale_warn(*stale_urls),
    }


async def fetch_rank_entry(kind: str, variant: str, rank: int) -> dict | None:
    """取指定排名的单条排行榜记录（用于玩家档案查询）。"""
    if kind == "rush":
        # Boss Rush 暂无公开排行榜接口，显式返回空结果，避免落入 CT 分支取到错误数据
        return None
    size = nkapi.lb_page_size(kind)
    if (rank - 1) // size + 1 > nkapi.LB_MAX_PAGE:
        # 超出可拉取页数：该名次取不到，返回 None 由调用方报"未找到"，
        # 避免 clamp 后取到错误名次的玩家（张冠李戴）
        return None
    page = (rank - 1) // size + 1
    idx = (rank - 1) % size
    now = util.bucket_now()
    start_url = ""
    if kind == "race":
        races = await nkapi.fetch_body(nkapi.URL_RACES)
        ev = util.pick_active(races, now) or util.fallback_latest(races)
        if not ev:
            return None
        start_url = ev.get("leaderboard") or ""
    elif kind == "boss":
        bosses = await nkapi.fetch_body(nkapi.URL_BOSSES)
        ev = util.pick_active(bosses, now) or util.fallback_latest(bosses)
        if not ev:
            return None
        elite = variant == "elite"
        url_key = "leaderboard_elite_players_1" if elite else "leaderboard_standard_players_1"
        start_url = ev.get(url_key) or ""
    else:  # ct
        cts = await nkapi.fetch_body(nkapi.URL_CT)
        ev = util.pick_active(cts, now) or util.fallback_latest(cts)
        if not ev:
            return None
        team = variant == "team"
        url_key = "leaderboard_team" if team else "leaderboard_player"
        start_url = ev.get(url_key) or ""
    if not start_url:
        return None
    # 直接拉取按 page = (rank-1)//size + 1 计算出的目标页（不从第 1 页逐页串行拉到目标页）。
    # 目标页缺行/为空说明榜单确实不足该名次（返回 None 由调用方报"未找到/超出可查范围"）；
    # 不做相邻页兜底：不同页同一页内序号对应的是另一个名次，兜底会展示错误玩家。
    entries = await fetch_leaderboard_page(start_url, page)
    if 0 <= idx < len(entries):
        return entries[idx]
    return None


def _rush_stage_rewards_text(reward_str: str) -> str:
    """把 StageRewards 配置串（K:V#K:V）转成中文摘要。"""
    parts = []
    for chunk in (reward_str or "").split("#"):
        if ":" not in chunk:
            continue
        k, v = chunk.split(":", 1)
        parts.append(f"{i18n._REWARD_LABELS.get(k, k)} {v}")
    return " · ".join(parts)


def _rush_rewards_pairs(reward_str: str) -> list:
    """把 StageRewards 配置串解析为 [(中文标签, 数值)]（不含收集事件）。"""
    pairs = []
    for chunk in (reward_str or "").split("#"):
        if ":" not in chunk:
            continue
        k, v = chunk.split(":", 1)
        if k == "CollectionEvent":
            continue  # 用户要求去掉收集事件
        pairs.append((i18n._REWARD_LABELS.get(k, k), v))
    return pairs


async def collect_rush() -> dict:
    """拉 /btd6/events 取 bossRush 摘要，用 rushgen 从活动种子生成逐阶段配置。

    NK 开放 API 不提供 Boss Rush 明细（/btd6/bossRush 为 404），逐阶段数据由
    rushgen（BTD6 API Explorer Lucy 逆向算法的 Python 移植）从活动 ID 确定性生成。
    """
    now = util.bucket_now()
    events = await nkapi.fetch_body(nkapi.URL_EVENTS)
    rush_list = [e for e in events if isinstance(e, dict) and e.get("type") == "bossRush"]
    ev = util.pick_active(rush_list, now) or util.pick_next(rush_list, now) or util.fallback_latest(rush_list)
    if not ev:
        return {"empty": "当前没有 Boss Rush 活动"}
    br = rushgen.load_constants()["bossRush"]
    scores = br.get("StageScores") or []
    rewards = br.get("StageRewards") or []
    gen = rushgen.generate_boss_rush(str(ev.get("id") or ""))

    def _stage_assets(boss_name: str, map_id: str) -> tuple[str, str]:
        """本地素材读取（同步磁盘 IO）打包到线程侧执行，避免卡住事件循环。"""
        boss_url = assets._game_asset_data_url(f"{boss_name}Portrait.webp")
        if not boss_url:
            art = assets._RUSH_BOSS_ART.get(boss_name)
            boss_url = (assets._ui_asset_data_url(art) if art else "") or assets._ui_asset_data_url("boss-event.png") or ""
        return boss_url, assets._game_asset_data_url(f"MapSelect{map_id}Button.webp")

    islands = []
    for st in gen["stages"]:
        idx = st["stage"] - 1
        boss_name = st["boss"]
        boss_url, map_img = await asyncio.to_thread(_stage_assets, boss_name, st["map"])
        islands.append({
            "stage": st["stage"],
            "name": f"Island {st['stage']} · {boss_name}",
            "map": boss_name,
            "map_name": st["map"],
            "map_img": map_img,
            "boss": boss_name,
            "kills": scores[idx] if idx < len(scores) else 0,
            "rewards": _rush_rewards_pairs(rewards[idx] if idx < len(rewards) else ""),
            "reward_text": _rush_stage_rewards_text(rewards[idx] if idx < len(rewards) else ""),
            "towers": st["towers"],
            "removed": st["removed"],
            "relics": st["relics"],
            "new_relic": st["newRelic"],
            "img": boss_url,
        })
    hero = gen.get("hero")
    return {"ev": ev,
            "hero": "ChosenPrimaryHero" if hero == "ChosenPrimaryHero" else (hero or ""),
            "stale_note": nkapi._stale_warn(nkapi.URL_EVENTS),
            "diffs": {"default": {"meta": {"isExtreme": False}, "maps": islands}}}


async def collect_collectevent(now_ms: int | None = None) -> dict:
    """收集活动 Featured Insta 计划表。

    NK 开放 API 只提供 collectableEvent 的起止时间；每 8 小时一轮的 Featured
    Insta 名单由 instagen 从活动 ID 种子确定性生成（与游戏内菜单一致）。
    优先取进行中的场次，其次下一场，最后最近一场。
    """
    now = now_ms if now_ms is not None else util.bucket_now()
    events = await nkapi.fetch_body(nkapi.URL_EVENTS)
    cands = [e for e in events if isinstance(e, dict) and e.get("type") == "collectableEvent"]
    ev = util.pick_active(cands, now) or util.pick_next(cands, now) or util.fallback_latest(cands)
    if not ev:
        return {"empty": "当前没有收集活动（约两个月一轮，开始前约一周上线计划表）"}
    start = int(ev.get("start") or 0)
    end = int(ev.get("end") or 0)
    gen = instagen.generate_collection_schedule(str(ev.get("id") or ""), start, end)
    # 当前轮序（活动未开始时为 -1，渲染层从第 0 轮列起）
    cur = (now - start) // instagen.ROTATION_MS if now >= start else -1
    return {"ev": ev, "gen": gen, "now": now, "cur": cur,
            "stale_note": nkapi._stale_warn(nkapi.URL_EVENTS)}


async def collect_ct(now_ms: int | None = None) -> dict:
    """争夺领土：NK 活动摘要 + tiles 布局 + 社区数据集（期数/逐格地图/模式/遗物）。

    NK 开放 API 的 /btd6/ct 与 /tiles 只有格子 id/类型/模式，无地图与期数；
    地图布局取自 btd6-ct-map 社区数据集（BTD6 API Explorer CT 页同源，
    event-seeds.json 的下标即期数）。数据集缺失时降级为纯 NK 数据（无地图页）。
    """
    now = now_ms if now_ms is not None else util.bucket_now()
    cts = await nkapi.fetch_body(nkapi.URL_CT)
    ev = util.pick_active(cts, now) or util.pick_next(cts, now) or util.fallback_latest(cts)
    if not ev:
        return {"empty": "当前没有争夺领土活动"}
    ev_id = str(ev.get("id") or "")
    seeds = await _safe(nkapi.fetch_json_raw(nkapi.URL_CT_EVENT_SEEDS), "ct_seeds")
    # 期号 = 事件在 seeds 列表中的下标；列表第 0 位固定为 null 占位（真实事件从
    # 下标 1 开始），因此 number=0 与"未找到"不会冲突——若数据集去掉该占位需同步调整
    number = 0
    if isinstance(seeds, list):
        for idx, seed in enumerate(seeds):
            if seed == ev_id:
                number = idx
                break
    nk_tiles: list = []
    if ev.get("tiles"):
        body = await _safe(nkapi.fetch_body(ev["tiles"]), "ct_tiles")
        if isinstance(body, dict):
            nk_tiles = body.get("tiles") or []
        elif isinstance(body, list):
            nk_tiles = body
    ct_tiles: dict = {}
    daily_powers: list = []
    event_relics: list = []
    if number:
        base = nkapi.URL_CT_EVENT_DATA.format(number)
        layout = await _safe(nkapi.fetch_json_raw(f"{base}/tiles.json"), "ct_layout")
        if isinstance(layout, dict):
            ct_tiles = layout
        powers = await _safe(nkapi.fetch_json_raw(f"{base}/daily_powers.json"), "ct_powers")
        if isinstance(powers, list):
            daily_powers = powers
        relics = await _safe(nkapi.fetch_json_raw(f"{base}/event_relics.json"), "ct_relics")
        if isinstance(relics, list):
            event_relics = relics
    return {"ev": ev, "number": number, "nk_tiles": nk_tiles, "ct_tiles": ct_tiles,
            "daily_powers": daily_powers, "event_relics": event_relics,
            "now": now, "stale_note": nkapi._stale_warn(nkapi.URL_CT)}
