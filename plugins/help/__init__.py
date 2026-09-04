import asyncio
import glob
import hashlib
import logging
import os
import threading

from nonebot import on_command
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import MessageEvent, MessageSegment
from PIL import Image, ImageDraw, ImageFont

from common import FONTS, TEST_PRIVILEGED_GROUPS, cleanup_cache, is_owner

_logger = logging.getLogger(__name__)
_BASE_DIR = os.path.dirname(__file__)
_CACHE_DIR = os.path.join(_BASE_DIR, "cache")
_CACHE_MAX_AGE = 30 * 24 * 60 * 60
_RENDER_VERSION = "help-card-v2"
_CACHE_LOCK = threading.RLock()

_IMAGE_SYMBOLS = {
    "🤖": "◆",
    "🎲": "◆",
    "🎈": "◆",
    "🎮": "◆",
    "💬": "◆",
    "⏳": "◆",
    "💸": "◆",
    "📌": "•",
    "🔔": "◆",
    "📊": "◆",
    "📢": "◆",
    "👥": "◆",
}

help_cmd = on_command("hp", aliases={"帮助", "help", "菜单"}, priority=1, block=True)


TEXT = """🤖 机器人指令菜单
━━━━━━━━━━━━━━━━━━━━
🎲 娱乐
  .rp            · 抽签 + 今日运势（别名：抽签、运势、求签、今日运势、今日运气）
  今天吃什么      · 群里聊到“吃什么/吃啥”时随机推荐美食

🎈 BTD6（气球塔防6）
  .btd6排行 竞赛|boss|领土 [P页码|排名] · 排行榜（默认前50；P2第2页；数字查该名次玩家）
  .btd6活动       · 当前竞赛/Boss/领土/远征总览
  .btd6竞速       · 竞赛规则详情 │ .btd6boss · Boss规则（标准+精英）
  .btd6ct         · 争夺领土详情 │ .btd6rush · Boss Rush冲刺
  .btd6每日       · 每日挑战 │ .btd6远征 · 远征 │ .btd6收集 · 收集活动
  .btd6玩家 <OAK> · 玩家档案（含存档；OAK在游戏账号设置生成，私聊使用）
  .btd6地图 最新|热门|点赞 [数量] · 自制地图榜单
  .btd6历史 [竞速|boss|领土|远征|每日] [数量] · 历史活动归档
  提示：发送 .btd6 查看完整 BTD6 帮助

🎮 守望先锋（国服）
  .绑定 名字#数字  · 绑定你的战网ID（一次即可）
  .战报 [ID]      · 近期战绩图
  .段位 [ID]      · 段位历史图
  .强度 [ID]      · 强度分析图
  .总结 [ID]      · 上分总结图（仅今日）
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

📢 每日推送（在哪个群开启就推送到哪个群，可多群）
  .新闻开启        · 本群开启每日晨报推送（每天 7:00，早安问候·农历节气·昨日新闻）
  .新闻关闭        · 本群关闭每日晨报推送
  .新闻测试        · 立即发送今日晨报测试
  .新闻状态        · 查看已开启推送的群
  .新闻key        · 私聊设置晨报AI的免费key（智谱GLM-Flash）
  .词云开启        · 本群开启每日词云推送（每天 00:00）
  .词云关闭        · 本群关闭每日词云推送
  .词云状态        · 查看已开启推送的群
  .倒计时开启      · 本群开启每天 17:00 倒计时推送
  .倒计时关闭      · 本群关闭倒计时推送
  .倒计时状态      · 查看已开启推送的群
  .倒计时测试      · 立即测试本群倒计时消息
  .统计开启        · 本群开启每日指令统计（每天 00:00）
  .统计关闭        · 本群关闭指令统计
  .统计状态        · 查看已开启推送的群
  .btd6推送开启/关闭/状态 · BTD6 活动自动推送管理（竞速/Boss/CT等刷新时推送）
  .btd6预热         · 手动预热全部BTD6活动数据

💬 随机插话（机器人围观群聊，按概率以人设插话）
  .插话开启        · 本群开启随机插话
  .插话关闭        · 本群关闭随机插话
  .插话状态        · 查看本群插话配置与上下文缓冲
  .插话概率 N      · 调整触发概率（N=1~20，百分比，默认2）

👥 进群管理
  .自动通过 关键字  · 开启自动通过（附言匹配关键字即放行）
  .自动通过关闭    · 关闭自动通过
  .自动通过查看    · 查看当前关键字配置
  .自动通过数量    · 查看关键字数量
  .同意 QQ号      · 通过待审批的入群申请（战网验证超时转人工时用）
  .战网验证 开启/关闭 · 按群开关入群战网ID验证（在本群发可省略群号）
  .战网验证       · 查看各群验证开关状态
  .ow维护 开启/关闭/状态 · OW查询维护开关
  私聊回复 同意/拒绝 · 处理待审批的加群/好友申请（多个时引用通知消息）"""


