"""LLM 价格图插件：主流大模型 API 价格对比图

命令:
  .ai              实时抓取 OpenRouter（免费 API），失败回退本地缓存
  .ai 更新          强制实时抓取并覆盖缓存（仅主人可用）
  别名: .价格图

数据源: https://openrouter.ai/api/v1/models（无需鉴权）
"""
import asyncio
import json
import logging
import os
import threading
import time
from datetime import datetime

import httpx
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message, MessageEvent, MessageSegment
from nonebot.params import CommandArg
from PIL import Image, ImageDraw, ImageFont

from common import at_prefix, cleanup_cache, is_owner

_logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, "cache")
CACHE_PATH = os.path.join(CACHE_DIR, "latest.json")

API_URL = "https://openrouter.ai/api/v1/models"
FETCH_TIMEOUT = 20
USD_CNY = 7.2
M_TOKENS = 1_000_000

FONT_REG = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"

WHITE = (255, 255, 255)
BLACK = (40, 40, 40)
GRAY = (120, 120, 120)
GRID = (228, 228, 228)
INPUT_COLOR = "#4C9AFF"
OUTPUT_COLOR = "#FF8B3D"
INPUT_TEXT = (30, 95, 168)
OUTPUT_TEXT = (184, 90, 20)

price_cmd = on_command("ai", aliases={"价格图", "模型价格"}, priority=5, block=True)

_http_client: httpx.AsyncClient | None = None
_client_lock = threading.Lock()

# 钉死清单: (openrouter_id, 厂商, 模型名)
MODELS = [
    ("openai/gpt-5.6-luna-pro", "OpenAI", "GPT-5.6 Luna Pro"),
    ("openai/gpt-5.6-terra-pro", "OpenAI", "GPT-5.6 Terra Pro"),
    ("openai/gpt-5.6-sol-pro", "OpenAI", "GPT-5.6 Sol Pro"),
    ("anthropic/claude-opus-5", "Anthropic", "Claude Opus 5"),
    ("anthropic/claude-sonnet-5", "Anthropic", "Claude Sonnet 5"),
    ("google/gemini-3.1-pro-preview", "Google", "Gemini 3.1 Pro"),
    ("google/gemini-3.6-flash", "Google", "Gemini 3.6 Flash"),
    ("google/gemini-3.5-flash-lite", "Google", "Gemini 3.5 Flash Lite"),
    ("deepseek/deepseek-v4-pro-0813", "DeepSeek", "V4 Pro"),
    ("deepseek/deepseek-v4-flash", "DeepSeek", "V4 Flash"),
    ("qwen/qwen3.8-max", "阿里", "千问3.8 Max"),
    ("qwen/qwen3.8-2.4t-a95b", "阿里", "千问3.8 2.4T"),
    ("qwen/qwen3.7-plus", "阿里", "千问3.7 Plus"),
    ("z-ai/glm-5.2", "智谱", "GLM-5.2"),
    ("z-ai/glm-5.1", "智谱", "GLM-5.1"),
    ("moonshotai/kimi-k3", "月之暗面", "Kimi K3"),
    ("moonshotai/kimi-k2.7-code", "月之暗面", "Kimi K2.7 Code"),
    ("x-ai/grok-4.6", "xAI", "Grok 4.6"),
    ("meta-llama/llama-4-maverick", "Meta", "Llama 4 Maverick"),
    ("mistralai/mistral-medium-3-5", "Mistral", "Medium 3.5"),
    ("minimax/minimax-m3", "MiniMax", "M3"),
    ("bytedance-seed/seed-2-1-turbo", "字节", "豆包 Seed 2.1 Turbo"),
    ("bytedance-seed/seed-2.0-lite", "字节", "豆包 Seed 2.0 Lite"),
    ("tencent/hy3", "腾讯", "混元 HY3"),
    ("stepfun/step-3.7-flash", "阶跃", "Step 3.7 Flash"),
    ("baidu/ernie-4.5-vl-424b-a47b", "百度", "ERNIE 4.5 VL"),
    ("xiaomi/mimo-v2.5-pro", "小米", "MiMo V2.5 Pro"),
    ("amazon/nova-premier-v1", "Amazon", "Nova Premier"),
    ("cohere/command-a", "Cohere", "Command A"),
    ("thinkingmachines/inkling", "Thinking Machines", "Inkling"),
]


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        with _client_lock:
            if _http_client is None or _http_client.is_closed:
                _http_client = httpx.AsyncClient(timeout=FETCH_TIMEOUT)
    return _http_client


