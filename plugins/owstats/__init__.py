import asyncio
import json
import os
import re
import threading
import time

from nonebot import get_bot, logger, on_command, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageEvent, MessageSegment
from nonebot.params import CommandArg

from common import at_prefix, parse_tag, save_json_state

try:
    from nonebot_plugin_apscheduler import scheduler
except ImportError:  # 测试 stub 环境缺依赖时跳过定时任务
    scheduler = None

BIND_FILE = os.path.join(os.path.dirname(__file__), "bindings.json")
MAINTENANCE_FILE = os.path.join(os.path.dirname(__file__), "maintenance.json")
_LOCK = threading.RLock()

# ---- 任务中继模式：本机不再直调 overstats API ----
RELAY_GROUP_ID = 864213945
RELAY_BOT_QQ = 3889045090
TASK_TIMEOUT = 180
TASK_MAX_PENDING = 20
TASK_CMD_TEXT = {
    "matchrep": "/大神对局",
    "rankhist": "/历史段位",
    "strength": "/快速强度指数",
    "summary": "/今日总结",
}

# 对方机器人的纯文本进度提示（不含图片）：只忽略，不消费任务
_PROGRESS_RE = re.compile("正在生成|正在查询|正在分析|正在处理|排队|请稍候|请稍等|等待片刻|查询中|生成中")

# Maintenance mode: when enabled, all OW queries return maintenance message
MAINTENANCE_MSG = "OW\u63a5\u53e3\u6b63\u5728\u7ef4\u62a4\u4e2d\uff0c\u8bf7\u7a0d\u540e\u518d\u8bd5\uff5e"

