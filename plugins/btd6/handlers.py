"""命令处理层：全部 nonebot matcher/命令 handler 与命令参数解析。"""
import asyncio
import logging
import re
import time
from pathlib import Path

from nonebot import on_command
from nonebot.adapters.onebot.v11 import MessageEvent, MessageSegment

from common import is_owner

from . import cards as cards_mod, collect, ctmap, i18n, nkapi, push, textfmt, util

_logger = logging.getLogger(__name__)


# ---------------- 参数解析 ----------------

RUSH_WORDS = {"rush", "冲刺", "bossrush", "bossRush", "冲刺赛"}
RACE_WORDS = {"race", "races", "竞速", "竞赛"}
BOSS_WORDS = {"boss", "bosses", "首领", "boss战", "魔王"}
CT_WORDS = {"ct", "领土", "争夺", "争夺领土"}
STANDARD_WORDS = {"standard", "标准", "普通"}
ELITE_WORDS = {"elite", "精英"}
PLAYER_WORDS = {"player", "个人", "玩家"}
TEAM_WORDS = {"team", "战队", "团队"}


def parse_kind(tokens: list[str]) -> str | None:
    for t in tokens:
        k = t.lower()
        if k in RUSH_WORDS:
            return "rush"
        if k in RACE_WORDS:
            return "race"
        if k in BOSS_WORDS:
            return "boss"
        if k in CT_WORDS:
            return "ct"
    return None


def parse_variant(tokens: list[str], default: str) -> str:
    for t in tokens:
        k = t.lower()
        if k in ELITE_WORDS:
            return "elite"
        if k in STANDARD_WORDS:
            return "standard"
        if k in TEAM_WORDS:
            return "team"
        if k in PLAYER_WORDS:
            return "player"
    return default


def parse_rows(tokens: list[str]) -> int:
    rows = nkapi.DEFAULT_ROWS
    for t in tokens:
        if t.isdigit():
            rows = max(1, min(int(t), nkapi.MAX_ROWS))
    return rows


def parse_lb_page(tokens: list[str]) -> int | None:
    """解析 P 页码：支持 'P2' / 'p2' / 'P 2' 两种写法。"""
    for i, t in enumerate(tokens):
        if t.lower() == "p" and i + 1 < len(tokens) and tokens[i + 1].isdigit():
            try:
                n = int(tokens[i + 1])
                if 1 <= n <= nkapi.LB_MAX_PAGE:
                    return n
            except ValueError:
                pass
        if re.fullmatch(r"[pP]\d+", t):
            try:
                n = int(t[1:])
                if 1 <= n <= nkapi.LB_MAX_PAGE:
                    return n
            except ValueError:
                pass
    return None


def parse_lb_rank(tokens: list[str]) -> int | None:
    """解析排名数字：纯数字且不是 P 页码的一部分。"""
    for i, t in enumerate(tokens):
        if t.lower() == "p":
            continue
        if re.fullmatch(r"[pP]\d+", t):
            continue
        if t.isdigit():
            if i > 0 and tokens[i - 1].lower() == "p":
                continue  # P 后的数字已作为页码
            try:
                n = int(t)
                if 1 <= n <= nkapi.LB_MAX_RANK:
                    return n
            except ValueError:
                pass
    return None


# ---------------- 命令处理 ----------------

help_cmd = on_command("btd6", priority=5, block=True)
help_alias_cmd = on_command("btd6帮助", priority=5, block=True)
events_cmd = on_command("btd6活动", priority=5, block=True)
ct_cmd = on_command("btd6领土", priority=5, block=True)
rush_cmd = on_command("btd6冲刺", priority=5, block=True)
collect_cmd = on_command("btd6收集", priority=5, block=True)
lb_cmd = on_command("btd6排行", priority=5, block=True)
rules_cmd = on_command("btd6竞速", priority=5, block=True)
maps_cmd = on_command("btd6地图", priority=5, block=True)
daily_cmd = on_command("btd6每日", priority=5, block=True)
odyssey_cmd = on_command("btd6远征", priority=5, block=True)
player_cmd = on_command("btd6玩家", priority=5, block=True)
push_on_cmd = on_command("btd6推送开启", priority=5, block=True)
push_off_cmd = on_command("btd6推送关闭", priority=5, block=True)
push_status_cmd = on_command("btd6推送状态", priority=5, block=True)
hist_cmd = on_command("btd6历史", priority=5, block=True)
prewarm_cmd = on_command("btd6预热", priority=5, block=True)


