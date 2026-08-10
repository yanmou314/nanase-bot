import asyncio
import json
import math
import os
import random
import re
import sqlite3
import threading
import time
from collections import Counter, defaultdict, deque
from datetime import date, timedelta

from PIL import Image, ImageDraw, ImageFont
from nonebot import get_bot, on_command, on_message
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment
from nonebot.params import CommandArg
from nonebot_plugin_apscheduler import scheduler

from common import (
    OWNER,
    cleanup_cache,
    is_owner,
    save_image,
)

DB = os.path.join(os.path.dirname(__file__), "stats.db")
WORD_CACHE = os.path.join(os.path.dirname(__file__), "cache")
WORDS_STATE = os.path.join(os.path.dirname(__file__), "words_state.json")
_lock = asyncio.Lock()
_state_lock = threading.Lock()
_nick_cache: dict = {}
_nick_ts: dict = {}
NICK_TTL = 300

_conn: sqlite3.Connection | None = None


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.execute(
            """CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER, user_id INTEGER,
                msg_type TEXT, day TEXT, hour INTEGER, text TEXT
            )"""
        )
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_gday ON messages(group_id, day)")
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_guday ON messages(group_id, user_id, day)")
        _conn.commit()
    return _conn


def _exec(sql: str, params: tuple = ()) -> list:
    return _db().execute(sql, params).fetchall()


def _write(group_id: int, user_id: int, msg_type: str, text: str) -> None:
    _db().execute(
        "INSERT INTO messages(group_id, user_id, msg_type, day, hour, text) VALUES(?,?,?,?,?,?)",
        (group_id, user_id, msg_type, date.today().isoformat(), time.localtime().tm_hour, text[:200] or ""),
    )
    _db().commit()


record_matcher = on_message(priority=1, block=False)
dragon_cmd = on_command("龙王", priority=5, block=True)
words_cmd = on_command("词频", aliases={"热词"}, priority=5, block=True)

STOPWORDS = set("的了是在我有和你这不那啊呢吧吗哦嗯就都要也会没很他说她我们他们自己一个没什么可以"
                "真的还是因为所以但是然后现在今天明天昨天知道觉得应该可能如果这样那样这个那个什么为什么怎么")

FONT = "/usr/share/fonts/custom/ZCOOLKuaiLe-Regular.ttf"
PALETTE = ["#FF6B6B", "#FCA311", "#FFC93C", "#4ECDC4", "#45B7D1", "#96CEB4",
           "#F15BB5", "#9B5DE5", "#00BBF9", "#FEE440", "#FF8C42", "#5FD068"]


