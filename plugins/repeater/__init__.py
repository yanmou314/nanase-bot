import asyncio
import json
import os
import time
from collections import deque

from nonebot import get_driver, on_message
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageSegment

rep_matcher = on_message(priority=50, block=False)

_track: dict = {}
_replied_ts: dict = {}
COOLDOWN = 300

STATE_FILE = os.path.join(os.path.dirname(__file__), "repeater_state.json")


def _load_state() -> None:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for gid, items in data.get("track", {}).items():
            deq: deque = deque(maxlen=3)
            for item in items:
                deq.append(tuple(item) if isinstance(item, list) else item)
            _track[int(gid)] = deq
        for gid, ts in data.get("replied_ts", {}).items():
            _replied_ts[int(gid)] = ts
    except Exception:
        pass


def _save_state(snapshot: dict | None = None) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(
                snapshot or {
                    "track": {str(g): list(d) for g, d in _track.items()},
                    "replied_ts": {str(g): t for g, t in _replied_ts.items()},
                },
                f,
                ensure_ascii=False,
            )
    except Exception:
        pass


_load_state()


@get_driver().on_shutdown
async def _save_on_shutdown():
    # 先在事件循环内做快照，再交给线程写盘，避免线程内迭代时字典被修改
    snapshot = {
        "track": {str(g): list(d) for g, d in _track.items()},
        "replied_ts": {str(g): t for g, t in _replied_ts.items()},
    }
    await asyncio.to_thread(_save_state, snapshot)


def _prune() -> None:
    """清理 7 天以上无活动的群记录，防止字典无限增长。"""
    cutoff = time.time() - 7 * 86400
    for gid in list(_track):
        if _replied_ts.get(gid, 0) < cutoff:
            _track.pop(gid, None)
            _replied_ts.pop(gid, None)


def _fingerprint(event: GroupMessageEvent):
    segs = list(event.message)
    text = event.get_plaintext().strip()
    if text and not text.startswith(".") and all(s.type == "text" for s in segs):
        return ("t", text)
    if len(segs) == 1 and segs[0].type == "image":
        file_id = segs[0].data.get("file") or segs[0].data.get("url") or ""
        if file_id:
            return ("i", file_id)
    return None


@rep_matcher.handle()
async def repeater(bot: Bot, event: GroupMessageEvent):
    if len(_track) > 2000:
        _prune()
    gid = event.group_id
    fp = _fingerprint(event)

    deq = _track.setdefault(gid, deque(maxlen=3))
    deq.append(fp)
    if len(deq) < 3 or not (deq[0] == deq[1] == deq[2]):
        return
    if fp is None:
        return

    if time.time() - _replied_ts.get(gid, 0) < COOLDOWN:
        return

    _replied_ts[gid] = time.time()
    deq.clear()
    try:
        if fp[0] == "t":
            await bot.send_group_msg(group_id=gid, message=fp[1])
        else:
            await bot.send_group_msg(group_id=gid, message=MessageSegment.image(fp[1]))
    except Exception:
        pass
