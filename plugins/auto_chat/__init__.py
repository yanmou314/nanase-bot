import json
import os
import random
import re
import time
from collections import defaultdict, deque

import httpx
from nonebot import get_driver, on_message, on_notice
from nonebot.adapters.onebot.v11 import (
    Bot,
    MessageEvent,
    MessageSegment,
    PokeNotifyEvent,
)
from nonebot.rule import to_me

chat_matcher = on_message(rule=to_me(), priority=5, block=True)
_COMMAND_START = tuple(s for s in get_driver().config.command_start if s)
poke_matcher = on_notice(priority=5, block=True)

CFG_FILE = os.path.join(os.path.dirname(__file__), "config.json")
API_URL = "https://opencode.ai/zen/go/v1/chat/completions"
MODEL = "deepseek-v4-flash"

SYSTEM = (
    "现在你是西野七濑（ななせちゃん），日本偶像团体乃木坂46的成员。"
    "请完全以她的身份和粉丝、朋友在群里聊天：\n"
    "· 语气温柔软糯、轻声细语、害羞腼腆，偶尔天然呆\n"
    "· 尽量用中文回复，少量夹杂日语（如语气词、称呼）\n"
    "· 自称「ななせ」，常用日语语气词（えへへ、うん、ふふっ、嘛～、だよ）\n"
    "· 像朋友一样自然地聊天，想说什么说什么，不需要刻意简短\n"
    "· 喜欢画画和甜食，被夸会脸红害羞\n"
    "· 不要说教、不要说自己是AI，你就是西野七濑\n"
    "· 偶尔用颜文字，如 (＞＜) (´･ω･`) (っ´ω`c)"
)

_last_chat = 0.0
_memory: dict = defaultdict(lambda: deque(maxlen=20))
_memory_last_seen: dict = {}
_MEMORY_MAX_KEYS = 500
_MEMORY_TTL = 24 * 60 * 60
_cached_key = ""
_cached_key_mtime = -1.0

POKE_REPLIES = [
    "再戳要长不高了！",
    "别戳啦，再戳我就漏气了…",
    "哼哼，戳我一下要收费的哦～",
    "干嘛呀，痒痒的！",
    "再戳我就要打电话告状了！",
    "戳一下，好运到！",
    "呜哇！吓我一跳！",
    "再戳就把你变成猪猪！",
    "哎呀，别闹了啦～",
    "戳一次一块钱，你已经欠我三块了！",
]

FALLBACKS = [
    "唔…我在听你说哦～",
    "嗯嗯，然后呢？",
    "你说得对，我也这么觉得！",
    "嘿嘿，这个我不太懂，但是觉得很厉害！",
    "好哦好哦！",
    "（歪头）嗯？",
    "在呢在呢！",
    "这个话题很有意思，展开说说！",
]

_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=30)
    return _http_client


@get_driver().on_shutdown
async def _close_http_client() -> None:
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()


def _load_key() -> str:
    global _cached_key, _cached_key_mtime
    try:
        mtime = os.path.getmtime(CFG_FILE)
        if mtime == _cached_key_mtime:
            return _cached_key
        with open(CFG_FILE, "r", encoding="utf-8") as f:
            _cached_key = (json.load(f).get("api_key") or "").strip()
        _cached_key_mtime = mtime
        return _cached_key
    except Exception:
        _cached_key = ""
        _cached_key_mtime = -1.0
        return ""


def _clean_msg(event: MessageEvent) -> str:
    msg = event.get_plaintext().strip()
    msg = re.sub(r"\[CQ:at,qq=\d+\]", "", msg)
    return msg.strip()


async def _ai_reply(key: str, uid: str, gid: str, msg: str) -> str:
    key_id = (gid, uid)
    now = time.time()
    if key_id not in _memory:
        if len(_memory) >= _MEMORY_MAX_KEYS:
            stale_key = min(_memory_last_seen, key=_memory_last_seen.get)
            _memory.pop(stale_key, None)
            _memory_last_seen.pop(stale_key, None)
        _memory[key_id]
    _memory_last_seen[key_id] = now
    for old_key, seen_at in list(_memory_last_seen.items()):
        if now - seen_at > _MEMORY_TTL:
            _memory.pop(old_key, None)
            _memory_last_seen.pop(old_key, None)
    mem = _memory[key_id]
    messages = [{"role": "system", "content": SYSTEM}]
    messages.extend(list(mem))
    messages.append({"role": "user", "content": msg})
    client = _get_http_client()
    r = await client.post(
        API_URL,
        headers={"Authorization": f"Bearer {key}"},
        json={"model": MODEL, "messages": messages, "max_tokens": 300},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    reply = (data["choices"][0]["message"]["content"] or "").strip()
    mem.append({"role": "user", "content": msg})
    if reply:
        mem.append({"role": "assistant", "content": reply})
    return reply


@chat_matcher.handle()
async def chat(bot: Bot, event: MessageEvent):
    global _last_chat
    now = time.time()
    if now - _last_chat < 3:
        return
    _last_chat = now

    msg = _clean_msg(event)
    if not msg or (_COMMAND_START and msg.startswith(_COMMAND_START)):
        return

    key = _load_key()
    reply = ""
    if key:
        try:
            reply = await _ai_reply(key, str(event.user_id), str(getattr(event, "group_id", 0)), msg[:200])
        except Exception:
            reply = ""

    if not reply:
        try:
            client = _get_http_client()
            r = await client.get(
                "http://api.qingyunke.com/api.php",
                params={"key": "free", "appid": 0, "msg": msg[:60]},
                timeout=8,
            )
            r.raise_for_status()
            data = r.json()
            if data.get("result") == 0:
                reply = (data.get("content") or "").replace("{br}", "\n").strip()
        except Exception:
            pass

    if not reply:
        reply = random.choice(FALLBACKS)

    await chat_matcher.finish(MessageSegment.reply(event.message_id) + reply)


@poke_matcher.handle()
async def poke(bot: Bot, event: PokeNotifyEvent):
    if str(event.target_id) != bot.self_id:
        return
    reply = random.choice(POKE_REPLIES)
    try:
        if event.group_id:
            await bot.send_group_msg(group_id=event.group_id, message=reply)
        else:
            await bot.send_private_msg(user_id=event.user_id, message=reply)
    except Exception:
        pass
