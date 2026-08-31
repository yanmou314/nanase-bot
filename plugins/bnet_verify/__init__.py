"""入群验证（战网 ID）：主人按群开启后自动核验加群申请。

流程：群设置验证问题（如"请填写你的战网ID"），申请人填写的答案会随申请的
comment 到达；本插件把附言整体作为战网ID，调用 overstats
（/api/v2/dashen-profile）查询守望先锋档案——查到即自动通过，查不到则拒绝
并告知战网ID不正确；查询服务异常时申请保持待处理并私聊通知主人转人工。

通过后自动：把新成员的群名片改为战网ID（成员进群后异步重试），并把
QQ 与战网ID写入 owstats 的绑定表（进群即可直接使用 .战报/.总结 等命令）。

注意：
- 机器人必须持有目标群的管理员身份，否则无法通过/拒绝申请、无法改群名片。
- 验证问题本身需要在群设置里配置一次（OneBot 协议不支持机器人设置验证问题）。
- 大神档案仅覆盖国服数据，外服玩家查不到成绩会被拒绝。
"""

import asyncio
import os
import threading

from nonebot import logger, on_command, on_request
from nonebot.adapters.onebot.v11 import Bot, GroupRequestEvent, Message, MessageSegment
from nonebot.params import CommandArg

from common import (
    OWNER,
    get_http_client,
    is_owner,
    load_json_state,
    save_json_state_async,
)

API = os.getenv("OW_API_BASE", "http://127.0.0.1:18080")
STATE_FILE = os.path.join(os.path.dirname(__file__), "groups.json")
_STATE_LOCK = threading.RLock()

# 单次核验超时：申请人不在线等待，稍长无妨；上游有 2 并发/4 排队的队列
VERIFY_TIMEOUT = 60.0

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


def clean_join_answer(comment: str) -> str:
    """申请附言即为战网ID本体，不做任何提取：仅裁剪首尾空白并把全角#归一。"""
    return (comment or "").replace("＃", "#").strip()


async def query_overwatch_profile(tag: str) -> dict:
    """查询守望先锋档案。返回 {"status": "found"|"not_found"|"error", ...}。"""
    client = get_http_client(VERIFY_TIMEOUT)
    try:
        r = await client.post(
            f"{API}/api/v2/dashen-profile",
            json={"bnet_id": tag},
            timeout=VERIFY_TIMEOUT,
        )
    except Exception as exc:
        return {"status": "error", "error": repr(exc)}
    try:
        data = r.json()
    except Exception:
        return {"status": "error", "error": f"上游响应非JSON（HTTP {r.status_code}）"}
    if not isinstance(data, dict):
        return {"status": "error", "error": "上游返回格式异常"}
    if data.get("ok"):
        return {"status": "found"}
    code = str(data.get("error") or "")
    if code in ("bnet_not_found", "missing_target"):
        return {"status": "not_found"}
    return {"status": "error", "error": code or f"HTTP {r.status_code}"}


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
            "战网ID不能为空：请填写你的战网ID（形如 昵称#12345）后重新申请",
        )
        return
    result = await query_overwatch_profile(tag)
    if result["status"] == "found":
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
    if result["status"] == "not_found":
        await _reject(
            bot, event,
            f"战网ID不正确：未查询到 {tag} 的守望先锋成绩，"
            "请核对名字大小写和 # 后面的数字后重新申请",
        )
        return
    # 查询服务异常：不盲目通过/拒绝，保持待处理并转人工
    logger.warning(
        f"[bnet_verify] 群 {gid} 验证查询失败（{result.get('error')}），"
        f"申请转人工 user={event.user_id}"
    )
    await _notify_owner(
        bot,
        MessageSegment.text(f"⚠️ 入群验证服务异常，申请待人工处理\n🏘 群号：{gid}\n👤 QQ：{event.user_id}\n💬 附言：")
        + MessageSegment.text(comment[:200])  # 申请人可控内容，防 CQ 码注入
        + MessageSegment.text(f"\n❓ 原因：{result.get('error')}"),
    )


verify_matcher = on_request(priority=1, block=False)


@verify_matcher.handle()
async def handle_join_request(bot: Bot, event):
    if not isinstance(event, GroupRequestEvent) or event.sub_type not in ("add", "apply"):
        return
    await _handle_group_join(bot, event)


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
