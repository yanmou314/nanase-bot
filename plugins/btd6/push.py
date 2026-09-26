"""后台任务层：活动历史归档、数据/卡片预热、活动刷新推送（apscheduler 定时）。"""
import asyncio
import logging
import os
import threading
import time
from pathlib import Path

from nonebot import get_bot, get_driver, on_command
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent, MessageSegment
from nonebot_plugin_apscheduler import scheduler

from common import is_owner, load_json_state, save_json_state

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
                    jobs.append(cards._render_card("btd6ody", lambda: cards.odyssey_html(ody_col)))
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

    每日挑战 00:20（Asia/Shanghai，NK 实测 16:20 UTC）切换，新一期首查原本必然冷渲染
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


async def _prewarm_coop_card() -> bool:
    """预热 Co-op 挑战卡（与每日同源接口，期号变更是按 createdAt 的独立节奏）。

    每日卡预热不覆盖 Co-op：coop 有独立 id/推送标记，首查原本冷渲染。
    """
    try:
        col = await collect.collect_daily_coop()
    except Exception:
        _logger.warning("BTD6 Co-op 预热取数失败", exc_info=True)
        return False
    if not col or col.get("empty"):
        return False
    try:
        await cards._render_card("btd6coop", lambda c=col: cards.rules_html(c))
        return True
    except Exception:
        _logger.warning("BTD6 Co-op 预热渲染失败", exc_info=True)
        return False


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
    + 标准/高级每日卡 + Co-op 卡（00:20 每日切换后最迟约 50 分钟暖好，内容不变时近零成本）。

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
        await _prewarm_coop_card()
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
    # 连接后全类补检：停机超过 misfire 宽限(1h)跨过刷新点时不漏推（此前只补 CT，
    # daily/coop 最长漏 24h）。各类有 last_pushed 去重，重复调用幂等。
    for _kind in _BTD6_PUSH_KINDS:
        try:
            await _btd6_push_kind(_kind)
        except Exception:
            _logger.warning("BTD6 连接后 %s 补检失败", _kind, exc_info=True)


# ---------------- 活动刷新推送（群自动播报） ----------------
BTD6_PUSH_STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")
_BTD6_PUSH_LOCK = threading.RLock()
_BTD6_PUSH_KINDS = ("race", "boss", "ct", "odyssey", "daily", "rush", "coop", "social")

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

async def _fetch_push_event(kind: str, now: int, real_now: int):
    """单类取当前事件（定时采样与手动检查共用）；无进行中返回 None。

    各类只取自己需要的列表接口（collect_overview 一次拉 5 个列表，采样场景纯浪费；
    fetch_body 有 TTL 缓存，同一刷新点相邻采样任务也不会重复打 API）。
    """
    if kind == "race":
        items = await collect._safe(nkapi.fetch_body(nkapi.URL_RACES), "push_race")
        return util._pick_section(items if isinstance(items, list) else [], now)
    if kind == "boss":
        items = await collect._safe(nkapi.fetch_body(nkapi.URL_BOSSES), "push_boss")
        return util._pick_section(items if isinstance(items, list) else [], now)
    if kind == "ct":
        items = await collect._safe(nkapi.fetch_body(nkapi.URL_CT), "push_ct")
        items = items if isinstance(items, list) else []
        return util.pick_active(items, now) or util.pick_next(items, now) or util.fallback_latest(items)
    if kind == "odyssey":
        # 远征列表独立接口（与归档/预热同一数据源）
        items = await collect._safe(nkapi.fetch_body(nkapi.URL_ODYSSEY), "push_odyssey")
        return util._pick_section(items if isinstance(items, list) else [], now)
    if kind == "rush":
        items = await collect._safe(nkapi.fetch_body(nkapi.URL_EVENTS), "push_rush")
        rush_list = [e for e in (items if isinstance(items, list) else [])
                     if isinstance(e, dict) and e.get("type") == "bossRush"]
        return util._pick_section(rush_list, now)
    if kind == "social":
        # 社交赛季与 Boss Rush 同源 /btd6/events，按 type 拆分；
        # 刷新点不固定，只取进行中的一期（不取 next/latest，避免误推未开始/已结束）
        items = await collect._safe(nkapi.fetch_body(nkapi.URL_EVENTS), "push_social")
        social_list = [e for e in (items if isinstance(items, list) else [])
                       if isinstance(e, dict) and e.get("type") == "socialseason"]
        return util.pick_active(social_list, now)
    if kind == "daily":
        items = await collect._safe(nkapi.fetch_body(nkapi.URL_DAILY)) or []
        # 与 collect_daily 同口径：按 16:00 CST 刷新日选期（不看 createdAt）
        return collect._daily_pick(items if isinstance(items, list) else [],
                                   "Standard", real_now)
    if kind == "coop":
        # Co-op 挑战与每日挑战同一列表（name 以 coop 开头）；未来排期的条目
        # 元数据未开放，只取 createdAt ≤ 当前的最新一期
        items = await collect._safe(nkapi.fetch_body(nkapi.URL_DAILY)) or []
        return collect._coop_pick(items if isinstance(items, list) else [], real_now)
    return None