def _wrap_line(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    if not text:
        return [""]
    lines: list[str] = []
    current = ""
    for char in text:
        candidate = current + char
        if current and draw.textlength(candidate, font=font) > max_width:
            lines.append(current)
            current = char
        else:
            current = candidate
    lines.append(current)
    return lines


def _help_cache_path(text: str, variant: str) -> str:
    payload = (
        f"{_RENDER_VERSION}\n{variant}\n{_plugin_source_signature()}\n{text}"
    ).encode()
    digest = hashlib.sha256(payload).hexdigest()[:20]
    return os.path.join(_CACHE_DIR, f"help_{variant}_{digest}.png")


def _normalize_image_text(text: str) -> str:
    """将服务器缺失字体的装饰 emoji 转成稳定的可渲染符号。"""
    for emoji, symbol in _IMAGE_SYMBOLS.items():
        text = text.replace(emoji, symbol)
    return text


def _plugin_source_signature() -> str:
    """用插件源码的路径、大小和修改时间检测帮助相关实现是否变化。"""
    digest = hashlib.sha256()
    plugin_root = os.path.dirname(_BASE_DIR)
    pattern = os.path.join(plugin_root, "**", "*.py")
    for path in sorted(glob.glob(pattern, recursive=True)):
        if "__pycache__" in path:
            continue
        try:
            stat = os.stat(path)
        except OSError:
            continue
        relative = os.path.relpath(path, plugin_root).replace(os.sep, "/")
        digest.update(f"{relative}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode())
    return digest.hexdigest()[:20]


def _render_help_image(text: str, variant: str) -> str:
    """按帮助内容哈希缓存图片，内容变化时自动生成新版本。"""
    path = _help_cache_path(text, variant)
    with _CACHE_LOCK:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        cleanup_cache(_CACHE_DIR, max_age=_CACHE_MAX_AGE)
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return path

        # 字体缺失时直接抛错交给上层走文本回退；load_default 不含 CJK，产出整页乱码
        title_font = ImageFont.truetype(FONTS["noto_bold"], 42)
        body_font = ImageFont.truetype(FONTS["noto_reg"], 27)

        width = 1080
        margin = 58
        content_width = width - margin * 2
        measure = Image.new("RGB", (width, 10), "white")
        measure_draw = ImageDraw.Draw(measure)
        rendered: list[tuple[str, object, int, tuple[int, int, int]]] = []
        source_lines = _normalize_image_text(text).splitlines()
        for index, source_line in enumerate(source_lines):
            font = title_font if index == 0 else body_font
            line_height = 62 if index == 0 else 42
            fill = (31, 35, 45) if index == 0 else (55, 58, 68)
            for line in _wrap_line(measure_draw, source_line, font, content_width):
                rendered.append((line, font, line_height, fill))

        height = 56 + sum(item[2] for item in rendered) + 48
        image = Image.new("RGB", (width, height), (244, 246, 250))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle(
            (18, 18, width - 18, height - 18),
            radius=24,
            fill=(255, 255, 255),
            outline=(224, 228, 236),
            width=2,
        )
        y = 48
        for line, font, line_height, fill in rendered:
            draw.text((margin, y), line, font=font, fill=fill)
            y += line_height

        tmp_path = f"{path}.tmp-{os.getpid()}-{threading.get_ident()}"
        try:
            image.save(tmp_path, "PNG")
            os.replace(tmp_path, path)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        return path


TEST_GROUP_IDS = TEST_PRIVILEGED_GROUPS


def _is_test_group(event: MessageEvent) -> bool:
    gid = getattr(event, "group_id", None)
    try:
        return int(gid) in TEST_GROUP_IDS if gid is not None else False
    except (TypeError, ValueError):
        return False


@help_cmd.handle()
async def handle(bot: Bot, event: MessageEvent):
    group_id = getattr(event, "group_id", None)
    is_test = _is_test_group(event)
    is_priv = is_owner(event)

    # 测试群：完整菜单直接在群内展示；其他群：始终公开菜单在群内展示（不再私发）；
    # 私聊：按权限展示
    if is_test:
        text = TEXT + OWNER_TEXT
        variant = "owner"
    elif group_id is not None:
        text = TEXT
        variant = "public"
    else:
        if is_priv:
            text = TEXT + OWNER_TEXT
            variant = "owner"
        else:
            text = TEXT
            variant = "public"

    try:
        path = await asyncio.to_thread(_render_help_image, text, variant)
        content = MessageSegment.image("file://" + path)
    except Exception:
        _logger.exception("帮助图片生成失败，回退发送文本")
        content = text
    await help_cmd.finish(content)
