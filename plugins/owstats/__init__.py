import asyncio
import json
import os
import threading
import time
from pathlib import Path

import httpx
from nonebot import get_driver, logger, on_command
from nonebot.adapters.onebot.v11 import Message, MessageEvent, MessageSegment
from nonebot.params import CommandArg

from common import at_prefix, close_http_clients, get_http_client, parse_tag, save_image_async, save_json_state

try:
    from nonebot_plugin_apscheduler import scheduler
except ImportError:  # 测试 stub 环境缺依赖时跳过定时预热
    scheduler = None

# 上游数据服务（overstats）地址，可用环境变量覆盖
API = os.getenv("OW_API_BASE", "http://127.0.0.1:18080")
CACHE = os.path.join(os.path.dirname(__file__), "cache")
BIND_FILE = os.path.join(os.path.dirname(__file__), "bindings.json")
_LOCK = threading.RLock()
OW_LOCK = asyncio.Lock()

# 总结各档位的超时预算：上游有 5 请求/秒的硬限速，一次总结要几十上百次请求，
# 冷缓存时仅限速排队就要 20-30 秒，预算必须盖住冷查询（预热只是缓解、不能保证全热）
SUMMARY_TIMEOUTS = {"today": 75, "yesterday": 90, "week": 180}

matchrep_cmd = on_command("战报", aliases={"战绩图", "report"}, priority=5, block=True)
rankhist_cmd = on_command("段位", aliases={"段位历史", "rank"}, priority=5, block=True)
strength_cmd = on_command("强度", aliases={"强度分析", "strength"}, priority=5, block=True)
summary_cmd = on_command("总结", aliases={"上分总结"}, priority=5, block=True)
bind_cmd = on_command("绑定", aliases={"bind"}, priority=5, block=True)
unbind_cmd = on_command("解绑", aliases={"unbind"}, priority=5, block=True)
myid_cmd = on_command("我的ID", aliases={"我的绑定", "myid"}, priority=5, block=True)


_bind_cache: dict | None = None


@get_driver().on_shutdown
async def _close_http() -> None:
    await close_http_clients()


def _http():
    return get_http_client(timeout=120)


