"""文本渲染层：总览/排行/规则/地图/远征/玩家/Boss Rush/收集活动/CT 的纯文本输出。"""
from collections import Counter
from datetime import datetime

from . import ctmap, i18n, instagen, util


# ---------------- 活动总览（文本） ----------------


def _single_event_text(ev: dict, kind: str, now_ms: int) -> list[str]:
    """单场活动的文本行（三段式总览的统一出口，race/boss/ct/odyssey/rush/social/collectable）。"""
    if kind == "race":
        total = int(ev.get("totalScores") or 0)
        return [
            f"🏁 每周竞赛「{(ev.get('name') or '').strip()}」",
            f"   {util.event_status_line(ev, now_ms)}",
            f"   👥 参与人数 {total:,}",
        ]
    if kind == "boss":
        name = (ev.get("name") or "").strip()
        title = f"👹 Boss 事件「{name}」"
        bt = i18n.boss_cn(ev.get("bossType"))
        if bt:
            title += f"（{bt}）"
        std = i18n.SCORING_CN.get(str(ev.get("normalScoringType") or ""), str(ev.get("normalScoringType") or ""))
        elite = i18n.SCORING_CN.get(str(ev.get("eliteScoringType") or ""), str(ev.get("eliteScoringType") or ""))
        n_std = int(ev.get("totalScores_standard") or 0)
        n_elite = int(ev.get("totalScores_elite") or 0)
        return [
            title,
            f"   {util.event_status_line(ev, now_ms)}",
            f"   📊 标准模式 {std} · 精英模式 {elite}",
            f"   👥 参与：标准 {n_std:,} · 精英 {n_elite:,}",
        ]
    if kind == "odyssey":
        name = (ev.get("name") or "").strip() or "远征"
        desc = (ev.get("description") or "").strip()
        lines = [
            f"🏰 远征「{name}」",
            f"   {util.event_status_line(ev, now_ms)}",
        ]
        if desc:
            # 远征描述可能较长，截断到 60 字避免刷屏
            short = desc[:60] + ("…" if len(desc) > 60 else "")
            lines.append(f"   📜 {short}")
        return lines
    if kind == "rush":
        # Boss Rush 仅在 /btd6/events 提供名称与起止时间摘要，无参与人数字段
        raw = (ev.get("name") or "").strip()
        title = i18n._EVENT_NAME_CN.get(raw) or (f"Boss Rush「{raw}」" if raw else "Boss Rush")
        return [
            f"⚔️ {title}",
            f"   {util.event_status_line(ev, now_ms)}",
        ]
    if kind == "ct":
        n_player = int(ev.get("totalScores_player") or 0)
        n_team = int(ev.get("totalScores_team") or 0)
        return [
            "🏰 争夺领土（CT）",
            f"   {util.event_status_line(ev, now_ms)}",
            f"   👥 参与：个人 {n_player:,} · 战队 {n_team:,}",
        ]
    if kind == "social":
        # 社交赛季（socialseason）通常没有参与人数/描述，以名称+时间为主
        name = (ev.get("name") or "").strip() or "社交赛季"
        return [
            f"🤝 社交赛季「{name}」",
            f"   {util.event_status_line(ev, now_ms)}",
        ]
    if kind == "collectable":
        # 收集活动（collectableEvent）通常有描述与奖励列表
        name = (ev.get("name") or "").strip() or "收集活动"
        desc = (ev.get("description") or "").strip()
        lines = [
            f"🎁 收集活动「{name}」",
            f"   {util.event_status_line(ev, now_ms)}",
        ]
        if desc:
            short = desc[:60] + ("…" if len(desc) > 60 else "")
            lines.append(f"   📜 {short}")
        return lines
    # 未知类型兜底：只展示名称与时间，避免误用其他活动类型的文案
    name = (ev.get("name") or "").strip() or "活动"
    return [
        f"🎈 {name}",
        f"   {util.event_status_line(ev, now_ms)}",
    ]


