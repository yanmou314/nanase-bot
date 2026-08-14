"""随机插话插件：开启后机器人围观群聊，按概率以人设自然插话。开关仅 owner 可控，按群生效。"""
import os
import random
import time
from collections import deque

from nonebot import get_driver, on_command, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent

from common import is_owner, load_json_state, save_json_state

watcher = on_message(priority=11, block=False)

on_cmd = on_command("插话开启", aliases={"插话on"}, priority=5, block=True)
off_cmd = on_command("插话关闭", aliases={"插话off"}, priority=5, block=True)
status_cmd = on_command("插话状态", priority=5, block=True)
prob_cmd = on_command("插话概率", priority=5, block=True)

_COMMAND_START = tuple(s for s in get_driver().config.command_start if s)

STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")

DEFAULT_PROBABILITY = 0.02  # 每条消息 2% 概率插话
DEFAULT_MIN_INTERVAL = 600  # 同群两次插话最小间隔（秒）
MIN_BUFFER = 5  # 缓冲至少 5 条消息才考虑插话
BUFFER_SIZE = 20  # 每群保留最近 20 条消息作为上下文
MAX_GROUPS = 200  # 最多为多少个群维护缓冲，防内存增长

_state = {
    "enabled_groups": [],
    "probability": DEFAULT_PROBABILITY,
    "min_interval": DEFAULT_MIN_INTERVAL,
}

_buffers: dict[int, deque] = {}  # group_id -> 最近消息文本
_last_interject: dict[int, float] = {}  # group_id -> 上次插话时间戳

FALLBACK_LINES = [
    "えっと…ななせ一直在这里看你们聊天哦…（小声）",
    "ふふっ…这个话题好有意思…",
    "そうそう…ななせ也这么觉得…",
    "（偷偷冒头）えへへ…聊什么呢…？",
    "唔…ななせ也在听哦…然后呢然后呢？",
    "诶…那个…ななせ也想插一句…（小声）",
    "（探头）大家聊得好开心…",
    "ん？…なんか…很热闹的样子…えへへ",
]

EXTRA_PROMPT = (
    "\n\n现在你正在围观一个QQ群的聊天，下面是群里最近的聊天记录（格式为「昵称: 内容」）。"
    "请你以人设的身份自然地插一两句话，就像随手参与话题那样，可以回应某个人说的话，也可以顺着话题接梗。"
    "要求：非常简短（一两句以内），不要@任何人，不要刷屏式提问，不要重复别人刚说过的话。"
    "如果聊天记录里没有合适的话题可以参与，就只回复 [SKIP] 这四个字符，不要回复其他任何内容。"
)


def _load_state() -> None:
    global _state
    data = load_json_state(STATE_FILE)
    if not data:
        return
    groups = data.get("enabled_groups", [])
    _state["enabled_groups"] = [int(g) for g in groups if str(g).isdigit()]
    try:
        p = float(data.get("probability", DEFAULT_PROBABILITY))
        _state["probability"] = min(max(p, 0.01), 0.2)
    except (TypeError, ValueError):
        pass
    try:
        _state["min_interval"] = int(data.get("min_interval", DEFAULT_MIN_INTERVAL))
    except (TypeError, ValueError):
        pass


def _save_state() -> None:
    try:
        save_json_state(STATE_FILE, _state)
    except Exception:
        pass


_load_state()

_enabled_set: set[int] = set(_state["enabled_groups"])


def _is_enabled(gid: int) -> bool:
    return gid in _enabled_set


def _record(gid: int, sender: str, text: str) -> None:
    buf = _buffers.get(gid)
    if buf is None:
        if len(_buffers) >= MAX_GROUPS:
            stale = min(_buffers, key=lambda g: _last_interject.get(g, 0))
            _buffers.pop(stale, None)
        buf = _buffers[gid] = deque(maxlen=BUFFER_SIZE)
    buf.append(f"{sender}: {text}")


def _auto_chat_mod():
    """延迟导入 auto_chat 插件，复用人设、API key、http 客户端与并发信号量。"""
    import importlib

    for name in ("plugins.auto_chat", "auto_chat"):
        try:
            return importlib.import_module(name)
        except Exception:
            continue
    return None


