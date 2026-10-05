"""Boss/活动排行榜卡片：对齐 BTD6 API Explorer 布局。

行结构（自左向右）：
  RANK | 头像+昵称 | Time Submitted（绝对+相对） | Game Time（层数+用时）
顶部：BOSS LEADERBOARD 横幅 + 标准/精英切换条。
"""
import os

from . import common
from .. import util


_MEDAL_COLOR = {1: "#f5c518", 2: "#c0c0c0", 3: "#cd7f32"}
_TIER_ICON_FALLBACK = "🏆"
_WATCH_FALLBACK = "⏱"

# _list_shell 的背景渐变节点（与 common._list_shell 的 linear-gradient 一致）
_LB_BG_STOPS = ((0.0, (70, 200, 241)), (0.56, (18, 158, 208)), (1.0, (8, 126, 175)))


def _lb_probe_height(body: str, probe_prefix: str, generous: int, *, margin: int = 24) -> int:
    """两遍渲染定高：宽松探针实测内容底部，再按实测出正式页面高度。

    行高公式（boss 78px/紧凑 56px）会被表格 border-spacing、长昵称换行、
    提示行等推高，累计误差把最后一名推出页面底边（2026-10-06 实测 25 行
    boss 榜裁掉末行）。与每日三面板卡同思路；探针渲染进 cache/.render
    子目录（max_age=120），不触碰正式卡片缓存；探针失败回退宽松估算值。
    若内容顶到探针页底（初始估算仍偏低，测量不可信），逐轮放大重测（至多 3 轮）。"""
    try:
        from common import render_html_to_png
        from .rules import _daily_content_bottom_css
        probe_dpi = 60
        cache_dir = os.path.join(os.path.dirname(__file__), "cache", ".render")
        g = max(generous, 320)
        need = max(g, 320)
        for _ in range(3):
            probe = render_html_to_png(
                common._list_shell(body, g), probe_prefix, cache_dir,
                max_age=120, dpi=probe_dpi)
            need = _daily_content_bottom_css(probe, probe_dpi, stops=_LB_BG_STOPS) + margin
            need = max(320, min(int(need), 11000))
            if need + 8 <= g:  # 内容在探针页内自然结束：测量可信
                return need
            g = g * 3 // 2 + 400  # 内容顶到页底：估算仍偏低，放大重测
        return need
    except Exception:
        return max(320, min(generous, 11000))


def _lb_banner_html(title: str, sub: str = "", note: str = "", img: str = "") -> str:
    """横幅：主标题 + 剩余时间（sub）都在条内；note 仍放条下。

    img（boss 官图 data URI）存在时作为标题条背景：按官图宽高比撑出横幅高度
    （contain 完整显示不裁切不变形，用户需求 2026-10-06），文字沉底叠在蒙版上；
    宽高比测量失败时回退 180px 高 + contain（同样不裁切）。"""
    note_html = f"<div class='exlb-note'>{util._esc(note)}</div>" if note else ""
    sub_html = f"<div class='exlb-sub'>{util._esc(sub)}</div>" if sub else ""
    bg_attr = ""
    if img:
        ratio = 0.0
        try:
            import base64 as _b64
            from io import BytesIO as _BIO
            from PIL import Image as _PILImage
            payload = img.split(",", 1)[1] if "," in img else ""
            im = _PILImage.open(_BIO(_b64.b64decode(payload)))
            w, h = im.size
            if w > 0 and h > 0:
                ratio = w / h
        except Exception:
            ratio = 0.0
        inner_w = 774  # 800 卡宽 - 横幅左右 margin 20 - 边框 6
        h_px = min(int(inner_w / ratio), 480) if ratio else 180
        pad_top = max(14, h_px - 90)  # 文字沉底：避开官图中部的 logo 区
        bg_attr = (
            f" style=\"min-height:{h_px}px;padding:{pad_top}px 16px 14px;"
            "background-image:linear-gradient(180deg,rgba(6,42,68,.35),rgba(6,42,68,.55)),"
            f"url('" + util._esc(img) + "');"
            "background-size:auto,contain;background-repeat:no-repeat,no-repeat;"
            "background-position:center;\"")
    return (
        f"<div class='exlb-banner'{bg_attr}>"
        f"<div class='exlb-banner-title'>{util._esc(title)}</div>"
        f"{sub_html}"
        "</div>"
        + (f"<div class='exlb-meta'>{note_html}</div>" if note_html else "")
    )


