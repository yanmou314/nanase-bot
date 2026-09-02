"""后台任务层：活动历史归档、数据/卡片预热、活动刷新推送（apscheduler 定时）。"""
import asyncio
import logging
import os
import threading
import time
from pathlib import Path

from nonebot import get_bot, get_driver
from nonebot.adapters.onebot.v11 import MessageSegment
from nonebot_plugin_apscheduler import scheduler

from common import load_json_state, save_json_state

from . import cards, collect, i18n, nkapi, util

_logger = logging.getLogger(__name__)


PREWARM_LEADERBOARD_HOURS = 6   # 榜单卡预热周期（小时）：榜单分数变化快但查询可按需渲染兜底


# ---------------- 活动历史归档（NK 各列表只保留近几期，本地落盘补长历史） ----------------

HISTORY_FILE = os.path.join(os.path.dirname(__file__), "history.json")
HISTORY_MAX_PER_KIND = 500  # 每类最多归档期数（按 start 降序裁剪，单条 ~0.5KB，上限 ~1MB）
HISTORY_KIND_CN = {"race": "竞速", "boss": "Boss", "ct": "争夺领土",
                   "odyssey": "远征", "daily": "每日挑战"}
_history_lock = threading.RLock()


def _load_history() -> dict:
    data = load_json_state(HISTORY_FILE, _history_lock)
    return data if isinstance(data, dict) else {}


def _merge_history(kind: str, items: list) -> bool:
    """按 id 合并该类活动到归档；有新增返回 True。start 降序存储并裁剪上限。"""
    if not items:
        return False
    with _history_lock:
        data = _load_history()
        arc = data.get(kind) if isinstance(data.get(kind), list) else []
        by_id = {str(x.get("id") or ""): x for x in arc if isinstance(x, dict)}
        changed = False
        for it in items:
            if not isinstance(it, dict):
                continue
            iid = str(it.get("id") or "")
            if iid and iid not in by_id:
                by_id[iid] = it
                changed = True
        if not changed:
            return False
        merged = sorted(by_id.values(),
                        key=lambda x: int(x.get("start") or 0), reverse=True)[:HISTORY_MAX_PER_KIND]
        data[kind] = merged
        save_json_state(HISTORY_FILE, data, _history_lock)
        return True


async def _archive_events(data: dict | None = None) -> None:
    """预热顺带归档：把竞速/Boss/CT/远征/每日列表合并进本地 history.json，
    弥补 NK API 只保留近 3~16 期的限制（文件小、增量合并，失败不影响预热）。"""
    try:
        if data is None:
            data = await collect.collect_overview()
        for kind, key in (("race", "races"), ("boss", "bosses"), ("ct", "cts")):
            items = data.get(key) or []
            if items:
                await collect._safe(asyncio.to_thread(_merge_history, kind, items))
        ody = await collect._safe(nkapi.fetch_body(nkapi.URL_ODYSSEY))
        if isinstance(ody, list) and ody:
            await collect._safe(asyncio.to_thread(_merge_history, "odyssey", ody))
        daily = await collect._safe(nkapi.fetch_body(nkapi.URL_DAILY))
        if isinstance(daily, list) and daily:
            await collect._safe(asyncio.to_thread(_merge_history, "daily", daily))
    except Exception:
        _logger.warning("BTD6 活动归档失败", exc_info=True)


# ---------------- 后台预热：数据 + 素材 + 热门卡片 ----------------

_prewarm_running = False


