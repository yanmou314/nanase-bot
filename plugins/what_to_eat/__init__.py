"""群里聊到“吃什么/吃啥”时，随机推荐一种食物。"""
from __future__ import annotations

import random
import re
import time

from nonebot import get_driver, on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent

eater = on_message(priority=30, block=False)

# “今天吃什么”“晚上吃啥”“中午到底吃什么啊”之类均命中；指令消息不触发被动回复
_KEYWORD_RE = re.compile(r"吃(?:什么|啥)")
_COMMAND_START = tuple(s for s in get_driver().config.command_start if s)

# 每群回复冷却，防止连续刷“吃什么”刷屏
COOLDOWN_SECONDS = 10
_last_reply: dict[int, float] = {}

FOODS: list[str] = [
    "火锅",
    "麻辣烫",
    "麻辣香锅",
    "冒菜",
    "串串香",
    "KFC 疯狂星期四（V我50）",
    "麦当劳",
    "塔斯汀中国汉堡",
    "必胜客披萨",
    "黄焖鸡米饭",
    "兰州拉面",
    "沙县小吃",
    "螺蛳粉",
    "过桥米线",
    "酸菜鱼",
    "烤肉",
    "烧烤",
    "炸鸡",
    "日式便当",
    "寿司",
    "饺子",
    "馄饨",
    "牛肉面",
    "煎饼果子",
    "肉夹馍",
    "凉皮",
    "麻辣拌",
    "轻食沙拉",
    "皮蛋瘦肉粥",
    "包子配豆浆",
    "泡面加蛋（穷鬼套餐）",
    "咖喱饭",
    "小龙虾",
    "意大利面",
    "石锅拌饭",
    "羊肉泡馍",
    "关东煮",
    "北京烤鸭",
    "热干面",
    "钵钵鸡",
    "蛋炒饭",
    "生煎包",
    "酸辣粉",
    "隆江猪脚饭",
    "干锅土豆片",
    "台式卤肉饭",
    "椰子鸡",
    "铁锅炖",
    "梅菜扣肉盖饭",
    "老北京鸡肉卷",
]


def hit(text: str) -> bool:
    """文本是否命中“吃什么”类提问（排除以指令前缀开头的消息）。"""
    t = text.strip()
    if not t or t.startswith(_COMMAND_START):
        return False
    return bool(_KEYWORD_RE.search(t))


def allow(group_id: int, now: float) -> bool:
    """该群是否已过回复冷却期。"""
    return now - _last_reply.get(group_id, 0.0) >= COOLDOWN_SECONDS


def pick() -> str:
    """随机选一种食物。"""
    return random.choice(FOODS)


@eater.handle()
async def recommend(event: GroupMessageEvent):
    if not isinstance(event, GroupMessageEvent):
        return
    if not hit(event.get_plaintext()):
        return
    now = time.monotonic()
    if not allow(event.group_id, now):
        return
    _last_reply[event.group_id] = now
    await eater.send(f"吃{pick()}")
