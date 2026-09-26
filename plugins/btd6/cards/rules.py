"""竞赛/Boss 规则卡片（游戏内挑战详情页风格）与争夺领土卡片。"""
import re

from . import common
from .. import assets, i18n, roundsets, rushgen, textfmt, util


def _race_emblem(ev: dict | None, side_img: str, fallback: str = "🏆", is_daily: bool = False) -> str:
    """Boss 使用首领徽章，普通竞速使用游戏内竞速奖杯图标，每日挑战用日历圆形头图。"""
    if is_daily:
        daily_asset = "daily-challenge.png"
        if assets._ui_asset_data_url(daily_asset):
            return common._race_ui_img(daily_asset, "📅", "race-emblem-img")
    asset = assets._boss_event_asset(ev)
    if asset and assets._ui_asset_data_url(asset):
        return common._race_ui_img(asset, "🐒", "race-emblem-img")
    if side_img:
        return f"<img class='race-emblem-img' src='{util._esc(side_img)}' alt='首领'/>"
    race_asset = "race-event.png"
    if assets._ui_asset_data_url(race_asset):
        return common._race_ui_img(race_asset, "🏆", "race-emblem-img")
    return f"<div class='race-emblem-fallback'>{util._esc(fallback)}</div>"


def _race_title(name: str, ev: dict | None, side_img: str) -> str:
    raw = str(name or "").strip()
    if side_img and ev:
        # 标题只保留 Boss 汉化名，不带活动期数（原“菱背 7”→“菱背”）
        return i18n.boss_cn(ev.get("bossType"))
    return i18n._RACE_TITLE_CN.get(raw.casefold(), raw) or "气球塔防6挑战"


def _race_time_line(ev: dict | None) -> str:
    if not ev:
        return ""
    try:
        start, end = int(ev.get("start") or 0), int(ev.get("end") or 0)
        if start <= 0 or end <= start:
            return ""
    except (TypeError, ValueError):
        return ""
    # 只显示活动的固定时间范围。倒计时随时间流逝不断变化，会让本应长期
    # 不变的竞速规则卡片不断生成新 HTML，从而失去持久缓存的意义。
    return f"活动时间：{util._fmt_range(ev)}"


def _custom_round_set_keys(meta: dict) -> list[str]:
    raw = meta.get("roundSets")
    values = [raw] if isinstance(raw, str) else raw if isinstance(raw, (list, tuple)) else []
    return [str(value or "").strip() for value in values
            if str(value or "").strip() and str(value or "").strip().casefold() != "default"]


def _custom_round_sets(meta: dict) -> list[str]:
    """API 的 roundSets 中除 default 外的回合组，返回卡片可读的中文名称。"""
    return [i18n._ROUND_SET_CN.get(key.casefold(), key) for key in _custom_round_set_keys(meta)]


def _custom_round_details(meta: dict) -> list[tuple[str, str]]:
    """把自定义回合组展开为“第几回合：出现什么气球”。

    优先用本地回合组快照（roundsets.describe，与默认回合组逐回合对比生成）；
    快照缺失时退回 i18n._ROUND_SET_DETAILS 的静态摘要；两者皆无才显示
    “API 未提供逐回合明细”兜底文案。
    """
    details = []
    for key in _custom_round_set_keys(meta):
        rows = roundsets.describe(key)
        if rows:
            details.extend(rows)
            continue
        known = i18n._ROUND_SET_DETAILS.get(key.casefold())
        if known:
            details.extend(known)
        else:
            name = i18n._ROUND_SET_CN.get(key.casefold(), key)
            details.append(("回合组", f"{name}：API 未提供逐回合明细"))
    return details


_ROUND_BLOON_ICON_FILES = {
    "MOAB级气球": "Moab.png",
    "陶瓷气球": "Ceramic.png",
    "MOAB": "Moab.png",
    "BFB": "Bfb.png",
    "ZOMG": "Zomg.png",
    "BAD": "Bad.png",
    "DDT": "DdtCamo.png",
}
_ROUND_BLOON_TOKEN_RE = re.compile("|".join(
    re.escape(token) for token in sorted(_ROUND_BLOON_ICON_FILES, key=len, reverse=True)
))


def _round_bloon_icon(token: str) -> str:
    """回合明细中的气球名称 → 本地图标，素材缺失时保留文字降级。"""
    fname = _ROUND_BLOON_ICON_FILES.get(token, "")
    url = assets._ui_asset_data_url(fname)
    if not url:
        return f"<span class='race-bloon-fallback'>{util._esc(token)}</span>"
    return (f"<span class='race-bloon-icon' title='{util._esc(token)}'>"
            f"<img src='{util._esc(url)}' alt='{util._esc(token)}'/></span>")


def _round_detail_desc_html(description: str) -> str:
    """转义普通文字，并把已知气球名替换为本地透明图标。"""
    text = str(description or "")
    chunks = []
    pos = 0
    for match in _ROUND_BLOON_TOKEN_RE.finditer(text):
        chunks.append(util._esc(text[pos:match.start()]))
        chunks.append(_round_bloon_icon(match.group(0)))
        pos = match.end()
    chunks.append(util._esc(text[pos:]))
    return "".join(chunks)


def _path_max_txt(blocked: dict) -> str:
    """路径限制 → 3-2-3（每条路可升到的最高层数，BTD6 每路满级 5 层）。
    封锁值 -1 表示整路禁用 → 显示 0（与游戏/BTD6 API Explorer 一致）。"""
    def cap(p: int) -> str:
        n = int(blocked.get(p, 0) or 0)
        return "0" if n == -1 else str(max(0, 5 - n))
    return "-".join(cap(p) for p in (1, 2, 3))


