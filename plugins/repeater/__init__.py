import time
from collections import deque

from nonebot import on_message
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageSegment

rep_matcher = on_message(priority=50, block=False)

_track: dict = {}
_replied: dict = {}
_replied_ts: dict = {}
COOLDOWN = 60


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
    gid = event.group_id
    fp = _fingerprint(event)

    deq = _track.setdefault(gid, deque(maxlen=3))
    deq.append(fp)
    if len(deq) < 3 or not (deq[0] == deq[1] == deq[2]):
        return
    if fp is None:
        return

    if _replied.get(gid) == fp and time.time() - _replied_ts.get(gid, 0) < COOLDOWN:
        return

    _replied[gid] = fp
    _replied_ts[gid] = time.time()
    deq.clear()
    try:
        if fp[0] == "t":
            await bot.send_group_msg(group_id=gid, message=fp[1])
        else:
            await bot.send_group_msg(group_id=gid, message=MessageSegment.image(fp[1]))
    except Exception:
        pass