def _lb_tabs_html(active: str = "standard") -> str:
    """标准 / 精英 切换条（Explorer 的 1/2/3/4 PLAYER 简化为模式）。"""
    def tab(key: str, label: str) -> str:
        sel = " sel" if key == active else ""
        return f"<div class='exlb-tab{sel}'>{util._esc(label)}</div>"
    return "<div class='exlb-tabs'>" + tab("standard", "标准") + tab("elite", "精英") + "</div>"


def _lb_row_html(rank: int, name: str, score: str, *,
                 avatar: str = "", submitted: str = "", relative: str = "",
                 tiers: str = "", banner: str = "", watch_icon: str = "",
                 tier_icon: str = "") -> str:
    medal = _MEDAL_COLOR.get(rank)
    if medal:
        rank_html = f"<span class='exlb-rank-medal' style='background:{medal};'>{rank}</span>"
    else:
        rank_html = f"<span class='exlb-rank'>{rank}</span>"
    av = (f"<img class='exlb-avatar' src='{util._esc(avatar)}' alt=''/>"
          if avatar else "<div class='exlb-avatar exlb-avatar-fb'>🐵</div>")
    sub_html = ""
    if submitted or relative:
        sub_html = (
            "<div class='exlb-time-submit'>"
            + (f"<div class='exlb-time-abs'>{util._esc(submitted)}</div>" if submitted else "")
            + (f"<div class='exlb-time-rel'>{util._esc(relative)}</div>" if relative else "")
            + "</div>"
        )
    watch = (f"<img class='exlb-ico' src='{util._esc(watch_icon)}' alt=''/>"
             if watch_icon else f"<span class='exlb-ico-fb'>{_WATCH_FALLBACK}</span>")
    tier_html = ""
    if tiers:
        tico = (f"<img class='exlb-ico' src='{util._esc(tier_icon)}' alt=''/>"
            if tier_icon else _TIER_ICON_FALLBACK + " ")
        tier_html = f"<div class='exlb-tiers'>{tico}{util._esc(tiers)} Tiers</div>"
    bg = f" style=\"background-image:url('{util._esc(banner)}');\"" if banner else ""
    return (
        f"<tr class='exlb-row' {bg.strip() if bg else ''}>"
        f"<td class='exlb-c-rank'>{rank_html}</td>"
        f"<td class='exlb-c-player'>{av}<span class='exlb-name'>{util._esc(name)}</span></td>"
        f"<td class='exlb-c-submit'>{sub_html}</td>"
        f"<td class='exlb-c-score'>{tier_html}"
        f"<div class='exlb-gametime'>{watch}<span>{util._esc(score)}</span></div></td>"
        "</tr>"
    )