def build_overview(
    races: list, bosses: list, cts: list, now_ms: int,
    odysseys: list | None = None, rush: list | None = None,
    socials: list | None = None, collectables: list | None = None,
) -> str:
    """三段式总览：进行中 / 即将开始 / 已结束(最近 ENDED_SHOW 场)，从上到下排列。

    odysseys 为可选的远征列表，rush/socials/collectables 同理；未传时与旧版一致。
    """
    odysseys = odysseys or []
    rush = rush or []
    socials = socials or []
    collectables = collectables or []
    ongoing, upcoming, ended = util._classify_overview_events(
        races, bosses, cts, now_ms, odysseys, rush, socials, collectables
    )
    ended = ended[:util.ENDED_SHOW]
    parts = ["🎮 BTD6 当前活动", ""]
    # 进行中
    parts.append(f"🟢 正在进行 ({len(ongoing)})")
    if not ongoing:
        parts.append("  暂无")
    else:
        for ev, kind in ongoing:
            parts.extend(_single_event_text(ev, kind, now_ms))
            parts.append("")
        # 去掉最后多余的空行并补一个分隔
        if parts and parts[-1] == "":
            parts.pop()
    parts.append("")
    # 即将开始
    parts.append(f"🟡 即将开始 ({len(upcoming)})")
    if not upcoming:
        parts.append("  暂无")
    else:
        for ev, kind in upcoming:
            parts.extend(_single_event_text(ev, kind, now_ms))
            parts.append("")
        if parts and parts[-1] == "":
            parts.pop()
    parts.append("")
    # 已结束（最近 ENDED_SHOW 场）
    parts.append(f"⚪ 已结束 · 最近 {len(ended)} 场" if ended else "⚪ 已结束 · 最近 0 场")
    if not ended:
        parts.append("  暂无")
    else:
        for ev, kind in ended:
            parts.extend(_single_event_text(ev, kind, now_ms))
            parts.append("")
        if parts and parts[-1] == "":
            parts.pop()
    return "\n".join(parts)


def overview_text(data: dict) -> str:
    text = build_overview(
        data["races"], data["bosses"], data["cts"], data["now"],
        data.get("odysseys") or [],
        data.get("rush") or [],
        data.get("socials") or [],
        data.get("collectables") or [],
    )
    note = data.get("stale_note") or ""
    return f"{text}\n{note}" if note else text


MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def leaderboard_text(col: dict) -> str:
    if col.get("empty"):
        return col["empty"]
    lines = [col["head"], f"   {col['status']}", ""]
    if not col["entries"]:
        lines.append("（暂无上榜数据）")
    for i, name, score_txt in col["entries"]:
        prefix = MEDALS.get(i, f"{i}.")
        lines.append(f"{prefix} {name} — {score_txt}")
    note = col.get("stale_note") or ""
    if note:
        lines.extend(["", note])
    return "\n".join(lines)


# ---------------- 活动规则 ----------------


def _mult_txt(key: str, value) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return ""
    if abs(v - 1.0) < 1e-9:
        return ""
    return f"{key} ×{v:g}"


def bloon_mod_lines(mods: dict | None) -> list[str]:
    out = []
    speed_parts = [
        t for t in (
            _mult_txt("气球速度", (mods or {}).get("speedMultiplier")),
            _mult_txt("MOAB速度", (mods or {}).get("moabSpeedMultiplier")),
            _mult_txt("Boss速度", (mods or {}).get("bossSpeedMultiplier")),
            _mult_txt("再生速度", (mods or {}).get("regrowRateMultiplier")),
        ) if t
    ]
    if speed_parts:
        out.append("、".join(speed_parts))
    hm = (mods or {}).get("healthMultipliers") or {}
    hp_parts = [
        t for t in (
            _mult_txt("气球血量", hm.get("bloons")),
            _mult_txt("MOAB血量", hm.get("moabs")),
            _mult_txt("Boss血量", hm.get("boss")),
        ) if t
    ]
    if hp_parts:
        out.append("、".join(hp_parts))
    if mods and mods.get("allCamo"):
        out.append("全体隐身")
    if mods and mods.get("allRegen"):
        out.append("全体再生")
    return out


