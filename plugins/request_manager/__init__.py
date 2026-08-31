import logging
import os
import re
import threading
import time

from nonebot import on_command, on_message, on_request
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import (
    FriendRequestEvent,
    GroupRequestEvent,
    GroupMessageEvent,
    MessageEvent,
    MessageSegment,
)
from nonebot.matcher import Matcher
from nonebot.params import CommandArg

from common import (
    OWNER,
    is_owner,
    load_json_state,
    now_str,
    save_json_state,
    save_json_state_async,
)

_logger = logging.getLogger(__name__)

request_matcher = on_request(priority=1, block=False)
# 私聊转发必须跑在 auto_chat 的 chat_matcher（priority=5, block=True）之前：
# 私聊消息恒满足 to_me()，block=True 在 handler 结束后无条件 StopPropagation，
# 若排在其后（原 priority=20）本 matcher 永远不会执行，转发功能静默失效
private_matcher = on_message(priority=4, block=False)


def _reply_message_id(event: MessageEvent) -> str:
    """取消息里 reply 段引用的 message_id（无引用返回空串）。"""
    for seg in event.message:
        if seg.type == "reply":
            return str(seg.data.get("id") or "")
    return ""


async def _owner_decision_rule(event: MessageEvent) -> bool:
    """主人的纯「同意/拒绝」审批指令：

    priority=1 + block=True，抢在 auto_chat 等聊天插件之前。
    私聊始终识别；群内只在**引用回复**机器人发出的申请通知时才识别
    （通知发出后 message_id 已登记进 _notify_index，按引用 id 精确路由），
    群里正常聊天说「同意/拒绝」不会再被吞掉，也不会静默放行陌生人。
    反馈只私发给主人、不在群里回话，避免审批操作被聊天插件当成聊天公开回复。
    """
    if not is_owner(event):
        return False
    if event.get_plaintext().strip() not in {"同意", "拒绝"}:
        return False
    if isinstance(event, GroupMessageEvent):
        reply_id = _reply_message_id(event)
        return bool(reply_id) and _notify_index.get(reply_id) in _pending
    return True


decision_matcher = on_message(
    rule=_owner_decision_rule,
    priority=1,
    block=True,
)

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "auto_approve.json")
GUARD_FILE = os.path.join(os.path.dirname(__file__), "approve_guard.json")

# 自动通过的两道安全闸（防「暗号外泄后被反复利用」）：
# 1) 黑名单：名单内 QQ 永不自动通过，一律转人工
# 2) 二次申请节流：自动通过后 _REAPPLY_MANUAL_DAYS 天内同号再次申请需人工核实
_REAPPLY_MANUAL_DAYS = 7
_GUARD_LOCK = threading.RLock()  # 可重入：辅助函数会嵌套经过 load/save_json_state 的同一把锁

blk_on_cmd = on_command("进群拉黑", priority=5, block=True)
blk_off_cmd = on_command("解除拉黑", aliases={"进群解除拉黑"}, priority=5, block=True)
blk_list_cmd = on_command("拉黑列表", aliases={"进群拉黑列表"}, priority=5, block=True)


def _load_guard() -> dict:
    return load_json_state(GUARD_FILE, _GUARD_LOCK)


def _save_guard(data: dict) -> None:
    save_json_state(GUARD_FILE, data, _GUARD_LOCK)


def _guard_block_reason(uid: int) -> str:
    """返回非空表示该申请人本次不应自动通过（转人工）：blacklist / reapply。"""
    data = _load_guard()
    blacklist = data.get("blacklist") if isinstance(data.get("blacklist"), dict) else {}
    if str(uid) in blacklist:
        return "blacklist"
    approved = data.get("approved") if isinstance(data.get("approved"), dict) else {}
    last = approved.get(str(uid))
    try:
        if last and time.time() - float(last) < _REAPPLY_MANUAL_DAYS * 86400:
            return "reapply"
    except (TypeError, ValueError):
        pass
    return ""


def _guard_mark_approved(uid: int) -> None:
    """记录一次自动通过时间戳；只保留近 60 天记录防文件无限增长。"""
    with _GUARD_LOCK:
        data = _load_guard()
        approved = data.get("approved") if isinstance(data.get("approved"), dict) else {}
        cutoff = time.time() - 60 * 86400
        approved = {
            k: v for k, v in approved.items()
            if isinstance(v, (int, float)) and v >= cutoff
        }
        approved[str(uid)] = time.time()
        data["approved"] = approved
        _save_guard(data)