@help_cmd.handle()
async def handle_help(event: MessageEvent):
    await nkapi._enforce_cooldown(help_cmd, event, "help")
    # 兼容 ".btd6 CT"（含空格）直接查看争夺领土/活动总览
    _plain = event.get_plaintext().strip().lower()
    _arg = _plain.replace(".btd6", "", 1).strip() if _plain.startswith(".btd6") else _plain
    if _arg in {"ct", "领土", "争夺", "争夺领土"}:
        try:
            data = await collect.collect_overview()
        except Exception:
            _logger.exception("BTD6 CT 总览获取失败")
            nkapi._release_cooldown(event, "help")
            await help_cmd.finish("⚠️ 获取 BTD6 活动信息失败，请稍后再试")
        await cards_mod._send_card(help_cmd, "btd6ov", lambda: cards_mod.overview_html(data), lambda: textfmt.overview_text(data))
        return
    await cards_mod._send_card(help_cmd, "btd6help", cards_mod.help_html, lambda: i18n.HELP_TEXT)


@help_alias_cmd.handle()
async def handle_help_alias(event: MessageEvent):
    await nkapi._enforce_cooldown(help_alias_cmd, event, "help")
    await cards_mod._send_card(help_alias_cmd, "btd6help", cards_mod.help_html, lambda: i18n.HELP_TEXT)


@events_cmd.handle()
async def handle_events(event: MessageEvent):
    await nkapi._enforce_cooldown(events_cmd, event, "events")
    try:
        data = await collect.collect_overview()
    except Exception:
        nkapi._release_cooldown(event, "events")
        _logger.exception("BTD6 活动总览获取失败")
        await events_cmd.finish("⚠️ 获取 BTD6 活动信息失败，请稍后再试")
    await cards_mod._send_card(events_cmd, "btd6ov", lambda: cards_mod.overview_html(data), lambda: textfmt.overview_text(data))


_CT_PRESET_ALIAS = {
    "default": "default", "默认": "default",
    "gametypes": "gametypes", "游戏类型": "gametypes", "类型": "gametypes",
    "maps": "maps", "地图背景": "maps", "背景": "maps",
    "heroes": "heroes", "英雄": "heroes",
    "coords": "coords", "坐标": "coords",
}