async def fetch_openrouter() -> dict:
    """实时抓取 OpenRouter, 返回 {id: {in_rmb, out_rmb}}"""
    client = _get_http_client()
    r = await client.get(API_URL, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    data = r.json()
    out = {}
    for m in data.get("data", []):
        pr = m.get("pricing") or {}

        def num(key: str):
            try:
                v = float(pr.get(key, -1))
            except (TypeError, ValueError):
                return None
            return v if v >= 0 else None

        prompt, completion = num("prompt"), num("completion")
        if prompt is None or completion is None:
            continue
        out[m["id"]] = {
            "in_rmb": prompt * M_TOKENS * USD_CNY,
            "out_rmb": completion * M_TOKENS * USD_CNY,
        }
    return out


def load_cache() -> dict | None:
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def save_cache(payload: dict) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CACHE_PATH)


def build_rows(prices: dict, cache: dict | None):
    """组装价格行: [(vendor, name, in_rmb, out_rmb, stale), ...]"""
    cache_models = (cache or {}).get("models", {})
    rows = []
    for mid, vendor, name in MODELS:
        if mid in prices:
            rows.append((vendor, name, prices[mid]["in_rmb"], prices[mid]["out_rmb"], False))
            continue
        item = cache_models.get(mid)
        if item and item.get("in_rmb") is not None:
            rows.append((vendor, name, item["in_rmb"], item["out_rmb"], True))
    return rows


def _log10(v: float) -> float:
    import math
    return math.log10(max(v, 1e-9))


def render_chart(rows, source_desc: str) -> str:
    """PIL 渲染横向分组条形图（对数刻度），返回图片路径"""
    ROW_H, BAR_H, GAP = 46, 15, 3
    X0, X1 = 330, 1545
    HEADER_H, FOOTER_H = 128, 64

    rows = sorted(rows, key=lambda r: r[3], reverse=True)
    n = len(rows)
    W = 1600
    H = HEADER_H + n * ROW_H + FOOTER_H

    try:
        font_title = ImageFont.truetype(FONT_REG, 42)
        font_label = ImageFont.truetype(FONT_REG, 22)
        font_val = ImageFont.truetype(FONT_REG, 18)
        font_tick = ImageFont.truetype(FONT_REG, 20)
        font_foot = ImageFont.truetype(FONT_REG, 20)
        font_legend = ImageFont.truetype(FONT_REG, 24)
    except OSError:
        font_title = font_label = font_val = font_tick = font_foot = font_legend = ImageFont.load_default()

    img = Image.new("RGB", (W, H), WHITE)
    draw = ImageDraw.Draw(img)

    # 对数刻度范围（覆盖 0.5~500 元/百万 tokens）
    vmin, vmax = 0.5, 500.0
    span = _log10(vmax) - _log10(vmin)

    def x_of(v: float) -> float:
        return X0 + (_log10(v) - _log10(vmin)) / span * (X1 - X0)

    # 标题
    draw.text((40, 34), "主流大模型 API 价格对比（元/百万 tokens）", font=font_title, fill=BLACK)
    draw.line([(40, 104), (W - 40, 104)], fill=(220, 220, 220), width=2)

    # 图例（右上角，避免与标题重叠）
    lx = W - 40
    for name, color in (("输出价", OUTPUT_COLOR), ("输入价", INPUT_COLOR)):
        tw = draw.textlength(name, font=font_legend)
        lx -= tw + 40  # 色块 24 + 间距 16
        draw.rectangle([lx, 40, lx + 24, 64], fill=color)
        draw.text((lx + 32, 36), name, font=font_legend, fill=BLACK)

    # 网格与刻度
    for tick in (0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500):
        tx = x_of(tick)
        draw.line([(tx, HEADER_H), (tx, HEADER_H + n * ROW_H)], fill=GRID, width=1)
        draw.text((tx - 14, HEADER_H + n * ROW_H + 6), f"{tick:g}", font=font_tick, fill=GRAY)
    draw.line([(X0, HEADER_H), (X0, HEADER_H + n * ROW_H)], fill=(200, 200, 200), width=2)
    draw.line([(X0, HEADER_H + n * ROW_H), (X1, HEADER_H + n * ROW_H)], fill=(200, 200, 200), width=2)

    # 数据条
    for i, (vendor, name, in_rmb, out_rmb, stale) in enumerate(rows):
        cy = HEADER_H + i * ROW_H + ROW_H / 2
        label = f"{vendor}·{name}" + ("（缓存）" if stale else "")
        draw.text((40, cy - 15), label, font=font_label, fill=BLACK if not stale else GRAY)

        in_x = x_of(in_rmb)
        out_x = x_of(out_rmb)
        y_in = cy - GAP - BAR_H
        y_out = cy + GAP
        draw.rectangle([X0, y_in, in_x, y_in + BAR_H], fill=INPUT_COLOR)
        draw.rectangle([X0, y_out, out_x, y_out + BAR_H], fill=OUTPUT_COLOR)
        draw.text((in_x + 8, y_in - 1), f"{in_rmb:.2f}", font=font_val, fill=INPUT_TEXT)
        draw.text((out_x + 8, y_out - 1), f"{out_rmb:.2f}", font=font_val, fill=OUTPUT_TEXT)

    # 页脚
    draw.line([(40, H - 44), (W - 40, H - 44)], fill=(220, 220, 220), width=2)
    draw.text((40, H - 38),
              f"来源：{source_desc} · 汇率 1 USD = {USD_CNY} CNY · {datetime.now():%Y-%m-%d %H:%M}",
              font=font_foot, fill=GRAY)

    os.makedirs(CACHE_DIR, exist_ok=True)
    cleanup_cache(CACHE_DIR, max_age=24 * 60 * 60)
    path = os.path.join(CACHE_DIR, f"price_{int(time.time() * 1000)}.png")
    img.save(path, "PNG")
    return path