def boss_leaderboard_html(col: dict) -> str:
    """Boss 专用：Explorer 风格。"""
    if col.get("empty"):
        return common._list_shell(
            f"<div class='exlb-banner'><div class='exlb-banner-title'>Boss 排行榜</div></div>"
            f"<div class='lb-empty'>{util._esc(col['empty'])}</div>", 180)

    variant = str(col.get("variant") or "standard")
    mode_cn = "精英模式" if variant == "elite" else "标准模式"
    boss_name = (col.get("boss_name") or "").strip()
    title = f"Boss 排行榜 · {mode_cn}" if not boss_name else f"{boss_name} Boss 排行榜 · {mode_cn}"
    entries = col.get("entries_detailed") or []
    # 兼容旧三元组
    if not entries:
        for row in col.get("entries") or []:
            if len(row) >= 3:
                rank, name, score = row[0], row[1], row[2]
                entries.append({"rank": rank, "name": name, "score": score})

    rows_html = "".join(
        _lb_row_html(
            int(e.get("rank") or 0),
            str(e.get("name") or "?"),
            str(e.get("score") or ""),
            avatar=str(e.get("avatar") or ""),
            submitted=str(e.get("submitted") or ""),
            relative=str(e.get("relative") or ""),
            tiers=str(e.get("tiers") or ""),
            banner=str(e.get("banner") or ""),
            watch_icon=str(e.get("watch_icon") or ""),
            tier_icon=str(e.get("tier_icon") or ""),
        )
        for e in entries
    )
    # 表头与数据行放进同一张 table：WeasyPrint 下列宽才严格对齐
    table = (
        "<table class='exlb-table'><thead><tr class='exlb-headrow'>"
        "<th class='exlb-c-rank'>排名</th>"
        "<th class='exlb-c-player'>玩家</th>"
        "<th class='exlb-c-submit'>提交时间</th>"
        "<th class='exlb-c-score'>层数 / 用时</th>"
        "</tr></thead><tbody>"
        + rows_html +
        "</tbody></table>"
    )
    body = (
        _lb_banner_html(title, col.get("status") or "",
                        col.get("stale_note") or "", col.get("img") or "")
        + table
    )
    h = _lb_probe_height(
        body, "btd6lbprobe",
        20 + 56 + 40 + 36 + max(len(entries), 1) * 104 + 300)
    return common._list_shell(body, h)


def leaderboard_html(col: dict) -> str:
    """统一入口：Boss 走 Explorer 布局，其余保持紧凑列表（仍带成绩）。"""
    if col.get("kind") == "boss" or col.get("entries_detailed") and col.get("is_boss"):
        return boss_leaderboard_html(col)
    if col.get("empty"):
        return common._list_shell(
            f"<div class='lb-head'><div class='lb-empty'>{util._esc(col['empty'])}</div></div>", 150)

    # 竞赛/领土：同样加上下文行，成绩在右侧
    note = col.get("stale_note") or ""
    note_html = f"<div class='lb-note'>{util._esc(note)}</div>" if note else ""
    img = col.get("img") or ""
    img_cell = (
        f"<div style='display:table-cell;width:120px;vertical-align:middle;text-align:right;'>"
        f"<img style='width:104px;height:64px;object-fit:cover;border-radius:8px;"
        f"border:2px solid #699bd9;' src='{util._esc(img)}'/></div>"
    ) if img else ""
    title = col.get("head") or "排行榜"
    banner_title = "竞赛排行榜" if "竞赛" in title else ("争夺领土排行榜" if "领土" in title else "排行榜")
    head_body = (
        f"<div style='display:table;width:100%;table-layout:fixed;'>"
        f"<div style='display:table-cell;vertical-align:middle;'>"
        f"<div class='lb-title'>{util._esc(title)}</div>"
        f"<div class='lb-subtitle'>{util._esc(col.get('status') or '')}</div>{note_html}"
        f"</div>{img_cell}</div>"
    )
    rows = []
    for row in col.get("entries") or []:
        rank, name, score = row[0], row[1], row[2]
        medal = _MEDAL_COLOR.get(rank)
        rank_html = (f"<span style='background:{medal};color:#122032;'>{rank:02d}</span>"
                     if medal else f"<span>{rank:02d}</span>")
        rows.append(
            f"<div class='lb-row'><div class='lb-rank'>{rank_html}</div>"
            f"<div class='lb-name'>{util._esc(name)}</div>"
            f"<div class='lb-score'>{util._esc(score)}</div></div>"
        )
    rows_html = "".join(rows) or "<div class='lb-empty'>（暂无上榜数据）</div>"
    body = (f"<div class='exlb-banner'><div class='exlb-banner-title'>{util._esc(banner_title)}</div></div>"
            f"<div class='lb-head'>{head_body}</div>"
            f"<div class='lb-panel'>{rows_html}</div>")
    h = _lb_probe_height(
        body, "btd6lbprobec",
        20 + 56 + 90 + max(len(rows), 1) * 64 + 200)
    return common._list_shell(body, h)