async def _guard_blacklist(nums: list[str]) -> None:
    with _GUARD_LOCK:
        data = _load_guard()
        blacklist = data.get("blacklist") if isinstance(data.get("blacklist"), dict) else {}
        for n in nums:
            blacklist[n] = time.time()
        data["blacklist"] = blacklist
    # 锁释放后再落盘：save_json_state_async 在工作线程会重新申请 _GUARD_LOCK，
    # 协程持锁跨 await 会死锁
    await save_json_state_async(GUARD_FILE, data, _GUARD_LOCK)


async def _guard_unblacklist(nums: list[str]) -> list[str]:
    with _GUARD_LOCK:
        data = _load_guard()
        blacklist = data.get("blacklist") if isinstance(data.get("blacklist"), dict) else {}
        removed = [n for n in nums if blacklist.pop(n, None) is not None]
        data["blacklist"] = blacklist
    await save_json_state_async(GUARD_FILE, data, _GUARD_LOCK)
    return removed

auto_on_cmd = on_command("自动通过", aliases={"自动同意"}, priority=5, block=True)
auto_off_cmd = on_command("自动通过关闭", aliases={"自动同意关闭"}, priority=5, block=True)
auto_show_cmd = on_command("自动通过查看", aliases={"自动同意查看"}, priority=5, block=True)
auto_count_cmd = on_command("自动通过数量", aliases={"自动同意数量", "自动通过统计", "自动同意统计"}, priority=5, block=True)

_pending: dict[str, dict] = {}
_notify_index: dict[str, str] = {}  # 机器人发给主人的申请通知 message_id -> pending flag
# 可重入：_save_keywords 持锁期间会经 _load_config/save_json_state 再次申请同一把锁
_save_lock = threading.RLock()
_PENDING_TTL = 48 * 3600  # 与 QQ 侧 flag 有效期一致


def _purge_pending() -> None:
    """清理超过 TTL 的待处理申请与失效的通知索引，防止内存无限增长。"""
    now = time.time()
    for key in list(_pending):
        if now - _pending[key].get("ts", 0) > _PENDING_TTL:
            _pending.pop(key, None)
    for mid in list(_notify_index):
        if _notify_index[mid] not in _pending:
            _notify_index.pop(mid, None)


def _remember_notify(resp, flag: str) -> None:
    """登记申请通知消息的 message_id，供私聊引用回复时精确路由审批目标。"""
    try:
        mid = resp.get("message_id") if isinstance(resp, dict) else None
    except Exception:
        mid = None
    if mid is None:
        return
    if len(_notify_index) > 200:  # 有界：只留最近 200 条
        for k in list(_notify_index)[: len(_notify_index) - 100]:
            _notify_index.pop(k, None)
    _notify_index[str(mid)] = flag


def _load_config() -> dict:
    # common 的 load_json_state：损坏/不可读时先备份留档（.corrupt-<ts>/.unreadable-<ts>）
    # 再返回 {}，防止后续保存把其他群的配置静默清掉；权限沿用现状/新文件 600（暗号敏感）
    return load_json_state(CONFIG_FILE, _save_lock)


def _load_keywords(group_id: int) -> list[str]:
    return [k for k in (_load_config().get(str(group_id)) or []) if k]


def _save_keywords(group_id: int, kw: list[str], merge: bool = False) -> list[str]:
    with _save_lock:
        data = _load_config()
        keywords = [k for k in kw if k]
        if merge:
            existing = [k for k in (data.get(str(group_id)) or []) if k]
            keywords = list(dict.fromkeys([*existing, *keywords]))
        else:
            keywords = list(dict.fromkeys(keywords))
        data[str(group_id)] = keywords
        # common 的 save_json_state：tmp + fsync + os.replace 原子写，
        # 权限沿用现状、新文件 600——auto_approve.json 存各群进群暗号，不能掉回 644
        save_json_state(CONFIG_FILE, data, _save_lock)
        return keywords


