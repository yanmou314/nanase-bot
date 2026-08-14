import asyncio
import json
import os
import threading
import time
from pathlib import Path

from nonebot import get_driver, on_command
from nonebot.adapters.onebot.v11 import Message, MessageEvent, MessageSegment
from nonebot.params import CommandArg

from common import at_prefix, cleanup_cache, close_http_clients, get_http_client, parse_tag, save_json_state, save_image as save_img

# 上游数据服务（overstats）地址，可用环境变量覆盖
API = os.getenv("OW_API_BASE", "http://127.0.0.1:18080")
CACHE = os.path.join(os.path.dirname(__file__), "cache")
BIND_FILE = os.path.join(os.path.dirname(__file__), "bindings.json")
_LOCK = threading.RLock()
OW_LOCK = asyncio.Lock()

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


def _resolve_tag(arg: Message, event: MessageEvent) -> str:
    raw = arg.extract_plain_text().strip()
    if raw:
        tag = parse_tag(raw.split()[0])
        if tag:
            return tag
    return _get_bound(str(event.user_id))


async def _wait_queue(matcher, event: MessageEvent):
    if OW_LOCK.locked():
        await matcher.send(at_prefix(event) + Message("⏳ 有请求正在处理中，你已进入队列，完成后自动回复，请稍候..."))
    await OW_LOCK.acquire()
    return OW_LOCK


def _done(matcher_msg: Message, at: Message, elapsed: float) -> Message:
    return at + matcher_msg + Message(f"\n⏱ 用时 {elapsed:.1f}s")


def _friendly_error(data: dict) -> str:
    code = data.get("error") or ""
    msg = data.get("message") or ""
    if code == "summary_empty":
        return "该玩家在过去 24 小时内没有对局记录，暂时无法生成总结～"
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
    tag = _resolve_tag(arg, event)
    if not tag:
        await matchrep_cmd.finish(at + "请先绑定你的 ID：.绑定 名字#数字\n或直接指定：.战报 名字#数字")
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
    tag = _resolve_tag(arg, event)
    if not tag:
        await rankhist_cmd.finish(at + "请先绑定你的 ID：.绑定 名字#数字\n或直接指定：.段位 名字#数字")
    await _wait_queue(rankhist_cmd, event)
    try:
        await rankhist_cmd.send(at + f"⏳ 正在查询 {tag} 的段位历史...")
        try:
            data = await _post_json_with_notice(rankhist_cmd, at, f"正在查询 {tag} 的段位历史",
                                                "/api/v2/dashen-rank-history/image", {"bnet_id": tag}, timeout=120)
        except httpx.HTTPError:
            await rankhist_cmd.finish(at + "查询失败：请求超时，请稍后再试")
        if data.get("_image"):
            path = save_img(data["bytes"], data["content_type"], "rank", CACHE)
            await rankhist_cmd.finish(_done(Message(MessageSegment.image(Path(path).as_uri())), at, time.monotonic() - t0))
        await rankhist_cmd.finish(at + _friendly_error(data))
    finally:
        OW_LOCK.release()


# ---------------- 强度分析 ----------------
@strength_cmd.handle()
async def strength(event: MessageEvent, arg: Message = CommandArg()):
    t0 = time.monotonic()
    at = at_prefix(event)
    tag = _resolve_tag(arg, event)
    if not tag:
        await strength_cmd.finish(at + "请先绑定你的 ID：.绑定 名字#数字\n或直接指定：.强度 名字#数字")
    await _wait_queue(strength_cmd, event)
    try:
        await strength_cmd.send(at + f"⏳ 正在分析 {tag} 的强度...")
        try:
            data = await _post_json_with_notice(strength_cmd, at, f"正在分析 {tag} 的强度",
                                                "/api/v2/dashen-quick-strength/image", {"bnet_id": tag, "limit": 12}, timeout=120)
        except httpx.HTTPError:
            await strength_cmd.finish(at + "查询失败：请求超时，请稍后再试")
        if data.get("_image"):
            path = save_img(data["bytes"], data["content_type"], "strength", CACHE)
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
    if parts:
        tag = parse_tag(parts[0])
    if not tag:
        tag = _get_bound(str(event.user_id))
    if not tag:
        await summary_cmd.finish(at + "请先绑定你的 ID：.绑定 名字#数字\n或直接指定：.总结 名字#数字")
    await _wait_queue(summary_cmd, event)
    try:
        timeout = {"today": 40, "yesterday": 60, "week": 150}.get(scope, 40)
        labels = {"today": "今日", "yesterday": "昨日", "week": "本周"}
        await summary_cmd.send(at + f"⏳ 正在生成 {tag} 的{labels[scope]}总结，数据量大请稍候...")
        try:
            data = await _post_json_with_notice(summary_cmd, at, f"正在生成 {tag} 的{labels[scope]}总结",
                                                f"/api/v2/dashen-summary/{scope}/image", {"bnet_id": tag}, timeout=timeout)
        except httpx.HTTPError:
            await summary_cmd.finish(at + "生成失败：超时，请稍后再试")
        if data.get("_image"):
            path = save_img(data["bytes"], data["content_type"], "summary", CACHE)
            await summary_cmd.finish(_done(Message(MessageSegment.image(Path(path).as_uri())), at, time.monotonic() - t0))
        await summary_cmd.finish(at + _friendly_error(data))
    finally:
        OW_LOCK.release()
