"""远征（Odyssey）难度卡片：队伍/奖励/可用单位/逐岛规则。"""
import re

from . import common
from .. import assets, i18n, util


def _odyssey_power_icon(raw: str) -> str:
    """远征力量名称 → PowerIcon 本地图标；未知新力量安全降级。"""
    raw = str(raw or "").strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", raw):
        return ""
    return assets._ui_asset_data_url(f"{raw}Icon.png")


def _odyssey_upgrade_caps(t: dict) -> str:
    """由 path*NumBlockedTiers 计算三路最大开放层级，例如 0/0/1 → 5-5-4；英雄固定满级不显示。"""
    if not isinstance(t, dict) or t.get("isHero"):
        return ""
    def cap(blocked) -> int:
        try:
            n = int(blocked or 0)
        except (TypeError, ValueError):
            return 5
        return 0 if n == -1 else max(0, 5 - n)
    return f"{cap(t.get('path1NumBlockedTiers'))}-{cap(t.get('path2NumBlockedTiers'))}-{cap(t.get('path3NumBlockedTiers'))}"


def _odyssey_top_icon(kind: str) -> str:
    """顶部蓝丝带用小图标：kind ∈ {lives, seats, towers}；缺失返回空串。"""
    mapping = {
        "lives":  ("game", "UI_LivesIcon.webp"),
        "seats":  ("game", "UI_HeroSeat.webp"),
        "towers": ("ui",   "monkey-cap.png"),
    }
    item = mapping.get(kind)
    if not item:
        return ""
    src, fname = item
    if src == "game":
        return assets._game_asset_data_url(fname)
    return assets._ui_asset_data_url(fname)


_ODYSSEY_REWARD_ICON = {
    # reward 标签 → (source, filename)；game 用 .webp, ui 用 .png
    "MonkeyMoney": ("game", "BloonjaminsIcon.webp"),
    "Trophy":      ("game", "UI_TrophyIcon.webp"),
}


def _odyssey_reward_icon(kind: str, sub: str) -> str:
    """奖励图标：MonkeyMoney / Trophy 用本地素材；Power/Insta 由调用方决定。"""
    base = _ODYSSEY_REWARD_ICON.get(kind)
    if base:
        src, fname = base
        if src == "game":
            return assets._game_asset_data_url(fname)
        return assets._ui_asset_data_url(fname)
    return ""


def _odyssey_map_icons() -> dict[str, str]:
    """岛屿行用小图标：金币 / 开始回合 / 难度脸谱（按当前难度选取）。"""
    icons = {
        "coin":   assets._game_asset_data_url("UI_CoinIcon.webp"),
        "play":   assets._ui_asset_data_url("start-round.png"),
    }
    return icons


def _odyssey_img(data_url: str, cls: str, fallback: str, alt: str = "") -> str:
    if data_url:
        return f"<img class='{cls}' src='{util._esc(data_url)}' alt='{util._esc(alt)}'/>"
    return f"<span class='{cls}-fallback'>{util._esc(fallback)}</span>"


def _odyssey_tower_lookup(meta: dict) -> dict[str, dict]:
    return {
        str(t.get("tower") or "").strip(): t
        for t in meta.get("_availableTowers") or []
        if isinstance(t, dict) and str(t.get("tower") or "").strip()
    }


def _odyssey_tower_card(raw: str, is_hero: bool, count_text: str = "",
                        classes: str = "", category: str = "",
                        upgrade_caps: str = "",
                        badge_pos: str = "right") -> str:
    name = assets._tower_display_name(raw, is_hero)
    icon = assets._tower_icon(raw, is_hero)
    cat = category or assets._tower_category(raw, is_hero)
    card_classes = "ody-unit-card" + (" hero" if is_hero else "") + f" cat-{cat}"
    if classes:
        card_classes += " " + classes
    if upgrade_caps:
        card_classes += " with-caps"
    content = _odyssey_img(icon, "ody-unit-icon", name, name)
    if not icon:
        content = f"<span class='ody-unit-fallback'>{util._esc(name)}</span>"
    if count_text:
        qcls = "ody-unit-quantity" + (" left" if badge_pos == "left" else "")
        content += f"<span class='{qcls}'>{util._esc(count_text)}</span>"
    if upgrade_caps:
        content += f"<span class='ody-unit-caps'>{util._esc(upgrade_caps)}</span>"
    return f"<div class='ody-unit-wrap'><div class='{card_classes}' title='{util._esc(name)}'>{content}</div></div>"