def _load_bindings() -> dict:
    global _bind_cache
    if _bind_cache is None:
        try:
            with open(BIND_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            _bind_cache = data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            # 文件损坏：备份现场后重置，避免后续覆盖丢失全部绑定
            try:
                os.replace(BIND_FILE, BIND_FILE + f".corrupt-{int(time.time())}")
            except OSError:
                pass
            _bind_cache = {}
        except (FileNotFoundError, OSError):
            _bind_cache = {}
    return _bind_cache


def _save_bindings(data: dict) -> None:
    global _bind_cache
    _bind_cache = data
    save_json_state(BIND_FILE, data, _LOCK)


def _bind(uid: str, tag: str) -> None:
    with _LOCK:
        data = _load_bindings()
        data[uid] = tag
        _save_bindings(data)


def _unbind(uid: str) -> bool:
    with _LOCK:
        data = _load_bindings()
        if uid in data:
            del data[uid]
            _save_bindings(data)
            return True
        return False


def _get_bound(uid: str) -> str:
    with _LOCK:
        return _load_bindings().get(uid, "")


# ---------------- 后台预热 ----------------
WARMUP_ENABLED = os.getenv("OW_WARMUP", "1").strip().lower() not in {"0", "false", "no", "off"}
WARMUP_DETAIL_MATCHES = 20  # 预取最近 N 局详情（详情缓存 TTL 30 分钟，查询直接命中）
WARMUP_GAP_SECONDS = 20  # 玩家间隔，避免挤占上游 5 请求/秒共享配额


async def _warmup_player(tag: str) -> bool:
    """调用 overstats 预热接口：暖玩家解析/对局列表/详情/天气缓存，不做图片渲染。"""
    try:
        data = await _post_json(
            "/api/v2/dashen-summary/warmup",
            {"bnet_id": tag, "detail_matches": WARMUP_DETAIL_MATCHES},
            timeout=180,
        )
    except Exception as e:
        logger.warning(f"owstats 预热 {tag} 请求失败（{e!r}）")
        return False
    if not data.get("ok"):
        logger.info(f"owstats 预热 {tag} 未执行：{data.get('error') or data.get('message')}")
        return False
    logger.info(
        f"owstats 预热 {tag} 完成：列表 {data.get('match_count')} 局 / "
        f"详情 {data.get('detail_count')} 局 / 天气 {data.get('weather_count')} 局"
    )
    return True


if scheduler is not None:
    @scheduler.scheduled_job("cron", minute="7", hour="9-23/2", id="owstats_warmup", timezone="Asia/Shanghai")
    async def _warmup_bound_players():
        """白天每两小时预热所有绑定玩家，让 .总结 查询尽量命中缓存。

        预热期间用户查询会收到"正忙+预计时长"提示（见 _warmup_busy_notice），
        不与预热共享并发槽和上游 5 请求/秒配额。
        """
        if not WARMUP_ENABLED:
            return
        tags = sorted(set(_load_bindings().values()))
        if not tags:
            return
        logger.info(f"owstats 开始预热 {len(tags)} 个绑定玩家")
        _warmup_state["busy"] = True  # 先亮正忙标志再逐个玩家抢锁，压缩用户查询与预热抢跑的窗口
        try:
            for idx, tag in enumerate(tags):
                _warmup_state["deadline"] = time.time() + (len(tags) - idx) * WARMUP_PER_PLAYER_BUDGET
                if OW_LOCK.locked():  # 有用户查询正在跑，本轮让位（与原语义一致）
                    logger.info("owstats 预热检测到用户查询进行中，本轮中止")
                    return
                # 抢到锁并全程持有到该玩家预热结束：上游请求绝不与用户查询并发挤占 5 请求/秒配额。
                # 即使上面检查后恰有用户查询抢先拿到锁，这里也只会排队等它完成，而不是并发执行。
                async with OW_LOCK:
                    await _warmup_player(tag)
                await asyncio.sleep(WARMUP_GAP_SECONDS)
        finally:
            _warmup_state["busy"] = False


async def _post_json(path: str, payload: dict, timeout: float = 90.0):
    r = await _http().post(f"{API}{path}", json=payload, timeout=timeout)
    if r.status_code == 200 and "image" in r.headers.get("content-type", ""):
        return {"_image": True, "bytes": r.content, "content_type": r.headers["content-type"]}
    try:
        data = r.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return {"ok": False, "message": f"上游服务异常（HTTP {r.status_code}）"}
    return data if isinstance(data, dict) else {"ok": False, "message": "上游返回格式异常"}


async def _post_json_with_notice(matcher, at: Message, notice: str, path: str, payload: dict,
                                 timeout: float = 90.0, remind_after: float = 30.0):
    done = asyncio.Event()

    async def reminder():
        try:
            await asyncio.wait_for(done.wait(), timeout=remind_after)
        except asyncio.TimeoutError:
            try:
                await matcher.send(at + Message(f"⏳ {notice}（已等待 {int(remind_after)} 秒，数据量较大请再耐心等待...）"))
            except Exception:
                pass

    rem = asyncio.create_task(reminder())
    try:
        return await _post_json(path, payload, timeout)
    finally:
        done.set()
        if not rem.done():
            rem.cancel()
        try:
            await rem
        except asyncio.CancelledError:
            pass


def _resolve_tag(arg: Message, event: MessageEvent) -> tuple[str, bool]:
    """解析查询目标。返回 (tag, 显式输入但格式无效)——后者用于提示用户而不是
    静默回退到发送者自己的绑定（否则「.战报 张三」会查出别人以为的数据）。"""
    raw = arg.extract_plain_text().strip()
    if raw:
        tag = parse_tag(raw.split()[0])
        return (tag, False) if tag else ("", True)
    return _get_bound(str(event.user_id)), False


_BAD_ID_HINT = "ID 格式不对哦：要用 名字#数字（例如 Yanmou#51293）\n去掉 ID 直接发指令则查询自己绑定的 ID"


_last_query: dict[str, float] = {}
_QUERY_COOLDOWN = 10  # 每用户查询冷却，防止连点刷屏排满渲染队列


def _check_cooldown(uid: str) -> float:
    """通过冷却则记账并返回 0；冷却中返回剩余秒数（不记账）。"""
    now = time.time()
    if len(_last_query) > 5000:  # 防内存增长
        for k in [k for k, t in _last_query.items() if now - t > 3600]:
            _last_query.pop(k, None)
    remain = _QUERY_COOLDOWN - (now - _last_query.get(uid, 0))
    if remain > 0:
        return remain
    _last_query[uid] = now
    return 0.0


# 预热状态：预热进行中时用户查询直接提示稍后再试，而不是与预热共享并发槽/上游限速。
# deadline 到点自动视为空闲，即使预热任务异常退出也不会永久"正忙"。
_warmup_state = {"busy": False, "deadline": 0.0}
WARMUP_PER_PLAYER_BUDGET = 45  # 单玩家预热+间隔的耗时估算（秒），用于向用户报 ETA


def _warmup_remaining() -> float:
    if not _warmup_state["busy"]:
        return 0.0
    return max(0.0, _warmup_state["deadline"] - time.time())


def _warmup_busy_notice() -> str:
    """预热进行中返回带预计等待时长的提示，空闲返回空串。"""
    remain = _warmup_remaining()
    if remain <= 0:
        return ""
    if remain >= 60:
        return f"机器人正在后台预热数据，约 {int(remain // 60) + 1} 分钟后完成，请稍后再试～"
    return f"机器人正在后台预热数据，约 {int(remain) + 1} 秒后完成，请稍后再试～"


async def _wait_queue(matcher, event: MessageEvent):
    if OW_LOCK.locked():
        await matcher.send(at_prefix(event) + Message("⏳ 有请求正在处理中，你已进入队列，完成后自动回复，请稍候..."))
    await OW_LOCK.acquire()
    return OW_LOCK


def _done(matcher_msg: Message, at: Message, elapsed: float) -> Message:
    return at + matcher_msg + Message(f"\n⏱ 用时 {elapsed:.1f}s")


def _friendly_error(data: dict, scope: str = "") -> str:
    code = data.get("error") or ""
    msg = data.get("message") or ""
    if code == "summary_empty":
        empty_texts = {
            "today": "该玩家在过去 24 小时内没有对局记录，暂时无法生成总结～",
            "yesterday": "该玩家昨日没有对局记录，暂时无法生成总结～",
            "week": "该玩家在过去 7 天内没有对局记录，暂时无法生成总结～",
        }
        return empty_texts.get(scope) or "该玩家近期没有对局记录，暂时无法生成总结～"
    if code == "bnet_not_found":
        details = data.get("details") if isinstance(data.get("details"), dict) else {}
        query = str(details.get("query") or "").strip()
        target = f"「{query}」" if query else "该 ID"
        return f"没找到 {target}，请检查名字大小写和 # 后面的数字是否正确～"
    if code == "missing_target":
        return "缺少查询 ID：请带上 名字#数字，或先用 .绑定 绑定自己的 ID"
    if code == "too_many_requests":
        return "现在查询的人有点多，请稍等几秒再试～"
    if code == "summary_busy":
        return "总结正在生成中，请稍后再试～"
    if code and not msg:
        return f"查询失败：{code}"
    return msg or "未知错误"


# ---------------- 绑定 ----------------
@bind_cmd.handle()
async def bind(event: MessageEvent, arg: Message = CommandArg()):
    tag = parse_tag(arg.extract_plain_text().strip())
    if not tag:
        await bind_cmd.finish(at_prefix(event) + "用法：.绑定 名字#数字\n例如：.绑定 Yanmou#51293")
    _bind(str(event.user_id), tag)
    await bind_cmd.finish(at_prefix(event) + f"✅ 绑定成功：{tag}\n之后直接发 .战报、.段位、.强度、.总结 即可查询；加 ID 可查别人，如 .战报 其他人#1234")


@unbind_cmd.handle()
async def unbind(event: MessageEvent):
    if _unbind(str(event.user_id)):
        await unbind_cmd.finish(at_prefix(event) + "✅ 已解除绑定")
    await unbind_cmd.finish(at_prefix(event) + "你还没有绑定过 ID")


@myid_cmd.handle()
async def myid(event: MessageEvent):
    tag = _get_bound(str(event.user_id))
    if tag:
        await myid_cmd.finish(at_prefix(event) + f"🎮 当前绑定：{tag}\n如需更换请用 .绑定 新ID")
    await myid_cmd.finish(at_prefix(event) + "你还没有绑定 ID，用 .绑定 名字#数字 绑定")


# ---------------- 战报 ----------------
@matchrep_cmd.handle()
async def match_report(event: MessageEvent, arg: Message = CommandArg()):
    t0 = time.monotonic()
    at = at_prefix(event)
    tag, bad_id = _resolve_tag(arg, event)
    if bad_id:
        await matchrep_cmd.finish(at + _BAD_ID_HINT)
    if not tag:
        await matchrep_cmd.finish(at + "请先绑定你的 ID：.绑定 名字#数字\n或直接指定：.战报 名字#数字")
    busy = _warmup_busy_notice()
    if busy:
        await matchrep_cmd.finish(at + busy)
    remain = _check_cooldown(str(event.user_id))
    if remain > 0:
        await matchrep_cmd.finish(at + f"查询太频繁啦，请 {int(remain) + 1} 秒后再试～")
    await _wait_queue(matchrep_cmd, event)
    try:
        await matchrep_cmd.send(at + f"⏳ 正在生成 {tag} 的战绩图...")
        try:
            data = await _post_json_with_notice(matchrep_cmd, at, f"正在生成 {tag} 的战绩图",
                                                "/api/v2/dashen-match/replies", {"bnet_id": tag, "limit": 5}, timeout=90)
        except httpx.HTTPError:
            await matchrep_cmd.finish(at + "查询失败：请求超时，请稍后再试")
        if not data.get("ok"):
            await matchrep_cmd.finish(at + _friendly_error(data))
        segments = []
        for rep in data.get("replies") or []:
            if rep.get("type") == "image" and rep.get("base64"):
                segments.append(MessageSegment.image("base64://" + rep["base64"]))
        if not segments:
            await matchrep_cmd.finish(at + "没有生成战绩图，请稍后再试")
        await matchrep_cmd.finish(_done(Message(segments), at, time.monotonic() - t0))
    finally:
        OW_LOCK.release()


# ---------------- 段位历史 ----------------
@rankhist_cmd.handle()
async def rank_history(event: MessageEvent, arg: Message = CommandArg()):
    t0 = time.monotonic()
    at = at_prefix(event)
    tag, bad_id = _resolve_tag(arg, event)
    if bad_id:
        await rankhist_cmd.finish(at + _BAD_ID_HINT)
    if not tag:
        await rankhist_cmd.finish(at + "请先绑定你的 ID：.绑定 名字#数字\n或直接指定：.段位 名字#数字")
    busy = _warmup_busy_notice()
    if busy:
        await rankhist_cmd.finish(at + busy)
    remain = _check_cooldown(str(event.user_id))
    if remain > 0:
        await rankhist_cmd.finish(at + f"查询太频繁啦，请 {int(remain) + 1} 秒后再试～")
    await _wait_queue(rankhist_cmd, event)
    try:
        await rankhist_cmd.send(at + f"⏳ 正在查询 {tag} 的段位历史...")
        try:
            data = await _post_json_with_notice(rankhist_cmd, at, f"正在查询 {tag} 的段位历史",
                                                "/api/v2/dashen-rank-history/image", {"bnet_id": tag}, timeout=120)
        except httpx.HTTPError:
            await rankhist_cmd.finish(at + "查询失败：请求超时，请稍后再试")
        if data.get("_image"):
            path = await save_image_async(data["bytes"], data["content_type"], "rank", CACHE)
            await rankhist_cmd.finish(_done(Message(MessageSegment.image(Path(path).as_uri())), at, time.monotonic() - t0))
        await rankhist_cmd.finish(at + _friendly_error(data))
    finally:
        OW_LOCK.release()


# ---------------- 强度分析 ----------------
@strength_cmd.handle()
async def strength(event: MessageEvent, arg: Message = CommandArg()):
    t0 = time.monotonic()
    at = at_prefix(event)
    tag, bad_id = _resolve_tag(arg, event)
    if bad_id:
        await strength_cmd.finish(at + _BAD_ID_HINT)
    if not tag:
        await strength_cmd.finish(at + "请先绑定你的 ID：.绑定 名字#数字\n或直接指定：.强度 名字#数字")
    busy = _warmup_busy_notice()
    if busy:
        await strength_cmd.finish(at + busy)
    remain = _check_cooldown(str(event.user_id))
    if remain > 0:
        await strength_cmd.finish(at + f"查询太频繁啦，请 {int(remain) + 1} 秒后再试～")
    await _wait_queue(strength_cmd, event)
    try:
        await strength_cmd.send(at + f"⏳ 正在分析 {tag} 的强度...")
        try:
            data = await _post_json_with_notice(strength_cmd, at, f"正在分析 {tag} 的强度",
                                                "/api/v2/dashen-quick-strength/image", {"bnet_id": tag, "limit": 12}, timeout=120)
        except httpx.HTTPError:
            await strength_cmd.finish(at + "查询失败：请求超时，请稍后再试")
        if data.get("_image"):
            path = await save_image_async(data["bytes"], data["content_type"], "strength", CACHE)
            await strength_cmd.finish(_done(Message(MessageSegment.image(Path(path).as_uri())), at, time.monotonic() - t0))
        await strength_cmd.finish(at + _friendly_error(data))
    finally:
        OW_LOCK.release()


# ---------------- 每日总结 ----------------
@summary_cmd.handle()
async def summary(event: MessageEvent, arg: Message = CommandArg()):
    t0 = time.monotonic()
    at = at_prefix(event)
    parts = arg.extract_plain_text().split()
    scope = "today"
    if parts and parts[0] in ("今日", "今天", "昨日", "昨天", "本周"):
        scope = {"今日": "today", "今天": "today", "昨日": "yesterday",
                 "昨天": "yesterday", "本周": "week"}.get(parts[0], "today")
        parts = parts[1:]
    tag = ""
    bad_id = False
    if parts:
        tag = parse_tag(parts[0])
        bad_id = not tag
    if not tag:
        tag = _get_bound(str(event.user_id))
    if bad_id:
        await summary_cmd.finish(at + _BAD_ID_HINT)
    if not tag:
        await summary_cmd.finish(at + "请先绑定你的 ID：.绑定 名字#数字\n或直接指定：.总结 名字#数字")
    busy = _warmup_busy_notice()
    if busy:
        await summary_cmd.finish(at + busy)
    remain = _check_cooldown(str(event.user_id))
    if remain > 0:
        await summary_cmd.finish(at + f"查询太频繁啦，请 {int(remain) + 1} 秒后再试～")
    await _wait_queue(summary_cmd, event)
    try:
        timeout = SUMMARY_TIMEOUTS.get(scope, SUMMARY_TIMEOUTS["today"])
        labels = {"today": "今日", "yesterday": "昨日", "week": "本周"}
        await summary_cmd.send(at + f"⏳ 正在生成 {tag} 的{labels[scope]}总结，数据量大请稍候...")
        try:
            data = await _post_json_with_notice(summary_cmd, at, f"正在生成 {tag} 的{labels[scope]}总结",
                                                f"/api/v2/dashen-summary/{scope}/image", {"bnet_id": tag}, timeout=timeout)
        except httpx.HTTPError:
            await summary_cmd.finish(at + "生成失败：超时，请稍后再试")
        if data.get("_image"):
            path = await save_image_async(data["bytes"], data["content_type"], "summary", CACHE)
            await summary_cmd.finish(_done(Message(MessageSegment.image(Path(path).as_uri())), at, time.monotonic() - t0))
        await summary_cmd.finish(at + _friendly_error(data, scope))
    finally:
        OW_LOCK.release()