async def _btd6_push_kind(kind: str) -> None:
    """精准采样：仅检查单类活动是否刚刷新；命中则入队，由防抖批量统一渲染后发送。

    原每 5 分钟全量检查 288 次/日 → 现仅刷新点后 3 次/类。同小时多类（如每日+Coop）
    错峰采样时先进队列，等防抖窗口结束后一起处理，避免总览卡重复发送。
    """
    groups = await asyncio.to_thread(_push_groups)
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
    last = await asyncio.to_thread(_last_pushed)
    try:
        ev = await _fetch_push_event(kind, now, real_now)
        if not isinstance(ev, dict):
            return
        ev_id = str(ev.get("id") or ev.get("name") or "")
        label = str(ev.get("name") or "")
        if not ev_id or last.get(kind) == ev_id:
            return
        start = int(ev.get("start") or 0)
        if kind in ("daily", "coop", "social"):
            # daily/coop 无 start，以 id 变化即视为刷新；
            # social 刷新点不固定，只取进行中活动，同样按 id 去重，不套 12 分钟窗口
            pass
        elif not start or not (0 <= real_now - start < window_ms):
            # 非窗口期：已推过同 id 直接跳过；未推过且进行中/首次 30 分钟内补发
            #（覆盖错过刷新点、bot 当时离线或渲染失败后未标记的情况）
            if last.get(kind):
                return
            in_first = bool(start) and 0 <= real_now - start < 30 * 60 * 1000
            ongoing = util._state_of(ev, real_now) == "on"
            if not (in_first or ongoing):
                return
        await _enqueue_push(kind, ev, ev_id, label)
    except Exception:
        _logger.warning("BTD6 精准推送 kind=%s 失败", kind, exc_info=True)


# ---------------- 批量推送：先渲染完再发；总览只发一次且在详情之前 ----------------

# 同小时错峰采样最大约 40 秒（如 race:00 / boss:20 / odyssey:40），防抖窗口盖住即可合并
_PUSH_BATCH_DELAY_S = 70.0
_PENDING_BATCH_CAP = 32  # 防御：异常时缓冲不无限涨
_pending_batch: dict[str, tuple[dict, str, str]] = {}  # kind -> (ev, ev_id, label)
_batch_lock = threading.RLock()  # 与 state 同风格；跨线程仅保护 dict 读写
_batch_flush_task: asyncio.Task | None = None
_flush_in_progress = False  # 防抖任务已进入 flush 时不可 cancel，避免发送中途被打断
# 总览已发出但详情失败的 kind：重试时只补详情，避免总览刷两遍
_overview_already_sent: set[str] = set()
_OVERVIEW_KINDS = frozenset({"race", "boss", "ct", "odyssey", "rush"})
# 单期重试预算与按群送达记账：详情失败只补发失败群；同一期连续多轮不完整送达则
# 放弃本轮（交还采样层/手动补推），避免一个坏群让全部健康群每 70s 重复收推送
_PUSH_RETRY_MAX = 3
_batch_retries: dict[tuple[str, str], int] = {}  # (kind, ev_id) -> 已失败轮数
_detail_delivered: dict[tuple[str, str], set[int]] = {}  # (kind, ev_id) -> 已收到详情的群