@ct_cmd.handle()
async def handle_ct(event: MessageEvent):
    """争夺领土：无参 → 总览图；带 preset 名 → 预设图；带 tile id → 单格详情。"""
    await nkapi._enforce_cooldown(ct_cmd, event, "ct")
    try:
        col = await collect.collect_ct()
    except Exception as e:
        # 已 finish 的异常直接抛出（回滚冷却只针对真正的失败路径，与 handle_leaderboard 一致）
        from nonebot.exception import FinishedException
        if isinstance(e, FinishedException):
            raise
        nkapi._release_cooldown(event, "ct")
        _logger.exception("BTD6 CT 获取失败")
        await ct_cmd.finish("⚠️ 获取争夺领土失败，请稍后再试")
    if col.get("empty"):
        await ct_cmd.finish(col["empty"])
    raw = event.get_plaintext().strip()
    arg = raw.split(maxsplit=1)[1].strip() if len(raw.split(maxsplit=1)) > 1 else ""
    arg_key = arg.lower()
    if not arg:
        await cards_mod._send_card(ct_cmd, "btd6ct",
                                   lambda: cards_mod.ctmap_html(col),
                                   lambda: textfmt.ct_text(col))
        return
    preset_name = _CT_PRESET_ALIAS.get(arg_key)
    if preset_name is not None:
        await cards_mod._send_card(ct_cmd, f"btd6ctp_{preset_name}",
                                   lambda n=preset_name: cards_mod.ctmap_preset_html(col, n),
                                   lambda n=preset_name: textfmt.ct_preset_text(col, n))
        return
    # 兜底：当作 tile id（支持大写 / 小写 / 出生点 6 个）查单格
    tile_id = arg.upper()
    grid = ctmap.build_ct_grid()
    spawn_ids = {sp["id"] for sp in grid["spawns"]}
    if tile_id in grid["tiles"] or tile_id in spawn_ids:
        await cards_mod._send_card(ct_cmd, f"btd6ct_tile_{tile_id}",
                                   lambda t=tile_id: cards_mod.ct_tile_html(col, t),
                                   lambda t=tile_id: textfmt.ct_tile_text(col, t))
        return
    valid = "、".join(f"{n}({lbl})" for n, lbl in cards_mod.CT_PRESET_CARDS)
    await ct_cmd.finish(
        f"⚠️ 未知参数：{arg}\n预设：{valid}\n"
        f"或格子 id（例：DAA / AAA，共 {len(grid['tiles']) + 6} 个）")


@rush_cmd.handle()
async def handle_rush(event: MessageEvent):
    """模仿 handle_odyssey：fetch → collect → _send_card，使用 _rush_diff_html（1:1 odyssey_diff_html）。"""
    await nkapi._enforce_cooldown(rush_cmd, event, "rush")
    try:
        col = await collect.collect_rush()
    except Exception:
        nkapi._release_cooldown(event, "rush")
        _logger.exception("Boss Rush 获取失败")
        await rush_cmd.finish("⚠️ 获取 Boss Rush 失败，请稍后再试")
        return
    if col.get("empty"):
        await rush_cmd.finish(col["empty"])
    await cards_mod._send_card(rush_cmd, "btd6rush", lambda: cards_mod._rush_diff_html(col), lambda: textfmt._rush_text(col))


@collect_cmd.handle()
async def handle_collect(event: MessageEvent):
    """收集活动 Featured Insta 计划表（instagen 由活动种子确定性生成，模仿 handle_rush）。"""
    await nkapi._enforce_cooldown(collect_cmd, event, "collect")
    try:
        col = await collect.collect_collectevent()
    except Exception:
        nkapi._release_cooldown(event, "collect")
        _logger.exception("BTD6 收集活动获取失败")
        await collect_cmd.finish("⚠️ 获取收集活动失败，请稍后再试")
        return
    if col.get("empty"):
        await collect_cmd.finish(col["empty"])
    await cards_mod._send_card(collect_cmd, "btd6col", lambda: cards_mod.collectevent_html(col), lambda: textfmt.collectevent_text(col))


