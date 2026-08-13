"""关键词触发回复插件：群内出现配置的关键词时自动回复，关键词仅 owner 可增删。"""
import json
import os
import time

from nonebot import get_driver, on_command, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent
from common import is_owner, save_json_state

trigger = on_message(priority=10, block=False)

add_cmd = on_command("add", aliases={"关键词添加"}, priority=5, block=True)
del_cmd = on_command("关键词删除", priority=5, block=True)
list_cmd = on_command("关键词列表", aliases={"关键词查看", "关键词"}, priority=5, block=True)

_COMMAND_START = tuple(get_driver().config.command_start)

CFG_FILE = os.path.join(os.path.dirname(__file__), "keyword_config.json")
DEFAULT_REPLY_PREFIX = "不可以"
GROUP_COOLDOWN = 60

_config = {"keywords": {}}  # keyword -> reply
_replied_ts: dict = {}  # group_id -> last reply timestamp


def _load_config() -> None:
    global _config
    try:
        with open(CFG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        keywords = data.get("keywords", {})
        if isinstance(keywords, dict):
            _config["keywords"] = {str(k): str(v) for k, v in keywords.items()}
    except Exception:
        _config = {"keywords": {}}


def _save_config() -> None:
    try:
        save_json_state(CFG_FILE, _config)
    except Exception:
        pass


_load_config()


@trigger.handle()
async def keyword_reply(bot: Bot, event: GroupMessageEvent):
    text = event.get_plaintext().strip()
    if not text or text.startswith(_COMMAND_START):
        return
    keywords = _config["keywords"]
    if not keywords:
        return
    hit = None
    for kw, reply in keywords.items():
        if kw and kw in text:
            hit = (kw, reply)
            break
    if hit is None:
        return

    gid = event.group_id
    now = time.time()
    if now - _replied_ts.get(gid, 0) < GROUP_COOLDOWN:
        return
    _replied_ts[gid] = now
    try:
        await bot.send_group_msg(group_id=gid, message=hit[1])
    except Exception:
        pass


@add_cmd.handle()
async def add_keyword(event: MessageEvent):
    if not is_owner(event):
        return
    arg = event.get_plaintext().strip()
    if not arg:
        await add_cmd.finish("用法：.add 关键词 [回复内容]")
    parts = arg.split(maxsplit=1)
    kw = parts[0].strip()
    if not kw:
        await add_cmd.finish("关键词不能为空")
    reply = parts[1].strip() if len(parts) > 1 else DEFAULT_REPLY_PREFIX + kw
    existed = kw in _config["keywords"]
    _config["keywords"][kw] = reply
    _save_config()
    if existed:
        await add_cmd.finish(f"关键词「{kw}」已更新，触发回复：{reply}")
    await add_cmd.finish(f"已添加关键词「{kw}」，触发回复：{reply}")


@del_cmd.handle()
async def del_keyword(event: MessageEvent):
    if not is_owner(event):
        return
    kw = event.get_plaintext().strip()
    if not kw:
        await del_cmd.finish("用法：.关键词删除 关键词")
    if kw not in _config["keywords"]:
        await del_cmd.finish(f"关键词「{kw}」不存在")
    _config["keywords"].pop(kw)
    _save_config()
    await del_cmd.finish(f"已删除关键词「{kw}」")


@list_cmd.handle()
async def list_keywords(event: MessageEvent):
    if not is_owner(event):
        return
    keywords = _config["keywords"]
    if not keywords:
        await list_cmd.finish("当前没有任何关键词")
    lines = [f"{kw} -> {reply}" for kw, reply in keywords.items()]
    await list_cmd.finish("当前关键词列表：\n" + "\n".join(lines))