def _is_maintenance() -> bool:
    try:
        with open(MAINTENANCE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return bool(data.get("enabled"))
    except (FileNotFoundError, OSError, json.JSONDecodeError, AttributeError):
        return False

def _set_maintenance(enabled: bool) -> None:
    save_json_state(MAINTENANCE_FILE, {"enabled": bool(enabled), "updated": int(time.time())}, _LOCK)

matchrep_cmd = on_command("战报", aliases={"战绩图", "report"}, priority=5, block=True)
rankhist_cmd = on_command("段位", aliases={"段位历史", "rank"}, priority=5, block=True)
strength_cmd = on_command("强度", aliases={"强度分析", "strength"}, priority=5, block=True)
summary_cmd = on_command("总结", aliases={"上分总结"}, priority=5, block=True)
bind_cmd = on_command("绑定", aliases={"bind"}, priority=5, block=True)
unbind_cmd = on_command("解绑", aliases={"unbind"}, priority=5, block=True)
myid_cmd = on_command("我的ID", aliases={"我的绑定", "myid"}, priority=5, block=True)
maintenance_cmd = on_command("ow\u7ef4\u62a4", aliases={"OW\u7ef4\u62a4", "ow\u5173\u95ed", "ow\u5f00\u542f", "ow\u5f00\u5173"}, priority=5, block=True)


_bind_cache: dict | None = None


def _load_bindings() -> dict:
    global _bind_cache
    if _bind_cache is None:
        try:
            with open(BIND_FILE, encoding="utf-8") as f:
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


# ---------------- 任务中继（串行：同时只跑 1 个任务） ----------------
_task_seq = 0
_task_queue: list = []  # 等待派发的任务 FIFO
_task_current = None  # 正在对方机器人处执行的任务


def _task_kind_label(kind: str) -> str:
    return {"matchrep": "战报", "rankhist": "段位", "strength": "强度", "summary": "总结"}.get(kind, kind)


async def _dispatch_next() -> None:
    """无在途任务且队列非空时，派发下一个任务到中继群。"""
    global _task_current
    if _task_current is not None or not _task_queue:
        return
    task = _task_queue.pop(0)
    task["t0"] = time.monotonic()
    try:
        bot = get_bot()
    except Exception:
        _task_queue.insert(0, task)
        logger.warning("owstats 任务派发失败：拿不到 Bot 实例")
        return
    text = task.get("text") or (" {} {}".format(TASK_CMD_TEXT.get(task["kind"], ""), task["tag"]))
    try:
        ret = await bot.send_group_msg(
            group_id=RELAY_GROUP_ID,
            message=MessageSegment.at(RELAY_BOT_QQ) + MessageSegment.text(text),
        )
        task["task_msg_id"] = (ret or {}).get("message_id") if isinstance(ret, dict) else None
    except Exception:
        _task_queue.insert(0, task)
        logger.warning("owstats 任务派发失败", exc_info=True)
        if str(task.get("group_id")) == "864213945":
            return
        try:
            if task.get("group_id"):
                await bot.send_group_msg(
                    group_id=int(task["group_id"]),
                    message=MessageSegment.at(int(task["user_id"])) + MessageSegment.text("任务派发失败，请稍后再试～"))
            else:
                await bot.send_private_msg(user_id=int(task["user_id"]), message=MessageSegment.text("任务派发失败，请稍后再试～"))
        except Exception:
            pass
        return
    _task_current = task
    logger.info(f"owstats 已派发任务 #{task['seq']}（{task['kind']} {task['tag']}）")


async def _enqueue_task(kind: str, tag: str, group_id, user_id: str, matcher, event) -> None:
    """入队并尝试派发；先回复排队位置。"""
    global _task_seq
    if len(_task_queue) >= TASK_MAX_PENDING:
        await matcher.finish(at_prefix(event) + Message("排队任务太多，请稍后再试～"))
    _task_seq += 1
    _task_queue.append({"seq": _task_seq, "kind": kind, "tag": tag,
                        "group_id": group_id, "user_id": user_id, "t0": 0.0})
    waiting = len(_task_queue) - 1 + (1 if _task_current is not None else 0)
    label = _task_kind_label(kind)
    if str(getattr(event, "group_id", None)) != "864213945":
        if waiting <= 0:
            await matcher.send(at_prefix(event) + MessageSegment.text(f"已提交{label}查询（{tag}），正在派发给查询机器人..."))
        else:
            await matcher.send(at_prefix(event) + MessageSegment.text(f"已提交{label}查询（{tag}），前面还有 {waiting} 个任务，完成後转发给你～"))
    await _dispatch_next()


# priority=4：必须排在 auto_chat(to_me, priority=5, block=True) 之前，
# 否则对方机器人每条带@的消息都会先被 auto_chat 吃掉拦截，本监听器永远看不到
async def submit_relay_task(kind: str, tag: str, text=None, timeout=None):
    """供其他插件提交中继任务（串行，共用对方机器人）。

    text 缺省时按 kind 查 TASK_CMD_TEXT；timeout 缺省 TASK_TIMEOUT。
    返回 asyncio.Future，值为 (segs|None, has_image)；超时 segs 为 None。
    """
    global _task_seq
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    _task_seq += 1
    _task_queue.append({"seq": _task_seq, "kind": kind, "tag": tag,
                        "text": text, "future": fut,
                        "timeout": timeout or TASK_TIMEOUT,
                        "group_id": None, "user_id": "", "t0": 0.0})
    await _dispatch_next()
    return fut


relay_listener = on_message(priority=4, block=False)


@relay_listener.handle()
async def _relay_result(bot: Bot, event: MessageEvent):
    """监听中继群里查询机器人的回复，去掉@自身后转发给原群/原用户（串行，无需任务ID关联）。"""
    global _task_current
    if not isinstance(event, GroupMessageEvent):
        return
    if event.group_id != RELAY_GROUP_ID or str(event.user_id) != str(RELAY_BOT_QQ):
        return
    if _task_current is None:
        logger.info("owstats 中继群收到非任务结果，已忽略")
        return
    has_image = False
    for _seg in event.message:
        if _seg.type == "image":
            has_image = True
            break
    if not has_image and _PROGRESS_RE.search(event.message.extract_plain_text()):
        logger.info("owstats 中继群收到进度提示，已忽略")
        return
    task = _task_current
    _task_current = None
    if str(task.get("group_id")) == "864213945":
        logger.info("owstats 结果已在中继群内可见，跳过转回")
        await _dispatch_next()
        return
    try:
        self_id = str(bot.self_id)
    except Exception:
        self_id = ""
    segs = []
    for seg in event.message:
        if seg.type == "at" and (str(seg.data.get("qq", "")) == self_id or seg.data.get("qq") == "all"):
            continue
        segs.append(seg)
    elapsed = time.monotonic() - task["t0"] if task.get("t0") else 0.0
    fut = task.get("future")
    if fut is not None:
        if not fut.done():
            fut.set_result((segs, has_image))
        await _dispatch_next()
        return
    if not segs:
        body = MessageSegment.text("对方机器人返回了空结果，请重试～")
    else:
        body = Message(segs) + MessageSegment.text(f"\n用时 {elapsed:.1f}s")
    try:
        if str(task["user_id"]).isdigit():
            target_at = MessageSegment.at(int(task["user_id"]))
        else:
            target_at = MessageSegment.text("")
        if task.get("group_id"):
            await bot.send_group_msg(group_id=int(task["group_id"]), message=target_at + body)
        else:
            await bot.send_private_msg(user_id=int(task["user_id"]), message=body)
    except Exception:
        logger.warning("owstats 结果转发失败", exc_info=True)
    await _dispatch_next()


if scheduler is not None:
    @scheduler.scheduled_job("cron", minute="*", id="owstats_task_sweep", timezone="Asia/Shanghai", max_instances=1)
    async def _task_sweep():
        """每分钟清理超时的在途任务并派发下一个。"""
        global _task_current
        if _task_current is None:
            await _dispatch_next()
            return
        if time.monotonic() - (_task_current.get("t0") or 0.0) < (_task_current.get("timeout") or TASK_TIMEOUT):
            return
        task = _task_current
        _task_current = None
        logger.warning(f"owstats 任务 #{task['seq']} 超时")
        fut = task.get("future")
        if fut is not None:
            if not fut.done():
                fut.set_result((None, False))
            await _dispatch_next()
            return
        if str(task.get("group_id")) == "864213945":
            await _dispatch_next()
            return
        try:
            bot = get_bot()
            if str(task["user_id"]).isdigit():
                msg = MessageSegment.at(int(task["user_id"])) + MessageSegment.text("查询超时（超过3分钟），请稍后再试～")
            else:
                msg = MessageSegment.text("查询超时（超过3分钟），请稍后再试～")
            if task.get("group_id"):
                await bot.send_group_msg(group_id=int(task["group_id"]), message=msg)
            else:
                await bot.send_private_msg(user_id=int(task["user_id"]), message=msg)
        except Exception:
            pass
        await _dispatch_next()


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


# ---------------- 绑定 ----------------
@bind_cmd.handle()
async def bind(event: MessageEvent, arg: Message = CommandArg()):
    tag = parse_tag(arg.extract_plain_text().strip())
    if not tag:
        await bind_cmd.finish(at_prefix(event) + "用法：.绑定 名字#数字\n例如：.绑定 Yanmou#51293")
    _bind(str(event.user_id), tag)
    # tag 为用户自由输入（防 CQ 码注入）：回显一律 MessageSegment.text 包裹
    await bind_cmd.finish(at_prefix(event) + MessageSegment.text(f"✅ 绑定成功：{tag}") + "\n之后直接发 .战报、.段位、.强度、.总结 即可查询；加 ID 可查别人，如 .战报 其他人#1234")


@unbind_cmd.handle()
async def unbind(event: MessageEvent):
    if _unbind(str(event.user_id)):
        await unbind_cmd.finish(at_prefix(event) + "✅ 已解除绑定")
    await unbind_cmd.finish(at_prefix(event) + "你还没有绑定过 ID")


@myid_cmd.handle()
async def myid(event: MessageEvent):
    tag = _get_bound(str(event.user_id))
    if tag:
        await myid_cmd.finish(at_prefix(event) + MessageSegment.text(f"🎮 当前绑定：{tag}") + "\n如需更换请用 .绑定 新ID")
    await myid_cmd.finish(at_prefix(event) + "你还没有绑定 ID，用 .绑定 名字#数字 绑定")


# ---------------- 战报 ----------------
@matchrep_cmd.handle()
async def match_report(event: MessageEvent, arg: Message = CommandArg()):
    at = at_prefix(event)
    tag, bad_id = _resolve_tag(arg, event)
    if bad_id:
        await matchrep_cmd.finish(at + _BAD_ID_HINT)
    if not tag:
        await matchrep_cmd.finish(at + "请先绑定你的 ID：.绑定 名字#数字\n或直接指定：.战报 名字#数字")
    if _is_maintenance():
        await matchrep_cmd.finish(at + MessageSegment.text(MAINTENANCE_MSG))
    await _enqueue_task("matchrep", tag, getattr(event, "group_id", None), str(event.user_id), matchrep_cmd, event)


# ---------------- 段位历史 ----------------
@rankhist_cmd.handle()
async def rank_history(event: MessageEvent, arg: Message = CommandArg()):
    at = at_prefix(event)
    tag, bad_id = _resolve_tag(arg, event)
    if bad_id:
        await rankhist_cmd.finish(at + _BAD_ID_HINT)
    if not tag:
        await rankhist_cmd.finish(at + "请先绑定你的 ID：.绑定 名字#数字\n或直接指定：.段位 名字#数字")
    if _is_maintenance():
        await rankhist_cmd.finish(at + MessageSegment.text(MAINTENANCE_MSG))
    await _enqueue_task("rankhist", tag, getattr(event, "group_id", None), str(event.user_id), rankhist_cmd, event)


# ---------------- 强度分析 ----------------
@strength_cmd.handle()
async def strength(event: MessageEvent, arg: Message = CommandArg()):
    at = at_prefix(event)
    tag, bad_id = _resolve_tag(arg, event)
    if bad_id:
        await strength_cmd.finish(at + _BAD_ID_HINT)
    if not tag:
        await strength_cmd.finish(at + "请先绑定你的 ID：.绑定 名字#数字\n或直接指定：.强度 名字#数字")
    if _is_maintenance():
        await strength_cmd.finish(at + MessageSegment.text(MAINTENANCE_MSG))
    await _enqueue_task("strength", tag, getattr(event, "group_id", None), str(event.user_id), strength_cmd, event)


# ---------------- 每日总结 ----------------
@summary_cmd.handle()
async def summary(event: MessageEvent, arg: Message = CommandArg()):
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
    if _is_maintenance():
        await summary_cmd.finish(at + MessageSegment.text(MAINTENANCE_MSG))
    if scope != "today":
        await summary_cmd.finish(at + MessageSegment.text("对方查询机器人暂只支持今日总结～"))
    await _enqueue_task("summary", tag, getattr(event, "group_id", None), str(event.user_id), summary_cmd, event)


# ---------------- Maintenance toggle ----------------
@maintenance_cmd.handle()
async def maintenance_toggle(event: MessageEvent, arg: Message = CommandArg()):
    raw = arg.extract_plain_text().strip().lower()
    owner = str(os.getenv("QQBOT_OWNER", "1543758852")).strip()
    if str(event.user_id) != owner:
        await maintenance_cmd.finish(at_prefix(event) + "\u4ec5Bot\u4e3b\u4eba\u53ef\u64cd\u4f5c\u7ef4\u62a4\u5f00\u5173")
    if raw in {"\u5f00\u542f", "\u5f00", "on", "enable", "1", "\u7ef4\u62a4"}:
        _set_maintenance(True)
        await maintenance_cmd.finish(at_prefix(event) + "\u2705 \u5df2\u5f00\u542f\u7ef4\u62a4\u6a21\u5f0f\uff0cOW\u67e5\u8be2\u5c06\u63d0\u793a\u7ef4\u62a4\u4e2d")
    elif raw in {"\u5173\u95ed", "\u5173", "off", "disable", "0", "\u6062\u590d"}:
        _set_maintenance(False)
        await maintenance_cmd.finish(at_prefix(event) + "\u2705 \u5df2\u5173\u95ed\u7ef4\u62a4\u6a21\u5f0f\uff0cOW\u67e5\u8be2\u5df2\u6062\u590d")
    elif raw in {"\u72b6\u6001", "\u67e5\u8be2", "status"}:
        enabled = _is_maintenance()
        await maintenance_cmd.finish(at_prefix(event) + ("\uD83D\uDD27 \u5f53\u524d\u4e3a\u7ef4\u62a4\u4e2d" if enabled else "\u2705 \u5f53\u524d\u6b63\u5e38\u8fd0\u884c"))
    elif not raw:
        enabled = _is_maintenance()
        status = "\u7ef4\u62a4\u4e2d" if enabled else "\u6b63\u5e38"
        await maintenance_cmd.finish(at_prefix(event) + f"\u5f53\u524d\uff1a{status}\n\u7528\u6cd5\uff1a.ow\u7ef4\u62a4 \u5f00\u542f/\u5173\u95ed/\u72b6\u6001")
    else:
        await maintenance_cmd.finish(at_prefix(event) + "\u53c2\u6570\u9519\u8bef\uff0c\u7528\u6cd5\uff1a.ow\u7ef4\u62a4 \u5f00\u542f/\u5173\u95ed/\u72b6\u6001")