def _odyssey_default_crew_html(meta: dict) -> str:
    available = _odyssey_tower_lookup(meta)
    defaults = [x for x in meta.get("_defaultTowers") or [] if isinstance(x, dict)]
    hero_item = next((x for x in defaults if available.get(str(x.get("name") or ""), {}).get("isHero")), None)
    hero_html = "<span class='ody-unit-fallback'>无英雄</span>"
    if hero_item:
        raw = str(hero_item.get("name") or "").strip()
        info = available.get(raw) or {}
        quantity = int(hero_item.get("quantity") or 0)
        denom = int(info.get("max") or 1)
        hero_html = _odyssey_tower_card(raw, True, f"{quantity}/{denom}", "big",
                                        category="hero")

    # 对照游戏内活动页：英雄左侧放大，猴子单行排列在右侧，圆形分类色底 + 左上角数量角标
    tower_html = []
    for item in defaults:
        raw = str(item.get("name") or "").strip()
        if not raw or item is hero_item:
            continue
        info = available.get(raw) or {}
        quantity = int(item.get("quantity") or 0)
        max_count = info.get("max")
        denom = int(max_count) if isinstance(max_count, (int, float)) and max_count > 0 else quantity
        img = assets._tower_portrait(raw)
        face = (f"<img src='{img}' style='width:38px;height:38px;object-fit:contain;'/>" if img
                else f"<span style='font-size:8px;color:#ffffff;line-height:10px;'>{util._esc(i18n.tower_cn(raw))}</span>")
        cat = common._rush_tower_category(raw)
        c0, c1 = common._TOWER_CAT_COLORS.get(cat, ("#c67b27", "#8a5630"))
        tower_html.append(
            "<span style='position:relative;display:inline-block;width:49px;margin:10px 1px 0 0;"
            "vertical-align:top;text-align:center;'>"
            "<span style='position:absolute;top:-9px;left:50%;width:30px;margin-left:-15px;z-index:2;"
            "background:#1596d2;border:2px solid #e7f8ff;border-radius:6px;color:#ffffff;"
            f"font-size:10px;line-height:14px;font-weight:900;text-align:center;'>{quantity}/{denom}" + "</span>"
            "<span style='display:flex;align-items:center;justify-content:center;width:46px;height:46px;"
            "border-radius:10px;background:linear-gradient(180deg," + c0 + " 0%," + c1 + " 100%);"
            "border:2px solid rgba(0,0,0,.3);box-shadow:inset 0 1px 0 rgba(255,255,255,.4);'>" + face + "</span></span>")
    return ("<div class='ody-panel ody-crew-panel'>"
            "<div class='ody-ribbon ody-panel-title'><span>默认队伍</span></div>"
            "<div class='ody-crew-body'><div class='ody-crew-hero'>" + hero_html +
            "</div><div class='ody-crew-grid-cell'><div class='ody-default-grid' style='white-space:nowrap;'>"
            + "".join(tower_html) + "</div></div></div></div>")


