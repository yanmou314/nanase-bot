import asyncio
import json
import os
import re
import threading
import time

from nonebot import get_bot, logger, on_command, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageEvent, MessageSegment
from nonebot.matcher import Matcher
from nonebot.params import CommandArg

from common import RELAY_BOT_QQ, RELAY_GROUP_ID, at_prefix, is_owner, parse_tag, save_json_state

try:
    from nonebot_plugin_apscheduler import scheduler
except ImportError:  # 测试 stub 环境缺依赖时跳过定时任务
    scheduler = None

BIND_FILE = os.path.join(os.path.dirname(__file__), "bindings.json")
_LOCK = threading.RLock()

# ---- 任务中继模式：本机不再直调 overstats API ----
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

# 对方机器人「绘制将超过 QQ 5 分钟时限，稍后 @ 我领取」的通知
# 典型文案：@nanase <@...> 仍在绘制中，但预计会超过 QQ 官方 5 分钟回复时限。图片准备好后请再次 @机器人领取，缓存 24 小时。
_DRAWING_RE = re.compile(r"仍在绘制|超过\s*QQ\s*官方|图片准备好后请再次")

# 查询完成但无内容可画的终态文本：收到即转发收尾，不再等第二条图
# 典型文案：你在过去的 24 小时内没有对局记录。
_EMPTY_RESULT_RE = re.compile(r"没有对局记录|暂无对局|没有找到对局|无对局记录|没有比赛记录")
DRAWING_CLAIM_DELAY = 300  # 通知到达后延迟领取秒数（约 5 分钟）
DRAWING_CLAIM_TIMEOUT = 180  # 发出领取 @ 后等待图片的上限
MAX_FROZEN_TASKS = 10  # 冻结任务上限，超出时放弃最旧的并提示用户
LATE_MESSAGE_WINDOW = 45.0  # 超时/领取覆盖/重置后的迟到消息隔离窗（秒）

matchrep_cmd = on_command("战报", aliases={"战绩图", "report"}, priority=5, block=True)
rankhist_cmd = on_command("段位", aliases={"段位历史", "rank"}, priority=5, block=True)
strength_cmd = on_command("强度", aliases={"强度分析", "strength"}, priority=5, block=True)
summary_cmd = on_command("总结", aliases={"上分总结"}, priority=5, block=True)
bind_cmd = on_command("绑定", aliases={"bind"}, priority=5, block=True)
unbind_cmd = on_command("解绑", aliases={"unbind"}, priority=5, block=True)
myid_cmd = on_command("我的ID", aliases={"我的绑定", "myid"}, priority=5, block=True)


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
_frozen_tasks: list = []  # 绘制超时待领取的任务（FIFO，含 claim_at）
_claiming_task = None  # 已发出领取 @、正在等图片的任务
_claim_sent_at = 0.0
# 派发互斥：并发 _dispatch_next 只允许一次真正发送（先占槽再 await）
_dispatch_lock: asyncio.Lock | None = None
# 迟到消息隔离窗：超时/领取覆盖/重置后，窗口内 RELAY_BOT 的非领取消息直接丢弃
_discarded_until = 0.0
# 强引用 flush 任务，防止被 GC 掉导致宽限收尾丢失
_flush_tasks: set = set()


def _get_dispatch_lock() -> asyncio.Lock:
    """惰性创建派发锁；跨 asyncio.run 时若锁未持有可安全复用。"""
    global _dispatch_lock
    if _dispatch_lock is None:
        _dispatch_lock = asyncio.Lock()
    return _dispatch_lock


def _open_discard_window(seconds: float = LATE_MESSAGE_WINDOW) -> None:
    """打开迟到消息隔离窗，避免旧任务的迟到回复写入下一任务 first_segs。"""
    global _discarded_until
    _discarded_until = time.monotonic() + seconds


def _close_discard_window() -> None:
    """关闭隔离窗（领取图已交付 / 状态已稳定，放行后续正常归集）。"""
    global _discarded_until
    _discarded_until = 0.0