def _cap_items(items: list, limit: int = 8) -> str:
    shown = items[:limit]
    tail = f" …等{len(items)}项" if len(items) > limit else ""
    return "、".join(shown) + tail


def tower_limit_lines(towers: list) -> list[str]:
    """受限塔的文本行；max=0（整塔禁用）的猴子不显示，仅展示仍可用的限制（限购/路径/英雄）。"""
    limited, pathed, heroes = [], [], []
    for t in towers or []:
        raw = str(t.get("tower") or "").strip()
        if not raw or raw == "ChosenPrimaryHero":  # 内部占位符，非真实塔
            continue
        mx = t.get("max")
        if isinstance(mx, (int, float)) and mx == 0:
            continue  # 整塔禁用：直接不显示
        name = i18n.tower_cn(raw)
        blocked = {
            p: n for p in (1, 2, 3)
            if (n := int(t.get(f"path{p}NumBlockedTiers") or 0)) != 0
        }
        if bool(t.get("isHero")):
            if isinstance(mx, (int, float)) and mx > 0:  # 允许的英雄（如 max=99 表示仅此英雄）
                heroes.append(name)
            continue
        if isinstance(mx, (int, float)) and 0 < mx < 99:
            limited.append(f"{name}×{int(mx)}")
        if blocked:
            detail = "、".join(f"路{p}禁{n}层" for p, n in sorted(blocked.items()))
            pathed.append(f"{name}（{detail}）")
    lines = []
    if limited:
        lines.append(f"🔢 塔限购：{_cap_items(limited)}")
    if pathed:
        lines.append(f"🧱 路径限制：{_cap_items(pathed)}")
    if heroes:
        lines.append(f"🦸 英雄限定：{_cap_items(heroes)}")
    return lines


def _rules_lines(meta: dict, prefix: str) -> list[str]:
    diff = i18n.cn(meta.get("difficulty"), i18n.DIFFICULTY_CN)
    mode = i18n.cn(meta.get("mode"), i18n.MODE_CN)
    # 地图名经 MAP_CN 译为中文，查不到回退原始内部名（如 ThreeMinesAround）
    map_name = i18n.map_cn(str(meta.get("map") or "").strip()) or "?"
    cash = int(meta.get("startingCash") or 0)
    lives = int(meta.get("lives") or 0)
    rounds = f"{int(meta.get('startRound') or 0)}–{int(meta.get('endRound') or 0)}"
    max_towers = int(meta.get("maxTowers") or 0)
    max_paragons = int(meta.get("maxParagons") or 0)

    lines = [f"{prefix}「{(meta.get('name') or '').strip()}」规则"]
    lines.append(f"🗺 地图：{map_name}｜难度：{diff}" + (f"｜模式：{mode}" if mode else ""))
    lines.append(f"💰 初始资金 {cash:,}｜❤️ 生命 {lives:,}｜回合 {rounds}")
    towers_cap = "无限制" if max_towers >= 9999 else f"{max_towers:,}"
    paragon_part = "禁止 Paragon" if max_paragons == 0 else f"Paragon 上限 {max_paragons}"
    lines.append(f"🐒 塔位上限 {towers_cap}｜{paragon_part}")

    bans = [label for key, label in i18n.FLAG_LABELS if meta.get(key)]
    if bans:
        lines.append(f"🚫 禁用：{'、'.join(bans)}")
    mod_lines = bloon_mod_lines(meta.get("_bloonModifiers"))
    if mod_lines:
        lines.append(f"气球强化：{'；'.join(mod_lines)}")
    lines += tower_limit_lines(meta.get("_towers"))
    return lines


