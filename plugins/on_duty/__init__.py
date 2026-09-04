"""上号组队榜：.上号A=B 创建 A+B 人小队（A=已有人数含队长、B=还缺），.加入N 入队，
.谁玩 按队号查看全部小队，.下班 队长解散全队 / 队员仅移除自己。

- 所有命令带前导点「.」且**严格完全匹配**：消息仅含单个文本段、全角数字/＝/．
  归一化后与命令形态全等（@/引用/图片混排、多词、缺点消息不触发）；仅群聊生效。
- 按群开关：默认关闭，主人 .上号开启/关闭/状态 管理。
- 小队创建后 24 小时自动解散（有人加入会重新计时）；.上号A=B 的 A 含队长本人，
  多出的 A-1 席记为「随行」占位（随行的人不单独登记，显示为 随行N人）。
- 状态按群独立落盘 plugins/on_duty/state.json（.gitignore 已排除），重启不丢。
  开群集合带内存缓存（规则每条消息都会查询），手工改 state.json 的 enabled 需重启。
"""
import asyncio
import logging
import os
import re
import threading
import time

from nonebot import on_command, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageSegment
from nonebot.params import CommandArg
from nonebot.rule import Rule

from common import at_prefix, is_owner, load_json_state, save_json_state

_logger = logging.getLogger(__name__)

STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")
_LOCK = threading.RLock()

MAX_SQUAD_LIFETIME = 24 * 3600   # 小队存活上限：超时自动解散（有人加入会重新计时）
MAX_SQUADS_PER_GROUP = 10        # 单群同时存在的小队数上限
MAX_SQUAD_SIZE = 30              # 单支小队总人数上限
MAX_CREATE_PART = 20             # .上号A=B 中 A、B 各自的上限

WORD_QUERY = ".谁玩"
WORD_OFF = ".下班"
WORD_ON_HINT = ".上号"
RE_CREATE = re.compile(r"\.上号(\d+)=(\d+)")
RE_JOIN = re.compile(r"\.加入(\d{1,2})")

# 全角数字/＝/．归一化：.上号２＝３ 与 .上号2=3 等价
_FULLWIDTH_TRANS = str.maketrans("０１２３４５６７８９＝．", "0123456789=.")


def _now() -> float:
    return time.time()


