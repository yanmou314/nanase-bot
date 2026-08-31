"""排行榜与自制地图卡片（深蓝列表页风格：行式排名表 + 金银铜名次块）。"""
from . import common
from .. import util


# 前三名名次块配色（金 / 银 / 铜，取样自设计规范）
_MEDAL_COLOR = {1: "#f5c518", 2: "#c0c0c0", 3: "#cd7f32"}


def leaderboard_html(col: dict) -> str:
    """排行榜：深蓝渐变底 + 头部信息面板 + 行式排名表。"""
    if col.get("empty"):
        return common._list_shell(f"<div class='lb-head'><div class='lb-empty'>{util._esc(col['empty'])}</div></div>", 150)
    # 头部信息面板（数据超 24h 未刷新时附加过期提示小字）
    note = col.get("stale_note") or ""
    note_html = f"<div class='lb-note'>{util._esc(note)}</div>" if note else ""
    img = col.get("img") or ""
    img_cell = (f"<div style='display:table-cell;width:120px;vertical-align:middle;text-align:right;'>"
                f"<img style='width:104px;height:64px;object-fit:cover;border-radius:8px;"
                f"border:2px solid #699bd9;' src='{util._esc(img)}'/></div>") if img else ""
    head_body = (f"<div style='display:table;width:100%;table-layout:fixed;'>"
                 f"<div style='display:table-cell;vertical-align:middle;'>"
                 f"<div class='lb-title'>{util._esc(col['head'])}</div>"
                 f"<div class='lb-subtitle'>{util._esc(col['status'])}</div>{note_html}"
                 f"</div>{img_cell}</div>")
    head_html = f"<div class='lb-head'>{head_body}</div>"
    entries = col["entries"]
    if entries:
        rows = []
        for i, name, score_txt in entries:
            medal = _MEDAL_COLOR.get(i)
            if medal:
                # 金银铜名次块：深色文字保证对比度
                rank_html = f"<span style='background:{medal};color:#122032;'>{i:02d}</span>"
            else:
                rank_html = f"<span>{i:02d}</span>"
            rows.append(
                f"<div class='lb-row'><div class='lb-rank'>{rank_html}</div>"
                f"<div class='lb-name'>{util._esc(name)}</div>"
                f"<div class='lb-score'>{util._esc(score_txt)}</div></div>"
            )
        rows_html = "".join(rows)
    else:
        rows_html = "<div class='lb-empty'>（暂无上榜数据）</div>"
    body = head_html + f"<div class='lb-panel'>{rows_html}</div>"
    h = 24 + 100 + max(len(entries), 1) * 56 + 56
    return common._list_shell(body, h)


def maps_html(col: dict) -> str:
    """自制地图：深蓝列表页 + 行式地图卡（缩略图 + 游玩/点赞）。"""
    entries = col["entries"]
    label = col["label"]
    header = f"<div class='lb-head'><div class='lb-title'>自制地图 · {util._esc(label)} Top{len(entries)}</div></div>"
    if not entries:
        return common._list_shell(header + "<div class='lb-panel'><div class='lb-empty'>（暂无地图数据）</div></div>", 190)
    rows = []
    for i, name, created, img, plays, upvotes in entries:
        thumb = (f"<img class='ody-map-img' src='{util._esc(img)}'/>" if img else "<div class='ody-map-empty'>🗺</div>")
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