@lb_cmd.handle()
async def handle_leaderboard(event: MessageEvent):
    await nkapi._enforce_cooldown(lb_cmd, event, "leaderboard")
    tokens = event.get_plaintext().split()[1:]
    kind = parse_kind(tokens)
    if kind is None:
        await lb_cmd.finish(i18n.LB_USAGE)
    page = parse_lb_page(tokens)
    rank = parse_lb_rank(tokens) if page is None else None

    # 排名查询：数字直接视为名次，返回该名次玩家的档案
    if rank is not None:
        rank = max(1, min(rank, nkapi.LB_MAX_RANK))
        has_variant_word = any(
            t.lower() in ELITE_WORDS or t.lower() in STANDARD_WORDS or t.lower() in PLAYER_WORDS or t.lower() in TEAM_WORDS
            for t in tokens
        )
        # Boss/CT 未显式指定子榜时，双榜各取一名玩家一起返回
        if kind in ("boss", "ct") and not has_variant_word:
            variants = {"boss": ("standard", "elite"), "ct": ("player", "team")}[kind]
            cards = []
            for variant in variants:
                try:
                    entry = await collect.fetch_rank_entry(kind, variant, rank)
                    if not entry:
                        continue
                    pid = collect._extract_player_id(str(entry.get("profile") or ""))
                    if not pid:
                        _logger.warning("排名 %s 的记录缺少 profile，跳过 variant=%s", rank, variant)
                        continue
                    col = await collect.collect_player(pid)
                    if col.get("empty"):
                        continue
                    cards.append((
                        f"btd6pl_rank_{variant}",
                        lambda c=col: cards_mod.player_html(c),
                        lambda c=col, v=variant: f"第 {rank} 名（{v}）· " + textfmt.player_text(c),
                    ))
                except Exception:
                    _logger.exception("BTD6 排名玩家获取失败 kind=%s variant=%s rank=%s", kind, variant, rank)
            if not cards:
                # 双榜全部失败：回滚冷却，允许用户立即重试（与 handle_daily 已修路径一致）
                nkapi._release_cooldown(event, "leaderboard")
                await lb_cmd.finish(f"⚠️ 未找到第 {rank} 名的玩家（排行榜可能不足 {rank} 人或档案缺失）")
            await cards_mod._finish_multi_cards(lb_cmd, cards)
            return
        # 单榜单排名查询
        variant = parse_variant(tokens, {"boss": "standard", "ct": "player"}.get(kind, ""))
        try:
            entry = await collect.fetch_rank_entry(kind, variant, rank)
            if not entry:
                await lb_cmd.finish(f"⚠️ 未找到第 {rank} 名的玩家（排行榜可能不足 {rank} 人）")
            pid = collect._extract_player_id(str(entry.get("profile") or ""))
            if not pid:
                # displayName 为 NK API 外部字段且玩家可自设昵称（防 CQ 码注入）：MessageSegment.text 包裹
                await lb_cmd.finish(
                    f"⚠️ 第 {rank} 名玩家 "
                    + MessageSegment.text(str(entry.get("displayName") or "未知"))
                    + " 的档案链接缺失，无法查询"
                )
            col = await collect.collect_player(pid)
            if col.get("empty"):
                await lb_cmd.finish(col["empty"])
        except Exception as e:
            # 已 finish 的异常直接抛出
            from nonebot.exception import FinishedException
            if isinstance(e, FinishedException):
                raise
            nkapi._release_cooldown(event, "leaderboard")
            _logger.exception("BTD6 排名玩家获取失败 kind=%s variant=%s rank=%s", kind, variant, rank)
            await lb_cmd.finish("⚠️ 获取该名次玩家信息失败，请稍后再试")
        await cards_mod._send_card(lb_cmd, "btd6pl_rank", lambda: cards_mod.player_html(col), lambda: f"第 {rank} 名 · " + textfmt.player_text(col))
        return

    # 分页查询：P2 / P 2 / p2
    if page is not None:
        page = max(1, min(page, nkapi.LB_MAX_PAGE))
        variants = {"boss": ("standard", "elite"), "ct": ("player", "team")}.get(kind)
        if variants:
            has_variant_word = any(
                t.lower() in ELITE_WORDS or t.lower() in STANDARD_WORDS or t.lower() in PLAYER_WORDS or t.lower() in TEAM_WORDS
                for t in tokens
            )
            if has_variant_word:
                variant = parse_variant(tokens, {"boss": "standard", "ct": "player"}[kind])
                try:
                    col = await collect.collect_leaderboard_page(kind, variant, page)
                except Exception:
                    nkapi._release_cooldown(event, "leaderboard")
                    _logger.exception("BTD6 排行榜分页获取失败 kind=%s variant=%s page=%s", kind, variant, page)
                    await lb_cmd.finish("⚠️ 获取 BTD6 排行榜失败，请稍后再试")
                await cards_mod._send_card(lb_cmd, f"btd6lb_p{page}", lambda: cards_mod.leaderboard_html(col), lambda: textfmt.leaderboard_text(col))
                return
            cards = []
            for variant in variants:
                try:
                    c = await collect.collect_leaderboard_page(kind, variant, page)
                    if not c.get("empty"):
                        cards.append((f"btd6lb_{variant}_p{page}", lambda c=c: cards_mod.leaderboard_html(c), lambda c=c: textfmt.leaderboard_text(c)))
                except Exception:
                    _logger.exception("BTD6 排行榜分页获取失败 kind=%s variant=%s page=%s", kind, variant, page)
            if not cards:
                # 双榜全部失败：回滚冷却，允许用户立即重试
                nkapi._release_cooldown(event, "leaderboard")
                await lb_cmd.finish("⚠️ 获取 BTD6 排行榜失败，请稍后再试")
            await cards_mod._finish_multi_cards(lb_cmd, cards)
            return
        variant = parse_variant(tokens, {"boss": "standard", "ct": "player"}.get(kind, ""))
        try:
            col = await collect.collect_leaderboard_page(kind, variant, page)
        except Exception:
            nkapi._release_cooldown(event, "leaderboard")
            _logger.exception("BTD6 排行榜分页获取失败 kind=%s variant=%s page=%s", kind, variant, page)
            await lb_cmd.finish("⚠️ 获取 BTD6 排行榜失败，请稍后再试")
        await cards_mod._send_card(lb_cmd, f"btd6lb_p{page}", lambda: cards_mod.leaderboard_html(col), lambda: textfmt.leaderboard_text(col))
        return

    # 默认：前50名
    rows = nkapi.LB_DEFAULT_ROWS
    variants = {"boss": ("standard", "elite"), "ct": ("player", "team")}.get(kind)
    if variants:
        cards = []
        for variant in variants:
            try:
                c = await collect.collect_leaderboard(kind, variant, rows)
                if not c.get("empty"):
                    cards.append((f"btd6lb_{variant}",
                                  lambda c=c: cards_mod.leaderboard_html(c), lambda c=c: textfmt.leaderboard_text(c)))
            except Exception:
                _logger.exception("BTD6 排行榜获取失败 kind=%s variant=%s", kind, variant)
        if not cards:
            # 双榜全部失败：回滚冷却，允许用户立即重试
            nkapi._release_cooldown(event, "leaderboard")
            await lb_cmd.finish("⚠️ 获取 BTD6 排行榜失败，请稍后再试")
        await cards_mod._finish_multi_cards(lb_cmd, cards)
        return
    variant = parse_variant(tokens, {"boss": "standard", "ct": "player"}.get(kind, ""))
    try:
        col = await collect.collect_leaderboard(kind, variant, rows)
    except Exception:
        nkapi._release_cooldown(event, "leaderboard")
        _logger.exception("BTD6 排行榜获取失败")
        await lb_cmd.finish("⚠️ 获取 BTD6 排行榜失败，请稍后再试")
    await cards_mod._send_card(lb_cmd, "btd6lb", lambda: cards_mod.leaderboard_html(col), lambda: textfmt.leaderboard_text(col))


