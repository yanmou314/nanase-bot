"""Boss Rush 卡片：逐阶段块（1:1 复刻 BTD6 API Explorer 的 Boss Rush 页布局）。"""
from . import common
from .. import assets, i18n, rushgen, util


_RUSH_MAX_MONKEYS = 30  # 每场战斗猴子数量上限（rushdata bossRush.BalanceSettings.MaxTowerCount）
_GRID_PER_ROW = 8       # 每行猴子格数（与网站 grid-template-columns: repeat(8, auto) 一致）
_CELL_W, _CELL_H = 82, 98  # 塔格尺寸（容器图 100×120 等比缩至 0.82）


def _relic_cn(r: str) -> str:
    return i18n.relic_cn(r)


def _rush_shell(body: str, h: int) -> str:
    """Boss Rush 专属外壳：原版蓝色渐变页底 + 深棕内容框（网站 relic-container 配色）。"""
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
@page {{ size: {common.ODYSSEY_CARD_W}px {h}px; margin: 0; background: #4B3B2F; }}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html, body {{ width: {common.ODYSSEY_CARD_W}px; height: {h}px; color: #ffffff;
        font-family: "WenQuanYi Micro Hei", "Noto Sans CJK SC", sans-serif;
        background: #4B3B2F; overflow: hidden; }}
.rush-frame {{ margin: 0; padding: 12px 14px; background: #4B3B2F; border: 0;
               border-radius: 12px; box-shadow: inset 0 1px 0 rgba(255,255,255,.08), 0 2px 4px rgba(0,0,0,.3); }}
.rush-title {{ margin-bottom: 10px; padding: 8px 12px; background: #5C4B3E; border: 1px solid #6e5a48;
               border-radius: 10px; text-align: center; color: #ffd964; font-size: 17px; font-weight: 900;
               letter-spacing: 1px; text-shadow: 0 1px 0 rgba(0,0,0,.75); }}
table.rush-head {{ table-layout: fixed; width: 100%; border-collapse: collapse; background: #5C4B3E; border-radius: 10px;
                   margin-bottom: 10px; }}
td.rush-mapcell {{ width: 210px; padding: 8px; vertical-align: top; }}
td.rush-statcell {{ padding: 8px 8px 8px 0; vertical-align: top; }}
td.rush-towercell {{ padding: 0 8px 8px 8px; text-align: left; }}
.rush-map {{ position: relative; width: 186px; height: 114px; overflow: hidden;
             border: 3px solid #cfd8e0; border-radius: 8px; box-shadow: 0 2px 3px rgba(0,0,0,.35); }}
.rush-mapimg {{ width: 100%; height: 100%; object-fit: cover; }}
.rush-stagebanner {{ position: absolute; top: 2px; left: 4px; color: #ffffff; font-size: 15px;
                     font-weight: 900; text-shadow: 1px 0 0 #000, -1px 0 0 #000, 0 1px 0 #000,
                     0 -1px 0 #000, 1px 1px 0 #000, -1px 1px 0 #000, 1px -1px 0 #000, -1px -1px 0 #000,
                     0 2px 3px rgba(0,0,0,.7); }}
.rush-mapname {{ position: absolute; left: 0; right: 0; bottom: 2px; text-align: center; color: #ffffff;
                 font-size: 15px; font-weight: 900;
                 text-shadow: 1px 0 0 #000, -1px 0 0 #000, 0 1px 0 #000, 0 -1px 0 #000,
                 1px 1px 0 #000, -1px 1px 0 #000, 1px -1px 0 #000, -1px -1px 0 #000,
                 0 2px 3px rgba(0,0,0,.7); }}
.rush-mapboss {{ position: absolute; right: -4px; top: -6px; width: 80px; object-fit: contain;
                 transform: scaleX(-1); filter: drop-shadow(0 2px 2px rgba(0,0,0,.5)); }}
.rush-chip {{ display: inline-block; margin: 0 14px 6px 0; color: #ffffff;
              font-size: 14px; line-height: 24px; font-weight: 900; white-space: nowrap;
              text-shadow: 0 1px 0 rgba(0,0,0,.75); }}
.rush-chip img {{ width: 24px; height: 24px; object-fit: contain; vertical-align: -6px; margin-right: 5px; }}
table.rush-sub {{ border-collapse: collapse; margin-top: 4px; width: 100%; }}
td.rush-reliccell {{ padding: 0 10px 0 0; vertical-align: top; }}
td.rush-lostcell {{ width: 172px; padding: 0; vertical-align: top; text-align: right; }}
.rush-relicswrap {{ display: inline-block; background: #4B3B2F; border-radius: 8px; padding: 5px; }}
.rush-relic {{ position: relative; display: inline-block; width: 50px; height: 58px; margin: 0 4px 2px 0;
               border-radius: 8px; background: #5C3B2F;
               outline: 3px solid rgba(255,255,255,.25); outline-offset: -3px;
               text-align: center; }}
.rush-relic.new {{ background: #7C3C9C; outline: 3px solid #ffd700; }}
.rush-relic img.relic {{ width: 38px; height: 38px; object-fit: contain; margin-top: 8px; }}
.rush-relic img.newribbon {{ position: absolute; top: -8px; left: 5px; width: 40px; object-fit: contain; z-index: 3; }}
.rush-lost {{ display: inline-block; width: 166px; background: #3B2B2B; border-radius: 8px; padding: 5px 6px; }}
.rush-lost-title {{ color: #FF6666; font-size: 12px; font-weight: 900; text-align: center; margin-bottom: 3px;
                    text-shadow: 0 1px 0 rgba(0,0,0,.7); }}
.rush-lost-body {{ text-align: center; }}
.rush-lost .rush-cell {{ width: 68px; height: 80px; margin: 2px 1px 1px; }}
.rush-lost .rush-cell img.monkey {{ width: 56px; height: 56px; margin-top: 8px; }}
.rush-lost .rush-cell img.slash {{ left: 3px; top: 4px; width: 26px; height: 26px; }}
.rush-none {{ color: #d8c8b4; font-size: 14px; font-weight: 900; }}
.rush-towers {{ display: inline-block; background: #4B3B2F; border-radius: 8px; padding: 6px 0 2px; text-align: left; vertical-align: top;
               overflow: hidden; }}
.rush-cell {{ position: relative; display: inline-block; width: {_CELL_W}px; height: {_CELL_H}px;
              margin: 0 14px 4px 0; background-size: contain; background-repeat: no-repeat;
              background-position: center bottom; text-align: center; }}
.rush-cell img.monkey {{ width: 70px; height: 70px; object-fit: contain; margin-top: 12px;
                         filter: drop-shadow(0 1px 1px rgba(0,0,0,.35)); }}
.rush-cell img.slash {{ position: absolute; left: 8px; top: 12px; width: 66px; height: 66px; object-fit: contain; }}
.rush-note {{ margin-top: 4px; text-align: center; font-size: 10px; color: #cbb89f; }}
</style></head><body>{body}</body></html>"""


def _container_bg(t: str) -> str:
    """塔分类 → 容器底图（网站 TowerContainer*.webp），未知分类回退 Primary。"""
    cat = common._rush_tower_category(t)
    fname = {"Primary": "TowerContainerPrimary", "Military": "TowerContainerMilitary",
             "Magic": "TowerContainerMagic", "Support": "TowerContainerSupport",
             "Hero": "TowerContainerHero"}.get(cat, "TowerContainerPrimary")
    url = assets._game_asset_data_url(fname + ".webp")
    # style 属性用单引号包裹，url() 内必须用双引号（写成 &quot;），否则属性被截断
    return f"background-image:url(&quot;{url}&quot;);" if url else ""


def _rush_diff_html(col: dict, d: str = "default", label: str = "") -> str:
    """Boss Rush 卡片：逐阶段块（地图+Boss 图标 / 需求角标 / 遗物 / 损失猴子 / 本阶段塔池）。"""
    if col.get("empty"):
        return _rush_shell(f"<div style='padding:30px;text-align:center;font-size:15px;'>{util._esc(col['empty'])}</div>", 160)
    ev = col["ev"]
    maps = ((col.get("diffs") or {}).get("default") or {}).get("maps") or []
    # 每阶段初始资金按 Boss 取自 BalanceSettings.StartingCash（与网站/游戏内一致）
    start_cash = ((rushgen.load_constants().get("bossRush") or {}).get("BalanceSettings")
                  or {}).get("StartingCash") or {}


    kills_icon = assets._game_asset_data_url("BossRushKills.webp")
    coin_icon = assets._game_asset_data_url("UI_CoinIcon.webp")
    monkeys_icon = assets._game_asset_data_url("MaxMonkeysIcon.webp")
    new_ribbon = assets._game_asset_data_url("NewRibbon.webp")
    strike_icon = assets._game_asset_data_url("StrikethroughRound.webp")

    def _chip(icon: str, text: str, alt_fallback: str = "") -> str:
        if icon:
            return f"<span class='rush-chip'><img src='{util._esc(icon)}' alt=''/>{util._esc(text)}</span>"
        return f"<span class='rush-chip'>{util._esc(alt_fallback or text)}</span>"

    def _monkey_cell(t: str, lost: bool = False, last_in_row: bool = False) -> str:
        img = assets._tower_portrait(t)
        if not img:
            # 立绘缺失时用网站容器图标兜底（InstaContainer 000-{塔}，Wizard 特名）
            n = "Wizard" if t == "WizardMonkey" else t
            img = assets._game_asset_data_url(f"InstaContainer000-{n}.webp")
        face = (f"<img class='monkey' src='{util._esc(img)}' alt='{util._esc(i18n.tower_cn(t))}'/>" if img
                else f"<div style='font-size:10px;color:#3a2c1c;margin-top:30px;'>{util._esc(i18n.tower_cn(t))}</div>")
        slash = (f"<img class='slash' src='{util._esc(strike_icon)}' alt=''/>" if lost and strike_icon else "")
        extra = "margin-right:0;" if (last_in_row and not lost) else ""
        return (f"<div class='rush-cell' style='{extra}{_container_bg(t)}'>{face}{slash}</div>")

    rows = []
    for idx, mp in enumerate(maps):
        cash = start_cash.get(mp["boss"], "-")

        # Boss 图标：网站 BossIcon/{Boss}Portrait.webp，回退游戏立绘
        boss_img = assets._game_asset_data_url(f"BossIcon{mp['boss']}Portrait.webp") or mp.get("img") or ""

        # 遗物牌：新遗物紫底金边 + NEW 缎带（第 1 阶段不挂，与网站一致）
        relic_badges = []
        for r in mp["relics"]:
            is_new = r == mp.get("new_relic")
            r_img = assets._game_asset_data_url(f"{r}.webp")
            inner = (f"<img class='relic' src='{r_img}' alt='{util._esc(_relic_cn(r))}'/>" if r_img
                     else f"<span style='font-size:9px;'>{util._esc(_relic_cn(r))}</span>")
            ribbon = (f"<img class='newribbon' src='{new_ribbon}' alt='新'/>"
                      if is_new and idx > 0 and new_ribbon else "")
            relic_badges.append(f"<div class='rush-relic{' new' if is_new else ''}'>{inner}{ribbon}</div>")

        # 损失猴子
        lost_body = [_monkey_cell(t, lost=True) for t in mp.get("removed") or []]
        lost_inner = "".join(lost_body) if lost_body else "<span class='rush-none'>暂无</span>"

        # 本阶段可用猴子网格
        towers = mp.get("towers") or []
        # 格子散开铺满整行：8 格 x82 + 7 间距 x14 = 754，占满面板内容 756
        cells = [_monkey_cell(t, last_in_row=((i + 1) % _GRID_PER_ROW == 0)) for i, t in enumerate(towers)]
        grid_cells = "".join(cells)
        table_w = common.ODYSSEY_CARD_W - 28  # 外框 padding 14x2，无 margin/border
        panel_w = table_w - 16  # 底块占满格子行内容宽，深色铺满不露底色

        rows.append(
            "<div class='rush-stage'>"
            "<table class='rush-head'><tr>"
            "<td class='rush-mapcell'>"
            "<div class='rush-map'>"
            + (f"<img class='rush-mapimg' src='{util._esc(mp['map_img'])}' alt='{util._esc(mp['map_name'])}'/>"
               if mp.get("map_img") else
               "<div class='rush-mapimg' style='background:linear-gradient(160deg,#3f6f5e,#26473c);'></div>")
            + f"<div class='rush-stagebanner'>第 {mp['stage']} 阶段</div>"
            + (f"<img class='rush-mapboss' src='{util._esc(boss_img)}' alt='{util._esc(i18n.boss_cn(mp['boss']))}'/>"
               if boss_img else "")
            + f"<div class='rush-mapname'>{util._esc(mp['map_name'])}</div>"
            + "</div>"
            + "</td>"
            + "<td class='rush-statcell'>"
            + "<div>"
            + _chip(kills_icon, f"{mp['kills']} 击杀需求", "💀")
            + _chip(coin_icon, f"{cash} 初始资金")
            + _chip(monkeys_icon, f"{_RUSH_MAX_MONKEYS} 最大猴子数量")
            + "</div>"
            + "<table class='rush-sub'><tr>"
            + "<td class='rush-reliccell'>"
            + (f"<div class='rush-relicswrap'>{''.join(relic_badges)}</div>" if relic_badges else "")
            + "</td>"
            + "<td class='rush-lostcell'>"
            + "<div class='rush-lost'><div class='rush-lost-title'>损失猴子：</div>"
            + f"<div class='rush-lost-body'>{lost_inner}</div></div>"
            + "</td>"
            + "</tr></table>"
            + "</td>"
            + "</tr>"
            + "<tr><td class='rush-towercell' colspan='2'>"
            + f"<div class='rush-towers' style='width:{panel_w}px;'>{grid_cells}</div>"
            + "</td></tr>"
            + "</table>"
            + "</div>"
        )

    body = ("<div class='rush-frame'>"
            + f"<div class='rush-title'>👹 Boss Rush 竞速冲刺 · {util._esc(util._fmt_range(ev))}</div>"
            + "".join(rows)
            + "<div class='rush-note'>阶段配置由活动种子确定性生成，与游戏内一致</div>"
            # 数据版本标识：rushdata.json 裁剪自 Constants.json v3.0.0，游戏更新参数后需同步
            + "<div class='rush-note' style='margin-top:2px;'>数据版本: Constants v3.0.0 · rushgen</div>"
            + "</div>")
    # 高度：外框(margin12×2 + padding12×2 + border2×2) + 逐阶段(头行 + 网格行×101 + 底距10) + 注释 46
    # 头行高随“损失猴子”行数增长（盒内两列排布，每行 69px）
    head_h_base = 170
    stage_h = 0
    for mp in maps:
        lost_rows = max(1, -(-len(mp.get("removed") or []) // 2))
        head_h = max(head_h_base, 50 + lost_rows * 90)
        stage_h += head_h + max(1, -(-len(mp.get("towers") or []) // _GRID_PER_ROW)) * (_CELL_H + 4) + 26
    height = 52 + stage_h + 46 + 16
    return _rush_shell(body, height)
