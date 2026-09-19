"""远征（Odyssey）统一难度卡片：三难度合卡 + 可用单位 + 逐岛规则。

版式约定（改前先看本段）：
- 一张图同时展示 简单/中等/困难，顶栏三框颜色固定：蓝 / 红 / 黑。
- 不再单独渲染「默认队伍」。
- 可用英雄/猴子/力量图标从左到右排列（不居中）。
- 岛屿规则默认展示困难难度的地图列表；每张图用左缘色条标明
  该图属于哪些难度（蓝/红/黑），便于对照上方三框。
- 金钱/回合/难度信息靠左贴地图；气球强化用图标（与每日挑战同素材），
  不以长文字罗列。
"""
import re

from . import common
from .. import assets, i18n, util

# 三难度固定色：与顶栏框一致，地图左缘色条复用
_DIFF_KEY_ORDER = ("easy", "medium", "hard")
_DIFF_LABEL = {"easy": "简单", "medium": "中等", "hard": "困难"}
_DIFF_FACE = {
    # BTD6 API Explorer 同款月桂难度徽章
    "easy": ("game", "OdysseyModeEasyBtn.webp"),
    "medium": ("game", "OdysseyModeMediumBtn.webp"),
    "hard": ("game", "OdysseyModeHardBtn.webp"),
}

# 力量容器三色（与 Explorer 一致）：默认座 / IAP / Pro
_POWER_KIND = {
    "BattleCat": "iap", "SheRa": "iap", "Skeletor": "iap", "SwordOfPower": "iap",
    "BananaFarmerPro": "pro", "SuperMonkeyBeacon": "pro", "MonkeyBoostPro": "pro",
    "TechBotPrime": "pro", "PortableLakePro": "pro",
}
_POWER_BG = {
    "default": "PowerSeat.webp",
    "iap": "PowerIAPContainer.webp",
    "pro": "PowersProContainer.webp",
}
# 与 BTD6 API Explorer 一致：目录全量渲染；仅「已列出且 max==0」跳过。
_POWER_CATALOG = (
    ("CashDrop", "default"), ("RoadSpikes", "default"), ("MonkeyBoost", "default"),
    ("BananaFarmer", "default"), ("CamoTrap", "default"), ("Pontoon", "default"),
    ("SuperMonkeyStorm", "default"), ("GlueTrap", "default"), ("PortableLake", "default"),
    ("MoabMine", "default"), ("Thrive", "default"), ("EnergisingTotem", "default"),
    ("DartTime", "default"), ("TechBot", "default"), ("CaveMonkey", "default"),
    ("BattleCat", "iap"), ("SheRa", "iap"), ("Skeletor", "iap"), ("SwordOfPower", "iap"),
    ("BananaFarmerPro", "pro"), ("SuperMonkeyBeacon", "pro"), ("MonkeyBoostPro", "pro"),
    ("TechBotPrime", "pro"), ("PortableLakePro", "pro"),
)
# 明显区分：蓝 / 红 / 黑
_DIFF_THEME = {
    "easy": {
        "grad": "linear-gradient(180deg,#5eb7f0 0%,#2f86c8 55%,#1a5f96 100%)",
        "border": "#0d4a78",
        "ink": "#ffffff",
        "bar": "#2d7cc0",
    },
    "medium": {
        "grad": "linear-gradient(180deg,#f0715e 0%,#d63a14 55%,#a02800 100%)",
        "border": "#7a1a00",
        "ink": "#ffffff",
        "bar": "#d63a14",
    },
    "hard": {
        "grad": "linear-gradient(180deg,#4a4a4a 0%,#1f1f1f 55%,#000000 100%)",
        "border": "#000000",
        "ink": "#ffffff",
        "bar": "#1a1a1a",
    },
}


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
    """难度框内小图标：kind ∈ {lives, seats, towers}；缺失返回空串。"""
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
    """岛屿行用小图标：金币 / 开始回合。"""
    return {
        "coin": assets._game_asset_data_url("UI_CoinIcon.webp"),
        "play": assets._ui_asset_data_url("start-round.png"),
    }


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