@rules_cmd.handle()
async def handle_rules(event: MessageEvent):
    await nkapi._enforce_cooldown(rules_cmd, event, "rules")
    tokens = event.get_plaintext().split()[1:]
    kind = parse_kind(tokens) or "race"
    # 多版本一起发：boss→标准+精英，其余单版本
    if kind == "boss":
        cards = []
        for variant in ("standard", "elite"):
            try:
                c = await collect.collect_rules(kind, variant)
                if not c.get("empty"):
                    cards.append((f"btd6rule_{variant}",
                                  lambda c=c: cards_mod.rules_html(c), lambda c=c: textfmt.rules_text(c)))
            except Exception:
                _logger.exception("BTD6 规则获取失败 kind=%s variant=%s", kind, variant)
        if not cards:
            # 双版本全部失败：回滚冷却，允许用户立即重试
            nkapi._release_cooldown(event, "rules")
            await rules_cmd.finish("⚠️ 获取 BTD6 规则失败，请稍后再试")
        await cards_mod._finish_multi_cards(rules_cmd, cards)
        return
    variant = "" if kind == "race" else \
        ("elite" if any(t.lower() in ELITE_WORDS for t in tokens) else "standard")
    try:
        col = await collect.collect_rules(kind, variant)
    except Exception:
        nkapi._release_cooldown(event, "rules")
        _logger.exception("BTD6 规则获取失败")
        await rules_cmd.finish("⚠️ 获取 BTD6 规则失败，请稍后再试")
    await cards_mod._send_card(rules_cmd, "btd6rule", lambda: cards_mod.rules_html(col), lambda: textfmt.rules_text(col))