async def _auto_approve(bot: Bot, event: GroupRequestEvent, comment: str) -> bool:
    keywords = _load_keywords(event.group_id)
    if not keywords:
        return False
    comment_lower = comment.lower()  # 大小写不敏感匹配（中文不受影响）
    hit = [k for k in keywords if k.lower() in comment_lower]
    if hit:
        # 安全闸：黑名单成员与 N 天内已自动通过过的同号申请一律转人工
        if _guard_block_reason(event.user_id):
            return False
        try:
            await bot.set_group_add_request(flag=event.flag, sub_type=event.sub_type, approve=True)
        except Exception as e:
            try:
                await bot.send_private_msg(user_id=int(OWNER), message=MessageSegment.text(f"⚠️ 自动通过失败：{e}"))
            except Exception:
                _logger.warning("自动通过失败通知发送失败", exc_info=True)
            return False
        _guard_mark_approved(event.user_id)  # 只在实际通过后记账
        try:
            await bot.send_private_msg(
                user_id=int(OWNER),
                message=MessageSegment.text(
                    f"✅ 自动通过进群申请\n"
                    f"🏘 群号：{event.group_id}\n"
                    f"👤 申请人：{event.user_id}（{now_str()}）\n"
                    f"💬 附言：{comment or '无'}\n"
                    f"🔑 命中关键字：{' / '.join(hit)}"
                ),
            )
        except Exception:
            # 通知私发失败不代表审批失败：已通过的申请不能被上层当作「未通过」放回待处理重复处理
            _logger.warning("自动通过成功但结果通知私发失败", exc_info=True)
        return True
    return False


async def _finish_owner_config(bot: Bot, matcher: Matcher, event: MessageEvent, text: str) -> None:
    """自动通过配置结果含进群暗号，群聊里不回显：完整结果私发给主人，群里只回简短确认。"""
    if not hasattr(event, "group_id"):
        await matcher.finish(text)
    try:
        await bot.send_private_msg(user_id=int(OWNER), message=MessageSegment.text(text))
    except Exception:
        _logger.warning("自动通过配置结果私发失败", exc_info=True)
    await matcher.finish("✅ 配置结果已私发给你")


# ---------------- 关键字配置（群内或私聊按群号） ----------------
@auto_on_cmd.handle()
async def auto_on(bot: Bot, event: MessageEvent, arg=CommandArg()):
    if not is_owner(event):
        await auto_on_cmd.finish("❌ 你没有权限使用此功能")
    text = arg.extract_plain_text().strip()
    if not text:
        if hasattr(event, "group_id"):
            await _finish_owner_config(bot, auto_on_cmd, event, "用法：.自动通过 关键字\n例如：.自动通过 我是老玩家\n多个关键字用空格分隔：.自动通过 关键字1 关键字2")
        await auto_on_cmd.finish("私聊用法：.自动通过 <群号> 关键字\n例如：.自动通过 <群号> 我是老玩家")
    parts = text.split()
    if hasattr(event, "group_id"):
        gid = event.group_id
        kws = [k for k in re.split(r"[\s,，、]+", text) if k]
    else:
        if not parts[0].isdigit():
            await auto_on_cmd.finish("私聊用法：.自动通过 <群号> 关键字\n例如：.自动通过 <群号> 我是老玩家")
        gid = int(parts[0])
        kws = [k for k in re.split(r"[\s,，、]+", " ".join(parts[1:])) if k]
    if not kws:
        await auto_on_cmd.finish("请提供至少一个关键字")
    merged = _save_keywords(gid, kws, merge=True)
    await _finish_owner_config(bot, auto_on_cmd, event, f"✅ 群 {gid} 自动通过已开启\n🔑 关键字：{' / '.join(merged)}\n进群附言包含任一关键字将自动同意（不区分大小写）")


@auto_off_cmd.handle()
async def auto_off(bot: Bot, event: MessageEvent, arg=CommandArg()):
    if not is_owner(event):
        await auto_off_cmd.finish("❌ 你没有权限使用此功能")
    text = arg.extract_plain_text().strip()
    if hasattr(event, "group_id"):
        gid = event.group_id
    else:
        if not text.isdigit():
            await auto_off_cmd.finish("私聊用法：.自动通过关闭 <群号>\n例如：.自动通过关闭 <群号>")
        gid = int(text)
    _save_keywords(gid, [])
    await _finish_owner_config(bot, auto_off_cmd, event, f"✅ 群 {gid} 自动通过已关闭，进群申请将等待手动处理")