def _kind_display_name(kind: str, label: str) -> str:
    kind_name = {"race": "竞速", "ct": "争夺领土", "rush": "Boss Rush",
                 "boss": "Boss", "odyssey": "远征", "daily": "每日挑战",
                 "coop": "Co-op", "social": "社交赛季"}.get(kind, kind)
    if label:
        event_name = i18n._EVENT_NAME_CN.get(label.strip())
        return event_name or kind_name
    return kind_name


async def _enqueue_push(kind: str, ev: dict, ev_id: str, label: str) -> None:
    """把待推活动放进批量缓冲；若已有防抖计时则并入，否则挂新计时。"""
    global _batch_flush_task
    with _batch_lock:
        _pending_batch[kind] = (ev, ev_id, label)
        if len(_pending_batch) > _PENDING_BATCH_CAP:
            # 只保留最近写入的（dict 插入序），异常积压时不无限涨内存
            drop = list(_pending_batch.keys())[: len(_pending_batch) - _PENDING_BATCH_CAP]
            for k in drop:
                _pending_batch.pop(k, None)
        # 社季几乎不会与其他类并批，单独命中时立即发送，避免固定多等 70s
        social_alone = kind == "social" and len(_pending_batch) == 1
    if _flush_in_progress:
        return  # flush 进行中：条目留在缓冲，flush 结束的 leftover 检测统一补挂，防两轮并发
    if social_alone or _PUSH_BATCH_DELAY_S <= 0:
        await _flush_push_batch()
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        await _flush_push_batch()
        return
    # 仅取消仍在 sleep 的计时器；flush 进行中则不打断，新条目留给本轮结束后
    # 的剩余检测或下一轮采样（flush 开头会 snapshot+clear，新条目会留在缓冲）。
    if _batch_flush_task is not None and not _batch_flush_task.done() and not _flush_in_progress:
        _batch_flush_task.cancel()
    _batch_flush_task = loop.create_task(_delayed_flush())


async def _delayed_flush() -> None:
    try:
        await asyncio.sleep(_PUSH_BATCH_DELAY_S)
        await _flush_push_batch()
    except asyncio.CancelledError:
        # 被更新的采样顶替：新任务会带着更全的批次再 flush
        pass


def _resolve_overview_path(kind: str, shared_ov: str | None) -> str | None:
    """批量场景下本类总览策略：已发过→跳过("")；有共享图→用共享；否则自渲(None)。"""
    if kind not in _OVERVIEW_KINDS:
        return None
    if kind in _overview_already_sent:
        return ""
    return shared_ov