@maps_cmd.handle()
async def handle_maps(event: MessageEvent):
    await nkapi._enforce_cooldown(maps_cmd, event, "maps", "heavy")
    tokens = event.get_plaintext().split()[1:]
    filt = "newest"
    for t in tokens:
        mapped = collect.MAP_FILTERS.get(t.lower())
        if mapped:
            filt = mapped
    rows = min(parse_rows(tokens), 20)
    try:
        col = await collect.collect_maps(filt, rows)
    except Exception:
        nkapi._release_cooldown(event, "maps")
        _logger.exception("BTD6 地图列表获取失败")
        await maps_cmd.finish("⚠️ 获取 BTD6 自制地图失败，请稍后再试")
    await cards_mod._send_card(maps_cmd, "btd6map", lambda: cards_mod.maps_html(col), lambda: textfmt.maps_text(col))


@daily_cmd.handle()
async def handle_daily(event: MessageEvent):
    await nkapi._enforce_cooldown(daily_cmd, event, "daily")
    # 统一三版本一起发（标准+高级+Coop），忽略参数区分；三次取数相互独立，并发执行
    daily_cols = await asyncio.gather(
        collect._safe(collect.collect_daily(False), "daily"),
        collect._safe(collect.collect_daily(True), "daily_adv"),
        collect._safe(collect.collect_daily_coop(), "coop"))
    cards = []
    for key, c in zip(("btd6daily", "btd6dailya", "btd6coop"), daily_cols):
        if c and not c.get("empty"):
            cards.append((key, lambda c=c: cards_mod.rules_html(c), lambda c=c: textfmt.rules_text(c)))
    if not cards:
        nkapi._release_cooldown(event, "daily")
        await daily_cmd.finish("⚠️ 获取 BTD6 每日挑战失败，请稍后再试")
    await cards_mod._finish_multi_cards(daily_cmd, cards)


