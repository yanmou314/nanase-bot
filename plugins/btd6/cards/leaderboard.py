"""Boss/活动排行榜卡片：对齐 BTD6 API Explorer 布局。

行结构（自左向右）：
  RANK | 头像+昵称 | Time Submitted（绝对+相对） | Game Time（层数+用时）
顶部：BOSS LEADERBOARD 横幅 + 标准/精英切换条。
"""
from . import common
from .. import util


_MEDAL_COLOR = {1: "#f5c518", 2: "#c0c0c0", 3: "#cd7f32"}
_TIER_ICON_FALLBACK = "🏆"
_WATCH_FALLBACK = "⏱"


def _lb_banner_html(title: str, sub: str = "", note: str = "", img: str = "") -> str:
    """横幅：主标题 + 剩余时间（sub）都在条内；note 仍放条下。"""
    note_html = f"<div class='exlb-note'>{util._esc(note)}</div>" if note else ""
    sub_html = f"<div class='exlb-sub'>{util._esc(sub)}</div>" if sub else ""
    img_html = (f"<img class='exlb-hero-img' src='{util._esc(img)}' alt=''/>" if img else "")
    return (
        "<div class='exlb-banner'>"
        f"<div class='exlb-banner-title'>{util._esc(title)}</div>"
        f"{sub_html}{img_html}"
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
    h = 20 + 56 + 40 + 36 + max(len(entries), 1) * 78 + 24
    return common._list_shell(body, min(h, 3200))


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
    h = 20 + 56 + 90 + max(len(rows), 1) * 56 + 40
    return common._list_shell(body, h)


def maps_html(col: dict) -> str:
    """自制地图：保持原列表布局。"""
    entries = col["entries"]
    label = col["label"]
    header = f"<div class='lb-head'><div class='lb-title'>自制地图 · {util._esc(label)} Top{len(entries)}</div></div>"
    if not entries:
        return common._list_shell(header + "<div class='lb-panel'><div class='lb-empty'>（暂无地图数据）</div></div>", 190)
    rows = []
    for i, name, created, img, plays, upvotes in entries:
        thumb = (f"<img class='ody-map-img' src='{util._esc(img)}' alt=''/>" if img else "<div class='ody-map-empty'>🗺</div>")
        rows.append(
            f"<div class='map-row'><div class='map-img-cell'>{thumb}</div>"
            f"<div class='map-info'>"
            f"<div class='map-name'>#{i:02d} {util._esc(name)}</div>"
            f"<div class='map-meta'>"
            f"<div class='map-meta-item'>▶ 游玩 {plays:,}</div>"
            f"<div class='map-meta-item'>♥ 点赞 {upvotes:,}</div>"
            f"<div class='map-meta-item'>{util._esc(created)}</div>"
            f"</div></div></div>"
        )
    body = header + f"<div class='map-panel'>{''.join(rows)}</div>"
    h = 24 + 56 + max(len(entries), 1) * 126 + 56
    return common._list_shell(body, min(h, 2600))