async def _prewarm_once() -> None:
    """周期预热（瘦身后只做两类事，全部容错）：
    1) 归档活动列表到 history.json（复用本轮 overview，仅额外拉远征/每日两个列表）；
    2) 仅当竞赛/Boss/CT 进行中时预热榜单卡，并顺带预热进行中的 Rush/远征卡——
       分数/相对时间变化快且查询最频繁，值得周期渲染。
    总览/规则/每日内容只在刷新点变化，内容哈希缓存保证"首查渲染、后续秒回"，无需预热。"""
    global _prewarm_running
    if _prewarm_running:
        return
    _prewarm_running = True
    try:
        data = await collect.collect_overview()
        now = data["now"]
        await collect._safe(_archive_events(data))
        jobs = await _lb_prewarm_jobs(data)
        # odyssey and rush via events
        rush_list = data.get("rush") or []
        rush_ev = util._pick_section(rush_list, now) if rush_list else None
        ody_list = data.get("odysseys") or []
        ody_ev = util._pick_section(ody_list, now) if ody_list else None
        if rush_ev:
            # 1:1 模仿 handle_odyssey：collect_rush → _rush_diff_html；
            # HTML 构建交给渲染线程执行（html_fn 模式），不在协程内同步构建
            try:
                col = await collect.collect_rush()
                if not col.get("empty"):
                    jobs.append(cards._render_card("btd6rush", lambda: cards._rush_diff_html(col)))
            except Exception:
                _logger.warning("BTD6 预热 Rush 卡渲染失败", exc_info=True)
        if ody_ev:
            # 远征三难度取当前其一预热；与 handle_odyssey 相同注入 _unified_h，
            # 保证预热卡与首次查询命中同一缓存键
            try:
                ody_col = await collect.collect_odyssey()
                if not ody_col.get("empty"):
                    try:
                        _inject_odyssey_unified_h(ody_col)
                    except Exception:
                        _logger.debug("BTD6 预热远征统一高度计算失败，使用各自高度", exc_info=True)
                    jobs.append(cards._render_card("btd6ody", lambda: cards.odyssey_diff_html(ody_col, "easy", "简单")))
            except Exception:
                _logger.warning("BTD6 预热远征卡渲染失败", exc_info=True)
        if util._pick_section(data.get("collectables") or [], now):
            # 收集活动计划表（8 小时轮换 + 15 分钟桶倒计时）同桶查询可直接复用预热卡
            try:
                ce_col = await collect.collect_collectevent(now)
                if not ce_col.get("empty"):
                    jobs.append(cards._render_card("btd6col", lambda: cards.collectevent_html(ce_col)))
            except Exception:
                _logger.warning("BTD6 预热收集活动卡渲染失败", exc_info=True)
        if util._pick_section(data.get("cts") or [], now):
            # CT 地图卡：总览 + 4 张显示预设图；布局数据整周不变，预热后首查秒回
            try:
                ct_col = await collect.collect_ct(now)
                if not ct_col.get("empty"):
                    jobs.append(cards._render_card("btd6ct", lambda: cards.ctmap_html(ct_col)))
                    for name, _label in cards.CT_PRESET_CARDS:
                        jobs.append(cards._render_card(
                            f"btd6ctp_{name}", lambda n=name: cards.ctmap_preset_html(ct_col, n)))
            except Exception:
                _logger.warning("BTD6 预热 CT 地图卡渲染失败", exc_info=True)
        # 逐张渲染并在间隔让出信号量：用户查询优先于预热渲染
        for i, job in enumerate(jobs):
            await collect._safe(job)
            if i < len(jobs) - 1:
                await asyncio.sleep(1.0)
        # 帮助菜单纯静态，兜底再渲一次（首次 _btd6_warm_on_connect 失败时仍能补上）
        if not jobs:
            await collect._safe(cards._render_card("btd6help", cards.help_html))
    except Exception:
        _logger.warning("BTD6 预热异常", exc_info=True)
    finally:
        _prewarm_running = False


def _inject_odyssey_unified_h(col: dict) -> None:
    """与 handle_odyssey 相同的 _unified_h 注入：取三难度最大卡片高度统一画布，
    保证预热渲染与首次查询命中同一缓存键（QQ 按最大边等比缩放，高度不同会导致视觉宽度不一）。"""
    unified = max(cards._odyssey_card_height((col["diffs"].get(d) or {}).get("meta"),
                                             len((col["diffs"].get(d) or {}).get("maps") or []))
                  for d, _lab in i18n._ODYSSEY_DIFFS)
    for d, _lab in i18n._ODYSSEY_DIFFS:
        if d in col["diffs"]:
            col["diffs"][d]["_unified_h"] = unified


