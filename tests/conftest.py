"""pytest 配置：用最小 stub 替代 nonebot 运行时，使插件模块可被直接导入测试纯逻辑。

必须在任何插件导入前完成 stub 注入（pytest 会在收集测试前先导入本文件）。
"""
import logging
import os
import sys
import types
from pathlib import Path

BOT_ROOT = Path(__file__).resolve().parents[1]
if str(BOT_ROOT) not in sys.path:
    sys.path.insert(0, str(BOT_ROOT))

# 固定测试用 owner QQ，避免 int(OWNER) 等路径依赖真实环境变量
os.environ["QQBOT_OWNER"] = "10000"


class _Matcher:
    def __init__(self, *args, **kwargs):
        self.handlers = []
        self.sent = []  # send() 发送过的消息（供断言实际发送内容）
        self.finished = []  # finish() 结束时带的消息

    def handle(self):
        def deco(fn):
            self.handlers.append(fn)
            return fn
        return deco

    async def finish(self, message=None, **kwargs):
        self.finished.append(message)
        raise FinishedException(message)

    async def send(self, message=None, **kwargs):
        self.sent.append(message)
        return None


class FinishedException(Exception):
    def __init__(self, message=None):
        self.message = message


class _Driver:
    config = types.SimpleNamespace(command_start=(".",))

    def __init__(self):
        self._on_startup = []
        self._on_shutdown = []
        self._on_bot_connect = []

    def on_shutdown(self, fn):
        self._on_shutdown.append(fn)
        return fn

    def on_startup(self, fn):
        self._on_startup.append(fn)
        return fn

    def on_bot_connect(self, fn):
        """机器人连接钩子；与 on_startup 同样注册到列表并原样返回（恒等装饰器）。"""
        self._on_bot_connect.append(fn)
        return fn


_DRIVER = _Driver()


def get_driver():
    return _DRIVER


def get_bot():
    raise RuntimeError("tests have no bot instance")


class Message:
    def __init__(self, segments=None):
        if isinstance(segments, MessageSegment):
            segments = [segments]
        self.segments = list(segments or [])

    def __add__(self, other):
        if isinstance(other, MessageSegment):
            return Message(self.segments + [other])
        if isinstance(other, Message):
            return Message(self.segments + other.segments)
        if isinstance(other, str):
            return Message(self.segments + [MessageSegment.text(other)])
        return NotImplemented

    def __iter__(self):
        return iter(self.segments)

    def __str__(self):
        return "".join(str(s) for s in self.segments)

    def extract_plain_text(self):
        return "".join(
            s.data.get("text", "") for s in self.segments if s.type == "text"
        )


class MessageSegment:
    def __init__(self, type_, data=None):
        self.type = type_
        self.data = dict(data or {})

    @classmethod
    def text(cls, t):
        return cls("text", {"text": str(t)})

    @classmethod
    def at(cls, qq):
        return cls("at", {"qq": qq})

    @classmethod
    def image(cls, file):
        return cls("image", {"file": file})

    @classmethod
    def reply(cls, mid):
        return cls("reply", {"id": mid})

    def __add__(self, other):
        if isinstance(other, MessageSegment):
            return Message([self, other])
        if isinstance(other, Message):
            return Message([self] + other.segments)
        if isinstance(other, str):
            return Message([self, MessageSegment.text(other)])
        return NotImplemented

    def __str__(self):
        if self.type == "text":
            return self.data.get("text", "")
        inner = ",".join(f"{k}={v}" for k, v in self.data.items())
        return f"[CQ:{self.type},{inner}]"


class Event:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class MessageEvent(Event):
    def get_plaintext(self):
        return str(getattr(self, "plain", ""))


class GroupMessageEvent(MessageEvent):
    pass


class FriendRequestEvent(Event):
    pass


class GroupRequestEvent(Event):
    pass


class PokeNotifyEvent(Event):
    pass


class GroupDecreaseNoticeEvent(Event):
    """退群通知：group_id/user_id/sub_type（leave|kick）+ operator_id。"""

    def __init__(self, group_id=0, user_id=0, sub_type="leave", operator_id=0, self_id=10000, **kw):
        super().__init__(group_id=group_id, user_id=user_id, sub_type=sub_type,
                         operator_id=operator_id, self_id=self_id, **kw)


class GroupIncreaseNoticeEvent(Event):
    """入群通知：group_id/user_id/sub_type。"""

    def __init__(self, group_id=0, user_id=0, sub_type="", operator_id=0, self_id=10000, **kw):
        super().__init__(group_id=group_id, user_id=user_id, sub_type=sub_type,
                         operator_id=operator_id, self_id=self_id, **kw)