@odyssey_cmd.handle()
async def handle_odyssey(event: MessageEvent):
    await nkapi._enforce_cooldown(odyssey_cmd, event, "odyssey", "heavy")
    try:
        col = await collect.collect_odyssey()
    except Exception:
        nkapi._release_cooldown(event, "odyssey")
        _logger.exception("BTD6 远征获取失败")
        await odyssey_cmd.finish("⚠️ 获取 BTD6 远征信息失败，请稍后再试")
    if col.get("empty"):
        await odyssey_cmd.finish(col["empty"])
    # 统一三图尺寸：QQ 预览按最大边等比缩放，像素高度不同会导致显示宽度视觉不一；取三难度最大高度作为统一画布高度
    try:
        _unified_h = max(cards_mod._odyssey_card_height((col["diffs"].get(_d) or {}).get("meta"),
                                              len((col["diffs"].get(_d) or {}).get("maps") or []))
                         for _d, _lab in i18n._ODYSSEY_DIFFS)
        for _d, _ in i18n._ODYSSEY_DIFFS:
            if _d in col["diffs"]:
                col["diffs"][_d]["_unified_h"] = _unified_h
    except Exception:
        _logger.debug("BTD6 远征统一高度计算失败，使用各自高度", exc_info=True)
    # 流式渲染：渲染一张立刻发送一张，避免三张全部渲染完才首图可见；单张 weasyprint 渲染约 1-3s，三张串行合计 3-9s，流式可让首图 1-3s 内到达。
    for idx, (d, lab) in enumerate(i18n._ODYSSEY_DIFFS):
        try:
            t0 = time.monotonic()
            path = await cards_mod._render_card("btd6ody", lambda d=d, lab=lab: cards_mod.odyssey_diff_html(col, d, lab))
            _logger.info("BTD6 远征 %s 渲染 %.2fs -> %s", lab, time.monotonic() - t0, path)
        except Exception:
            _logger.warning("BTD6 远征卡片渲染失败，回退文本消息", exc_info=True)
            await odyssey_cmd.finish(MessageSegment.text(textfmt.odyssey_text(col)))
        if idx < len(i18n._ODYSSEY_DIFFS) - 1:
            await odyssey_cmd.send(MessageSegment.image(Path(path).as_uri()))
        else:
            await odyssey_cmd.finish(MessageSegment.image(Path(path).as_uri()))


@player_cmd.handle()
async def handle_player(event: MessageEvent):
    await nkapi._enforce_cooldown(player_cmd, event, "player", "heavy")
    tokens = event.get_plaintext().split()[1:]
    pid = collect._extract_player_id(" ".join(tokens))
    if not pid:
        await player_cmd.finish("用法：.btd6玩家 <玩家ID>\nID 是排行榜玩家链接末尾的长串十六进制（40+ 位）")
    try:
        col = await collect.collect_player(pid)
    except Exception:
        nkapi._release_cooldown(event, "player")
        _logger.exception("BTD6 玩家档案获取失败")
        await player_cmd.finish("⚠️ 获取 BTD6 玩家档案失败，请稍后再试")
    await cards_mod._send_card(player_cmd, "btd6pl", lambda: cards_mod.player_html(col), lambda: textfmt.player_text(col))


@push_on_cmd.handle()
async def handle_push_on(event: MessageEvent):
    if not is_owner(event):
        await push_on_cmd.finish("❌ 仅机器人主人可开启活动推送")
    gid = getattr(event, "group_id", None)
    if gid is None:
        await push_on_cmd.finish("请在需要推送的群内发送此命令")
    changed = await asyncio.to_thread(push._push_change_group, int(gid), True)
    await push_on_cmd.finish("✅ 本群已开启 BTD6 活动自动推送（竞速/Boss/CT/远征/每日 刷新时推送）" if changed else "本群已在推送列表中")


@push_off_cmd.handle()
async def handle_push_off(event: MessageEvent):
    if not is_owner(event):
        await push_off_cmd.finish("❌ 仅机器人主人可关闭活动推送")
    gid = getattr(event, "group_id", None)
    if gid is None:
        await push_off_cmd.finish("请在群内发送此命令")
    changed = await asyncio.to_thread(push._push_change_group, int(gid), False)
    await push_off_cmd.finish("✅ 本群已关闭 BTD6 活动自动推送" if changed else "本群未在推送列表中")


@push_status_cmd.handle()
async def handle_push_status(event: MessageEvent):
    if not is_owner(event):
        await push_status_cmd.finish("❌ 仅机器人主人可查看推送状态")
    groups = push._push_groups()
    last = push._last_pushed()
    lines = ["📋 BTD6 推送状态"]
    lines.append(f"推送群数：{len(groups)}" + (f"（{', '.join(str(g) for g in sorted(groups))}）" if groups else "（未配置）"))
    if last:
        lines.append("最近推送：")
        for k in push._BTD6_PUSH_KINDS:
            if k in last:
                lines.append(f"  {k}: {last[k][:24]}")
    else:
        lines.append("最近推送：无")
    lines.append("")
    lines.append("命令：.btd6推送开启 / .btd6推送关闭（仅主人）")
    # ev_id 等字段来自 NK API（防 CQ 码注入）：整段经 MessageSegment.text 发送
    await push_status_cmd.finish(MessageSegment.text("\n".join(lines)))