def _overlap(a, b) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _wordcloud(counter: Counter, n: int, msg_count: int) -> str:
    W, H = 960, 640
    img = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(img, "RGBA")
    top, bottom = (255, 241, 248), (232, 245, 255)
    for y in range(H):
        t = y / H
        draw.line([(0, y), (W, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    rnd = random.Random(42)
    pastel = ["#FFD3E0", "#C9F0FF", "#FFF3C4", "#D8F3DC", "#E7D9FF", "#FFE8D6"]
    for _ in range(70):
        x, y, r = rnd.randint(0, W), rnd.randint(0, H), rnd.randint(6, 26)
        draw.ellipse([x - r, y - r, x + r, y + r], fill=pastel[rnd.randint(0, 5)] + f"{rnd.randint(35, 90):02X}")

    words = counter.most_common(n)
    maxc = max(c for _, c in words) if words else 1
    entries = sorted([(w, int(28 + 66 * (c / maxc) ** 0.6), c) for w, c in words], key=lambda e: -e[1])
    placed = []
    cx, cy = W / 2, H / 2
    for w, size, c in entries:
        font = ImageFont.truetype(FONT, size)
        bb = font.getbbox(w)
        ww, hh = bb[2] - bb[0], bb[3] - bb[1]
        color = rnd.choice(PALETTE)
        for step in range(5000):
            t = step * 0.04
            r = 6 + t * 3.0
            x = cx + r * math.cos(t * 2.7)
            y = cy + r * math.sin(t * 2.7) * 0.72
            box = (x - ww / 2 - 3, y - hh / 2 - 3, x + ww / 2 + 3, y + hh / 2 + 3)
            if box[0] < 8 or box[2] > W - 8 or box[1] < 8 or box[3] > H - 8:
                continue
            if any(_overlap(box, p[1]) for p in placed):
                continue
            placed.append((w, box, font, color))
            break
    for w, box, font, color in placed:
        draw.text(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), w, font=font, fill=color, anchor="mm")
    return save_image(img.tobytes() if False else b"", "", "words", WORD_CACHE) if False else \
        save_image_png(img, WORD_CACHE)


def save_image_png(img, cache_dir: str) -> str:
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"words_{int(time.time() * 1000)}.png")
    img.save(path, "PNG")
    return path


@record_matcher.handle()
async def record(event: GroupMessageEvent):
    if not hasattr(event, "group_id"):
        return
    mtype = "text"
    for seg in event.message:
        if seg.type != "text":
            mtype = seg.type
            break
    await asyncio.to_thread(_write, event.group_id, event.user_id, mtype, event.get_plaintext())


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


def _medal(i: int) -> str:
    return ["🥇", "🥈", "🥉"][i] if i < 3 else f"{i + 1}."


@dragon_cmd.handle()
async def dragon(bot: Bot, event: GroupMessageEvent):
    if not is_owner(event):
        await dragon_cmd.finish("❌ 你没有权限使用此功能")
    today = date.today().isoformat()
    rows = _exec("SELECT user_id, COUNT(*) c FROM messages WHERE group_id=? AND day=? GROUP BY user_id ORDER BY c DESC LIMIT 3",
                 (event.group_id, today))
    if not rows:
        await dragon_cmd.finish("今天还没有人发言哦～")
    names = {r[0]: await _get_name(bot, event.group_id, r[0]) for r in rows}
    lines = [f"👑 今日龙王 · {date.today().month}月{date.today().day}日"]
    for i, (uid, cnt) in enumerate(rows):
        lines.append(f"{'👑' if i == 0 else _medal(i)} {names[uid]} · {cnt} 条")
    await dragon_cmd.finish("\n".join(lines))


def _build_word_image(group_id: int, day: str, n: int) -> str | None:
    rows = _exec("SELECT text FROM messages WHERE group_id=? AND day=? AND text!='''",
                 (group_id, day))
    counter: Counter = Counter()
    for (text,) in rows:
        for seg in re.findall(r"[\u4e00-\u9fff]{2,}", text):
            if seg not in STOPWORDS:
                counter[seg] += 1
    if not counter:
        return None
    cleanup_cache(WORD_CACHE)
    return _wordcloud(counter, min(n, len(counter)), len(rows))


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


words_on_cmd = on_command("词频开启", priority=5, block=True)
words_off_cmd = on_command("词频关闭", priority=5, block=True)
words_status_cmd = on_command("词频状态", priority=5, block=True)


@words_on_cmd.handle()
async def words_on(event: GroupMessageEvent):
    if not is_owner(event):
        await words_on_cmd.finish("❌ 你没有权限使用此功能")
    if not hasattr(event, "group_id"):
        await words_on_cmd.finish("请在有机器人的群里开启此功能")
    _set_words_group(str(event.group_id))
    await words_on_cmd.finish(f"✅ 每日词频推送已开启\n每天 8:00 自动发送昨日热词词云到此群（本群 {event.group_id}）")


@words_off_cmd.handle()
async def words_off(event: GroupMessageEvent):
    if not is_owner(event):
        await words_off_cmd.finish("❌ 你没有权限使用此功能")
    _clear_words_group()
    await words_off_cmd.finish("✅ 每日词频推送已关闭")


@words_status_cmd.handle()
async def words_status(event: GroupMessageEvent):
    if not is_owner(event):
        await words_status_cmd.finish("❌ 你没有权限使用此功能")
    gid = _words_group()
    if gid:
        await words_status_cmd.finish(f"📊 每日词频推送：已开启（群 {gid}，每天 8:00 发送）")
    await words_status_cmd.finish("📊 每日词频推送：未开启")


@scheduler.scheduled_job("cron", hour=8, minute=5, id="daily_words", timezone="Asia/Shanghai")
async def daily_words_job():
    gid = _words_group()
    if not gid:
        return
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    try:
        path = await asyncio.to_thread(_build_word_image, int(gid), yesterday, 20)
        if not path:
            return
        bot = get_bot()
        await bot.send_group_msg(group_id=int(gid), message=MessageSegment.image("file://" + path))
    except Exception:
        pass


@words_cmd.handle()
async def words(event: GroupMessageEvent, arg: Message = CommandArg()):
    if not is_owner(event):
        await words_cmd.finish("❌ 你没有权限使用此功能")
    try:
        n = max(1, min(int(arg.extract_plain_text().strip() or 20), 30))
    except ValueError:
        n = 20
    path = await asyncio.to_thread(_build_word_image, event.group_id, date.today().isoformat(), n)
    if not path:
        await words_cmd.finish("今天还没有可统计的文字内容～")
    await words_cmd.finish(MessageSegment.image("file://" + path))