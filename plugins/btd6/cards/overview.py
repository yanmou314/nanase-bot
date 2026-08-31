"""活动总览卡片（三段式图标列表）。"""
import re

from . import common
from .. import assets, i18n, util


_SEC_COLOR = {"race": "#4a90d9", "boss": "#d95c5c", "ct": "#d9a94e"}


def overview_html(data: dict) -> str:
    """活动总览：三段式 - 正在进行 / 即将开始 / 已结束(近5)，从上到下排列。"""
    now = data["now"]
    races = data.get("races") or []
    bosses = data.get("bosses") or []
    cts = data.get("cts") or []
    odysseys = data.get("odysseys") if isinstance(data.get("odysseys"), list) else []
    rush = data.get("rush") if isinstance(data.get("rush"), list) else []
    ongoing, upcoming, ended = util._classify_overview_events(races, bosses, cts, now, odysseys, rush)
    parts: list[str] = []
    total_h = 6

    def _row_html(ev: dict, kind: str, state: str) -> str:
        """单行活动：圆形图标 + 名称 + 日期范围 + 右侧状态。"""
        # 图标：按 kind 取最匹配的圆形方块图（已有 race-tile/boss-tile/odyssey-tile/ct-tile 为横幅，回退旧 boss-* 与 odyssey-event/ct-event 方形）
        icon_data = ""
        if kind == "race":
            # 中性活动图标优先：Bloonarius 是特定 Boss 的头像，用作所有竞速活动的默认图标语义不符
            icon_data = assets._ui_asset_data_url("boss-event-official.png") or assets._ui_asset_data_url("race-tile.png") or ""
        elif kind == "boss":
            bt = str(ev.get("bossType") or "").strip().lower()
            boss_icon_map = {
                "bloonarius": "boss-bloonarius.png",
                "lych": "boss-lych.png",
                "vortex": "boss-vortex.png",
                "dreadbloon": "boss-dreadbloon.png",
                "phayze": "boss-phayze.png",
                "blastapopoulos": "boss-blastapopoulos.png",
            }
            asset = boss_icon_map.get(bt, "")
            icon_data = assets._ui_asset_data_url(asset) or assets._ui_asset_data_url("boss-event-official.png") or assets._ui_asset_data_url("boss-tile.png") or ""
        elif kind == "odyssey":
            icon_data = assets._ui_asset_data_url("odyssey-event.png") or assets._ui_asset_data_url("odyssey-tile.png") or ""
        elif kind == "rush":
            icon_data = assets._ui_asset_data_url("boss-rush.png") or assets._ui_asset_data_url("boss-event.png") or assets._ui_asset_data_url("boss-tile.png") or ""
        elif kind == "ct":
            icon_data = assets._ui_asset_data_url("ct-event.png") or assets._ui_asset_data_url("ct-tile.png") or ""
        # 默认占位（彩色圆底白字）
        if not icon_data:
            ph = {"race": "🏁", "boss": "👹", "odyssey": "🏰", "ct": "⚔️", "rush": "🎈"}.get(kind, "🎈")
            icon_html = f"<div class='ev-icon' style='background:#3a5a7a'>{ph}</div>"
        else:
            icon_html = f"<img class='ev-icon' src='{util._esc(icon_data)}'/>"
        # 名称：按 kind 翻译为中文，保留 NK API 原文作小字副标题
        raw_name = (ev.get("name") or "").strip()
        generic = i18n._EVENT_NAME_CN.get(raw_name)
        if kind == "race":
            name = "每周竞速活动" if generic else f"每周竞赛 · {raw_name or 'Race Event'}"
        elif kind == "boss":
            bt_raw = str(ev.get("bossType") or "").strip()
            if not bt_raw:
                bt_raw = re.sub(r"\d+$", "", str(ev.get("id") or "").split("_", 1)[0])
            bt_cn = i18n.boss_cn(bt_raw)
            if generic:
                name = f"Boss 战 · {bt_cn}" if bt_cn else generic
            else:
                name = f"{raw_name}（{bt_cn}）" if bt_cn and bt_cn not in raw_name else (raw_name or "Boss 事件")
        elif kind == "odyssey":
            name = "远征活动" if generic else f"远征 · {raw_name or 'Odyssey Event'}"
        elif kind == "rush":
            name = "Boss 竞速冲刺" if generic else f"Boss Rush · {raw_name}"
        elif kind == "ct":
            name = "争夺领土（CT）"
        else:
            name = generic or raw_name or "未知活动"
        # 日期
        s, e = int(ev.get("start") or 0), int(ev.get("end") or 0)
        date1 = util.fmt_date(s)
        date2 = util.fmt_date(e)
        # 状态（中文）
        if state == "on":
            if s and e:
                # 复用 util.fmt_remaining（含分钟粒度）：剩余不足 1 小时不再显示"剩余 0 小时"
                right = f"剩余 {util.fmt_remaining(e - now)}"
            else:
                right = "进行中"
            right_cls = "ev-right-on"
        elif state == "up":
            right = "即将开始"
            right_cls = "ev-right-up"
        else:
            right = "已结束"
            right_cls = "ev-right-off"
        return (
            f"<div class='ev-row'>"
            f"<div class='ev-icon-cell'>{icon_html}</div>"
            f"<div class='ev-name'>{util._esc(name)}</div>"
            f"<div class='ev-dates'><div>开始 {util._esc(date1)}</div><div>结束 {util._esc(date2)}</div></div>"
            f"<div class='ev-right {right_cls}'>{util._esc(right)}</div>"
            f"</div>"
        )

    def _section(title: str, items: list, empty_text: str = "暂无活动") -> tuple[str, int]:
        """三段式：分区面板（标题条 + 行式倒计时列表或空态）。返回 (html, height)."""
        h = [f"<div class='ev-section'><div class='ev-banner'>{util._esc(title)}</div>"]
        height = 66
        if not items:
            h.append(f"<div class='ev-empty'>{util._esc(empty_text)}</div>")
            height += 48
        else:
            for ev, kind in items:
                s, e = int(ev.get("start") or 0), int(ev.get("end") or 0)
                state = "on" if s <= now < e else ("up" if now < s else "off")
                h.append(_row_html(ev, kind, state))
                height += 61
        h.append("</div>")
        return "".join(h), height

    # 正在进行
    h1, total_h = _section(f"🟢 进行中 ({len(ongoing)})", ongoing,
                            empty_text="暂无进行中的活动")
    parts.append(h1)
    parts.append('<div class="ev-spacer"></div>')
    total_h += 12

    # 即将开始
    h2, add = _section(
        f"🟡 即将开始 ({len(upcoming)})", upcoming,
        empty_text="暂无即将开始的活动",
    )
    parts.append(h2)
    total_h += add + 12
    parts.append('<div class="ev-spacer"></div>')

    # 已结束（最近 ENDED_SHOW 场，与文本总览条数一致）
    ended_show = ended[:util.ENDED_SHOW] if ended else []
    h3, add = _section(f"⚪ 已结束 · 近 {len(ended_show)} 场",
                        [(ev, kind) for ev, kind in ended_show],
                        empty_text="暂无已结束的活动")
    parts.append(h3)
    total_h += add

    # 数据超 24h 未刷新时在卡片底部附加过期提示小字
    note = data.get("stale_note") or ""
    if note:
        parts.append(f"<div class='ev-empty'>{util._esc(note)}</div>")
        total_h += 48

    return common._list_shell("\n".join(parts), total_h + 18)
