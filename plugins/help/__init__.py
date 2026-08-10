from nonebot import on_command
from nonebot.adapters.onebot.v11 import MessageEvent
from common import is_owner

help_cmd = on_command("帮助", aliases={"help", "菜单"}, priority=1, block=True)


TEXT = """🤖 机器人指令菜单
━━━━━━━━━━━━━━━━━━━━
🎲 娱乐
  .rp           · 抽签 + 今日运势

🎮 守望先锋（国服）
  .绑定 名字#数字 · 绑定你的战网ID（一次即可）
  .战报 [ID]     · 近期战绩图
  .段位 [ID]     · 段位历史图
  .强度 [ID]     · 强度分析图
  .总结 [ID] [今日|昨日|本周] · 上分总结图
  .我的ID        · 查看当前绑定
  .解绑          · 解除绑定

📊 群聊统计
  .龙王         · 今日发言最多的人
  .词频 [N]     · 今日热词

━━━━━━━━━━━━━━━━━━━━
📌 所有指令均以 . 开头；绑定后 [ID] 可省略"""

OWNER_TEXT = """
🔔 管理功能（仅你可见）
  .新闻开启      · 开启每日新闻推送（每天 8:00）
  .新闻关闭      · 关闭每日新闻
  .新闻测试      · 立即发送新闻总结测试
  .新闻状态      · 查看新闻推送状态
  .词频开启      · 开启每日词频推送（每天 8:05）
  .词频关闭      · 关闭每日词频推送
  .词频状态      · 查看词频推送状态
  .服务器        · 服务器状态卡片"""


@help_cmd.handle()
async def handle(event: MessageEvent):
    text = TEXT
    if is_owner(event):
        text += OWNER_TEXT
    await help_cmd.finish(text)