def _odyssey_available_html(meta: dict) -> str:
    towers = [t for t in meta.get("_availableTowers") or []
              if isinstance(t, dict) and str(t.get("tower") or "").strip() and t.get("max") != 0]
    heroes = [t for t in towers if t.get("isHero")]
    regular = [t for t in towers if not t.get("isHero")]
    hero_html = "".join(
        _odyssey_tower_card(str(t.get("tower")), True, "", "available",
                            category="hero",
                            upgrade_caps=_odyssey_upgrade_caps(t),
                            badge_pos="left")
        for t in heroes
    )
    tower_html = "".join(
        _odyssey_tower_card(
            str(t.get("tower")), False,
            str(int(t.get("max"))) if isinstance(t.get("max"), (int, float)) and t.get("max") > 0 else "∞",
            "available",
            category=assets._tower_category(str(t.get("tower")), False),
            upgrade_caps=_odyssey_upgrade_caps(t),
            badge_pos="left",
        )
        for t in regular
    )
    powers = [p for p in meta.get("_availablePowers") or []
              if isinstance(p, dict) and str(p.get("power") or "").strip() and p.get("max")]
    power_html = []
    for power in powers:
        raw = str(power.get("power") or "").strip()
        icon = _odyssey_power_icon(raw)
        name = i18n._odyssey_power_name(raw)
        icon_html = _odyssey_img(icon, "ody-power-icon", name, name)
        if not icon:
            icon_html = f"<span class='ody-power-fallback'>{util._esc(name)}</span>"
        count = int(power.get("max") or 0)
        power_html.append(
            f"<div class='ody-power-wrap'><div class='ody-power-tile' title='{util._esc(name)}'>"
            f"{icon_html}<span class='ody-power-count'>{count}</span></div></div>"
        )
    # 面板直接作为表格单元格：同行单元格天然等高，三面板底部严格对齐
    return ("<div class='ody-available'>"
            "<div class='ody-panel ody-av-panel heroes'>"
            f"<div class='ody-av-title ody-av-title-dark'>可用英雄：</div>"
            f"<div class='ody-hero-grid'>{hero_html or '—'}</div></div>"
            "<div class='ody-panel ody-av-panel towers'>"
            f"<div class='ody-av-title ody-av-title-dark'>可用猴子：</div>"
            f"<div class='ody-tower-grid'>{tower_html or '—'}</div></div>"
            "<div class='ody-panel ody-av-panel powers'>"
            f"<div class='ody-av-title ody-av-title-dark'>可用力量：</div>"
            f"<div class='ody-power-grid'>{''.join(power_html) or '—'}</div></div>"
            "</div>")


def _odyssey_rewards_html(rewards: list) -> str:
    items = []
    for reward in rewards or []:
        raw = str(reward or "")
        if raw.startswith("MonkeyMoney:"):
            value = raw.split(":", 1)[1]
            icon = _odyssey_reward_icon("MonkeyMoney", value)
            icon_html = _odyssey_img(icon, "ody-reward-icon", "💵", "猴币")
            if not icon:
                icon_html = "<span class='ody-reward-emoji'>💵</span>"
            items.append(f"<div class='ody-reward-cell'>{icon_html}"
                         f"<div class='ody-reward-value'>{util._esc(value)}</div></div>")
        elif raw.startswith("Trophy:"):
            value = raw.split(":", 1)[1]
            icon = _odyssey_reward_icon("Trophy", value)
            icon_html = _odyssey_img(icon, "ody-reward-icon", "🏆", "奖杯")
            if not icon:
                icon_html = "<span class='ody-reward-emoji'>🏆</span>"
            items.append(f"<div class='ody-reward-cell'>{icon_html}"
                         f"<div class='ody-reward-value'>{util._esc(value)}</div></div>")
        elif raw.startswith("Power:"):
            power = raw.split(":", 1)[1]
            icon = _odyssey_power_icon(power)
            icon_html = _odyssey_img(icon, "ody-reward-icon", i18n._odyssey_power_name(power), i18n._odyssey_power_name(power))
            if not icon:
                icon_html = "<span class='ody-reward-emoji'>⚡</span>"
            items.append(f"<div class='ody-reward-cell'>{icon_html}"
                         f"<div class='ody-reward-value'>{util._esc(i18n._odyssey_power_name(power))}</div></div>")
        elif raw.startswith("InstaMonkey:"):
            spec = raw.split(":", 1)[1]
            tower, _, tiers = spec.partition(",")
            tier_txt = "-".join(tiers) if tiers else ""
            icon = ""
            if re.fullmatch(r"[0-9]{3}", tiers):
                # 塔立绘只体现最高单路外观：3-0-2 → 300-SpikeFactory、0-4-2 → 040-EngineerMonkey
                best = max(range(3), key=lambda i: int(tiers[i]))
                combo = ["0", "0", "0"]
                combo[best] = tiers[best]
                if int("".join(combo)) > 0:
                    icon = assets._game_asset_data_url(f"{''.join(combo)}-{tower}.webp")
            if not icon:
                icon = assets._tower_icon(tower, False)
            icon_html = _odyssey_img(icon, "ody-reward-icon", i18n.tower_cn(tower), i18n.tower_cn(tower))
            value_txt = "即时猴" + (f" {tier_txt}" if tier_txt else "")
            items.append(f"<div class='ody-reward-cell'>{icon_html}"
                         f"<div class='ody-reward-value'>{util._esc(value_txt)}</div></div>")
    return ("<div class='ody-panel ody-reward-panel'>"
            "<div class='ody-ribbon ody-panel-title'><span>奖励</span></div>"
            "<div class='ody-reward-grid'>" + ("".join(items) or "<div class='ody-reward-value'>无</div>") +
            "</div></div>")