class Bot:
    """可实例化的 Bot stub：send_private_msg 记录到 sent_private 供断言。"""

    def __init__(self, self_id="10000"):
        self.self_id = str(self_id)
        self.sent_private = []

    async def send_private_msg(self, user_id=None, message=None, **kwargs):
        self.sent_private.append({"user_id": user_id, "message": message})
        return None


def _install_stubs():
    nb = types.ModuleType("nonebot")
    nb.get_driver = get_driver
    nb.get_bot = get_bot
    nb.logger = logging.getLogger("nonebot")
    nb.on_message = lambda *a, **k: _Matcher()
    nb.on_command = lambda *a, **k: _Matcher()
    nb.on_notice = lambda *a, **k: _Matcher()
    nb.on_request = lambda *a, **k: _Matcher()
    nb.run_post_processor = lambda fn: fn
    nb.run_postprocessor = nb.run_post_processor  # 兼容旧拼写
    nb.require = lambda name: None

    message_mod = types.ModuleType("nonebot.message")
    message_mod.run_post_processor = nb.run_post_processor
    message_mod.run_postprocessor = nb.run_post_processor

    adapters = types.ModuleType("nonebot.adapters")
    adapters.__path__ = []
    adapters.Bot = Bot
    onebot = types.ModuleType("nonebot.adapters.onebot")
    onebot.__path__ = []
    v11 = types.ModuleType("nonebot.adapters.onebot.v11")
    v11.Message = Message
    v11.MessageSegment = MessageSegment
    v11.MessageEvent = MessageEvent
    v11.GroupMessageEvent = GroupMessageEvent
    v11.FriendRequestEvent = FriendRequestEvent
    v11.GroupRequestEvent = GroupRequestEvent
    v11.PokeNotifyEvent = PokeNotifyEvent
    v11.GroupDecreaseNoticeEvent = GroupDecreaseNoticeEvent
    v11.GroupIncreaseNoticeEvent = GroupIncreaseNoticeEvent
    v11.Bot = Bot
    onebot.v11 = v11

    class _Rule:
        """nonebot.rule.Rule 最小桩：包装异步规则调用器。真实 nonebot 经 DI 按注解
        注入 (matcher/event/state)，桩里优先按三参调用、失败则退回单参 (event)。"""

        def __init__(self, *callers):
            self.callers = callers

        async def __call__(self, matcher, event, state):
            for caller in self.callers:
                try:
                    ok = await caller(matcher, event, state)
                except TypeError:
                    ok = await caller(event)
                if not ok:
                    return False
            return True

    rule = types.ModuleType("nonebot.rule")
    rule.to_me = lambda: (lambda *_a, **_k: True)
    rule.Rule = _Rule
    params_mod = types.ModuleType("nonebot.params")
    params_mod.CommandArg = lambda: None

    matcher_mod = types.ModuleType("nonebot.matcher")
    matcher_mod.Matcher = _Matcher

    class _NoneBotException(Exception):
        pass

    class _MatcherException(_NoneBotException):
        pass

    class _ProcessException(_NoneBotException):
        pass

    exception_mod = types.ModuleType("nonebot.exception")
    exception_mod.MatcherException = _MatcherException
    exception_mod.SkippedException = _ProcessException
    exception_mod.IgnoredException = _ProcessException
    exception_mod.NoneBotException = _NoneBotException

    aps = types.ModuleType("nonebot_plugin_apscheduler")

    class _Scheduler:
        def __init__(self):
            self.listeners = []

        def scheduled_job(self, *a, **k):
            def deco(fn):
                return fn
            return deco

        def add_listener(self, callback, mask=0):
            self.listeners.append((callback, mask))

        def get_job(self, job_id):
            return None

    aps.scheduler = _Scheduler()

    apscheduler_pkg = types.ModuleType("apscheduler")
    apscheduler_pkg.__path__ = []
    aps_events = types.ModuleType("apscheduler.events")
    aps_events.EVENT_JOB_ERROR = 64  # 与真实值一致（1 << 6）
    aps_events.JobExecutionEvent = object

    psycopg_pool = types.ModuleType("psycopg_pool")
    psycopg_pool.AsyncConnectionPool = object  # 仅满足 import；测试不真正建池

    for name, mod in {
        "nonebot": nb,
        "nonebot.adapters": adapters,
        "nonebot.adapters.onebot": onebot,
        "nonebot.adapters.onebot.v11": v11,
        "nonebot.rule": rule,
        "nonebot.params": params_mod,
        "nonebot.message": message_mod,
        "nonebot.matcher": matcher_mod,
        "nonebot.exception": exception_mod,
        "nonebot_plugin_apscheduler": aps,
        "apscheduler": apscheduler_pkg,
        "apscheduler.events": aps_events,
        "psycopg_pool": psycopg_pool,
    }.items():
        sys.modules[name] = mod


_install_stubs()