def _in_discard_window() -> bool:
    return time.monotonic() < _discarded_until


def _spawn_flush_wait(task_seq: int) -> None:
    """启动单条宽限收尾任务并持有强引用，done 后从 set 移除。"""
    t = asyncio.create_task(_flush_first_after_wait(task_seq))
    _flush_tasks.add(t)
    t.add_done_callback(_flush_tasks.discard)


def _task_kind_label(kind: str) -> str:
    return {"matchrep": "战报", "rankhist": "段位", "strength": "强度", "summary": "总结"}.get(kind, kind)


async def _dispatch_next() -> None:
    """无在途任务且队列非空时，派发下一个任务到中继群。

    领取中（已 @ 对方等图片）不派发：避免新查询挤掉领取结果。
    用 asyncio.Lock 串行化临界区：先写入 _task_current 再 await 发送，
    防止两个并发 _dispatch_next 同时弹出任务双发。
    """
    global _task_current
    async with _get_dispatch_lock():
        if _claiming_task is not None:
            return
        # 超时/重置后的隔离窗内不派发：避免新任务的合法回复被当作迟到消息丢弃
        if _in_discard_window():
            return
        if _task_current is not None or not _task_queue:
            return
        task = _task_queue.pop(0)
        task["t0"] = time.monotonic()
        # 先占槽：await send 期间其他 dispatch 看到 _task_current 非空会直接返回
        _task_current = task
        try:
            bot = get_bot()
        except Exception:
            _task_current = None
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
            _task_current = None
            _task_queue.insert(0, task)
            logger.warning("owstats 任务派发失败", exc_info=True)
            if str(task.get("group_id")) == str(RELAY_GROUP_ID):
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
        logger.info(f"owstats 已派发任务 #{task['seq']}（{task['kind']} {task['tag']}）")


async def _enqueue_task(kind: str, tag: str, group_id, user_id: str, matcher, event) -> None:
    """入队并尝试派发；先回复排队位置。"""
    global _task_seq
    if len(_task_queue) >= TASK_MAX_PENDING:
        await matcher.finish(at_prefix(event) + Message("排队任务太多，请稍后再试～"))
    _task_seq += 1
    if len(_task_queue) >= TASK_MAX_PENDING:
        raise RuntimeError("OW 查询队列已满，请稍后再试")
    _task_queue.append({"seq": _task_seq, "kind": kind, "tag": tag,
                        "group_id": group_id, "user_id": user_id, "t0": 0.0})
    waiting = len(_task_queue) - 1 + (1 if _task_current is not None else 0)
    label = _task_kind_label(kind)
    if str(getattr(event, "group_id", None)) != str(RELAY_GROUP_ID):
        if _in_discard_window():
            remain = int(max(0, _discarded_until - time.monotonic()))
            await matcher.send(at_prefix(event) + MessageSegment.text(
                f"已提交{label}查询（{tag}），中继冷却约 {remain}s 后自动派发～"))
        elif waiting <= 0:
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
    if len(_task_queue) >= TASK_MAX_PENDING:
        raise RuntimeError("OW 查询队列已满，请稍后再试")
    _task_queue.append({"seq": _task_seq, "kind": kind, "tag": tag,
                        "text": text, "future": fut,
                        "timeout": timeout or TASK_TIMEOUT,
                        "group_id": None, "user_id": "", "t0": 0.0})
    await _dispatch_next()
    return fut


relay_listener = on_message(priority=4, block=False)


# 对方机器人一条查询的回复形态（状态机）：
#   1) 首条即图片           → 本任务单图结果，直接转发
#   2) 文本 + 图片          → 正常两段归集
#   3) 无对局终态文案       → 立刻转发收尾（如「你在过去的 24 小时内没有对局记录。」）
#   4) 绘制超时通知         → 冻结本任务，先处理后面的查询
#   5) 进度提示             → 忽略
#   6) 其他第二条非图文本   → 按报错中断
# 首条图片一律归属当前在途任务；任务超时后的迟到消息由 discard window 拦截，
# 不再用「上一任务缺图」标记去丢弃本任务的合法结果。
SECOND_MSG_WAIT = 30  # 收到第一条文本后等待第二条的宽限秒数


