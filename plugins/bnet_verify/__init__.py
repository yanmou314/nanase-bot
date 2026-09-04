"""入群验证（战网 ID）：主人按群开启后自动核验加群申请。

流程：群设置验证问题（如"请填写你的战网ID"），申请人填写的答案会随申请的
comment 到达；本插件把附言整体作为战网ID，经 owstats 中继向查询机器人
发送“/大神数据 ID”——收到图片即自动通过；收不到图片（超时/报错）则不拒绝，
保持待处理并私聊通知主人转人工，主人回复 .同意 QQ号 即可通过并自动改名片+绑定。

通过后自动：把新成员的群名片改为战网ID（成员进群后异步重试），并把
QQ 与战网ID写入 owstats 的绑定表（进群即可直接使用 .战报/.总结 等命令）。

注意：
- 机器人必须持有目标群的管理员身份，否则无法通过/拒绝申请、无法改群名片。
- 验证问题本身需要在群设置里配置一次（OneBot 协议不支持机器人设置验证问题）。
- 大神档案仅覆盖国服数据，外服玩家查不到成绩会被拒绝。
"""

import asyncio
import json
import os
import re
import threading
import time

from nonebot import get_bot, get_driver, logger, on_command, on_request
from nonebot.adapters.onebot.v11 import Bot, GroupRequestEvent, Message, MessageSegment
from nonebot.params import CommandArg

from common import (
    OWNER,
    is_owner,
    load_json_state,
    save_json_state_async,
)

STATE_FILE = os.path.join(os.path.dirname(__file__), "groups.json")
_STATE_LOCK = threading.RLock()

# 通过后设置群名片的重试间隔（秒）：审批通过到成员实际进群有几秒延迟
CARD_RETRY_DELAYS = (3, 5, 5, 5, 5)
_card_tasks: set = set()


def _load_state() -> dict:
    return load_json_state(STATE_FILE, _STATE_LOCK)


async def _save_state(data: dict) -> None:
    await save_json_state_async(STATE_FILE, data, _STATE_LOCK)


def is_managed_group(group_id: int) -> bool:
    """该群的加群申请是否由本插件全权处理（request_manager 据此跳过）。"""
    return str(group_id) in (_load_state().get("groups") or [])


_TAG_ANSWER_RE = re.compile(r"答案\s*[:：]\s*([^\s@#＃]+#\d+)")
_TAG_ANY_RE = re.compile(r"([^\s@#＃]+#\d+)")


def clean_join_answer(comment: str) -> str:
    """从申请附言中只提取战网ID（附言常连带验证问题，如“问题：…\n答案：名字#123”）。
    优先取“答案：”后的 ID，否则取首个“名字#数字”；没有则返回空串。"""
    text = (comment or "").replace("＃", "#").strip()
    m = _TAG_ANSWER_RE.search(text)
    if m:
        return m.group(1)
    m = _TAG_ANY_RE.search(text)
    if not m:
        return ""
    tag = m.group(1)
    # 兜底：“问题：答案”连在一起且没有“答案：”标记时，取最后一个冒号之后
    #（战网ID 本身不可能含中英文冒号）
    if "：" in tag:
        tag = tag.rsplit("：", 1)[1]
    if ":" in tag:
        tag = tag.rsplit(":", 1)[1]
    return tag


_VERIFY_FILE = os.path.join(os.path.dirname(__file__), "verify_state.json")
_verify_pending: dict = {}  #申请人QQ(str) -> {flag, sub_type, group_id, tag, comment, ts}
_verify_active: dict = {}  #申请人QQ(str) -> 同上（已提交中继、待图片期间）


def _load_verify_state() -> dict:
    try:
        with open(_VERIFY_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, OSError, ValueError):
        pass
    return {}


async def _save_verify_state() -> None:
    try:
        await save_json_state_async(
            _VERIFY_FILE, {"pending": _verify_pending, "active": _verify_active})
    except Exception:
        logger.warning("[bnet_verify] 验证状态落盘失败", exc_info=True)


def _restore_verify_state() -> None:
    try:
        data = _load_verify_state()
        for k, v in (data.get("pending") or {}).items():
            if isinstance(v, dict):
                _verify_pending.setdefault(str(k), v)
        for k, v in (data.get("active") or {}).items():
            if isinstance(v, dict):
                _verify_active.setdefault(str(k), v)
    except Exception:
        logger.warning("[bnet_verify] 验证状态恢复失败", exc_info=True)


_STARTED_AT = time.time()  # 进程启动时刻：区分重启前遗留的在途验证与重启后新发起的


_restore_verify_state()


