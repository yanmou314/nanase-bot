import asyncio
import hashlib
import json
import os
import re
import time
from collections import deque

from nonebot import get_driver, on_message
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageSegment

rep_matcher = on_message(priority=50, block=False)

_track: dict = {}
_replied_ts: dict = {}
_replied_fp: dict = {}  # 每个群最近一次已复读的指纹；同一串连续复读只触发一次

STATE_FILE = os.path.join(os.path.dirname(__file__), "repeater_state.json")


def _text_hash(text: str) -> str:
    """文本指纹只存哈希，不落盘原文（隐私）。"""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _normalize_item(item) -> tuple:
    """状态条目归一化为 3 元组 (kind, fingerprint, payload)。"""
    item = tuple(item) if isinstance(item, list) else item
    if len(item) == 2:
        kind, val = item
        if kind == "t":
            # 旧格式存的是原文 → 迁移为哈希；新格式已是 40 位 hex 哈希
            if isinstance(val, str) and re.fullmatch(r"[0-9a-f]{40}", val):
                return ("t", val, "")
            return ("t", _text_hash(str(val)), "")
        return (kind, val, val)  # 图片指纹（QQ 文件哈希，非消息内容）
    return item


def _load_state() -> None:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for gid, items in data.get("track", {}).items():
            deq: deque = deque(maxlen=3)
            for item in items:
                deq.append(_normalize_item(item))
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
                    # 文本指纹只持久化哈希部分，payload（原文）仅存内存
                    "track": {str(g): [list(i[:2]) for i in d] for g, d in _track.items()},
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
        "track": {str(g): [list(i[:2]) for i in d] for g, d in _track.items()},
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
            _replied_fp.pop(gid, None)


def _fingerprint(event: GroupMessageEvent):
    segs = list(event.message)
    text = event.get_plaintext().strip()
    if text and not text.startswith(".") and all(s.type == "text" for s in segs):
        return ("t", _text_hash(text), text)  # 哈希用于比对，原文仅存内存供复读发送
    if len(segs) == 1 and segs[0].type == "image":
        file_id = segs[0].data.get("file") or segs[0].data.get("url") or ""
        if file_id:
            return ("i", file_id, file_id)
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
        _replied_fp.pop(gid, None)  # 复读链被打断（出现了不同消息），重置已复读标记
        return
    if fp is None:
        return
    if _replied_fp.get(gid) == fp:
        return  # 同一串连续复读只触发一次

    _replied_fp[gid] = fp
    _replied_ts[gid] = time.time()
    try:
        if fp[0] == "t":
            # MessageSegment.text 包裹：用户输入的字面 [CQ:...] 不会被解析为真实 CQ 码
            await bot.send_group_msg(group_id=gid, message=MessageSegment.text(fp[2]))
        else:
            await bot.send_group_msg(group_id=gid, message=MessageSegment.image(fp[2]))
    except Exception:
        pass
