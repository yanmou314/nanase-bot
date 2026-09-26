"""群员清理：按「最后发言时间」自定义截止点，踢掉此后没再回过消息的人。

仅主人可用。可在目标群内发命令（省略群号），也可在私聊/其他群指定群号远程操作。
默认保护：机器人自己、主人、群主、管理员、保护名单。
执行前请先预览确认；执行会重新拉成员列表再踢。
"""
import asyncio
import logging
import os
import re
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, MessageSegment
from nonebot.params import CommandArg

from common import OWNER, is_owner, load_json_state, save_json_state

_logger = logging.getLogger(__name__)
_SH = ZoneInfo("Asia/Shanghai")

STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")
_LOCK = threading.RLock()

_MAX_KICK_PER_RUN = 40
_KICK_INTERVAL_SEC = 1.2
_PREVIEW_LIST_MAX = 40

_CLEAR_WORDS = {"清除", "取消", "关闭", "重置", "clear", "none", "off"}
_CONFIRM_WORDS = {"确认", "y", "Y", "yes", "执行"}
_DT_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
    "%Y%m%d %H:%M:%S",
    "%Y%m%d %H:%M",
    "%Y%m%d",
)
_REL_RE = re.compile(r"^(\d{1,4})\s*(天|日|d|D)?$")
# 群号：5~12 位数字，可带 g:/群:/# 前缀；相对天数最多 3650（4 位）不会误判
_GID_TOKEN_RE = re.compile(r"^(?:g:|群:|#)?(\d{5,12})$", re.I)

set_time_cmd = on_command("清人时间", aliases={"踢人时间"}, priority=5, block=True)
preview_cmd = on_command("清人预览", aliases={"踢人预览"}, priority=5, block=True)
run_cmd = on_command("清人执行", aliases={"踢人执行"}, priority=5, block=True)
status_cmd = on_command("清人状态", aliases={"踢人状态"}, priority=5, block=True)
protect_cmd = on_command("清人保护", aliases={"踢人保护"}, priority=5, block=True)


def _load_state() -> dict:
    data = load_json_state(STATE_FILE, _LOCK)
    if not isinstance(data.get("groups"), dict):
        data["groups"] = {}
    return data


def _save_state(data: dict) -> None:
    save_json_state(STATE_FILE, data, _LOCK)


def _mutate(mutate) -> dict:
    with _LOCK:
        data = _load_state()
        mutate(data)
        _save_state(data)
        return data


def _group_cfg(data: dict, gid: str) -> dict:
    g = (data.get("groups") or {}).get(gid)
    if not isinstance(g, dict):
        return {}
    return g


def _require_owner(event: MessageEvent) -> None:
    if not is_owner(event):
        raise RuntimeError("只有主人可以操作清人功能")


def split_gid_token(text: str) -> tuple[str | None, str]:
    """从命令参数里拆出可选群号。

    返回 (gid_or_None, rest)。仅当首 token 形如群号时才剥离。
    """
    raw = (text or "").strip()
    if not raw:
        return None, ""
    parts = raw.split(None, 1)
    m = _GID_TOKEN_RE.match(parts[0])
    if not m:
        return None, raw
    rest = parts[1].strip() if len(parts) > 1 else ""
    return m.group(1), rest


def resolve_target_gid(event: MessageEvent, text: str) -> tuple[str | None, str, str | None]:
    """解析目标群号。

    返回 (gid, rest, error)。error 非空时 gid/rest 不可用。
    指定了群号则用群号；否则用当前事件所在群（私聊无 group_id → 报错）。
    """
    gid, rest = split_gid_token(text)
    if gid:
        return gid, rest, None
    event_gid = getattr(event, "group_id", None)
    if event_gid is not None and str(event_gid).strip():
        return str(event_gid), rest, None
    return None, rest, "请指定群号（私聊/其他群操作必须带群号），例如：.清人预览 123456789"


def parse_cutoff(text: str, now: datetime | None = None) -> tuple[str, datetime | None]:
    """解析截止时间：("clear"|"ok"|"error", dt|None)。"""
    raw = (text or "").strip()
    if not raw:
        return "error", None
    low = raw.lower()
    if raw in _CLEAR_WORDS or low in _CLEAR_WORDS:
        return "clear", None
    now = now or datetime.now(_SH)
    if now.tzinfo is not None:
        now = now.astimezone(_SH).replace(tzinfo=None)
    m = _REL_RE.match(raw)
    if m:
        days = int(m.group(1))
        if days <= 0 or days > 3650:
            return "error", None
        return "ok", now - timedelta(days=days)
    for fmt in _DT_FORMATS:
        try:
            return "ok", datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return "error", None