def _monkey_cell(raw: str, is_hero: bool, mx, blocked: dict) -> str:
    """猴子限制网格中的一个格子：立绘 + 限购/路径角标（禁用的塔不进网格）。"""
    icon = assets._tower_icon(raw, is_hero)
    name = assets._tower_display_name(raw, is_hero)
    limited = isinstance(mx, (int, float)) and 0 < mx < 99
    if not icon:
        tags = []
        if limited:
            tags.append(f"×{int(mx)}")
        if blocked:
            tags.append(_path_max_txt(blocked))
        line = f"<div class='mk txt'>{util._esc(name)}" \
               + (f"<br>{util._esc('  '.join(tags))}" if tags else "") + "</div>"
        return f"<div class='mkwrap'>{line}</div>"
    cell = f"<div class='mk'><img src='{util._esc(icon)}'/><div class='nm'>{util._esc(name)}</div>"
    if limited:
        cell += f"<div class='lim'>×{int(mx)}</div>"
    if blocked:
        cell += f"<div class='pth'><span>{_path_max_txt(blocked)}</span></div>"
    cell += "</div>"
    return f"<div class='mkwrap'>{cell}</div>"


def _monkey_grid(towers: list) -> str:
    """渲染可用/受限猴子网格（禁用的塔直接移除；兼容规则 _towers 与远征 _availableTowers）。"""
    cells = []
    for t in towers or []:
        raw = str(t.get("tower") or "").strip()
        if not raw or raw == "ChosenPrimaryHero":
            continue
        mx = t.get("max")
        if mx == 0:
            continue  # 禁用：直接不显示
        blocked = {
            p: n for p in (1, 2, 3)
            if (n := int(t.get(f"path{p}NumBlockedTiers") or 0)) != 0
        }
        is_hero = bool(t.get("isHero"))
        limited = isinstance(mx, (int, float)) and 0 < mx < 99
        if not limited and not blocked and not (is_hero and mx):
            continue  # 无限制的普通塔不上图
        cells.append(_monkey_cell(raw, is_hero, mx, blocked))
    if not cells:
        return "<div class='empty'>本活动没有猴子限制</div>"
    return "<div class='mkgrid'>" + "".join(cells) + "</div>"


def _daily_monkey_cells(meta: dict) -> list[str]:
    """每日挑战猴子格子（全塔总览口径），供 rules_html 的 div 网格与每日 bdual 表格共用。

    官方 _towers 里有记录的塔带限制角标，未记录的塔表示无限制（正常可放、可满级），
    max=0 的塔不显示；-1 视为无限制（与游戏内 9999 同义）；英雄全部可用时合并为一张
    “全部英雄”块放首位。
    """
    restrictions = {}
    for t in meta.get("_towers") or []:
        raw = str(t.get("tower") or "").strip()
        if raw and raw != "ChosenPrimaryHero":
            restrictions[raw] = t
    constants = rushgen.load_constants()
    # 英雄放首位 + 去重：初级/高级的英雄默认 1 不打 ×1 角标，且应排在最前
    order = list(constants.get("heroesInOrder") or []) + list(constants.get("towersInOrder") or [])
    all_heroes_ok = _heroes_all_available(meta.get("_towers"))
    hero_cells = []
    monkey_cells = []
    for name in order:
        if all_heroes_ok and name in (constants.get("heroesInOrder") or []):
            continue  # 英雄全部可用：跳过逐个英雄，稍后放一张“全部英雄”块
        entry = dict(restrictions.get(name) or {"tower": name})
        entry.setdefault("tower", name)
        if "isHero" not in entry:
            entry["isHero"] = name in (constants.get("heroesInOrder") or [])
        # 英雄默认 1 不视为限购：不打角标
        is_hero = bool(entry.get("isHero"))
        try:
            mx_val = entry.get("max")
            # -1 视为无限制
            if float(mx_val) == -1:
                entry = dict(entry)
                entry["max"] = None
                mx_val = None
            if float(entry.get("max")) == 0:
                continue
            # 英雄 max=1 视为默认，不打角标
            if is_hero and mx_val is not None and float(mx_val) == 1:
                entry = dict(entry)
                entry["max"] = None
        except (TypeError, ValueError):
            pass
        cell = _race_monkey_cell(entry)
        if is_hero:
            hero_cells.append(cell)
        else:
            monkey_cells.append(cell)
    if all_heroes_ok:
        hero_cells = [_all_heroes_tile()]
    return hero_cells + monkey_cells


def _daily_monkey_grid(meta: dict) -> str:
    cells = _daily_monkey_cells(meta)
    if not cells:
        return "<div class='mkgrid race-mkgrid'><div class='race-mk-fallback'>无</div></div>"
    return "<div class='mkgrid race-mkgrid' style='height:auto;overflow:visible;'>" + "".join(cells) + "</div>"



def _race_visible_towers(towers: list) -> list[dict]:
    """截图中的默认视图：显示所有可用/限购塔，隐藏 max=0 与占位英雄。"""
    visible = []
    for tower in towers or []:
        raw = str(tower.get("tower") or "").strip()
        if not raw or raw == "ChosenPrimaryHero":
            continue
        try:
            if float(tower.get("max")) == 0:
                continue
        except (TypeError, ValueError):
            pass
        visible.append(tower)
    return sorted(visible, key=lambda tower: assets._RACE_TOWER_ORDER_INDEX.get(
        str(tower.get("tower") or "").strip(), len(assets._RACE_TOWER_ORDER)
    ))