def _msg_reply_id(message) -> str | None:
    """取消息里 [CQ:reply] 引用的消息 id；无引用返回 None。

    对方机器人回复时若带引用，可据此把图片/文本精确归属到某条下发任务，
    替代“冻结任务优先”的启发式猜测（防止把 B 的结果发给 A）。
    """
    for _seg in message:
        if _seg.type == "reply":
            return str((_seg.data or {}).get("id") or "") or None
    return None


def _msg_has_image(message) -> bool:
    for _seg in message:
        if _seg.type == "image":
            return True
    return False


def _strip_self_at(message, self_id: str) -> list:
    segs = []
    for seg in message:
        if seg.type == "at" and (str(seg.data.get("qq", "")) == self_id or seg.data.get("qq") == "all"):
            continue
        segs.append(seg)
    return segs


async def _send_to_requester(bot: Bot, task: dict, body) -> None:
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


def _pop_frozen(task: dict | None) -> dict | None:
    """从冻结列表移除指定任务（或按同一对象匹配），返回该任务或 None。"""
    if task is None:
        return None
    for i, t in enumerate(_frozen_tasks):
        if t is task or t.get("seq") == task.get("seq"):
            return _frozen_tasks.pop(i)
    return None


def _settle_future(task: dict | None) -> None:
    """丢弃任务时统一结算 future：未完成则 set_result((None, False))。"""
    if not task:
        return
    fut = task.get("future")
    if fut is not None and not fut.done():
        fut.set_result((None, False))


def _requeue_current_overwrite() -> dict | None:
    """领取图片挤占了在途查询：把当前任务原样放回队首，待会儿重发。"""
    global _task_current
    task = _task_current
    if task is None:
        return None
    _task_current = None
    task["t0"] = 0.0
    task.pop("first_segs", None)
    task.pop("task_msg_id", None)
    _task_queue.insert(0, task)
    logger.warning(f"owstats 任务 #{task.get('seq')} 查询指令被领取覆盖，重新入队")
    return task


async def _freeze_task_for_drawing(bot: Bot, task: dict, notice_segs: list | None = None) -> None:
    """对方回复「绘制将超 5 分钟」：通知用户、冻结任务、继续处理其他查询。"""
    global _task_current
    if _task_current is not task:
        return
    _task_current = None
    task.pop("first_segs", None)
    task["frozen_at"] = time.monotonic()
    task["claim_at"] = time.monotonic() + DRAWING_CLAIM_DELAY
    _frozen_tasks.append(task)
    # 冻结堆积过多时放弃最旧的，避免队列无限膨胀
    while len(_frozen_tasks) > MAX_FROZEN_TASKS:
        stale = _frozen_tasks.pop(0)
        fut = stale.get("future")
        if fut is not None and not fut.done():
            fut.set_result((None, False))
        elif str(stale.get("group_id")) != str(RELAY_GROUP_ID):
            try:
                await _send_to_requester(
                    bot, stale,
                    MessageSegment.text("绘制任务排队过多已取消，请稍后重新查询～"),
                )
            except Exception:
                pass
        logger.warning(f"owstats 冻结任务过多，丢弃最旧的 #{stale.get('seq')}")
    elapsed = time.monotonic() - task["t0"] if task.get("t0") else 0.0
    logger.info(
        f"owstats 任务 #{task.get('seq')} 绘制超时冻结，{DRAWING_CLAIM_DELAY}s 后领取（已等 {elapsed:.0f}s）"
    )
    fut = task.get("future")
    # 有 future 的调用方（如 bnet_verify）继续挂起，等领取图片后再 resolve
    if fut is None and str(task.get("group_id")) != str(RELAY_GROUP_ID):
        body = (
            "对方查询机器人已在绘制，但预计会超过 QQ 5 分钟回复时限。\n"
            f"图片缓存 24 小时，约 {DRAWING_CLAIM_DELAY // 60} 分钟后我会自动 @ 对方领取并转发给你；\n"
            "期间可先查询其他内容，请勿重复提交同一查询～"
        )
        if notice_segs:
            body = Message(notice_segs) + "\n———\n" + body
        await _send_to_requester(bot, task, MessageSegment.text(body) if isinstance(body, str) else body)
    await _dispatch_next()