def format_rules(meta: dict, prefix: str) -> str:
    return "\n".join(_rules_lines(meta, prefix))


def rules_text(col: dict) -> str:
    if col.get("empty"):
        return col["empty"]
    text = format_rules(col["meta"], col["prefix"])
    note = col.get("stale_note") or ""
    return f"{text}\n{note}" if note else text


def maps_text(col: dict) -> str:
    lines = [f"自制地图 · {col['label']} Top{len(col['entries'])}", ""]
    if not col["entries"]:
        lines.append("（暂无地图数据）")
    for i, name, created, *_rest in col["entries"]:
        lines.append(f"{i}. {name}（{created}）")
    note = col.get("stale_note") or ""
    if note:
        lines.extend(["", note])
    return "\n".join(lines)


def _reward_txt(rewards: list) -> str:
    out = []
    for r in rewards or []:
        s = str(r)
        if s.startswith("MonkeyMoney:"):
            out.append(f"猴币×{s.split(':', 1)[1]}")
        elif s.startswith("InstaMonkey:"):
            out.append(f"即时猴·{i18n.tower_cn(s.split(':')[1])}")
        elif s.startswith("Power:"):
            out.append(f"力量·{s.split(':', 1)[1]}")
        else:
            out.append(s.replace(":", "·"))
    return "、".join(out) if out else "无"


def _odyssey_meta_lines(meta: dict | None) -> list[str]:
    if not meta:
        return ["（该难度数据缺失）"]
    powers = meta.get("_availablePowers") or []
    usable_powers = [p.get("power") for p in powers if isinstance(p, dict) and p.get("max")]
    towers = meta.get("_availableTowers") or []
    lines = [f"初始生命 {int(meta.get('startingHealth') or 0):,}"]
    if meta.get("isExtreme"):
        lines.append("极限模式")
    if towers:
        lines.append(f"可用塔 {len(towers)} 种")
    if powers:
        shown = "、".join(i18n._odyssey_power_name(str(x)) for x in usable_powers[:6])
        tail = f" 等{len(usable_powers)}种" if len(usable_powers) > 6 else ""
        lines.append(f"力量：{shown}{tail}")
    return lines


def odyssey_text(col: dict) -> str:
    if col.get("empty"):
        return col["empty"]
    ev, diffs = col["ev"], col["diffs"]
    state = util._STATE_TXT[util._state_of(ev, util.bucket_now())]
    lines = [
        "🏰 远征活动",
        f"{(ev.get('name') or '').strip()}（{state}）",
        util._fmt_range(ev),
        (ev.get("description") or "").strip(),
        "",
    ]
    for d, label in i18n._ODYSSEY_DIFFS:
        diff = diffs.get(d) or {}
        meta = diff.get("meta")
        lines.append(f"【{label}】")
        lines += [f"  {x}" for x in _odyssey_meta_lines(meta)]
        rewards = (meta or {}).get("_rewards") or []
        if rewards:
            lines.append(f"  {_reward_txt(rewards)}")
        maps = diff.get("maps") or []
        if maps:
            lines.append(f"  🗺 地图：{'、'.join(m['name'] for m in maps)}")
        lines.append("")
    note = col.get("stale_note") or ""
    if note:
        lines.append(note)
    return "\n".join(lines).rstrip()


