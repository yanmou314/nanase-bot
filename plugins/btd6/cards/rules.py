"""竞赛/Boss 规则卡片（游戏内挑战详情页风格）与争夺领土卡片。"""
import re

from . import common
from .. import assets, i18n, rushgen, textfmt, util


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
    race_asset = "RaceIcon.png"
    if assets._ui_asset_data_url(race_asset):
        return common._race_ui_img(race_asset, "🏆", "race-emblem-img")
    return f"<div class='race-emblem-fallback'>{util._esc(fallback)}</div>"


def _race_title(name: str, ev: dict | None, side_img: str) -> str:
    raw = str(name or "").strip()
    if side_img and ev:
        boss = i18n.boss_cn(ev.get("bossType"))
        tier = re.search(r"(\d+)\s*$", raw)
        return f"{boss} {tier.group(1)}" if tier else boss
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
    """把已知回合组展开为“第几回合：出现什么气球”，未知组保留可读降级。"""
    details = []
    for key in _custom_round_set_keys(meta):
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


def _ct_html(ev: dict, tiles: list, now: int) -> str:
    """争夺领土专属卡片：Explorer 详情页版式，展示当前 CT 事件的领地与时间。"""
    name = (ev.get("id") or "CT").strip()
    state = util._state_of(ev, now)
    # 统计 tiles
    total = len(tiles) if isinstance(tiles, list) else 0
    # 取前 6 个 tile 展示
    tile_preview = ""
    if tiles:
        # 简化：显示 tile 类型统计
        from collections import Counter
        cnt = Counter(x.get("type") for x in tiles if isinstance(x, dict))
        preview = " / ".join(f"{k}:{v}" for k, v in list(cnt.items())[:4])
        tile_preview = f"领地 {total} 块 · {preview}" if preview else f"领地 {total} 块"
    else:
        tile_preview = "领地数据加载中"
    ct_img = assets._ui_asset_data_url("ct-event.png") or assets._ui_asset_data_url("ct-tile.png") or ""
    if ct_img:
        emblem = f"<img src='{util._esc(ct_img)}'/>"
    else:
        emblem = "<div class='race-emblem-fallback'>🏴</div>"
    title = "争夺领土"
    subtitle = f"CT {name}"
    time_line = util._fmt_range(ev)
    player = int(ev.get("totalScores_player") or 0)
    team = int(ev.get("totalScores_team") or 0)
    body = (f"<div class='race-topbar'><div class='race-head'><div class='race-emblem-cell'><div class='race-emblem'>{emblem}</div></div>"
            f"<div class='race-title-cell'><div class='race-title'>{util._esc(title)}</div><div class='race-subtitle'>{util._esc(subtitle)}</div><div class='race-time'>{util._esc(time_line)} · {util._STATE_TXT[state]}</div></div></div></div>"
            f"<div class='race-content'><div class='race-layout'><div class='race-map'><div class='race-map-empty'>🗺️</div></div>"
            f"<div class='race-stats-cell'><div class='race-stats'><div class='race-stat-col'>"
            f"<div class='race-stat'><div class='race-stat-icon-cell'>{common._race_ui_img('ct-event.png', '🏴', 'race-stat-icon')}</div><div class='race-stat-copy'><div class='race-stat-label'>个人参与</div><div class='race-stat-value'>{player:,}</div></div></div>"
            f"<div class='race-stat'><div class='race-stat-icon-cell'>{common._race_ui_img('ct-tile.png', '🧩', 'race-stat-icon')}</div><div class='race-stat-copy'><div class='race-stat-label'>战队参与</div><div class='race-stat-value'>{team:,}</div></div></div>"
            f"</div><div class='race-stat-col'>"
            f"<div class='race-stat'><div class='race-stat-icon-cell'>{common._race_ui_img('ct-event.png', '⏱', 'race-stat-icon')}</div><div class='race-stat-copy'><div class='race-stat-label'>活动状态</div><div class='race-stat-value'>{util._STATE_TXT[state]}</div></div></div>"
            f"<div class='race-stat'><div class='race-stat-icon-cell'>{common._race_ui_img('ct-tile.png', '🗺️', 'race-stat-icon')}</div><div class='race-stat-copy'><div class='race-stat-label'>领地</div><div class='race-stat-value'>{total} 块</div></div></div>"
            f"</div></div></div></div></div>"
            f"<div class='race-monkey-section'><div class='race-options'><div class='race-available'>{util._esc(tile_preview)}</div></div></div></div>")
    return common._race_shell(body, 560)


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