async def _send_claim_at(bot: Bot | None = None) -> bool:
    """向对方机器人发领取 @。成功返回 True。

    领取 @ 会覆盖对方处的在途查询：先把在途任务放回队首，等图片到达后再重发。
    """
    global _claiming_task, _claim_sent_at
    if _claiming_task is not None or not _frozen_tasks:
        return False
    task = _frozen_tasks[0]
    if time.monotonic() < (task.get("claim_at") or 0.0):
        return False
    if bot is None:
        try:
            bot = get_bot()
        except Exception:
            logger.warning("owstats 领取失败：拿不到 Bot 实例")
            return False
    # 先摘走在途任务，避免领取结果到达时与在途归集互相干扰。
    # 只要发生了 requeue 就必须立刻开隔离窗：对方处旧查询的迟到回复
    # 可能在领取 @ 发送失败后仍到达，不能喂给重发后的新任务。
    overwritten = _requeue_current_overwrite() is not None
    if overwritten:
        _open_discard_window()
    try:
        await bot.send_group_msg(
            group_id=RELAY_GROUP_ID,
            message=MessageSegment.at(RELAY_BOT_QQ),
        )
    except Exception:
        logger.warning("owstats 发送领取 @ 失败", exc_info=True)
        return False
    if not _frozen_tasks:
        # await 发送期间列表可能被 .ow重置 清空：领取无从谈起
        return False
    _claiming_task = _frozen_tasks.pop(0)
    _claim_sent_at = time.monotonic()
    # 领取期间在途旧回复不得冒充领取图/首条：开隔离窗（仅放行 claiming 的图片）
    _open_discard_window()
    logger.info(f"owstats 已为冻结任务 #{_claiming_task.get('seq')} 发送领取 @")
    return True


async def _deliver_frozen_image(bot: Bot, image_segs: list) -> bool:
    """首条即图片 / 领取回复：交给冻结任务的原用户；若仍压着在途查询则重发。"""
    global _claiming_task, _claim_sent_at
    task = _claiming_task
    if task is None and _frozen_tasks:
        task = _frozen_tasks.pop(0)
        _claiming_task = None
    elif task is not None:
        _claiming_task = None
    if task is None:
        return False
    _pop_frozen(task)
    _claim_sent_at = 0.0
    elapsed = time.monotonic() - task["t0"] if task.get("t0") else 0.0
    fut = task.get("future")
    if fut is not None and not fut.done():
        fut.set_result((list(image_segs), True))
    elif str(task.get("group_id")) != str(RELAY_GROUP_ID):
        body = Message(list(image_segs)) + MessageSegment.text(f"\n绘制完成，用时 {elapsed:.1f}s")
        await _send_to_requester(bot, task, body)
    else:
        logger.info("owstats 冻结领取结果已在中继群内可见，跳过转回")
    # 未走领取通道时（对方主动把缓存图贴在新查询 @ 后），在途查询会被覆盖，补一次重发
    _requeue_current_overwrite()
    logger.info(f"owstats 冻结任务 #{task.get('seq')} 领取图片已转发")
    # 领取结果已交付：关掉隔离窗，让重发/下一任务能正常归集
    _close_discard_window()
    await _dispatch_next()
    return True


async def _fail_claim_or_stale() -> None:
    """领取超时或领取结果异常：通知原用户失败，并继续队列。"""
    global _claiming_task, _claim_sent_at
    task = _claiming_task
    if task is None:
        return
    _claiming_task = None
    _claim_sent_at = 0.0
    _pop_frozen(task)
    fut = task.get("future")
    if fut is not None and not fut.done():
        fut.set_result((None, False))
    elif str(task.get("group_id")) != str(RELAY_GROUP_ID):
        try:
            bot = get_bot()
            await _send_to_requester(
                bot, task,
                MessageSegment.text("绘制图片领取超时，请稍后重新发起查询～"),
            )
        except Exception:
            pass
    logger.warning(f"owstats 冻结任务 #{task.get('seq')} 领取失败")
    _close_discard_window()
    await _dispatch_next()