def player_text(col: dict) -> str:
    if col.get("empty"):
        return col["empty"]
    p = col["p"]
    popped = p.get("bloonsPopped") or {}
    gp = p.get("gameplay") or {}
    vr = p.get("veteranRank") or 0
    lines = [
        # 纯文本路径：displayName 等 NK 字段由 MessageSegment.text 发送，无需 HTML 转义
        f"🐒 {str(p.get('displayName'))}",
        f"等级 {p.get('rank')}" + (f"（老兵 {vr}）" if vr else "")
        + f" · 粉丝 {util.fmt_cn_num(p.get('followers'))}",
        f"最高回合 {p.get('highestRound')} · CHIMPS {gp.get('highestRoundCHIMPS', '—')}"
        f" · 成就 {p.get('achievements')}",
        f"最常用猴：{i18n.tower_cn(str(p.get('mostExperiencedMonkey') or ''))}",
        "",
        " popped：",
        f"  总气球 {util.fmt_cn_num(popped.get('bloonsPopped'))} · Boss {util.fmt_cn_num(popped.get('bossesPopped'))}",
        f"  MOAB {util.fmt_cn_num(popped.get('moabsPopped'))} · ZOMG {util.fmt_cn_num(popped.get('zomgsPopped'))}"
        f" · 陶瓷 {util.fmt_cn_num(popped.get('ceramicsPopped'))}",
        f"  迷彩 {util.fmt_cn_num(popped.get('camosPopped'))} · 金气球 {util.fmt_cn_num(popped.get('goldenBloonsPopped'))}",
        "",
        f"局数 {util.fmt_cn_num(gp.get('gameCount'))} · 胜场 {util.fmt_cn_num(gp.get('gamesWon'))}"
        f" · 挑战完成 {util.fmt_cn_num(gp.get('challengesCompleted'))}",
        f"累计猴币 {util.fmt_cn_num(gp.get('cashEarned'))} · 奖杯 {util.fmt_cn_num(gp.get('totalTrophiesEarned'))}",
    ]
    note = col.get("stale_note") or ""
    if note:
        lines.extend(["", note])
    return "\n".join(lines)


def _rush_text(col: dict) -> str:
    """模仿 odyssey_text：文字版阶段路线。"""
    if col.get("empty"):
        return col["empty"]
    ev, diffs = col["ev"], col["diffs"]
    state = util._STATE_TXT[util._state_of(ev, util.bucket_now())]
    lines = [
        "🏝️ Boss Rush",
        f"{(ev.get('name') or '').strip() or 'Boss Rush'}（{state}）",
        util._fmt_range(ev),
        "",
    ]
    diff = diffs.get("default") or {}
    for mp in diff.get("maps") or []:
        towers = "、".join(i18n.tower_cn(t) for t in mp["towers"])
        lines.append(f"  第{mp['stage']}阶段 · {i18n.boss_cn(mp['boss'])} · 击杀 {mp['kills']}")
        lines.append(f"    地图：{mp['map_name']}")
        lines.append(f"    塔池：{towers}")
        lines.append(f"    奖励：{mp['reward_text']}")
        if mp.get("removed"):
            lines.append(f"    移除：{'、'.join(i18n.tower_cn(t) for t in mp['removed'])}")
    hero = col.get("hero") or ""
    lines.append("英雄：" + ("队长自选" if hero == "ChosenPrimaryHero" else i18n.hero_cn(hero)))
    note = col.get("stale_note") or ""
    if note:
        lines.append(note)
    return "\n".join(lines).rstrip()


def collectevent_text(col: dict) -> str:
    """收集活动 Featured Insta 计划表（文本版，与卡片同一数据）。"""
    if col.get("empty"):
        return col["empty"]
    ev, gen = col["ev"], col["gen"]
    now = int(col.get("now") or 0)
    cur = int(col.get("cur") or 0)
    start, end = int(ev.get("start") or 0), int(ev.get("end") or 0)
    rotations: dict = gen.get("rotations") or {}
    total = len(rotations)
    if now < start:
        lines = ["🎁 收集活动 · Featured Insta 计划表（未开始）"]
    elif now >= end:
        lines = ["🎁 收集活动 · Featured Insta 计划表（已结束）"]
    else:
        lines = ["🎁 收集活动 · Featured Insta 计划表",
                 f"剩余 {util.fmt_remaining(end - now)}（{util.fmt_time(end)} 结束）"]
    lines.append(util._fmt_range(ev))
    lines.append("时间均为北京时间（UTC+8），每 8 小时轮换")
    lines.append("")
    first = 0 if cur < 0 or cur >= total else cur
    for idx in range(first, total):
        towers = rotations.get(idx) or []
        if not towers:
            continue
        slot_start = instagen.rotation_start(start, idx)
        dt = datetime.fromtimestamp(slot_start / 1000, tz=util._SH)
        when = f"{dt.year}/{dt.month}/{dt.day} {dt:%H:%M}"
        if idx == cur and 0 <= cur < total:
            remain = util.fmt_remaining(slot_start + instagen.ROTATION_MS - now)
            when += f"（更换于 {remain}后）"
        names = "、".join(i18n.tower_cn(t) for t in towers)
        lines.append(f"  {when}  {names}")
    note = col.get("stale_note") or ""
    if note:
        lines.extend(["", note])
    return "\n".join(lines).rstrip()