@get_driver().on_startup
async def _report_interrupted_verifies() -> None:
    """重启导致中断的在途验证：延迟通知主人去客户端查看（记录仍保留，可 .同意）。"""
    if not _verify_active:
        return
    await asyncio.sleep(60)
    # 只算重启前遗留的（ts 早于本次启动）：启动后新发起且仍在途的不算中断
    interrupted = {qq: rec for qq, rec in _verify_active.items()
                   if isinstance(rec, dict) and float(rec.get("ts") or 0) < _STARTED_AT}
    if not interrupted:
        return
    lines = []
    for qq, rec in interrupted.items():
        lines.append(f"群{rec.get('group_id')} QQ{qq}（{rec.get('tag')}）")
    try:
        bot = get_bot()
        await bot.send_private_msg(
            user_id=int(OWNER),
            message=MessageSegment.text(
                "⚠️ 以下入群验证因机器人重启而中断（申请仍在QQ侧挂起）：\n"
                + "\n".join(lines)
                + "\n如对方已进群请忽略；否则请在QQ客户端手动处理，或等对方重申请后用 .同意 QQ号 处理"))
    except Exception:
        logger.warning("[bnet_verify] 中断通知发送失败", exc_info=True)
        return
    # 通知即完结：清理已提醒的遗留记录，避免之后每次重启都重复提醒
    for qq in interrupted:
        _verify_active.pop(qq, None)
    await _save_verify_state()


def _remember_verify_pending(uid: str, rec: dict) -> None:
    now = time.time()
    for k in [k for k, v in _verify_pending.items()
              if now - float(v.get("ts") or 0) > 24 * 3600]:
        _verify_pending.pop(k, None)
    _verify_pending[uid] = rec


async def _relay_verify_profile(tag: str, timeout: int = 180,
                                active_key: str = "", group_id: int | None = None):
    """经 owstats 中继向对方机器人查大神数据。返回 (has_image, segs)，绝不抛异常。

    active_key 提供时把本次查询登记进 _verify_active（含群号），机器人若在等待期间
    重启，启动钩子据此通知主人哪些验证被中断。"""
    try:
        from plugins.owstats import submit_relay_task
    except Exception:
        logger.warning("[bnet_verify] owstats 中继不可用", exc_info=True)
        return (False, [])
    try:
        fut = await submit_relay_task("verify", tag, text=f" /大神数据 {tag}", timeout=timeout)
    except Exception:
        logger.warning("[bnet_verify] 提交中继任务失败", exc_info=True)
        return (False, [])
    if active_key:
        _verify_active[active_key] = {"tag": tag, "group_id": group_id, "ts": time.time()}
        await _save_verify_state()
    try:
        segs, has_image = await asyncio.wait_for(fut, timeout + 10)
        return (bool(has_image), list(segs or []))
    except asyncio.TimeoutError:
        return (False, [])
    except Exception:
        logger.warning("[bnet_verify] 等待中继结果失败", exc_info=True)
        return (False, [])
    finally:
        if active_key and active_key in _verify_active:
            _verify_active.pop(active_key, None)
            await _save_verify_state()


async def _notify_owner(bot: Bot, text: str) -> None:
    try:
        await bot.send_private_msg(user_id=int(OWNER), message=text)
    except Exception:
        logger.warning("[bnet_verify] 通知主人失败", exc_info=True)


def _auto_bind(uid: int, tag: str) -> bool:
    """把 QQ 与战网ID写入 owstats 绑定表（.绑定 同款存储），进群即可直接查询。"""
    try:
        from plugins.owstats import _bind  # 延迟导入，避免插件加载顺序耦合
    except Exception:
        logger.warning("[bnet_verify] owstats 不可用，跳过自动绑定", exc_info=True)
        return False
    try:
        _bind(str(uid), tag)
        return True
    except Exception:
        logger.warning(f"[bnet_verify] 自动绑定失败 uid={uid} tag={tag}", exc_info=True)
        return False


async def _set_card_with_retry(bot: Bot, gid: int, uid: int, tag: str) -> None:
    """成员进群通常晚于审批生效，按 CARD_RETRY_DELAYS 重试设置群名片。"""
    for i, delay in enumerate(CARD_RETRY_DELAYS):
        await asyncio.sleep(delay)
        try:
            await bot.set_group_card(group_id=gid, user_id=uid, card=tag)
            logger.info(f"[bnet_verify] 群 {gid} 已把 {uid} 的群名片设为 {tag}（第 {i + 1} 次）")
            return
        except Exception:
            logger.warning(
                f"[bnet_verify] 群 {gid} 设置群名片失败（第 {i + 1} 次）uid={uid} card={tag}",
                exc_info=True,
            )
    logger.error(f"[bnet_verify] 群 {gid} 群名片设置最终失败 uid={uid} card={tag}")


def _schedule_card(bot: Bot, gid: int, uid: int, tag: str) -> None:
    task = asyncio.create_task(_set_card_with_retry(bot, gid, uid, tag))
    _card_tasks.add(task)
    task.add_done_callback(_card_tasks.discard)


