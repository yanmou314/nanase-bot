"""收集活动卡片：Featured Insta Schedule 计划表（1:1 复刻游戏内收集活动菜单）。

版式对照游戏内截图：木质容器 + 左侧时间栏 + 右侧 4 张分类色即时猴瓦片；
首行显示当前轮换倒计时，末行显示活动结束时间。塔瓦片底色与全站一致，
取 cards.common._tower_cat_grad 的分类渐变（初级蓝/军事绿/魔法紫/支援橙）。
"""
from datetime import datetime

from . import common
from .. import assets, i18n, instagen, util


def _col_shell(body: str, h: int) -> str:
    """收集活动外壳：页面沿用全站蓝色渐变，内容为木质计划表容器。"""
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
@page {{ size: {common.ODYSSEY_CARD_W}px {h}px; margin: 0; background: linear-gradient(180deg, #46c8f1 0%, #129ed0 56%, #087eaf 100%); }}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html, body {{ width: {common.ODYSSEY_CARD_W}px; height: {h}px; color: #ffffff;
        font-family: "WenQuanYi Micro Hei", "Noto Sans CJK SC", sans-serif;
        background: linear-gradient(180deg, #46c8f1 0%, #129ed0 56%, #087eaf 100%); overflow: hidden; }}
.col-page {{ padding: 14px; }}
/* 木质计划表容器（游戏内 totem 面板色） */
.col-frame {{ background: linear-gradient(180deg, #83704f 0%, #7a674b 60%, #6e5c42 100%);
              border: 2px solid #5d4d38; border-radius: 20px; padding: 14px 0 0;
              box-shadow: inset 0 2px 0 rgba(255,244,222,.18), 0 2px 0 rgba(30,18,6,.35); }}
.col-head {{ display: table; width: 100%; table-layout: fixed; padding: 2px 20px 10px; }}
.col-headtext {{ display: table-cell; vertical-align: middle; }}
.col-title {{ color: #ffffff; font-size: 30px; line-height: 36px; font-weight: 900;
              text-shadow: 0 2px 0 rgba(40,24,8,.65); letter-spacing: .5px; }}
.col-range {{ color: #f4e4cd; font-size: 20px; line-height: 26px; font-weight: 700; padding-top: 2px;
              text-shadow: 0 1px 0 rgba(40,24,8,.55); }}
.col-badgecell {{ display: table-cell; width: 200px; text-align: right; vertical-align: middle; }}
.col-badge {{ display: inline-block; min-width: 170px; padding: 8px 14px; border-radius: 12px;
              background: linear-gradient(180deg, #3ec1f0 0%, #16a5d8 60%, #0b86b8 100%);
              border: 3px solid #0c6d99; color: #ffffff; font-size: 16px; line-height: 20px;
              font-weight: 900; text-align: center; text-shadow: 0 1px 0 rgba(4,40,58,.6);
              box-shadow: inset 0 2px 0 rgba(255,255,255,.45), 0 2px 0 rgba(9,58,82,.4); }}
.col-badge.off {{ background: linear-gradient(180deg, #9fb6d4 0%, #7d93ad 60%, #5f7185 100%);
                  border-color: #4c5a6a; }}
.col-note {{ padding: 0 20px 12px; color: #fdf3e0; font-size: 14px; line-height: 18px; font-weight: 700;
             text-shadow: 0 1px 0 rgba(40,24,8,.5); }}
.col-row {{ display: table; width: 100%; table-layout: fixed; border-top: 2px solid #3d3020; }}
.col-row.alt {{ background: rgba(0,0,0,.22); }}
.col-when {{ display: table-cell; width: 172px; vertical-align: middle; text-align: center;
             background: #6b5a40; padding: 8px 6px; }}
.col-row.alt .col-when {{ background: #5d4d38; }}
.col-date {{ color: #ffffff; font-size: 22px; line-height: 26px; font-weight: 900;
             text-shadow: 0 2px 0 rgba(30,18,6,.7); white-space: nowrap; }}
.col-time {{ color: #ffe9b8; font-size: 20px; line-height: 24px; font-weight: 900;
             text-shadow: 0 2px 0 rgba(30,18,6,.7); white-space: nowrap; }}
.col-tiles {{ display: table-cell; vertical-align: middle; }}
.col-tilewrap {{ display: inline-block; width: 25%; vertical-align: top; }}
.col-tile {{ position: relative; height: 82px; overflow: hidden; text-align: center;
             box-shadow: inset 0 1px 0 rgba(255,255,255,.28), inset 0 0 0 1px rgba(0,0,0,.22); }}
.col-tile img {{ width: 66px; height: 66px; margin-top: 3px; object-fit: contain;
                 filter: drop-shadow(0 1px 1px rgba(0,0,0,.35)); }}
.col-tname {{ position: absolute; left: 0; right: 0; bottom: 0; background: rgba(24,14,4,.55);
              color: #ffffff; font-size: 12px; line-height: 16px; font-weight: 700;
              white-space: nowrap; overflow: hidden; }}
.col-tile .col-tfallback {{ display: block; padding: 22px 4px 0; color: #ffffff; font-size: 12px;
                            font-weight: 900; text-shadow: 0 1px 0 rgba(0,0,0,.5); }}
.col-endrow {{ display: table; width: 100%; table-layout: fixed; border-top: 2px solid #3d3020;
               border-radius: 0 0 18px 18px; background: rgba(0,0,0,.22); }}
.col-endlabel {{ display: table-cell; vertical-align: middle; padding: 10px 20px;
                 color: #ffffff; font-size: 22px; line-height: 28px; font-weight: 900;
                 text-shadow: 0 2px 0 rgba(30,18,6,.7); }}
.col-foot {{ padding: 10px 20px 0; color: #e8dcc0; font-size: 12px; line-height: 17px;
             font-weight: 700; text-align: center; text-shadow: 0 1px 0 rgba(40,24,8,.5); }}
</style></head>
<body><div class="col-page"><div class="col-frame">{body}</div></div></body></html>"""


def _fmt_slot(ms: int) -> tuple[str, str]:
    """轮换开始时刻 → (日期, 时间)，北京时间。"""
    dt = datetime.fromtimestamp(ms / 1000, tz=util._SH)
    return f"{dt.year}/{dt.month}/{dt.day}", f"{dt:%H:%M}"


def _tile_html(tower: str) -> str:
    img = assets._tower_portrait(tower)
    c0, c1 = common._tower_cat_grad(tower)
    name = i18n.tower_cn(tower)
    face = (f"<img src='{img}' alt='{util._esc(name)}'/>" if img
            else f"<span class='col-tfallback'>{util._esc(name)}</span>")
    return (f"<div class='col-tilewrap'><div class='col-tile' "
            f"style='background:linear-gradient(180deg,{c0},{c1});'>{face}"
            f"<div class='col-tname'>{util._esc(name)}</div></div></div>")


def collectevent_html(col: dict) -> str:
    if col.get("empty"):
        return _col_shell(f"<div style='padding:30px;text-align:center;font-size:15px;'>"
                          f"{util._esc(col['empty'])}</div>", 140)
    ev = col["ev"]
    gen = col["gen"]
    now = int(col.get("now") or 0)
    cur = int(col.get("cur") or 0)
    rotations: dict = gen.get("rotations") or {}
    start = int(ev.get("start") or 0)
    end = int(ev.get("end") or 0)
    total = len(rotations)
    # 进行中从当前轮列到活动结束；未开始/已结束展示整期
    first = 0 if cur < 0 or cur >= total else cur
    ended = now >= end
    upcoming = now < start

    if upcoming:
        badge = "未开始"
    elif ended:
        badge = "已结束"
    else:
        badge = f"剩余 {util.fmt_remaining(end - now)}"

    head = (f"<div class='col-head'><div class='col-headtext'>"
            f"<div class='col-title'>收集活动 · Featured Insta 计划表</div>"
            f"<div class='col-range'>{util._esc(util._fmt_range(ev))}</div></div>"
            f"<div class='col-badgecell'><span class='col-badge{' off' if upcoming or ended else ''}'>"
            f"{util._esc(badge)}</span></div></div>"
            "<div class='col-note'>时间均为北京时间（UTC+8）· 每 8 小时轮换 4 种精选即时猴 · 活动列表与时间可能变动</div>")

    rows = []
    shown = 0
    for idx in range(first, total):
        towers = rotations.get(idx) or []
        if not towers:
            continue
        slot_start = instagen.rotation_start(start, idx)
        if idx == cur and not upcoming:
            label = ("<div class='col-date'>更换于</div>"
                     f"<div class='col-time'>{util._esc(util.fmt_remaining(slot_start + instagen.ROTATION_MS - now))}后</div>")
        else:
            d, t = _fmt_slot(slot_start)
            label = f"<div class='col-date'>{d}</div><div class='col-time'>{t}</div>"
        alt = " alt" if shown % 2 else ""
        rows.append(f"<div class='col-row{alt}'><div class='col-when'>{label}</div>"
                    f"<div class='col-tiles'>{''.join(_tile_html(t) for t in towers)}</div></div>")
        shown += 1

    end_label = "结束于" if not upcoming else "开始于"
    end_ms = end if not upcoming else start
    end_row = (f"<div class='col-endrow'><div class='col-endlabel'>{end_label}："
               f"{util._esc(util.fmt_time(end_ms))}</div></div>")

    foot = ("<div class='col-foot'>计划表由活动种子确定性生成（BTD6 API Explorer 算法），与游戏内菜单一致 · instagen</div>")

    height = 150 + shown * 84 + 48 + 42
    return _col_shell(head + "".join(rows) + end_row + foot, height)