async def _render_kind_payload(kind: str, ev: dict, label: str,
                               overview_path: str | None = None) -> dict | None:
    """渲染单类活动的全部卡片（不发送）。

    overview_path:
      None  — 本类需要总览且自行渲染
      ""    — 跳过总览（本批/此前已发过目录，重试只补详情）
      str   — 使用共享总览路径
    返回:
      overview: 总览卡路径（race/boss/ct/odyssey/rush）；daily/coop/social 为 None
      announce: 与总览一起发出的刷新文案；无总览时为 ""
      details:  [(text, image_path|None), ...] 总览之后的详情消息
    """
    try:
        if kind in ("race", "boss", "ct", "rush"):
            if overview_path == "":
                path = None
            elif overview_path is None:
                data = await collect.collect_overview()
                path = await cards._render_card("btd6ov", lambda: cards.overview_html(data))
            else:
                path = overview_path
            text = f"🎮 BTD6 {_kind_display_name(kind, label)}已刷新：{label}" if label \
                else f"🎮 BTD6 {_kind_display_name(kind, '')} 已刷新"
            details: list[tuple[str, str | None]] = []
            if kind == "boss":
                col = await collect._safe(collect.collect_boss_dual())
                if col and not col.get("empty"):
                    p2 = await cards._render_card(
                        "btd6rule_dual", lambda c=col: cards.boss_dual_html(c))
                    vt = f"Boss规则：{label}" if label else "Boss规则"
                    details.append((vt, p2))
            if kind == "ct":
                col = await collect._safe(collect.collect_ct())
                if col and not col.get("empty"):
                    p2 = await cards._render_card(
                        "btd6ct", lambda c=col: cards.ctmap_html(c))
                    vt = f"争夺领土地图：{label}" if label else "争夺领土地图"
                    details.append((vt, p2))
            return {"overview": path, "announce": text, "label": label, "details": details}
        if kind == "odyssey":
            if overview_path == "":
                path = None
            elif overview_path is None:
                data = await collect.collect_overview()
                path = await cards._render_card("btd6ov", lambda: cards.overview_html(data))
            else:
                path = overview_path
            text = f"🏰 远征已刷新：{label}" if label else "🏰 远征已刷新"
            col = await collect.collect_odyssey()
            if not col or col.get("empty"):
                # 无有效远征数据：不能只靠总览把本期标成已推
                return None
            try:
                p = await cards._render_card("btd6ody", lambda: cards.odyssey_html(col))
                p = await asyncio.to_thread(cards.trim_odyssey_png, p)
                details = [("远征", p)]
            except Exception:
                _logger.warning("BTD6 远征合卡渲染失败", exc_info=True)
                return None
            return {"overview": path, "announce": text, "label": label, "details": details}
        if kind == "daily":
            # 标准+高级合并为一张 bdual 风格卡（参考 Boss 双面板版式）
            col = await collect._safe(collect.collect_daily_dual(), "daily_push")
            if not col or col.get("empty"):
                return None
            p = await cards._render_card("btd6daily", lambda c=col: cards.daily_dual_html(c))
            t = f"📅 每日挑战已刷新·{col.get('announce') or '标准+高级'}"
            return {"overview": None, "announce": "", "label": label, "details": [(t, p)]}
        if kind == "coop":
            col = await collect._safe(collect.collect_daily_coop(), "coop_push")
            if not col or col.get("empty"):
                return None
            p = await cards._render_card("btd6coop", lambda c=col: cards.rules_html(c))
            coop_name = str((col.get("meta") or {}).get("name") or "").strip()
            t = f"🤝 Co-op 挑战已刷新：{coop_name}" if coop_name else "🤝 Co-op 挑战已刷新"
            return {"overview": None, "announce": "", "label": label, "details": [(t, p)]}
        if kind == "social":
            # 社交赛季：无独立规则卡/榜单，只推一条文本（名称+起止日期）
            name = (label or "").strip() or str(ev.get("name") or "").strip() or "社交赛季"
            start, end = int(ev.get("start") or 0), int(ev.get("end") or 0)
            t = f"🤝 社交赛季已开启：{name}"
            if start and end:
                t += f"（{util.fmt_date(start)} - {util.fmt_date(end)}）"
            return {"overview": None, "announce": "", "label": label, "details": [(t, None)]}
    except Exception:
        _logger.warning("BTD6 推送渲染 kind=%s 失败", kind, exc_info=True)
        return None
    return None


