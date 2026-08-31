import asyncio
import logging
import os
import re
import threading
from collections import Counter
from contextlib import aclosing
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from nonebot import get_bot, get_driver, on_command, on_message
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment
from nonebot.params import CommandArg
from nonebot_plugin_apscheduler import scheduler

from common import (
    RENDER_SEM,
    cleanup_cache,
    get_member_name,
    is_owner,
    load_json_state,
    save_json_state,
)
from .db_pg import exec, iter_rows, wait_writes_drained, write as db_write

_logger = logging.getLogger(__name__)
_SH = ZoneInfo("Asia/Shanghai")

WORD_CACHE = os.path.join(os.path.dirname(__file__), "cache")
WORDS_STATE = os.path.join(os.path.dirname(__file__), "words_state.json")
_state_lock = threading.RLock()  # 必须可重入：_words_groups 持锁时内部会再调保存
RETENTION_DAYS = 30
WORDS_CUTOFF_HOUR = 0

record_matcher = on_message(priority=1, block=False)
dragon_cmd = on_command("龙王", priority=5, block=True)
words_cmd = on_command("词云", priority=5, block=True)
words_on_cmd = on_command("词云开启", priority=5, block=True)
words_off_cmd = on_command("词云关闭", priority=5, block=True)
words_status_cmd = on_command("词云状态", priority=5, block=True)

_COMMAND_START = tuple(s for s in get_driver().config.command_start if s)  # 过滤空串，与 auto_chat 等插件一致

STOPWORDS = set("的了是在我有和你这不那啊呢吧吗哦嗯就都要也会没很他说她我们他们自己一个没什么可以"
                "真的还是因为所以但是然后现在今天明天昨天知道觉得应该可能如果这样那样这个那个什么为什么怎么")


def _sh_today() -> date:
    """上海时区的今天，供统计边界与清理使用（与调度器时区一致，不受数据库时区影响）。"""
    return datetime.now(_SH).date()


# ---------------- 存储（PostgreSQL） ----------------

async def _purge_old_records() -> None:
    cutoff = (_sh_today() - timedelta(days=RETENTION_DAYS)).isoformat()
    await exec("DELETE FROM messages WHERE day < %s", (cutoff,))
    # 指令使用记录与消息同保留期清理，否则 command_usages 会无限增长
    await exec("DELETE FROM command_usages WHERE day < %s", (cutoff,))


@scheduler.scheduled_job("cron", hour=3, minute=0, id="purge_old_stats", timezone="Asia/Shanghai")
async def purge_old_stats():
    try:
        await _purge_old_records()
    except Exception:
        _logger.exception("清理过期统计记录失败")


# ---------------- 消息记录 ----------------

@record_matcher.handle()
async def record(event: GroupMessageEvent):
    # msg_type 探测结果无任何查询消费方，已删除；统一按列默认值 "text" 记录
    await db_write(event.group_id, event.user_id, "text", event.get_plaintext())


# ---------------- 工具 ----------------

# 昵称获取统一走 common.get_member_name（带 TTL 的跨插件共享 LRU 缓存）


async def _build_word_image(group_id: int, n: int) -> str | None:
    # 词云窗口：昨天 00:00 至今天 00:00（按上海时区计算，不依赖数据库时区）
    today = _sh_today()
    yesterday = (today - timedelta(days=1)).isoformat()
    if WORDS_CUTOFF_HOUR == 0:
        # 截止 0 点时窗口恰为"昨天一整天"，day=昨天 与原条件等价且无恒假分支
        sql = "SELECT text FROM messages WHERE group_id=%s AND day=%s AND text!=''"
        args: tuple = (group_id, yesterday)
    else:
        sql = (
            "SELECT text FROM messages WHERE group_id=%s AND "
            "((day=%s AND hour>=%s) OR (day=%s AND hour<%s)) AND text!=''"
        )
        args = (group_id, yesterday, WORDS_CUTOFF_HOUR, today.isoformat(), WORDS_CUTOFF_HOUR)
    counter: Counter = Counter()
    normal_message_count = 0
    # 流式逐行消费：活跃大群的全天文本不再一次性载入内存；
    # aclosing 保证循环体异常时也能立即释放游标与池连接
    async with aclosing(iter_rows(sql, args)) as rows:
        async for (text,) in rows:
            if text.startswith(_COMMAND_START):
                continue
            normal_message_count += 1
            for seg in re.findall(r"[\u4e00-\u9fff]{2,}", text):
                if len(seg) > 8:
                    # 超长连续文本按 4 字非重叠分块切词（尾部不足 4 字的残余丢弃），
                    # 避免整段作为一个"词"无法排版
                    segs = [seg[i:i + 4] for i in range(0, len(seg) - 3, 4)]
                else:
                    segs = [seg]
                for s in segs:
                    if s not in STOPWORDS:
                        counter[s] += 1
    if not counter:
        return None
    await asyncio.to_thread(cleanup_cache, WORD_CACHE)  # 同步磁盘扫描不阻塞事件循环
    from .wordcloud_card import _render as render_cloud
    # PIL 渲染经全局渲染信号量串行化，避免小机器上并发渲染打爆内存
    async with RENDER_SEM:
        return await asyncio.to_thread(render_cloud, counter, min(n, len(counter)), normal_message_count)


# ---------------- 龙王 ----------------