def _int0(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _load_state() -> dict:
    data = load_json_state(STATE_FILE, _LOCK)
    return data if isinstance(data, dict) else {}


def _save_state(data: dict) -> None:
    data.pop("groups", None)  # 旧版单人榜遗留键，新组队逻辑不再使用
    save_json_state(STATE_FILE, data, _LOCK)


# ---------------- 按群开关 ----------------

# 开群集合内存缓存：规则对每条群消息都会求值，不能每次同步读盘卡事件循环；
# 仅经 _set_enabled 变更（写穿失效重读），手工改 state.json 的 enabled 需重启生效
_ENABLED_CACHE: set[str] | None = None


def _load_enabled() -> set[str]:
    global _ENABLED_CACHE
    if _ENABLED_CACHE is None:
        enabled = _load_state().get("enabled")
        _ENABLED_CACHE = set(enabled) if isinstance(enabled, list) else set()
    return _ENABLED_CACHE


def _is_enabled(gid: str) -> bool:
    return gid in _load_enabled()


def _set_enabled(gid: str, enabled: bool) -> bool:
    """开启/关闭本群上号榜。返回状态是否真的变化。"""
    global _ENABLED_CACHE
    with _LOCK:
        state = _load_state()
        enabled_list = state.get("enabled") if isinstance(state.get("enabled"), list) else []
        changed = (gid not in enabled_list) if enabled else (gid in enabled_list)
        if enabled:
            if gid not in enabled_list:
                enabled_list.append(gid)
        else:
            enabled_list = [g for g in enabled_list if g != gid]
        if changed:
            state["enabled"] = sorted(enabled_list)
            _save_state(state)
            _ENABLED_CACHE = None  # 写穿失效：下一条消息的规则查询重读一次
    return changed


def _parse_gid(event, arg) -> int | None:
    """参数里给群号就用参数，否则取当前群。"""
    text = arg.extract_plain_text().strip()
    if text.isdigit():
        return int(text)
    gid = getattr(event, "group_id", None)
    return int(gid) if gid else None


# ---------------- 严格匹配 ----------------

def _plain_text(event) -> str | None:
    """严格形态检查：群聊、仅单个文本段。命中返回归一化（全角数字/＝/．转半角）
    并去首尾空白后的文本；@/引用/图片混排、多词、私聊返回 None。"""
    if not isinstance(event, GroupMessageEvent):
        return None
    msg = event.message
    if len(msg) != 1 or msg[0].type != "text":
        return None
    return str(msg[0].data.get("text", "")).strip().translate(_FULLWIDTH_TRANS)


def _match_word(event, word: str) -> bool:
    """严格完全匹配：归一化文本与 word（含前导点）全等。"""
    return _plain_text(event) == word


def _match_re(event, pattern: re.Pattern) -> re.Match | None:
    """严格匹配的参数版：归一化文本做正则 fullmatch；未命中返回 None。"""
    text = _plain_text(event)
    if text is None:
        return None
    return pattern.fullmatch(text)


async def _rule_create(event: GroupMessageEvent) -> bool:
    return _is_enabled(str(event.group_id)) and _match_re(event, RE_CREATE) is not None


async def _rule_join(event: GroupMessageEvent) -> bool:
    return _is_enabled(str(event.group_id)) and _match_re(event, RE_JOIN) is not None


async def _rule_query(event: GroupMessageEvent) -> bool:
    return _is_enabled(str(event.group_id)) and _match_word(event, WORD_QUERY)


async def _rule_off(event: GroupMessageEvent) -> bool:
    return _is_enabled(str(event.group_id)) and _match_word(event, WORD_OFF)


async def _rule_hint(event: GroupMessageEvent) -> bool:
    return _is_enabled(str(event.group_id)) and _match_word(event, WORD_ON_HINT)


# ---------------- 小队操作（纯逻辑，handler 与测试共用） ----------------

def _squads_of(state: dict, gid: str) -> list:
    all_squads = state.get("squads") if isinstance(state.get("squads"), dict) else {}
    lst = all_squads.get(gid)
    return lst if isinstance(lst, list) else []


def _persist_squads(state: dict, gid: str, squads: list) -> None:
    all_squads = state.get("squads") if isinstance(state.get("squads"), dict) else {}
    if squads:
        all_squads[gid] = squads
    else:
        all_squads.pop(gid, None)
    state["squads"] = all_squads
    _save_state(state)


def _filled(sq: dict) -> int:
    """已占席数 = 队长 1 + 随行占位 + 已加入成员。"""
    return 1 + _int0(sq.get("reserved")) + len(sq.get("members") or [])


def _open(sq: dict) -> int:
    return _int0(sq.get("capacity")) - _filled(sq)


def _purge_expired(squads: list) -> int:
    """解散超 24 小时的小队，返回解散数量。"""
    now = _now()
    keep = [sq for sq in squads
            if now - float(sq.get("refreshed") or sq.get("created") or 0) < MAX_SQUAD_LIFETIME]
    removed = len(squads) - len(keep)
    squads[:] = keep
    return removed


def _create_squad(gid: str, uid: str, have: int, need: int) -> tuple[bool, int, str]:
    """创建小队：容量 have+need，队长占 1 席，随行占位 have-1。返回 (ok, 队号, 错误文案)。"""
    with _LOCK:
        state = _load_state()
        squads = _squads_of(state, gid)
        if _purge_expired(squads):
            _persist_squads(state, gid, squads)
        for i, sq in enumerate(squads):
            if sq.get("leader") == uid:
                return False, i + 1, f"你已创建 {i + 1}号队（先 .下班 解散再开新的）"
            if any(m.get("uid") == uid for m in (sq.get("members") or [])):
                return False, i + 1, f"你已在 {i + 1}号队，先 .下班 退出再开新的"
        if len(squads) >= MAX_SQUADS_PER_GROUP:
            return False, 0, f"小队太多（上限 {MAX_SQUADS_PER_GROUP} 支），请先用 .下班 清理"
        squads.append({
            "leader": uid, "capacity": have + need, "reserved": have - 1,
            "members": [], "created": int(_now()), "refreshed": int(_now()),
        })
        _persist_squads(state, gid, squads)
        return True, len(squads), ""


def _join_squad(gid: str, uid: str, no: int) -> tuple[bool, str]:
    """加入 no 号队。返回 (ok, 回复文案)。"""
    with _LOCK:
        state = _load_state()
        squads = _squads_of(state, gid)
        if _purge_expired(squads):
            _persist_squads(state, gid, squads)
        if not 1 <= no <= len(squads):
            return False, f"没有 {no}号队（当前共 {len(squads)} 支），发「.谁玩」查看"
        for i, sq in enumerate(squads):
            if sq.get("leader") == uid:
                return False, f"你已创建 {i + 1}号队，不能同时加入其他小队"
            if any(m.get("uid") == uid for m in (sq.get("members") or [])):
                return False, f"你已在 {i + 1}号队，先 .下班 退出再加入"
        sq = squads[no - 1]
        if _open(sq) <= 0:
            return False, f"{no}号队已满员（{_filled(sq)}/{sq.get('capacity')}）"
        sq.setdefault("members", []).append({"uid": uid, "ts": int(_now())})
        sq["refreshed"] = int(_now())
        _persist_squads(state, gid, squads)
        return True, f"{no}号队（{_filled(sq)}/{sq.get('capacity')}）"


def _leave_squad(gid: str, uid: str) -> tuple[str, str]:
    """下班：队长解散全队，队员仅移除自己（队伍保留）。

    返回 (kind, msg)，kind ∈ leader / member / none。"""
    with _LOCK:
        state = _load_state()
        squads = _squads_of(state, gid)
        if _purge_expired(squads):
            _persist_squads(state, gid, squads)
        for i, sq in enumerate(squads):
            if sq.get("leader") == uid:
                squads.pop(i)
                _persist_squads(state, gid, squads)
                return "leader", f"{i + 1}号队已解散"
            members = sq.get("members") or []
            mine = next((m for m in members if m.get("uid") == uid), None)
            if mine is not None:
                members.remove(mine)
                sq["refreshed"] = int(_now())
                _persist_squads(state, gid, squads)
                return "member", f"{i + 1}号队"
        return "none", ""


async def _member_name(bot: Bot, gid: str, uid: str) -> str:
    """名单显示名：OW 绑定名优先，其次群名片/群昵称；都无则回退 QQ 号。"""
    tag = _bound_tag(uid)
    if tag:
        return tag
    try:
        info = await bot.get_group_member_info(group_id=int(gid), user_id=int(uid))
        name = str((info or {}).get("card") or (info or {}).get("nickname") or "").strip()
        if name:
            return name
    except Exception:
        _logger.debug("上号榜获取群成员 %s 群名片失败", uid, exc_info=True)
    return uid


def _bound_tag(uid: str) -> str:
    """OW 插件（owstats）的绑定名（名字#数字）；未绑定或 owstats 不可用返回空串。"""
    try:
        from plugins.owstats import _get_bound  # 延迟导入，避免插件加载顺序耦合
    except Exception:
        return ""
    try:
        return str(_get_bound(uid) or "").strip()
    except Exception:
        return ""


async def _squads_text(bot: Bot, gid: str) -> str:
    """全部小队的纯文本名单（不 @ 人）；无小队返回空串。"""
    with _LOCK:
        state = _load_state()
        squads = _squads_of(state, gid)
        if _purge_expired(squads):
            _persist_squads(state, gid, squads)
        snapshot = [(i + 1, dict(sq)) for i, sq in enumerate(squads)]
    if not snapshot:
        return ""
    lines = [f"🎮 当前小队 {len(snapshot)} 支："]
    for no, sq in snapshot:
        leader = await _member_name(bot, gid, str(sq.get("leader") or ""))
        open_n = _open(sq)
        head = f"【{no}号队】{_filled(sq)}/{_int0(sq.get('capacity'))} ｜ 队长：{leader}"
        if _int0(sq.get("reserved")) > 0:
            head += f"（随行{sq['reserved']}人）"
        head += " ｜ 已满员" if open_n <= 0 else f" ｜ 缺{open_n}人"
        lines.append(head)
        members = sq.get("members") or []
        if members:
            names = await asyncio.gather(*(
                _member_name(bot, gid, str(m.get("uid") or "")) for m in members))
            lines.append("　已加入：" + "、".join(names))
        else:
            lines.append("　已加入：暂无")
        if open_n > 0:
            lines.append(f"　空位 {open_n} 个，发「.加入{no}」加入")
    return "\n".join(lines)


# ---------------- 命令处理（priority=3 + block：命中后不再传给聊天/复读） ----------------

create_matcher = on_message(Rule(_rule_create), priority=3, block=True)


@create_matcher.handle()
async def _(event: GroupMessageEvent):
    m = _match_re(event, RE_CREATE)
    have, need = int(m.group(1)), int(m.group(2))
    if not (1 <= have <= MAX_CREATE_PART and 1 <= need <= MAX_CREATE_PART
            and have + need <= MAX_SQUAD_SIZE):
        await create_matcher.finish(at_prefix(event) + MessageSegment.text(
            "用法：.上号已有=还缺，如 .上号2=3 = 已有2人、缺3人，共5人小队，你当队长"
            f"（两项各 ≤{MAX_CREATE_PART}，总人数 ≤{MAX_SQUAD_SIZE}）"))
    ok, no, err = _create_squad(str(event.group_id), str(event.user_id), have, need)
    if not ok:
        await create_matcher.finish(at_prefix(event) + MessageSegment.text(err))
    await create_matcher.finish(at_prefix(event) + MessageSegment.text(
        f"🎮 {no}号队已创建！共{have + need}人（已有{have}、缺{need}），队友发「.加入{no}」加入"))


join_matcher = on_message(Rule(_rule_join), priority=3, block=True)


@join_matcher.handle()
async def _(event: GroupMessageEvent):
    m = _match_re(event, RE_JOIN)
    ok, msg = _join_squad(str(event.group_id), str(event.user_id), int(m.group(1)))
    if ok:
        await join_matcher.finish(at_prefix(event) + MessageSegment.text(f"✅ 已加入 {msg}"))
    await join_matcher.finish(at_prefix(event) + MessageSegment.text(msg))


who_matcher = on_message(Rule(_rule_query), priority=3, block=True)


@who_matcher.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    text = await _squads_text(bot, str(event.group_id))
    if not text:
        await who_matcher.finish("现在没有人在玩，发「.上号2=3」开队～")
    # 名单为纯文本（绑定名/群名片为用户可控内容，走 MessageSegment.text 防注入），不 @ 人
    await who_matcher.finish(MessageSegment.text(text))


off_matcher = on_message(Rule(_rule_off), priority=3, block=True)


@off_matcher.handle()
async def _(event: GroupMessageEvent):
    kind, msg = _leave_squad(str(event.group_id), str(event.user_id))
    if kind == "leader":
        await off_matcher.finish(at_prefix(event) + MessageSegment.text(f"👋 队长下班，{msg}"))
    if kind == "member":
        await off_matcher.finish(at_prefix(event) + MessageSegment.text(f"👋 已退出{msg}，辛苦啦～"))
    await off_matcher.finish(at_prefix(event) + MessageSegment.text(
        "你还没在榜上哦，发「.上号2=3」开队或「.加入1」入队"))


hint_matcher = on_message(Rule(_rule_hint), priority=3, block=True)


@hint_matcher.handle()
async def _(event: GroupMessageEvent):
    await hint_matcher.finish(at_prefix(event) + MessageSegment.text(
        "用法：.上号已有=还缺，如 .上号2=3 = 已有2人、缺3人，共5人小队，你当队长"))


# ---------------- 主人开关 ----------------

enable_cmd = on_command("上号开启", priority=5, block=True)
disable_cmd = on_command("上号关闭", priority=5, block=True)
status_cmd = on_command("上号状态", priority=5, block=True)


@enable_cmd.handle()
async def _(event, arg=CommandArg()):
    if not is_owner(event):
        await enable_cmd.finish("只有主人可以操作上号榜开关")
    gid = _parse_gid(event, arg)
    if not gid:
        await enable_cmd.finish("用法：.上号开启 [群号]（在本群发送可省略群号）")
    changed = _set_enabled(str(gid), True)
    if not changed:
        await enable_cmd.finish(f"群 {gid} 的上号榜本来就是开着的")
    await enable_cmd.finish(f"✅ 已开启群 {gid} 的上号榜（.上号A=B / .加入N / .谁玩 / .下班）")


@disable_cmd.handle()
async def _(event, arg=CommandArg()):
    if not is_owner(event):
        await disable_cmd.finish("只有主人可以操作上号榜开关")
    gid = _parse_gid(event, arg)
    if not gid:
        await disable_cmd.finish("用法：.上号关闭 [群号]（在本群发送可省略群号）")
    changed = _set_enabled(str(gid), False)
    if not changed:
        await disable_cmd.finish(f"群 {gid} 的上号榜本来就没开")
    await disable_cmd.finish(f"✅ 已关闭群 {gid} 的上号榜")


@status_cmd.handle()
async def _(event):
    if not is_owner(event):
        await status_cmd.finish("只有主人可以查看上号榜状态")
    enabled = _load_state().get("enabled")
    groups = enabled if isinstance(enabled, list) else []
    usage = "\n用法：.上号开启 [群号] / .上号关闭 [群号]（在本群发送可省略群号）"
    if not groups:
        await status_cmd.finish("当前没有群开启上号榜。" + usage)
    await status_cmd.finish("已开启上号榜的群：" + "、".join(groups) + usage)