async def _send_push_batch(payloads: list[tuple[str, str, dict]], groups: set[int],
                           delivered: dict[str, set[int]] | None = None) -> dict:
    """先发总览目录（至多一张），再发各类详情；全部渲染完成后才开始发送。

    delivered: {"kind:消息序号": 已收到该条详情的群}——重试轮据此跳过已送达的
    消息，一个坏群不再让其余订阅群每轮重收全部详情。就地更新。

    返回 per-kind 结果，供调用方只标记真正推出去的类：
      overview_ok: 总览消息是否至少在一个群发出
      detail_ok:   {kind: 该类详情是否至少一条发出}
    """
    if delivered is None:
        delivered = {}
    empty = {"overview_ok": False, "detail_ok": {k: False for k, _i, _p in payloads}}
    try:
        bot = get_bot()
    except Exception:
        return empty
    if not payloads or not groups:
        return empty
    overview_path = next((p.get("overview") for _k, _i, p in payloads if p.get("overview")), None)
    ov_payloads = [p for _k, _i, p in payloads if p.get("overview")]
    if overview_path:
        if len(ov_payloads) == 1:
            ov_text = ov_payloads[0].get("announce") or ""
        else:
            # 多类同时刷新：合并为一条目录文案，总览图只发一次
            short = "、".join(
                _kind_display_name(k, str(p.get("label") or ""))
                for k, _i, p in payloads if p.get("overview")
            )
            ov_text = f"🎮 BTD6 活动已刷新：{short}"
    else:
        ov_text = ""
    # 稳定顺序，便于群友阅读
    order = {k: i for i, k in enumerate(_BTD6_PUSH_KINDS)}
    payloads = sorted(payloads, key=lambda x: order.get(x[0], 99))
    # (text, path, owner_kind)；owner_kind=None 表示共享总览
    messages: list[tuple[str, str | None, str | None, int]] = []
    if overview_path:
        messages.append((ov_text, overview_path, None, 0))
    per_kind_idx: dict[str, int] = {}
    for kind, _ev_id, p in payloads:
        for text, path in p.get("details") or []:
            messages.append((text, path, kind, per_kind_idx.get(kind, 0)))
            per_kind_idx[kind] = per_kind_idx.get(kind, 0) + 1
    if not messages:
        return empty
    detail_ok: dict[str, bool] = {k: False for k, _i, _p in payloads}
    overview_ok = False
    # 按群记账：只有每个目标群都成功发出，才算完整送达，避免任一群成功即写 last_pushed
    detail_all: dict[str, bool] = {k: True for k, _i, _p in payloads}
    overview_all = True
    for gid in groups:
        gid_detail_ok: dict[str, bool] = {k: False for k, _i, _p in payloads}
        gid_overview_ok = False
        for text, path, owner, owner_idx in messages:
            msg_key = None if owner is None else f"{owner}:{owner_idx}"
            if msg_key is not None and gid in delivered.get(msg_key, set()):
                gid_detail_ok[owner] = True  # 重试轮：该群已收过本条详情，跳过但计入送达
                continue
            try:
                if path and text:
                    msg = MessageSegment.text(text) + MessageSegment.image(Path(path).as_uri())
                elif path:
                    msg = MessageSegment.image(Path(path).as_uri())
                else:
                    msg = MessageSegment.text(text or "")
                await bot.send_group_msg(group_id=gid, message=msg)
                if owner is None:
                    overview_ok = True
                    gid_overview_ok = True
                else:
                    detail_ok[owner] = True
                    gid_detail_ok[owner] = True
                    delivered.setdefault(msg_key, set()).add(gid)
                await asyncio.sleep(0.5)
            except Exception:
                _logger.warning("BTD6 推送到群 %s 失败 owner=%s", gid, owner, exc_info=True)
        for k in detail_all:
            if not gid_detail_ok.get(k):
                detail_all[k] = False
        if not gid_overview_ok:
            overview_all = False
    return {
        "overview_ok": overview_ok,
        "detail_ok": detail_ok,
        "overview_all": overview_all,
        "detail_all": detail_all,
    }


def _kind_push_ok(kind: str, payload: dict, result: dict) -> bool:
    """该 kind 是否算「完整推送成功」——只有全部目标群成功才允许写 last_pushed。

    - 有详情（Boss 规则 / 远征分图 / 每日 / Co-op / 社季）：每个目标群都至少发出一条详情
    - 纯总览（竞速 / CT / Rush）：每个目标群都发出总览
    - 总览已发但详情全失败：不算成功，重试时跳过总览只补详情
    """
    details = payload.get("details") or []
    if details:
        return bool(result.get("detail_all", result.get("detail_ok", {})).get(kind))
    if payload.get("overview"):
        return bool(result.get("overview_all", result.get("overview_ok")))
    return False


