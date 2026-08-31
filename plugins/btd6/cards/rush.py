"""Boss Rush 卡片：塔池墙 + 逐阶段行（1:1 复刻游戏内活动页）。"""
from . import common
from .. import assets, i18n, util


_RUSH_MAX_MONKEYS = 30  # 每场战斗猴子数量上限（游戏内活动页固定值，转录）


def _relic_cn(r: str) -> str:
    return i18n._RELIC_CN.get(r, r)


def _rush_shell(body: str, h: int) -> str:
    """Boss Rush 专属外壳：与全站统一 token（原版蓝色渐变底 + #213753 面板）。"""
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
@page {{ size: {common.ODYSSEY_CARD_W}px {h}px; margin: 0; background: linear-gradient(180deg, #46c8f1 0%, #129ed0 56%, #087eaf 100%); }}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html, body {{ width: {common.ODYSSEY_CARD_W}px; height: {h}px; color: #ffffff;
        font-family: "WenQuanYi Micro Hei", "Noto Sans CJK SC", sans-serif;
        background: linear-gradient(180deg, #46c8f1 0%, #129ed0 56%, #087eaf 100%); overflow: hidden; }}
.rush-pool {{ display: flex; flex-wrap: wrap; justify-content: center; margin-bottom: 12px; }}
.rush-cell {{ position: relative; width: 88px; height: 88px; border-radius: 8px;
              border: 1px solid rgba(0,0,0,.28); display: flex; align-items: center;
              justify-content: center; margin: 0 3px 3px 0;
              box-shadow: inset 0 1px 0 rgba(255,255,255,.35), 0 1px 0 rgba(0,0,0,.25); }}
.rush-cell img {{ width: 74px; height: 74px; object-fit: contain;
                  filter: drop-shadow(0 1px 1px rgba(0,0,0,.35)); }}
.rush-cell i.slash {{ position: absolute; inset: 0;
                      background: linear-gradient(135deg, transparent 42%, #e0442e 46%, #e0442e 54%, transparent 58%); }}
.rush-stage {{ position: relative; display: flex; gap: 12px; background: #213753;
               border: 1px solid #2f4a6d; border-radius: 10px; padding: 10px 12px;
               margin-bottom: 10px; box-shadow: inset 0 1px 0 rgba(255,255,255,.06), 0 1px 0 rgba(0,0,0,.2); }}
.rush-map {{ position: relative; flex: none; width: 168px; height: 120px; }}
.rush-mapimg {{ width: 100%; height: 100%; object-fit: cover; border-radius: 6px; }}
.rush-mapname {{ position: absolute; left: 0; right: 0; bottom: 0; text-align: center;
                 background: rgba(6,14,26,.82); color: #ffd964; font-size: 13px;
                 font-weight: 900; line-height: 24px; letter-spacing: 1px; }}
.rush-stagebanner {{ position: absolute; top: -8px; left: -6px; padding: 2px 10px; z-index: 2;
                     background: linear-gradient(180deg,#ff9a3d,#e2611b); border: 2px solid #93400f;
                     border-radius: 4px; color: #ffffff; font-size: 13px; font-weight: 900;
                     box-shadow: 0 2px 0 rgba(0,0,0,.35); }}
.rush-bossimg {{ position: absolute; right: 26px; bottom: 22px; width: 86px; object-fit: contain;
                 z-index: 3; transform: scaleX(-1); filter: drop-shadow(0 2px 3px rgba(0,0,0,.45)); }}
.rush-mid {{ flex: 1; min-width: 0; }}
.rush-chips {{ display: flex; flex-wrap: wrap; gap: 6px; }}
.rush-chip {{ display: flex; align-items: center; gap: 5px; padding: 3px 10px; border-radius: 5px;
              background: #344d6c; border: 1px solid #3d5a7f; color: #ffffff;
              font-size: 12px; line-height: 18px; font-weight: 700; white-space: nowrap;
              text-shadow: 0 1px 0 rgba(2,10,20,.5); }}
.rush-chip img {{ width: 15px; height: 15px; object-fit: contain; }}
.rush-relics {{ display: flex; gap: 12px; margin-top: 9px; align-items: flex-start; }}
.rush-relic {{ position: relative; width: 48px; height: 48px; border-radius: 50%; background: #16324e;
               border: 2px solid #699bd9; display: flex; align-items: center; justify-content: center; }}
.rush-relic img {{ width: 40px; height: 40px; object-fit: contain; }}
.rush-relic.new {{ border-color: #fecb00; box-shadow: 0 0 0 1px #fecb00; }}
.rush-newribbon {{ position: absolute; top: -10px; right: -12px; width: 34px; object-fit: contain; }}
.rush-lost {{ flex: none; width: 200px; background: rgba(11,22,38,.45); border: 1px solid #2f4a6d;
              border-radius: 6px; padding: 6px 8px; }}
.rush-lost-title {{ color: #ff6a5a; font-size: 11px; font-weight: 900; margin-bottom: 6px; }}
.rush-lost-body {{ display: flex; flex-wrap: wrap; gap: 3px; align-items: center; }}
.rush-none {{ color: #9fb6d4; font-size: 11px; }}
.rush-note {{ margin-top: 4px; text-align: center; font-size: 10px; color: #8fa8c4; }}
</style></head><body>{body}</body></html>"""


def _rush_diff_html(col: dict, d: str = "default", label: str = "") -> str:
    """Boss Rush 卡片：1:1 复刻游戏内活动页版式（塔池墙 + 逐阶段行，全汉化）。"""
    if col.get("empty"):
        return _rush_shell(f"<div style='padding:30px;text-align:center;font-size:15px;'>{util._esc(col['empty'])}</div>", 160)
    ev = col["ev"]
    maps = ((col.get("diffs") or {}).get("default") or {}).get("maps") or []

    event_name = (ev.get("name") or "Boss Rush").strip() or "Boss Rush"

    # ---- 顶部塔池墙（同类同色底块，对照截图两行猴子头像） ----
    pool = maps[0]["towers"] if maps else []
    pool_cells = []
    for t in pool:
        img = assets._tower_portrait(t)
        c0, c1 = common._tower_cat_grad(t)
        face = (f"<img src='{img}' alt='{util._esc(i18n.tower_cn(t))}' style='width:74px;height:74px;"
                f"object-fit:contain;filter:drop-shadow(0 1px 1px rgba(0,0,0,.35));'/>" if img
                else f"<div style='font-size:10px;color:#e8dcc0;'>{util._esc(i18n.tower_cn(t))}</div>")
        pool_cells.append(
            f"<div class='rush-cell' style='background:linear-gradient(180deg,{c0},{c1});'>{face}</div>")
    pool_block = f"<div class='rush-pool'>{''.join(pool_cells)}</div>"

    skull = ("<svg width='14' height='14' viewBox='0 0 24 24' fill='#e05545' style='flex:none;'>"
             "<path d='M12 2C7 2 3 6 3 11c0 2.4 1.2 4.5 3 5.7V19c0 1.1.9 2 2 2h1v-2h2v2h2v-2h2v2h1c1.1 0 2-.9 2-2v-2.3"
             "c1.8-1.2 3-3.3 3-5.7 0-5-4-9-9-9zM8.5 13a1.8 1.8 0 110-3.6 1.8 1.8 0 010 3.6zm7 0a1.8 1.8 0 110-3.6"
             " 1.8 1.8 0 010 3.6z'/></svg>")
    coin_icon = assets._game_asset_data_url("UI_CoinIcon.webp")
    new_ribbon = assets._game_asset_data_url("NewRibbon.webp")

    rows = []
    for mp in maps:
        boss_name_cn = i18n.boss_cn(mp["boss"])
        rewards = mp.get("rewards") or []
        mm = next((v for k, v in rewards if k == "猴币"), "-")
        tr = next((v for k, v in rewards if k == "奖杯"), "-")
        tt = next((v for k, v in rewards if k == "战队奖杯"), "-")
        extra = " · ".join(f"{k} {v}" for k, v in rewards if k not in ("猴币", "奖杯", "战队奖杯"))

        # 遗物圆牌：官方遗物图标 + 金环 + NEW 缎带
        relic_badges = []
        for r in mp["relics"]:
            is_new = r == mp.get("new_relic")
            r_img = assets._game_asset_data_url(f"{r}.webp")
            inner = (f"<img src='{r_img}' alt='{util._esc(_relic_cn(r))}'/>" if r_img
                     else f"<span style='font-size:9px;'>{util._esc(_relic_cn(r))}</span>")
            ribbon = (f"<img class='rush-newribbon' src='{new_ribbon}' alt='新'/>"
                      if is_new and new_ribbon else
                      ("<div class='rush-newtag'>新</div>" if is_new else ""))
            relic_badges.append(
                f"<div class='rush-relic{' new' if is_new else ''}'>{inner}{ribbon}</div>")

        # 损失猴子：与顶部塔池同样的大立绘底块 + 红斜杠
        lost_body = []
        for t in mp.get("removed") or []:
            img = assets._tower_portrait(t)
            c0, c1 = common._tower_cat_grad(t)
            if img:
                lost_body.append(
                    f"<div class='rush-cell' style='background:linear-gradient(180deg,{c0},{c1});'>"
                    f"<img src='{img}' alt='{util._esc(i18n.tower_cn(t))}' style='filter:grayscale(.6);opacity:.85;'/>"
                    f"<i class='slash'></i></div>")
            else:
                lost_body.append(
                    f"<span style='padding:1px 6px;border-radius:4px;background:rgba(255,90,70,.14);"
                    f"border:1px solid rgba(255,90,70,.4);font-size:10px;color:#ff9a8a;"
                    f"line-height:16px;text-decoration:line-through;'>{util._esc(i18n.tower_cn(t))}</span>")
        lost_inner = "".join(lost_body) if lost_body else "<span class='rush-none'>暂无</span>"

        rows.append(
            "<div class='rush-stage'>"
            "<div class='rush-map'>"
            + (f"<img class='rush-mapimg' src='{util._esc(mp['map_img'])}' alt='{util._esc(mp['map_name'])}'/>"
               if mp.get("map_img") else
               "<div class='rush-mapimg' style='background:linear-gradient(160deg,#3f6f5e,#26473c);'></div>")
            + f"<div class='rush-mapname'>{util._esc(mp['map_name'])}</div>"
            f"<div class='rush-stagebanner'>第 {mp['stage']} 阶段</div>"
            + (f"<img class='rush-bossimg' src='{util._esc(mp['img'])}' alt='{util._esc(boss_name_cn)}'/>" if mp.get("img") else "")
            + "</div>"
            + "<div class='rush-mid'>"
            + "<div class='rush-chips'>"
            + f"<span class='rush-chip'>{skull}<span style='color:#ffb0a0;'>{mp['kills']} 击杀需求</span></span>"
            + (f"<span class='rush-chip'><img src='{coin_icon}'/>{mm} 猴币</span>" if coin_icon
               else f"<span class='rush-chip'>{mm} 猴币</span>")
            + "<span class='rush-chip' style='background:#3f5a78;border-color:#2b3f57;color:#bfe3ff;'>"
            + f"🐵 {_RUSH_MAX_MONKEYS} 最大猴子数量</span>"
            + (f"<span class='rush-chip'>奖杯 {tr} · 战队奖杯 {tt}"
               + (f" · {extra}" if extra else "") + "</span>" if (tr != "-" or tt != "-" or extra) else "")
            + "</div>"
            + f"<div class='rush-relics'>{''.join(relic_badges)}</div>"
            + "</div>"
            + "<div class='rush-lost'><div class='rush-lost-title'>损失猴子：</div>"
            + f"<div class='rush-lost-body'>{lost_inner}</div></div>"
            + "</div>"
        )

    body = (pool_block
            + "".join(rows)
            + f"<div class='rush-note'>{util._esc(event_name)} · {util._esc(util._fmt_range(ev))} · "
            + "阶段配置由活动种子确定性生成，与游戏内一致</div>"
            # 数据版本标识：rushdata.json 裁剪自 Constants.json v3.0.0，游戏更新参数后需同步
            + "<div class='rush-note' style='margin-top:2px;'>数据版本: Constants v3.0.0 · rushgen</div>")
    # 高度：塔池墙(每行8格×91px，按实际塔数折行) + 阶段行×152 + 底部注释30 + 版本行18。
    # 旧公式硬编码"2行"恰与当前 16 塔数据零余量，塔池一扩到 17 塔即被 overflow:hidden 裁切
    pool_rows = max(1, -(-len(pool) // 8))
    height = 200 + (max(0, pool_rows - 2)) * 91 + len(maps) * 152 + 48
    return _rush_shell(body, height)
