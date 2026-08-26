"""每日晨报：早安问候（日期+一言+情境祝愿）、农历节气、昨日新闻，纯文字推送。"""
import asyncio
import json
import logging
import os
import random
import threading
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import cnlunar
import httpx
from nonebot import get_bot, get_driver, on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent, MessageSegment
from nonebot_plugin_apscheduler import scheduler

from common import close_http_clients, get_http_client, is_owner, save_json_state

_logger = logging.getLogger(__name__)
_SH = ZoneInfo("Asia/Shanghai")

STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")
_LOCK = threading.Lock()

NEWS_API = "https://60s.viki.moe/v2/60s"
HITOKOTO_API = "https://v1.hitokoto.cn/"
AI_API = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
AI_CFG_FILE = os.path.join(os.path.dirname(__file__), "ai_config.json")
_WEEKDAY_CN = "一二三四五六日"
# 每日晨报推送时刻（与 daily_news_job 的 cron 保持一致），启动补发判断用
_PUSH_HOUR, _PUSH_MINUTE = 7, 0


def _get_http_client() -> httpx.AsyncClient:
    # 统一走 common 的按超时缓存单例，由 owstats 注册的 on_shutdown 统一关闭
    return get_http_client(20)


@get_driver().on_shutdown
async def _close_shared_http_clients() -> None:
    await close_http_clients()


news_on_cmd = on_command("新闻开启", priority=5, block=True)
news_off_cmd = on_command("新闻关闭", priority=5, block=True)
news_test_cmd = on_command("新闻测试", priority=5, block=True)
news_status_cmd = on_command("新闻状态", priority=5, block=True)
news_key_cmd = on_command("新闻key", priority=5, block=True)


def _sh_today() -> date:
    """上海时区的今天：与调度器时区一致，不受部署机时区影响。"""
    return datetime.now(_SH).date()


def _load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    # 旧格式 {"group_id": "xxx"} 自动迁移到多群格式
    if "groups" not in data and data.get("group_id"):
        data["groups"] = [str(data["group_id"])]
        data.pop("group_id", None)
        _save_state(data)
    if not isinstance(data.get("groups"), list):
        data["groups"] = []
    return data


