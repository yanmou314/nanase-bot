import random
from datetime import datetime
from zoneinfo import ZoneInfo

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message, MessageEvent, MessageSegment

_SH = ZoneInfo("Asia/Shanghai")

luck_cmd = on_command(
    "rp",
    aliases={"运势", "抽签", "求签", "今日运势", "今日运气"},
    priority=5,
    block=True,
)

TIERS = [
    (95, 100, "上上签", "大吉大利，诸事顺遂！", "大吉", "今日运势极佳，适合表白、面试、买彩票！"),
    (80, 94, "上签", "心想事成，万事如意。", "中吉", "运势不错，事情都能顺利推进。"),
    (60, 79, "中上签", "小有波折，但结局圆满。", "小吉", "平稳的一天，保持好心态。"),
    (40, 59, "中签", "平平淡淡才是真，稳中求进。", "小凶", "注意保管财物，少熬夜。"),
    (20, 39, "中下签", "稍安勿躁，静待时机。", "中凶", "不宜冲动消费，谨防口舌。"),
    (1, 19, "下签", "否极泰来，黎明前的黑暗。", "大凶", "诸事小心，平安是福。"),
]


@luck_cmd.handle()
async def luck(event: MessageEvent):
    today = datetime.now(_SH).date()  # 运势按上海时区的日期重置，不受部署机时区影响
    rnd = random.Random(f"{event.user_id}-{today.isoformat()}")
    score = rnd.randint(1, 100)
    for lo, hi, sign, sign_desc, luck_level, luck_desc in TIERS:
        if lo <= score <= hi:
            break
    at = Message()
    if hasattr(event, "group_id"):
        at = Message(MessageSegment.at(event.user_id))
    await luck_cmd.finish(
        at + f"🔮 {today.month}月{today.day}日 运势\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🎯 幸运数字：{score}\n"
        f"🗒 签文：「{sign}」{sign_desc}\n"
        f"✨ 运势：{luck_level} · {luck_desc}"
    )