async def _flush_push_batch() -> None:
    """取出缓冲中的全部待推活动：统一渲染 → 统一发送 → 按 kind 成功才标记。"""
    global _flush_in_progress, _batch_flush_task
    if _flush_in_progress:
        return  # 已有 flush 在跑：新条目由其结束后的 leftover 检测统一补挂
    _flush_in_progress = True
    with _batch_lock:
        pending = dict(_pending_batch)
        _pending_batch.clear()
    if not pending:
        _flush_in_progress = False
        return
    groups = await asyncio.to_thread(_push_groups)
    if not groups:
        with _batch_lock:
            for k, v in pending.items():
                _pending_batch.setdefault(k, v)
        _flush_in_progress = False
        return
    try:
        get_bot()
    except Exception:
        # bot 不可用：整批放回，等下轮采样或手动补推
        with _batch_lock:
            for k, v in pending.items():
                _pending_batch.setdefault(k, v)
        _flush_in_progress = False
        return
    try:
        # 需要总览的类型共享同一张渲染结果，避免两类同时刷新时渲/发两遍目录
        need_shared_ov = any(
            k in _OVERVIEW_KINDS and k not in _overview_already_sent for k in pending)
        shared_ov: str | None = None
        if need_shared_ov:
            try:
                ov_data = await collect.collect_overview()
                shared_ov = await cards._render_card(
                    "btd6ov", lambda: cards.overview_html(ov_data))
            except Exception:
                _logger.warning("BTD6 批量推送总览渲染失败，各类回退自渲", exc_info=True)
                shared_ov = None
        payloads: list[tuple[str, str, dict]] = []
        render_failed: list[str] = []
        for kind in _BTD6_PUSH_KINDS:
            if kind not in pending:
                continue
            ev, ev_id, label = pending[kind]
            payload = await _render_kind_payload(
                kind, ev, label,
                overview_path=_resolve_overview_path(kind, shared_ov))
            if payload:
                payloads.append((kind, ev_id, payload))
            else:
                render_failed.append(kind)
        # 渲染失败：放回缓冲，交给后续采样/防抖重试，不静默丢弃
        if render_failed:
            with _batch_lock:
                for kind in render_failed:
                    if kind in pending:
                        _pending_batch.setdefault(kind, pending[kind])
            _logger.warning("BTD6 批量推送渲染失败已回填 kinds=%s", render_failed)
        if not payloads:
            # 渲染全失败：条目已回填缓冲，仍要走 leftover 补挂，否则要等下一轮采样点
            if _PUSH_BATCH_DELAY_S > 0:
                try:
                    _batch_flush_task = asyncio.get_running_loop().create_task(_delayed_flush())
                except RuntimeError:
                    pass
            return
        # 按群送达记账（键 "kind:消息序号"）：重试轮只补发未收到的消息，健康群不重复收
        delivered: dict[str, set[int]] = {}
        for kind, ev_id, _p in payloads:
            for mk, gids in (_detail_delivered.get((kind, ev_id)) or {}).items():
                delivered.setdefault(mk, set()).update(gids)
        result = await _send_push_batch(payloads, groups, delivered)
        for kind, ev_id, _p in payloads:
            mine = {mk: gids for mk, gids in delivered.items() if mk.startswith(kind + ":")}
            if mine:
                _detail_delivered[(kind, ev_id)] = mine
        if len(_detail_delivered) > 256:
            for k in list(_detail_delivered)[: len(_detail_delivered) - 256]:
                _detail_delivered.pop(k, None)
        to_mark: list[tuple[str, str]] = []
        to_give_up: list[str] = []
        to_retry: list[str] = []
        for kind, ev_id, p in payloads:
            if _kind_push_ok(kind, p, result):
                to_mark.append((kind, ev_id))
                if kind in _OVERVIEW_KINDS:
                    _overview_already_sent.add(kind)
            else:
                n = _batch_retries.get((kind, ev_id), 0) + 1
                _batch_retries[(kind, ev_id)] = n
                if n > _PUSH_RETRY_MAX:
                    to_give_up.append(kind)
                else:
                    to_retry.append(kind)
                # 总览已发出且本类还有详情可补：标记 skip，重试只补详情。
                # 纯总览类（race/rush）无详情，重试必须重发总览——否则重试轮
                # messages 为空恒判失败直至 give-up，且标记滞留会让后续期次永久静默。
                if result.get("overview_ok") and kind in _OVERVIEW_KINDS and payload.get("details"):
                    _overview_already_sent.add(kind)
        for kind, ev_id in to_mark:
            await asyncio.to_thread(_set_last_pushed, kind, ev_id)
            # 本期已完成，下一期新 id 再重新渲染总览
            _overview_already_sent.discard(kind)
            _batch_retries.pop((kind, ev_id), None)
            _detail_delivered.pop((kind, ev_id), None)
        if to_give_up:
            for kind in to_give_up:
                # 放弃本期必须清总览跳过标记：否则纯总览类下一期被永久跳过且无详情可补
                _overview_already_sent.discard(kind)
            _logger.error(
                "BTD6 批量推送连续 %d 轮未完整送达，本轮放弃 kinds=%s"
                "（多半是某个订阅群已不可达；可用 .btd6推送检查 手动补推）",
                _PUSH_RETRY_MAX, to_give_up)
        if to_retry:
            with _batch_lock:
                for kind in to_retry:
                    if kind in pending:
                        _pending_batch.setdefault(kind, pending[kind])
            _logger.warning("BTD6 批量推送未完成已回填 kinds=%s (marked=%s)",
                            to_retry, [k for k, _ in to_mark])
        if to_mark:
            _logger.info(
                "BTD6 批量推送 %s 到 %d 群（成功标记）",
                ",".join(f"{k}={i}" for k, i in to_mark), len(groups))
    finally:
        _flush_in_progress = False
        # flush 期间新入队/失败回填的条目：再挂一轮防抖（放 finally：异常路径也必须补挂）
        with _batch_lock:
            leftover = bool(_pending_batch)
        if leftover and _PUSH_BATCH_DELAY_S > 0:
            try:
                _batch_flush_task = asyncio.get_running_loop().create_task(_delayed_flush())
            except RuntimeError:
                pass