def _odyssey_available_html(meta: dict) -> str:
    """可用英雄/猴子/力量三面板；图标从左到右排列。"""
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
    avail = {}
    for p in meta.get("_availablePowers") or []:
        if isinstance(p, dict) and str(p.get("power") or "").strip():
            avail[str(p.get("power") or "").strip()] = p.get("max")
    power_html = []
    for raw, default_kind in _POWER_CATALOG:
        if raw in avail and (avail[raw] is None or int(avail[raw] or 0) == 0):
            continue
        kind = _POWER_KIND.get(raw, default_kind)
        bg = assets._game_asset_data_url(_POWER_BG[kind])
        icon = _odyssey_power_icon(raw)
        name = i18n._odyssey_power_name(raw)
        icon_html = _odyssey_img(icon, "ody-power-icon", name, name)
        if not icon:
            icon_html = f"<span class='ody-power-fallback'>{util._esc(name)}</span>"
        count = avail.get(raw)
        count_html = ""
        if count is not None and int(count or 0) > 0:
            count_html = f"<span class='ody-power-count'>{int(count)}</span>"
        # 六边形底座图（角透明）出形；tile 本身不能铺矩形底色
        seat_html = (
            f"<img class='ody-power-seat' src='{util._esc(bg)}' alt=''/>"
            if bg else ""
        )
        # 座图缺失时用 CSS clip-path 兜底（部分渲染器支持）
        power_html.append(
            f"<div class='ody-power-wrap'>"
            f"<div class='ody-power-tile ody-power-{kind}' title='{util._esc(name)}'>"
            f"{seat_html}{icon_html}{count_html}</div></div>"
        )
    return ("<div class='ody-available'>"
            "<div class='ody-panel ody-av-panel heroes'>"
            f"<div class='ody-av-title ody-av-title-dark'>可用英雄：</div>"
            f"<div class='ody-hero-grid ody-grid-ltr'>{hero_html or '—'}</div></div>"
            "<div class='ody-panel ody-av-panel towers'>"
            f"<div class='ody-av-title ody-av-title-dark'>可用猴子：</div>"
            f"<div class='ody-tower-grid ody-grid-ltr'>{tower_html or '—'}</div></div>"
            "<div class='ody-panel ody-av-panel powers'>"
            f"<div class='ody-av-title ody-av-title-dark'>可用力量：</div>"
            f"<div class='ody-power-grid ody-grid-ltr'>{''.join(power_html) or '—'}</div></div>"
            "</div>")


def _odyssey_reward_cells(rewards: list) -> str:
    """单难度框内的奖励图标行（竖列下方）。"""
    items = []
    for reward in rewards or []:
        raw = str(reward or "")
        if raw.startswith("MonkeyMoney:"):
            value = raw.split(":", 1)[1]
            icon = _odyssey_reward_icon("MonkeyMoney", value)
            icon_html = _odyssey_img(icon, "ody-diff-reward-icon", "💵", "猴币")
            items.append(f"<div class='ody-diff-reward'>{icon_html}"
                         f"<span class='ody-diff-reward-val'>{util._esc(value)}</span></div>")
        elif raw.startswith("Trophy:"):
            value = raw.split(":", 1)[1]
            icon = _odyssey_reward_icon("Trophy", value)
            icon_html = _odyssey_img(icon, "ody-diff-reward-icon", "🏆", "奖杯")
            items.append(f"<div class='ody-diff-reward'>{icon_html}"
                         f"<span class='ody-diff-reward-val'>{util._esc(value)}</span></div>")
        elif raw.startswith("Power:"):
            power = raw.split(":", 1)[1]
            icon = _odyssey_power_icon(power)
            name = i18n._odyssey_power_name(power)
            icon_html = _odyssey_img(icon, "ody-diff-reward-icon", name, name)
            items.append(f"<div class='ody-diff-reward' title='{util._esc(name)}'>{icon_html}</div>")
        elif raw.startswith("InstaMonkey:"):
            spec = raw.split(":", 1)[1]
            tower, _, tiers = spec.partition(",")
            tier_txt = "-".join(tiers) if tiers else ""
            icon = ""
            if re.fullmatch(r"[0-9]{3}", tiers):
                best = max(range(3), key=lambda i: int(tiers[i]))
                combo = ["0", "0", "0"]
                combo[best] = tiers[best]
                if int("".join(combo)) > 0:
                    icon = assets._game_asset_data_url(f"{''.join(combo)}-{tower}.webp")
            if not icon:
                icon = assets._tower_icon(tower, False)
            name = i18n.tower_cn(tower)
            icon_html = _odyssey_img(icon, "ody-diff-reward-icon", name, name)
            tier_html = f"<span class='ody-diff-reward-val'>{util._esc(tier_txt)}</span>" if tier_txt else ""
            items.append(
                f"<div class='ody-diff-reward' title='{util._esc(name)} {util._esc(tier_txt)}'>"
                f"{icon_html}{tier_html}</div>")
    return "".join(items) or "<div class='ody-diff-reward-none'>无</div>"