def parse_run_confirm(text: str) -> tuple[str | None, bool]:
    """解析执行命令参数 → (gid|None, confirmed)。"""
    gid, rest = split_gid_token(text)
    return gid, rest.strip() in _CONFIRM_WORDS


def parse_protect_args(text: str) -> tuple[str | None, str]:
    """解析保护命令参数 → (gid|None, rest)。rest 形如 +QQ / -QQ / 空。"""
    return split_gid_token(text)


def cutoff_ts(dt: datetime) -> float:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_SH).timestamp()
    return dt.timestamp()


def format_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def format_ts(ts: float | int | None) -> str:
    try:
        t = float(ts or 0)
    except (TypeError, ValueError):
        return "从未"
    if t <= 0:
        return "从未"
    return datetime.fromtimestamp(t, _SH).strftime("%Y-%m-%d %H:%M")


def member_name(m: dict) -> str:
    return str(m.get("card") or m.get("nickname") or m.get("user_id") or "").strip() or str(
        m.get("user_id")
    )


def safe_last_sent(m: dict) -> float:
    """last_sent_time 安全转 float；缺失/非法一律 0（视作从未发言）。"""
    try:
        return float(m.get("last_sent_time") or 0)
    except (TypeError, ValueError):
        return 0.0


def select_kick_targets(
    members: list[dict],
    cutoff: float,
    protect: set[str],
    bot_id: str | int | None = None,
    owner: str | int | None = None,
) -> list[dict]:
    always = set(protect)
    if bot_id is not None and str(bot_id):
        always.add(str(bot_id))
    if owner is not None and str(owner):
        always.add(str(owner))
    targets: list[dict] = []
    for m in members:
        if not isinstance(m, dict):
            continue
        uid = m.get("user_id")
        if uid is None:
            continue
        uid_s = str(uid)
        if uid_s in always:
            continue
        role = str(m.get("role") or "").lower()
        if role in ("owner", "admin"):
            continue
        if safe_last_sent(m) < cutoff:
            targets.append(m)
    targets.sort(key=safe_last_sent)
    return targets


def mass_wipe_guard(targets: list[dict], members: list[dict]) -> str | None:
    """误踢护栏：接口数据异常时中止执行，返回原因；正常返回 None。"""
    if len(targets) < 5:
        return None
    # 全员 last_sent=0：常见于 NapCat/OneBot 成员缓存异常，会误踢几乎所有人
    if all(safe_last_sent(m) <= 0 for m in targets):
        return (
            f"待清理 {len(targets)} 人的 last_sent_time 全为 0/缺失，"
            "疑似接口数据异常，已中止以免误踢。请稍后重试或先 .清人预览 核对。"
        )
    ordinary = [
        m for m in members
        if str(m.get("role") or "").lower() not in ("owner", "admin")
    ]
    if ordinary and len(targets) >= max(20, int(len(ordinary) * 0.7)):
        return (
            f"待清理 {len(targets)} 人，约占普通成员 {len(ordinary)} 的七成，"
            "已中止以免误踢。请收紧截止时间，或确认数据正常后分批（改小截止范围）再执行。"
        )
    return None


def _usage_time() -> str:
    return (
        "用法（群号可省略=当前群；私聊/其他群必须带群号）：\n"
        ".清人时间 2026-09-01\n"
        ".清人时间 123456789 2026-09-01\n"
        ".清人时间 123456789 30天\n"
        ".清人时间 123456789 清除\n"
        "其他：.清人预览 [群号] / .清人执行 [群号] 确认 / .清人保护 [群号] +QQ"
    )


def _cfg_cutoff_dt(cfg: dict) -> datetime | None:
    raw = cfg.get("cutoff")
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _protect_set(cfg: dict) -> set[str]:
    raw = cfg.get("protect")
    if not isinstance(raw, list):
        return set()
    return {str(x) for x in raw if str(x).strip()}


async def _fetch_members(bot: Bot, gid: int) -> list[dict]:
    members = await bot.get_group_member_list(group_id=gid)
    return [m for m in members if isinstance(m, dict)]


def _bot_role(members: list[dict], bot_id: str) -> str:
    for m in members:
        if str(m.get("user_id")) == bot_id:
            return str(m.get("role") or "").lower()
    return ""


async def _bot_in_group(bot: Bot, gid: int) -> bool:
    try:
        groups = await bot.get_group_list()
    except Exception:
        return True  # 拉不到列表时不挡，交给成员接口报错
    return any(int(g.get("group_id") or 0) == gid for g in groups if isinstance(g, dict))


