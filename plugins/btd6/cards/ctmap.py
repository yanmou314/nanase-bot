"""CT 争夺领土卡片：七环六边形领土地图（复刻 BTD6 API Explorer CT 站地图页）。

weasyprint 62 不支持 clip-path，六边形棋盘改由 PIL 直绘整图（超采样抗锯齿 +
遮罩贴图 + 描边文字），HTML 外壳承载标题/图例/统计。

总览卡 = Maps Background 视图 + 旗帜/遗物/Boss 角标 + 图例统计；另按站点
DISPLAY PRESETS 出 4 张预设图：default（地形纹理+旗帜/遗物金框图标）、
gametypes（类型着色+模式图标）、maps（地图贴图+回合数）、heroes（类型着色+
英雄头像+回合数），渲染规则与站点 renderPresets/updateCTBackground 一致。
"""
import base64
import io
import math
from collections import Counter

from PIL import Image, ImageDraw, ImageFont

import logging

from . import common, rules
from .. import assets, ctmap, i18n, util

_logger = logging.getLogger(__name__)


_PAD = 16  # 卡片内容左右内边距

# 棋盘直绘的字体（渲染线程内使用；wqy-microhei 与全站 CSS 字体一致）
_FONT_PATH = "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"
_FONT_CACHE: dict[int, ImageFont.FreeTypeFont] = {}
_SS = 2  # 棋盘超采样倍率（抗锯齿）
_OUTLINE = (14, 30, 52)      # 格子描边深蓝
_TXT_STROKE = (11, 22, 38)   # 回合文字描边
_LIME = (0xB9, 0xE5, 0x46)   # 站点 default/tileType 底色


def _font(px: float) -> ImageFont.FreeTypeFont:
    key = max(8, int(px))
    f = _FONT_CACHE.get(key)
    if f is None:
        try:
            f = ImageFont.truetype(_FONT_PATH, key)
        except OSError:
            # 环境缺 wqy-microhei（如 CI 干净环境）：退回默认字体保证渲染不崩，
            # CJK 字形缺失只影响无该字体的环境（生产机已安装）
            _logger.warning("BTD6 CT 棋盘字体缺失，回退默认字体: %s", _FONT_PATH)
            f = ImageFont.load_default(size=key)
        _FONT_CACHE[key] = f
    return f


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def _rounds_txt(gd: dict) -> str:
    """格子回合范围；Boss 格（endRound=-1）显示 20+20×层数 上限（与站点一致）。"""
    rules = (gd.get("dcModel") or {}).get("startRules") or {}
    r0 = int(rules.get("round") or 0)
    r1 = rules.get("endRound")
    if r1 is None:
        return f"{r0}/?"
    if int(r1) < 0:
        tier = int(((gd.get("bossData") or {}).get("TierCount")) or 0)
        return f"{r0}/{20 + 20 * tier}+"
    return f"{r0}/{int(r1)}"


def _decode_asset(data_url: str) -> Image.Image | None:
    if not data_url.startswith("data:image/"):
        return None
    try:
        raw = base64.b64decode(data_url.split(",", 1)[1], validate=True)
        return Image.open(io.BytesIO(raw))
    except Exception:  # noqa: BLE001  # 素材损坏按缺失处理
        return None


def _cover_crop(img: Image.Image, tw: int, th: int) -> Image.Image:
    """缩放并中心裁剪到目标尺寸（object-fit: cover）。"""
    sw, sh = img.size
    scale = max(tw / sw, th / sh)
    nw, nh = max(1, round(sw * scale)), max(1, round(sh * scale))
    img = img.convert("RGB").resize((nw, nh), Image.LANCZOS)
    left = (nw - tw) // 2
    top = (nh - th) // 2
    return img.crop((left, top, left + tw, top + th))


def _hex_bbox_pts(tw: float, th: float) -> list[tuple[float, float]]:
    """外接尺寸 tw×th 的尖顶正六边形顶点（顶点在上/下）。"""
    return [(tw / 2, 0), (tw, th / 4), (tw, th * 3 / 4), (tw / 2, th), (0, th * 3 / 4), (0, th / 4)]


# 棋盘 PNG 与渲染期无关（活动一周期数/布局固定），按事件+视图+角标缓存
_BOARD_CACHE: dict[tuple, str] = {}