def _odyssey_rewards_html(rewards: list) -> str:
    """兼容旧调用：奖励面板（统一卡已改用 _odyssey_reward_cells）。"""
    return ("<div class='ody-panel ody-reward-panel'>"
            "<div class='ody-ribbon ody-panel-title'><span>奖励</span></div>"
            "<div class='ody-reward-grid'>" + _odyssey_reward_cells(rewards) +
            "</div></div>")


def _odyssey_diff_box(key: str, meta: dict | None) -> str:
    """顶栏三难度框：标题 + 竖排生命/猴位/猴子上限 + 奖励。"""
    theme = _DIFF_THEME[key]
    label = _DIFF_LABEL[key]
    src, fname = _DIFF_FACE[key]
    face = assets._game_asset_data_url(fname) if src == "game" else assets._ui_asset_data_url(fname)
    face_html = (f"<img class='ody-diff-face' src='{util._esc(face)}'/>"
                 if face else "<span class='ody-diff-face-fb'>●</span>")
    if not meta:
        body = "<div class='ody-diff-stat'>（数据缺失）</div>"
    else:
        lives = int(meta.get("startingHealth") or 0)
        seats = int(meta.get("maxMonkeySeats") or 0)
        cap = int(meta.get("maxMonkeysOnBoat") or 0)

        def row(kind: str, text: str, fallback: str) -> str:
            icon = _odyssey_top_icon(kind)
            img = f"<img class='ody-diff-ico' src='{util._esc(icon)}'/>" if icon else f"<span>{fallback}</span>"
            return f"<div class='ody-diff-stat'>{img}<span>{util._esc(text)}</span></div>"

        body = (
            row("lives", f"生命 {lives}", "❤")
            + row("seats", f"猴位 {seats}", "🪑")
            + row("towers", f"猴子上限 {cap}", "🐵")
            + "<div class='ody-diff-rewards'>" + _odyssey_reward_cells(meta.get("_rewards") or []) + "</div>"
        )
        if meta.get("isExtreme"):
            body += "<div class='ody-diff-extreme'>极限</div>"
    return (
        f"<div class='ody-diff-box ody-diff-{key}' "
        f"style='background:{theme['grad']};border-color:{theme['border']};'>"
        f"<div class='ody-diff-head'>{face_html}<span class='ody-diff-name'>{util._esc(label)}</span></div>"
        f"<div class='ody-diff-body'>{body}</div></div>"
    )


def _odyssey_available_meta(diffs: dict) -> dict:
    """可用单位与三难度无关（实测 easy/medium/hard 相同），取第一个带塔表的 meta。"""
    for k in _DIFF_KEY_ORDER:
        m = (diffs.get(k) or {}).get("meta")
        if m and (m.get("_availableTowers") or m.get("_availablePowers")):
            return m
    for k in _DIFF_KEY_ORDER:
        m = (diffs.get(k) or {}).get("meta")
        if m:
            return m
    return {}