def ct_text(col: dict) -> str:
    """争夺领土文本版：活动信息 + 格子/模式统计（地图详情见图片卡片）。"""
    if col.get("empty"):
        return col["empty"]
    ev = col["ev"]
    number = int(col.get("number") or 0)
    now = int(col.get("now") or 0)
    state = util._STATE_TXT[util._state_of(ev, now)]
    lines = [
        f"🏴 争夺领土 #{number or '?'}（{state}）",
        f"{util._fmt_range(ev)} · 个人 {int(ev.get('totalScores_player') or 0):,} · 战队 {int(ev.get('totalScores_team') or 0):,}",
    ]
    if state == "on":
        lines[0] += f" 剩余 {util.fmt_remaining(int(ev.get('end') or 0) - now)}"
    elif state == "up":
        lines[0] += f" {util.fmt_remaining(int(ev.get('start') or 0) - now)}后开始"
    nk_tiles = col.get("nk_tiles") or []
    ct_tiles = col.get("ct_tiles") or {}
    if nk_tiles:
        # NK tiles 的类型带后缀（如 "Relic - Techbot"），按主类型归并统计
        type_cnt = Counter(str(t.get("type") or "?").split(" - ")[0] for t in nk_tiles if isinstance(t, dict))
        parts = [f"{k} {v}" for k, v in type_cnt.most_common()]
        lines.append(f"🗺 领地 {len(nk_tiles)} 块：{' · '.join(parts)}")
    if ct_tiles:
        mode_cnt = Counter((d.get("GameData") or {}).get("subGameType") for d in ct_tiles.values())
        labels = {2: "竞速", 4: "Boss", 8: "最少现金", 9: "最少层数"}
        mode_parts = [f"{labels.get(k, k)} {v}" for k, v in mode_cnt.most_common() if k is not None]
        if mode_parts:
            lines.append(f"⚔ 模式：{' · '.join(mode_parts)}")
    powers = col.get("daily_powers") or []
    if powers:
        lines.append("⚡ 每日力量：" + "、".join(i18n._odyssey_power_name(str(p)) for p in powers[:7])
                     + ("…" if len(powers) > 7 else ""))
    relics = col.get("event_relics") or []
    if relics:
        lines.append("💎 本期遗物池：" + "、".join(i18n._RELIC_CN.get(str(r), str(r)) for r in relics))
    if not ct_tiles:
        lines.append("（地图布局数据暂缺，仅显示 NK 官方摘要）")
    note = col.get("stale_note") or ""
    if note:
        lines.extend(["", note])
    return "\n".join(lines)


_CT_PRESET_CN = {"default": "默认", "gametypes": "游戏类型", "maps": "地图背景", "heroes": "英雄", "coords": "坐标"}