async def _complete_task_success(bot: Bot, task: dict, first_segs: list, second_segs: list) -> None:
    """两段收齐（文本 + 图片）：合并转发，结束任务并派发下一个。"""
    global _task_current
    if _task_current is not task:
        return
    _task_current = None
    elapsed = time.monotonic() - task["t0"] if task.get("t0") else 0.0
    fut = task.get("future")
    if fut is not None:
        if not fut.done():
            fut.set_result((list(first_segs) + list(second_segs), True))
        await _dispatch_next()
        return
    if str(task.get("group_id")) == str(RELAY_GROUP_ID):
        logger.info("owstats 结果已在中继群内可见，跳过转回")
        await _dispatch_next()
        return
    combined = list(first_segs) + list(second_segs)
    if not combined:
        body = MessageSegment.text("对方机器人返回了空结果，请重试～")
    else:
        body = Message(combined) + MessageSegment.text(f"\n用时 {elapsed:.1f}s")
    await _send_to_requester(bot, task, body)
    await _dispatch_next()


async def _complete_task_single(bot: Bot, task: dict, single_segs: list) -> None:
    """只收到一条文本且宽限内无第二条：按单条转发，结束任务。"""
    global _task_current
    if _task_current is not task:
        return
    _task_current = None
    elapsed = time.monotonic() - task["t0"] if task.get("t0") else 0.0
    fut = task.get("future")
    if fut is not None:
        if not fut.done():
            fut.set_result((list(single_segs), False))
        await _dispatch_next()
        return
    if str(task.get("group_id")) == str(RELAY_GROUP_ID):
        logger.info("owstats 结果已在中继群内可见，跳过转回")
        await _dispatch_next()
        return
    if not single_segs:
        body = MessageSegment.text("对方机器人返回了空结果，请重试～")
    else:
        body = Message(list(single_segs)) + MessageSegment.text(f"\n用时 {elapsed:.1f}s")
    await _send_to_requester(bot, task, body)
    await _dispatch_next()


async def _abort_task_error(bot: Bot, task: dict, error_segs: list) -> None:
    """第二条不是图片：视为报错，中断本次查询（丢弃第一条），通知请求方后派发下一个。"""
    global _task_current
    if _task_current is not task:
        return
    _task_current = None
    logger.warning(f"owstats 任务 #{task.get('seq')} 第二条非图片，按报错中断")
    fut = task.get("future")
    if fut is not None:
        if not fut.done():
            fut.set_result((list(error_segs or []), False))
        await _dispatch_next()
        return
    if str(task.get("group_id")) == str(RELAY_GROUP_ID):
        logger.info("owstats 报错已在中继群内可见，跳过转回")
        await _dispatch_next()
        return
    if error_segs:
        body = Message([MessageSegment.text("查询失败，对方机器人返回：\n")]) + Message(error_segs)
    else:
        body = MessageSegment.text("查询失败，对方机器人返回报错，请稍后再试～")
    await _send_to_requester(bot, task, body)
    await _dispatch_next()


async def _flush_first_after_wait(task_seq: int) -> None:
    """第一条文本到达后宽限 SECOND_MSG_WAIT 秒仍无第二条：按单条转发，避免无限挂起。"""
    global _task_current
    await asyncio.sleep(SECOND_MSG_WAIT)
    task = _task_current
    if task is None or task.get("seq") != task_seq or not task.get("first_segs"):
        return
    logger.info(f"owstats 任务 #{task_seq} 只收到一条文本，超时按单条转发")
    first = task.pop("first_segs")
    try:
        bot = get_bot()
    except Exception:
        logger.warning("owstats 单条转发失败：拿不到 Bot 实例，丢弃任务")
        _task_current = None
        await _dispatch_next()
        return
    await _complete_task_single(bot, task, first)