def _odyssey_diff_trio(col: dict) -> str:
    boxes = []
    for key in _DIFF_KEY_ORDER:
        diff = (col.get("diffs") or {}).get(key) or {}
        boxes.append(_odyssey_diff_box(key, diff.get("meta")))
    return "<div class='ody-diff-trio'>" + "".join(boxes) + "</div>"


def _odyssey_map_rule_text(mp: dict) -> str:
    """仅在无强化时用短文案；有强化时由图标行展示。"""
    modifiers = common._race_modifier_items(mp.get("_bloonModifiers"))
    details = [f"{label} {value}" for label, value, _icon in modifiers]
    custom_rounds = [str(x) for x in mp.get("roundSets") or [] if str(x).casefold() != "default"]
    if custom_rounds:
        details.append("自定义回合")
    for key, label in i18n.FLAG_LABELS:
        if mp.get(key):
            details.append(label)
    return "默认规则 · 无强化" if not details else ""


def _ody_mod_display_label(label: str) -> str:
    """强化中文名 → 卡片短标签（与 Boss 规则 chips 口径一致）。"""
    mapping = {
        "气球速度": "气球速度",
        "重型气球速度": "MOAB速度",
        "首领速度": "BOSS速度",
        "再生速度": "再生速度",
        "气球血量": "气球血量",
        "重型气球血量": "MOAB血量",
        "首领血量": "BOSS血量",
        "全体隐身": "全体隐身",
        "全体再生": "全体再生",
    }
    return mapping.get(label, label)


def _ody_diff_mode_icons(difficulty: str) -> str:
    """困难难度图标；模式用竞赛图标。"""
    raw = str(difficulty or "").strip()
    key = raw.lower()
    diff_map = {
        "beginner": "MapBeginnerBtn.png",
        "intermediate": "MapIntermediateBtn.png",
        "advanced": "MapAdvancedBtn.png",
        "expert": "MapExpertBtn.png",
        "easy": "MapBeginnerBtn.png",
        "medium": "MapIntermediateBtn.png",
        "hard": "MapAdvancedBtn.png",
        "简单": "MapBeginnerBtn.png",
        "中等": "MapIntermediateBtn.png",
        "困难": "MapAdvancedBtn.png",
        "专家": "MapExpertBtn.png",
    }
    fname = diff_map.get(key) or diff_map.get(raw) or ""
    diff_html = (
        common._race_ui_img(fname, "★", "ody-meta-ico") if fname
        else "<span class='ody-meta-ico-fb'>★</span>"
    )
    mode_html = common._race_ui_img("RaceIcon.png", "🏁", "ody-meta-ico")
    return diff_html, mode_html


def _odyssey_mod_chips_html(mp: dict) -> str:
    """气球强化：图标 + 名称（上）+ 倍率（下）。"""
    items = common._race_modifier_items(mp.get("_bloonModifiers"))
    chips = []
    for label, value, icon in items:
        name = _ody_mod_display_label(label)
        chips.append(
            "<span class='ody-mod-chip'>"
            f"{common._race_ui_img(icon, '⚡', 'ody-mod-chip-ico')}"
            "<span class='ody-mod-chip-text'>"
            f"<span class='ody-mod-chip-label'>{util._esc(name)}</span>"
            f"<span class='ody-mod-chip-val'>{util._esc(value)}</span>"
            "</span></span>"
        )
    flags = []
    for key, label in i18n.FLAG_LABELS:
        if mp.get(key):
            flags.append(f"<span class='ody-flag-chip'>{util._esc(label)}</span>")
    custom = [str(x) for x in mp.get("roundSets") or [] if str(x).casefold() != "default"]
    if custom:
        flags.append("<span class='ody-flag-chip'>自定义回合</span>")
    if not chips and not flags:
        return "<div class='ody-mod-default'>默认规则</div>"
    return "<div class='ody-mod-row'>" + "".join(chips) + "".join(flags) + "</div>"