async def _reject(bot: Bot, event: GroupRequestEvent, reason: str) -> None:
    try:
        await bot.set_group_add_request(
            flag=event.flag, sub_type=event.sub_type, approve=False, reason=reason
        )
    except Exception:
        # 申请人在验证期间撤回申请会导致 flag 失效，属正常情况
        logger.warning(
            f"[bnet_verify] 群 {event.group_id} 拒绝申请失败（flag 可能已失效）"
            f"user={event.user_id}",
            exc_info=True,
        )
        return
    logger.info(f"[bnet_verify] 群 {event.group_id} 拒绝 {event.user_id}：{reason}")


async def _handle_group_join(bot: Bot, event: GroupRequestEvent) -> None:
    gid = event.group_id
    if not is_managed_group(gid):
        return
    comment = str(event.comment or "")
    # 附言整体即战网ID，不做提取；仅裁剪空白与全角#归一
    tag = clean_join_answer(comment)
    if not tag:
        await _reject(
            bot, event,
            "未能识别战网ID：请填写你的战网ID（形如 昵称#12345）后重新申请",
        )
        return
    has_image, relay_segs = await _relay_verify_profile(
        tag, active_key=str(event.user_id), group_id=gid)
    if has_image:
        try:
            await bot.set_group_add_request(
                flag=event.flag, sub_type=event.sub_type, approve=True
            )
        except Exception:
            logger.warning(
                f"[bnet_verify] 群 {gid} 通过申请失败（flag 可能已失效）user={event.user_id}",
                exc_info=True,
            )
            return
        logger.info(f"[bnet_verify] 群 {gid} 自动通过 {event.user_id}（战网ID {tag}）")
        bound = _auto_bind(event.user_id, tag)
        _schedule_card(bot, gid, event.user_id, tag)
        bind_note = "已自动绑定ID" if bound else "自动绑定失败（owstats 不可用）"
        # tag 为申请人可控内容：必须经 MessageSegment.text 包裹转义，防 CQ 码注入
        await _notify_owner(
            bot,
            MessageSegment.text(f"✅ 入群验证通过\n🏘 群号：{gid}\n👤 QQ：{event.user_id}\n🎮 战网ID：")
            + MessageSegment.text(tag)
            + MessageSegment.text(f"\n🔗 {bind_note}\n🏷 群名片将自动改为战网ID"),
        )
        return
    _remember_verify_pending(str(event.user_id), {
        "flag": event.flag, "sub_type": event.sub_type, "user_id": event.user_id,
        "group_id": gid, "tag": tag, "comment": comment, "ts": time.time(),
    })
    await _save_verify_state()
    logger.warning(
        f"[bnet_verify] 群 {gid} 验证未收到图片结果，转人工 user={event.user_id} tag={tag}"
    )
    show = [seg for seg in (relay_segs or []) if seg.type in ("image", "text")]
    await _notify_owner(
        bot,
        MessageSegment.text(f"⚠️ 入群验证待人工审批（对方查询无图片结果，未自动拒绝）\n🏘 群号：{gid}\n👤 申请人QQ：{event.user_id}\n🎮 战网ID：")
        + MessageSegment.text(tag)
        + MessageSegment.text(f"\n💬 附言：{comment[:200]}\n✅ 通过请回复：.同意 {event.user_id}"),
    )
    if show:
        try:
            await _notify_owner(bot, Message(show))
        except Exception:
            pass


verify_matcher = on_request(priority=1, block=False)


@verify_matcher.handle()
async def handle_join_request(bot: Bot, event):
    if not isinstance(event, GroupRequestEvent) or event.sub_type not in ("add", "apply"):
        return
    await _handle_group_join(bot, event)


def list_verify_pending() -> dict:
    """待审批快照 {QQ: rec}（供 request_manager 裸回同意桥接）。"""
    return {str(k): v for k, v in _verify_pending.items() if isinstance(v, dict)}