@set_time_cmd.handle()
async def _set_time(event: MessageEvent, arg=CommandArg()):
    try:
        _require_owner(event)
        gid, rest, err = resolve_target_gid(event, arg.extract_plain_text())
        if err:
            raise RuntimeError(err + "\n" + _usage_time())
        # `.清人时间 20260901`：8 位纯数字且是合法日期时，视为日期而非群号
        if not rest and gid and re.fullmatch(r"\d{8}", gid):
            kind_try, dt_try = parse_cutoff(gid)
            if kind_try == "ok" and dt_try is not None:
                event_gid = getattr(event, "group_id", None)
                if event_gid is None or not str(event_gid).strip():
                    raise RuntimeError(
                        "私聊无法区分群号与 YYYYMMDD 日期，请写全：\n"
                        ".清人时间 123456789 20260901\n" + _usage_time()
                    )
                rest, gid = gid, str(event_gid)
    except RuntimeError as e:
        await set_time_cmd.finish(str(e))
        return
    kind, dt = parse_cutoff(rest)
    if kind == "error":
        await set_time_cmd.finish("时间格式无法识别\n" + _usage_time())
        return
    if kind == "clear":
        def _do(data: dict) -> None:
            g = data.setdefault("groups", {}).setdefault(gid, {})
            g.pop("cutoff", None)

        _mutate(_do)
        await set_time_cmd.finish(f"✅ 已清除群 {gid} 的清人截止时间（不会踢人）")
        return
    assert dt is not None
    if cutoff_ts(dt) > datetime.now(_SH).timestamp():
        await set_time_cmd.finish("❌ 截止时间不能在未来（否则会踢掉所有人）\n" + _usage_time())
        return

    def _save(data: dict) -> None:
        g = data.setdefault("groups", {}).setdefault(gid, {})
        g["cutoff"] = dt.strftime("%Y-%m-%d %H:%M:%S")
        g["updated_at"] = datetime.now(_SH).strftime("%Y-%m-%d %H:%M:%S")

    _mutate(_save)
    await set_time_cmd.finish(
        f"✅ 群 {gid} 清人截止时间：{format_dt(dt)}\n"
        f"预览：.清人预览 {gid}\n"
        f"执行：.清人执行 {gid} 确认\n"
        f"保护名单/群主/管理员/机器人不会被踢。"
    )


@protect_cmd.handle()
async def _protect(event: MessageEvent, arg=CommandArg()):
    try:
        _require_owner(event)
        gid, raw, err = resolve_target_gid(event, arg.extract_plain_text())
        if err:
            raise RuntimeError(err)
    except RuntimeError as e:
        await protect_cmd.finish(str(e))
        return
    raw = (raw or "").strip()
    if not raw:
        cur = _protect_set(_group_cfg(_load_state(), gid))
        body = "、".join(sorted(cur)) if cur else "（空）"
        await protect_cmd.finish(
            f"群 {gid} 清人保护名单：{body}\n"
            f"用法：.清人保护 {gid} +QQ号 / .清人保护 {gid} -QQ号\n"
            f"群主、管理员、机器人、主人始终保护，无需写入。"
        )
        return
    op = raw[0]
    qq = re.sub(r"\D", "", raw[1:])

    def _apply(sign: int) -> set[str]:
        def _do(data: dict) -> None:
            g = data.setdefault("groups", {}).setdefault(gid, {})
            cur = _protect_set(g)
            if sign > 0:
                cur.add(qq)
            else:
                cur.discard(qq)
            g["protect"] = sorted(cur)

        after = _mutate(_do)
        return _protect_set(_group_cfg(after, gid))

    if op in ("+", "＋") and qq:
        _apply(1)
        await protect_cmd.finish(f"✅ 群 {gid} 保护名单加入：{qq}")
        return
    if op in ("-", "－") and qq:
        _apply(-1)
        await protect_cmd.finish(f"✅ 群 {gid} 保护名单移除：{qq}")
        return
    await protect_cmd.finish(
        f"用法：.清人保护 [{gid}] +QQ号 / .清人保护 [{gid}] -QQ号 / .清人保护 [{gid}]（查看）"
    )