async def _btd6_push_single(kind: str, ev: dict, ev_id: str, label: str, groups: set[int], *, mark_global: bool = True) -> None:
    """单类立即推送（手动补推/兼容旧调用）：同样先渲染完再发，总览在详情之前。

    mark_global=False 用于「只补推到本群」的路径：成功后不得写全局 last_pushed，
    否则其他订阅群本期会被跳过造成永久漏推。
    """
    payload = await _render_kind_payload(kind, ev, label)
    if not payload:
        return
    result = await _send_push_batch([(kind, ev_id, payload)], groups)
    ok = _kind_push_ok(kind, payload, result)
    if ok and mark_global:
        await asyncio.to_thread(_set_last_pushed, kind, ev_id)
    _logger.info("BTD6 精准推送 %s %s 到 %d 群（成功标记：%s，写全局：%s）",
                 kind, ev_id, len(groups), ok, mark_global and ok)


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
# CT 兜底：每小时 :12 采样。窗口外仅在「未推过该 id 且进行中」时入队，成功后由 last_pushed 去重。
@scheduler.scheduled_job("cron", minute=12, second=0, id="btd6_push_ct_hourly", timezone="Asia/Shanghai")
async def btd6_push_ct_hourly(): await _btd6_push_kind("ct")
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

# 每日 16:00（北京时间，与游戏内正式刷新点一致；普通+高级双版本）。勿改：见 collect.py 每日选期模块注释
# 备注：Co-op 业务约定刷新/推送固定为北京时间下午 16:00，与部分 API 展示口径不一致，以本排程为准。
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

# 社交赛季：刷新点不固定（NK 未公布固定周常），每小时 :15 采样一次；
# 只取进行中，id 与 last_pushed 不同即推送（无 12 分钟窗口，避免漏检）
@scheduler.scheduled_job("cron", minute=15, second=45, id="btd6_push_social", timezone="Asia/Shanghai")
async def btd6_push_social_job(): await _btd6_push_kind("social")