def _save_state(data: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def _get_groups() -> list[str]:
    with _LOCK:
        state = _load_state()
        return [str(g) for g in (state.get("groups") or []) if str(g)]


def _add_group(gid: str) -> None:
    with _LOCK:
        state = _load_state()
        groups = {str(g) for g in (state.get("groups") or [])}
        groups.add(gid)
        state["groups"] = sorted(groups)
        _save_state(state)


def _remove_group(gid: str) -> None:
    with _LOCK:
        state = _load_state()
        groups = {str(g) for g in (state.get("groups") or [])}
        groups.discard(gid)
        state["groups"] = sorted(groups)
        _save_state(state)


def _last_push_date() -> str:
    """读取最近一次晨报推送日期（YYYY-MM-DD，上海时区）；从未推送返回空串。"""
    with _LOCK:
        return str(_load_state().get("last_push_date") or "")


def _mark_pushed(day: date) -> None:
    """记录当日晨报已推送完成，用于防重复推送与启动补发判断。"""
    with _LOCK:
        state = _load_state()
        state["last_push_date"] = day.isoformat()
        _save_state(state)


def _join_str(value) -> str:
    """cnlunar 的节日字段可能返回 str 或 list，统一转成一段文本。"""
    if isinstance(value, (list, tuple)):
        return "".join(str(x) for x in value)
    return str(value or "")


# cnlunar 未覆盖的公历固定日期节日
_FIXED_FESTIVALS = {
    (10, 31): "万圣节",
    (12, 24): "平安夜",
}

_FESTIVAL_EMOJI = {
    "春节": "🧨", "除夕": "🏮", "元宵节": "🏮", "清明节": "🌿", "端午节": "🐉",
    "七夕": "💕", "中秋节": "🥮", "重阳节": "🍂", "腊八节": "🥣",
    "情人节": "💕", "母亲节": "👩", "父亲节": "👨", "儿童节": "🎈", "教师节": "🌸",
    "圣诞节": "🎄", "平安夜": "🍎", "万圣节": "🎃", "感恩节": "🦃", "国庆节": "🎉", "元旦": "🎉",
}


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> int:
    """某月第 n 个星期 weekday 是几号；weekday: 0=周一 … 6=周日。"""
    first = date(year, month, 1)
    d = first + timedelta(days=(weekday - first.weekday()) % 7)
    return d.day + 7 * (n - 1)


def _rule_festivals(day: date) -> str:
    """规则型公历节日：感恩节（11 月第 4 个周四）。"""
    if day.month == 11 and day.day == _nth_weekday(day.year, 11, 3, 4):
        return "感恩节"
    return ""


def _lunar_festivals(a, day: date) -> str:
    """农历节日：七夕（七月初七）、重阳（九月初九）、除夕（腊月最后一天）。"""
    if a.lunarMonth == 7 and a.lunarDay == 7:
        return "七夕"
    if a.lunarMonth == 9 and a.lunarDay == 9:
        return "重阳节"
    if str(a.lunarMonthCn or "").startswith("腊") and str(a.lunarDayCn) in ("廿九", "三十"):
        nxt = cnlunar.Lunar(datetime(day.year, day.month, day.day) + timedelta(days=1), godType="8char")
        if str(nxt.lunarMonthCn or "").startswith("正") and str(nxt.lunarDayCn) == "初一":
            return "除夕"
    return ""


def _lunar_info(day: date) -> tuple[str, str, bool]:
    """返回 (农历行文本, 节日名, 是否法定假日)；本地计算，不依赖外部 API。"""
    a = cnlunar.Lunar(datetime(day.year, day.month, day.day), godType="8char")
    month = str(a.lunarMonthCn or "")
    if month and month[-1] in "大小":
        month = month[:-1]
    parts = [f"农历{month}{a.lunarDayCn}"]
    term = str(a.todaySolarTerms or "").strip()
    if term and term != "无":
        parts.append(f"节气 {term}")
    lunar_line = " · ".join(parts)

    legal = _join_str(a.get_legalHolidays()).strip()
    if legal:
        return lunar_line, legal, True
    other = _join_str(a.get_otherHolidays()).strip()
    if other:
        return lunar_line, other, False
    festival = _lunar_festivals(a, day) or _rule_festivals(day) or _FIXED_FESTIVALS.get((day.month, day.day), "")
    return lunar_line, festival, False


def _lunar_line(day: date) -> str:
    return _lunar_info(day)[0]


_FALLBACK_QUOTES = [
    "若心如朝阳，所见皆朝霞。",
    "心有暖阳，何惧沧桑。",
    "晨光熹微，万物皆可爱。",
    "把每一个清晨，都当作崭新的礼物。",
    "日子缓缓，阳光暖暖，好事正在路上。",
]

_WEEKEND_WISHES = [
    "愿你在这个周末的清晨醒来，阳光很暖，微风很甜，不用赶时间，不用想工作，好好享受属于自己的时光。",
    "周末的早晨不用设闹钟，愿你睡到自然醒，慢慢吃一顿早餐，把时间花在喜欢的事情上。",
    "难得的周末，愿你卸下一周的疲惫，出门晒晒太阳，或者窝在家里发发呆，怎么舒服怎么来。",
]

_WEEKDAY_WISHES = [
    "愿你新的一天元气满满，记得好好吃早餐，再忙也别忘了抬头看看天。",
    "愿你今天出门顺利，遇见的都是善意，做的每件事都有着落。",
    "新的一天，愿你步履轻快，心里有光，把小事一件件做完，把日子一点点过好。",
]

_HOLIDAY_WISHES = [
    "假期快乐，愿你把日子过成自己喜欢的样子。",
    "难得的假日，愿你吃好睡好玩好，给自己充满电。",
]

_CLOSINGS = [
    "万物可爱，人间值得。",
    "愿你眼里有光，心中有暖。",
    "日子滚烫，温暖又明亮。",
    "慢慢来，一切都会更好的。",
    "今天也要开开心心的呀。",
]


def _context_wish(day: date, festival: str = "", legal: bool = False) -> str:
    """按 法定假日 > 周末 > 工作日 挑一句贴合情境的祝愿（普通节日不占用假日文案）。"""
    if festival and legal:
        return random.choice(_HOLIDAY_WISHES)
    if day.weekday() >= 5:
        return random.choice(_WEEKEND_WISHES)
    return random.choice(_WEEKDAY_WISHES)


def _build_greeting(day: date, quote: str = "", festival: str = "", legal: bool = False) -> str:
    """组装兜底早安问候：一言 + 情境祝愿 + 收尾（日期信息在消息首行另行展示）。"""
    poetic = quote.strip() or random.choice(_FALLBACK_QUOTES)
    return f"早安。{poetic}{_context_wish(day, festival, legal)}{random.choice(_CLOSINGS)}"


async def _fetch_news(day: date) -> list[str]:
    """取指定日期的「每天60秒读懂世界」新闻列表。"""
    client = _get_http_client()
    r = await client.get(NEWS_API, params={"date": day.isoformat()}, timeout=20)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 200:
        raise RuntimeError(data.get("message", "unknown"))
    news = (data.get("data") or {}).get("news") or []
    return [str(x).strip() for x in news if str(x).strip()]


def _parse_hitokoto(data: dict) -> str:
    return str(data.get("hitokoto") or "").strip()


_ai_cfg_cache: dict = {"mtime": -1.0, "key": "", "model": "glm-4-flash"}
_ai_cfg_warned = False


def _load_ai_cfg() -> tuple[str, str]:
    """读取晨报 AI 配置（api_key/model），带 mtime 缓存；未配置返回空 key。"""
    global _ai_cfg_warned
    mtime: float | None = None
    try:
        mtime = os.path.getmtime(AI_CFG_FILE)
        if mtime != _ai_cfg_cache["mtime"]:
            with open(AI_CFG_FILE, encoding="utf-8") as f:
                data = json.load(f)
            _ai_cfg_cache.update(
                mtime=mtime,
                key=str(data.get("api_key") or "").strip(),
                model=str(data.get("model") or "glm-4-flash").strip(),
            )
    except FileNotFoundError:
        # 未配置属于正常情况：清空缓存但不告警
        _ai_cfg_cache.update(key="", model="glm-4-flash")
    except Exception:
        # 读取/解析失败：只警告一次避免每次调用刷屏
        if not _ai_cfg_warned:
            _ai_cfg_warned = True
            _logger.warning("晨报 AI 配置读取失败，按未配置处理", exc_info=True)
        # 已拿到 mtime 时记录之，文件未变化前不再反复重读同一个坏文件
        _ai_cfg_cache.update(
            mtime=mtime if mtime is not None else _ai_cfg_cache["mtime"],
            key="",
            model="glm-4-flash",
        )
    return _ai_cfg_cache["key"], _ai_cfg_cache["model"]


def _save_ai_cfg(key: str) -> None:
    _, model = _load_ai_cfg()
    save_json_state(AI_CFG_FILE, {"api_key": key, "model": model})
    _ai_cfg_cache.update(mtime=-1.0)  # 强制下次重新读取


def _greeting_messages(day: date, lunar_line: str, festival: str = "", legal: bool = False) -> list:
    """构造生成早安问候的消息列表，附上日期/农历/节气/节日等真实上下文。"""
    weekday = f"星期{_WEEKDAY_CN[day.weekday()]}"
    if festival and legal:
        situation = f"今天是法定假日「{festival}」"
    elif festival:
        situation = f"今天是{festival}"
    elif day.weekday() >= 5:
        situation = "今天是周末"
    else:
        situation = "今天是工作日"
    context = f"今天是{day.year}年{day.month}月{day.day}日，{weekday}，{lunar_line}，{situation}。"
    req = (
        f"{context}请写一段 80~120 字的早安问候：以「早安。」开头，"
        "接着一句简短诗意的话，再接两三句贴合当天情境（周末/节气/节日）的温暖祝愿，最后一句简短收尾。"
        "语气自然温暖，像朋友发的早安消息；不要 emoji、不要引号、不要换行、"
        "不要华丽辞藻堆砌，也不要再重复日期、星期、农历等信息（这些会另行展示）。"
    )
    return [
        {"role": "system", "content": "你是温暖的早安问候文案作者，只输出问候正文本身。"},
        {"role": "user", "content": req},
    ]


def _sanitize_greeting(text: str) -> str:
    """清理 AI 输出：只取第一行，去掉首尾包裹引号与空白。"""
    lines = str(text or "").strip().splitlines()
    t = lines[0].strip() if lines else ""
    for left, r in (("「", "」"), ("“", "”"), ('"', '"')):
        if len(t) >= 2 and t.startswith(left) and t.endswith(r):
            t = t[1:-1].strip()
    return t


async def _fetch_ai_greeting(day: date, lunar_line: str, festival: str = "", legal: bool = False) -> str:
    """用智谱免费 GLM-Flash 生成早安问候；未配置 key 返回空串，请求失败抛异常。"""
    key, model = _load_ai_cfg()
    if not key:
        return ""
    client = _get_http_client()
    r = await client.post(
        AI_API,
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": model,
            "messages": _greeting_messages(day, lunar_line, festival, legal),
            "max_tokens": 200,
            "temperature": 0.9,
        },
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    choices = data.get("choices") or [{}]
    content = (choices[0].get("message") or {}).get("content") or ""
    return _sanitize_greeting(content)


async def _fetch_quote() -> str:
    """从一言 API 取一句文学/诗词类句子；失败返回空串（调用方用本地句子兜底）。"""
    client = _get_http_client()
    r = await client.get(
        HITOKOTO_API,
        params=[("c", "d"), ("c", "i"), ("encode", "json")],
        timeout=10,
    )
    r.raise_for_status()
    return _parse_hitokoto(r.json())


async def _build_message(day: date) -> str:
    """组装晨报文本。问候优先用免费 AI 生成，失败回退本地拼接；日期与农历永在。"""
    lunar_line, festival, legal = _lunar_info(day)
    greeting = ""
    try:
        greeting = await _fetch_ai_greeting(day, lunar_line, festival, legal)
    except Exception as exc:
        _logger.warning("晨报 AI 生成失败，回退本地问候: %s", exc)
    if not greeting:
        quote = ""
        try:
            quote = await _fetch_quote()
        except Exception as exc:
            _logger.warning("一言获取失败，使用本地句子: %s", exc)
        greeting = _build_greeting(day, quote, festival, legal)
    head = f"今天是{day.month}月{day.day}日，星期{_WEEKDAY_CN[day.weekday()]}"
    if festival:
        head += f"，{_FESTIVAL_EMOJI.get(festival, '🎉')} {festival}"
    lines = [
        head + f"，🌙 {lunar_line}",
        greeting,
    ]
    # 新闻取昨日（60s 每天早上更新的是前一天的内容）；昨日取不到时回退当日
    for news_day in (day - timedelta(days=1), day):
        try:
            items = (await _fetch_news(news_day))[:10]
            if items:
                lines.append("")
                lines.append(f"📰 昨日新闻（{news_day.month}月{news_day.day}日）：")
                lines.extend(f"· {x}" for x in items)
                break
        except Exception as exc:
            _logger.warning("新闻获取失败（%s）: %s", news_day, exc)
    return "\n".join(lines)


_push_running = False  # 推送进行中标记：定时触发与启动补发恰好并发时只跑一路


async def _send_daily() -> bool:
    """推送当日晨报到所有订阅群；至少一个群送达才记录 last_push_date 并返回 True。"""
    global _push_running
    today = _sh_today().isoformat()
    if _last_push_date() == today:
        return True  # 今日已推送（定时与补发共用此标记，防重复）
    if _push_running:  # 另一路正在推送本次晨报，无需重复
        return False
    _push_running = True
    try:
        if _last_push_date() == today:  # 双重检查：进入前另一路可能刚好推完
            return True
        groups = _get_groups()
        if not groups:
            return False
        try:
            text = await _build_message(_sh_today())
        except Exception:
            _logger.exception("晨报内容生成失败")
            return False
        try:
            bot = get_bot()
        except Exception:
            _logger.exception("获取 bot 失败")
            return False
        # 并发发送：单个群失败不影响其他群
        # MessageSegment.text 包裹：一言/60s新闻/AI 生成的外部文本不会被解析为 CQ 码（防注入）
        results = await asyncio.gather(
            *(
                bot.send_group_msg(group_id=int(gid), message=MessageSegment.text(text))
                for gid in groups
            ),
            return_exceptions=True,
        )
        sent = 0
        for gid, result in zip(groups, results, strict=False):
            if isinstance(result, BaseException):
                _logger.warning("晨报发送到群 %s 失败", gid, exc_info=result)
            else:
                sent += 1
        if sent:
            _mark_pushed(_sh_today())  # 有群成功送达才记录，全失败保留补发机会
        return bool(sent)
    finally:
        _push_running = False


@scheduler.scheduled_job("cron", hour=_PUSH_HOUR, minute=_PUSH_MINUTE, id="daily_news", timezone="Asia/Shanghai")
async def daily_news_job():
    if _last_push_date() == _sh_today().isoformat():
        return  # 今日已推送（如启动补发已执行过），防重复
    await _send_daily()


# 启动补发：APScheduler 用内存 jobstore，进程重启后错过的当日晨报静默丢失，
# bot 连上后检查"已过推送时刻且今日未推送"则立即补发一次。
# 优先 on_bot_connect（此时 get_bot 可用）；无该钩子的环境（旧版 nonebot / 测试 stub）回退 on_startup。
_register_catchup = getattr(get_driver(), "on_bot_connect", get_driver().on_startup)


@_register_catchup
async def _daily_news_catchup(bot: Bot) -> None:
    now = datetime.now(_SH)
    if (now.hour, now.minute) < (_PUSH_HOUR, _PUSH_MINUTE):
        return  # 还没到当日推送时刻，交给定时任务
    if _last_push_date() >= now.date().isoformat():
        return  # 今日已推送（last_push_date 为今天或更晚），不重复
    await _send_daily()


@news_on_cmd.handle()
async def news_on(event: MessageEvent):
    if not is_owner(event):
        await news_on_cmd.finish("❌ 你没有权限使用此功能")
    if not isinstance(event, GroupMessageEvent):
        await news_on_cmd.finish("请在有机器人的群里开启此功能")
    _add_group(str(event.group_id))
    await news_on_cmd.finish("✅ 本群已开启每日晨报推送\n每天 07:00 发送「早安问候 · 农历节气 · 昨日新闻」")


@news_off_cmd.handle()
async def news_off(event: MessageEvent):
    if not is_owner(event):
        await news_off_cmd.finish("❌ 你没有权限使用此功能")
    if not isinstance(event, GroupMessageEvent):
        await news_off_cmd.finish("请在有机器人的群里关闭此功能")
    _remove_group(str(event.group_id))
    await news_off_cmd.finish("✅ 本群已关闭每日晨报推送")


@news_test_cmd.handle()
async def news_test(event: MessageEvent):
    if not is_owner(event):
        await news_test_cmd.finish("❌ 你没有权限使用此功能")
    if not isinstance(event, GroupMessageEvent):
        await news_test_cmd.finish("请在群里使用此命令")
    try:
        text = await _build_message(_sh_today())
    except Exception:
        await news_test_cmd.finish("晨报内容生成失败，请稍后再试")
    # MessageSegment.text 包裹：外部 API/AI 文本不会被解析为 CQ 码（防注入）
    await news_test_cmd.finish(MessageSegment.text(text))


@news_key_cmd.handle()
async def news_key(event: MessageEvent):
    if not is_owner(event):
        await news_key_cmd.finish("❌ 你没有权限使用此功能")
    if isinstance(event, GroupMessageEvent):
        await news_key_cmd.finish("⚠️ 请私聊我发送 .新闻key <key>，避免 key 泄露")
    parts = event.get_plaintext().split(maxsplit=1)
    key = parts[1].strip() if len(parts) == 2 else ""
    if not key:
        saved_key, model = _load_ai_cfg()
        state = f"已配置 {model}（{saved_key[:6]}…）" if saved_key else "未配置（当前用本地问候兜底）"
        await news_key_cmd.finish(
            f"🤖 晨报 AI：{state}\n"
            "用法：.新闻key <智谱APIkey>\n"
            "免费获取：open.bigmodel.cn 注册 → 右上角「API Keys」创建"
        )
    _save_ai_cfg(key)
    await news_key_cmd.finish("✅ 晨报 AI key 已保存，之后早安问候将由 GLM-Flash 生成")


@news_status_cmd.handle()
async def news_status(event: MessageEvent):
    if not is_owner(event):
        await news_status_cmd.finish("❌ 你没有权限使用此功能")
    groups = _get_groups()
    if groups:
        await news_status_cmd.finish(f"📰 每日晨报推送已开启于 {len(groups)} 个群（每天 07:00 发送）：\n{'、'.join(groups)}")
    await news_status_cmd.finish("📰 每日晨报推送：未开启")
