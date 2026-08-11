import asyncio
import json
import os
import re
import threading
import time
from collections import Counter
from datetime import date, timedelta
from nonebot import get_bot, on_command, on_message
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment
from nonebot.params import CommandArg
from nonebot_plugin_apscheduler import scheduler

from common import cleanup_cache, is_owner
from .db_pg import exec, write as db_write

WORD_CACHE = os.path.join(os.path.dirname(__file__), "cache")
WORDS_STATE = os.path.join(os.path.dirname(__file__), "words_state.json")
_state_lock = threading.Lock()
_nick_cache: dict = {}
_nick_ts: dict = {}
NICK_TTL = 300
RETENTION_DAYS = 30

record_matcher = on_message(priority=1, block=False)
dragon_cmd = on_command("龙王", priority=5, block=True)
words_cmd = on_command("词云", priority=5, block=True)
words_on_cmd = on_command("词云开启", priority=5, block=True)
words_off_cmd = on_command("词云关闭", priority=5, block=True)
words_status_cmd = on_command("词云状态", priority=5, block=True)

STOPWORDS = set("的了是在我有和你这不那啊呢吧吗哦嗯就都要也会没很他说她我们他们自己一个没什么可以"
                "真的还是因为所以但是然后现在今天明天昨天知道觉得应该可能如果这样那样这个那个什么为什么怎么")

FONT = "/usr/share/fonts/custom/ZCOOLKuaiLe-Regular.ttf"
PALETTE = ["#FF6B6B", "#FCA311", "#FFC93C", "#4ECDC4", "#45B7D1", "#96CEB4",
           "#F15BB5", "#9B5DE5", "#00BBF9", "#FEE440", "#FF8C42", "#5FD068"]


# ---------------- 存储（PostgreSQL） ----------------

async def _purge_old_records() -> int:
    cutoff = (date.today() - timedelta(days=RETENTION_DAYS)).isoformat()
    return len(await exec("DELETE FROM messages WHERE day < %s RETURNING id", (cutoff,)))


@scheduler.scheduled_job("cron", hour=3, minute=0, id="purge_old_stats", timezone="Asia/Shanghai")
async def purge_old_stats():
    try:
        await _purge_old_records()
    except Exception:
        pass


# ---------------- 消息记录 ----------------

@record_matcher.handle()
async def record(event: GroupMessageEvent):
    if not hasattr(event, "group_id"):
        return
    mtype = "text"
    for seg in event.message:
        if seg.type != "text":
            mtype = seg.type
            break
    await db_write(event.group_id, event.user_id, mtype, event.get_plaintext())


# ---------------- 工具 ----------------

async def _get_name(bot: Bot, group_id: int, user_id: int) -> str:
    key = (group_id, user_id)
    now = time.time()
    cached = _nick_cache.get(key)
    if cached and now - _nick_ts.get(key, 0) < NICK_TTL:
        return cached
    try:
        info = await bot.get_group_member_info(group_id=group_id, user_id=user_id)
        name = info.get("card") or info.get("nickname") or str(user_id)
    except Exception:
        name = str(user_id)
    _nick_cache[key] = name
    _nick_ts[key] = now
    return name


async def _build_word_image(group_id: int, day: str, n: int, window: bool = False) -> str | None:
    if window:
        rows = await exec(
            "SELECT text FROM messages WHERE group_id=%s AND "
            "((day=CURRENT_DATE-1 AND hour>=7) OR (day=CURRENT_DATE AND hour<7)) AND text!=''",
            (group_id,),
        )
    else:
        rows = await exec(
            "SELECT text FROM messages WHERE group_id=%s AND "
            "((day=CURRENT_DATE-1 AND hour>=7) OR (day=CURRENT_DATE AND hour<7)) AND text!=''",
            (group_id,),
        )
    counter: Counter = Counter()
    for (text,) in rows:
        for seg in re.findall(r"[\u4e00-\u9fff]{2,}", text):
            if seg not in STOPWORDS:
                counter[seg] += 1
    if not counter:
        return None
    cleanup_cache(WORD_CACHE)
    from .wordcloud_card import _render as render_cloud
    return await asyncio.to_thread(render_cloud, counter, min(n, len(counter)), len(rows))


