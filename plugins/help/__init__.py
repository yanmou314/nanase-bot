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

from common import FONTS, RENDER_SEM, RENDER_TOTAL_TIMEOUT, TEST_PRIVILEGED_GROUPS, cleanup_cache, is_owner

_logger = logging.getLogger(__name__)
_BASE_DIR = os.path.dirname(__file__)
_CACHE_DIR = os.path.join(_BASE_DIR, "cache")
_CACHE_MAX_AGE = 30 * 24 * 60 * 60
_RENDER_VERSION = "help-card-v3"
_CACHE_LOCK = threading.RLock()

help_cmd = on_command("hp", aliases={"帮助", "help", "菜单"}, priority=1, block=True)


# ---------------- 内容（结构化：分区色条 + 命令/说明行） ----------------
# 第三元为分区强调色；一行一条命令，说明不与命令挤在同一行。

PUBLIC_SECTIONS: list[tuple[str, str, list[tuple[str, str]]]] = [
    ("娱乐", "#F59E0B", [
        (".rp", "抽签 + 今日运势（别名：抽签、运势、求签、今日运势、今日运气）"),
        ("吃什么 / 吃啥", "群聊被动触发随机推荐美食（非指令）"),
    ]),
    ("BTD6 · 气球塔防6", "#0EA5E9", [
        (".btd6活动", "竞赛 · Boss · 领土 · 远征 总览"),
        (".btd6排行 竞赛|boss|领土 [P页|排名]", "排行榜默认前 25；P2 翻页；数字查该名次玩家"),
        (".btd6竞速", "竞赛规则详情"),
        (".btd6boss", "Boss 规则（标准 + 精英）"),
        (".btd6ct [预设|格子]", "争夺领土。预设：默认/游戏类型/地图背景/英雄/坐标；格子如 DAA"),
        (".btd6rush", "Boss Rush 冲刺"),
        (".btd6每日", "今日每日挑战（标准 + 高级 + Coop）"),
        (".btd6远征", "当前远征 Odyssey"),
        (".btd6收集", "收集活动 Featured Insta 计划表"),
        (".btd6玩家 <OAK>", "玩家档案（OAK 在游戏账号设置生成，请私聊查询）"),
        (".btd6历史 [类型] [数量]", "本地归档的历史活动"),
        (".btd6 CT  /  .btd CT", "快捷看活动总览（CT 前带空格）"),
        (".btd6  /  .btd", "打开完整 BTD6 帮助"),
    ]),
    ("守望先锋 · 国服", "#8B5CF6", [
        (".绑定 名字#数字", "绑定你的战网 ID（一次即可）"),
        (".战报 [ID]", "近期战绩图"),
        (".段位 [ID]", "段位历史图"),
        (".强度 [ID]", "强度分析图"),
        (".总结 [ID]", "上分总结图（仅今日）"),
        (".我的ID", "查看当前绑定"),
        (".解绑", "解除绑定"),
    ]),
    ("游戏组队", "#10B981", [
        (".上号2=6", "开新队：已有 2 人、缺 6 人（你当队长）"),
        (".加入N", "加入 N 号小队"),
        (".谁玩", "看现在谁在玩"),
        (".下班", "退出小队"),
        ("__note__", "开队前主人先在本群发 .上号开启"),
    ]),
    ("AI 聊天", "#EC4899", [
        ("@机器人 + 消息", "和西野七濑 AI 聊天"),
    ]),
    ("时间提醒", "#64748B", [
        (".倒计时", "下一个周末和节假日倒计时"),
    ]),
]