def _race_monkey_cell(tower: dict) -> str:
    raw = str(tower.get("tower") or "").strip()
    is_hero = bool(tower.get("isHero"))
    mx = tower.get("max")
    icon = assets._tower_icon(raw, is_hero)
    name = assets._tower_display_name(raw, is_hero)
    category = assets._tower_category(raw, is_hero)
    try:
        max_num = float(mx)
    except (TypeError, ValueError):
        max_num = None
    limited = max_num is not None and 0 < max_num < 99
    blocked = {
        p: n for p in (1, 2, 3)
        if (n := int(tower.get(f"path{p}NumBlockedTiers") or 0)) != 0
    }
    tags = []
    if limited:
        tags.append(f"×{int(max_num)}")
    if blocked:
        tags.append(_path_max_txt(blocked))
    title = name + (f" · {' '.join(tags)}" if tags else "")
    if icon:
        cell = f"<div class='race-mk {category}' title='{util._esc(title)}'>"
        cell += f"<img src='{util._esc(icon)}' alt='{util._esc(name)}'/>"
    else:
        cell = f"<div class='race-mk {category}' title='{util._esc(title)}'>"
        cell += f"<div class='race-mk-fallback'>{util._esc(name)}</div>"
    if limited:
        cell += f"<div class='race-lim'>×{int(max_num)}</div>"
    if blocked:
        cell += f"<div class='race-path'>{util._esc(_path_max_txt(blocked))}</div>"
    cell += "</div>"
    return f"<div class='race-mkwrap'>{cell}</div>"
def _hero_entries(towers: list) -> list[dict]:
    """_towers 里的英雄条目（排除 ChosenPrimaryHero 占位）。"""
    return [t for t in towers or [] if isinstance(t, dict) and bool(t.get("isHero"))
            and str(t.get("tower") or "").strip() not in ("", "ChosenPrimaryHero")]


def _heroes_all_available(towers: list) -> bool:
    """英雄是否全部可选。首选英雄（ChosenPrimaryHero）可用即默认英雄规则，
    视为全部英雄可选（如 Boss 活动允许首选英雄）；否则任一具体英雄被禁
    （max=0）、限购（max>1）或封路径即为 False，未列出的英雄视为默认可用。"""
    if _chosen_hero_allowed(towers):
        return True
    for t in _hero_entries(towers):
        try:
            mx = float(t.get("max"))
        except (TypeError, ValueError):
            mx = None
        if mx is not None and (mx == 0 or mx > 1):
            return False
        if any(int(t.get(f"path{p}NumBlockedTiers") or 0) != 0 for p in (1, 2, 3)):
            return False
    return True


def _all_heroes_tile() -> str:
    """“全部英雄可用”单块：官方 AllHeroesIcon，替代整排逐个英雄格。"""
    url = assets._game_asset_data_url("AllHeroesIcon.webp")
    if url:
        cell = ("<div class='race-mk hero' title='全部英雄可用'>"
                f"<img src='{util._esc(url)}' alt='全部英雄'/></div>")
    else:
        cell = ("<div class='race-mk hero' title='全部英雄可用'>"
                "<div class='race-mk-fallback'>全部英雄</div></div>")
    return f"<div class='race-mkwrap'>{cell}</div>"


def _chosen_hero_allowed(towers: list) -> bool:
    """ChosenPrimaryHero（首选英雄）占位条目 max>0 或 -1 时可用。
    游戏默认本就只能带一个英雄（开局自选），首选英雄可用即默认英雄规则。"""
    for t in towers or []:
        if isinstance(t, dict) and str(t.get("tower") or "").strip() == "ChosenPrimaryHero":
            try:
                mx = float(t.get("max"))
            except (TypeError, ValueError):
                return False
            return mx > 0 or mx == -1
    return False


def _race_monkey_grid(towers: list) -> str:
    cells = []
    if _heroes_all_available(towers):
        # 英雄全部可用：不逐个铺英雄格，改用一张“全部英雄”图标放最前
        cells.append(_all_heroes_tile())
        towers = [t for t in towers or []
                  if not (isinstance(t, dict) and bool(t.get("isHero"))
                          and str(t.get("tower") or "").strip() != "ChosenPrimaryHero")]
    cells.extend(_race_monkey_cell(tower) for tower in _race_visible_towers(towers))
    if not cells:
        return "<div class='mkgrid race-mkgrid'><div class='race-mk-fallback'>无</div></div>"
    return "<div class='mkgrid race-mkgrid'>" + "".join(cells) + "</div>"


def _stat(label: str, value: str) -> str:
    return f"<div class='st'>{util._esc(label)} <b>{util._esc(value)}</b></div>"



