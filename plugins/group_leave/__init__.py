import asyncio
import logging
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

from common import load_json_state, save_json_state

_logger = logging.getLogger(__name__)

leave_matcher = on_notice(priority=1, block=False)
welcome_matcher = on_notice(priority=1, block=False)

STATE_FILE = os.path.join(os.path.dirname(__file__), "join_state.json")

# (group_id, user_id) -> 入群时间戳
_join_ts: dict = {}
_JOIN_MAX_KEYS = 20000  # 防止字典无限增长，超过后清理 30 天前的记录（全量 dict 常驻内存，2 万条约 3-5MB）
_JOIN_TTL = 30 * 86400

_imported_groups: set = set()  # 本次进程已导入过的群，避免重复拉取

MESSAGES = [
    "呜……ななせ有点难过呢。虽然舍不得，但还是祝你一路顺风，要照顾好自己哦 (´･ω･`)",
    "えへへ……虽然有点寂寞，但离开的人也要幸福才行！以后要常回来看看ななせ哦～",
    "ふふっ，群里的座位少了一个呢。不过没关系，江湖再见！ななせ会记得你的～",
    "うん……さよなら有点难说出口，但天下没有不散的筵席嘛。愿你一切都好 (っ´ω`c)",
    "嘛～走之前都没有好好和我道别，ななせ有点小生气哦！不过……还是祝你前程似锦！",
    "诶？要、要走啦？呜哇，等等……至少让ななせ画一张送别的画吧！……已经走了吗，那、那你要幸福呀！",
    "だよ～虽然很不舍，但说不定哪天还会再见的！常回来看看，群里永远欢迎你哦～",
    "（在画本上偷偷写下他的名字）愿你的未来像糖果一样甜，一路顺风呀～",
    "诶嘿嘿，虽然你退群了，但在这里聊过的天、说过的话，ななせ都会好好记住的！保重哦～",
    "さようなら……うん，再见啦！要记得按时吃饭、照顾好自己，ななせ会在这里为你加油的 (＞＜)",
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
    data = load_json_state(STATE_FILE)
    for key, ts in data.items():
        try:
            gid, uid = key.split(":", 1)
            gid, uid, ts = int(gid), int(uid), float(ts)
            if gid > 0 and uid > 0 and ts > 0:
                _join_ts[(gid, uid)] = ts
        except (TypeError, ValueError):
            continue


def _save_state(snapshot: dict | None = None) -> None:
    """【同步阻塞】全量落盘；事件循环内请改用 _save_state_async（先快照再进线程）。"""
    try:
        data = snapshot if snapshot is not None else _join_ts
        save_json_state(STATE_FILE, {f"{g}:{u}": t for (g, u), t in data.items()})
    except Exception:
        _logger.warning("group_leave 状态写入失败: %s", STATE_FILE, exc_info=True)


async def _save_state_async() -> None:
    """事件循环内先做 dict 快照，再交给线程全量写盘。

    入群记录上限 10 万条、全量序列化可达数 MB，直接在事件循环内写会阻塞其他事件；
    先 copy 再进线程也避免了线程内迭代时字典被并发修改。
    """
    snapshot = dict(_join_ts)
    await asyncio.to_thread(_save_state, snapshot)


_load_state()


@get_driver().on_shutdown
async def _save_on_shutdown():
    # 复用"先快照再进线程"模式，避免线程内迭代 _join_ts 时字典被修改
    await _save_state_async()


def _prune() -> None:
    if len(_join_ts) <= _JOIN_MAX_KEYS:
        return
    cutoff = time.time() - _JOIN_TTL
    for key in list(_join_ts):
        if _join_ts[key] < cutoff:
            _join_ts.pop(key, None)
    # 落盘由调用方统一执行，避免一次操作触发多次全量写盘


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


async def _record_join(gid: int, uid: int) -> None:
    _join_ts[(gid, uid)] = time.time()
    _prune()
    await _save_state_async()  # 写盘放线程，避免全量序列化阻塞事件循环


def _pop_join(gid: int, uid: int):
    return _join_ts.pop((gid, uid), None)


async def _import_group_members(bot: Bot, gid: int) -> bool:
    """拉取群成员列表，用 API 的 join_time 补录老成员的入群时间。

    返回本轮是否新增了成员记录。已导入过的群直接跳过（避免重复拉取）；
    重连期间新加入的群不在 _imported_groups 中，重连时会被正常补录。
    """
    if gid in _imported_groups:
        return False
    try:
        members = await bot.get_group_member_list(group_id=gid)
    except Exception:
        return False
    _imported_groups.add(gid)
    now = time.time()
    added = 0
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
        added += 1
    _prune()
    # 落盘由 _import_existing_members 在所有群导完后统一判断执行
    return added > 0


@get_driver().on_bot_connect
async def _import_existing_members(bot: Bot):
    """机器人连上后，一次性导入各群老成员的入群时间（join_time）。

    仅当本轮确有新增记录时才落盘一次：WS 重连很频繁，无新增时全量写 2 万条
    状态纯属浪费（同步盘只有 1.6G 小机的宝贵 IO）。
    """
    try:
        groups = await bot.get_group_list()
    except Exception:
        return
    added = False
    for g in groups:
        if await _import_group_members(bot, g["group_id"]):
            added = True
    if added:
        await _save_state_async()


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
    if sub not in ("leave", "kick"):
        return  # 未知 sub_type 不弹出记录，避免误丢逗留时长数据
    name = await _get_name(bot, uid)

    ts = _pop_join(gid, uid)
    if ts is not None:
        dur = _format_duration(time.time() - ts)
        await _save_state_async()

    if sub == "leave":
        msg = f"👋 {name}（{uid}）退群了"
        if ts is not None:
            msg += f"，在群里待了 {dur}"
        msg += "\n" + random.choice(MESSAGES)
    else:
        op = "群管理员"
        try:
            info = await bot.get_group_member_info(group_id=gid, user_id=event.operator_id)
            op = info.get("card") or info.get("nickname") or str(event.operator_id)
        except Exception:
            pass
        msg = f"🔨 {name}（{uid}）被 {op} 移出了群"
        if ts is not None:
            msg += f"，在群里待了 {dur}"

    try:
        # MessageSegment.text 包裹：昵称等外部文本不会被解析为 CQ 码（防注入）
        await bot.send_group_msg(group_id=gid, message=MessageSegment.text(msg))
    except Exception:
        pass


@welcome_matcher.handle()
async def handle_welcome(bot: Bot, event: GroupIncreaseNoticeEvent):
    uid = event.user_id
    gid = event.group_id
    if uid == event.self_id:
        return  # 机器人自己被拉进群，不 @ 自己
    name = await _get_name(bot, uid)
    msg = (
        Message(MessageSegment.at(uid))
        + MessageSegment.text(f" 欢迎 {name} 加入本群！\n{random.choice(WELCOME_MESSAGES)}")
    )
    await _record_join(gid, uid)
    try:
        await bot.send_group_msg(group_id=gid, message=msg)
    except Exception:
        pass