async def _lb_prewarm_jobs(data: dict, *, ongoing_only: bool = False) -> list:
    """按 collect_overview 结果为竞赛/Boss/CT 构建榜单卡渲染任务。

    ongoing_only=False（每日 04:00 预热）：与历史行为一致，无进行中活动时回退最近/下一场；
    ongoing_only=True（每小时对齐）：仅进行中的活动参与预热，无则返回空列表跳过。
    数据全部来自既有缓存/既有 fetch_body（SWR），不新增网络请求路径。
    """
    jobs = []
    now = data["now"]
    picker = util.pick_active if ongoing_only else util._pick_section
    race = picker(data["races"], now)
    boss = picker(data["bosses"], now)
    ct = picker(data["cts"], now)
    if race:
        # 行数与用户默认查询参数一致，预热渲染才能命中缓存
        lb = await collect._safe(collect.collect_leaderboard("race", "", nkapi.LB_DEFAULT_ROWS))
        if lb and not lb.get("empty"):
            jobs.append(cards._render_card("btd6lb", lambda: cards.leaderboard_html(lb)))
    if boss:
        blb = await collect._safe(collect.collect_leaderboard("boss", "standard", nkapi.LB_DEFAULT_ROWS))
        if blb and not blb.get("empty"):
            jobs.append(cards._render_card("btd6lb", lambda: cards.leaderboard_html(blb)))
    if ct:
        # CT 榜单预热（个人榜）
        ct_lb = await collect._safe(collect.collect_leaderboard("ct", "player", nkapi.LB_DEFAULT_ROWS))
        if ct_lb and not ct_lb.get("empty"):
            jobs.append(cards._render_card("btd6lb", lambda: cards.leaderboard_html(ct_lb)))
    return jobs


async def _prewarm_daily_cards() -> int:
    """渲染标准+高级每日卡各一张，返回成功张数。

    每日挑战 08:00（Asia/Shanghai）重置，新一天首查原本必然冷渲染
    （跨境取数 + 地图大图 + 每张卡约 40 个塔立绘的重渲染，10 秒级）。
    卡片按内容哈希缓存：内容不变时重复预热近零成本，因此挂进每小时 :10
    的任务即可在重置后最迟 10 分钟自动暖好，用户查询命中缓存。
    """
    done = 0
    for i, (adv, key) in enumerate(((False, "btd6daily"), (True, "btd6dailya"))):
        try:
            col = await collect.collect_daily(adv)
        except Exception:
            _logger.warning("BTD6 每日预热取数失败 adv=%s", adv, exc_info=True)
            continue
        if col.get("empty"):
            continue
        try:
            await cards._render_card(key, lambda c=col: cards.rules_html(c))
            done += 1
        except Exception:
            _logger.warning("BTD6 每日预热渲染失败 adv=%s", adv, exc_info=True)
        if i == 0:
            await asyncio.sleep(1.0)
    return done


# 注意：APScheduler 3.11 的 scheduled_job 装饰器内部恒以 replace_existing=True 注册，
# 显式传该参数会因参数冲突抛 TypeError 使插件导入失败（校验见 test_push_jobs_apscheduler_compat）。
@scheduler.scheduled_job("cron", hour=4, minute=0, id="btd6_prewarm",
                         timezone="Asia/Shanghai")
async def btd6_prewarm_job():
    """每天 04:00 固定预热：归档活动列表 + 预热进行中竞赛/Boss/CT/Rush/远征的热门卡片。
    由每周按需改为每天定点，确保跨周活动切换后首日内即有缓存。"""
    await _prewarm_once()


@scheduler.scheduled_job("cron", minute=10, second=0, id="btd6_prewarm_lb_hourly",
                         timezone="Asia/Shanghai")
async def btd6_prewarm_lb_hourly_job():
    """每小时 :10 错峰预热：进行中活动的榜单卡（race/boss/ct 各一张，无则跳过）
    + 标准/高级每日卡（08:00 每日重置后最迟 10 分钟暖好，内容不变时近零成本）。

    榜单卡含"剩余 X天X小时"相对时间（时间粒度按 15 分钟桶取整），04:00 预热的卡只对
    邻近时段有效；每小时错峰重渲才能让"首查秒回"持续生效。数据全部来自既有缓存/
    既有 fetch_body（SWR），不新增网络请求路径；渲染失败仅 warning 不抛出。"""
    global _prewarm_running
    if _prewarm_running:
        return
    # 全程置位：不置位时本任务可与 04:00 定点预热/手动预热并发，
    # 取数 + HTML 构建 + PIL 缩略图叠加会放大 1.6G 机器的瞬时内存与 CPU
    _prewarm_running = True
    try:
        data = await collect._safe(collect.collect_overview())
        if not data:
            return
        jobs = await _lb_prewarm_jobs(data, ongoing_only=True)
        for i, job in enumerate(jobs):
            await collect._safe(job)
            if i < len(jobs) - 1:
                await asyncio.sleep(1.0)
        await _prewarm_daily_cards()
    except Exception:
        _logger.warning("BTD6 每小时预热异常", exc_info=True)
    finally:
        _prewarm_running = False