@relay_listener.handle()
async def _relay_result(bot: Bot, event: MessageEvent, matcher: Matcher):
    """监听中继群里查询机器人的回复，两段归集后转发给原群/原用户（串行，无需任务ID关联）。

    额外分支：
    - 「仍在绘制中，超过 QQ 5 分钟」→ 冻结任务，先去处理其他用户；
    - 首条即图片且存在冻结/领取中任务 → 默认交给冻结用户，在途查询重新入队；
      但图片若带 reply 引用，按引用精确归属，不再猜测。
    命中中继机器人消息时 matcher.block=True，避免落到 auto_chat 等后续 matcher。
    """
    global _task_current
    if not isinstance(event, GroupMessageEvent):
        return
    if event.group_id != RELAY_GROUP_ID or str(event.user_id) != str(RELAY_BOT_QQ):
        return
    # 中继结果由本 handler 专管：拦住后续 on_message（尤其 auto_chat）
    try:
        matcher.block = True
    except Exception:
        pass
    has_image = _msg_has_image(event.message)
    reply_id = _msg_reply_id(event.message)
    plain = event.message.extract_plain_text()
    try:
        self_id = str(bot.self_id)
    except Exception:
        self_id = ""

    # 迟到消息隔离窗：超时/领取覆盖/重置后的在途旧回复不得写入下一任务；
    # 仅放行「领取中 + 图片」的预期领取结果。
    if _in_discard_window():
        if not (has_image and _claiming_task is not None):
            logger.info("owstats 丢弃隔离窗内的迟到中继消息")
            return

    # 图片归属判定：
    # 1) 已发出领取 @ → 该图必是领取结果，即使压着在途任务也优先交付；
    # 2) 尚未领取，但队列有冻结任务且在途还没收到第一条文本 → 对方可能把缓存图
    #    直接贴在了新查询 @ 后面（查询指令被覆盖），也算冻结用户的；
    # 3) 在途已收到第一条文本后的第二条图 → 仍是本任务的，不得抢走。
    if has_image and _claiming_task is not None:
        cur = _task_current
        if reply_id and cur and str(reply_id) == str(cur.get("task_msg_id") or ""):
            pass  # 引用归属：图片明确属于在途任务，不是领取结果，落到下方处理
        else:
            if _task_current is not None:
                _task_current.pop("first_segs", None)
            segs = _strip_self_at(event.message, self_id)
            if segs and await _deliver_frozen_image(bot, segs):
                return
    elif has_image and _frozen_tasks and (
        _task_current is None or not _task_current.get("first_segs")
    ):
        # 归属关联（替代“冻结优先”猜测，防把 B 的图发给 A）：
        # - 引用在途任务下发消息 → 属于在途任务，不判给冻结用户；
        # - 引用冻结任务下发消息 → 明确判给冻结用户；
        # - 无引用信息 → 沿用原启发式（对方可能把缓存图贴在新查询后）。
        cur = _task_current
        quoted_current = bool(reply_id and cur and str(reply_id) == str(cur.get("task_msg_id") or ""))
        quoted_frozen = bool(reply_id) and any(
            str(reply_id) == str(ft.get("task_msg_id") or "") for ft in _frozen_tasks
        )
        if not quoted_current and (quoted_frozen or not reply_id):
            segs = _strip_self_at(event.message, self_id)
            if segs and await _deliver_frozen_image(bot, segs):
                return

    if _task_current is None:
        logger.info("owstats 中继群收到非任务结果，已忽略")
        return

    if not has_image and _DRAWING_RE.search(plain):
        notice = _strip_self_at(event.message, self_id)
        logger.info(f"owstats 任务 #{_task_current.get('seq')} 收到绘制超时通知，转入冻结")
        await _freeze_task_for_drawing(bot, _task_current, notice)
        return

    if not has_image and _PROGRESS_RE.search(plain):
        logger.info("owstats 中继群收到进度提示，已忽略")
        return

    task = _task_current
    if not task.get("first_segs"):
        # 首条即图片：一律视为本任务的单图结果（summary/verify 常见），
        # 不再按上一任务缺图标记丢弃——派发时已清孤儿，超时迟到由隔离窗拦。
        if has_image:
            logger.info(f"owstats 任务 #{task.get('seq')} 首条即图片，按单图结果处理")
            segs = _strip_self_at(event.message, self_id)
            await _complete_task_success(bot, task, [], segs)
            return
        segs = _strip_self_at(event.message, self_id)
        if not segs:
            logger.info("owstats 中继群收到空文本，已忽略")
            return
        # 无对局等终态文案：立刻转发收尾，不必再等第二条
        if _EMPTY_RESULT_RE.search(plain):
            logger.info(f"owstats 任务 #{task.get('seq')} 收到无对局终态文案，直接转发收尾")
            await _complete_task_success(bot, task, [], segs)
            return
        task["first_segs"] = segs
        logger.info(f"owstats 任务 #{task.get('seq')} 收到第一条文本，等第二条图片（{SECOND_MSG_WAIT}s）")
        _spawn_flush_wait(task["seq"])
        return
    # 已有第一条，在等第二条
    if has_image:
        first = task.pop("first_segs")
        segs = _strip_self_at(event.message, self_id)
        logger.info(f"owstats 任务 #{task.get('seq')} 收到第二条图片，合并转发")
        await _complete_task_success(bot, task, first, segs)
        return
    if _DRAWING_RE.search(plain):
        notice = _strip_self_at(event.message, self_id)
        logger.info(f"owstats 任务 #{task.get('seq')} 第二条为绘制超时通知，转入冻结")
        await _freeze_task_for_drawing(bot, task, notice)
        return
    # 无对局终态文案作为第二条：合并转发，不要当报错
    if _EMPTY_RESULT_RE.search(plain):
        first = task.pop("first_segs")
        segs = _strip_self_at(event.message, self_id)
        logger.info(f"owstats 任务 #{task.get('seq')} 第二条为无对局终态文案，合并转发")
        await _complete_task_success(bot, task, first, segs)
        return
    segs = _strip_self_at(event.message, self_id)
    if not segs:
        logger.info("owstats 中继群收到空文本，已忽略")
        return
    logger.info(f"owstats 任务 #{task.get('seq')} 第二条非图片，按报错中断")
    await _abort_task_error(bot, task, segs)