def ct_preset_text(col: dict, name: str) -> str:
    """CT 显示预设卡的文本兜底（图片发送失败时）。"""
    ct_tiles = col.get("ct_tiles") or {}
    number = int(col.get("number") or 0)
    label = _CT_PRESET_CN.get(name, name)
    lines = [f"🏴 争夺领土 #{number or '?'} · {label} 预设（Display Presets）"]
    if name == "gametypes":
        cnt = Counter()
        for d in ct_tiles.values():
            sub = (d.get("GameData") or {}).get("subGameType")
            if sub is not None:
                cnt[sub] += 1
        parts = [f"{lab} {cnt.get(sub, 0)}" for sub, lab in
                 ((8, "最少现金"), (9, "最少层数"), (2, "竞速"), (4, "Boss")) if cnt.get(sub)]
        if parts:
            lines.append("模式分布：" + " · ".join(parts))
    elif name == "heroes":
        hero_cnt = Counter()
        for d in ct_tiles.values():
            hero_cnt[ctmap.hero_icon_name(d.get("GameData") or {})] += 1
        parts = []
        for icon, n in hero_cnt.most_common(8):
            if icon.startswith("HeroIcon"):
                parts.append(f"{i18n.hero_cn(icon[len('HeroIcon'):])}×{n}")
            elif icon == "NoHeroSelected":
                parts.append(f"未限定 {n}")
            else:
                parts.append(f"自选/全体 {n}")
        if parts:
            lines.append("英雄限定：" + " · ".join(parts))
    lines.append("（本条为降级文本，完整地图请见图片）")
    return "\n".join(lines)


def ct_tile_text(col: dict, tile_id: str) -> str:
    """CT 单格详情文本（图片发送失败时使用）。"""
    if col.get("empty"):
        return col["empty"]
    grid = ctmap.build_ct_grid()
    ct_tiles = col.get("ct_tiles") or {}
    if tile_id not in grid["tiles"]:
        return f"未找到格子 {tile_id}（共 {len(grid['tiles']) + 6} 个有效 id）"
    data = ct_tiles.get(tile_id) or {}
    gd = data.get("GameData") or {}
    rules = (gd.get("dcModel") or {}).get("startRules") or {}
    r0 = int(rules.get("round") or 0)
    r1 = rules.get("endRound")
    if r1 is None:
        rounds_txt = f"{r0}/?"
    elif int(r1) < 0:
        tier = int(((gd.get("bossData") or {}).get("TierCount")) or 0)
        rounds_txt = f"{r0}/{20 + 20 * tier}+"
    else:
        rounds_txt = f"{r0}/{int(r1)}"
    tile_type = str(data.get("TileType") or "Regular")
    relic = str(data.get("RelicType") or "None")

    sub = gd.get("subGameType")
    boss = gd.get("bossData") or {}
    if sub == 4 and boss:
        idx = int(boss.get("bossBloon") or 0)
        boss_name = ctmap.BOSSES_IN_ORDER[idx] if 0 <= idx < len(ctmap.BOSSES_IN_ORDER) else "Boss"
        tier = int(boss.get("TierCount") or 0)
        mode = f"Boss {boss_name}" + (f" T{tier}" if tier else "")
    else:
        mode = ctmap.GAMETYPE_CN.get(sub, "标准")
    lines = [
        f"🏴 争夺领土 · 格子 {tile_id}",
        f"模式：{mode}",
        f"回合：{rounds_txt}",
        f"地图：{gd.get('selectedMap') or '未指定'}",
    ]
    if tile_type == "Banner":
        lines.append("格子类型：CT积分（旗帜）")
    if relic != "None":
        lines.append(f"遗物：{i18n._RELIC_CN.get(relic, relic)}")
    heroes = ctmap.tile_heroes(gd)
    if heroes:
        if any(h == "ChosenPrimaryHero" for h in heroes):
            lines.append("英雄：队长自选")
        else:
            lines.append("英雄：" + "、".join(i18n.hero_cn(h) for h in dict.fromkeys(heroes)))
    else:
        icon_name = ctmap.hero_icon_name(gd)
        if icon_name == "NoHeroSelected":
            lines.append("英雄：无限定")
        elif icon_name == "AllHeroesIcon":
            lines.append("英雄：自选/全体")
    return "\n".join(lines)