# 启动预热：连上 bot 后先跑一轮（榜单/归档即时可用），此后由每日 04:00 定点预热
# 与每小时 :10 的榜单对齐任务（btd6_prewarm_lb_hourly）接管
_register_warmup = getattr(get_driver(), "on_bot_connect", get_driver().on_startup)


@_register_warmup
async def _btd6_warm_on_connect(bot=None) -> None:
    await asyncio.sleep(5)  # 等 NapCat 连接稳定后再拉数据/归档/预热榜单
    # 帮助菜单是纯静态卡片，预先渲染到持久缓存目录，确保 .btd6 / .btd6帮助 首屏直接复用。
    # 与 _prewarm_once 并发：help 不依赖网络/信号量，但走同一 RENDER_SEM 串行化，所以放到
    # _prewarm_once 之后避免抢用户首查的渲染位。
    await _prewarm_once()
    try:
        await cards._render_card("btd6help", cards.help_html)
        _logger.info("BTD6 帮助菜单已预渲染到本地")
    except Exception:
        _logger.warning("BTD6 帮助菜单预渲染失败", exc_info=True)


# ---------------- 活动刷新推送（群自动播报） ----------------
BTD6_PUSH_STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")
_BTD6_PUSH_LOCK = threading.RLock()
_BTD6_PUSH_KINDS = ("race", "boss", "ct", "odyssey", "daily", "rush", "coop")

def _load_push_state() -> dict:
    return load_json_state(BTD6_PUSH_STATE_FILE, _BTD6_PUSH_LOCK)

def _save_push_state(data: dict) -> None:
    save_json_state(BTD6_PUSH_STATE_FILE, data, _BTD6_PUSH_LOCK)

def _push_groups() -> set[int]:
    data = _load_push_state()
    groups = data.get("groups", []) if isinstance(data.get("groups"), list) else []
    out = set()
    for gid in groups:
        try:
            gid = int(gid)
        except (TypeError, ValueError):
            continue
        if gid > 0:
            out.add(gid)
    return out

def _last_pushed() -> dict:
    data = _load_push_state()
    lp = data.get("last_pushed", {}) if isinstance(data.get("last_pushed"), dict) else {}
    return {k: str(v) for k, v in lp.items() if k in _BTD6_PUSH_KINDS}

def _set_last_pushed(kind: str, ev_id: str) -> None:
    with _BTD6_PUSH_LOCK:  # 读改写全程持锁，避免并发推送互相覆盖记录
        data = load_json_state(BTD6_PUSH_STATE_FILE, _BTD6_PUSH_LOCK)
        lp = data.get("last_pushed", {}) if isinstance(data.get("last_pushed"), dict) else {}
        lp[kind] = str(ev_id)
        data["last_pushed"] = lp
        save_json_state(BTD6_PUSH_STATE_FILE, data, _BTD6_PUSH_LOCK)

def _push_change_group(group_id: int, enabled: bool) -> bool:
    with _BTD6_PUSH_LOCK:
        data = load_json_state(BTD6_PUSH_STATE_FILE, _BTD6_PUSH_LOCK)
        groups = data.get("groups", []) if isinstance(data.get("groups"), list) else []
        s = set()
        for gid in groups:
            try:
                s.add(int(gid))
            except (TypeError, ValueError):
                continue
        changed = (group_id not in s) if enabled else (group_id in s)
        if enabled:
            s.add(group_id)
        else:
            s.discard(group_id)
        if changed:
            data["groups"] = sorted(s)
            save_json_state(BTD6_PUSH_STATE_FILE, data, _BTD6_PUSH_LOCK)
        return changed

