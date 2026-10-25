"""`.ai <问题>`：免费 AI 问答，多供应商故障转移（智谱 → OpenRouter → Gemini）。

三家都是免费档，接口全部 OpenAI 兼容，区别只在 URL/model/key：
- 智谱 GLM-Flash：晨报共用 key（news.get_glm_config 公开接口），国内直连，无日上限；
- OpenRouter :free 模型：50 次/天，走 clash 代理；
- Gemini（OpenAI 兼容端点）：免费档，走 clash 代理。
任一供应商成功即返回，全部失败才报错。OpenRouter/Gemini 的 key 由主人
.aikey 命令设置，存 config.json（.gitignore 已覆盖 plugins/**/config.json）。
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
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent, MessageSegment

from common import get_http_client, is_owner, load_json_state, save_json_state

_logger = logging.getLogger(__name__)

GLM_API = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
OPENROUTER_API = "https://openrouter.ai/api/v1/chat/completions"
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"

# OpenRouter/Gemini 国内不可直连，走 clash；AI_ASK_PROXY 可覆盖，BTD6_PROXY 次之，设空串禁用
PROXY = (os.getenv("AI_ASK_PROXY") or os.getenv("BTD6_PROXY") or "http://127.0.0.1:7890").strip()

ai_cmd = on_command("ai", aliases={"AI", "问AI"}, priority=5, block=True)
aikey_cmd = on_command("aikey", aliases={"AIKey"}, priority=5, block=True)

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
# 持锁期间调用它们，普通 Lock 会不可重入死锁（与 8/27 修复约定一致）
_usage_lock = threading.RLock()
_CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")
_config_lock = threading.RLock()
_GLM_SEM = asyncio.Semaphore(2)  # 免费档并发有限，单供应商全局最多 2 路在途

_cooldowns: OrderedDict[str, float] = OrderedDict()
_cooldown_lock = threading.Lock()

# ---------------- 对话上下文（按 群/私聊+用户 隔离，仅内存不落盘） ----------------
# 保留最近 _HIST_MAX 条 user/assistant 消息；_HIST_TTL 内无追问视为新话题。
# bot 重启即清空，避免群聊内容落盘的隐私残留。
_HIST_MAX = 8
_HIST_TTL = 30 * 60.0
_HIST_MAX_ENTRIES = 512
_histories: dict[str, tuple[float, list]] = {}
_hist_lock = threading.RLock()  # RLock：_hist_store 持锁期间调用 _hist_load 重入同锁


def _hist_key(event) -> str:
    gid = getattr(event, "group_id", None)
    return f"{gid if gid is not None else 'private'}:{getattr(event, 'user_id', '0')}"


def _hist_load(key: str) -> list:
    with _hist_lock:
        entry = _histories.get(key)
        if not entry:
            return []
        last, msgs = entry
        if time.monotonic() - last > _HIST_TTL:
            _histories.pop(key, None)
            return []
        return list(msgs)


def _hist_store(key: str, q: str, answer: str) -> None:
    with _hist_lock:
        msgs = _hist_load(key)
        msgs.append({"role": "user", "content": q})
        msgs.append({"role": "assistant", "content": answer})
        _histories[key] = (time.monotonic(), msgs[-_HIST_MAX:])
        if len(_histories) > _HIST_MAX_ENTRIES:  # 过期与最旧会话清扫，防无界增长
            now = time.monotonic()
            for k in [k for k, (t, _) in _histories.items() if now - t > _HIST_TTL]:
                _histories.pop(k, None)
            while len(_histories) > _HIST_MAX_ENTRIES:
                _histories.pop(next(iter(_histories)))


def _news_mod():
    """延迟导入晨报插件，经公开接口 get_glm_config 复用免费 GLM 配置。"""
    for name in ("plugins.news", "news"):
        try:
            return importlib.import_module(name)
        except Exception:
            continue
    return None


def _provider_chain() -> list[tuple[str, str, str, str, str | None]]:
    """按优先级返回已配置的供应商：(名称, api_url, model, key, proxy)。

    key 未配置的供应商跳过；全部未配置返回空列表（由调用方给引导文案）。
    """
    chain: list[tuple[str, str, str, str, str | None]] = []
    news = _news_mod()
    if news is not None and hasattr(news, "get_glm_config"):
        key, model = news.get_glm_config()
        if key:
            chain.append(("智谱", GLM_API, model or "glm-4-flash", key, None))
    cfg = load_json_state(_CONFIG_FILE, _config_lock)
    if cfg.get("openrouter_key"):
        chain.append(("OpenRouter", OPENROUTER_API,
                      str(cfg.get("openrouter_model") or "openrouter/free"),
                      str(cfg["openrouter_key"]), PROXY or None))
    if cfg.get("gemini_key"):
        chain.append(("Gemini", GEMINI_API,
                      str(cfg.get("gemini_model") or "gemini-2.5-flash"),
                      str(cfg["gemini_key"]), PROXY or None))
    return chain


def _mask(key: str) -> str:
    return f"{key[:6]}***{key[-4:]}" if len(key) > 12 else "***"


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


async def _chat_completion(name: str, url: str, model: str, key: str,
                           messages: list, proxy: str | None) -> str:
    """统一 OpenAI 兼容调用；返回空内容视为失败（免费档偶发空回复）。"""
    headers = {"Authorization": f"Bearer {key}"}
    if "openrouter.ai" in url:
        # OpenRouter 建议的归属标识头
        headers["HTTP-Referer"] = "https://github.com/qqbot-local"
        headers["X-Title"] = "QQBot ai_ask"
    async with _GLM_SEM:
        client = get_http_client(60, proxy=proxy)
        r = await client.post(
            url,
            headers=headers,
            json={"model": model, "messages": messages,
                  "max_tokens": 1024, "temperature": 0.7},
            timeout=60,
        )
    r.raise_for_status()
    data = r.json()
    choices = data.get("choices") or [{}]
    content = str((choices[0].get("message") or {}).get("content") or "").strip()
    if not content:
        raise RuntimeError(f"{name} 返回空内容")
    return content


async def _ask_any(q: str, history: list | None = None) -> tuple[str, str]:
    """依次尝试供应商链，首个成功即返回 (回答, 供应商名)；全部失败抛汇总异常。

    history 为该用户最近的 user/assistant 消息（上下文），随供应商一并发送。
    """
    messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
    messages += (history or [])[-_HIST_MAX:]
    messages.append({"role": "user", "content": q})
    errors: list[str] = []
    for name, url, model, key, proxy in _provider_chain():
        try:
            return await _chat_completion(name, url, model, key, messages, proxy), name
        except Exception:
            errors.append(name)
            _logger.warning(".ai 供应商 %s 调用失败，切换下一个", name, exc_info=True)
    raise RuntimeError("全部供应商失败: " + "、".join(errors) if errors else "无可用供应商")


@ai_cmd.handle()
async def handle_ai(event: MessageEvent):
    raw = event.get_plaintext().strip()
    m = re.match(r"^[\.。]?\s*(?:ai|问ai)\s*(.*)$", raw, re.IGNORECASE | re.DOTALL)
    q = (m.group(1) if m else "").strip()
    if not q:
        await ai_cmd.finish("用法：.ai <问题>\n例：.ai 帮我写一句给朋友的生日祝福\n连续追问即可带上下文；.ai 重置 开新话题")
    # 重置上下文是本地操作：放在冷却检查之前，追问失败后也能随时开新话题
    if q in {"重置", "新话题", "reset"}:
        with _hist_lock:
            _histories.pop(_hist_key(event), None)
        await ai_cmd.finish("🔄 已清空对话上下文，开始新话题")
    uid = str(getattr(event, "user_id", "0") or "0")
    remaining = _cooldown_remaining(uid)
    if remaining:
        await ai_cmd.finish(f"⏳ 提问太频繁，请 {remaining} 秒后再试")
    day = time.strftime("%Y%m%d")
    if _DAILY_LIMIT > 0 and not is_owner(event) and _usage_count(uid, day) >= _DAILY_LIMIT:
        await ai_cmd.finish(f"⚠️ 你今日的 .ai 提问已达上限 {_DAILY_LIMIT} 次，明天再来吧")
    if not _provider_chain():
        _cooldown_release(uid)
        await ai_cmd.finish(
            "⚠️ 还没有可用的 AI 供应商，请主人配置任意一家：\n"
            "1）.新闻key <智谱key>（免费，推荐）\n"
            "2）.aikey openrouter <key>\n"
            "3）.aikey gemini <key>"
        )
    try:
        answer, _provider = await _ask_any(q, _hist_load(_hist_key(event)))
    except Exception:
        _cooldown_release(uid)
        _logger.exception(".ai 全部供应商失败")
        await ai_cmd.finish("⚠️ AI 服务暂时不可用，请稍后再试")
    _usage_incr(uid, day)
    _hist_store(_hist_key(event), q, _clip(answer))  # 仅成功时记忆，失败不污染上下文
    # AI 输出为外部内容（防 CQ 码注入）：一律 MessageSegment.text 发送
    await ai_cmd.finish(MessageSegment.text(_clip(answer)))


@aikey_cmd.handle()
async def handle_aikey(event: MessageEvent):
    if not is_owner(event):
        await aikey_cmd.finish("❌ 仅机器人主人可管理 AI 供应商 key")
    if isinstance(event, GroupMessageEvent):
        # key 会随消息留在群聊记录里：与 .新闻key 同款策略，只允许私聊设置
        await aikey_cmd.finish("⚠️ 请私聊我发送 .aikey openrouter <key> 设置，避免 key 泄露")
    tokens = event.get_plaintext().split()
    args = [t for t in tokens if "aikey" not in t.lower()]
    if not args:
        # 状态一览（key 脱敏）
        cfg = load_json_state(_CONFIG_FILE, _config_lock)
        news = _news_mod()
        glm_key = ""
        if news is not None and hasattr(news, "get_glm_config"):
            glm_key = news.get_glm_config()[0]
        lines = ["🔑 AI 供应商配置：",
                 f"智谱：{'已配置 ' + _mask(glm_key) if glm_key else '未配置（.新闻key 设置）'}",
                 f"OpenRouter：{'已配置 ' + _mask(str(cfg.get('openrouter_key'))) if cfg.get('openrouter_key') else '未配置'}",
                 f"Gemini：{'已配置 ' + _mask(str(cfg.get('gemini_key'))) if cfg.get('gemini_key') else '未配置'}",
                 "设置：.aikey openrouter <key> / .aikey gemini <key>"]
        await aikey_cmd.finish(MessageSegment.text("\n".join(lines)))
    target = args[0].lower()
    if target not in {"openrouter", "gemini"}:
        await aikey_cmd.finish("用法：.aikey openrouter <key> 或 .aikey gemini <key>")
    if len(args) < 2:
        await aikey_cmd.finish(f"请附上 key：.aikey {target} <key>")
    with _config_lock:
        cfg = load_json_state(_CONFIG_FILE, _config_lock)
        cfg[f"{target}_key"] = args[1].strip()
        save_json_state(_CONFIG_FILE, cfg, _config_lock)
    await aikey_cmd.finish(f"✅ {target} key 已保存（{target} 通道将在智谱失败时自动启用）")