def _odyssey_map_diff_bars(mp: dict, col: dict) -> str:
    """地图左缘色条：标明该图出现在哪些难度（蓝/红/黑）。"""
    name = str(mp.get("map") or "").strip()
    title = str(mp.get("name") or "").strip()
    bars = []
    for key in _DIFF_KEY_ORDER:
        maps = ((col.get("diffs") or {}).get(key) or {}).get("maps") or []
        hit = any(
            str(m.get("map") or "").strip() == name
            or (name and str(m.get("map") or "") == name)
            or (title and str(m.get("name") or "").strip() == title)
            for m in maps
        )
        # 困难列表作为主展示时，困难图必然命中；无 map 字段则按 index 兜底在调用处处理
        if hit:
            bars.append(f"<i class='ody-diff-bar ody-diff-bar-{key}'></i>")
    if not bars:
        bars.append("<i class='ody-diff-bar ody-diff-bar-hard'></i>")
    return "<div class='ody-diff-bars'>" + "".join(bars) + "</div>"


def _odyssey_map_row_html(mp: dict, col: dict) -> str:
    """单岛行：贴图 + 一行元信息 + 强化图标（不再用左侧色条，改由外层嵌套圈表达难度）。"""
    icons = _odyssey_map_icons()
    thumb = (f"<img class='ody-map-img' src='{util._esc(mp['img'])}' alt='{util._esc(mp.get('map') or mp.get('name') or '')}'/>"
             if mp.get("img") else "<div class='ody-map-empty'>暂无地图图像</div>")
    difficulty = i18n.cn(mp.get("difficulty"), i18n.DIFFICULTY_CN) or "未知难度"
    mode = i18n.cn(mp.get("mode"), i18n.MODE_CN) or "标准"
    start_round = int(mp.get("startRound") or 0)
    end_round = int(mp.get("endRound") or 0)
    rounds = f"{start_round}/{end_round}" if start_round or end_round else "—"
    plain_rule = _odyssey_map_rule_text(mp)
    coin_img = _odyssey_img(icons.get("coin", ""), "ody-mini-icon", "🪙", "金币")
    play_img = _odyssey_img(icons.get("play", ""), "ody-mini-icon", "▶", "开始")
    diff_ico, mode_ico = _ody_diff_mode_icons(difficulty)
    if plain_rule:
        mods_html = f"<div class='ody-map-rule'>{util._esc(plain_rule)}</div>"
    else:
        mods_html = _odyssey_mod_chips_html(mp)
    return (
        "<div class='ody-map-row'>"
        f"<div class='ody-map-img-cell'>{thumb}</div>"
        "<div class='ody-map-info'>"
        "<div class='ody-map-meta ody-map-meta-line'>"
        f"<span class='ody-map-meta-item'>{coin_img}{int(mp.get('startingCash') or 0):,}</span>"
        f"<span class='ody-map-meta-item'>{play_img}{util._esc(rounds)}</span>"
        f"<span class='ody-map-meta-item'>{diff_ico}{util._esc(difficulty)} / {mode_ico}{util._esc(mode)}</span>"
        "</div>"
        f"{mods_html}"
        "</div></div>"
    )


def _ody_map_key(mp: dict) -> str:
    return str(mp.get("map") or mp.get("name") or "").strip()


def _odyssey_nested_maps_html(primary: list, col: dict) -> str:
    """嵌套难度圈：地图行先统一缩进对齐，三色框用 inset 描边彼此贴合。

    蓝=简单图；红=简单+中等多出；黑=全部。外圈 padding 撑开内圈描边空间，
    保证地图缩略图左缘对齐。
    """
    diffs = col.get("diffs") or {}
    easy_keys = {_ody_map_key(m) for m in (diffs.get("easy") or {}).get("maps") or []}
    med_keys = {_ody_map_key(m) for m in (diffs.get("medium") or {}).get("maps") or []}

    easy_maps, med_extra, hard_extra = [], [], []
    for mp in primary or []:
        k = _ody_map_key(mp)
        if k in easy_keys:
            easy_maps.append(mp)
        elif k in med_keys:
            med_extra.append(mp)
        else:
            hard_extra.append(mp)

    def rows(ms):
        return "".join(_odyssey_map_row_html(mp, col) for mp in ms)

    easy_block = "<div class='ody-map-ring ody-ring-easy'>" + rows(easy_maps) + "</div>"
    med_extra_html = ("<div class='ody-ring-pad'>" + rows(med_extra) + "</div>") if med_extra else ""
    hard_extra_html = ("<div class='ody-ring-pad'>" + rows(hard_extra) + "</div>") if hard_extra else ""
    med_block = "<div class='ody-map-ring ody-ring-medium'>" + easy_block + med_extra_html + "</div>"
    hard_block = "<div class='ody-map-ring ody-ring-hard'>" + med_block + hard_extra_html + "</div>"
    return "<div class='ody-maps ody-maps-nested'>" + hard_block + "</div>"