_HIST_KIND_WORDS = {"竞速": "race", "race": "race", "boss": "boss", "首领": "boss",
                    "领土": "ct", "ct": "ct", "争夺": "ct", "远征": "odyssey",
                    "odyssey": "odyssey", "每日": "daily", "daily": "daily"}


@hist_cmd.handle()
async def handle_history(event: MessageEvent):
    """查询本地归档的历史活动：.btd6历史 [竞速|boss|领土|远征|每日] [数量]。
    NK API 各列表只保留近几期（Boss 仅 3 期），本命令读的是预热顺带落盘的 history.json。"""
    await nkapi._enforce_cooldown(hist_cmd, event, "history")
    tokens = event.get_plaintext().split()[1:]
    kind = ""
    for t in tokens:
        k = _HIST_KIND_WORDS.get(t.lower()) or _HIST_KIND_WORDS.get(t)
        if k:
            kind = k
            break
    rows = parse_rows(tokens)
    # 归档文件读取（含磁盘 IO 与锁）放到线程池，避免卡住事件循环
    try:
        hist = await asyncio.to_thread(push._load_history)
    except Exception:
        nkapi._release_cooldown(event, "history")
        raise
    now = int(time.time() * 1000)
    kinds = [kind] if kind else ["race", "boss", "ct", "odyssey", "daily"]
    lines = ["🗂 BTD6 活动历史归档"]
    any_data = False
    for k in kinds:
        items = [x for x in (hist.get(k) or []) if isinstance(x, dict)]
        if not items:
            continue
        any_data = True
        items.sort(key=lambda x: int(x.get("start") or 0), reverse=True)
        lines.append(f"【{push.HISTORY_KIND_CN[k]}】共 {len(items)} 期")
        for ev in items[:rows]:
            s = int(ev.get("start") or 0)
            e = int(ev.get("end") or 0)
            name = str(ev.get("name") or "").strip() or str(ev.get("id") or "?")
            if s and e:
                state = "进行中" if s <= now < e else ("未开始" if now < s else "已结束")
                lines.append(f"  {util.fmt_date(s)} ~ {util.fmt_date(e)} {state} {name}")
            else:
                lines.append(f"  {name}")
    if not any_data:
        lines.append("（归档为空，随预热每轮自动积累）")
    # 活动名等来自 NK API 历史归档（防 CQ 码注入）：整段经 MessageSegment.text 发送
    await hist_cmd.finish(MessageSegment.text("\n".join(lines)))


@prewarm_cmd.handle()
async def handle_prewarm(event: MessageEvent):
    if not is_owner(event):
        await prewarm_cmd.finish("❌ 仅主人可手动预热")
    await nkapi._enforce_cooldown(prewarm_cmd, event, "prewarm", "heavy")
    if push._prewarm_running:
        await prewarm_cmd.finish("⏳ 预热已在进行中，请稍候")
    await prewarm_cmd.send("⏳ 开始手动预热（活动/榜单/素材）...")
    try:
        await push._prewarm_once()
        await cards_mod._render_card("btd6help", cards_mod.help_html)
    except Exception as e:
        from nonebot.exception import FinishedException
        if isinstance(e, FinishedException):
            raise
        _logger.exception("手动预热失败")
        # 异常信息含外部输入内容（防 CQ 码注入）：经 MessageSegment.text 发送
        await prewarm_cmd.finish(MessageSegment.text(f"⚠️ 预热失败: {e}"))
        return
    await prewarm_cmd.finish("✅ 预热完成（活动已归档，热门榜单/帮助已刷新）")
