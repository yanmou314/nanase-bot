import asyncio
import logging
import os
import re
import threading
from collections import Counter
from contextlib import aclosing
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import jieba

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

# 词级停用表：jieba 分词后按整词过滤；单字 token 一律不计，无需单字停用词
STOPWORDS = frozenset("""
我们 你们 他们 她们 自己 一个 一些 没有 什么 可以 就是 但是 然后 现在 今天 明天 昨天
知道 觉得 应该 可能 如果 这样 那样 还是 因为 所以 已经 不过 真的 直接 只是 而且
时候 地方 东西 时间 问题 有点 一点 不是 这个 那个 为什么 怎么 一下 起来 出来 大家
""".split())

jieba.setLogLevel(logging.WARNING)


@get_driver().on_startup
async def _warm_jieba():
    """启动时预热分词词典（约 1 秒），避免首次词云请求阻塞事件循环。"""
    try:
        await asyncio.to_thread(jieba.initialize)
    except Exception:
        _logger.warning("jieba 预热失败，将在首次使用时重试", exc_info=True)


def _count_into(counter: Counter, text: str) -> None:
    """jieba 分词统计中文词：仅保留 ≥2 字的纯中文词，过滤停用词。

    旧实现把 >8 字的连续中文按每 4 字切块，会把句子拦腰截断成无意义碎片。
    """
    for word in jieba.cut(text):
        w = word.strip()
        if len(w) < 2 or w in STOPWORDS:
            continue
        if not all("\u4e00" <= c <= "\u9fff" for c in w):
            continue
        counter[w] += 1


def _quote_worthy(s: str) -> bool:
    """判定一条消息是否可入选语录提取：长度适中（4~24 字）且非单字复读（如 哈哈哈哈、666666）。"""
    s = s.strip()
    return 4 <= len(s) <= 24 and len(set(s)) > 1


def _process_messages_batch(texts: list[str]) -> tuple[Counter, Counter, int]:
    """纯 CPU 批处理：对一批消息做 jieba 分词计数 + 短语提取。

    必须在工作线程执行（asyncio.to_thread），禁止在事件循环内直接调用。
    返回 (词频 Counter, 短语 Counter, 有效消息数)。
    """
    counter: Counter = Counter()
    phrase_counter: Counter = Counter()
    normal_message_count = 0
    for text in texts:
        if text.startswith(_COMMAND_START):
            continue
        normal_message_count += 1
        _count_into(counter, text)
        if _quote_worthy(text):
            p = _phrase_from_sentence(text, counter)
            if p:
                phrase_counter[p] += 1
    return counter, phrase_counter, normal_message_count


def _phrase_from_sentence(s: str, counter: Counter, max_len: int = 8) -> str:
    """从句子中按词边界截取一段 ≤max_len 字的短语（词云混排用，不整句上版）。

    先按标点（含逗号）切子句，再在单个子句内以全局词频最高的词为锚点向两侧扩词；
    跨子句拼接会得到"吃火锅然后看电影"这种语义不完整的串，禁止。
    """
    # 中文/英文标点均视为子句边界；表情、符号不是边界但会在 token 过滤时剔除
    clauses = [c.strip() for c in re.split(r"[，。！？；、,.!?;:：…\s]+", s.strip()) if c.strip()]

    def _score(w: str) -> int:
        if w in counter:
            return counter[w]
        return max((v for k, v in counter.items() if len(k) >= 2 and k in w), default=0)

    best = ""
    best_score = -1
    for clause in clauses:
        # 只保留纯中文 token：表情、标点、字母混进短语会因字体缺字渲染成方框
        words = [w for w in jieba.cut(clause) if w.strip() and all("\u4e00" <= c <= "\u9fff" for c in w)]
        if not words:
            continue
        if sum(len(w) for w in words) <= max_len:
            phrase = "".join(words)
        else:
            # 锚点：子句内全局词频最高的 ≥2 字词；没有则取中间词。
            # 计分对 jieba 复合词（如"吃火锅"）做子串匹配，否则全局词"火锅"匹配不上
            cands = [w for w in words if len(w) >= 2]

            if not cands:
                anchor_idx = len(words) // 2
            else:
                anchor_idx = max(range(len(words)),
                                 key=lambda i: _score(words[i]) if words[i] in cands else -1)
            lo = hi = anchor_idx
            total = len(words[anchor_idx])
            while total < max_len:
                left = len(words[lo - 1]) if lo > 0 else max_len + 1
                right = len(words[hi + 1]) if hi + 1 < len(words) else max_len + 1
                if min(left, right) > max_len - total:
                    break
                if left <= right:
                    lo -= 1
                    total += left
                else:
                    hi += 1
                    total += right
            phrase = "".join(words[lo:hi + 1])
        # 子句间取词频得分最高的一个短语
        score = max((_score(w) for w in jieba.cut(clause)
                     if len(w) >= 2 and w in counter), default=0)
        if score > best_score:
            best, best_score = phrase, score
    return best


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
    phrase_counter: Counter = Counter()
    normal_message_count = 0
    batch: list[str] = []
    BATCH_SIZE = 256  # 分批落线程，兼顾内存与 to_thread 开销

    async def _flush_batch() -> None:
        nonlocal counter, phrase_counter, normal_message_count, batch
        if not batch:
            return
        texts, batch = batch, []
        # jieba 分词是纯 CPU 重活，必须离开事件循环；批处理后并回全局计数
        c, pc, n = await asyncio.to_thread(_process_messages_batch, texts)
        counter.update(c)
        phrase_counter.update(pc)
        normal_message_count += n

    # 流式逐行消费：活跃大群的全天文本不再一次性载入内存；
    # aclosing 保证循环体异常时也能立即释放游标与池连接
    async with aclosing(iter_rows(sql, args)) as rows:
        async for (text,) in rows:
            batch.append(text)
            if len(batch) >= BATCH_SIZE:
                await _flush_batch()
    await _flush_batch()
    if not counter:
        return None
    # 被重复 ≥2 次的短语最多取 3 条，与常规词一起渲染
    phrases = [(q, c) for q, c in phrase_counter.most_common(3) if c >= 2]
    await asyncio.to_thread(cleanup_cache, WORD_CACHE)  # 同步磁盘扫描不阻塞事件循环
    from .wordcloud_card import _render as render_cloud
    # PIL 渲染经全局渲染信号量串行化，避免小机器上并发渲染打爆内存
    async with RENDER_SEM:
        return await asyncio.to_thread(
            render_cloud, counter, min(n, len(counter)), normal_message_count, phrases
        )


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