async def _btd6_push_kind(kind: str) -> None:
    """精准采样：仅检查单类活动是否刚刷新，减少 99% 空轮询（原每5分钟全量检查 288次/日 → 现仅刷新点后3次/类）。"""
    groups = _push_groups()
    if not groups:
        return
    try:
        get_bot()  # 无已连接 bot 时直接跳过本轮采样
    except Exception:
        return
    now = util.bucket_now()
    real_now = int(time.time() * 1000)  # 窗口比较用真实时间：start 带秒级偏移时桶取整会恒判"未在窗口内"而漏推
    # 12 分钟窗口：:10 采样点距刷新点恰为 10min，10min 窗口会漏掉第三次容错采样
    window_ms = 12 * 60 * 1000
    last = _last_pushed()
    # 单类检查（只取列表判断 id/start，重量级元数据/素材留到确认推送后再拉）
    try:
        ev = None
        if kind == "race":
            data = await collect.collect_overview()
            ev = util._pick_section(data["races"], now)
        elif kind == "boss":
            data = await collect.collect_overview()
            ev = util._pick_section(data["bosses"], now)
        elif kind == "ct":
            data = await collect.collect_overview()
            ev = util.pick_active(data["cts"], now) or util.pick_next(data["cts"], now) or util.fallback_latest(data["cts"])
        elif kind == "odyssey":
            items = await collect._safe(nkapi.fetch_body(nkapi.URL_ODYSSEY)) or []
            ev = util.pick_active(items, now) or util.pick_next(items, now) or util.fallback_latest(items)
        elif kind == "rush":
            data = await collect.collect_overview()
            ev = util._pick_section(data.get("rush") or [], now)
        elif kind == "daily":
            items = await collect._safe(nkapi.fetch_body(nkapi.URL_DAILY)) or []
            ev = next((x for x in items if str(x.get("name") or "").startswith("Standard")), None)
        elif kind == "coop":
            # Co-op 挑战与每日挑战同一列表（name 以 coop 开头）；未来排期的条目
            # 元数据未开放，只取 createdAt ≤ 当前的最新一期
            items = await collect._safe(nkapi.fetch_body(nkapi.URL_DAILY)) or []
            ev = collect._coop_pick(items if isinstance(items, list) else [], real_now)
        if not isinstance(ev, dict):
            return
        ev_id = str(ev.get("id") or ev.get("name") or "")
        label = str(ev.get("name") or "")
        if not ev_id or last.get(kind) == ev_id:
            return
        start = int(ev.get("start") or 0)
        if kind in ("daily", "coop"):
            # daily/coop 无 start，以 id 变化即视为刷新
            pass
        elif not start or not (0 <= real_now - start < window_ms):
            # 非窗口期内且非首次配置则跳过；首次配置 30 分钟内补发
            if last.get(kind) or not start or not (0 <= real_now - start < 30 * 60 * 1000):
                return
        # 确认要推送后才拉取重量级数据并渲染发送
        await _btd6_push_single(kind, ev, ev_id, label, groups)
    except Exception:
        _logger.warning("BTD6 精准推送 kind=%s 失败", kind, exc_info=True)