@auto_show_cmd.handle()
async def auto_show(bot: Bot, event: MessageEvent):
    if not is_owner(event):
        await auto_show_cmd.finish("❌ 你没有权限使用此功能")
    if hasattr(event, "group_id"):
        kws = _load_keywords(event.group_id)
        if kws:
            await _finish_owner_config(bot, auto_show_cmd, event, f"🔑 本群自动通过已开启\n🏘 群号：{event.group_id}\n关键字：{' / '.join(kws)}")
        await _finish_owner_config(bot, auto_show_cmd, event, f"🔑 本群自动通过未开启（群 {event.group_id}）")
    data = _load_config()
    lines = ["🔑 已开启自动通过的群："]
    for gid, kws in data.items():
        if kws:
            lines.append(f"群 {gid}：{' / '.join(kws)}")
    if len(lines) == 1:
        await auto_show_cmd.finish("🔑 没有任何群开启自动通过")
    await auto_show_cmd.finish("\n".join(lines))


# ---------------- 拉黑名单管理（owner） ----------------
def _parse_qq_args(arg) -> list[str]:
    return [p for p in re.split(r"[\s,，、]+", arg.extract_plain_text().strip()) if p.isdigit()]


@blk_on_cmd.handle()
async def blk_add(bot: Bot, event: MessageEvent, arg=CommandArg()):
    if not is_owner(event):
        await blk_on_cmd.finish("❌ 你没有权限使用此功能")
    nums = _parse_qq_args(arg)
    if not nums:
        await blk_on_cmd.finish("用法：.进群拉黑 <QQ号> [更多QQ号...]\n例如：.进群拉黑 123456 789012\n拉黑后这些号码的进群申请将永不自动通过")
    await _guard_blacklist(nums)
    await _finish_owner_config(bot, blk_on_cmd, event,
                               f"⛔ 已拉黑 {len(nums)} 个 QQ：{'、'.join(nums)}\n其进群申请将不再自动通过，一律转人工")


@blk_off_cmd.handle()
async def blk_remove(bot: Bot, event: MessageEvent, arg=CommandArg()):
    if not is_owner(event):
        await blk_off_cmd.finish("❌ 你没有权限使用此功能")
    nums = _parse_qq_args(arg)
    if not nums:
        await blk_off_cmd.finish("用法：.解除拉黑 <QQ号> [更多QQ号...]\n例如：.解除拉黑 123456")
    removed = await _guard_unblacklist(nums)
    missing = [n for n in nums if n not in removed]
    text = f"✅ 已移出拉黑：{'、'.join(removed)}" if removed else "没有匹配的记录"
    if missing:
        text += f"\nℹ️ 不在名单中：{'、'.join(missing)}"
    await _finish_owner_config(bot, blk_off_cmd, event, text)


@blk_list_cmd.handle()
async def blk_show(bot: Bot, event: MessageEvent):
    if not is_owner(event):
        await blk_list_cmd.finish("❌ 你没有权限使用此功能")
    data = _load_guard()
    blacklist = data.get("blacklist") if isinstance(data.get("blacklist"), dict) else {}
    if not blacklist:
        await _finish_owner_config(bot, blk_list_cmd, event, "⛔ 进群拉黑名单为空\n用 .进群拉黑 <QQ号> 添加")
        return
    ids = sorted(blacklist)
    shown = ids[:50]
    text = f"⛔ 进群拉黑名单（共 {len(ids)} 人）：\n" + "、".join(shown)
    if len(ids) > len(shown):
        text += f"\n…其余 {len(ids) - len(shown)} 人略"
    approved = data.get("approved") if isinstance(data.get("approved"), dict) else {}
    recent = sum(
        1 for v in approved.values()
        if isinstance(v, (int, float)) and time.time() - v < _REAPPLY_MANUAL_DAYS * 86400
    )
    text += f"\n⏳ 节流期中（{_REAPPLY_MANUAL_DAYS} 天内已自动通过过）：{recent} 人"
    await _finish_owner_config(bot, blk_list_cmd, event, text)


# ---------------- 好友申请 / 加群申请 ----------------
@auto_count_cmd.handle()
async def auto_count(bot: Bot, event: MessageEvent, arg=CommandArg()):
    if not is_owner(event):
        await auto_count_cmd.finish("❌ 你没有权限使用此功能")
    text = arg.extract_plain_text().strip()
    if hasattr(event, "group_id"):
        gid = event.group_id
    else:
        if not text.isdigit():
            await auto_count_cmd.finish("私聊用法：.自动通过数量 <群号>\n例如：.自动通过数量 <群号>")
        gid = int(text)
    kws = _load_keywords(gid)
    if kws:
        await _finish_owner_config(bot, auto_count_cmd, event, f"🔑 群 {gid} 共有 {len(kws)} 个关键字：{' / '.join(kws)}")
    await _finish_owner_config(bot, auto_count_cmd, event, f"🔑 群 {gid} 当前没有配置自动通过关键字（共 0 个）")