WORDS_PUSH_HOUR = 0
WORDS_PUSH_MINUTE = 2


_words_push_running = False


def _last_push_date() -> str:
    """读取最近一次每日词云推送日期（YYYY-MM-DD）；从未推送返回空串。"""
    data = load_json_state(WORDS_STATE, _state_lock)
    return str(data.get("last_push_date") or "")


def _mark_pushed(day: date) -> None:
    """记录当日词云已推送，用于防重复与启动补发判断。"""
    def _do() -> None:
        with _state_lock:
            data = load_json_state(WORDS_STATE, _state_lock)
            data["last_push_date"] = day.isoformat()
            save_json_state(WORDS_STATE, data, _state_lock)
    _do()


@scheduler.scheduled_job(
    "cron", hour=WORDS_PUSH_HOUR, minute=WORDS_PUSH_MINUTE, id="daily_words", timezone="Asia/Shanghai"
)
async def daily_words_job():
    global _words_push_running
    if _words_push_running:
        return
    if _last_push_date() >= _sh_today().isoformat():
        return  # 今日已推送（如启动补发已执行过），防重复
    groups = _words_groups()
    if not groups:
        return
    _words_push_running = True
    try:
        # 写路径有 0.5 秒批量 flush 窗口，先等队列清空再统计，避免漏掉临界消息；
        # 30 秒超时兜底防止数据库异常时任务卡死
        await wait_writes_drained(30)
        try:
            bot = get_bot()
        except Exception:
            _logger.exception("获取 bot 失败")
            return
        sent = 0
        for gid in groups:
            try:
                path = await _build_word_image(int(gid), 40)
                if not path:
                    continue
                await bot.send_group_msg(group_id=int(gid), message=MessageSegment.image("file://" + path))
                sent += 1
            except Exception:
                _logger.exception("每日词云推送到群 %s 失败", gid)
        if sent:
            _mark_pushed(_sh_today())  # 有群成功送达才记录，全失败保留补发机会
    finally:
        _words_push_running = False


# 启动补发：APScheduler 用内存 jobstore，进程重启后错过的当日词云静默丢失；
# bot 连上后检查「已过推送时刻且今日未推送」则立即补发一次（对齐 news 模式）。
_register_words_catchup = getattr(get_driver(), "on_bot_connect", get_driver().on_startup)


@_register_words_catchup
async def _daily_words_catchup(bot=None) -> None:
    now = datetime.now(_SH)
    if (now.hour, now.minute) < (WORDS_PUSH_HOUR, WORDS_PUSH_MINUTE):
        return  # 还没到当日推送时刻，交给定时任务
    if _last_push_date() >= now.date().isoformat():
        return  # 今日已推送，不重复
    await daily_words_job()


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