def _render_board(col: dict, view: str | None = None, badge: bool = False) -> tuple[str, float, float]:
    """PIL 直绘棋盘 → PNG data URL。返回 (data_url, css_w, css_h)。

    view：None/\"maps\" = 地图贴图+回合数（总览另加角标）；\"default\" = 地形纹理
    +旗帜/遗物居中图标；\"gametypes\" = 类型着色+模式图标；\"heroes\" = 类型着色
    +英雄头像+回合数。渲染规则与站点 renderPresets 一致。
    """
    ev_id = str((col.get("ev") or {}).get("id") or "")
    cache_key = (ev_id, view, badge)
    cached = _BOARD_CACHE.get(cache_key)
    if cached:
        return cached

    grid = ctmap.build_ct_grid()
    ct_tiles: dict = col.get("ct_tiles") or {}
    content_w = common.CARD_W - 2 * _PAD - 24
    # board_metrics 返回 (width, height, left, top)，取 width 反推格尺寸
    w1 = ctmap.board_metrics(grid["tiles"], grid["spawns"], 1.0, pad=0.0)[0]
    size = content_w / w1
    css_w, css_h, left, top = ctmap.board_metrics(grid["tiles"], grid["spawns"], size, pad=12.0)

    s = _SS
    W, H = round(css_w * s), round(css_h * s)
    board = Image.new("RGB", (W, H), (13, 32, 58))
    draw = ImageDraw.Draw(board)
    R = size * s            # 格外接半径（像素）
    ol = max(1, round(1.5 * s))

    thumbs: dict[tuple, Image.Image | None] = {}

    def _asset_img(name: str) -> Image.Image | None:
        url = assets._game_asset_data_url(name)
        return _decode_asset(url) if url else None

    def thumb(name: str, tw: int, th: int):
        key = (name, tw, th)
        if key not in thumbs:
            img = _asset_img(name) if name else None
            thumbs[key] = _cover_crop(img, tw, th) if img is not None else None
        return thumbs[key]

    def icon(name: str, target: float) -> Image.Image | None:
        key = (name, round(target))
        if key not in thumbs:
            img = _asset_img(name)
            if img is None:
                thumbs[key] = None
            else:
                img = img.convert("RGBA")
                w, h = img.size
                scale = target / max(w, h)
                thumbs[key] = img.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                                         Image.LANCZOS)
        return thumbs[key]

    def tile_xy(q: int, r: int) -> tuple[float, float]:
        x, y = ctmap.hex_position(q, r, size)
        return (x - left + 12) * s, (y - top + 12) * s

    def paste_hex_art(cx: float, cy: float, art: Image.Image, Ri: float, tw: int, th: int) -> None:
        mask = Image.new("L", (tw, th), 0)
        ImageDraw.Draw(mask).polygon(_hex_bbox_pts(tw, th), fill=255)
        board.paste(art, (int(cx - tw / 2), int(cy - th / 2)), mask)

    def fill_hex(cx: float, cy: float, color: tuple[int, int, int], Ri: float) -> None:
        draw.polygon([(cx + Ri * math.cos(math.radians(60 * k - 30)),
                       cy + Ri * math.sin(math.radians(60 * k - 30))) for k in range(6)], fill=color)

    decor_cache: dict[tuple, Image.Image | None] = {}

    def decor_thumb(theme: str, tw: int, th: int):
        """主题装饰纹理（透明底）按 meet 适配到格子包围盒，保留 alpha。"""
        if not theme:
            return None
        key = (theme, tw, th)
        if key not in decor_cache:
            img = _asset_img(f"{theme}.webp")
            if img is None:
                decor_cache[key] = None
            else:
                img = img.convert("RGBA")
                sw, sh = img.size
                scale = min(tw / sw, th / sh)
                nw, nh = max(1, round(sw * scale)), max(1, round(sh * scale))
                decor_cache[key] = img.resize((nw, nh), Image.LANCZOS)
        return decor_cache[key]

    def paste_center(cx: float, cy: float, img: Image.Image) -> None:
        board.paste(img, (int(cx - img.width / 2), int(cy - img.height / 2)), img)

    def rounds_text(cx: float, cy: float, gd: dict) -> None:
        f = _font(0.44 * size * s)
        draw.text((cx, cy - 0.60 * R), _rounds_txt(gd), font=f, fill=(255, 255, 255),
                  anchor="mm", stroke_width=max(1, round(1.1 * s)), stroke_fill=_TXT_STROKE)

    view = view or "maps"
    for tid, (q, r) in grid["tiles"].items():
        cx, cy = tile_xy(q, r)
        data = ct_tiles.get(tid) or {}
        gd = data.get("GameData") or {}
        tile_type = str(data.get("TileType") or "Regular")
        # 深色描边（外六边形）
        draw.polygon([(cx + (R - ol // 2) * math.cos(math.radians(60 * k - 30)),
                       cy + (R - ol // 2) * math.sin(math.radians(60 * k - 30))) for k in range(6)],
                     fill=_OUTLINE)
        Ri = R - ol
        tw, th = int(math.sqrt(3) * Ri), int(2 * Ri)

        if view == "default":
            # 站点 decor 层：青柠底 + 主题装饰纹理（RGBA 透明底，meet 适配居中）
            fill_hex(cx, cy, _LIME, Ri)
            theme = ctmap.MAP_THEMES.get(str(gd.get("selectedMap") or ""), "")
            decor = decor_thumb(theme, tw, th)
            if decor is not None:
                board.paste(decor, (int(cx - decor.width / 2), int(cy - decor.height / 2)), decor)
            if tile_type == "Banner":
                icon_img = icon("CTPointsBanner.webp", 1.15 * Ri)
                if icon_img is not None:
                    paste_center(cx, cy, icon_img)
            relic = str(data.get("RelicType") or "None")
            if relic != "None":
                icon_img = icon(f"{relic}.webp", 1.2 * Ri)
                if icon_img is not None:
                    paste_center(cx, cy, icon_img)
        elif view == "gametypes":
            fill_hex(cx, cy, _hex_to_rgb(ctmap.tile_type_color(tile_type)), Ri)
            icon_name, tier = ctmap.gametype_icon(gd)
            if icon_name:
                icon_img = icon(f"{icon_name}.webp", 1.15 * Ri)
                if icon_img is not None:
                    paste_center(cx, cy, icon_img)
                if tier:
                    tf = _font(0.42 * size * s)
                    draw.text((cx + 0.52 * R, cy + 0.44 * R), str(tier), font=tf,
                              fill=(255, 255, 255), anchor="mm",
                              stroke_width=max(1, round(1.1 * s)), stroke_fill=_TXT_STROKE)
        elif view == "heroes":
            fill_hex(cx, cy, _hex_to_rgb(ctmap.tile_type_color(tile_type)), Ri)
            hero_icon = icon(f"{ctmap.hero_icon_name(gd)}.webp", 1.3 * Ri)
            if hero_icon is not None:
                paste_center(cx, cy, hero_icon)
            rounds_text(cx, cy, gd)
        else:  # maps（总览亦用此视图）
            map_name = str(gd.get("selectedMap") or "")
            art = thumb(f"MapSelect{map_name}Button.webp" if map_name else "", tw, th)
            if art is not None:
                paste_hex_art(cx, cy, art, Ri, tw, th)
            else:
                fill_hex(cx, cy, (38, 58, 84), Ri)
            if view == "coords":
                # 坐标预设：maps 背景 + 三字母 id 居中（黑色描边白字，小字不挡贴图）
                f = _font(0.30 * size * s)
                draw.text((cx, cy + 0.55 * R), tid, font=f, fill=(255, 255, 255),
                          anchor="mm", stroke_width=max(1, round(0.9 * s)),
                          stroke_fill=(0, 0, 0))
            else:
                rounds_text(cx, cy, gd)
            if badge:
                if tile_type == "Banner":
                    icon_img = icon("CTPointsBanner.webp", 0.56 * th)
                    if icon_img is not None:
                        board.paste(icon_img, (int(cx + tw / 2 - icon_img.width), int(cy - th / 2)), icon_img)
                relic = str(data.get("RelicType") or "None")
                if relic != "None":
                    icon_img = icon(f"{relic}.webp", 0.60 * th)
                    if icon_img is not None:
                        board.paste(icon_img, (int(cx - tw / 2), int(cy + th / 2 - icon_img.height)), icon_img)
                sub = gd.get("subGameType")
                if sub == 4:
                    icon_name, _tier = ctmap.gametype_icon(gd)
                    icon_img = icon(f"{icon_name}.webp", 0.62 * th) if icon_name else None
                    if icon_img is not None:
                        board.paste(icon_img, (int(cx + tw / 2 - icon_img.width),
                                               int(cy + th / 2 - icon_img.height)), icon_img)

    for sp in grid["spawns"]:
        cx, cy = tile_xy(sp["q"], sp["r"])
        color = _hex_to_rgb(ctmap.TEAM_COLORS.get(sp["team"], "#888888"))
        draw.polygon([(cx + (R - ol // 2) * math.cos(math.radians(60 * k - 30)),
                       cy + (R - ol // 2) * math.sin(math.radians(60 * k - 30))) for k in range(6)],
                     fill=(242, 246, 251))
        draw.polygon([(cx + (R - ol) * math.cos(math.radians(60 * k - 30)),
                       cy + (R - ol) * math.sin(math.radians(60 * k - 30))) for k in range(6)],
                     fill=color)
        f = _font(0.42 * size * s)
        draw.text((cx, cy - 0.30 * R), "起点", font=f, fill=(255, 255, 255),
                  anchor="mm", stroke_width=max(1, round(1.1 * s)), stroke_fill=_TXT_STROKE)
        draw.text((cx, cy + 0.26 * R), f"{sp['team']}AA", font=f, fill=(255, 255, 255),
                  anchor="mm", stroke_width=max(1, round(1.1 * s)), stroke_fill=_TXT_STROKE)

    buf = io.BytesIO()
    board.save(buf, "PNG", optimize=True)
    data_url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    if len(_BOARD_CACHE) > 24:
        _BOARD_CACHE.clear()
    _BOARD_CACHE[cache_key] = (data_url, css_w, css_h)
    return data_url, css_w, css_h


def _map_shell(body: str, w: int, h: int) -> str:
    """CT 地图外壳：蓝色蜂窝底 + 深色信息条（页面即棋盘画布）。"""
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
@page {{ size: {w}px {h}px; margin: 0; background: linear-gradient(160deg, #3b8fdd 0%, #2277cc 52%, #185da8 100%); }}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html, body {{ width: {w}px; height: {h}px; color: #ffffff;
        font-family: "WenQuanYi Micro Hei", "Noto Sans CJK SC", sans-serif;
        background: linear-gradient(160deg, #3b8fdd 0%, #2277cc 52%, #185da8 100%); overflow: hidden; }}
.ct-page {{ width: {w}px; height: {h}px; padding: 12px {_PAD}px 12px; }}
.ct-topbar {{ background: rgba(10,24,42,.78); border: 1px solid rgba(9,20,36,.9); border-radius: 12px;
              padding: 8px 16px; display: table; width: 100%; table-layout: fixed;
              box-shadow: inset 0 1px 0 rgba(255,255,255,.08); }}
.ct-title-cell {{ display: table-cell; vertical-align: middle; }}
.ct-title {{ font-size: 24px; line-height: 30px; font-weight: 900; text-shadow: 0 1px 0 rgba(4,12,24,.8); }}
.ct-subtitle {{ font-size: 15px; line-height: 20px; font-weight: 700; color: #d8e8fa;
                text-shadow: 0 1px 0 rgba(4,12,24,.8); padding-top: 1px; }}
.ct-badgecell {{ display: table-cell; width: 230px; text-align: right; vertical-align: middle; }}
.ct-badge {{ display: inline-block; min-width: 210px; padding: 6px 12px; border-radius: 9px;
             background: rgba(8,16,28,.55); border: 2px solid rgba(255,255,255,.35); color: #ffffff;
             font-size: 15px; line-height: 19px; font-weight: 900; text-align: center; }}
.ct-boardimg {{ display: block; margin: 10px auto 0; border-radius: 10px;
                border: 1px solid rgba(9,20,36,.55); }}
.ct-foot {{ margin-top: 10px; background: rgba(10,24,42,.78); border: 1px solid rgba(9,20,36,.9);
            border-radius: 12px; padding: 8px 14px; color: #e2ecf8; font-size: 13.5px; line-height: 21px;
            font-weight: 700; }}
.ct-foot b {{ color: #ffffff; }}
.ct-legend {{ display: table; width: 100%; table-layout: fixed; margin-top: 8px;
              background: rgba(10,24,42,.78); border: 1px solid rgba(9,20,36,.9);
              border-radius: 12px; padding: 6px 8px; }}
.ct-lg {{ display: table-cell; vertical-align: middle; text-align: center; color: #e2ecf8;
          font-size: 13px; line-height: 18px; font-weight: 700; white-space: nowrap;
          text-shadow: 0 1px 0 rgba(4,12,24,.8); }}
.ct-lg img {{ width: 16px; height: 16px; margin-right: 5px; vertical-align: -3px; object-fit: contain; }}
.ct-note {{ margin-top: 8px; text-align: center; color: #dceafc; font-size: 12px; line-height: 17px;
            font-weight: 700; text-shadow: 0 1px 0 rgba(4,12,24,.8); }}
</style></head>
<body><div class="ct-page">{body}</div></body></html>"""


def _board_html(col: dict, view: str | None = None, badge: bool = False,
                with_header: bool = True, with_footer: bool = False,
                preset_label: str = "") -> str:
    """拼一张完整的 CT 地图卡并返回 HTML。"""
    ev = col["ev"]
    number = int(col.get("number") or 0)
    ct_tiles: dict = col.get("ct_tiles") or {}
    nk_tiles: list = col.get("nk_tiles") or []
    now = int(col.get("now") or 0)

    board_url, board_w, board_h = _render_board(col, view=view, badge=badge)

    chunks = []
    if with_header:
        if not preset_label:
            state = util._state_of(ev, now)
            if state == "on":
                badge_txt = f"剩余 {util.fmt_remaining(int(ev.get('end') or 0) - now)}"
            elif state == "up":
                badge_txt = f"{util.fmt_remaining(int(ev.get('start') or 0) - now)}后开始"
            else:
                badge_txt = "已结束"
            chunks.append(
                f"<div class='ct-topbar'><div class='ct-title-cell'>"
                f"<div class='ct-title'>争夺领土 #{number or '?'}"
                f"<span style='font-size:14px;color:#c8dcf2;'> Contested Territory</span></div>"
                f"<div class='ct-subtitle'>{util._esc(util._fmt_range(ev))} · "
                f"个人 {int(ev.get('totalScores_player') or 0):,} · 战队 {int(ev.get('totalScores_team') or 0):,}</div></div>"
                f"<div class='ct-badgecell'><span class='ct-badge'>{util._esc(badge_txt)}</span></div></div>")
        else:
            chunks.append(
                f"<div class='ct-topbar'><div class='ct-title-cell'>"
                f"<div class='ct-title'>争夺领土 #{number or '?'} · {util._esc(preset_label)} 预设</div>"
                f"<div class='ct-subtitle'>Display Presets · 完整活动信息见总览图</div></div></div>")

    chunks.append(f"<img class='ct-boardimg' style='width:{board_w:.0f}px;height:{board_h:.0f}px;' src='{board_url}'/>")

    if with_footer:
        type_cnt = Counter(str(t.get("type") or "?").split(" - ")[0] for t in nk_tiles if isinstance(t, dict))
        mode_cnt = Counter()
        for data in ct_tiles.values():
            sub = (data.get("GameData") or {}).get("subGameType")
            if sub is not None:
                mode_cnt[sub] += 1
        n_relic = sum(1 for d in ct_tiles.values() if str(d.get("RelicType") or "None") != "None")
        n_banner = sum(1 for d in ct_tiles.values() if d.get("TileType") == "Banner")

        legend_cells = []
        for sub, label in ((8, "最少现金"), (9, "最少层数"), (2, "竞速"), (4, "Boss")):
            legend_cells.append(
                f"<div class='ct-lg'><span style='display:inline-block;width:14px;height:14px;margin-right:5px;"
                f"vertical-align:-2px;background:{ctmap.GAMETYPE_COLORS[sub]};'></span>{label} {mode_cnt.get(sub, 0)}</div>")
        banner_url = assets._game_asset_data_url("CTPointsBanner.webp")
        relic_url = assets._game_asset_data_url("Thrive.webp")
        if banner_url:
            legend_cells.append(f"<div class='ct-lg'><img src='{banner_url}'/>旗帜 {n_banner}</div>")
        if relic_url:
            legend_cells.append(f"<div class='ct-lg'><img src='{relic_url}'/>遗物 {n_relic}</div>")

        powers = "、".join(i18n._odyssey_power_name(str(p)) for p in (col.get("daily_powers") or [])[:7])
        relics_pool = "、".join(i18n.relic_cn(str(r)) for r in (col.get("event_relics") or []))
        n_regular = type_cnt.get("Regular", 0) + type_cnt.get("TeamFirstCapture", 0)
        mode_line = " · ".join(f"{label} {mode_cnt.get(sub, 0)}"
                               for sub, label in ((8, "最少现金"), (9, "最少层数"), (2, "竞速"), (4, "Boss"))
                               if mode_cnt.get(sub))

        foot = [f"格子 <b>{len(nk_tiles)}</b> = 地图格 <b>{len(nk_tiles) - type_cnt.get('TeamStart', 0)}</b>"
                f"（{mode_line}）+ 出生点 <b>{type_cnt.get('TeamStart', 0)}</b>",
                f"按类型：常规 {n_regular} · 旗帜 {n_banner} · 遗物 {type_cnt.get('Relic', 0)} · 出生点 {type_cnt.get('TeamStart', 0)}"]
        if powers:
            foot.append(f"每日力量：<b>{powers}</b>")
        if relics_pool:
            foot.append(f"本期遗物池：<b>{relics_pool}</b>")
        chunks.append("<div class='ct-legend'>" + "".join(legend_cells) + "</div>")
        chunks.append("<div class='ct-foot'>" + "<br>".join(foot) + "</div>")
        chunks.append("<div class='ct-note'>地图布局由 btd6-ct-map 社区数据集提供（BTD6 API Explorer 同源），与游戏内一致</div>")

    body = "".join(chunks)
    header_h = 78 if with_header else 0
    footer_h = (46 + 128) if with_footer else 0
    total_h = int(header_h + board_h + footer_h + 34)
    return _map_shell(body, common.CARD_W, total_h)


def ctmap_html(col: dict) -> str:
    """总览卡：活动信息 + 地图贴图视图（含角标）+ 图例与统计。"""
    if col.get("empty"):
        return common._shell(f"<div class='empty'>{util._esc(col['empty'])}</div>", 180)
    return _board_html(col, view="maps", badge=True, with_footer=True)


# 站点 DISPLAY PRESETS 的 4 种预设（handler 发送顺序）
CT_PRESET_CARDS = (
    ("default", "默认"),
    ("gametypes", "游戏类型"),
    ("maps", "地图背景"),
    ("heroes", "英雄"),
    ("coords", "坐标"),
)


def ctmap_preset_html(col: dict, name: str) -> str:
    """预设卡：按站点 DISPLAY PRESETS 渲染对应视图。"""
    if col.get("empty"):
        return common._shell(f"<div class='empty'>{util._esc(col['empty'])}</div>", 180)
    label = dict(CT_PRESET_CARDS).get(name, name)
    return _board_html(col, view=name, badge=False, with_footer=False, preset_label=label)


# ---------------- 单格详情（沿用 BTD6 API Explorer 站点 / daily 卡版式） ----------------
# 单格详情是 daily 卡片的"窄版"：复用 common._race_shell + _race_emblem/_race_title 等
# 站点样式 token，避免重新发明一套颜色/版式。下方仅做数据映射（CT tile -> 站点字段）。


def ct_tile_html(col: dict, tile_id: str) -> str:
    """单格详情卡：沿用 daily / 竞速卡版式（站点 _race_shell）。"""
    if col.get("empty"):
        return common._shell(f"<div class='empty'>{util._esc(col['empty'])}</div>", 180)
    grid = ctmap.build_ct_grid()
    ct_tiles = col.get("ct_tiles") or {}
    if tile_id in grid["tiles"]:
        data = ct_tiles.get(tile_id) or {}
    elif tile_id in {sp["id"] for sp in grid["spawns"]}:
        data = {}
    else:
        return common._shell(
            f"<div class='empty'>未找到格子 {util._esc(tile_id)}（共 {len(grid['tiles']) + 6} 个有效 id）</div>",
            180)

    gd = data.get("GameData") or {}
    dc = gd.get("dcModel") or {}
    rules = dc.get("startRules") or {}
    # ---- 标题/模式 ----
    sub = gd.get("subGameType")
    boss = gd.get("bossData") or {}
    difficulty = str(dc.get("difficulty") or "Medium")
    # 模式中文译名 + 对应游戏图标（subGameType 编号）
    mode_cn, mode_icon = _tile_mode_zh(sub, boss)
    # Boss 模式还要叠加 Boss 名
    if sub == 4 and boss:
        idx = int(boss.get("bossBloon") or 0)
        boss_name_cn = i18n.boss_cn(ctmap.BOSSES_IN_ORDER[idx]) if 0 <= idx < len(ctmap.BOSSES_IN_ORDER) else "Boss"
        tier = int(boss.get("TierCount") or 0)
        mode_cn = f"{mode_cn} {boss_name_cn}" + (f" T{tier}" if tier else "")

    ev = col.get("ev") or {}
    number = int(col.get("number") or 0)
    now = int(col.get("now") or 0)
    state = util._STATE_TXT[util._state_of(ev, now)]
    title = f"{difficulty} - {mode_cn}"
    subtitle = f"争夺领土 #{number or '?'} · {tile_id} · {state}"

    # ---- 资源数据 ----
    cash = int(rules.get("cash") or 0)
    lives_raw = int(rules.get("lives") or 0)
    max_towers = int(dc.get("maxTowers") or 0)
    default_lives = {"Easy": 200, "Medium": 150, "Hard": 100,
                     "Impoppable": 1, "EasyMedium": 150}
    lives = lives_raw if lives_raw > 0 else default_lives.get(difficulty, 0)
    towers_cap = "∞" if max_towers <= 0 or max_towers >= 9999 else f"{max_towers:,}"
    r0 = int(rules.get("round") or 0)
    r1 = rules.get("endRound")
    if r1 is None:
        rounds_txt = f"{r0}/?"
    elif int(r1) < 0:
        rounds_txt = f"{r0}/{20 + 20 * int(boss.get('TierCount') or 0)}+"
    else:
        rounds_txt = f"{r0}/{int(r1)}"

    # ---- 资源图标（沿用 _race_ui_img 站点 PNG 资源） ----
    def stat(icon_fname: str, fallback: str, label: str, value: str = "") -> str:
        if icon_fname.startswith("data:"):
            img_html = f"<img class='race-stat-icon' src='{util._esc(icon_fname)}'/>"
        else:
            img_html = common._race_ui_img(icon_fname, fallback, "race-stat-icon")
        value_html = f"<div class='race-stat-value'>{util._esc(value)}</div>" if value else ""
        return ("<div class='race-stat'><div class='race-stat-icon-cell'>"
                f"{img_html}"
                f"</div><div class='race-stat-copy'><div class='race-stat-label'>"
                f"{util._esc(label)}</div>{value_html}</div></div>")

    # 左 3 行（去掉"格子/标准"），右 2 行（去掉"难度"和"模式"重复信息）
    stat_left = "".join([
        stat("cash.png", "🪙", "初始资金", f"{cash:,}"),
        stat("heart.png", "❤", "初始生命", f"{lives:,}"),
        stat("end-round.png", "⏭", "回合", rounds_txt),
    ])
    stat_right = "".join([
        stat("start-round.png", "▶", "开始", str(r0) if r0 else "—"),
        stat("monkey-cap.png", "🐒", "最大猴子", towers_cap),
    ])

    # ---- 徽章：游戏类型图标（subGameType → 站点素材） ----
    emblem_src = mode_icon
    emblem_html = (f"<img class='race-emblem-img' src='{util._esc(emblem_src)}' alt='{util._esc(mode_cn)}'/>"
                   if emblem_src
                   else f"<div class='race-emblem-fallback'>{util._esc(mode_cn[:1])}</div>")

    # ---- 标题条：与 rules_html 一致的 .race-topbar 结构 ----
    body = ("<div class='race-topbar'><div class='race-head'>"
            f"<div class='race-emblem-cell'><div class='race-emblem'>{emblem_html}</div></div>"
            f"<div class='race-title-cell'><div class='race-title'>{util._esc(title)}</div>"
            f"<div class='race-subtitle'>{util._esc(subtitle)}</div>"
            "</div></div></div>")

    # ---- 主面板：左 = 地图缩略，右 = 资源两列 ----
    map_name = str(gd.get("selectedMap") or "")
    map_url = assets._game_asset_data_url(f"MapSelect{map_name}Button.webp") if map_name else ""
    map_alt = i18n.map_cn(map_name) or "map"
    map_img_html = (f"<img src='{util._esc(map_url)}' alt='{util._esc(map_alt)}'/>" if map_url
                    else "<div class='race-map-empty'>🗺</div>")
    body += ("<div class='race-content'><div class='race-layout'>"
             f"<div class='race-map'>{map_img_html}</div>"
             f"<div class='race-stats-cell'><div class='race-stats'>"
             f"<div class='race-stat-col'>{stat_left}</div>"
             f"<div class='race-stat-col'>{stat_right}</div></div></div></div>")

    # ---- 底部：猴塔/英雄瓦片网格（全部可用塔，无英雄显示 AllHeroesIcon） ----
    dc_items = ((dc.get("towers") or {}).get("_items")) or []
    body += ("<div class='race-monkey-section'><div class='race-options'>"
             "<div class='race-available'>可用猴子：</div></div>"
             f"{_render_tile_monkey_grid(dc_items)}</div>")

    # ---- 底部：气球强化 + 禁用项（合并到一个面板，沿用 _race_modifier_html 站点版式） ----
    mods = dc.get("bloonModifiers") or {}
    # 禁用项专用 PNG（来自 BTD6 API Explorer 站 Assets/ChallengeRulesIcon）。
    # 本地无图时 fallback 到 emoji 兜底。命名固定，方便后续维护。
    BAN_ICON = {
        "disableMK":          ("猴子知识",   "NoKnowledgeIcon.webp",   "🧠"),
        "disablePowers":      ("力量道具",   "PowersDisabledIcon.webp", "🍌"),
        "disableInstas":      ("即时猴塔",   "NoInstaMonkeys.webp",     "⚡"),
        "disableSelling":     ("卖塔",       "SellingDisabledIcon.webp","💰"),
        "noContinues":        ("重开续命",   "NoContinuesIcon.webp",    "❤️"),
        "disableDoubleCash":  ("双倍启动现金", "NoDoubleCashIcon.webp",  "💵"),
    }
    ban_items_html = []
    for key, (label, icon, emoji) in BAN_ICON.items():
        if dc.get(key):
            icon_url = assets._game_asset_data_url(icon)
            icon_html = (f"<img class='race-mod-icon' src='{util._esc(icon_url)}'/>"
                         if icon_url else f"<span class='race-mod-icon-fallback'>{emoji}</span>")
            ban_items_html.append(
                f"<div class='race-mod-item'>"
                f"<div class='race-mod-icon-cell'>{icon_html}</div>"
                f"<div class='race-mod-copy'><div class='race-mod-label'>{util._esc(label)}</div>"
                f"<div class='race-mod-value'>禁用</div></div></div>"
            )
    ban_html = "".join(ban_items_html) or "<div class='race-mod-default'>无</div>"
    modifier_html = common._race_modifier_html(mods)
    if ban_items_html:
        combined = (modifier_html
                    + "<div class='race-mod-row'>" + ban_html + "</div>")
    else:
        combined = modifier_html
    body += ("<div class='race-bottom'><div class='race-bottom-full'>"
             "<div class='race-panel-head'>气球强化 / 禁用项</div><div class='race-panel-body'>"
             f"{combined}</div></div></div>")

    # ---- 画布高度（参照 rules_html 估算） ----
    monkey_count = body.count("<div class='race-mkwrap'>")
    grid_rows = max(1, -(-monkey_count // 11)) if monkey_count else 1
    topbar_h = 102
    stat_rows = max(stat_left.count("<div class='race-stat'>"),
                    stat_right.count("<div class='race-stat'>"))
    stats_h = 16 + max(3, stat_rows) * 48
    layout_h = max(254, stats_h)
    monkey_h = 30 + grid_rows * 88
    # 底部单面板（气球强化 + 禁用项 合并），按 mod 行数 + ban 行数估算
    mod_items = len(common._race_modifier_items(mods))
    mod_rows = max(1, -(-mod_items // 2)) if mod_items else 1
    ban_count = sum(1 for key in BAN_ICON if dc.get(key))
    ban_rows = -(-ban_count // 2) if ban_count else 0
    total_rows = mod_rows + ban_rows
    if total_rows == 0:
        total_rows = 1
    rules_body = 18 + total_rows * 44
    bottom_h = 36 + 8 + rules_body + 10
    content_h = 28 + layout_h + 12 + monkey_h + bottom_h
    frame_height = topbar_h + 12 + content_h + 20
    extra_css = ("<style>"
                 ".race-bottom-left .race-mod-item{height:44px;}"
                 ".race-bottom-left .race-mod-icon-cell{width:40px;}"
                 ".race-bottom-left .race-mod-icon{width:36px;height:36px;}"
                 ".race-bottom-left .race-mod-label{font-size:13px;line-height:16px;}"
                 ".race-bottom-left .race-mod-value{font-size:14px;line-height:18px;}"
                 ".race-bottom-full{width:100%;}"
                 ".race-bottom-full .race-mod-item{height:44px;width:50%;}"
                 ".race-bottom-full .race-mod-icon-cell{width:40px;}"
                 ".race-bottom-full .race-mod-icon{width:36px;height:36px;object-fit:contain;}"
                 ".race-bottom-full .race-mod-label{font-size:13px;line-height:16px;}"
                 ".race-bottom-full .race-mod-value{font-size:14px;line-height:18px;color:#ff9a9a;}"
                 ".race-bottom-full .race-mod-row{display:table;width:100%;table-layout:fixed;}"
                 "</style>")
    return extra_css + common._race_shell(body, frame_height)


def _tile_mode_zh(sub, boss: dict) -> tuple[str, str]:
    """subGameType -> (中文名, 站点素材 data URL)。"""
    if sub == 2:
        return "竞速", assets._game_asset_data_url("EventRaceBtn.webp")
    if sub == 4:
        return "Boss", assets._game_asset_data_url("BossTiersIconSmall.webp")
    if sub == 8:
        return "最少现金", assets._game_asset_data_url("LeastCashIcon.webp")
    if sub == 9:
        return "最少层数", assets._game_asset_data_url("LeastTiersIcon.webp")
    return "标准", assets._ui_asset_data_url("ct-tile.png")


def _render_tile_monkey_grid(dc_items: list) -> str:
    """单格详情底部猴塔瓦片网格（沿用 _daily_monkey_grid 站点 daily 卡版式）。

    - 全塔总览：所有塔+英雄按 towersInOrder/heroesInOrder 列出，max=0 隐藏，
      max≠0 留下并打 ×N / 路径封锁角标（与 .btd6每日 / .btd6竞速卡完全一致）
    """
    meta = {"_towers": dc_items}
    return rules._daily_monkey_grid(meta)