def mod_body_est(modifier_html: str) -> int:
    """气球强化面板体的内容高度估算：与 rules_html 画布估算里 mod_body 同口径。"""
    n = modifier_html.count("race-mod-item")
    return 18 + max(1, -(-n // 2)) * 34 if n else 40

def _fmt_range_full(ev: dict) -> str:
    """活动起止时间（到秒，上海时区），与「BOSS情报」参考图一致。"""
    from datetime import datetime

    from ..util import _SH
    s = datetime.fromtimestamp(int(ev.get("start") or 0) / 1000, tz=_SH)
    e = datetime.fromtimestamp(int(ev.get("end") or 0) / 1000, tz=_SH)
    return (
        f"{s.year}/{s.month:02d}/{s.day:02d} {s.hour:02d}:{s.minute:02d}:{s.second:02d}"
        f" ~ "
        f"{e.year}/{e.month:02d}/{e.day:02d} {e.hour:02d}:{e.minute:02d}:{e.second:02d}"
    )


def _boss_dual_rule_chips(meta: dict) -> str:
    """规则调节 chips：限制塔数 / 限制模范 / MOAB速度 / BOSS速度 / BOSS血量。"""
    max_towers = int(meta.get("maxTowers") or 0)
    towers_cap = "无限制" if max_towers >= 9999 or max_towers <= 0 else f"{max_towers:,}"
    paragon_limit = int(meta.get("maxParagons") or 0)
    mods = meta.get("_bloonModifiers") or {}

    def chip(icon: str, fallback: str, label: str, value: str) -> str:
        img_html = common._race_ui_img(icon, fallback, "bdual-rule-icon")
        return (
            f"<div class='bdual-rule-chip'>{img_html}{util._esc(label)} "
            f"<b>{util._esc(value)}</b></div>"
        )

    chips = [
        chip("monkey-cap.png", "🐒", "限制塔数", towers_cap),
        chip("paragon.png", "◉", "限制模范", str(paragon_limit)),
    ]
    moab_spd = mods.get("moabSpeedMultiplier")
    boss_spd = mods.get("bossSpeedMultiplier")
    boss_hp = (mods.get("healthMultipliers") or {}).get("boss")

    def fmt_mult(v) -> str | None:
        try:
            n = float(v)
        except (TypeError, ValueError):
            return None
        if abs(n - 1.0) < 1e-9:
            return None
        return f"x{n:g}"

    v = fmt_mult(moab_spd)
    if v:
        chips.append(chip("FasterMoabIcon.png", "🚀", "MOAB速度", v))
    v = fmt_mult(boss_spd)
    if v:
        chips.append(chip("FasterBossIcon.png", "⚡", "BOSS速度", v))
    v = fmt_mult(boss_hp)
    if v:
        chips.append(chip("BossBoostIcon.png", "♥", "BOSS血量", v))
    # 自定义回合等额外规则不进 chips，仍由网格与兼容文本覆盖
    return "<div class='bdual-rule-chips'>" + "".join(chips) + "</div>"


def _difficulty_ui_icon(diff_raw: str) -> str:
    """难度 → 地图难度按钮图标（Beginner/Intermediate/Advanced/Expert）。"""
    key = str(diff_raw or "").strip().lower()
    mapping = {
        "beginner": "MapBeginnerBtn.png",
        "intermediate": "MapIntermediateBtn.png",
        "advanced": "MapAdvancedBtn.png",
        "expert": "MapExpertBtn.png",
        "easy": "MapBeginnerBtn.png",
        "medium": "MapIntermediateBtn.png",
        "hard": "MapAdvancedBtn.png",
    }
    return mapping.get(key, "")


def _boss_dual_meta_chips(variant_col: dict, daily: bool = False) -> str:
    meta = variant_col["meta"]
    diff_raw = str(meta.get("difficulty") or "")
    diff = i18n.cn(diff_raw, i18n.DIFFICULTY_CN)
    mode = i18n.mode_cn(meta.get("mode"))
    scoring = variant_col.get("scoring_cn") or ""
    start_r = int(meta.get("startRound") or 0)
    end_r = int(meta.get("endRound") or 0)
    cash = int(meta.get("startingCash") or 0)
    lives = int(meta.get("lives") or 0)

    def chip(icon: str, fallback: str, label: str, value: str) -> str:
        icon_html = common._race_ui_img(icon, fallback, "bdual-chip-icon") if icon else ""
        return (
            f"<span class='bdual-chip'>{icon_html}"
            f"{util._esc(label)} <b>{util._esc(value)}</b></span>"
        )

    diff_icon = _difficulty_ui_icon(diff_raw)
    row1 = (
        "<div class='bdual-chip-row'>"
        + chip(diff_icon, "★", "难度", diff or "?")
        + chip("RaceIcon.png", "🏁", "模式", mode or "?")
        + (chip("fastest-time.png", "⏱", "排位", scoring) if scoring else "")
        + chip("start-round.png", "▶", "回合", f"{start_r}–{end_r}")
        + "</div>"
    )
    row2 = (
        "<div class='bdual-chip-row'>"
        + chip("cash.png", "🪙", "资金", f"{cash:,}")
        + chip("heart.png", "❤", "生命", f"{lives:,}")
        + (chip("heart.png", "❤", "最大生命", f"{int(meta.get('maxLives') or 0):,}") if daily else "")
        + "</div>"
    )
    return row1 + row2


def _bdual_grid_table(cells: list[str], cols: int = 5) -> str:
    """猴子格子 → bdual 固定列表格：强制从左到右、从上到下，不足一行补空单元对齐。"""
    rows_html = []
    for start in range(0, len(cells), cols):
        chunk = cells[start:start + cols]
        while len(chunk) < cols:
            chunk.append("<td class='bdual-mk-empty'></td>")
        # _race_monkey_cell 返回的是 wrap div；塞进 td 时保留结构
        rows_html.append("<tr>" + "".join(f"<td class='bdual-mk-cell'>{cell}</td>" for cell in chunk) + "</tr>")
    return "<table class='bdual-mk-grid'>" + "".join(rows_html) + "</table>"


def _boss_dual_monkey_grid(meta: dict, cols: int = 5) -> str:
    """可用猴子网格：固定 cols 列表格，强制从左到右、从上到下。"""
    towers = meta.get("_towers")
    cells: list[str] = []
    if _heroes_all_available(towers):
        cells.append(_all_heroes_tile())
        towers = [
            t for t in towers or []
            if not (isinstance(t, dict) and bool(t.get("isHero"))
                    and str(t.get("tower") or "").strip() != "ChosenPrimaryHero")
        ]
    cells.extend(_race_monkey_cell(tower) for tower in _race_visible_towers(towers))
    if not cells:
        return "<div class='bdual-mk-grid'><div class='race-mk-fallback'>无</div></div>"
    return _bdual_grid_table(cells, cols)



def _boss_dual_tile_count(meta: dict, cols: int = 5) -> int:
    """网格行数：只数塔，不拼 HTML（高度估算避免二次生成大图 data URL）。"""
    towers = meta.get("_towers")
    if _heroes_all_available(towers):
        rest = [
            t for t in towers or []
            if not (isinstance(t, dict) and bool(t.get("isHero"))
                    and str(t.get("tower") or "").strip() != "ChosenPrimaryHero")
        ]
        n = 1 + len(_race_visible_towers(rest))
    else:
        n = len(_race_visible_towers(towers))
    return max(1, -(-max(n, 1) // cols))


def _boss_dual_rule_chip_count(meta: dict) -> int:
    """规则 chip 数量：与 _boss_dual_rule_chips 同口径，不拼 HTML。"""
    mods = meta.get("_bloonModifiers") or {}
    n = 2  # 限制塔数 / 限制模范

    def changed(v) -> bool:
        try:
            return abs(float(v) - 1.0) >= 1e-9
        except (TypeError, ValueError):
            return False

    if changed(mods.get("moabSpeedMultiplier")):
        n += 1
    if changed(mods.get("bossSpeedMultiplier")):
        n += 1
    if changed((mods.get("healthMultipliers") or {}).get("boss")):
        n += 1
    return n


def _boss_dual_panel_h(v: dict) -> int:
    meta_v = v.get("meta") or {}
    grid_rows = _boss_dual_tile_count(meta_v)
    chips = _boss_dual_rule_chip_count(meta_v)
    # head + meta 两行 + 规则标题/chips（按每行约 3 个估换行）+ 可用猴子标题 + 网格
    chip_rows = max(1, -(-chips // 3))
    return 42 + 78 + 36 + 34 + chip_rows * 36 + 8 + grid_rows * 120 + 8


def _boss_dual_panel(variant_col: dict) -> str:
    variant = variant_col["variant"]
    label = variant_col["label"]
    meta = variant_col["meta"]
    head_cls = "elite" if variant == "elite" else "standard"
    grid_html = _boss_dual_monkey_grid(meta)
    extras = []
    if _custom_round_sets(meta):
        extras.append("自定义回合")
    bans = [label_b for key, label_b in i18n.FLAG_LABELS if meta.get(key)]
    if bans:
        extras.append("禁用：" + "、".join(bans))
    extras_html = (
        f"<div class='bdual-extras'>{util._esc('；'.join(extras))}</div>" if extras else ""
    )
    return (
        "<div class='bdual-panel'>"
        f"<div class='bdual-panel-head {head_cls}'>{util._esc(label)}模式</div>"
        "<div class='bdual-panel-body'>"
        f"{_boss_dual_meta_chips(variant_col)}"
        "<div class='bdual-sec-label'>规则调节</div>"
        f"{_boss_dual_rule_chips(meta)}"
        f"{extras_html}"
        "<div class='bdual-sec-label'>可用猴子</div>"
        f"{grid_html}"
        "</div></div>"
    )


def boss_dual_html(col: dict) -> str:
    """Boss 标准+精英并排合卡：共享标题/横幅/地图条，左右双栏规则。"""
    if col.get("empty"):
        body = f"<div class='bdual-empty'>{util._esc(col['empty'])}</div>"
        return common._boss_dual_shell(body, 320)

    ev = col.get("ev") or {}
    variants = col.get("variants") or []
    primary = next(
        (v for v in variants if v.get("variant") == "standard"),
        variants[0] if variants else {},
    )
    meta = primary.get("meta") or {}
    boss_cn_name = i18n.boss_cn(ev.get("bossType") or "")
    raw_name = (ev.get("name") or meta.get("name") or "").strip()
    title = f"BOSS情报 - {raw_name or boss_cn_name or 'Boss'}"
    short_id = str(ev.get("id") or "")
    if "_" in short_id:
        short_id = short_id.split("_", 1)[1]
    time_range = _fmt_range_full(ev)
    subtitle = f"ID: {util._esc(short_id)} | {util._esc(time_range)}" if short_id else util._esc(time_range)

    side_img = col.get("side_img") or ""
    banner_inner = (
        f"<img src='{util._esc(side_img)}' alt='{util._esc(boss_cn_name)}'/>"
        if side_img
        else f"<div class='bdual-banner-fallback'>{util._esc(boss_cn_name or 'BOSS')}</div>"
    )
    banner = (
        "<div class='bdual-banner'>"
        f"{banner_inner}"
        "</div>"
    )

    map_cn_name = i18n.map_cn(str(meta.get("map") or "").strip())
    map_en = str(meta.get("map") or "").strip()
    map_title = f"{map_cn_name} ({map_en})" if map_en and map_cn_name != map_en else (map_cn_name or map_en or "?")
    map_img = col.get("map_img") or ""
    map_thumb = (
        f"<img src='{util._esc(map_img)}' alt='{util._esc(map_title)}'/>"
        if map_img
        else "<div class='bdual-map-thumb-fallback'>🗺</div>"
    )
    n_std = int(ev.get("totalScores_standard") or 0)
    n_elite = int(ev.get("totalScores_elite") or 0)
    mapbar = (
        "<div class='bdual-mapbar'>"
        f"<div class='bdual-map-thumb'>{map_thumb}</div>"
        "<div class='bdual-map-copy'>"
        f"<div class='bdual-map-name'>{util._esc(map_title)}</div>"
        f"<div class='bdual-map-time'>{util._esc(time_range)}</div>"
        "</div>"
        "<div class='bdual-map-counts'>"
        f"<span class='bdual-count-pill'>标准参与 {n_std:,}</span>"
        f"<span class='bdual-count-pill'>精英参与 {n_elite:,}</span>"
        "</div></div>"
    )

    std_html = ""
    eli_html = ""
    for v in variants:
        panel = _boss_dual_panel(v)
        if v.get("variant") == "standard":
            std_html = f"<div class='bdual-col std'>{panel}</div>"
        else:
            eli_html = f"<div class='bdual-col eli'>{panel}</div>"
    if not std_html:
        std_html = "<div class='bdual-col std'><div class='bdual-empty'>暂无标准规则</div></div>"
    if not eli_html:
        eli_html = "<div class='bdual-col eli'><div class='bdual-empty'>暂无精英规则</div></div>"

    body = (
        "<div class='bdual-titlebar'>"
        f"<div class='bdual-title'>{util._esc(title)}</div>"
        f"<div class='bdual-subtitle'>{subtitle}</div>"
        "</div>"
        f"{banner}{mapbar}"
        f"<div class='bdual-cols'>{std_html}{eli_html}</div>"
    )
    stale_note = col.get("stale_note") or ""
    if stale_note:
        body += f"<div class='bdual-note'>{util._esc(stale_note)}</div>"

    # 画布高度：轻量计数，避免为估高二次生成猴子网格 HTML
    extras_h = 22  # 可能的自定义回合/禁用行
    col_h = max((_boss_dual_panel_h(v) + extras_h for v in variants), default=420)
    frame_h = 10 + 62 + 8 + 268 + 8 + 104 + 8 + col_h + 8
    return common._boss_dual_shell(body, frame_h)


def _daily_dual_panel(v: dict, cols: int = 5, head_cls: str = "standard") -> tuple[str, int]:
    """每日 bdual 单面板：返回 (html, 网格行数)。

    与 Boss 面板共用元信息 chips/规则调节 chips 版式，猴子区改用每日全塔限制网格；
    面板自带地图缩略图与名称（标准/高级/Coop 地图各不相同），气球强化以 chips
    形式并入规则调节。
    """
    meta = v["meta"]
    grid_cells = _daily_monkey_cells(meta)
    rows = max(1, -(-max(len(grid_cells), 1) // cols))
    grid_html = _bdual_grid_table(grid_cells, cols)

    map_raw = str(meta.get("map") or "").strip()
    map_cn = i18n.map_cn(map_raw)
    map_txt = f"{map_cn} ({map_raw})" if map_raw and map_cn != map_raw else (map_cn or map_raw or "?")
    map_img = v.get("map_img") or ""
    map_thumb = (f"<img src='{util._esc(map_img)}' alt='{util._esc(map_txt)}'/>"
                 if map_img else "<div class='bdual-pmap-fallback'>🗺</div>")
    pmap = ("<div class='bdual-pmap'><div class='bdual-pmap-thumb'>"
            f"{map_thumb}</div>"
            f"<div class='bdual-pmap-copy'>{util._esc(map_txt)}</div></div>")

    extras = []
    if _custom_round_sets(meta):
        extras.append("自定义回合")
    bans = [label_b for key, label_b in i18n.FLAG_LABELS if meta.get(key)]
    if bans:
        extras.append("禁用：" + "、".join(bans))
    extras_html = (
        f"<div class='bdual-extras'>{util._esc('；'.join(extras))}</div>" if extras else ""
    )

    mod_lines = textfmt.bloon_mod_lines(meta.get("_bloonModifiers"))
    modifier_chips = ""
    if mod_lines:
        mod_chips = "".join(
            f"<div class='bdual-rule-chip'>{util._esc(line)}</div>" for line in mod_lines)
        modifier_chips = f"<div class='bdual-rule-chips'>{mod_chips}</div>"

    panel = (
        "<div class='bdual-panel'>"
        f"<div class='bdual-panel-head {head_cls}'>{util._esc(v['issue'])}</div>"
        "<div class='bdual-panel-body'>"
        f"{pmap}"
        f"{_boss_dual_meta_chips(v, daily=True)}"
        "<div class='bdual-sec-label'>规则调节</div>"
        f"{_boss_dual_rule_chips(meta)}"
        f"{modifier_chips}"
        f"{extras_html}"
        "<div class='bdual-sec-label'>可用猴子</div>"
        f"{grid_html}"
        "</div></div>")
    chips = _boss_dual_rule_chip_count(meta) + len(mod_lines)
    chip_rows = max(1, -(-chips // 3))
    panel_h = 142 + 78 + 68 + 36 + 34 + chip_rows * 36 + 8 + rows * 120 + 8  # +68: 面板地图行
    return panel, panel_h


def daily_dual_html(col: dict) -> str:
    """每日挑战 标准+高级并排合卡：复用 Boss bdual 版式组件。

    与 boss_dual_html 的差异：无 banner 图（NK 列表不提供每日横幅，且标准/高级
    是两期不同挑战）、地图缩略图取标准期、参与人数不可得故不显示；面板自带
    期号（标准·第N期 / 高级·第N期）与地图名，猴子区用每日全塔限制网格。
    """
    if col.get("empty"):
        body = f"<div class='bdual-empty'>{util._esc(col['empty'])}</div>"
        return common._boss_dual_shell(body, 320)

    variants = col.get('variants') or []
    ids = []
    main_rows = []  # 标准/高级/Coop 三面板平行
    panel_hs = []
    for v in variants:
        ev = v.get("ev") or {}
        sid = str(ev.get("id") or "")
        if "_" in sid:
            sid = sid.split("_", 1)[1]
        if sid:
            ids.append(sid)
        variant = v.get("variant")
        # 三面板平行：每列 3 列猴子网格；列间留 4px 缝，末列不留右侧缝
        gutter = "padding:0;" if len(main_rows) == 2 else "padding:0 4px 0 0;" if not main_rows else "padding:0 4px;"
        head_cls = {"advanced": "elite", "coop": "standard"}.get(variant, "standard")
        panel, panel_h = _daily_dual_panel(v, cols=3, head_cls=head_cls)
        main_rows.append(
            f"<div class='bdual-col {('eli' if variant == 'advanced' else 'std')}'"
            f" style='width:33.3%;{gutter}'>{panel}</div>")
        panel_hs.append(panel_h)
    if not main_rows:
        body = "<div class='bdual-empty'>暂无每日挑战数据</div>"
        return common._boss_dual_shell(body, 320)

    ids_txt = f"ID: {' / '.join(ids)}" if ids else ""
    titlebar = (
        "<div class='bdual-titlebar'>"
        "<div class='bdual-title'>每日挑战情报</div>"
        f"<div class='bdual-subtitle'>{util._esc(ids_txt)}</div>"
        "</div>")

    body = titlebar
    if main_rows:
        body += f"<div class='bdual-cols'>{''.join(main_rows)}</div>"
    stale_note = col.get("stale_note") or ""
    if stale_note:
        body += f"<div class='bdual-note'>{util._esc(stale_note)}</div>"

    extras_h = 22  # 可能的自定义回合/禁用行
    col_h = max((h + extras_h for h in panel_hs), default=420)
    frame_h = 10 + 62 + 8 + col_h + 8
    return common._boss_dual_shell(body, frame_h)


def rules_html(col: dict) -> str:
    if col.get("empty"):
        body = ("<div class='race-topbar'></div>"
                f"<div class='race-content'><div class='race-map-empty'>{util._esc(col['empty'])}</div></div>")
        return common._race_shell(body, 330)
    meta = col["meta"]
    name = (meta.get("name") or "").strip()
    diff = i18n.cn(meta.get("difficulty"), i18n.DIFFICULTY_CN)
    mode = i18n.mode_cn(meta.get("mode"))
    scoring = col.get("scoring_cn") or ""
    prefix = col.get("prefix") or ""
    kind_label = str(col.get("kind_label") or "")
    is_coop = kind_label.startswith("Co-op") or prefix.startswith("Co-op")
    # 每日/Co-op 共用每日系版式（日历徽章 + 全塔网格）；Co-op 不以「每日」作前缀
    is_daily = prefix.startswith("每日") or is_coop
    # 与每日一致：期号/类型放在标题下方副标题，不挤进大标题
    subtitle_parts = [prefix, diff, mode, scoring]
    subtitle = " - ".join(part for part in subtitle_parts if part)
    side_img = col.get("side_img") or ""
    ev = col.get("ev")
    map_img = col.get("map_img") or ""
    max_towers = int(meta.get("maxTowers") or 0)
    towers_cap = "无限制" if max_towers >= 9999 else f"{max_towers:,}"
    paragon_limit = int(meta.get("maxParagons") or 0)
    boss_label = "首领事件" if side_img else "竞速事件"
    if is_daily:
        boss_label = "每日挑战"
    # 收集层可用 kind_label 覆盖统计行标签（如 Co-op 挑战卡显示"Co-op 挑战"）
    boss_label = col.get("kind_label") or boss_label
    boss_asset = assets._boss_event_asset(ev) if side_img else ""
    custom_round_sets = _custom_round_sets(meta)

    def stat(icon: str, fallback: str, label: str, value: str = "") -> str:
        value_html = f"<div class='race-stat-value'>{util._esc(value)}</div>" if value else ""
        return ("<div class='race-stat'><div class='race-stat-icon-cell'>"
                f"{common._race_ui_img(icon, fallback, 'race-stat-icon')}"
                f"</div><div class='race-stat-copy'><div class='race-stat-label'>{util._esc(label)}</div>"
                f"{value_html}</div></div>")

    if is_daily:
        # 传文件名而非 data URL：stat() 内部经 _race_ui_img 按文件名解析，
        # 传 data URL 会被误当文件名拒绝而落入 emoji 兜底（字体无字形 → 空白）
        event_icon = "daily-challenge.png"
    else:
        event_icon = boss_asset or "RaceIcon.png"
    left_stats = [
        stat("cash.png", "🪙", "初始资金", f"{int(meta.get('startingCash') or 0):,}"),
        stat("heart.png", "❤", "初始生命", f"{int(meta.get('lives') or 0):,}"),
        stat("heart.png", "❤", "最大生命", f"{int(meta.get('maxLives') or 0):,}"),
    ]
    right_stats = [
        stat("start-round.png", "▶", "开始回合", str(int(meta.get('startRound') or 0))),
        stat("end-round.png", "⏭", "结束回合", str(int(meta.get('endRound') or 0))),
        stat("monkey-cap.png", "🐒", "最大猴子", towers_cap),
    ]
    if not is_daily:
        # 竞速/首领卡保留事件行与最快用时；每日系卡片去掉（事件行与标题重复、最快用时无值）
        left_stats.append(stat(event_icon, "⚑", boss_label))
        right_stats.append(stat("fastest-time.png", "⏱", "最快用时"))
    stat_left = "".join(left_stats)
    stat_right = "".join(right_stats)
    emblem = _race_emblem(ev, side_img, "📅" if is_daily else "🏆", is_daily=is_daily)
    # 每日/Co-op 大标题用中文地图名（与游戏内一致）；类型/期号/作者在副标题
    map_cn_name = i18n.map_cn(str(meta.get("map") or "").strip())
    if is_coop or prefix.startswith("每日"):
        title = map_cn_name or ("协作挑战" if is_coop else "每日挑战")
        if name and name not in subtitle:
            subtitle = f"{subtitle} · {name}" if subtitle else name
    else:
        title = _race_title(name, ev, side_img)
    time_line = _race_time_line(ev)
    time_html = f"<div class='race-time'>{util._esc(time_line)}</div>" if time_line else ""
    body = ("<div class='race-topbar'><div class='race-head'>"
            f"<div class='race-emblem-cell'><div class='race-emblem'>{emblem}</div></div>"
            f"<div class='race-title-cell'><div class='race-title'>{util._esc(title)}</div>"
            f"<div class='race-subtitle'>{util._esc(subtitle)}</div>"
            f"{time_html}</div>"
            "</div></div>")
    map_alt = map_cn_name or "map"
    map_img_html = (f"<img src='{util._esc(map_img)}' alt='{util._esc(map_alt)}'/>" if map_img
                    else "<div class='race-map-empty'>🗺</div>")
    if is_daily or str(meta.get('id') or '').startswith('rot'):
        # 每日挑战：全塔总览（限购/路径角标只在有限制的塔上，禁用塔不显示）
        grid_html = _daily_monkey_grid(meta)
    else:
        grid_html = _race_monkey_grid(meta.get('_towers'))
    body += ("<div class='race-content'><div class='race-layout'>"
             f"<div class='race-map'>{map_img_html}</div>"
             f"<div class='race-stats-cell'><div class='race-stats'><div class='race-stat-col'>{stat_left}</div>"
             f"<div class='race-stat-col'>{stat_right}</div></div></div></div>"
             "<div class='race-monkey-section'><div class='race-options'>"
             "<div class='race-available'>可用猴子：</div></div>"
             f"{grid_html}</div>")

    modifier_html = common._race_modifier_html(meta.get("_bloonModifiers"))
    custom_round_details = _custom_round_details(meta)
    custom_rule_item = ""
    paragon_rule_class = "race-rule-item race-rule-single"
    if custom_round_sets:
        custom_rule_item = ("<div class='race-rule-item'>"
                            f"<div class='race-rule-icon'>{common._race_ui_img('custom-rounds.png', '❓', 'race-rule-icon-img')}</div>"
                            "<div class='race-rule-copy'>自定义回合</div></div>")
        paragon_rule_class = "race-rule-item"
    # 左右两个面板体用同一 min-height，保证"气球强化"与"规则"方框等高
    #（一侧内容变多抬高 max 值时，另一侧同步撑到相同高度）
    panel_body_h = max(mod_body_est(modifier_html), 76)
    bottom = ("<div class='race-bottom'><div class='race-bottom-left'>"
              "<div class='race-panel-head'>气球强化</div>"
              f"<div class='race-panel-body' style='min-height:{panel_body_h}px'>"
              f"{modifier_html}</div></div>"
              "<div class='race-bottom-right'><div class='race-panel-head'>规则</div>"
              f"<div class='race-panel-body' style='min-height:{panel_body_h}px'>"
              f"<div class='race-rule-row'>"
              f"{custom_rule_item}<div class='{paragon_rule_class}'>"
              f"<div class='race-rule-icon'>{common._race_ui_img('paragon.png', '◉', 'race-rule-icon-img')}</div>"
              f"<div class='race-rule-copy'>神级猴上限<br><span class='race-limit-value'>{paragon_limit}</span></div>"
              "</div></div></div></div></div>")
    if custom_round_sets:
        custom_text = "、".join(custom_round_sets)
        custom_group_line = f"<div class='race-round-set-name'>启用回合组：{util._esc(custom_text)}</div>"
        custom_detail_lines = "".join(
            f"<div class='race-round-line'><span class='race-round-wave'>{util._esc(wave)}</span>"
            f"<span class='race-round-desc'>：{_round_detail_desc_html(description)}</span></div>"
            for wave, description in custom_round_details
        )
        bottom += ("<div class='race-custom-panel'><div class='race-custom-title'>自定义回合</div>"
                   "<div class='race-custom-body'>"
                   f"<div class='race-custom-icon'>{common._race_ui_img('custom-rounds.png', '❓', 'race-custom-icon-img')}</div>"
                   f"<div class='race-custom-copy'>{custom_group_line}"
                   f"<div class='race-round-lines'>{custom_detail_lines}</div></div>"
                   "</div></div>")
    body += bottom
    compat = _rules_compat_html(meta, prefix, scoring, ev)
    body += compat
    # ---- 画布高度估算（Explorer 版式）：标题条 + 主面板(两列/网格/底部双分区) ----
    grid_rows = max(1, -(-grid_html.count("<div class='race-mkwrap'>") // 11))
    topbar_h = 102 + (22 if time_html else 0)
    # 按实际行数计高（两列并排，取行数多的一列），避免日后加行被低估
    stat_rows = max(stat_left.count("<div class='race-stat'>"), stat_right.count("<div class='race-stat'>"))
    stats_h = 16 + max(4, stat_rows) * 48  # 每行约 48px，最少按 4 行保底
    layout_h = max(254, stats_h)
    monkey_h = 30 + grid_rows * 88
    mod_items = len(common._race_modifier_items(meta.get("_bloonModifiers")))
    mod_body = 18 + max(1, -(-mod_items // 2)) * 34 if mod_items else 40
    # 规则面板实际高度 = 上下 padding 9×2 + 行 min-height 54 = 72；低估会把行内图标
    # 挤到 PDF 第 2 页（pdftoppm -singlefile 只取第 1 页，图标凭空消失）
    rules_body = 76
    bottom_h = 36 + 8 + max(mod_body, rules_body) + 10
    content_h = 28 + layout_h + 12 + monkey_h + bottom_h
    custom_panel_height = 0
    if custom_round_sets:
        # 面板高度跟随“回合组名称 + 明细行”增长，避免多行内容被截图裁掉。
        custom_line_count = len(custom_round_details) + 1
        custom_panel_height = max(92, 14 + 20 + max(54, custom_line_count * 30)) + 18
    frame_height = topbar_h + 12 + content_h + custom_panel_height + 20
    return common._race_shell(body, frame_height)


def _rules_compat_html(meta: dict, prefix: str, scoring: str, ev: dict | None) -> str:
    """保留旧卡片中的中文可检索信息，不改变新卡片的视觉布局。"""
    lines = [
        "猴子限制",
        f"初始资金 {int(meta.get('startingCash') or 0):,}",
        f"初始生命 {int(meta.get('lives') or 0):,}",
        f"最快用时 {scoring or '—'}",
        "气球强化 " + ("；".join(textfmt.bloon_mod_lines(meta.get("_bloonModifiers"))) or "默认"),
        "禁用项 " + ("、".join(label for key, label in i18n.FLAG_LABELS if meta.get(key)) or "无"),
    ]
    for tower in _race_visible_towers(meta.get("_towers")):
        raw = str(tower.get("tower") or "").strip()
        name = assets._tower_display_name(raw, bool(tower.get("isHero")))
        tags = []
        try:
            max_num = float(tower.get("max"))
        except (TypeError, ValueError):
            max_num = None
        if max_num is not None and 0 < max_num < 99:
            tags.append(f"×{int(max_num)}")
        blocked = {
            p: n for p in (1, 2, 3)
            if (n := int(tower.get(f"path{p}NumBlockedTiers") or 0)) != 0
        }
        if blocked:
            tags.append(_path_max_txt(blocked))
        lines.append(name + (" " + " ".join(tags) if tags else ""))
    if ev:
        state = util._state_of(ev, util.bucket_now())
        lines.extend([util._STATE_TXT[state], util._fmt_range(ev)])
    return f"<div class='compat-data'>{util._esc('；'.join(lines))}</div>"
