"""插件报错通知：插件处理事件抛异常、定时任务执行失败时，私聊通知主人插件与错误摘要。

同类错误有 10 分钟冷却，防止连环报错刷屏。
"""
import asyncio
import time
from datetime import datetime

from nonebot import get_bot, get_driver, logger

# 与 chat_stats/news 等插件一致：直接导入调度器单例。
# 不能用 require()——该模块已被更早加载的插件按普通模块导入，二次按插件加载会报错。
from apscheduler.events import EVENT_JOB_ERROR
from nonebot_plugin_apscheduler import scheduler

from nonebot.adapters.onebot.v11 import MessageSegment
from nonebot.exception import IgnoredException, MatcherException, SkippedException
from nonebot.matcher import Matcher
from nonebot.message import run_postprocessor

from common import OWNER

_COOLDOWN = 10 * 60  # 同一插件同一错误 10 分钟内只提醒一次
_last_notified: dict[str, float] = {}
_loop: asyncio.AbstractEventLoop | None = None


def _plugin_label(matcher: Matcher) -> str:
    plugin = getattr(matcher, "plugin", None)
    if plugin is not None and getattr(plugin, "name", None):
        return plugin.name
    module = getattr(matcher, "module", None)
    if module is not None and getattr(module, "__name__", None):
        return module.__name__
    return "未知插件"


def _deepest_location(exc: Exception) -> str:
    """异常堆栈最深的一帧（出错代码的文件:行号:函数名）。"""
    tb = exc.__traceback__
    loc = "未知位置"
    while tb is not None:
        frame = tb.tb_frame
        loc = f"{frame.f_code.co_filename}:{tb.tb_lineno} in {frame.f_code.co_name}"
        tb = tb.tb_next
    return loc


def _error_key(plugin: str, exc: Exception, loc: str) -> str:
    return f"{plugin}|{type(exc).__name__}|{loc}"


def _should_notify(key: str, now: float) -> bool:
    if now - _last_notified.get(key, 0) < _COOLDOWN:
        return False
    if len(_last_notified) > 500:  # 防止冷门错误键无限累积
        cutoff = now - _COOLDOWN
        for k in [k for k, ts in _last_notified.items() if ts < cutoff]:
            _last_notified.pop(k, None)
    _last_notified[key] = now
    return True


def _build_message(plugin: str, exc: Exception, loc: str) -> str:
    detail = f"{type(exc).__name__}: {exc}"
    if len(detail) > 300:
        detail = detail[:300] + "…"
    return (
        f"⚠️ 插件运行报错\n"
        f"🔌 插件：{plugin}\n"
        f"❌ 错误：{detail}\n"
        f"📍 位置：{loc}\n"
        f"🕐 {datetime.now().strftime('%m-%d %H:%M')}\n"
        f"（同一错误 {_COOLDOWN // 60} 分钟内不重复提醒）"
    )


async def _send_notice(text: str) -> None:
    bot = get_bot()
    await bot.send_private_msg(user_id=int(OWNER), message=MessageSegment.text(text))


def _log_notice_result(fut) -> None:
    """run_coroutine_threadsafe 的 future 无人 await，异常需在此显式记日志而不是等 GC 报警。"""
    exc = fut.exception()
    if exc is not None:
        logger.opt(exception=exc).warning("定时任务报错通知发送失败")


@run_postprocessor
async def notify_plugin_error(matcher: Matcher, exception: Exception | None) -> None:
    # finish()/skip() 等流程控制异常不是真报错
    if exception is None or isinstance(
        exception, (MatcherException, SkippedException, IgnoredException)
    ):
        return
    try:
        plugin = _plugin_label(matcher)
        loc = _deepest_location(exception)
        if not _should_notify(_error_key(plugin, exception, loc), time.time()):
            return
        await _send_notice(_build_message(plugin, exception, loc))
    except Exception:
        # 通知钩子自身绝不向上抛，避免拖垮事件分发
        logger.exception("插件报错通知发送失败")


@get_driver().on_startup
async def _capture_loop() -> None:
    """记下主事件循环；定时任务监听器可能在别的线程触发，需投递回循环执行。"""
    global _loop
    _loop = asyncio.get_running_loop()


def _job_label(event) -> str:
    job_id = getattr(event, "job_id", "") or "未知任务"
    try:
        job = scheduler.get_job(job_id)
    except Exception:
        job = None
    fn = getattr(getattr(job, "func", None), "__qualname__", "") or ""
    return f"定时任务 {job_id}" + (f"（{fn}）" if fn else "")


def _on_job_error(event) -> None:
    """APScheduler 事件监听器：任何定时任务抛异常时通知主人。"""
    try:
        exc = getattr(event, "exception", None)
        if exc is None or isinstance(
            exc, (MatcherException, SkippedException, IgnoredException)
        ):
            return
        label = _job_label(event)
        loc = _deepest_location(exc)
        if not _should_notify(_error_key(label, exc, loc), time.time()):
            return
        text = _build_message(label, exc, loc)
        if _loop is not None and not _loop.is_closed():
            asyncio.run_coroutine_threadsafe(_send_notice(text), _loop).add_done_callback(_log_notice_result)
        else:
            logger.error("定时任务报错但事件循环未就绪，无法私发通知：%s", text)
    except Exception:
        logger.exception("定时任务报错通知分发失败")


scheduler.add_listener(_on_job_error, EVENT_JOB_ERROR)