async def approve_verify_by_qq(bot, target_qq: str):
    """通过指定 QQ 的战网验证待审批（含改名片+绑定）。返回 (handled, notice文本)。"""
    target = "".join(ch for ch in str(target_qq or "") if ch.isdigit())
    rec = _verify_pending.get(target)
    if not rec or not isinstance(rec, dict):
        return (False,
                f"没有 QQ {target} 的待审批入群申请（可能已处理，或机器人重启后记录丢失，可在QQ客户端手动处理）")
    # user_id 缺失时回退用记录键（早期版本记录没存 user_id，键本身就是 QQ）
    gid, uid, tag = rec.get("group_id"), rec.get("user_id") or target, rec.get("tag", "")
    try:
        uid_int = int(uid)
    except (TypeError, ValueError):
        uid_int = 0
    try:
        await bot.set_group_add_request(
            flag=rec.get("flag"), sub_type=rec.get("sub_type", "add"), approve=True)
    except Exception:
        _verify_pending.pop(target, None)
        await _save_verify_state()
        return (True,
                f"自动通过失败（申请可能已过期），请在QQ客户端手动处理 群{gid} QQ{target}")
    _verify_pending.pop(target, None)
    await _save_verify_state()
    logger.info(f"[bnet_verify] 群 {gid} 主人手动通过 {uid}（战网ID {tag}）")
    if not uid_int:
        return (True,
                f"✅ 已通过 群{gid} QQ{target}（{tag}），但记录缺少 QQ 号，请手工改名片与绑定")
    bound = _auto_bind(uid_int, tag)
    try:
        _schedule_card(bot, gid, uid_int, tag)
    except Exception:
        pass
    bind_note = "已自动绑定ID" if bound else "自动绑定失败（owstats 不可用）"
    return (True,
            f"✅ 已通过 群{gid} QQ{target}（{tag}），群名片与绑定已自动处理：{bind_note}")


agree_cmd = on_command("同意", priority=5, block=True)


@agree_cmd.handle()
async def agree_join(bot: Bot, event, arg: Message = CommandArg()):
    if not is_owner(event):
        await agree_cmd.finish("仅主人可用")
        return
    _ok, notice = await approve_verify_by_qq(bot, arg.extract_plain_text())
    await agree_cmd.finish(MessageSegment.text(notice))


# ---------------- 主人开关 ----------------


def _parse_gid(event, arg: Message) -> int | None:
    text = arg.extract_plain_text().strip()
    if text.isdigit():
        return int(text)
    gid = getattr(event, "group_id", None)
    return int(gid) if gid else None


async def _check_bot_admin(bot: Bot, gid: int) -> str:
    """机器人不是群管理时返回提醒文案（查询失败不阻塞开关）。"""
    try:
        info = await bot.get_group_member_info(group_id=gid, user_id=bot.self_id)
        role = str((info or {}).get("role") or "")
        if role not in ("admin", "owner"):
            return "\n⚠️ 警告：机器人当前不是该群管理员，无法自动通过/拒绝申请，请先授予管理员。"
    except Exception:
        logger.warning(f"[bnet_verify] 查询群 {gid} 成员身份失败", exc_info=True)
    return ""


verify_cmd = on_command("战网验证", aliases={"战网验证状态"}, priority=5, block=True)
enable_cmd = on_command("战网验证开启", priority=5, block=True)
disable_cmd = on_command("战网验证关闭", priority=5, block=True)


@enable_cmd.handle()
async def enable_verify(bot: Bot, event, arg: Message = CommandArg()):
    if not is_owner(event):
        await enable_cmd.finish("只有主人可以操作入群验证开关")
    gid = _parse_gid(event, arg)
    if not gid:
        await enable_cmd.finish("用法：.战网验证开启 [群号]（在本群发送可省略群号）")
    state = _load_state()
    groups = state.get("groups") or []
    if str(gid) in groups:
        await enable_cmd.finish(f"群 {gid} 的入群验证本来就是开着的")
    groups.append(str(gid))
    state["groups"] = sorted(groups)
    await _save_state(state)
    note = await _check_bot_admin(bot, gid)
    await enable_cmd.finish(f"✅ 已开启群 {gid} 的入群验证（战网ID核验，新申请自动处理）" + note)


@disable_cmd.handle()
async def disable_verify(bot: Bot, event, arg: Message = CommandArg()):
    if not is_owner(event):
        await disable_cmd.finish("只有主人可以操作入群验证开关")
    gid = _parse_gid(event, arg)
    if not gid:
        await disable_cmd.finish("用法：.战网验证关闭 [群号]（在本群发送可省略群号）")
    state = _load_state()
    groups = [g for g in (state.get("groups") or []) if g != str(gid)]
    if len(groups) == len(state.get("groups") or []):
        await disable_cmd.finish(f"群 {gid} 的入群验证本来就没开")
    state["groups"] = groups
    await _save_state(state)
    await disable_cmd.finish(f"✅ 已关闭群 {gid} 的入群验证（新申请转回 request_manager 人工流程）")


@verify_cmd.handle()
async def verify_status(event):
    if not is_owner(event):
        await verify_cmd.finish("只有主人可以查看入群验证状态")
    groups = _load_state().get("groups") or []
    usage = "\n用法：.战网验证开启 [群号] / .战网验证关闭 [群号]（在本群发送可省略群号）"
    if not groups:
        await verify_cmd.finish("当前没有群开启入群验证。" + usage)
    await verify_cmd.finish("已开启入群验证的群：" + "、".join(groups) + usage)
