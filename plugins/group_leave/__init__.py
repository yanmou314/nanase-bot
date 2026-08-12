import asyncio
import json
import os
import random
import time

from nonebot import get_driver, on_notice
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import (
    GroupDecreaseNoticeEvent,
    GroupIncreaseNoticeEvent,
    Message,
    MessageSegment,
)

leave_matcher = on_notice(priority=1, block=False)
welcome_matcher = on_notice(priority=1, block=False)

STATE_FILE = os.path.join(os.path.dirname(__file__), "join_state.json")

# (group_id, user_id) -> 入群时间戳
_join_ts: dict = {}
_JOIN_MAX_KEYS = 100000  # 防止字典无限增长，超过后清理 30 天前的记录
_JOIN_TTL = 30 * 86400

_imported_groups: set = set()  # 本次进程已导入过的群，避免重复拉取

MESSAGES = [
    "江湖再见～",
    "祝他前程似锦！",
    "一路顺风，常回来看看～",
    "散伙饭看来是不用吃了",
]

WELCOME_MESSAGES = [
    "えへへ～有新的朋友进来了！我是ななせ，请多关照哦～",
    "欢迎加入呢！ふふっ，以后聊天也要记得叫上ななせ哦 (っ´ω`c)",
    "呜哇，来了新朋友！紧张得不知道该说什么了……总之，欢迎你～",
    "嘛～是新面孔呢！要和大家好好相处哦，ななせ会一直在这里的！",
    "欢迎新朋友！えへへ，有什么想聊的都可以来找我哦～",
    "だよ～终于等到新朋友了！欢迎欢迎，以后就是一家人了呢！",
    "（从画本后面悄悄探出头）诶？有新人？……欢、欢迎你呀，要一起画画吗？",
    "你好呀，我是ななせ！うん，欢迎来到我们的群，要玩得开心哦～",
    "欢迎欢迎！ふふっ，群里又热闹了一点呢，ななせ好开心！",
    "新朋友进来啦！诶嘿嘿，先收下ななせ的欢迎（递出一块小蛋糕）～",
]


def _load_state() -> None:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for key, ts in data.items():
            gid, uid = key.split(":", 1)
            _join_ts[(int(gid), int(uid))] = float(ts)
    except Exception:
        pass


def _save_state() -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {f"{g}:{u}": t for (g, u), t in _join_ts.items()},
                f,
                ensure_ascii=False,
            )
    except Exception:
        pass


_load_state()


@get_driver().on_shutdown
async def _save_on_shutdown():
    await asyncio.to_thread(_save_state)


def _prune() -> None:
    if len(_join_ts) <= _JOIN_MAX_KEYS:
        return
    cutoff = time.time() - _JOIN_TTL
    for key in list(_join_ts):
        if _join_ts[key] < cutoff:
            _join_ts.pop(key, None)
    _save_state()


def _format_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} 秒"
    minutes = seconds // 60
    if seconds < 3600:
        return f"{minutes} 分 {seconds % 60} 秒"
    hours = seconds // 3600
    if seconds < 86400:
        return f"{hours} 小时 {minutes % 60} 分钟"
    days = seconds // 86400
    return f"{days} 天 {hours % 24} 小时"


def _record_join(gid: int, uid: int) -> None:
    _join_ts[(gid, uid)] = time.time()
    _prune()
    _save_state()


def _pop_join(gid: int, uid: int):
    return _join_ts.pop((gid, uid), None)


async def _import_group_members(bot: Bot, gid: int) -> None:
    """拉取群成员列表，用 API 的 join_time 补录老成员的入群时间。"""
    if gid in _imported_groups:
        return
    _imported_groups.add(gid)
    try:
        members = await bot.get_group_member_list(group_id=gid)
    except Exception:
        return
    now = time.time()
    for m in members:
        uid = m.get("user_id")
        if not uid or str(uid) == str(bot.self_id):
            continue  # 机器人自己不记录
        key = (gid, int(uid))
        if key in _join_ts:
            continue  # 已有记录（进群通知写入的），不覆盖
        try:
            ts = float(m.get("join_time") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        _join_ts[key] = ts if ts > 0 else now
    _prune()
    _save_state()


@get_driver().on_bot_connect
async def _import_existing_members(bot: Bot):
    """机器人连上后，一次性导入各群老成员的入群时间（join_time）。"""
    try:
        groups = await bot.get_group_list()
    except Exception:
        return
    for g in groups:
        await _import_group_members(bot, g["group_id"])


async def _get_name(bot: Bot, user_id: int) -> str:
    try:
        info = await bot.get_stranger_info(user_id=user_id)
        return info.get("nickname") or str(user_id)
    except Exception:
        return str(user_id)


@leave_matcher.handle()
async def handle(bot: Bot, event: GroupDecreaseNoticeEvent):
    uid = event.user_id
    gid = event.group_id
    sub = event.sub_type
    name = await _get_name(bot, uid)

    ts = _pop_join(gid, uid)
    if ts is not None:
        dur = _format_duration(time.time() - ts)
        _save_state()

    if sub == "leave":
        msg = f"👋 {name}（{uid}）退群了"
        if ts is not None:
            msg += f"，在群里待了 {dur}"
        msg += "\n" + random.choice(MESSAGES)
    elif sub == "kick":
        op = "群管理员"
        try:
            info = await bot.get_group_member_info(group_id=gid, user_id=event.operator_id)
            op = info.get("card") or info.get("nickname") or str(event.operator_id)
        except Exception:
            pass
        msg = f"🔨 {name}（{uid}）被 {op} 移出了群"
        if ts is not None:
            msg += f"，在群里待了 {dur}"
    else:
        return

    try:
        await bot.send_group_msg(group_id=gid, message=msg)
    except Exception:
        pass


@welcome_matcher.handle()
async def handle_welcome(bot: Bot, event: GroupIncreaseNoticeEvent):
    uid = event.user_id
    gid = event.group_id
    if uid == event.self_id:
        return  # 机器人自己被拉进群，不 @ 自己
    name = await _get_name(bot, uid)
    msg = Message(MessageSegment.at(uid)) + f" 欢迎 {name} 加入本群！" + chr(10) + f"{random.choice(WELCOME_MESSAGES)}"
    _record_join(gid, uid)
    try:
        await bot.send_group_msg(group_id=gid, message=msg)
    except Exception:
        pass