async def gen_chart(force: bool):
    """生成图表, 返回 (图片路径, 摘要文本)"""
    prices = None
    source_desc = ""
    try:
        prices = await fetch_openrouter()
        source_desc = f"OpenRouter 实时数据（{datetime.now():%m-%d %H:%M}）"
    except Exception as e:
        _logger.warning("OpenRouter 抓取失败: %s", e)
        if force:
            raise RuntimeError("实时数据获取失败（强制更新模式不降级使用旧数据）")
    cache = None if force else load_cache()
    if prices is None:
        cache = load_cache()
        if cache:
            source_desc = f"本地缓存（{cache.get('fetched_at', '未知')}）"
        else:
            raise RuntimeError("实时抓取失败，且本地无缓存数据")

    rows = build_rows(prices or {}, cache)
    if not rows:
        raise RuntimeError("没有可用价格数据")

    path = await asyncio.to_thread(render_chart, rows, source_desc)

    if force:
        payload = {
            "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": "openrouter",
            "usd_cny": USD_CNY,
            "models": {mid: prices[mid] for mid, *_ in MODELS if mid in prices},
        }
        save_cache(payload)

    # 摘要
    cheapest = min(rows, key=lambda r: r[3])
    priciest = max(rows, key=lambda r: r[3])
    stale_n = sum(1 for r in rows if r[4])
    lines = [
        f"💸 主流大模型价格对比（{len(rows)} 个旗舰）",
        f"💰 输出最贵：{priciest[0]}·{priciest[1]} ¥{priciest[3]:.2f}/百万",
        f"🪙 输出最便宜：{cheapest[0]}·{cheapest[1]} ¥{cheapest[3]:.2f}/百万",
        f"📡 数据源：{source_desc}",
    ]
    if stale_n:
        lines.append(f"⚠️ 其中 {stale_n} 个模型为缓存价（实时源缺失）")
    return path, "\n".join(lines)


@price_cmd.handle()
async def handle_price(event: MessageEvent, arg: Message = CommandArg()):
    text = arg.extract_plain_text().strip()
    force = "更新" in text or text.lower() in ("update", "-u")
    if force and not is_owner(event):
        await price_cmd.finish("❌ 你没有权限执行更新")

    await price_cmd.send(at_prefix(event) + "⏳ 正在获取大模型价格...")
    try:
        path, summary = await gen_chart(force)
    except Exception as e:
        _logger.exception("价格图生成失败")
        await price_cmd.finish(at_prefix(event) + f"❌ 生成失败：{e}")
    await price_cmd.finish(at_prefix(event) + summary + MessageSegment.image("file://" + path))
