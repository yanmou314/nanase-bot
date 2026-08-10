from nonebot import on_notice
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import GroupDecreaseNoticeEvent
import random

leave_matcher = on_notice(priority=1, block=False)

MESSAGES = [
    "静静离开了我们，大家一定很想他",
    "一位朋友悄悄退群了，江湖再见",
    "有人退群了，祝他前程似锦",
    "少了一位群友，散伙饭看来是不用吃了",
]


async def _get_name(bot: Bot, user_id: int) -> str:
    try:
        info = await bot.get_stranger_info(user_id=user_id)
        return info.get("nickname") or str(user_id)
    except Exception:
        return str(user_id)


@leave_matcher.handle()
async def handle(bot: Bot, event: GroupDecreaseNoticeEvent):
    uid = event.user_id
    gid = event.group_id
    sub = event.sub_type
    name = await _get_name(bot, uid)

    if sub == "leave":
        msg = f"👋 {name}（{uid}）退群了\n{random.choice(MESSAGES)}"
    elif sub == "kick":
        op = "群管理员"
        try:
            info = await bot.get_group_member_info(group_id=gid, user_id=event.operator_id)
            op = info.get("card") or info.get("nickname") or str(event.operator_id)
        except Exception:
            pass
        msg = f"🔨 {name}（{uid}）被 {op} 移出了群"
    else:
        return

    try:
        await bot.send_group_msg(group_id=gid, message=msg)
    except Exception:
        pass
