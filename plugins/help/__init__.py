from nonebot import on_command
from nonebot.adapters.onebot.v11 import MessageEvent
from common import is_owner

help_cmd = on_command("hp", aliases={"帮助", "help", "菜单"}, priority=1, block=True)


TEXT = """🤖 机器人指令菜单
━━━━━━━━━━━━━━━━━━━━
🎲 娱乐
  .rp            · 抽签 + 今日运势（别名：抽签、运势、求签）

🎮 守望先锋（国服）
  .绑定 名字#数字  · 绑定你的战网ID（一次即可）
  .战报 [ID]      · 近期战绩图
  .段位 [ID]      · 段位历史图
  .强度 [ID]      · 强度分析图
  .总结 [ID] [日]  · 上分总结图（今日/昨日/本周）
  .我的ID         · 查看当前绑定
  .解绑           · 解除绑定

💬 AI 聊天
  @机器人 + 消息   · 和西野七濑 AI 聊天

⏳ 时间提醒
  .倒计时         · 查看下一个周末和节假日倒计时

━━━━━━━━━━━━━━━━━━━━
📌 指令均以 . 开头；绑定后 [ID] 可省略"""

OWNER_TEXT = """
🔔 管理功能（仅你可见）
📊 统计查询
  .龙王           · 今日发言最多的人
  .词云 [N]       · 今日热词（N=1~60，默认40）

📢 每日推送
  .新闻开启        · 开启每日新闻推送（每天 8:00）
  .新闻关闭        · 关闭每日新闻
  .新闻测试        · 立即发送新闻总结测试
  .新闻状态        · 查看新闻推送状态
  .词云开启        · 开启每日词云推送（每天 7:00）
  .词云关闭        · 关闭每日词云推送
  .词云状态        · 查看词云推送状态
  .倒计时开启      · 开启本群每天 17:00 倒计时推送
  .倒计时关闭      · 关闭本群倒计时推送
  .倒计时状态      · 查看已开启推送的群数量
  .倒计时测试      · 立即测试本群倒计时消息

👥 进群管理
  .自动通过 关键字  · 开启自动通过（附言匹配关键字即放行）
  .自动通过关闭    · 关闭自动通过
  .自动通过查看    · 查看当前关键字配置
  .自动通过数量    · 查看关键字数量"""


@help_cmd.handle()
async def handle(event: MessageEvent):
    text = TEXT
    if is_owner(event):
        text += OWNER_TEXT
    await help_cmd.finish(text)