def _odyssey_map_rule_text(mp: dict) -> str:
    modifiers = common._race_modifier_items(mp.get("_bloonModifiers"))
    details = [f"{label} {value}" for label, value, _icon in modifiers]
    custom_rounds = [str(x) for x in mp.get("roundSets") or [] if str(x).casefold() != "default"]
    if custom_rounds:
        details.append("自定义回合")
    for key, label in i18n.FLAG_LABELS:
        if mp.get(key):
            details.append(label)
    return "默认规则 · 无强化" if not details else " · ".join(details)


def _odyssey_maps_html(maps: list) -> str:
    icons = _odyssey_map_icons()
    rows = []
    for mp in maps or []:
        thumb = (f"<img class='ody-map-img' src='{util._esc(mp['img'])}' alt='{util._esc(mp.get('map') or mp.get('name') or '')}'/>"
                 if mp.get("img") else "<div class='ody-map-empty'>暂无地图图像</div>")
        difficulty = i18n.cn(mp.get("difficulty"), i18n.DIFFICULTY_CN) or "未知难度"
        mode = i18n.cn(mp.get("mode"), i18n.MODE_CN) or "标准"
        start_round = int(mp.get("startRound") or 0)
        end_round = int(mp.get("endRound") or 0)
        rounds = f"{start_round}/{end_round}" if start_round or end_round else "—"
        rule = _odyssey_map_rule_text(mp)
        # 难度脸谱：Beginner/Intermediate/Advanced/Expert 对应四张脸谱
        diff_face = assets._ui_asset_data_url(f"Map{_ODIFF_TO_BTN.get(mp.get('difficulty'), 'Beginner')}Btn.png")
        coin_img = _odyssey_img(icons.get("coin", ""), "ody-mini-icon", "🪙", "金币")
        if not icons.get("coin"):
            coin_img = "<span class='ody-coin'>🪙</span>"
        play_img = _odyssey_img(icons.get("play", ""), "ody-mini-icon", "▶", "开始")
        if not icons.get("play"):
            play_img = "<span class='ody-play'>▶</span>"
        diff_img = _odyssey_img(diff_face, "ody-mini-icon", "●", difficulty)
        if not diff_face:
            diff_img = "<span class='ody-diff'>●</span>"
        rows.append(
            "<div class='ody-map-row'><div class='ody-map-img-cell'>" + thumb +
            "</div><div class='ody-map-info'>"
            "<div class='ody-map-meta'>"
            f"<div class='ody-map-meta-item'>{coin_img} {int(mp.get('startingCash') or 0):,}</div>"
            f"<div class='ody-map-meta-item'>{play_img}{util._esc(rounds)}</div>"
            f"<div class='ody-map-meta-item'>{diff_img}{util._esc(difficulty)} / {util._esc(mode)}</div>"
            "</div>"
            f"<div class='ody-map-rule'>{util._esc(rule)}</div>"
            "</div></div>"
        )
    return "<div class='ody-maps'>" + ("".join(rows) or "<div class='ody-panel ody-map-empty'>暂无远征地图</div>") + "</div>"