OWNER_SECTIONS: list[tuple[str, str, list[tuple[str, str]]]] = [
    ("组队榜开关", "#10B981", [
        (".上号开启 [群号]", "本群开启上号组队榜（本群发可省略群号）"),
        (".上号关闭 [群号]", "本群关闭上号组队榜"),
        (".上号状态", "查看已开启上号榜的群"),
    ]),
    ("统计查询", "#3B82F6", [
        (".龙王", "今日发言最多的人"),
        (".词云 [N]", "今日热词（N=1~60，默认 40）"),
    ]),
    ("每日推送", "#F59E0B", [
        (".新闻开启 / 关闭 / 测试 / 状态", "每日晨报（7:00）：早安 · 农历 · 昨日新闻"),
        (".新闻key", "私聊设置晨报 AI 的免费 key（智谱 GLM-Flash）"),
        (".词云开启 / 关闭 / 状态", "每日词云推送（00:00）"),
        (".倒计时开启 / 关闭 / 状态 / 测试", "每日 17:00 倒计时推送"),
        (".统计开启 / 关闭 / 状态", "每日指令统计（00:00）"),
        (".btd6推送开启 / 关闭 / 状态", "BTD6 活动刷新自动推送（竞速/Boss/CT 等）"),
        (".btd6推送检查 [类型]", "强制补推当期活动到本群；不带类型看推送对照"),
        (".btd6预热", "手动预热全部 BTD6 活动数据"),
        (".ow补丁订阅 / 退订 / 状态 / 测试", "OW 国服补丁推送（每小时检查）"),
        (".ow状态 / .ow重置", "查看 / 清空 OW 查询队列"),
    ]),
    ("随机插话", "#EC4899", [
        (".插话开启 / 关闭 / 状态", "围观群聊，按概率以人设插话"),
        (".插话概率 N", "触发概率 N=1~20%，默认 2%"),
    ]),
    ("进群管理", "#8B5CF6", [
        (".自动通过 关键字", "附言匹配关键字即放行"),
        (".自动通过关闭 / 查看 / 数量", "管理自动通过规则"),
        (".进群拉黑 QQ号 / .解除拉黑 / .拉黑列表", "拉黑后申请转人工"),
        (".同意 QQ号", "通过待审批的入群申请"),
        (".战网验证开启 / 关闭 / 状态", "按群开关入群战网 ID 验证"),
        ("私聊回复 同意 / 拒绝", "处理待审批的加群/好友申请"),
    ]),
    ("清人", "#EF4444", [
        (".清人时间 [群号] 日期|30天|清除", "按最后发言时间踢潜水"),
        (".清人预览 [群号]", "查看将被踢名单（不踢人）"),
        (".清人执行 [群号] 确认", "真实踢出（机器人需为管理员）"),
        (".清人状态 [群号]", "查看配置（不带群号看全部）"),
        (".清人保护 [群号] +QQ/-QQ", "保护名单（群主/管理/主人始终保护）"),
    ]),
]


def _sections_to_text(
    sections: list[tuple[str, str, list[tuple[str, str]]]],
    title: str,
    footer: str | None = None,
) -> str:
    lines = [title, ""]
    for sec_title, _color, rows in sections:
        lines.append(sec_title)
        for cmd, desc in rows:
            if cmd == "__note__":
                lines.append(f"  ※ {desc}")
                continue
            lines.append(f"  {cmd}")
            if desc:
                lines.append(f"    · {desc}")
        lines.append("")
    if footer:
        lines.append(footer)
    return "\n".join(lines).rstrip() + "\n"


TEXT = _sections_to_text(
    PUBLIC_SECTIONS,
    "机器人指令菜单",
    "指令均以 . 开头；绑定后 [ID] 可省略",
)
OWNER_TEXT = "\n" + _sections_to_text(
    OWNER_SECTIONS,
    "管理功能（仅主人可见）",
)


# ---------------- 渲染 ----------------

_PAGE_BG = (232, 237, 244)
_CARD_BG = (255, 255, 255)
_INK = (28, 36, 51)
_INK2 = (90, 102, 122)
_CHIP_BG = (22, 32, 48)
_CHIP_FG = (255, 255, 255)
_LINE = (228, 233, 241)
_FOOTER = (120, 132, 150)

_W = 1080
_PAD = 48


def _hex_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


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
    if current:
        lines.append(current)
    return lines or [""]


def _text_h(font) -> int:
    ascent, descent = font.getmetrics()
    return ascent + descent


def _help_cache_path(payload: str, variant: str) -> str:
    digest = hashlib.sha256(payload.encode()).hexdigest()[:20]
    return os.path.join(_CACHE_DIR, f"help_{variant}_{digest}.png")


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


