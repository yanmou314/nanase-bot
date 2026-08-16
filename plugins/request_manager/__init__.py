import json
import logging
import os
import re
import threading
import time
from datetime import datetime

from nonebot import on_command, on_message, on_request
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import (
    FriendRequestEvent,
    GroupRequestEvent,
    MessageEvent,
    MessageSegment,
)
from nonebot.params import CommandArg

from common import OWNER

_logger = logging.getLogger(__name__)

request_matcher = on_request(priority=1, block=False)
private_matcher = on_message(priority=20, block=False)

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "auto_approve.json")

auto_on_cmd = on_command("自动通过", aliases={"自动同意"}, priority=5, block=True)
auto_off_cmd = on_command("自动通过关闭", aliases={"自动同意关闭"}, priority=5, block=True)
auto_show_cmd = on_command("自动通过查看", aliases={"自动同意查看"}, priority=5, block=True)
auto_count_cmd = on_command("自动通过数量", aliases={"自动同意数量", "自动通过统计", "自动同意统计"}, priority=5, block=True)

_pending: dict[str, dict] = {}
_save_lock = threading.Lock()
_PENDING_TTL = 48 * 3600  # 与 QQ 侧 flag 有效期一致


def _now() -> str:
    return datetime.now().strftime("%m-%d %H:%M")


def _purge_pending() -> None:
    """清理超过 TTL 的待处理申请，防止内存无限增长。"""
    now = time.time()
    for key in list(_pending):
        if now - _pending[key].get("ts", 0) > _PENDING_TTL:
            _pending.pop(key, None)