async def _btd6_push_single(kind: str, ev: dict, ev_id: str, label: str, groups: set[int]) -> None:
    try:
        bot = get_bot()
    except Exception:
        return
    try:
        if kind in ("race", "ct", "rush"):
            data = await collect.collect_overview()
            path = await cards._render_card("btd6ov", lambda: cards.overview_html(data))
            # 活动名优先用 i18n 汉化映射（NK 返回英文模板名）；查表用普通语句，
            # 避免 f-string 内嵌字典字面量的 PEP 701 写法（3.12 前为语法错误）
            kind_name = {"race": "竞速", "ct": "争夺领土", "rush": "Boss Rush"}.get(kind, kind)
            event_name = i18n._EVENT_NAME_CN.get((label or "").strip())
            if label:
                text = f"🎮 BTD6 {event_name or kind_name}已刷新：{label}"
            else:
                text = f"🎮 BTD6 {kind_name} 已刷新"
        elif kind == "odyssey":
            col = await collect.collect_odyssey()
            data = await collect.collect_overview()
            path = await cards._render_card("btd6ov", lambda: cards.overview_html(data))
            text = f"🏰 远征已刷新：{label}" if label else "🏰 远征已刷新"
            # 与 handle_odyssey / 预热相同：注入 _unified_h 统一画布高度，
            # 否则推送图 HTML 变化导致渲染缓存必然 miss，且与后续查询图高度不一
            try:
                _inject_odyssey_unified_h(col)
            except Exception:
                _logger.debug("BTD6 推送远征统一高度计算失败，使用各自高度", exc_info=True)
            sent_any = False
            for d, lab in i18n._ODYSSEY_DIFFS:
                try:
                    p = await cards._render_card("btd6ody", lambda d=d, lab=lab: cards.odyssey_diff_html(col, d, lab))
                    for gid in groups:
                        try:
                            await bot.send_group_msg(group_id=gid, message=MessageSegment.image(Path(p).as_uri()))
                            sent_any = True
                            await asyncio.sleep(0.6)
                        except Exception:
                            _logger.warning("BTD6 远征分图推送到群 %s 失败", gid, exc_info=True)
                except Exception:
                    _logger.warning("BTD6 远征 %s 渲染失败", lab, exc_info=True)
            for gid in groups:
                try:
                    await bot.send_group_msg(group_id=gid, message=MessageSegment.text(text) + MessageSegment.image(Path(path).as_uri()))
                    sent_any = True
                    await asyncio.sleep(0.5)
                except Exception:
                    _logger.warning("BTD6 推送到群 %s 失败 kind=%s", gid, kind, exc_info=True)
            if sent_any:
                # 分图与总览任一发送成功即标记，避免部分成功时下轮重发整组刷屏；全部失败时保留待推状态
                await asyncio.to_thread(_set_last_pushed, kind, ev_id)
            _logger.info("BTD6 精准推送 %s %s 到 %d 群（成功标记：%s）", kind, ev_id, len(groups), sent_any)
            return
        elif kind == "daily":
            # 每日普通+高级双版本一起推送；标准/高级是独立编号，
            # 期号前缀必须取自各次 collect_daily 返回的真实事件名，不能复用外层 label
            pushed_any = False
            for adv in (False, True):
                col = await collect._safe(collect.collect_daily(adv), "daily_push")
                if not col or col.get("empty"):
                    continue
                path = await cards._render_card("btd6dailya" if adv else "btd6daily", lambda c=col: cards.rules_html(c))
                text = f"📅 每日挑战已刷新·{col.get('prefix') or collect._daily_prefix(label, adv)}"
                for gid in groups:
                    try:
                        await bot.send_group_msg(group_id=gid, message=MessageSegment.text(text) + MessageSegment.image(Path(path).as_uri()))
                        pushed_any = True
                        await asyncio.sleep(0.6)
                    except Exception:
                        _logger.warning("BTD6 推送到群 %s 失败 kind=%s adv=%s", gid, kind, adv, exc_info=True)
            if pushed_any:
                # 至少一个群发送成功才标记已推送；全部失败时保留待推状态，下次采样重试
                await asyncio.to_thread(_set_last_pushed, kind, ev_id)
                _logger.info("BTD6 精准推送 %s %s 到 %d 群", kind, ev_id, len(groups))
            return
        elif kind == "boss":
            # Boss 标准+精英双版本：先推总览，再推两套详细规则
            data = await collect.collect_overview()
            path = await cards._render_card("btd6ov", lambda: cards.overview_html(data))
            text = f"🎮 BTD6 Boss已刷新：{label}" if label else "🎮 BTD6 Boss 已刷新"
            sent_any = False
            for gid in groups:
                try:
                    await bot.send_group_msg(group_id=gid, message=MessageSegment.text(text) + MessageSegment.image(Path(path).as_uri()))
                    sent_any = True
                    await asyncio.sleep(0.5)
                except Exception:
                    _logger.warning("BTD6 推送到群 %s 失败 kind=%s", gid, kind, exc_info=True)
            for variant, vlab in [("standard", "标准"), ("elite", "精英")]:
                col = await collect._safe(collect.collect_rules("boss", variant))
                if not col or col.get("empty"):
                    continue
                p2 = await cards._render_card(f"btd6rule_{variant}", lambda c=col: cards.rules_html(c))
                vt = f"Boss·{vlab}规则：{label}" if label else f"Boss·{vlab}规则"
                for gid in groups:
                    try:
                        await bot.send_group_msg(group_id=gid, message=MessageSegment.text(vt) + MessageSegment.image(Path(p2).as_uri()))
                        sent_any = True
                        await asyncio.sleep(0.6)
                    except Exception:
                        _logger.warning("BTD6 Boss %s 推送到群 %s 失败", vlab, gid, exc_info=True)
            if sent_any:
                # 全部发送失败时不标记，下次采样窗口重试
                await asyncio.to_thread(_set_last_pushed, kind, ev_id)
                _logger.info("BTD6 精准推送 %s %s 到 %d 群", kind, ev_id, len(groups))
            return
        elif kind == "coop":
            # Co-op 挑战单卡推送；名称取 metadata.name（已无 "coop - " 前缀）
            col = await collect._safe(collect.collect_daily_coop(), "coop_push")
            if not col or col.get("empty"):
                return
            path = await cards._render_card("btd6coop", lambda c=col: cards.rules_html(c))
            coop_name = str((col.get("meta") or {}).get("name") or "").strip()
            text = f"🤝 Co-op 挑战已刷新：{coop_name}" if coop_name else "🤝 Co-op 挑战已刷新"
        else:
            return
        sent_any = False
        for gid in groups:
            try:
                await bot.send_group_msg(group_id=gid, message=MessageSegment.text(text) + MessageSegment.image(Path(path).as_uri()))
                sent_any = True
                await asyncio.sleep(0.5)
            except Exception:
                _logger.warning("BTD6 推送到群 %s 失败 kind=%s", gid, kind, exc_info=True)
        if sent_any:
            # 至少一个群发送成功才标记已推送；全部失败时下次采样重试
            await asyncio.to_thread(_set_last_pushed, kind, ev_id)
        _logger.info("BTD6 精准推送 %s %s 到 %d 群（成功标记：%s）", kind, ev_id, len(groups), sent_any)
    except Exception:
        _logger.warning("BTD6 推送 kind=%s 失败", kind, exc_info=True)