def _odyssey_maps_html(maps: list, col: dict | None = None) -> str:
    """岛屿规则：嵌套难度圈 + 一行元信息 + 强化图标。"""
    col = col or {}
    if not maps:
        return "<div class='ody-maps'><div class='ody-panel ody-map-empty'>暂无远征地图</div></div>"
    return _odyssey_nested_maps_html(maps, col)


_ODIFF_TO_BTN = {"Beginner": "Beginner", "Easy": "Beginner",
                 "Intermediate": "Intermediate", "Medium": "Intermediate",
                 "Advanced": "Advanced", "Hard": "Advanced",
                 "Expert": "Expert", "Impoppable": "Expert"}

_ODYSSEY_TABS = (("easy", "简单", "MapBeginnerBtn.png"),
                 ("medium", "中等", "MapIntermediateBtn.png"),
                 ("hard", "困难", "MapAdvancedBtn.png"))


def _odyssey_default_crew_html(meta: dict) -> str:
    """兼容保留：统一卡已去掉默认队伍，返回空面板避免旧调用炸。"""
    return "<div class='ody-panel'><div class='ody-ribbon ody-panel-title'><span>默认队伍</span></div></div>"


def _odyssey_layout_stats(meta: dict | None) -> dict:
    """可用区真实行数：英雄/猴子/力量按当前栏宽估算每行格数。"""
    meta = meta or {}
    at = [t for t in meta.get("_availableTowers") or []
          if isinstance(t, dict) and t.get("max") != 0]
    heroes = sum(1 for t in at if t.get("isHero"))
    regular = len(at) - heroes
    # 力量按目录全量渲染（max==0 跳过），与 _available_html 同口径
    avail_max0 = {
        str(p.get("power") or "").strip()
        for p in meta.get("_availablePowers") or []
        if isinstance(p, dict) and p.get("max") is not None and int(p.get("max") or 0) == 0
    }
    power_count = sum(1 for raw, _k in _POWER_CATALOG if raw not in avail_max0)
    # 栏宽约 20% / 42% / 38%（800px 卡）：英雄 2/行、猴子 5/行、力量 5/行
    h_rows = max(1, -(-heroes // 2))
    t_rows = max(1, -(-regular // 5))
    p_rows = max(1, -(-power_count // 5))
    av_rows = max(h_rows, t_rows, p_rows)
    return {"heroes": heroes, "regular": regular, "powers": power_count,
            "av_rows": av_rows}


def _odyssey_card_height(col: dict | None, maps_count: int = 0) -> int:
    """统一远征卡片高度：按真实内容行数估算，避免大片底部空白。

    maps_count 传困难地图张数；兼容旧签名 (meta, maps_count)。
    渲染后仍可用 trim_odyssey_png 裁掉多余底边（游戏更新增塔/力量时兜底）。
    """
    # 兼容旧调用：第一参数是 meta dict
    if isinstance(col, dict) and "diffs" not in col and "ev" not in col:
        st = _odyssey_layout_stats(col)
        return int(28 + 64 + 168 + 36 + st["av_rows"] * 86 + 48
                   + max(1, maps_count) * 140 + 96 + 28)

    if not isinstance(col, dict) or not col:
        return 320
    diffs = col.get("diffs") or {}
    hard_maps = (diffs.get("hard") or {}).get("maps") or []
    n_maps = maps_count if maps_count > 0 else len(hard_maps)
    meta = None
    for k in _DIFF_KEY_ORDER:
        m = (diffs.get(k) or {}).get("meta")
        if m and (m.get("_availableTowers") or m.get("_availablePowers")):
            meta = m
            break
    if not meta:
        for k in _DIFF_KEY_ORDER:
            m = (diffs.get(k) or {}).get("meta")
            if m:
                meta = m
                break
    st = _odyssey_layout_stats(meta)
    # 嵌套难度圈额外高度：三道描边 + easy/medium/hard padding（原先漏算会裁掉第 5 张图）
    nest_h = 96
    # 页边距 + 标题 + 三难度框 + 可用区 + 岛屿标题 + 地图行 + 页脚小字
    return int(28 + 64 + 168 + 36 + st["av_rows"] * 86 + 48
               + max(1, n_maps) * 140 + nest_h + 28)


def trim_odyssey_png(path: str, pad: int = 24) -> str:
    """按像素裁掉底部空白，使高度随内容自动收紧；内容变多时不会裁到正文。"""
    try:
        from PIL import Image
    except ImportError:
        return path
    try:
        im = Image.open(path).convert("RGB")
    except OSError:
        return path
    w, h = im.size
    if h < 40 or w < 40:
        return path
    bg = (228, 208, 188)  # _odyssey_shell tan 页面色
    last = h - 1
    step = max(1, h // 200)
    for y in range(h - 1, -1, -step):
        n = 0
        for x in range(8, w - 8, 6):
            c = im.getpixel((x, y))
            if abs(c[0] - bg[0]) + abs(c[1] - bg[1]) + abs(c[2] - bg[2]) > 36:
                n += 1
                if n >= 3:
                    break
        if n >= 3:
            last = y
            break
    new_h = min(h, last + pad)
    if new_h < h * 0.55:  # 异常过矮时放弃裁剪，避免误伤
        return path
    if h - new_h < 12:
        return path
    im.crop((0, 0, w, new_h)).save(path, format="PNG")
    return path


def odyssey_html(col: dict) -> str:
    """统一远征卡片（一张图含三难度）。"""
    if col.get("empty"):
        return common._odyssey_shell(
            f"<div class='ody-panel ody-map-empty'>{util._esc(col['empty'])}</div>", 320, theme="tan")

    ev = col["ev"]
    diffs = col.get("diffs") or {}
    event_name = (ev.get("name") or "远征活动").strip()
    description = (ev.get("description") or "").strip()
    state = util._STATE_TXT[util._state_of(ev, util.bucket_now())]
    any_extreme = any(
        bool(((diffs.get(k) or {}).get("meta") or {}).get("isExtreme"))
        for k in _DIFF_KEY_ORDER
    )

    banner = (f"<div class='ody-title-banner'><div class='ody-title'>{util._esc(event_name)}</div>"
              + (f"<div class='ody-title-sub'>{util._esc(description)}</div>" if description else "")
              + "</div>")

    # 岛屿：默认困难五图；困难缺失则 medium → easy
    primary_maps = []
    primary_key = "hard"
    for key in ("hard", "medium", "easy"):
        maps = (diffs.get(key) or {}).get("maps") or []
        if maps:
            primary_maps = maps
            primary_key = key
            break

    top = (
        banner
        + _odyssey_diff_trio(col)
        + ("<div class='ody-extreme-badge'>极限模式</div>" if any_extreme else "")
        + _odyssey_available_html(_odyssey_available_meta(diffs))
        + "<div class='ody-section-banner'>岛屿规则</div>"
        + _odyssey_maps_html(primary_maps, col)
        + "<div class='ody-event-desc' style='margin-top:6px;text-align:center;'>"
        + f"默认展示{_DIFF_LABEL[primary_key]}地图 · {util._esc(util._fmt_range(ev))} · {util._esc(state)}"
        + "</div>"
    )
    height = _odyssey_card_height(col, len(primary_maps))
    return common._odyssey_shell(top, height, theme="tan")


def odyssey_diff_html(col: dict, d: str = "hard", label: str = "") -> str:
    """兼容旧签名：统一卡不再按单难度拆图，一律走三难度合卡。"""
    return odyssey_html(col)