def _daily_monkey_grid(meta: dict) -> str:
    """每日挑战：全塔总览——官方 _towers 里有记录的塔带限制角标，
    未记录的塔表示无限制（正常可放、可满级），max=0 的塔直接不显示。
    修复：原先仅遍历 towersInOrder（26 猴子），漏掉 heroesInOrder 导致英雄永不显示；
    -1 视为无限制（与游戏内 9999 同义），不应按限购处理。"""
    restrictions = {}
    for t in meta.get("_towers") or []:
        raw = str(t.get("tower") or "").strip()
        if raw and raw != "ChosenPrimaryHero":
            restrictions[raw] = t
    constants = rushgen.load_constants()
    # 英雄放首位 + 去重：初级/高级的英雄默认 1 不打 ×1 角标，且应排在最前
    order = list(constants.get("heroesInOrder") or []) + list(constants.get("towersInOrder") or [])
    cells = []
    hero_cells = []
    monkey_cells = []
    for name in order:
        entry = dict(restrictions.get(name) or {"tower": name})
        entry.setdefault("tower", name)
        if "isHero" not in entry:
            entry["isHero"] = name in (constants.get("heroesInOrder") or [])
        # 英雄默认 1 不视为限购：不打 ×1
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
    cells = hero_cells + monkey_cells
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
def _race_monkey_grid(towers: list) -> str:
    cells = [_race_monkey_cell(tower) for tower in _race_visible_towers(towers)]
    if not cells:
        return "<div class='mkgrid race-mkgrid'><div class='race-mk-fallback'>无</div></div>"
    return "<div class='mkgrid race-mkgrid'>" + "".join(cells) + "</div>"


def _stat(label: str, value: str) -> str:
    return f"<div class='st'>{util._esc(label)} <b>{util._esc(value)}</b></div>"


def rules_html(col: dict) -> str:
    if col.get("empty"):
        body = ("<div class='race-topbar'></div>"
                f"<div class='race-content'><div class='race-map-empty'>{util._esc(col['empty'])}</div></div>")
        return common._race_shell(body, 330)
    meta = col["meta"]
    name = (meta.get("name") or "").strip()
    diff = i18n.cn(meta.get("difficulty"), i18n.DIFFICULTY_CN)
    mode = i18n.cn(meta.get("mode"), i18n.MODE_CN)
    scoring = col.get("scoring_cn") or ""
    prefix = col.get("prefix") or ""
    is_daily = prefix.startswith("每日")
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
    stat_left = "".join([
        stat("cash.png", "🪙", "初始资金", f"{int(meta.get('startingCash') or 0):,}"),
        stat("heart.png", "❤", "初始生命", f"{int(meta.get('lives') or 0):,}"),
        stat("heart.png", "❤", "最大生命", f"{int(meta.get('maxLives') or 0):,}"),
        stat(event_icon, "📅" if is_daily else "⚑", boss_label),
    ])
    stat_right = "".join([
        stat("start-round.png", "▶", "开始回合", str(int(meta.get('startRound') or 0))),
        stat("end-round.png", "⏭", "结束回合", str(int(meta.get('endRound') or 0))),
        stat("monkey-cap.png", "🐒", "最大猴子", towers_cap),
        stat("fastest-time.png", "⏱", "最快用时"),
    ])
    emblem = _race_emblem(ev, side_img, "📅" if is_daily else "🏆", is_daily=is_daily)
    title = _race_title(name, ev, side_img)
    time_line = _race_time_line(ev)
    time_html = f"<div class='race-time'>{util._esc(time_line)}</div>" if time_line else ""
    body = ("<div class='race-topbar'><div class='race-head'>"
            f"<div class='race-emblem-cell'><div class='race-emblem'>{emblem}</div></div>"
            f"<div class='race-title-cell'><div class='race-title'>{util._esc(title)}</div>"
            f"<div class='race-subtitle'>{util._esc(subtitle)}</div>"
            f"{time_html}</div>"
            "</div></div>")
    map_alt = i18n.map_cn(str(meta.get("map") or "").strip()) or "map"
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
    bottom = ("<div class='race-bottom'><div class='race-bottom-left'>"
              "<div class='race-panel-head'>气球强化</div><div class='race-panel-body'>"
              f"{modifier_html}</div></div>"
              "<div class='race-bottom-right'><div class='race-panel-head'>规则</div>"
              f"<div class='race-panel-body'><div class='race-rule-row'>"
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