@dragon_cmd.handle()
async def dragon(bot: Bot, event: GroupMessageEvent):
    if not is_owner(event):
        await dragon_cmd.finish("❌ 你没有权限使用此功能")
    try:
        rows = await exec(
            "SELECT user_id, COUNT(*) c FROM messages WHERE group_id=%s AND day=%s "
            "GROUP BY user_id ORDER BY c DESC LIMIT 3",
            (event.group_id, _sh_today().isoformat()),
        )
    except Exception:
        _logger.exception("龙王统计查询失败")
        await dragon_cmd.finish(MessageSegment.text("统计服务暂时不可用，请稍后再试"))
    if not rows:
        await dragon_cmd.finish("今天还没有人发言哦～")

    # 昵称并行拉取 + 整体 15 秒兜底（get_member_name 单次 10 秒超时，
    # 串行 await 遇 NapCat 卡死最坏挂 30 秒；与 cmd_stats 的处理一致）
    async def _name_or_fallback(uid: int) -> str:
        try:
            return await get_member_name(bot, event.group_id, uid)
        except Exception:
            return str(uid)

    try:
        name_list = await asyncio.wait_for(
            asyncio.gather(*(_name_or_fallback(uid) for uid, _ in rows)), 15
        )
    except asyncio.TimeoutError:
        _logger.warning("龙王昵称批量拉取超时，改用 QQ 号显示")
        name_list = [str(uid) for uid, _ in rows]
    names = {uid: name for (uid, _), name in zip(rows, name_list, strict=False)}
    data = [(uid, names[uid], cnt) for uid, cnt in rows]
    from .dragon_card import build_card_async
    path = await build_card_async(data)
    await dragon_cmd.finish(MessageSegment.image("file://" + path))


# ---------------- 词频推送状态 ----------------

def _words_groups() -> list[str]:
    data = load_json_state(WORDS_STATE, _state_lock)
    # 旧格式 {"group_id": "xxx"} 自动迁移到多群格式
    if "groups" not in data and data.get("group_id"):
        data["groups"] = [str(data["group_id"])]
        data.pop("group_id", None)
        save_json_state(WORDS_STATE, data, _state_lock)
    return [str(g) for g in (data.get("groups") or []) if str(g)]


async def _add_words_group(gid: str) -> None:
    # 整个 load→修改→落盘 在工作线程同一把锁内完成：旧写法锁内改、锁外存，
    # 两个群同时开关时后写覆盖先写、静默丢失配置；fsync 也移出事件循环
    def _rmw() -> None:
        with _state_lock:
            data = load_json_state(WORDS_STATE, _state_lock)
            if data.get("group_id") and "groups" not in data:  # 旧格式迁移
                data["groups"] = [str(data["group_id"])]
                data.pop("group_id", None)
            groups = {str(g) for g in (data.get("groups") or [])}
            groups.add(gid)
            data["groups"] = sorted(groups)
            save_json_state(WORDS_STATE, data, _state_lock)
    await asyncio.to_thread(_rmw)


async def _remove_words_group(gid: str) -> None:
    def _rmw() -> None:
        with _state_lock:
            data = load_json_state(WORDS_STATE, _state_lock)
            if not data:
                return
            groups = {str(g) for g in (data.get("groups") or [])}
            groups.discard(gid)
            data["groups"] = sorted(groups)
            save_json_state(WORDS_STATE, data, _state_lock)
    await asyncio.to_thread(_rmw)


@words_on_cmd.handle()
async def words_on(event: GroupMessageEvent):
    if not is_owner(event):
        await words_on_cmd.finish("❌ 你没有权限使用此功能")
    await _add_words_group(str(event.group_id))
    await words_on_cmd.finish("✅ 本群已开启每日词云推送\n每天凌晨自动发送前一天的热词词云到此群")


@words_off_cmd.handle()
async def words_off(event: GroupMessageEvent):
    if not is_owner(event):
        await words_off_cmd.finish("❌ 你没有权限使用此功能")
    await _remove_words_group(str(event.group_id))
    await words_off_cmd.finish("✅ 本群已关闭每日词云推送")


@words_status_cmd.handle()
async def words_status(event: GroupMessageEvent):
    if not is_owner(event):
        await words_status_cmd.finish("❌ 你没有权限使用此功能")
    groups = _words_groups()
    if groups:
        await words_status_cmd.finish(f"📊 每日词云推送已开启于 {len(groups)} 个群（每天凌晨发送）：\n{'、'.join(groups)}")
    await words_status_cmd.finish("📊 每日词云推送：未开启")


@scheduler.scheduled_job("cron", hour=0, minute=2, id="daily_words", timezone="Asia/Shanghai")
async def daily_words_job():
    groups = _words_groups()
    if not groups:
        return
    # 写路径有 0.5 秒批量 flush 窗口，先等队列清空再统计，避免漏掉临界消息；
    # 30 秒超时兜底防止数据库异常时任务卡死
    await wait_writes_drained(30)
    try:
        bot = get_bot()
    except Exception:
        _logger.exception("获取 bot 失败")
        return
    for gid in groups:
        try:
            path = await _build_word_image(int(gid), 40)
            if not path:
                continue
            await bot.send_group_msg(group_id=int(gid), message=MessageSegment.image("file://" + path))
        except Exception:
            _logger.exception("每日词云推送到群 %s 失败", gid)


# ---------------- 词频 ----------------

@words_cmd.handle()
async def words(event: GroupMessageEvent, arg: Message = CommandArg()):
    if not is_owner(event):
        await words_cmd.finish("❌ 你没有权限使用此功能")
    try:
        n = max(1, min(int(arg.extract_plain_text().strip() or 40), 60))
    except ValueError:
        n = 20
    try:
        path = await _build_word_image(event.group_id, n)
    except Exception:
        _logger.exception("词云统计查询失败")
        await words_cmd.finish(MessageSegment.text("统计服务暂时不可用，请稍后再试"))
    if not path:
        await words_cmd.finish("前一天还没有可统计的文字内容～")
    await words_cmd.finish(MessageSegment.image("file://" + path))