# 精准采样：已知刷新点后 0/5/10 分钟各一次（3 次容错，覆盖 API 延迟）
# 竞速 周四10:00 持续97h（second 偏移错峰：同小时 race/boss/odyssey 依次 0/20/40 秒触发，
# 避免三个采样任务整点并发冷启动拉取全量 API + 渲染）
@scheduler.scheduled_job("cron", hour=10, minute=0, second=0, id="btd6_push_race_0", timezone="Asia/Shanghai")
async def btd6_push_race_0(): await _btd6_push_kind("race")
@scheduler.scheduled_job("cron", hour=10, minute=5, second=0, id="btd6_push_race_5", timezone="Asia/Shanghai")
async def btd6_push_race_5(): await _btd6_push_kind("race")
@scheduler.scheduled_job("cron", hour=10, minute=10, second=0, id="btd6_push_race_10", timezone="Asia/Shanghai")
async def btd6_push_race_10(): await _btd6_push_kind("race")
# Boss 周五10:00 持续121h
@scheduler.scheduled_job("cron", hour=10, minute=0, second=20, id="btd6_push_boss_0", timezone="Asia/Shanghai")
async def btd6_push_boss_0(): await _btd6_push_kind("boss")
@scheduler.scheduled_job("cron", hour=10, minute=5, second=20, id="btd6_push_boss_5", timezone="Asia/Shanghai")
async def btd6_push_boss_5(): await _btd6_push_kind("boss")
@scheduler.scheduled_job("cron", hour=10, minute=10, second=20, id="btd6_push_boss_10", timezone="Asia/Shanghai")
async def btd6_push_boss_10(): await _btd6_push_kind("boss")
# CT 周二06:00 持续168h（双周刷新，样本含周三08:00 特殊场，兼顾）+ 周三08:00 兜底
@scheduler.scheduled_job("cron", hour=6, minute=0, second=0, id="btd6_push_ct_0", timezone="Asia/Shanghai")
async def btd6_push_ct_0(): await _btd6_push_kind("ct")
@scheduler.scheduled_job("cron", hour=6, minute=5, second=0, id="btd6_push_ct_5", timezone="Asia/Shanghai")
async def btd6_push_ct_5(): await _btd6_push_kind("ct")
@scheduler.scheduled_job("cron", hour=6, minute=10, second=0, id="btd6_push_ct_10", timezone="Asia/Shanghai")
async def btd6_push_ct_10(): await _btd6_push_kind("ct")
@scheduler.scheduled_job("cron", hour=8, minute=0, id="btd6_push_ct_w0", timezone="Asia/Shanghai")
async def btd6_push_ct_w0(): await _btd6_push_kind("ct")
@scheduler.scheduled_job("cron", hour=8, minute=5, id="btd6_push_ct_w5", timezone="Asia/Shanghai")
async def btd6_push_ct_w5(): await _btd6_push_kind("ct")
@scheduler.scheduled_job("cron", hour=8, minute=10, id="btd6_push_ct_w10", timezone="Asia/Shanghai")
async def btd6_push_ct_w10(): await _btd6_push_kind("ct")
# 远征 周三10:00 持续144h
@scheduler.scheduled_job("cron", hour=10, minute=0, second=40, id="btd6_push_ody_0", timezone="Asia/Shanghai")
async def btd6_push_ody_0(): await _btd6_push_kind("odyssey")
@scheduler.scheduled_job("cron", hour=10, minute=5, second=40, id="btd6_push_ody_5", timezone="Asia/Shanghai")
async def btd6_push_ody_5(): await _btd6_push_kind("odyssey")
@scheduler.scheduled_job("cron", hour=10, minute=10, second=40, id="btd6_push_ody_10", timezone="Asia/Shanghai")
async def btd6_push_ody_10(): await _btd6_push_kind("odyssey")