async def _generate_reply(gid: int) -> str:
    """基于群聊上下文生成插话内容，失败返回空串由调用方退回语料。"""
    mod = _auto_chat_mod()
    if mod is None:
        return ""
    key = mod._load_key()
    if not key:
        return ""
    transcript = "\n".join(_buffers.get(gid, ()))
    if not transcript:
        return ""
    messages = [
        {"role": "system", "content": mod.SYSTEM + EXTRA_PROMPT},
        {"role": "user", "content": f"最近的群聊记录：\n{transcript}"},
    ]
    client = mod._get_http_client()
    async with mod._AI_SEM:
        r = await client.post(
            mod.API_URL,
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": mod.MODEL,
                "messages": messages,
                "max_tokens": 120,
                "thinking": {"type": "disabled"},
            },
            timeout=30,
        )
    r.raise_for_status()
    data = r.json()
    reply = (data["choices"][0]["message"]["content"] or "").strip()
    if not reply or "[SKIP]" in reply:
        return ""
    # 去掉 AI 偶尔自带的引号包裹
    if len(reply) >= 2 and reply[0] == reply[-1] and reply[0] in "\"'「」『":
        reply = reply[1:-1].strip()
    return reply[:200]


@watcher.handle()
async def watch(bot: Bot, event: GroupMessageEvent):
    gid = event.group_id
    text = event.get_plaintext().strip()
    if not text or (_COMMAND_START and text.startswith(_COMMAND_START)):
        return
    if not _is_enabled(gid):
        return

    sender = event.sender.card or event.sender.nickname or str(event.user_id)
    _record(gid, sender, text[:100])

    buf = _buffers.get(gid)
    if buf is None or len(buf) < MIN_BUFFER:
        return
    now = time.time()
    if now - _last_interject.get(gid, 0) < _state["min_interval"]:
        return
    if random.random() >= _state["probability"]:
        return

    _last_interject[gid] = now
    reply = ""
    try:
        reply = await _generate_reply(gid)
    except Exception:
        reply = ""
    if not reply:
        if random.random() < 0.5:  # AI 不可用时也只按半概率退回语料，保持克制
            _last_interject[gid] = now - _state["min_interval"]  # 本次不算冷却
            return
        reply = random.choice(FALLBACK_LINES)
    try:
        await bot.send_group_msg(group_id=gid, message=reply)
    except Exception:
        pass


@on_cmd.handle()
async def enable(event: GroupMessageEvent):
    if not is_owner(event):
        return
    gid = event.group_id
    if gid in _enabled_set:
        await on_cmd.finish("本群随机插话已经是开启状态啦")
    _enabled_set.add(gid)
    if gid not in _state["enabled_groups"]:
        _state["enabled_groups"].append(gid)
    _save_state()
    await on_cmd.finish(
        f"已开启本群随机插话：每条消息 {_state['probability']:.0%} 概率插话，"
        f"同群冷却 {_state['min_interval'] // 60} 分钟"
    )


@off_cmd.handle()
async def disable(event: GroupMessageEvent):
    if not is_owner(event):
        return
    gid = event.group_id
    if gid not in _enabled_set:
        await off_cmd.finish("本群随机插话本来就是关闭的哦")
    _enabled_set.discard(gid)
    if gid in _state["enabled_groups"]:
        _state["enabled_groups"].remove(gid)
    _save_state()
    _buffers.pop(gid, None)
    await off_cmd.finish("已关闭本群随机插话")


@status_cmd.handle()
async def status(event: GroupMessageEvent):
    if not is_owner(event):
        return
    gid = event.group_id
    enabled = "开启 ✅" if _is_enabled(gid) else "关闭 ❌"
    last = _last_interject.get(gid)
    if last is None:
        last_desc = "还没有插过话"
    else:
        mins = int((time.time() - last) / 60)
        last_desc = f"{mins} 分钟前" if mins > 0 else "刚刚"
    buf_len = len(_buffers.get(gid, ()))
    await status_cmd.finish(
        f"本群随机插话状态：{enabled}\n"
        f"触发概率：{_state['probability']:.0%}（每条消息）\n"
        f"同群冷却：{_state['min_interval'] // 60} 分钟\n"
        f"上下文缓冲：{buf_len}/{BUFFER_SIZE} 条\n"
        f"上次插话：{last_desc}\n"
        f"已开启群数：{len(_enabled_set)}"
    )


@prob_cmd.handle()
async def set_prob(event: GroupMessageEvent):
    if not is_owner(event):
        return
    arg = event.get_plaintext().strip().rstrip("%％")
    try:
        pct = float(arg)
    except ValueError:
        await prob_cmd.finish(f"用法：.插话概率 5 （表示 5%），当前 {_state['probability']:.0%}")
    if not 1 <= pct <= 20:
        await prob_cmd.finish("概率需要在 1 ~ 20 之间（百分比）")
    _state["probability"] = pct / 100
    _save_state()
    await prob_cmd.finish(f"已将插话概率调整为 {_state['probability']:.0%}")
