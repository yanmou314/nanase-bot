"""采集层：组合 nkapi 取数 + assets 素材，产出各卡片/文本共用的数据 dict。"""
import asyncio
import logging
import re

from . import assets, i18n, nkapi, rushgen, util

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
    races, bosses, cts, odysseys, rush_events = await asyncio.gather(
        nkapi.fetch_body(nkapi.URL_RACES), nkapi.fetch_body(nkapi.URL_BOSSES), nkapi.fetch_body(nkapi.URL_CT),
        _safe(nkapi.fetch_body(nkapi.URL_ODYSSEY)),
        _safe(nkapi.fetch_body(nkapi.URL_EVENTS)),
    )
    # odysseys 可能为 None（网络失败）或非列表
    if not isinstance(odysseys, list):
        odysseys = []
    # rush via events type bossRush
    rush = []
    if isinstance(rush_events, list):
        # nkapi.fetch_body 恒返回解包后的 body 列表，dict 信封分支不可达
        rush = [e for e in rush_events if isinstance(e, dict) and e.get("type") == "bossRush"]
    # 三段式总览（文本/卡片两路）只消费活动列表本身：配图图标全部来自本地 UI 素材，
    # 无逐活动远端配图预取（原 race_map/boss_img 预取块的唯一消费方
    # _overview_panel_html/_banner 已删除）。
    return {
        "races": races, "bosses": bosses, "cts": cts, "odysseys": odysseys, "rush": rush, "now": now,
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
        meta = await nkapi.fetch_body(ev["metadata"])
        prefix = "竞赛"
        scoring_raw = "GameTime"
        scoring_cn = "最快用时"
        side_img = ""
        stale_urls = (nkapi.URL_RACES, ev["metadata"])
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


async def collect_daily(advanced: bool) -> dict:
    items = await nkapi.fetch_body(nkapi.URL_DAILY)
    if not isinstance(items, list):  # 非 list 响应按空数据处理，走既有失败文案
        items = []
    want = "Advanced" if advanced else "Standard"
    ev = next((x for x in items if str(x.get("name") or "").startswith(want)), None)
    if not ev:
        return {"empty": "暂无每日挑战数据"}
    meta = await nkapi.fetch_body(ev["metadata"])
    map_img = await _safe(assets._asset_data_url(meta.get("mapURL")), "daily_map") if meta.get("mapURL") else ""
    return {
        "prefix": _daily_prefix(str(ev.get("name") or ""), advanced),
        "meta": meta, "map_img": map_img or "", "side_img": "", "scoring_cn": "固定种子",
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


def _extract_player_id(arg: str) -> str:
    m = _PLAYER_ID_RE.search(arg or "")
    return m.group(0) if m else ""


async def collect_player(pid: str) -> dict:
    body = await nkapi.fetch_body(nkapi.URL_USERS + pid)
    if not isinstance(body, dict) or not body.get("displayName"):
        return {"empty": "未找到该玩家，请确认 ID 是否正确"}
    banner = await _safe(assets._asset_data_url(body.get("bannerURL")), "player_banner") if body.get("bannerURL") else ""
    avatar = await _safe(assets._asset_data_url(body.get("avatarURL")), "player_avatar") if body.get("avatarURL") else ""
    return {"p": body, "banner": banner or "", "avatar": avatar or "",
            "stale_note": nkapi._stale_warn(nkapi.URL_USERS + pid)}


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