def _is_bnet_managed(group_id: int) -> bool:
    """该群的加群申请是否已由战网ID验证插件（bnet_verify）托管。

    延迟导入避免插件加载顺序耦合；插件未加载/读取失败时按未托管处理。
    """
    try:
        from plugins.bnet_verify import is_managed_group
    except Exception:
        return False
    try:
        return bool(is_managed_group(group_id))
    except Exception:
        _logger.warning("查询 bnet_verify 托管状态失败，按未托管处理", exc_info=True)
        return False


@request_matcher.handle()
async def handle_request(bot: Bot, event):
    if event.user_id == int(OWNER):
        return
    _purge_pending()
    ts = now_str()
    if isinstance(event, GroupRequestEvent) and event.sub_type in ("add", "apply"):
        if _is_bnet_managed(event.group_id):
            return  # 该群由战网ID验证插件（bnet_verify）全权处理，避免双重响应
        if await _auto_approve(bot, event, event.comment or ""):
            return
        # 诊断日志：记录未自动通过时的原始附言（验证问答的回答也在这里），便于排查
        _logger.debug(
            "加群申请未自动通过: group=%s user=%s sub=%s comment=%r",
            event.group_id, event.user_id, event.sub_type, event.comment,
        )
        # 转人工时提示安全闸状态，主人可据此提高警惕
        blocked_note = ""
        reason = _guard_block_reason(event.user_id)
        if reason == "blacklist":
            blocked_note = "\n⛔ 该申请人在进群拉黑名单中，请谨慎处理（可用 .解除拉黑 <QQ号> 移出）"
        elif reason == "reapply":
            blocked_note = (
                f"\n⏳ 该账号 {_REAPPLY_MANUAL_DAYS} 天内已被自动通过过一次"
                f"（退了又进），请核实后再处理"
            )
        _pending[event.flag] = {
            "kind": "group", "flag": event.flag,
            "sub_type": event.sub_type, "group_id": event.group_id,
            "user_id": event.user_id, "ts": time.time(),
        }
        msg = (
            f"🔔 有人申请进群\n"
            f"🏘 群号：{event.group_id}\n"
            f"👤 申请人：{event.user_id}（{ts}）\n"
            f"💬 附言：{event.comment or '无'}\n"
            f"✍️ 私聊回复「同意」或「拒绝」即可处理（多个申请时请引用对应的通知消息）"
            + blocked_note
        )
    elif isinstance(event, FriendRequestEvent):
        _pending[event.flag] = {
            "kind": "friend", "flag": event.flag,
            "user_id": event.user_id, "ts": time.time(),
        }
        msg = (
            f"🔔 好友申请\n"
            f"👤 申请人：{event.user_id}（{ts}）\n"
            f"💬 验证消息：{event.comment or '无'}\n"
            f"✍️ 私聊回复「同意」或「拒绝」即可处理"
        )
    elif isinstance(event, GroupRequestEvent) and event.sub_type == "invite":
        _pending[event.flag] = {
            "kind": "group", "flag": event.flag,
            "sub_type": event.sub_type, "group_id": event.group_id,
            "user_id": event.user_id, "ts": time.time(),
        }
        msg = (
            f"🔔 有人邀请机器人进群\n"
            f"🏘 群号：{event.group_id}\n"
            f"👤 邀请人：{event.user_id}（{ts}）\n"
            f"💬 附言：{event.comment or '无'}\n"
            f"✍️ 私聊回复「同意」或「拒绝」即可处理（多个申请时请引用对应的通知消息）"
        )
    else:
        return
    try:
        # MessageSegment.text 包裹：附言等申请人可控文本不会被解析为 CQ 码（防注入）
        resp = await bot.send_private_msg(user_id=int(OWNER), message=MessageSegment.text(msg))
        _remember_notify(resp, event.flag)
    except Exception:
        _logger.warning("申请通知私发失败", exc_info=True)


# ---------------- 主人回复 同意/拒绝 ----------------
async def _send_decision_notice(bot: Bot, message: str) -> None:
    """审批结果是管理操作反馈，只私发给主人，不在群里公开。"""
    await bot.send_private_msg(
        user_id=int(OWNER),
        message=MessageSegment.text(message),
    )