# ---------------- 龙王 ----------------

@dragon_cmd.handle()
async def dragon(bot: Bot, event: GroupMessageEvent):
    if not is_owner(event):
        await dragon_cmd.finish("❌ 你没有权限使用此功能")
    rows = await exec(
        "SELECT user_id, COUNT(*) c FROM messages WHERE group_id=%s AND day=CURRENT_DATE "
        "GROUP BY user_id ORDER BY c DESC LIMIT 3",
        (event.group_id,),
    )
    if not rows:
        await dragon_cmd.finish("今天还没有人发言哦～")
    names = {r[0]: await _get_name(bot, event.group_id, r[0]) for r in rows}
    data = [(uid, names[uid], cnt) for uid, cnt in rows]
    from .dragon_card import build_card_async
    path = await build_card_async(data)
    await dragon_cmd.finish(MessageSegment.image("file://" + path))


# ---------------- 词频推送状态 ----------------

def _words_group() -> str:
    with _state_lock:
        try:
            with open(WORDS_STATE, "r", encoding="utf-8") as f:
                return (json.load(f).get("group_id") or "").strip()
        except Exception:
            return ""


def _set_words_group(gid: str) -> None:
    with _state_lock:
        with open(WORDS_STATE, "w", encoding="utf-8") as f:
            json.dump({"group_id": gid}, f, ensure_ascii=False, indent=2)


def _clear_words_group() -> None:
    with _state_lock:
        try:
            os.remove(WORDS_STATE)
        except OSError:
            pass


@words_on_cmd.handle()
async def words_on(event: GroupMessageEvent):
    if not is_owner(event):
        await words_on_cmd.finish("❌ 你没有权限使用此功能")
    if not hasattr(event, "group_id"):
        await words_on_cmd.finish("请在有机器人的群里开启此功能")
    _set_words_group(str(event.group_id))
    await words_on_cmd.finish(f"✅ 每日词云推送已开启\n每天 8:00 自动发送 7:00 前 24 小时的热词词云到此群（本群 {event.group_id}）")


@words_off_cmd.handle()
async def words_off(event: GroupMessageEvent):
    if not is_owner(event):
        await words_off_cmd.finish("❌ 你没有权限使用此功能")
    _clear_words_group()
    await words_off_cmd.finish("✅ 每日词云推送已关闭")


@words_status_cmd.handle()
async def words_status(event: GroupMessageEvent):
    if not is_owner(event):
        await words_status_cmd.finish("❌ 你没有权限使用此功能")
    gid = _words_group()
    if gid:
        await words_status_cmd.finish(f"📊 每日词云推送：已开启（群 {gid}，每天 8:00 发送 7:00 前 24 小时热词）")
    await words_status_cmd.finish("📊 每日词云推送：未开启")


@scheduler.scheduled_job("cron", hour=8, minute=5, id="daily_words", timezone="Asia/Shanghai")
async def daily_words_job():
    gid = _words_group()
    if not gid:
        return
    try:
        path = await _build_word_image(int(gid), "", 40, window=True)
        if not path:
            return
        bot = get_bot()
        await bot.send_group_msg(group_id=int(gid), message=MessageSegment.image("file://" + path))
    except Exception:
        pass


# ---------------- 词频 ----------------

@words_cmd.handle()
async def words(event: GroupMessageEvent, arg: Message = CommandArg()):
    if not is_owner(event):
        await words_cmd.finish("❌ 你没有权限使用此功能")
    try:
        n = max(1, min(int(arg.extract_plain_text().strip() or 40), 60))
    except ValueError:
        n = 20
    path = await _build_word_image(event.group_id, date.today().isoformat(), n)
    if not path:
        await words_cmd.finish("今天还没有可统计的文字内容～")
    await words_cmd.finish(MessageSegment.image("file://" + path))