# ---------------- 手动推送检查（owner 调试/补推） ----------------

push_check_cmd = on_command("btd6推送检查", priority=5, block=True)

_PUSH_KIND_CN = {"race": "竞速", "boss": "Boss", "ct": "争夺领土", "odyssey": "远征",
                 "daily": "每日挑战", "rush": "Boss Rush", "coop": "Co-op",
                 "social": "社交赛季"}
_PUSH_KIND_ALIAS = {"竞速": "race", "boss": "boss", "领土": "ct", "ct": "ct",
                    "远征": "odyssey", "odyssey": "odyssey", "每日": "daily", "daily": "daily",
                    "rush": "rush", "coop": "coop",
                    "社季": "social", "社交": "social", "社交赛季": "social", "social": "social"}


def _parse_push_kind(text: str) -> str:
    parts = (text or "").strip().split()
    if len(parts) < 2:
        return ""
    return _PUSH_KIND_ALIAS.get(parts[1].strip().lower(), "")


@push_check_cmd.handle()
async def _push_check(event: MessageEvent):
    """`.btd6推送检查 [kind]`：无参数看各路当前/已推对照；带 kind 则把当期强制补推到本群。

    强制补推仍以 id 去重（已推送的不重发），只跳过“刷新点 12 分钟窗口”限制，
    用于窗口期服务异常/重启导致的漏推。
    """
    if not is_owner(event):
        await push_check_cmd.finish("❌ 你没有权限使用此功能")
    kind = _parse_push_kind(event.get_plaintext())
    if kind not in _BTD6_PUSH_KINDS:
        lines = []
        try:
            now = util.bucket_now()
            real_now = int(time.time() * 1000)
            last = await asyncio.to_thread(_last_pushed)
            for k in _BTD6_PUSH_KINDS:
                try:
                    ev = await _fetch_push_event(k, now, real_now)
                except Exception:
                    ev = None
                cur = str((ev or {}).get("id") or (ev or {}).get("name") or "-") \
                    if isinstance(ev, dict) else "-"
                mark = "✅" if last.get(k) == cur and cur != "-" else "❌"
                lines.append(f"{mark}{_PUSH_KIND_CN[k]}：当前 {cur} / 已推 {last.get(k, '-')}")
        except Exception:
            await push_check_cmd.finish("检查失败，请稍后再试")
            return
        await push_check_cmd.finish("🔍 BTD6 推送对照（当前 / 已推）：\n" + "\n".join(lines)
                                    + "\n用法：.btd6推送检查 远征（强制补推当期到本群）")
        return
    if not isinstance(event, GroupMessageEvent):
        await push_check_cmd.finish("补推请在群里使用（将发到本群）")
    try:
        now = util.bucket_now()
        real_now = int(time.time() * 1000)
        ev = await _fetch_push_event(kind, now, real_now)
    except Exception:
        await push_check_cmd.finish("抓取失败，请稍后再试")
        return
    if not isinstance(ev, dict):
        await push_check_cmd.finish(f"{_PUSH_KIND_CN[kind]}当前无进行中活动")
        return
    ev_id = str(ev.get("id") or ev.get("name") or "")
    label = str(ev.get("name") or "")
    if not ev_id:
        await push_check_cmd.finish("当期活动无有效 id，拒绝推送")
        return
    last_now = await asyncio.to_thread(_last_pushed)
    if last_now.get(kind) == ev_id:
        await push_check_cmd.finish(f"{_PUSH_KIND_CN[kind]}当期（{ev_id}）已推送过，无需重推")
        return
    try:
        get_bot()  # 只验证实例可用，实际发送由 _btd6_push_single 内部获取
    except Exception:
        await push_check_cmd.finish("拿不到 Bot 实例，请稍后再试")
        return
    # 补推只发当前群，绝不能写全局 last_pushed（否则其他订阅群本期永久漏推）
    await _btd6_push_single(kind, ev, ev_id, label, {int(event.group_id)}, mark_global=False)
    await push_check_cmd.finish("补推已发送到本群（未标记全局已推，其他群仍可自动收）")