async def _process_decision(bot: Bot, event: MessageEvent, action: bool) -> None:
    _purge_pending()
    if not _pending:
        await _send_decision_notice(bot, "📭 当前没有待处理的申请")
        return

    target_key = None

    # 群内与私聊统一：引用回复了机器人发出的申请通知（message_id 登记于 _notify_index）
    # 就按引用 id 精确路由，同群有多个申请时也能准确指定。
    # 不解析被引用消息的文本内容：那可能是转发自陌生人的全文，能伪造「申请人：/群号：」行，
    # 主人一旦引用它回复同意，就会把操作路由到攻击者指定的其他申请上。
    reply_id = _reply_message_id(event)
    if reply_id:
        candidate = _notify_index.get(reply_id)
        target_key = candidate if candidate in _pending else None

    if target_key is None:
        if isinstance(event, GroupMessageEvent):
            # 群内兜底路径（matcher 规则已保证裸发「同意/拒绝」不会进入这里）：
            # 只允许处理当前群的进群/邀请申请，不能误操作其他群或好友申请。
            candidates = [
                (key, value)
                for key, value in _pending.items()
                if value.get("kind") == "group"
                and value.get("group_id") == event.group_id
            ]
            if not candidates:
                await _send_decision_notice(bot, "📭 当前群没有待处理的进群申请")
                return
            if len(candidates) > 1:
                await _send_decision_notice(
                    bot,
                    "⚠️ 当前群有多个待处理申请，请到私聊引用对应的申请通知消息来指定处理哪一个",
                )
                return
            target_key = candidates[0][0]
        else:
            if len(_pending) == 1:
                target_key = next(iter(_pending))
            else:
                await _send_decision_notice(
                    bot,
                    "⚠️ 有多个待处理申请，请回复时引用机器人发来的申请通知消息来指定处理哪一个",
                )
                return

    val = _pending.pop(target_key, None)
    if val is None:
        # 两例并发「同意」时第二个会取不到目标：第一个已把它处理完
        await _send_decision_notice(bot, "⚠️ 该申请刚已被处理")
        return
    kind, flag = val["kind"], val["flag"]
    try:
        if kind == "friend":
            if action:
                await bot.set_friend_add_request(flag=flag, approve=True, remark="")
                message = f"✅ 已通过好友申请（QQ {val.get('user_id')}）"
            else:
                await bot.set_friend_add_request(flag=flag, approve=False)
                message = f"❌ 已拒绝好友申请（QQ {val.get('user_id')}）"
        else:
            sub = val["sub_type"]  # 使用真实 sub_type，add 申请不再错误地以 invite 处理
            who = "邀请人" if sub == "invite" else "申请人"
            if action:
                await bot.set_group_add_request(flag=flag, sub_type=sub, approve=True)
                message = f"✅ 已通过进群申请\n🏘 群号：{val.get('group_id')}\n👤 {who}：{val.get('user_id')}"
            else:
                await bot.set_group_add_request(flag=flag, sub_type=sub, approve=False)
                message = f"❌ 已拒绝进群申请\n🏘 群号：{val.get('group_id')}\n👤 {who}：{val.get('user_id')}"
        await _send_decision_notice(bot, message)
    except Exception as e:
        _pending[target_key] = val  # 处理失败时放回待处理列表，主人可重试
        await _send_decision_notice(bot, f"⚠️ 处理失败：{e}")


@decision_matcher.handle()
async def owner_decision(bot: Bot, event: MessageEvent):
    text = event.get_plaintext().strip()
    await _process_decision(bot, event, text == "同意")


# ---------------- 私聊消息转发 ----------------
@private_matcher.handle()
async def forward_private(bot: Bot, event: MessageEvent):
    if is_owner(event) or event.message_type != "private":
        return
    text = event.get_plaintext().strip() or "（非文字消息）"
    name = str(event.user_id)
    try:
        info = await bot.get_stranger_info(user_id=event.user_id)
        name = info.get("nickname") or name
    except Exception:
        pass
    msg = (
        f"📨 {name}（{event.user_id}）私聊了机器人\n"
        f"🕐 {now_str()}\n"
        f"💬 {text}"
    )
    try:
        await bot.send_private_msg(user_id=int(OWNER), message=MessageSegment.text(msg))
    except Exception:
        _logger.warning("私聊转发通知发送失败", exc_info=True)