@status_cmd.handle()
async def _status(event: MessageEvent, arg=CommandArg()):
    try:
        _require_owner(event)
    except RuntimeError as e:
        await status_cmd.finish(str(e))
        return
    arg_gid, _ = split_gid_token(arg.extract_plain_text())
    data = _load_state()
    groups = data.get("groups") or {}
    if not isinstance(groups, dict) or not groups:
        await status_cmd.finish("尚未配置任何群的清人规则。\n" + _usage_time())
        return
    if arg_gid:
        if arg_gid not in groups:
            await status_cmd.finish(f"群 {arg_gid} 尚未配置清人规则。\n" + _usage_time())
            return
        show = {arg_gid: groups[arg_gid]}
    else:
        show = groups
    lines = ["🛡️ 清人配置"]
    for gid in sorted(show, key=str):
        cfg = show.get(gid) if isinstance(show.get(gid), dict) else {}
        cut = cfg.get("cutoff") or "未设置"
        prot = "、".join(_protect_set(cfg)) or "（空）"
        last = cfg.get("last_run") or cfg.get("last_preview") or ""
        lines.append(f"群 {gid}\n  截止：{cut}\n  保护：{prot}\n  最近操作：{last or '无'}")
    lines.append("\n" + _usage_time())
    await status_cmd.finish("\n".join(lines))


async def _collect(bot: Bot, gid: int, cutoff_dt: datetime) -> tuple[list[dict], list[dict], set[str]]:
    members = await _fetch_members(bot, gid)
    cfg = _group_cfg(_load_state(), str(gid))
    protect = _protect_set(cfg)
    targets = select_kick_targets(
        members,
        cutoff_ts(cutoff_dt),
        protect,
        bot_id=bot.self_id,
        owner=OWNER,
    )
    return members, targets, protect


@preview_cmd.handle()
async def _preview(bot: Bot, event: MessageEvent, arg=CommandArg()):
    try:
        _require_owner(event)
        gid, _, err = resolve_target_gid(event, arg.extract_plain_text())
        if err:
            raise RuntimeError(err)
    except RuntimeError as e:
        await preview_cmd.finish(str(e))
        return
    data = _load_state()
    cutoff_dt = _cfg_cutoff_dt(_group_cfg(data, gid))
    if not cutoff_dt:
        await preview_cmd.finish(f"群 {gid} 尚未设置清人时间。\n" + _usage_time())
        return
    if not await _bot_in_group(bot, int(gid)):
        await preview_cmd.finish(f"❌ 机器人不在群 {gid}，无法操作")
        return
    try:
        members, targets, protect = await _collect(bot, int(gid), cutoff_dt)
    except Exception:
        _logger.warning("清人预览拉取群成员失败 gid=%s", gid, exc_info=True)
        await preview_cmd.finish("拉取群成员列表失败，请稍后再试（确认机器人在该群）")
        return
    bot_role = _bot_role(members, str(bot.self_id))
    role_note = ""
    if bot_role not in ("owner", "admin"):
        role_note = "\n⚠️ 机器人当前不是该群管理员，执行时可能无法踢人。"
    stamp = datetime.now(_SH).strftime("%Y-%m-%d %H:%M:%S")

    def _mark(data: dict) -> None:
        g = data.setdefault("groups", {}).setdefault(gid, {})
        g["last_preview"] = f"{stamp} 截止 {format_dt(cutoff_dt)} 预览 {len(targets)} 人"

    _mutate(_mark)
    head = (
        f"🔍 清人预览（不会踢人）\n"
        f"群：{gid}\n"
        f"截止时间：{format_dt(cutoff_dt)}\n"
        f"保护名单：{'、'.join(sorted(protect)) if protect else '（空）'}\n"
        f"符合条件（最后发言早于截止）：{len(targets)} 人{role_note}\n"
    )
    if not targets:
        await preview_cmd.finish(head + "当前没有需要清理的成员。")
        return
    lines = [head, "名单（按最后发言从旧到新）："]
    for m in targets[:_PREVIEW_LIST_MAX]:
        lines.append(
            f"· {member_name(m)}（{m.get('user_id')}）最后发言 {format_ts(m.get('last_sent_time'))}"
        )
    if len(targets) > _PREVIEW_LIST_MAX:
        lines.append(f"… 另有 {len(targets) - _PREVIEW_LIST_MAX} 人未列出")
    lines.append(f"\n确认无误后发送：.清人执行 {gid} 确认")
    # MessageSegment.text 包裹：昵称由成员自行设置，防伪造 [CQ:...] 借回执注入
    await preview_cmd.finish(MessageSegment.text("\n".join(lines)))


_run_lock = asyncio.Lock()


@run_cmd.handle()
async def _run(bot: Bot, event: MessageEvent, arg=CommandArg()):
    if _run_lock.locked():
        await run_cmd.finish("已有清人任务在执行，请等当前批次结束后再试。")
        return
    async with _run_lock:
        await _run_inner(bot, event, arg)