if scheduler is not None:
    @scheduler.scheduled_job("cron", minute="*", id="owstats_task_sweep", timezone="Asia/Shanghai", max_instances=1)
    async def _task_sweep():
        """每分钟：领取到期冻结任务、清理在途超时，并派发下一个。"""
        global _task_current
        # 领取 @ 已发出但迟迟没有图片
        if _claiming_task is not None and time.monotonic() - (_claim_sent_at or 0.0) > DRAWING_CLAIM_TIMEOUT:
            logger.warning(f"owstats 冻结任务 #{_claiming_task.get('seq')} 领取超时")
            await _fail_claim_or_stale()
        # 领取中：不派发、不把在途超时算到别人头上（在途其实已被摘走）
        if _claiming_task is not None:
            return
        # 有到期冻结任务时优先领取（会把在途查询放回队首，等图片到达再重发）
        if _frozen_tasks:
            await _send_claim_at()
        if _claiming_task is not None:
            return
        if _task_current is None:
            # 隔离窗未结束则本轮不派发，窗结束后下一轮 sweep 再派
            await _dispatch_next()
            return
        if time.monotonic() - (_task_current.get("t0") or 0.0) < (_task_current.get("timeout") or TASK_TIMEOUT):
            return
        task = _task_current
        _task_current = None
        _open_discard_window()  # 迟到回复不得写入下一任务；窗内禁止再派发
        logger.warning(f"owstats 任务 #{task['seq']} 超时")
        fut = task.get("future")
        if fut is not None:
            if not fut.done():
                fut.set_result((None, False))
            return
        if str(task.get("group_id")) == str(RELAY_GROUP_ID):
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
    remain = _check_cooldown(str(event.user_id))
    if remain > 0:
        await matchrep_cmd.finish(at + MessageSegment.text(f"查询太频繁啦，请 {int(remain) + 1} 秒后再试～"))
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
    remain = _check_cooldown(str(event.user_id))
    if remain > 0:
        await rankhist_cmd.finish(at + MessageSegment.text(f"查询太频繁啦，请 {int(remain) + 1} 秒后再试～"))
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
    remain = _check_cooldown(str(event.user_id))
    if remain > 0:
        await strength_cmd.finish(at + MessageSegment.text(f"查询太频繁啦，请 {int(remain) + 1} 秒后再试～"))
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
    if scope != "today":
        await summary_cmd.finish(at + MessageSegment.text("对方查询机器人暂只支持今日总结～"))
    remain = _check_cooldown(str(event.user_id))
    if remain > 0:
        await summary_cmd.finish(at + MessageSegment.text(f"查询太频繁啦，请 {int(remain) + 1} 秒后再试～"))
    await _enqueue_task("summary", tag, getattr(event, "group_id", None), str(event.user_id), summary_cmd, event)