_ODIFF_TO_BTN = {"Beginner": "Beginner", "Easy": "Beginner",
                 "Intermediate": "Intermediate", "Medium": "Intermediate",
                 "Advanced": "Advanced", "Hard": "Advanced",
                 "Expert": "Expert", "Impoppable": "Expert"}


# 难度选项卡（Explorer 远征页三难度，选中黄底）
_ODYSSEY_TABS = (("easy", "简单", "MapBeginnerBtn.png"),
                 ("medium", "中等", "MapIntermediateBtn.png"),
                 ("hard", "困难", "MapAdvancedBtn.png"))


def _odyssey_tabs_html(current: str) -> str:
    """三难度选项卡：当前难度黄色高亮，脸谱图标取本地 UI 素材。"""
    cells = []
    for key, label, face_file in _ODYSSEY_TABS:
        face = assets._ui_asset_data_url(face_file)
        if face:
            icon = f"<img class='ody-tab-icon' src='{util._esc(face)}'/>"
        else:
            icon = "<span class='ody-tab-fallback'>●</span>"
        sel = " sel" if key == current else ""
        cells.append(f"<div class='ody-tab-cell'><div class='ody-tab{sel}'>"
                     f"<div class='ody-tab-icon-cell'>{icon}</div>"
                     f"<div class='ody-tab-label'>{util._esc(label)}</div></div></div>")
    return "<div class='ody-tabs'>" + "".join(cells) + "</div>"