async def _run_inner(bot: Bot, event: MessageEvent, arg=CommandArg()):
    try:
        _require_owner(event)
        gid, confirmed = parse_run_confirm(arg.extract_plain_text())
        if not gid:
            event_gid = getattr(event, "group_id", None)
            if event_gid is None or not str(event_gid).strip():
                raise RuntimeError(
                    "私聊/其他群执行必须带群号。\n用法：.清人执行 123456789 确认"
                )
            gid = str(event_gid)
            rest = arg.extract_plain_text().strip()
            confirmed = rest in _CONFIRM_WORDS
    except RuntimeError as e:
        await run_cmd.finish(str(e))
        return
    if not confirmed:
        await run_cmd.finish(
            f"⚠️ 清人执行会真实踢出成员，请先 .清人预览 {gid} 确认名单。\n"
            f"确认执行请发送：.清人执行 {gid} 确认"
        )
        return
    data = _load_state()
    cutoff_dt = _cfg_cutoff_dt(_group_cfg(data, gid))
    if not cutoff_dt:
        await run_cmd.finish(f"群 {gid} 尚未设置清人时间。\n" + _usage_time())
        return
    if not await _bot_in_group(bot, int(gid)):
        await run_cmd.finish(f"❌ 机器人不在群 {gid}，无法操作")
        return
    try:
        members, targets, protect = await _collect(bot, int(gid), cutoff_dt)
    except Exception:
        _logger.warning("清人执行拉取群成员失败 gid=%s", gid, exc_info=True)
        await run_cmd.finish("拉取群成员列表失败，未踢任何人")
        return
    if _bot_role(members, str(bot.self_id)) not in ("owner", "admin"):
        await run_cmd.finish(f"❌ 机器人不是群 {gid} 管理员，无法踢人。请先设为管理员。")
        return
    if not targets:
        await run_cmd.finish(f"群 {gid}：截止 {format_dt(cutoff_dt)} 后没有待清理成员，未执行踢人。")
        return
    guard_reason = mass_wipe_guard(targets, members)
    if guard_reason:
        _logger.warning("清人中止 gid=%s：%s", gid, guard_reason)
        await run_cmd.finish(f"❌ {guard_reason}")
        return
    batch = targets[:_MAX_KICK_PER_RUN]
    await run_cmd.send(
        f"🚀 开始清理群 {gid}：截止 {format_dt(cutoff_dt)}，"
        f"本轮最多踢 {len(batch)}/{len(targets)} 人"
    )
    ok: list[str] = []
    fail: list[str] = []
    for m in batch:
        try:
            uid = int(m.get("user_id"))
        except (TypeError, ValueError):
            fail.append(f"{member_name(m)}（非法QQ）")
            continue
        label = f"{member_name(m)}（{uid}）"
        try:
            await bot.set_group_kick(
                group_id=int(gid), user_id=uid, reject_add_request=False
            )
            ok.append(label)
            _logger.info("清人踢出 gid=%s uid=%s", gid, uid)
        except Exception:
            fail.append(label)
            _logger.warning("清人踢出失败 gid=%s uid=%s", gid, uid, exc_info=True)
        await asyncio.sleep(_KICK_INTERVAL_SEC)
    stamp = datetime.now(_SH).strftime("%Y-%m-%d %H:%M:%S")

    def _mark(data: dict) -> None:
        g = data.setdefault("groups", {}).setdefault(gid, {})
        g["last_run"] = f"{stamp} 截止 {format_dt(cutoff_dt)} 成功 {len(ok)} 失败 {len(fail)}"

    _mutate(_mark)
    more = len(targets) - len(batch)
    msg = (
        f"✅ 清人完成\n"
        f"群：{gid}\n"
        f"截止：{format_dt(cutoff_dt)}\n"
        f"成功踢出：{len(ok)}\n"
        f"失败：{len(fail)}\n"
    )
    if ok:
        msg += "成功：" + "、".join(ok[:20])
        if len(ok) > 20:
            msg += f" 等{len(ok)}人"
        msg += "\n"
    if fail:
        msg += "失败：" + "、".join(fail[:10]) + "\n"
    if more > 0:
        msg += (
            f"\n另有 {more} 人超出本轮上限 {_MAX_KICK_PER_RUN}，"
            f"可再次发送：.清人执行 {gid} 确认"
        )
    # 同预览回执：MessageSegment.text 包裹防 CQ 注入
    await run_cmd.finish(MessageSegment.text(msg))