# Boss Rush 隔周三 22:00 UTC -> 周四 06:00 CST，与 CT 交替，改为每天 06:00 定点检测
# （hour=6 与 ct 同小时：ct=0 秒 / rush=20 秒 错峰）
@scheduler.scheduled_job("cron", hour=6, minute=0, second=20, id="btd6_push_rush_0", timezone="Asia/Shanghai")
async def btd6_push_rush_0(): await _btd6_push_kind("rush")
@scheduler.scheduled_job("cron", hour=6, minute=5, second=20, id="btd6_push_rush_5", timezone="Asia/Shanghai")
async def btd6_push_rush_5(): await _btd6_push_kind("rush")
@scheduler.scheduled_job("cron", hour=6, minute=10, second=20, id="btd6_push_rush_10", timezone="Asia/Shanghai")
async def btd6_push_rush_10(): await _btd6_push_kind("rush")

# 每日 16:00 持续24h（按用户指定，普通+高级双版本）
@scheduler.scheduled_job("cron", hour=16, minute=0, id="btd6_push_daily_0", timezone="Asia/Shanghai")
async def btd6_push_daily_0(): await _btd6_push_kind("daily")
@scheduler.scheduled_job("cron", hour=16, minute=5, id="btd6_push_daily_5", timezone="Asia/Shanghai")
async def btd6_push_daily_5(): await _btd6_push_kind("daily")
@scheduler.scheduled_job("cron", hour=16, minute=10, id="btd6_push_daily_10", timezone="Asia/Shanghai")
async def btd6_push_daily_10(): await _btd6_push_kind("daily")

# Co-op 挑战与每日挑战同一 16:00 CST 刷新点（每 3~4 天一期），错峰 30 秒采样；
# 独立推送标记（last_pushed.coop），coop 变期而每日未变时也能准点推送
@scheduler.scheduled_job("cron", hour=16, minute=0, second=30, id="btd6_push_coop_0", timezone="Asia/Shanghai")
async def btd6_push_coop_0(): await _btd6_push_kind("coop")
@scheduler.scheduled_job("cron", hour=16, minute=5, second=30, id="btd6_push_coop_5", timezone="Asia/Shanghai")
async def btd6_push_coop_5(): await _btd6_push_kind("coop")
@scheduler.scheduled_job("cron", hour=16, minute=10, second=30, id="btd6_push_coop_10", timezone="Asia/Shanghai")
async def btd6_push_coop_10(): await _btd6_push_kind("coop")
