import asyncio
import hashlib
import json
import logging
import os
import re
import time
from collections import deque

from nonebot import get_driver, on_message
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageSegment

from common import RELAY_GROUP_ID, load_json_state, save_json_state

_logger = logging.getLogger(__name__)

rep_matcher = on_message(priority=50, block=False)

_track: dict = {}
_replied_ts: dict = {}
_replied_fp: dict = {}  # 每个群最近一次已复读的指纹；同一串连续复读只触发一次
_last_msg_ts: dict = {}  # 每群最近一条消息时间戳（仅内存，作为 _prune 的活动依据）

_COMMAND_START = tuple(s for s in get_driver().config.command_start if s)

STATE_FILE = os.path.join(os.path.dirname(__file__), "repeater_state.json")

# 单条消息段即可复读的 QQ 内置表情/商城大表情类型
_REPEATABLE_SINGLE_TYPES = ("face", "mface", "bface")


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
        if kind == "f":
            # 表情 payload（消息段）无法从哈希恢复；重启后仅参与计数，不再复读
            return ("f", val, "")
        return (kind, val, val)  # 图片指纹（QQ 文件哈希，非消息内容）
    return item


def _load_state() -> None:
    # 文件级读取（缺失/损坏/不可读）统一走 common，含 corrupt/unreadable 备份防护
    data = load_json_state(STATE_FILE)
    try:
        for gid, items in data.get("track", {}).items():
            deq: deque = deque(maxlen=3)
            for item in items:
                deq.append(_normalize_item(item))
            _track[int(gid)] = deq
        for gid, ts in data.get("replied_ts", {}).items():
            _replied_ts[int(gid)] = ts
    except Exception:
        _logger.warning("repeater 状态加载失败: %s", STATE_FILE, exc_info=True)


def _save_state(snapshot: dict | None = None) -> None:
    try:
        # 统一走 common 的原子写（tmp + fsync + os.replace），只存哈希，indent=2 带来的体积可接受
        save_json_state(
            STATE_FILE,
            snapshot or {
                # 文本指纹只持久化哈希部分，payload（原文）仅存内存；
                # 不可复读消息的指纹为 None，只参与计数不落盘
                "track": {str(g): [list(i[:2]) for i in d if i] for g, d in _track.items()},
                "replied_ts": {str(g): t for g, t in _replied_ts.items()},
            },
        )
    except Exception:
        _logger.warning("repeater 状态写入失败: %s", STATE_FILE, exc_info=True)


_load_state()


@get_driver().on_shutdown
async def _save_on_shutdown():
    try:
        # 先在事件循环内做快照，再交给线程写盘，避免线程内迭代时字典被修改
        snapshot = {
            "track": {str(g): [list(i[:2]) for i in d if i] for g, d in _track.items()},
            "replied_ts": {str(g): t for g, t in _replied_ts.items()},
        }
        await asyncio.to_thread(_save_state, snapshot)
    except Exception:
        # 关机钩子绝不向上抛异常，避免拖垮 lifespan 导致状态全丢
        _logger.warning("repeater 关机落盘失败", exc_info=True)


def _prune() -> None:
    """清理 7 天以上无活动（既没新消息也没复读）的群记录，防止字典无限增长。"""
    cutoff = time.time() - 7 * 86400
    for gid in list(_track):
        # 以最近一条消息时间为主；仅从持久化恢复、本进程还没收到过消息的群退回按最后复读时间判断
        if max(_last_msg_ts.get(gid, 0), _replied_ts.get(gid, 0)) < cutoff:
            _track.pop(gid, None)
            _replied_ts.pop(gid, None)
            _replied_fp.pop(gid, None)
            _last_msg_ts.pop(gid, None)


def _fingerprint(event: GroupMessageEvent):
    segs = list(event.message)
    text = event.get_plaintext().strip()
    if text and not (_COMMAND_START and text.startswith(_COMMAND_START)) and all(s.type == "text" for s in segs):
        return ("t", _text_hash(text), text)  # 哈希用于比对，原文仅存内存供复读发送
    if len(segs) == 1 and segs[0].type == "image":
        # 商城表情（带 emoji_id 的 gif）以下发为 image 段，file 是内部引用（如 a3-xxx.gif），
        # 用它重发会失败；改按表情处理：指纹取 emoji_id，payload 保留原消息段供重发
        if segs[0].data.get("emoji_id"):
            key = json.dumps({k: segs[0].data.get(k) for k in ("emoji_id", "emoji_package_id", "key", "summary")}, sort_keys=True, ensure_ascii=False)
            return ("f", _text_hash(key), segs[0])
        file_id = segs[0].data.get("file") or segs[0].data.get("url") or ""
        # 只接受内部 file_id 或 http(s) CDN，拒绝 file:// 等任意引用（防 SSRF/本地读）
        if file_id and not file_id.startswith(("file://", "ftp://", "gopher://")):
            return ("i", file_id, file_id)
    if len(segs) == 1 and segs[0].type in _REPEATABLE_SINGLE_TYPES:
        # QQ 小黄脸（face）/表情商城大表情（mface）/VIP 表情（bface）：
        # 指纹取消息段数据；payload 存原消息段供复读（仅内存，落盘只存哈希）
        if not segs[0].data:
            return None
        key = json.dumps(segs[0].data, sort_keys=True, ensure_ascii=False)
        return ("f", _text_hash(key), segs[0])
    return None


@rep_matcher.handle()
async def repeater(bot: Bot, event: GroupMessageEvent):
    if len(_track) > 2000:
        _prune()
    gid = event.group_id
    if gid == RELAY_GROUP_ID:  # OW 任务中继群：保持静默，不复读
        return
    _last_msg_ts[gid] = time.time()  # 记录群活动时间（仅内存），供 _prune 判断
    fp = _fingerprint(event)

    deq = _track.setdefault(gid, deque(maxlen=3))
    deq.append(fp)
    # 比较只取前两元（类型+哈希）；None（@、表情、混合内容等不可复读消息）
    # 参与计数但会打断复读链，且比较前必须判空，否则 None[:2] 直接 TypeError
    if (
        len(deq) < 3
        or deq[0] is None
        or deq[1] is None
        or deq[2] is None
        or deq[0][:2] != deq[1][:2]
        or deq[1][:2] != deq[2][:2]
    ):
        _replied_fp.pop(gid, None)  # 复读链被打断（出现了不同消息），重置已复读标记
        return
    if _replied_fp.get(gid) == fp[:2]:
        return  # 同一串连续复读只触发一次（表情指纹的 payload 是消息段对象，须按前两元比较）

    _replied_fp[gid] = fp[:2]
    _replied_ts[gid] = time.time()
    try:
        if fp[0] == "t":
            # MessageSegment.text 包裹：用户输入的字面 [CQ:...] 不会被解析为真实 CQ 码
            await bot.send_group_msg(group_id=gid, message=MessageSegment.text(fp[2]))
        elif fp[0] == "i":
            # 图片 file_id 过期后发送失败常见，留痕避免插件静默失效无从察觉
            await bot.send_group_msg(group_id=gid, message=MessageSegment.image(fp[2]))
        elif isinstance(fp[2], MessageSegment):
            # QQ 表情/商城大表情：原样重发该消息段
            await bot.send_group_msg(group_id=gid, message=fp[2])
    except Exception:
        _logger.warning("复读发送失败（群 %s）", gid, exc_info=True)
