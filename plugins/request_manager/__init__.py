import re
import time
from datetime import datetime

from nonebot import on_message, on_request
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import (
    FriendRequestEvent,
    GroupRequestEvent,
    MessageEvent,
)

from common import OWNER

request_matcher = on_request(priority=1, block=False)
private_matcher = on_message(priority=20, block=False)

_pending = {}


def _now() -> str:
    return datetime.now().strftime("%m-%d %H:%M")


# ---------------- 好友申请 / 加群申请 ----------------
@request_matcher.handle()
async def handle_request(bot: Bot, event):
    if event.user_id == int(OWNER):
        return
    ts = _now()
    if isinstance(event, FriendRequestEvent):
        _pending[str(event.user_id)] = ("friend", event.flag)
        msg = (
            f"🔔 好友申请\n"
            f"👤 申请人：{event.user_id}（{ts}）\n"
            f"💬 验证消息：{event.comment or '无'}\n"
            f"回复「同意」或「拒绝」处理"
        )
    elif isinstance(event, GroupRequestEvent) and event.sub_type == "invite":
        _pending[f"g{event.group_id}"] = ("group", event.flag, event.group_id)
        msg = (
            f"🔔 有人邀请机器人进群\n"
            f"🏘 群号：{event.group_id}\n"
            f"👤 邀请人：{event.user_id}（{ts}）\n"
            f"💬 附言：{event.comment or '无'}\n"
            f"回复「同意」或「拒绝」处理"
        )
    else:
        return
    try:
        await bot.send_private_msg(user_id=int(OWNER), message=msg)
    except Exception:
        pass


# ---------------- 主人回复 同意/拒绝 ----------------
@private_matcher.handle()
async def owner_decision(bot: Bot, event: MessageEvent):
    if str(event.user_id) != OWNER or event.message_type != "private":
        return
    text = event.get_plaintext().strip()
    if text.startswith("同意"):
        action = True
    elif text.startswith("拒绝"):
        action = False
    else:
        return
    if not _pending:
        await bot.send_private_msg(user_id=int(OWNER), message="📭 当前没有待处理的申请")
        return

    target_key = None
    reply_id = None
    for seg in event.message:
        if seg.type == "reply":
            reply_id = seg.data.get("id")
            break
    if reply_id:
        try:
            info = await bot.get_msg(message_id=int(reply_id))
            quoted = str(info.get("raw_message") or "")
            m = re.search(r"申请人[:：](\d+)", quoted)
            if m and m.group(1) in _pending:
                target_key = m.group(1)
            m = re.search(r"群号[:：](\d+)", quoted)
            if m and f"g{m.group(1)}" in _pending:
                target_key = f"g{m.group(1)}"
        except Exception:
            pass

    if target_key is None:
        if len(_pending) == 1:
            target_key = next(iter(_pending))
        else:
            await bot.send_private_msg(
                user_id=int(OWNER),
                message="⚠️ 有多个待处理申请，请回复时引用对应的通知消息来指定处理哪一个",
            )
            return

    val = _pending.pop(target_key)
    kind, flag = val[0], val[1]
    try:
        if kind == "friend":
            if action:
                await bot.set_friend_add_request(flag=flag, approve=True, remark="")
                await bot.send_private_msg(user_id=int(OWNER), message=f"✅ 已同意好友申请（QQ {target_key}）")
            else:
                await bot.set_friend_add_request(flag=flag, approve=False)
                await bot.send_private_msg(user_id=int(OWNER), message=f"❌ 已拒绝好友申请（QQ {target_key}）")
        else:
            sub = "invite"
            if action:
                await bot.set_group_add_request(flag=flag, sub_type=sub, approve=True)
                await bot.send_private_msg(user_id=int(OWNER), message=f"✅ 已同意进群（群 {val[2]}）")
            else:
                await bot.set_group_add_request(flag=flag, sub_type=sub, approve=False)
                await bot.send_private_msg(user_id=int(OWNER), message=f"❌ 已拒绝进群（群 {val[2]}）")
    except Exception as e:
        await bot.send_private_msg(user_id=int(OWNER), message=f"⚠️ 处理失败：{e}")


# ---------------- 私聊消息转发 ----------------
@private_matcher.handle()
async def forward_private(bot: Bot, event: MessageEvent):
    if str(event.user_id) == OWNER or event.message_type != "private":
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
        f"🕐 {_now()}\n"
        f"💬 {text}"
    )
    try:
        await bot.send_private_msg(user_id=int(OWNER), message=msg)
    except Exception:
        pass
