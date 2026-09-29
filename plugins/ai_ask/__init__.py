"""`.ai <问题>`：免费 AI 问答（智谱 GLM-Flash，复用晨报已配置的免费 key）。

网页版 ChatGPT 需要挂真实账号会话且对抗风控，封号风险高、随时失效，不采用；
GLM-Flash 对个人开放平台免费，与晨报共用同一把 key（经 news.get_glm_config
公开接口读取，未配置时引导主人 .新闻key 设置）。usage.json 记录每用户每日
提问次数（QQBOT_AI_ASK_DAILY_LIMIT 可调，主人豁免），usage.json 已在 .gitignore。
"""
import asyncio
import importlib
import logging
import os
import re
import threading
import time
from collections import OrderedDict

from nonebot import on_command
from nonebot.adapters.onebot.v11 import MessageEvent, MessageSegment

from common import get_http_client, is_owner, load_json_state, save_json_state

_logger = logging.getLogger(__name__)

AI_API = "https://open.bigmodel.cn/api/paas/v4/chat/completions"

ai_cmd = on_command("ai", aliases={"AI", "问AI"}, priority=5, block=True)

_SYSTEM_PROMPT = (
    "你是QQ群里的AI助手。用简洁准确的中文回答；默认不超过300字；"
    "不要重复用户的问题；代码/名单等结构化内容可用换行排版。"
)
_CLIP_LEN = 1500  # 回答截断上限，防止超长回答刷屏
_COOLDOWN_SECONDS = 5.0
_COOLDOWN_MAX = 4096
_DAILY_LIMIT = int(os.getenv("QQBOT_AI_ASK_DAILY_LIMIT", "30") or "30")
_USAGE_FILE = os.path.join(os.path.dirname(__file__), "usage.json")
# 必须用 RLock：load_json_state/save_json_state 内部会再获取传入的锁，
# _usage_incr 持锁期间调用它们，普通 Lock 会不可重入死锁（与 8/27 修复约定一致）
_usage_lock = threading.RLock()
_GLM_SEM = asyncio.Semaphore(2)  # 免费档并发有限，全局最多 2 路在途

_cooldowns: OrderedDict[str, float] = OrderedDict()
_cooldown_lock = threading.Lock()


def _news_mod():
    """延迟导入晨报插件，经公开接口 get_glm_config 复用免费 GLM 配置。"""
    for name in ("plugins.news", "news"):
        try:
            return importlib.import_module(name)
        except Exception:
            continue
    return None


def _cooldown_remaining(uid: str) -> int:
    now = time.monotonic()
    with _cooldown_lock:
        for k, exp in list(_cooldowns.items()):
            if exp <= now:
                _cooldowns.pop(k, None)
        exp = _cooldowns.get(uid, 0.0)
        if exp > now:
            _cooldowns.move_to_end(uid)
            return max(1, int(exp - now + 0.999))
        _cooldowns[uid] = now + _COOLDOWN_SECONDS
        _cooldowns.move_to_end(uid)
        while len(_cooldowns) > _COOLDOWN_MAX:
            _cooldowns.popitem(last=False)
    return 0


def _cooldown_release(uid: str) -> None:
    """提问失败、未产出回答时回滚冷却，允许用户立即重试。"""
    with _cooldown_lock:
        _cooldowns.pop(uid, None)


def _usage_count(uid: str, day: str) -> int:
    data = load_json_state(_USAGE_FILE, _usage_lock)
    day_data = data.get(day) if isinstance(data.get(day), dict) else {}
    try:
        return int(day_data.get(uid) or 0)
    except (TypeError, ValueError):
        return 0


def _usage_incr(uid: str, day: str) -> None:
    with _usage_lock:
        data = load_json_state(_USAGE_FILE, _usage_lock)
        day_data = data.get(day) if isinstance(data.get(day), dict) else {}
        day_data[uid] = int(day_data.get(uid) or 0) + 1
        # 只保留最近 2 天，防止文件无限增长
        days = sorted({*data.keys(), day})[-2:]
        out = {d: data[d] for d in days if isinstance(data.get(d), dict)}
        out[day] = day_data
        save_json_state(_USAGE_FILE, out, _usage_lock)


def _clip(text: str) -> str:
    text = text.strip()
    if len(text) <= _CLIP_LEN:
        return text
    return text[:_CLIP_LEN] + "\n…（回答过长已截断）"


async def _ask_glm(model: str, key: str, messages: list) -> str:
    """调用智谱免费 GLM；返回空内容视为失败（免费档偶发空回复，交上层重试提示）。"""
    async with _GLM_SEM:
        client = get_http_client(60)
        r = await client.post(
            AI_API,
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model, "messages": messages,
                  "max_tokens": 1024, "temperature": 0.7},
            timeout=60,
        )
    r.raise_for_status()
    data = r.json()
    choices = data.get("choices") or [{}]
    content = str((choices[0].get("message") or {}).get("content") or "").strip()
    if not content:
        raise RuntimeError("GLM 返回空内容")
    return content


@ai_cmd.handle()
async def handle_ai(event: MessageEvent):
    raw = event.get_plaintext().strip()
    m = re.match(r"^[\.。]?\s*(?:ai|问ai)\s*(.*)$", raw, re.IGNORECASE | re.DOTALL)
    q = (m.group(1) if m else "").strip()
    if not q:
        await ai_cmd.finish("用法：.ai <问题>\n例：.ai 帮我写一句给朋友的生日祝福")
    uid = str(getattr(event, "user_id", "0") or "0")
    remaining = _cooldown_remaining(uid)
    if remaining:
        await ai_cmd.finish(f"⏳ 提问太频繁，请 {remaining} 秒后再试")
    day = time.strftime("%Y%m%d")
    if _DAILY_LIMIT > 0 and not is_owner(event) and _usage_count(uid, day) >= _DAILY_LIMIT:
        await ai_cmd.finish(f"⚠️ 你今日的 .ai 提问已达上限 {_DAILY_LIMIT} 次，明天再来吧")

    news = _news_mod()
    key, model = "", "glm-4-flash"
    if news is not None and hasattr(news, "get_glm_config"):
        key, model = news.get_glm_config()
    if not key:
        _cooldown_release(uid)
        await ai_cmd.finish(
            "⚠️ 免费智谱 key 还没配置：请主人私聊发送 .新闻key <key> 设置后即可使用"
        )

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": q},
    ]
    try:
        answer = await _ask_glm(model, key, messages)
    except Exception:
        _cooldown_release(uid)
        _logger.exception(".ai 问答失败")
        await ai_cmd.finish("⚠️ AI 服务暂时不可用，请稍后再试")
    _usage_incr(uid, day)
    # GLM 输出为外部内容（防 CQ 码注入）：一律 MessageSegment.text 发送
    await ai_cmd.finish(MessageSegment.text(_clip(answer)))