def _odyssey_card_height(meta: dict | None, maps_count: int) -> int:
    """远征单难度卡片高度估算（与 _odyssey_shell 各分区高度对应）；
    handle_odyssey 用它取三难度最大值做统一画布，保证 QQ 预览显示宽度一致。"""
    if not meta:
        return 260
    at = [t for t in meta.get("_availableTowers") or []
          if isinstance(t, dict) and t.get("max") != 0]
    heroes = sum(1 for t in at if t.get("isHero"))
    regular = len(at) - heroes
    power_count = sum(1 for p in meta.get("_availablePowers") or []
                      if isinstance(p, dict) and p.get("max"))
    rows = max(1, -(-heroes // 2), -(-regular // 4), -(-power_count // 3))
    is_ext = bool(meta.get("isExtreme"))
    # 羊皮纸横幅 + 难度选项卡 + 丝带 + 队伍/奖励 + 可用网格 + 岛屿规则行
    return (12 + 62 + 10 + 58 + 10 + 38 + 24 + (22 if is_ext else 0) + 222 + 10
            + max(245, 42 + rows * 82) + 10 + 46 + 10
            + max(1, maps_count) * 130 + 40)


def odyssey_diff_html(col: dict, d: str, label: str) -> str:
    """远征单张难度卡片：按游戏内远征页布局展示队伍、奖励、猴子、力量和逐岛规则。"""
    if col.get("empty"):
        return common._odyssey_shell(f"<div class='ody-panel ody-map-empty'>{util._esc(col['empty'])}</div>", 260, theme="tan")
    ev = col["ev"]
    diff = col["diffs"].get(d) or {}
    meta = diff.get("meta")
    if not meta:
        return common._odyssey_shell(f"<div class='ody-event'>{util._esc((ev.get('name') or '').strip())} · {label}难度</div>"
                              "<div class='ody-panel ody-map-empty'>（该难度数据缺失）</div>", 260, theme="tan")

    lives = int(meta.get("startingHealth") or 0)
    seats = int(meta.get("maxMonkeySeats") or 0)
    towers_cap = int(meta.get("maxMonkeysOnBoat") or 0)
    state = util._STATE_TXT[util._state_of(ev, util.bucket_now())]
    event_name = (ev.get("name") or "远征活动").strip()
    description = (ev.get("description") or "").strip()
    is_extreme = bool(meta.get("isExtreme"))
    lives_icon = _odyssey_top_icon("lives")
    seats_icon = _odyssey_top_icon("seats")
    towers_icon = _odyssey_top_icon("towers")
    lives_img = f"<img class='ody-ribbon-icon' src='{util._esc(lives_icon)}'/>" if lives_icon else "❤"
    seats_img = f"<img class='ody-ribbon-icon' src='{util._esc(seats_icon)}'/>" if seats_icon else "🪑"
    towers_img = f"<img class='ody-ribbon-icon' src='{util._esc(towers_icon)}'/>" if towers_icon else "🐵"
    # 羊皮纸标题横幅（活动名 + 描述）+ 三难度选项卡（当前难度选中）
    banner = (f"<div class='ody-title-banner'><div class='ody-title'>{util._esc(event_name)}</div>"
              + (f"<div class='ody-title-sub'>{util._esc(description)}</div>" if description else "")
              + "</div>")
    top = (
        banner
        + _odyssey_tabs_html(d)
        + "<div class='ody-ribbons'>"
        f"<div class='ody-ribbon-cell'><div class='ody-ribbon'>{lives_img} 生命：{lives}</div></div>"
        f"<div class='ody-ribbon-cell'><div class='ody-ribbon'>{seats_img} 猴位：{seats}</div></div>"
        f"<div class='ody-ribbon-cell'><div class='ody-ribbon'>{towers_img} 猴子上限：{towers_cap}</div></div>"
        "</div>"
        + ("<div class='ody-extreme-badge'>极限模式</div>" if is_extreme else "")
        + "<div class='ody-top-grid'><div class='ody-top-cell crew'>"
        f"{_odyssey_default_crew_html(meta)}"
        "</div><div class='ody-top-cell reward'>"
        f"{_odyssey_rewards_html(meta.get('_rewards') or [])}"
        "</div></div>"
        f"{_odyssey_available_html(meta)}"
        "<div style='text-align:center;margin:14px auto 8px;'>"
        "<span style='position:relative;display:inline-block;padding:4px 26px;"
        "background:linear-gradient(180deg,#46c8f1 0%,#129ed0 56%,#087eaf 100%);"
        "border:2px solid #076b99;border-radius:2px;color:#ffffff;font-size:15px;"
        "font-weight:900;letter-spacing:0.5px;text-shadow:0 2px 0 #075b8b;"
        "box-shadow:inset 0 1px 0 rgba(255,255,255,.75),0 2px 0 rgba(0,59,79,.28);'>岛屿规则</span></div>"
        f"{_odyssey_maps_html(diff.get('maps') or [])}"
        + "<div class='ody-event-desc' style='margin-top:6px;text-align:center;'>"
        + f"{util._esc(label)}难度 · {util._esc(util._fmt_range(ev))} · {util._esc(state)}"
        + "</div>"
    )
    height = _odyssey_card_height(meta, len(diff.get("maps") or []))
    # 由调用方传入统一高度时，直接使用以保证三图在 QQ 预览中显示宽度一致（QQ 按最大边缩放，较矮的图会被等比放大导致视觉宽度不一）
    uh = diff.get("_unified_h")
    if isinstance(uh, int) and uh > height:
        height = uh
    return common._odyssey_shell(top, height, theme="tan")