def _load_config() -> dict:
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        # 文件损坏/读取失败时先备份现场再返回空，防止后续保存把其他群的配置静默清掉
        backup = f"{CONFIG_FILE}.corrupt-{int(time.time())}"
        try:
            os.replace(CONFIG_FILE, backup)
        except OSError:
            pass
        _logger.error("自动通过配置读取失败，原文件已备份为 %s", backup, exc_info=True)
        return {}


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
        temporary = CONFIG_FILE + ".tmp"
        with open(temporary, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(temporary, CONFIG_FILE)
        return keywords


async def _auto_approve(bot: Bot, event: GroupRequestEvent, comment: str) -> bool:
    keywords = _load_keywords(event.group_id)
    if not keywords:
        return False
    comment_lower = comment.lower()  # 大小写不敏感匹配（中文不受影响）
    hit = [k for k in keywords if k.lower() in comment_lower]
    if hit:
        try:
            await bot.set_group_add_request(flag=event.flag, sub_type=event.sub_type, approve=True)
            await bot.send_private_msg(
                user_id=int(OWNER),
                message=MessageSegment.text(
                    f"✅ 自动通过进群申请\n"
                    f"🏘 群号：{event.group_id}\n"
                    f"👤 申请人：{event.user_id}（{_now()}）\n"
                    f"💬 附言：{comment or '无'}\n"
                    f"🔑 命中关键字：{' / '.join(hit)}"
                ),
            )
            return True
        except Exception as e:
            try:
                await bot.send_private_msg(user_id=int(OWNER), message=MessageSegment.text(f"⚠️ 自动通过失败：{e}"))
            except Exception:
                pass
            return False
    return False


# ---------------- 关键字配置（群内或私聊按群号） ----------------
@auto_on_cmd.handle()
async def auto_on(event: MessageEvent, arg=CommandArg()):
    if str(event.user_id) != OWNER:
        await auto_on_cmd.finish("❌ 你没有权限使用此功能")
    text = arg.extract_plain_text().strip()
    if not text:
        if hasattr(event, "group_id"):
            await auto_on_cmd.finish("用法：.自动通过 关键字\n例如：.自动通过 我是老玩家\n多个关键字用空格分隔：.自动通过 关键字1 关键字2")
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
    await auto_on_cmd.finish(f"✅ 群 {gid} 自动通过已开启\n🔑 关键字：{' / '.join(merged)}\n进群附言包含任一关键字将自动同意（不区分大小写）")


@auto_off_cmd.handle()
async def auto_off(event: MessageEvent, arg=CommandArg()):
    if str(event.user_id) != OWNER:
        await auto_off_cmd.finish("❌ 你没有权限使用此功能")
    text = arg.extract_plain_text().strip()
    if hasattr(event, "group_id"):
        gid = event.group_id
    else:
        if not text.isdigit():
            await auto_off_cmd.finish("私聊用法：.自动通过关闭 <群号>\n例如：.自动通过关闭 <群号>")
        gid = int(text)
    _save_keywords(gid, [])
    await auto_off_cmd.finish(f"✅ 群 {gid} 自动通过已关闭，进群申请将等待手动处理")


@auto_show_cmd.handle()
async def auto_show(event: MessageEvent):
    if str(event.user_id) != OWNER:
        await auto_show_cmd.finish("❌ 你没有权限使用此功能")
    if hasattr(event, "group_id"):
        kws = _load_keywords(event.group_id)
        if kws:
            await auto_show_cmd.finish(f"🔑 本群自动通过已开启\n🏘 群号：{event.group_id}\n关键字：{' / '.join(kws)}")
        await auto_show_cmd.finish(f"🔑 本群自动通过未开启（群 {event.group_id}）")
    data = _load_config()
    lines = ["🔑 已开启自动通过的群："]
    for gid, kws in data.items():
        if kws:
            lines.append(f"群 {gid}：{' / '.join(kws)}")
    if len(lines) == 1:
        await auto_show_cmd.finish("🔑 没有任何群开启自动通过")
    await auto_show_cmd.finish("\n".join(lines))


# ---------------- 好友申请 / 加群申请 ----------------
@auto_count_cmd.handle()
async def auto_count(event: MessageEvent, arg=CommandArg()):
    if str(event.user_id) != OWNER:
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
        await auto_count_cmd.finish(f"🔑 群 {gid} 共有 {len(kws)} 个关键字：{' / '.join(kws)}")
    await auto_count_cmd.finish(f"🔑 群 {gid} 当前没有配置自动通过关键字（共 0 个）")


@request_matcher.handle()
async def handle_request(bot: Bot, event):
    if event.user_id == int(OWNER):
        return
    _purge_pending()
    ts = _now()
    if isinstance(event, GroupRequestEvent) and event.sub_type in ("add", "apply"):
        if await _auto_approve(bot, event, event.comment or ""):
            return
        # 诊断日志：记录未自动通过时的原始附言（验证问答的回答也在这里），便于排查
        _logger.debug(
            "加群申请未自动通过: group=%s user=%s sub=%s comment=%r",
            event.group_id, event.user_id, event.sub_type, event.comment,
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
            f"回复「同意」或「拒绝」处理"
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
            f"回复「同意」或「拒绝」处理"
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
            f"回复「同意」或「拒绝」处理"
        )
    else:
        return
    try:
        # MessageSegment.text 包裹：附言等申请人可控文本不会被解析为 CQ 码（防注入）
        await bot.send_private_msg(user_id=int(OWNER), message=MessageSegment.text(msg))
    except Exception:
        pass


# ---------------- 主人回复 同意/拒绝 ----------------
@private_matcher.handle()
async def owner_decision(bot: Bot, event: MessageEvent):
    if str(event.user_id) != OWNER or event.message_type != "private":
        return
    text = event.get_plaintext().strip()
    if text == "同意":
        action = True
    elif text == "拒绝":
        action = False
    else:
        return
    _purge_pending()
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
            if m:
                uid = m.group(1)
                for k, v in _pending.items():
                    if v["kind"] == "friend" and str(v.get("user_id")) == uid:
                        target_key = k
                        break
            if target_key is None:
                m = re.search(r"群号[:：](\d+)", quoted)
                if m:
                    gid = int(m.group(1))
                    for k, v in _pending.items():
                        if v["kind"] == "group" and v.get("group_id") == gid:
                            target_key = k
                            break
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
    kind, flag = val["kind"], val["flag"]
    try:
        if kind == "friend":
            if action:
                await bot.set_friend_add_request(flag=flag, approve=True, remark="")
                await bot.send_private_msg(user_id=int(OWNER), message=f"✅ 已同意好友申请（QQ {val.get('user_id')}）")
            else:
                await bot.set_friend_add_request(flag=flag, approve=False)
                await bot.send_private_msg(user_id=int(OWNER), message=f"❌ 已拒绝好友申请（QQ {val.get('user_id')}）")
        else:
            sub = val["sub_type"]  # 使用真实 sub_type，add 申请不再错误地以 invite 处理
            if action:
                await bot.set_group_add_request(flag=flag, sub_type=sub, approve=True)
                await bot.send_private_msg(user_id=int(OWNER), message=f"✅ 已同意进群（群 {val.get('group_id')}）")
            else:
                await bot.set_group_add_request(flag=flag, sub_type=sub, approve=False)
                await bot.send_private_msg(user_id=int(OWNER), message=f"❌ 已拒绝进群（群 {val.get('group_id')}）")
    except Exception as e:
        _pending[target_key] = val  # 处理失败时放回待处理列表，主人可重试
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
        await bot.send_private_msg(user_id=int(OWNER), message=MessageSegment.text(msg))
    except Exception:
        pass