def _render_help_image(
    sections: list[tuple[str, str, list[tuple[str, str]]]],
    variant: str,
    title: str,
    footer: str | None,
    owner_badge: bool = False,
) -> str:
    """结构化帮助卡片：分区色条 + 命令胶囊 + 灰色说明，一行一命令。"""
    payload = (
        f"{_RENDER_VERSION}\n{variant}\n{_plugin_source_signature()}\n"
        f"{title}\n{footer}\n{sections!r}"
    )
    path = _help_cache_path(payload, variant)
    with _CACHE_LOCK:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        cleanup_cache(_CACHE_DIR, max_age=_CACHE_MAX_AGE)
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return path

        # 字体缺失时直接抛错交给上层走文本回退
        title_font = ImageFont.truetype(FONTS["noto_bold"], 40)
        badge_font = ImageFont.truetype(FONTS["noto_reg"], 20)
        sec_font = ImageFont.truetype(FONTS["noto_bold"], 27)
        chip_font = ImageFont.truetype(FONTS["noto_bold"], 22)
        desc_font = ImageFont.truetype(FONTS["noto_reg"], 21)
        foot_font = ImageFont.truetype(FONTS["noto_reg"], 19)

        measure = Image.new("RGB", (_W, 10), _PAGE_BG)
        mdraw = ImageDraw.Draw(measure)
        content_w = _W - _PAD * 2
        chip_max_w = content_w - 28  # 色条+内缩

        # ---- 预排版：算总高 ----
        y_cursor = 0
        title_h = _text_h(title_font) + 14
        badge_h = (_text_h(badge_font) + 28) if owner_badge else 0
        y_cursor += title_h + badge_h + 10

        laid_sections = []
        for sec_title, color, rows in sections:
            accent = _hex_rgb(color)
            sec_h = 18 + _text_h(sec_font) + 14
            laid_rows = []
            for cmd, desc in rows:
                if cmd == "__note__":
                    note_lines = _wrap_line(mdraw, desc, desc_font, content_w - 56)
                    note_h = len(note_lines) * (_text_h(desc_font) + 6) + 14
                    laid_rows.append(("note", [desc], note_lines, note_h, 0))
                    sec_h += note_h + 10
                    continue

                chip_w_est = mdraw.textlength(cmd, font=chip_font) + 36
                chip_h = _text_h(chip_font) + 18
                # 短说明尽量放命令右侧；过长则换行到命令下方
                inline = (
                    desc
                    and chip_w_est < content_w * 0.42
                    and mdraw.textlength(desc, font=desc_font) + chip_w_est + 40 < content_w
                )
                if inline:
                    row_h = chip_h + 16
                    laid_rows.append(("inline", [cmd], [desc], row_h, chip_h))
                else:
                    chip_lines = _wrap_line(mdraw, cmd, chip_font, content_w - 60)
                    chip_block_h = _text_h(chip_font) + 18 + (len(chip_lines) - 1) * (_text_h(chip_font) + 6)
                    desc_lines = _wrap_line(mdraw, desc, desc_font, content_w - 36) if desc else []
                    desc_h = len(desc_lines) * (_text_h(desc_font) + 5)
                    row_h = chip_block_h + (8 + desc_h if desc_lines else 0) + 16
                    laid_rows.append(("stack", chip_lines, desc_lines, row_h, chip_block_h))
                sec_h += row_h + 8
            laid_sections.append((sec_title, accent, laid_rows, sec_h))
            y_cursor += sec_h + 16

        foot_h = (_text_h(foot_font) + 22) if footer else 0
        y_cursor += foot_h + 20
        height = y_cursor + 36

        image = Image.new("RGB", (_W, height), _PAGE_BG)
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle(
            (18, 18, _W - 18, height - 18),
            radius=28,
            fill=_CARD_BG,
            outline=(220, 226, 236),
            width=2,
        )

        # ---- 绘制 ----
        y = 18 + 36
        draw.text((_PAD, y), title, font=title_font, fill=_INK)
        y += title_h

        if owner_badge:
            bw = draw.textlength("仅主人可见", font=badge_font) + 28
            draw.rounded_rectangle(
                (_PAD, y, _PAD + bw, y + _text_h(badge_font) + 16),
                radius=14,
                fill=(255, 243, 224),
                outline=(255, 200, 120),
                width=1,
            )
            draw.text((_PAD + 14, y + 7), "仅主人可见", font=badge_font, fill=(180, 100, 20))
            y += badge_h

        y += 10

        for sec_title, accent, laid_rows, sec_h in laid_sections:
            top = y
            # 分区底板
            draw.rounded_rectangle(
                (_PAD, y, _W - _PAD, y + sec_h - 8),
                radius=18,
                fill=(248, 250, 253),
                outline=(232, 237, 244),
                width=1,
            )
            # 左侧强调色条
            draw.rounded_rectangle(
                (_PAD, y, _PAD + 8, y + sec_h - 8),
                radius=4,
                fill=accent,
            )
            draw.text((_PAD + 24, y + 14), sec_title, font=sec_font, fill=_INK)
            y += 18 + _text_h(sec_font) + 12

            for kind, chip_lines, desc_lines, row_h, chip_block_h in laid_rows:
                row_top = y
                if kind == "note":
                    # 浅色提示条，不伪装成命令
                    draw.rounded_rectangle(
                        (_PAD + 24, row_top, _W - _PAD - 16, row_top + row_h - 6),
                        radius=10,
                        fill=(255, 248, 235),
                    )
                    dy = row_top + 6
                    for dl in desc_lines:
                        draw.text((_PAD + 36, dy), dl, font=desc_font, fill=(160, 110, 40))
                        dy += _text_h(desc_font) + 5
                    y = row_top + row_h + 8
                    continue

                if kind == "inline":
                    cmd = chip_lines[0]
                    desc = desc_lines[0] if desc_lines else ""
                    chip_h = chip_block_h or (_text_h(chip_font) + 18)
                    tw = draw.textlength(cmd, font=chip_font)
                    draw.rounded_rectangle(
                        (_PAD + 24, row_top, _PAD + 24 + tw + 32, row_top + chip_h),
                        radius=12,
                        fill=_CHIP_BG,
                    )
                    draw.text((_PAD + 40, row_top + 9), cmd, font=chip_font, fill=_CHIP_FG)
                    if desc:
                        # 垂直居中
                        dy = row_top + (chip_h - _text_h(desc_font)) / 2
                        draw.text((_PAD + 24 + tw + 48, dy), desc, font=desc_font, fill=_INK2)
                    y = row_top + row_h + 8
                    continue

                # stack
                chip_text_h = _text_h(chip_font)
                max_tw = 0
                for cl in chip_lines:
                    max_tw = max(max_tw, draw.textlength(cl, font=chip_font))
                chip_w = max_tw + 36
                draw.rounded_rectangle(
                    (_PAD + 24, row_top, _PAD + 24 + chip_w, row_top + chip_block_h),
                    radius=12,
                    fill=_CHIP_BG,
                )
                ty = row_top + 9
                for cl in chip_lines:
                    draw.text((_PAD + 40, ty), cl, font=chip_font, fill=_CHIP_FG)
                    ty += chip_text_h + 6

                if desc_lines:
                    dy = row_top + chip_block_h + 8
                    for dl in desc_lines:
                        draw.text((_PAD + 24, dy), dl, font=desc_font, fill=_INK2)
                        dy += _text_h(desc_font) + 5
                y = row_top + row_h + 8

            y = top + sec_h + 16

        if footer:
            draw.text((_PAD, y), footer, font=foot_font, fill=_FOOTER)
            y += foot_h

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

    # 测试群/群聊：公开菜单；私聊：主人额外看管理菜单
    if is_test:
        sections = PUBLIC_SECTIONS
        variant = "owner"
        owner_badge = False
        title = "机器人指令菜单"
        footer = "指令均以 . 开头；绑定后 [ID] 可省略"
        fallback = TEXT
    elif group_id is not None:
        sections = PUBLIC_SECTIONS
        variant = "public"
        owner_badge = False
        title = "机器人指令菜单"
        footer = "指令均以 . 开头；绑定后 [ID] 可省略"
        fallback = TEXT
    else:
        if is_priv:
            sections = PUBLIC_SECTIONS + OWNER_SECTIONS
            variant = "owner"
            owner_badge = True
            title = "机器人指令菜单"
            footer = "指令均以 . 开头；绑定后 [ID] 可省略"
            fallback = TEXT + OWNER_TEXT
        else:
            sections = PUBLIC_SECTIONS
            variant = "public"
            owner_badge = False
            title = "机器人指令菜单"
            footer = "指令均以 . 开头；绑定后 [ID] 可省略"
            fallback = TEXT

    try:
        async with RENDER_SEM:
            path = await asyncio.wait_for(
                asyncio.to_thread(
                    _render_help_image, sections, variant, title, footer, owner_badge
                ),
                timeout=RENDER_TOTAL_TIMEOUT)
        content = MessageSegment.image("file://" + path)
    except Exception:
        _logger.exception("帮助图片生成失败，回退发送文本")
        content = fallback
    await help_cmd.finish(content)