owstatus_cmd = on_command("ow状态", aliases={"ow队列"}, priority=5, block=True)
owreset_cmd = on_command("ow重置", aliases={"ow清理"}, priority=5, block=True)


@owstatus_cmd.handle()
async def ow_status(event: MessageEvent):
    if not is_owner(event):
        await owstatus_cmd.finish(at_prefix(event) + "仅Bot主人可查看队列状态")
    if _task_current is None:
        cur = "当前无在途任务"
    else:
        t = _task_current
        el = time.monotonic() - t["t0"] if t.get("t0") else 0.0
        first = "已收到" if t.get("first_segs") else "未收到"
        cur = f"在途 #{t.get('seq')}（{_task_kind_label(t.get('kind', ''))} {t.get('tag')}）：已等待 {el:.0f}s，第一条文本{first}"
    claim = "无"
    if _claiming_task is not None:
        claim = f"#{_claiming_task.get('seq')}（已发出领取 @ {time.monotonic() - (_claim_sent_at or 0):.0f}s）"
    frozen = "无"
    if _frozen_tasks:
        frozen = "、".join(
            f"#{t.get('seq')}({int(max(0, (t.get('claim_at') or 0) - time.monotonic()))}s后可领)"
            for t in _frozen_tasks[:5]
        )
    await owstatus_cmd.finish(
        at_prefix(event)
        + f"{cur}\n排队 {len(_task_queue)} 个，冻结待领取 {frozen}，领取中 {claim}")


@owreset_cmd.handle()
async def ow_reset(event: MessageEvent):
    global _task_current, _claiming_task, _claim_sent_at
    if not is_owner(event):
        await owreset_cmd.finish(at_prefix(event) + "仅Bot主人可重置队列")
    dropped = []
    if _task_current is not None:
        dropped.append(f"在途 #{_task_current.get('seq')}")
        _settle_future(_task_current)
        _task_current = None
    if _claiming_task is not None:
        dropped.append(f"领取中 #{_claiming_task.get('seq')}")
        _settle_future(_claiming_task)
        _claiming_task = None
        _claim_sent_at = 0.0
    while _frozen_tasks:
        stale = _frozen_tasks.pop(0)
        dropped.append(f"冻结 #{stale.get('seq')}")
        _settle_future(stale)
    if not dropped:
        await owreset_cmd.finish(at_prefix(event) + f"没有在途/冻结任务，排队 {len(_task_queue)} 个，无需重置")
    _open_discard_window()  # 窗内禁止派发，避免下一任务回复被当迟到消息
    logger.warning(f"owstats 主人手动丢弃：{'、'.join(dropped)}")
    await owreset_cmd.finish(
        at_prefix(event) + f"已丢弃 {'、'.join(dropped)}，排队 {len(_task_queue)} 个任务继续执行（约 {int(LATE_MESSAGE_WINDOW)}s 隔离窗后自动续跑）")
