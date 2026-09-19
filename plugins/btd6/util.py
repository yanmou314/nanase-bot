"""时间/状态/格式化纯工具：全插件最低层，不依赖包内其他模块。"""
import html as html_mod
import time
from datetime import datetime
from zoneinfo import ZoneInfo


_SH = ZoneInfo("Asia/Shanghai")


BUCKET_PERIOD_MIN = 15        # 取整桶周期：倒计时文案按此粒度分窗 + API 数据 TTL；与预热频率解耦
BUCKET_MS = BUCKET_PERIOD_MIN * 60 * 1000     # bucket_now() 使用的毫秒桶宽


ENDED_SHOW = 8                    # 总览"已结束"段展示的场数（文本与卡片两路统一）


def fmt_time(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=_SH)
    return f"{dt.month}月{dt.day}日 {dt:%H:%M}"


def fmt_date(ms: float | None) -> str:
    if not ms:
        return "未知日期"
    dt = datetime.fromtimestamp(ms / 1000, tz=_SH)
    return f"{dt.year}-{dt.month:02d}-{dt.day:02d}"


def fmt_remaining(delta_ms: int) -> str:
    total_min = int(max(0, delta_ms) // 60000)
    days, rest = divmod(total_min, 1440)
    hours, minutes = divmod(rest, 60)
    if days >= 1:
        return f"{days}天{hours}小时"
    if hours >= 1:
        return f"{hours}小时{minutes}分"
    return f"{minutes}分钟"


def event_status_line(ev: dict, now_ms: int) -> str:
    start, end = int(ev.get("start") or 0), int(ev.get("end") or 0)
    if now_ms < start:
        return f"{fmt_remaining(start - now_ms)}后开始（{fmt_time(start)} 开启）"
    if now_ms < end:
        return f"剩余 {fmt_remaining(end - now_ms)}（{fmt_time(end)} 结束）"
    return f"已于 {fmt_time(end)} 结束"


def pick_active(items: list, now_ms: int):
    for it in items:
        if int(it.get("start") or 0) <= now_ms < int(it.get("end") or 0):
            return it
    return None


def pick_next(items: list, now_ms: int):
    upcoming = sorted((i for i in items if int(i.get("start") or 0) > now_ms),
                      key=lambda x: x.get("start") or 0)
    return upcoming[0] if upcoming else None


def fallback_latest(items: list):
    """列表按新→旧排列时取第一场，作为无进行中活动时的兜底展示。"""
    return items[0] if items else None


def bucket_now() -> int:
    """按预热周期取整的"当前时间"：同窗口内倒计时文案不变，渲染缓存才能命中预热卡。"""
    return int(time.time() * 1000) // BUCKET_MS * BUCKET_MS


def _classify_overview_events(
    races: list, bosses: list, cts: list, now_ms: int,
    odysseys: list | None = None, rush: list | None = None,
    socials: list | None = None, collectables: list | None = None,
) -> tuple[list[tuple[dict, str]], list[tuple[dict, str]], list[tuple[dict, str]]]:
    """将全部活动按时间状态分为三类：进行中 / 即将开始 / 已结束.

    返回 (ongoing, upcoming, ended)，每项为 (ev, kind) 其中 kind ∈ {race,boss,ct,odyssey,rush,social,collectable}.
    - ongoing:  start <= now < end，按结束时间升序（先结束的在前）
    - upcoming: start > now，按开始时间升序（最近开始的在前）
    - ended:    end <= now，按结束时间降序（最近结束的在前）；不截断，
                展示层统一用 ENDED_SHOW 控制条数（文本与卡片一致）。
    兼容旧调用：odysseys 为空时与旧版一致。
    """
    all_events: list[tuple[dict, str]] = []
    for ev in races or []:
        if isinstance(ev, dict):
            all_events.append((ev, "race"))
    for ev in bosses or []:
        if isinstance(ev, dict):
            all_events.append((ev, "boss"))
    for ev in cts or []:
        if isinstance(ev, dict):
            all_events.append((ev, "ct"))
    for ev in odysseys or []:
        if isinstance(ev, dict):
            all_events.append((ev, "odyssey"))
    for ev in rush or []:
        if isinstance(ev, dict):
            all_events.append((ev, "rush"))
    for ev in socials or []:
        if isinstance(ev, dict):
            all_events.append((ev, "social"))
    for ev in collectables or []:
        if isinstance(ev, dict):
            all_events.append((ev, "collectable"))
    ongoing: list[tuple[dict, str]] = []
    upcoming: list[tuple[dict, str]] = []
    ended: list[tuple[dict, str]] = []
    for ev, kind in all_events:
        try:
            s = int(ev.get("start") or 0)
            e = int(ev.get("end") or 0)
        except (TypeError, ValueError):
            continue
        if e <= 0 or s <= 0:
            continue
        if s <= now_ms < e:
            ongoing.append((ev, kind))
        elif now_ms < s:
            upcoming.append((ev, kind))
        elif now_ms >= e:
            ended.append((ev, kind))
    ongoing.sort(key=lambda x: int(x[0].get("end") or 0))
    upcoming.sort(key=lambda x: int(x[0].get("start") or 0))
    ended.sort(key=lambda x: int(x[0].get("end") or 0), reverse=True)
    return ongoing, upcoming, ended


def fmt_lb_score(scoring: str | None, entry: dict) -> str:
    """按计分类型从排行榜条目取成绩（与游戏一致）。

    Boss 榜顶层 score 只是「Boss Tier」层数，真正成绩在 scoreParts
    （Game Time / Least Cash / Boss Tier）。竞赛顶层 score 即用时。
    """
    st = str(scoring or "")
    entry = entry if isinstance(entry, dict) else {}
    parts = entry.get("scoreParts") or []

    def part(*names: str):
        for p in parts:
            if not isinstance(p, dict):
                continue
            if str(p.get("name") or "") in names:
                return p.get("score")
        return None

    # Boss 榜（有 Boss Tier）字段命名与直觉相反（与 API Explorer 一致）：
    # - "Least Cash"：通关用时（毫秒），GameTime 计分时展示的就是这个
    # - "Game Time"：相对活动 start 的提交偏移，用来算 Time Submitted
    # - "Boss Tier"：层数
    is_boss = part("Boss Tier") is not None or any(
        isinstance(p, dict) and str(p.get("name") or "") == "Boss Tier" for p in parts
    )
    if is_boss:
        if st == "GameTime":
            # 只能取 "Least Cash"（实为用时）；不能回退到 Boss Tier（层数 5）
            return fmt_score("GameTime", part("Least Cash"))
        if st == "LeastCash":
            return fmt_score("LeastCash", part("Least Cash") if part("Least Cash") is not None else entry.get("score"))
        if st == "LeastTiers":
            return fmt_score("LeastTiers", part("Boss Tier", "Tier") or entry.get("score"))
        return fmt_score(st, entry.get("score"))
    # 竞赛等：顶层 score / scoreParts 的 Game Time 即用时
    if st == "GameTime":
        v = part("Game Time")
        if v is None:
            v = entry.get("score")
        return fmt_score("GameTime", v)
    if st == "LeastCash":
        v = part("Least Cash")
        if v is None:
            v = entry.get("score")
        return fmt_score("LeastCash", v)
    if st == "LeastTiers":
        v = part("Boss Tier")
        if v is None:
            v = entry.get("score")
        return fmt_score("LeastTiers", v)
    return fmt_score(st, entry.get("score"))


def fmt_relative_time(ms: int | None, now_ms: int | None = None) -> str:
    """相对时间：19 hours ago / 2 days ago（Explorer 同口径英文短文案）。"""
    if not ms:
        return ""
    now = now_ms if now_ms is not None else int(__import__("time").time() * 1000)
    delta = max(0, now - int(ms))
    sec = delta // 1000
    if sec < 60:
        return "JUST NOW"
    if sec < 3600:
        m = sec // 60
        return f"{m} MINUTE{'S' if m != 1 else ''} AGO"
    if sec < 86400:
        h = sec // 3600
        return f"{h} HOUR{'S' if h != 1 else ''} AGO"
    d = sec // 86400
    return f"{d} DAY{'S' if d != 1 else ''} AGO"


def fmt_score(scoring: str | None, score) -> str:
    """按计分类型格式化分数：竞赛毫秒用时→分:秒.毫秒，最少现金→$，其余千分位。"""
    st = str(scoring or "")
    try:
        n = float(score)
    except (TypeError, ValueError):
        return str(score)
    if st == "GameTime":
        # 与 API Explorer formatScoreTime 一致：≥1 小时显示 HH:MM:SS.cc，
        # 否则 MM:SS.cc。避免 Boss 总用时被拼成 2459:02 这种四位数分钟。
        ms = int(n)
        total_sec = ms // 1000
        cs = (ms % 1000) // 10
        hours = total_sec // 3600
        minutes = (total_sec % 3600) // 60
        seconds = total_sec % 60
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{cs:02d}"
        return f"{minutes:02d}:{seconds:02d}.{cs:02d}"
    if st == "LeastCash":
        return f"${int(n):,}"
    if st == "LeastTiers":
        return str(int(n))
    return f"{int(n):,}"


def fmt_cn_num(n) -> str:
    """大数中文单位：1,884,842,684 → 18.8亿；94,098,63 → 941万。"""
    try:
        v = float(n or 0)
    except (TypeError, ValueError):
        return str(n)
    if abs(v) >= 1e8:
        return f"{v / 1e8:.1f}亿"
    if abs(v) >= 1e4:
        return f"{v / 1e4:.0f}万"
    return f"{int(v):,}"


def _esc(value) -> str:
    return html_mod.escape(str(value), quote=True)


def _state_of(ev: dict, now_ms: int) -> str:
    start, end = int(ev.get("start") or 0), int(ev.get("end") or 0)
    if now_ms < start:
        return "up"
    if now_ms < end:
        return "on"
    return "off"


def _pick_section(items: list, now_ms: int):
    return pick_active(items, now_ms) or pick_next(items, now_ms) or fallback_latest(items)


def _fmt_range(ev: dict) -> str:
    s = datetime.fromtimestamp(int(ev.get("start") or 0) / 1000, tz=_SH)
    e = datetime.fromtimestamp(int(ev.get("end") or 0) / 1000, tz=_SH)
    return f"{s.year}/{s.month}/{s.day} - {e.year}/{e.month}/{e.day}"


_STATE_TXT = {"on": "进行中", "up": "未开始", "off": "已结束"}
