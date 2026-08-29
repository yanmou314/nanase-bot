"""BTD6（Bloons TD 6）情报站：当前活动总览、活动排行榜、活动规则、自制地图查询。

数据源：Ninja Kiwi 官方开放数据 API（https://data.ninjakiwi.com/btd6），无需鉴权。
查询结果渲染为图片卡片（weasyprint），地图图 / Boss 头像等游戏素材取自官方静态
CDN 并落盘缓存；后台定时预热数据与热门卡片，命令到达时通常可直接发送缓存图。
渲染失败自动回退纯文本；外部内容一律经转义 / MessageSegment.text 发送（防注入）。
"""
import asyncio
import base64
import hashlib
import html as html_mod
import io
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from nonebot import get_bot, get_driver, on_command
from nonebot.adapters.onebot.v11 import MessageEvent, MessageSegment
from nonebot_plugin_apscheduler import scheduler

from common import RENDER_SEM, get_http_client, is_owner, load_json_state, render_html_to_png, save_json_state

try:  # 插件以包形式加载时用相对导入；兜底按顶层模块名导入
    from . import rushgen
    from .rushgen import generate_boss_rush as _generate_boss_rush
except ImportError:  # pragma: no cover
    import rushgen
    from rushgen import generate_boss_rush as _generate_boss_rush

_logger = logging.getLogger(__name__)
_SH = ZoneInfo("Asia/Shanghai")

API_ROOT = "https://data.ninjakiwi.com"
URL_RACES = f"{API_ROOT}/btd6/races"
URL_BOSSES = f"{API_ROOT}/btd6/bosses"
URL_CT = f"{API_ROOT}/btd6/ct"
URL_MAP_FILTER = API_ROOT + "/btd6/maps/filter/{}"
URL_DAILY = f"{API_ROOT}/btd6/challenges/filter/daily"
URL_ODYSSEY = f"{API_ROOT}/btd6/odyssey"
URL_EVENTS = f"{API_ROOT}/btd6/events"
URL_RUSH = f"{API_ROOT}/btd6/bossRush"
URL_USERS = f"{API_ROOT}/btd6/users/"

DEFAULT_ROWS = 10  # 排行榜/地图列表默认条数
MAX_ROWS = 100     # 条数上限（竞赛每页50、Boss/CT每页25，自动分页拉取至请求数）
LB_DEFAULT_ROWS = 50  # 排行榜默认前50
LB_PAGE_SIZES = {"race": 50, "boss": 25, "ct": 25}  # 各类型每页人数（与NK API分页对齐）
LB_MAX_PAGE = 20      # 最大页码
LB_MAX_RANK = 1000    # 最大排名查询
BUCKET_PERIOD_MIN = 15        # 取整桶周期：倒计时文案按此粒度分窗 + API 数据 TTL；与预热频率解耦
BUCKET_MS = BUCKET_PERIOD_MIN * 60 * 1000     # bucket_now() 使用的毫秒桶宽
CACHE_TTL = BUCKET_PERIOD_MIN * 60     # 数据缓存与桶周期对齐，保证同窗内查询命中同一份内容哈希
PREWARM_LEADERBOARD_HOURS = 6   # 榜单卡预热周期（小时）：榜单分数变化快但查询可按需渲染兜底
CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
ASSET_DIR = os.path.join(CACHE_DIR, "assets")
CARD_MAX_AGE = 6 * 60 * 60   # 渲染缓存文件存活时长
ASSET_TTL = 7 * 24 * 60 * 60  # 素材落盘缓存时长（版本更新才会变）
CARD_DPI = 120                # 渲染分辨率：小机器上 144 → 120 明显提速，QQ 显示足够
# 规则与远征卡片的数据通常会持续数天甚至数周不变，单独放在持久缓存目录。
# 帮助菜单（btd6help）内容完全静态、不依赖任何 API/时间，必须持久化以保证
# 首屏秒回；其余实时内容（排行榜/每日/总览）仍使用普通缓存，避免旧数据占用空间。
PERSISTENT_CARD_PREFIXES = {"btd6rule", "btd6ody", "btd6help"}
PERSISTENT_CARD_FILES = 128
PERSISTENT_CARD_BYTES = 256_000_000
MAX_JSON_BYTES = 8_000_000
MAX_STALE_ITEMS = 256
MAX_ASSET_MEM_ITEMS = 256
MAX_GAME_MEM_ITEMS = 128
MAX_CARD_FILES = 128
MAX_CARD_BYTES = 64_000_000
MAX_ASSET_FILES = 256
MAX_ASSET_BYTES = 64_000_000
URL_HOSTS = {"data.ninjakiwi.com", "static-api.nkstatic.com"}
REQUEST_LIMIT = asyncio.Semaphore(8)

_COOLDOWN_SECONDS = {
    "default": 3.0,
    "heavy": 10.0,
}
_COOLDOWN_MAX_ITEMS = 4096
_cooldowns: OrderedDict[str, float] = OrderedDict()
_cooldown_lock = threading.Lock()


def _prune_cache_files(directory: str, suffix: str, max_files: int, max_bytes: int,
                       protected: set[str] | None = None) -> None:
    protected = protected or set()
    try:
        files = [p for p in Path(directory).glob(f"*{suffix}") if p.is_file()]
    except OSError:
        return
    entries = []
    for path in files:
        try:
            entries.append((path.stat().st_mtime, path.stat().st_size, path))
        except OSError:
            continue
    entries.sort(key=lambda item: item[0])
    total = sum(size for _, size, _ in entries)
    while len(entries) > max_files or total > max_bytes:
        index = next((i for i, (_, _, path) in enumerate(entries) if str(path) not in protected), None)
        if index is None:
            break
        _, size, candidate = entries.pop(index)
        try:
            candidate.unlink()
            total -= size
        except OSError:
            pass


def _cooldown_key(event, command: str) -> str:
    user_id = str(getattr(event, "user_id", "0") or "0")
    group_id = getattr(event, "group_id", None)
    scope = f"group:{group_id}" if group_id is not None else "private"
    return f"{scope}:{user_id}:{command}"


def _cooldown_remaining(event, command: str, weight: str) -> int:
    now = time.monotonic()
    key = _cooldown_key(event, command)
    seconds = _COOLDOWN_SECONDS[weight]
    with _cooldown_lock:
        for old_key, expiry in list(_cooldowns.items()):
            if expiry <= now:
                _cooldowns.pop(old_key, None)
        expiry = _cooldowns.get(key, 0.0)
        if expiry > now:
            _cooldowns.move_to_end(key)
            return max(1, int(expiry - now + 0.999))
        _cooldowns[key] = now + seconds
        _cooldowns.move_to_end(key)
        _prune_ordered(_cooldowns, _COOLDOWN_MAX_ITEMS)
    return 0


async def _enforce_cooldown(matcher, event, command: str, weight: str = "default") -> None:
    remaining = _cooldown_remaining(event, command, weight)
    if remaining:
        await matcher.finish(f"⏳ 请求太频繁，请 {remaining} 秒后再试")

HELP_GROUPS = [
    ("活动", [
        (".btd6活动", "当前竞赛/Boss/争夺领土/远征总览（三段式）"),
        (".btd6每日", "今日每日挑战（标准+高级一起返回）"),
        (".btd6竞速 [竞赛|boss]", "竞赛/Boss 活动规则详情（Boss 标准+精英一起返回）"),
        (".btd6远征", "当前远征 Odyssey"),
        (".btd6领土", "争夺领土详情"),
        (".btd6冲刺", "Boss Rush 冲刺"),
    ]),
    ("排行与档案", [
        (".btd6排行 竞赛|boss|领土 [P页码|排名]", "排行榜：默认前50；P2=第2页；数字=该名次玩家档案（Boss双榜/领土双榜自动返回）"),
        (".btd6玩家 <ID>", "玩家档案"),
        (".btd6地图 最新|热门|点赞 [数量]", "自制地图榜单"),
        (".btd6历史 [竞速|boss|领土|远征|每日] [数量]", "本地归档的历史活动（API 只保留近几期）"),
        (".btd6预热", "手动预热全部活动（仅主人）"),
    ]),
]

HELP_TEXT = """🐒 BTD6 情报站（气球塔防6）
.btd6活动 — 当前竞赛/Boss/争夺领土/远征总览（三段式：进行中/即将开始/已结束）
.btd6每日 — 今日每日挑战（标准+高级一起返回）
.btd6竞速 [竞赛|boss] — 竞赛/Boss 活动规则详情（Boss 标准+精英一起返回；领土暂无通用规则）
.btd6远征 — 当前远征活动
.btd6领土 — 争夺领土详情
.btd6冲刺 — Boss Rush 冲刺
.btd6排行 竞赛|boss|领土 [P页码|排名] — 排行榜：默认前50；P2=第2页；数字=该名次玩家档案（Boss标准+精英、领土个人+战队一起返回）
.btd6玩家 <ID> — 玩家档案（排行榜链接末尾的长串十六进制）
.btd6地图 最新|热门|点赞 [数量] — 自制地图榜单
.btd6历史 [竞速|boss|领土|远征|每日] [数量] — 本地归档的历史活动（API 只保留近几期）
.btd6预热 — 手动预热全部活动（仅主人）
数据源：Ninja Kiwi 官方开放数据接口"""
LB_USAGE = "用法：.btd6排行 竞赛|boss|领土|冲刺 [P页码|排名]\n例：.btd6排行 竞赛 — 前50\n.btd6排行 竞赛 P2 — 第2页\n.btd6排行 竞赛 7 — 第7名玩家档案\nboss 自动返回标准+精英双榜，领土 自动返回个人+战队双榜，冲刺暂无榜单"

# ---------------- 请求边界与内存 TTL 缓存 ----------------


def _validate_url(url: str) -> str:
    """只允许 HTTPS Ninja Kiwi 地址，阻止上游字段变成任意出站请求。"""
    if not isinstance(url, str) or not url:
        raise ValueError("BTD6 URL 为空")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in URL_HOSTS:
        raise ValueError("BTD6 URL 主机不在允许列表")
    if parsed.username or parsed.password or parsed.port:
        raise ValueError("BTD6 URL 包含不允许的认证或端口")
    return url


def _prune_ordered(mapping: OrderedDict, limit: int) -> None:
    while len(mapping) > limit:
        mapping.popitem(last=False)


_cache: OrderedDict[str, tuple[float, object]] = OrderedDict()
_stale: OrderedDict[str, object] = OrderedDict()  # 过期旧数据：网络抖动时先返回旧值再后台刷新
_refreshing: set[str] = set()
_refresh_tasks: set[asyncio.Task] = set()  # 持有后台刷新任务引用，防止任务被 GC 中途回收
_cache_lock = threading.Lock()


def _cache_get(url: str):
    with _cache_lock:
        hit = _cache.get(url)
        if not hit:
            return None
        if hit[0] > time.monotonic():
            _cache.move_to_end(url)
            return hit[1]
        _cache.pop(url, None)
    return None


def _cache_put(url: str, body) -> None:
    with _cache_lock:
        _cache[url] = (time.monotonic() + CACHE_TTL, body)
        _cache.move_to_end(url)
        _stale[url] = body
        _stale.move_to_end(url)
        _prune_ordered(_cache, MAX_STALE_ITEMS)
        _prune_ordered(_stale, MAX_STALE_ITEMS)


async def _http_get(url: str, timeout: float):
    _validate_url(url)
    client = get_http_client(20)
    async with REQUEST_LIMIT:
        return await client.get(url, timeout=timeout)


async def _refresh_url(url: str) -> None:
    """后台刷新过期缓存；失败静默保留旧数据（跨境网络抖动时不拖慢回复）。"""
    if url in _refreshing:
        return
    _refreshing.add(url)
    try:
        r = await _http_get(url, 20)
        r.raise_for_status()
        if len(r.content or b"") > MAX_JSON_BYTES:
            raise ValueError("BTD6 API 响应过大")
        data = r.json()
        if isinstance(data, dict) and data.get("success") and data.get("body") is not None:
            _cache_put(url, data["body"])
    except Exception:
        _logger.debug("BTD6 后台刷新失败（保留旧缓存）: %s", url)
    finally:
        _refreshing.discard(url)


async def fetch_body(url: str):
    """GET 一个 NK 开放数据接口并返回 body；命中缓存直接返回；
    缓存过期时先返回旧数据并后台刷新（stale-while-revalidate），避免跨境延迟卡住回复。"""
    _validate_url(url)
    hit = _cache_get(url)
    if hit is not None:
        return hit
    with _cache_lock:
        stale = _stale.get(url)
        if stale is not None:
            _stale.move_to_end(url)
    if stale is not None:
        task = asyncio.create_task(_refresh_url(url))
        _refresh_tasks.add(task)
        task.add_done_callback(_refresh_tasks.discard)
        return stale
    r = await _http_get(url, 20)
    r.raise_for_status()
    if len(r.content or b"") > MAX_JSON_BYTES:
        raise ValueError("BTD6 API 响应过大")
    data = r.json()
    if not isinstance(data, dict) or not data.get("success") or data.get("body") is None:
        error = data.get("error") if isinstance(data, dict) else "响应格式异常"
        raise RuntimeError(f"NK API 返回异常: {error}")
    body = data["body"]
    _cache_put(url, body)
    return body


# ---------------- 游戏素材（官方静态 CDN → data: URL，落盘缓存） ----------------


def _sniff_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"GIF8"):
        return "image/gif"
    return ""


_asset_mem: OrderedDict[str, str] = OrderedDict()
_asset_mem_sizes: dict[str, int] = {}
_asset_mem_lock = threading.Lock()
# 素材 data URL 常驻内存的硬预算：只限条数时 256 条大图可吃掉数百 MB
MAX_ASSET_MEM_BYTES = 64_000_000


def _remember_asset(url: str, data_url: str) -> None:
    with _asset_mem_lock:
        _asset_mem[url] = data_url
        _asset_mem_sizes[url] = len(data_url)
        _asset_mem.move_to_end(url)
        for stale in [k for k in _asset_mem_sizes if k not in _asset_mem]:
            _asset_mem_sizes.pop(stale, None)
        total = sum(_asset_mem_sizes.values())
        while (total > MAX_ASSET_MEM_BYTES or len(_asset_mem) > MAX_ASSET_MEM_ITEMS) and _asset_mem:
            oldest, _ = _asset_mem.popitem(last=False)
            total -= _asset_mem_sizes.pop(oldest, 0)


async def _asset_data_url(url: str, max_bytes: int = 3_000_000) -> str:
    """下载图片素材并返回 data: URL；内存 + 落盘两级缓存（渲染管线只允许 data: URL）。"""
    if not url:
        return ""
    try:
        _validate_url(url)
    except ValueError:
        return ""
    with _asset_mem_lock:
        hit = _asset_mem.get(url)
        if hit:
            _asset_mem.move_to_end(url)
            return hit
    key = hashlib.md5(url.encode()).hexdigest()
    path = os.path.join(ASSET_DIR, key + ".txt")
    try:
        if time.time() - os.path.getmtime(path) < ASSET_TTL:
            with open(path, encoding="ascii") as f:
                data_url = f.read()
            if data_url.startswith("data:image/"):
                _remember_asset(url, data_url)
                return data_url
    except (OSError, UnicodeError):
        pass
    r = await _http_get(url, 15)
    r.raise_for_status()
    data = getattr(r, "content", b"") or b""
    if not data or len(data) > max_bytes:
        return ""
    mime = _sniff_mime(data)
    if mime not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
        return ""
    data_url = f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
    try:
        os.makedirs(ASSET_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="ascii") as f:
            f.write(data_url)
        os.replace(tmp, path)
        _prune_cache_files(ASSET_DIR, ".txt", MAX_ASSET_FILES, MAX_ASSET_BYTES, {path})
    except OSError:
        _logger.warning("BTD6 素材缓存写入失败", exc_info=True)
    _remember_asset(url, data_url)
    return data_url


# ---------------- 本地游戏素材（塔/英雄立绘，来自 BTD6 API Explorer 资源库） ----------------

GAME_ASSET_DIR = os.path.join(os.path.dirname(__file__), "assets", "game")
_game_mem: OrderedDict[str, str] = OrderedDict()


def _remember_game_asset(fname: str, data_url: str) -> None:
    _game_mem[fname] = data_url
    _game_mem.move_to_end(fname)
    _prune_ordered(_game_mem, MAX_GAME_MEM_ITEMS)


def _game_asset_data_url(fname: str) -> str:
    """读取本地游戏素材并转 data: URL；缺失返回空串（卡片自动降级为文字）。"""
    if not fname or not re.fullmatch(r"[A-Za-z0-9_-]+\.webp", fname):
        return ""
    hit = _game_mem.get(fname)
    if hit:
        _game_mem.move_to_end(fname)
        return hit
    path = os.path.join(GAME_ASSET_DIR, fname)
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return ""
    url = "data:image/webp;base64," + base64.b64encode(data).decode("ascii")
    _remember_game_asset(fname, url)
    return url


# 截图风格的界面图标：由参考卡片裁切并随插件本地部署，避免渲染时依赖外链。
UI_ASSET_DIR = os.path.join(os.path.dirname(__file__), "assets", "ui")
_ui_mem: OrderedDict[str, str] = OrderedDict()


def _ui_asset_data_url(fname: str) -> str:
    """读取本地卡片 UI 图标；资源缺失时由调用方回退到 CSS/文字。"""
    if not fname or not re.fullmatch(r"[A-Za-z0-9_-]+\.png", fname):
        return ""
    hit = _ui_mem.get(fname)
    if hit:
        _ui_mem.move_to_end(fname)
        return hit
    path = os.path.join(UI_ASSET_DIR, fname)
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return ""
    url = "data:image/png;base64," + base64.b64encode(data).decode("ascii")
    _ui_mem[fname] = url
    _ui_mem.move_to_end(fname)
    _prune_ordered(_ui_mem, 32)
    return url


# 与游戏内猴子选择器一致的分类色；新塔缺少分类时仍可安全回退为普通蓝色。
_TOWER_CATEGORY = {
    "DartMonkey": "primary", "BoomerangMonkey": "primary", "BombShooter": "primary",
    "TackShooter": "primary", "IceMonkey": "primary", "GlueGunner": "primary",
    "Desperado": "primary",
    "SniperMonkey": "military", "MonkeySub": "military", "MonkeyBuccaneer": "military",
    "HeliPilot": "military", "MonkeyAce": "military", "MortarMonkey": "military",
    "DartlingGunner": "military", "Skywarden": "military",
    "WizardMonkey": "magic", "SuperMonkey": "magic", "NinjaMonkey": "magic",
    "Druid": "magic", "Alchemist": "magic", "Mermonkey": "magic",
    "MonkeyVillage": "support", "BananaFarm": "support", "SpikeFactory": "support",
    "EngineerMonkey": "support", "BeastHandler": "support",
}

_RACE_TOWER_ORDER = [
    # 英雄先行，其后按游戏选择器的主力 / 军事 / 魔法 / 支援分组。
    "Etienne", "Quincy", "Gwendolin", "StrikerJones", "ObynGreenfoot",
    "CaptainChurchill", "Benjamin", "Ezili", "PatFusty", "Adora", "AdmiralBrickell",
    "Sauda", "Psi", "Geraldo", "Corvus", "Rosalia", "Silas", "DanDMonke",
    "DartMonkey", "BoomerangMonkey", "BombShooter", "TackShooter", "IceMonkey",
    "GlueGunner", "Desperado",
    "SniperMonkey", "MonkeySub", "MonkeyBuccaneer", "MonkeyAce", "HeliPilot", "MortarMonkey",
    "DartlingGunner", "Skywarden",
    "WizardMonkey", "SuperMonkey", "NinjaMonkey", "Druid", "Alchemist", "Mermonkey",
    "BananaFarm", "SpikeFactory", "MonkeyVillage", "EngineerMonkey", "BeastHandler",
]
_RACE_TOWER_ORDER_INDEX = {name: index for index, name in enumerate(_RACE_TOWER_ORDER)}


def _tower_category(raw: str, is_hero: bool) -> str:
    if is_hero:
        return "hero"
    return _TOWER_CATEGORY.get(raw, "primary")


def _tower_icon(raw: str, is_hero: bool) -> str:
    """API 塔名 → 本地立绘文件名；向导猴的素材文件名是 Wizard 而非 WizardMonkey。"""
    if is_hero:
        fname = _SKIN_PORTRAIT.get(raw)
        if not fname and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", raw or ""):
            fname = f"{raw}Portrait.webp"
        return _game_asset_data_url(fname or "")
    if raw == "WizardMonkey":
        fname = "000-Wizard.webp"
    elif re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", raw or ""):
        fname = f"000-{raw}.webp"
    else:
        fname = ""
    return _game_asset_data_url(fname)


def _tower_display_name(raw: str, is_hero: bool) -> str:
    """皮肤英雄显示为「基础英雄·皮肤」，其余按翻译表。"""
    if is_hero and raw in _SKIN_PORTRAIT:
        base = _SKIN_PORTRAIT[raw].replace("Portrait.webp", "")
        return f"{tower_cn(base)}·皮肤"
    return tower_cn(raw)


# 英雄皮肤 → 基础英雄立绘（API 会把可用皮肤单列，本地只存基础立绘）
_SKIN_PORTRAIT = {
    "Silas": "CorvusPortrait.webp", "CorvusDecryptor": "CorvusPortrait.webp",
    "DanDMonke": "PatFustyPortrait.webp", "FustyTheSnowman": "PatFustyPortrait.webp",
    "KaijuPat": "PatFustyPortrait.webp",
    "ETn": "EtiennePortrait.webp", "BookWyrmEtienne": "EtiennePortrait.webp",
    "BikerBones": "BenjaminPortrait.webp", "BenJammin": "BenjaminPortrait.webp",
    "JiangshiSauda": "SaudaPortrait.webp", "RedSauda": "SaudaPortrait.webp",
    "VikingSauda": "SaudaPortrait.webp", "JoanOfArc": "SaudaPortrait.webp",
    "DreamstatePsi": "PsiPortrait.webp", "QuincyCyber": "QuincyPortrait.webp",
    "AdoraSheRa": "AdoraPortrait.webp", "MountainObyn": "ObynGreenfootPortrait.webp",
    "OceanObyn": "ObynGreenfootPortrait.webp", "Galaxili": "EziliPortrait.webp",
}


# ---------------- 时间与活动状态 ----------------


def fmt_time(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=_SH)
    return f"{dt.month}月{dt.day}日 {dt:%H:%M}"


def fmt_date(ms: float | None) -> str:
    if not ms:
        return "未知日期"
    dt = datetime.fromtimestamp(ms / 1000, tz=_SH)
    return f"{dt.year}-{dt.month:02d}-{dt.day:02d}"


def fmt_remaining(delta_ms: int) -> str:
    total_min = int(max(0, delta_ms) // 60000)
    days, rest = divmod(total_min, 1440)
    hours, minutes = divmod(rest, 60)
    if days >= 1:
        return f"{days}天{hours}小时"
    if hours >= 1:
        return f"{hours}小时{minutes}分"
    return f"{minutes}分钟"


def event_status_line(ev: dict, now_ms: int) -> str:
    start, end = int(ev.get("start") or 0), int(ev.get("end") or 0)
    if now_ms < start:
        return f"{fmt_remaining(start - now_ms)}后开始（{fmt_time(start)} 开启）"
    if now_ms < end:
        return f"剩余 {fmt_remaining(end - now_ms)}（{fmt_time(end)} 结束）"
    return f"已于 {fmt_time(end)} 结束"


def pick_active(items: list, now_ms: int):
    for it in items:
        if int(it.get("start") or 0) <= now_ms < int(it.get("end") or 0):
            return it
    return None


def pick_next(items: list, now_ms: int):
    upcoming = sorted((i for i in items if int(i.get("start") or 0) > now_ms),
                      key=lambda x: x.get("start") or 0)
    return upcoming[0] if upcoming else None


def fallback_latest(items: list):
    """列表按新→旧排列时取第一场，作为无进行中活动时的兜底展示。"""
    return items[0] if items else None


def bucket_now() -> int:
    """按预热周期取整的"当前时间"：同窗口内倒计时文案不变，渲染缓存才能命中预热卡。"""
    return int(time.time() * 1000) // BUCKET_MS * BUCKET_MS


def _classify_overview_events(
    races: list, bosses: list, cts: list, now_ms: int, odysseys: list | None = None, rush: list | None = None,
) -> tuple[list[tuple[dict, str]], list[tuple[dict, str]], list[tuple[dict, str]]]:
    """将全部活动按时间状态分为三类：进行中 / 即将开始 / 已结束(近5).

    返回 (ongoing, upcoming, ended)，每项为 (ev, kind) 其中 kind ∈ {race,boss,ct,odyssey}.
    - ongoing:  start <= now < end，按结束时间升序（先结束的在前）
    - upcoming: start > now，按开始时间升序（最近开始的在前）
    - ended:    end <= now，按结束时间降序取前 5（最近结束的在前）
    兼容旧调用：odysseys 为空时与旧版一致。
    """
    all_events: list[tuple[dict, str]] = []
    for ev in races or []:
        if isinstance(ev, dict):
            all_events.append((ev, "race"))
    for ev in bosses or []:
        if isinstance(ev, dict):
            all_events.append((ev, "boss"))
    for ev in cts or []:
        if isinstance(ev, dict):
            all_events.append((ev, "ct"))
    for ev in odysseys or []:
        if isinstance(ev, dict):
            all_events.append((ev, "odyssey"))
    for ev in rush or []:
        if isinstance(ev, dict):
            all_events.append((ev, "rush"))
    ongoing: list[tuple[dict, str]] = []
    upcoming: list[tuple[dict, str]] = []
    ended: list[tuple[dict, str]] = []
    for ev, kind in all_events:
        try:
            s = int(ev.get("start") or 0)
            e = int(ev.get("end") or 0)
        except (TypeError, ValueError):
            continue
        if e <= 0 or s <= 0:
            continue
        if s <= now_ms < e:
            ongoing.append((ev, kind))
        elif now_ms < s:
            upcoming.append((ev, kind))
        elif now_ms >= e:
            ended.append((ev, kind))
    ongoing.sort(key=lambda x: int(x[0].get("end") or 0))
    upcoming.sort(key=lambda x: int(x[0].get("start") or 0))
    ended.sort(key=lambda x: int(x[0].get("end") or 0), reverse=True)
    ended = ended[:5]
    return ongoing, upcoming, ended


# ---------------- 常用名词翻译 ----------------

BOSS_CN = {
    "bloonarius": "膨胀气球神", "lych": "巫妖", "vortex": "漩涡",
    "dreadbloon": "恐惧气球岩", "phayze": "幻影", "blastapopoulos": "爆裂魔炎",
    "diamondback": "菱背龙",
    "blastapopolous": "爆裂魔炎",
}
DIFFICULTY_CN = {
    # 地图分级
    "Beginner": "初级", "Intermediate": "中级", "Advanced": "高级", "Expert": "专家",
    # 游戏内难度（活动元数据用）
    "Easy": "简单", "Medium": "中等", "Hard": "困难",
}
MODE_CN = {
    "Standard": "标准", "Reverse": "反向", "Apopalypse": "天启",
    "Half Cash": "半价", "Double HP": "双倍血量", "CHIMPS": "CHIMPS",
}
MAP_CN = {
    "TownCentre": "城镇中心", "Scrapyard": "废品场", "TreeStump": "树桩",
    "Logs": "原木", "InTheLoop": "循环圈", "Cubism": "立体主义",
    "Resort": "度假胜地", "FourCircles": "四圆环", "ParkPath": "公园小径",
    "AdorasTemple": "阿朵拉神殿", "Ravine": "峡谷", "DarkCastle": "黑暗城堡",
}
SCORING_CN = {"GameTime": "最快用时", "LeastCash": "最少现金", "LeastTiers": "最少升级"}

TOWER_CN = {
    "Dart Monkey": "飞镖猴", "Tack Shooter": "钉子射手", "Bomb Shooter": "炸弹射手",
    "Ice Monkey": "冰猴", "Glue Gunner": "胶水枪手", "Sniper Monkey": "狙击猴",
    "Monkey Sub": "潜艇猴", "Monkey Buccaneer": "海盗猴", "Heli Pilot": "直升机猴",
    "Mortar Monkey": "迫击炮猴", "Dartling Gunner": "连发枪手", "Wizard Monkey": "巫师猴",
    "Super Monkey": "超级猴", "Ninja Monkey": "忍者猴", "Alchemist": "炼金术士",
    "Druid": "德鲁伊", "Banana Farm": "香蕉农场", "Engineer Monkey": "工程师猴",
    "Spike Factory": "尖刺工厂", "Monkey Village": "猴村", "Beast Handler": "驯兽师",
    "Boomerang Monkey": "回旋镖猴", "Monkey Ace": "飞机猴",
    # 新塔（API 驼峰名直接命中去空格查找表）
    "Mermonkey": "人鱼猴", "Desperado": "亡命徒猴", "Skywarden": "天空守卫",
}
HERO_CN = {
    "Quincy": "昆西", "Gwendolin": "格温多林", "Striker Jones": "琼斯",
    "Obyn Greenfoot": "奥宾", "Captain Churchill": "丘吉尔", "Benjamin": "本杰明",
    "Ezili": "伊兹莉", "Pat Fusty": "帕特", "Adora": "阿朵拉", "Admiral Brickell": "布里克",
    "Etienne": "艾蒂安", "Sauda": "绍达", "Psi": "赛", "Geraldo": "杰拉尔多",
    "Corvus": "科沃斯", "Rosalia": "罗莎莉娅",
}
# NK API 的塔名为无空格驼峰（如 BananaFarm），按去空格键补一份查找表
_TOWER_CN_FLAT = {k.replace(" ", "").lower(): v for k, v in TOWER_CN.items()}
_HERO_CN_FLAT = {k.replace(" ", "").lower(): v for k, v in HERO_CN.items()}

FLAG_LABELS = [
    ("disableMK", "猴子知识"), ("disablePowers", "力量道具"), ("disableInstas", "即时塔"),
    ("disableSelling", "卖塔"), ("noContinues", "重开续命"), ("disableDoubleCash", "双倍启动现金"),
]


def cn(value, mapping: dict) -> str:
    raw = str(value or "").strip()
    return mapping.get(raw, raw)


def boss_cn(boss_type: str) -> str:
    raw = str(boss_type or "").strip()
    return BOSS_CN.get(raw.lower(), raw)


def tower_cn(name: str) -> str:
    flat = name.replace(" ", "").lower()
    return TOWER_CN.get(name) or _TOWER_CN_FLAT.get(flat) \
        or HERO_CN.get(name) or _HERO_CN_FLAT.get(flat) or name


def fmt_score(scoring: str | None, score) -> str:
    """按计分类型格式化分数：竞赛毫秒用时→分:秒.毫秒，最少现金→$，其余千分位。"""
    st = str(scoring or "")
    try:
        n = float(score)
    except (TypeError, ValueError):
        return str(score)
    if st == "GameTime":
        sec = n / 1000.0
        m = int(sec // 60)
        return f"{m}:{sec - m * 60:06.3f}"
    if st == "LeastCash":
        return f"${int(n):,}"
    if st == "LeastTiers":
        return str(int(n))
    return f"{int(n):,}"


def fmt_cn_num(n) -> str:
    """大数中文单位：1,884,842,684 → 18.8亿；94,098,63 → 941万。"""
    try:
        v = float(n or 0)
    except (TypeError, ValueError):
        return str(n)
    if abs(v) >= 1e8:
        return f"{v / 1e8:.1f}亿"
    if abs(v) >= 1e4:
        return f"{v / 1e4:.0f}万"
    return f"{int(v):,}"


# ---------------- 活动总览（文本） ----------------


def _race_overview(races: list, now_ms: int) -> list[str]:
    ev = pick_active(races, now_ms) or pick_next(races, now_ms) or fallback_latest(races)
    if not ev:
        return ["🏁 每周竞赛：暂无数据"]
    total = int(ev.get("totalScores") or 0)
    return [
        f"🏁 每周竞赛「{(ev.get('name') or '').strip()}」",
        f"   {event_status_line(ev, now_ms)}",
        f"   👥 参与人数 {total:,}",
    ]


def _boss_overview(bosses: list, now_ms: int) -> list[str]:
    ev = pick_active(bosses, now_ms) or pick_next(bosses, now_ms) or fallback_latest(bosses)
    if not ev:
        return ["👹 Boss 事件：暂无数据"]
    name = (ev.get("name") or "").strip()
    title = f"👹 Boss 事件「{name}」"
    bt = boss_cn(ev.get("bossType"))
    if bt:
        title += f"（{bt}）"
    std = SCORING_CN.get(str(ev.get("normalScoringType") or ""), str(ev.get("normalScoringType") or ""))
    elite = SCORING_CN.get(str(ev.get("eliteScoringType") or ""), str(ev.get("eliteScoringType") or ""))
    n_std = int(ev.get("totalScores_standard") or 0)
    n_elite = int(ev.get("totalScores_elite") or 0)
    return [
        title,
        f"   {event_status_line(ev, now_ms)}",
        f"   📊 标准模式 {std} · 精英模式 {elite}",
        f"   👥 参与：标准 {n_std:,} · 精英 {n_elite:,}",
    ]


def _ct_overview(cts: list, now_ms: int) -> list[str]:
    ev = pick_active(cts, now_ms) or pick_next(cts, now_ms) or fallback_latest(cts)
    if not ev:
        return ["🏰 争夺领土：暂无数据"]
    n_player = int(ev.get("totalScores_player") or 0)
    n_team = int(ev.get("totalScores_team") or 0)
    return [
        "争夺领土（CT）",
        f"   {event_status_line(ev, now_ms)}",
        f"   👥 参与：个人 {n_player:,} · 战队 {n_team:,}",
    ]


def _single_event_text(ev: dict, kind: str, now_ms: int) -> list[str]:
    """单场活动的文本行（与 _race/_boss/_ct_overview 保持同格式，确保可通过现有单测）。"""
    if kind == "race":
        total = int(ev.get("totalScores") or 0)
        return [
            f"🏁 每周竞赛「{(ev.get('name') or '').strip()}」",
            f"   {event_status_line(ev, now_ms)}",
            f"   👥 参与人数 {total:,}",
        ]
    if kind == "boss":
        name = (ev.get("name") or "").strip()
        title = f"👹 Boss 事件「{name}」"
        bt = boss_cn(ev.get("bossType"))
        if bt:
            title += f"（{bt}）"
        std = SCORING_CN.get(str(ev.get("normalScoringType") or ""), str(ev.get("normalScoringType") or ""))
        elite = SCORING_CN.get(str(ev.get("eliteScoringType") or ""), str(ev.get("eliteScoringType") or ""))
        n_std = int(ev.get("totalScores_standard") or 0)
        n_elite = int(ev.get("totalScores_elite") or 0)
        return [
            title,
            f"   {event_status_line(ev, now_ms)}",
            f"   📊 标准模式 {std} · 精英模式 {elite}",
            f"   👥 参与：标准 {n_std:,} · 精英 {n_elite:,}",
        ]
    if kind == "odyssey":
        name = (ev.get("name") or "").strip() or "远征"
        desc = (ev.get("description") or "").strip()
        lines = [
            f"🏰 远征「{name}」",
            f"   {event_status_line(ev, now_ms)}",
        ]
        if desc:
            # 远征描述可能较长，截断到 60 字避免刷屏
            short = desc[:60] + ("…" if len(desc) > 60 else "")
            lines.append(f"   📜 {short}")
        return lines
    # ct
    n_player = int(ev.get("totalScores_player") or 0)
    n_team = int(ev.get("totalScores_team") or 0)
    return [
        "🏰 争夺领土（CT）",
        f"   {event_status_line(ev, now_ms)}",
        f"   👥 参与：个人 {n_player:,} · 战队 {n_team:,}",
    ]


def build_overview(races: list, bosses: list, cts: list, now_ms: int, odysseys: list | None = None, rush: list | None = None) -> str:
    """三段式总览：进行中 / 即将开始 / 已结束(近5)，从上到下排列。

    odysseys 为可选的远征列表，未传时与旧版一致（仅 race/boss/ct）。
    """
    odysseys = odysseys or []
    rush = rush or []
    ongoing, upcoming, ended = _classify_overview_events(races, bosses, cts, now_ms, odysseys, rush)
    parts = ["🎮 BTD6 当前活动", ""]
    # 进行中
    parts.append(f"🟢 正在进行 ({len(ongoing)})")
    if not ongoing:
        parts.append("  暂无")
    else:
        for ev, kind in ongoing:
            parts.extend(_single_event_text(ev, kind, now_ms))
            parts.append("")
        # 去掉最后多余的空行并补一个分隔
        if parts and parts[-1] == "":
            parts.pop()
    parts.append("")
    # 即将开始
    parts.append(f"🟡 即将开始 ({len(upcoming)})")
    if not upcoming:
        parts.append("  暂无")
    else:
        for ev, kind in upcoming:
            parts.extend(_single_event_text(ev, kind, now_ms))
            parts.append("")
        if parts and parts[-1] == "":
            parts.pop()
    parts.append("")
    # 已结束（近5）
    parts.append(f"⚪ 已结束 · 最近 {len(ended)} 场" if ended else "⚪ 已结束 · 最近 0 场")
    if not ended:
        parts.append("  暂无")
    else:
        for ev, kind in ended:
            parts.extend(_single_event_text(ev, kind, now_ms))
            parts.append("")
        if parts and parts[-1] == "":
            parts.pop()
    return "\n".join(parts)


# ---------------- 取数层（供文本/卡片两路共用） ----------------


async def _safe(coro):
    """执行协程，任何异常返回 None（预热/可选素材失败不拖垮主流程）。"""
    try:
        return await coro
    except Exception:
        return None


async def _race_event_map_url(ev: dict) -> str:
    """单场竞赛的地图 data URL（经 metadata → mapURL → data URL），失败返回空串。"""
    url = ev.get("metadata") if isinstance(ev, dict) else None
    if not url:
        return ""
    try:
        _validate_url(url)
    except ValueError:
        return ""
    meta = await _safe(fetch_body(url))
    if not isinstance(meta, dict) or not meta.get("mapURL"):
        return ""
    return await _safe(_asset_data_url(meta["mapURL"])) or ""


async def _boss_event_image_url(ev: dict) -> str:
    """单场 Boss 的头像 data URL，优先使用本地方形图标（296×295，适配 160×100 contain），失败回退远端宽图。"""
    if isinstance(ev, dict):
        # 本地方形图标在 160×100 contain 下显示完整且居中，比 1440×332 宽图更适合缩略
        try:
            local_asset = _boss_event_asset(ev)  # type: ignore[name-defined]  # 定义在后，运行时已就绪
            if local_asset:
                local_url = _ui_asset_data_url(local_asset)
                if local_url:
                    return local_url
        except Exception:
            pass
    url = ev.get("bossTypeURL") if isinstance(ev, dict) else None
    if not url:
        return ""
    try:
        _validate_url(url)
    except ValueError:
        return ""
    return await _safe(_asset_data_url(url)) or ""


async def collect_overview(now_ms: int | None = None) -> dict:
    now = now_ms if now_ms is not None else bucket_now()
    races, bosses, cts, odysseys, rush_events = await asyncio.gather(
        fetch_body(URL_RACES), fetch_body(URL_BOSSES), fetch_body(URL_CT),
        _safe(fetch_body(URL_ODYSSEY)),
        _safe(fetch_body(URL_EVENTS)),
    )
    # odysseys 可能为 None（网络失败）或非列表
    if not isinstance(odysseys, list):
        odysseys = []
    # rush via events type bossRush
    rush = []
    if isinstance(rush_events, list):
        rush = [e for e in rush_events if isinstance(e, dict) and e.get("type") == "bossRush"]
    elif isinstance(rush_events, dict) and isinstance(rush_events.get("body"), list):
        rush = [e for e in rush_events["body"] if isinstance(e, dict) and e.get("type") == "bossRush"]
    race = _pick_section(races, now)
    boss = _pick_section(bosses, now)
    meta = await _safe(fetch_body(race["metadata"])) if race and race.get("metadata") else None
    race_map = await _safe(_asset_data_url(meta.get("mapURL"))) if meta and meta.get("mapURL") else None
    # Boss 首图优先本地方形图标，避免 1440×332 宽图在 160×100 下被 contain 压成 37px 高
    if boss:
        try:
            boss_img = await _boss_event_image_url(boss)
        except Exception:
            boss_img = await _safe(_asset_data_url(boss.get("bossTypeURL"))) or "" if boss.get("bossTypeURL") else ""
    else:
        boss_img = ""

    # 为三段式总览预取每场活动的独立配图，避免“全部复用当前场配图”的错图问题。
    # 仅对将要展示的场次取图：进行中 + 即将开始 + 已结束(近5)，并发拉取并走两级缓存。
    ongoing, upcoming, ended = _classify_overview_events(races, bosses, cts, now, odysseys, rush)
    display = ongoing + upcoming + ended
    # 构建 id -> 图 映射
    race_map_by_id: dict[str, str] = {}
    boss_img_by_id: dict[str, str] = {}
    # 保留旧的单图作为兜底（_overview_panel_html 会优先查按 id 的表，缺失再回退到旧字段）
    if race and race.get("id"):
        race_map_by_id[str(race.get("id"))] = race_map or ""
    if boss and boss.get("id"):
        boss_img_by_id[str(boss.get("id"))] = boss_img or ""

    async def _fetch_race(ev_kind: tuple[dict, str]):
        ev, kind = ev_kind
        if kind != "race":
            return
        eid = str(ev.get("id") or "")
        if not eid or eid in race_map_by_id:
            return
        url = await _race_event_map_url(ev)
        if url:
            race_map_by_id[eid] = url

    async def _fetch_boss(ev_kind: tuple[dict, str]):
        ev, kind = ev_kind
        if kind != "boss":
            return
        eid = str(ev.get("id") or "")
        if not eid or eid in boss_img_by_id:
            return
        url = await _boss_event_image_url(ev)
        if url:
            boss_img_by_id[eid] = url

    # 并发预取（带 _safe，已在内部处理异常）
    if display:
        await asyncio.gather(*(_fetch_race(x) for x in display), *(_fetch_boss(x) for x in display))

    return {
        "races": races, "bosses": bosses, "cts": cts, "odysseys": odysseys, "rush": rush, "now": now,
        "race_map": race_map or "", "boss_img": boss_img or "",
        "race_meta": meta or {},
        "race_map_by_id": race_map_by_id,
        "boss_img_by_id": boss_img_by_id,
    }


def overview_text(data: dict) -> str:
    return build_overview(
        data["races"], data["bosses"], data["cts"], data["now"],
        data.get("odysseys") or [],
        data.get("rush") or [],
    )


MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


async def fetch_leaderboard_paginated(start_url: str, rows: int) -> list:
    """分页拉取排行榜至多 rows 条，自动跟随 next 链接（竞赛 50/页，Boss/CT 25/页）。"""
    entries: list = []
    url: str | None = start_url
    seen: set[str] = set()
    while url and len(entries) < rows and url not in seen:
        seen.add(url)
        # 优先走 fetch_body（测试桩会 mock 它，且走缓存）；失败再降级为 _http_get 以获取 next
        body = None
        next_url = None
        try:
            # 尝试通过 fetch_body 获取 body（测试环境友好）
            body = await fetch_body(url)
            # 若需要分页，再用 _http_get 取 next（可能因 URL 白名单校验失败，测试环境会走到 except）
            if len(entries) + len(body or []) < rows:
                try:
                    r = await _http_get(url, 20)
                    r.raise_for_status()
                    data = r.json()
                    next_url = data.get("next")
                except Exception:
                    next_url = None
        except Exception:
            # fetch_body 失败（如真实环境 next 分页），改走 _http_get 批量解析
            try:
                r = await _http_get(url, 20)
                r.raise_for_status()
                data = r.json()
                body = data.get("body") or []
                next_url = data.get("next")
            except Exception:
                _logger.warning("排行榜分页拉取失败 url=%s", url, exc_info=True)
                break
        if isinstance(body, list):
            entries.extend(body)
        else:
            break
        if len(entries) >= rows:
            break
        url = next_url
    return entries[:rows]


async def collect_leaderboard(kind: str, variant: str, rows: int) -> dict:
    """拉取并整理一个排行榜；无可展示活动时返回 {"empty": 文案}。"""
    now = bucket_now()
    scoring = ""
    img = ""
    if kind == "race":
        races = await fetch_body(URL_RACES)
        ev = pick_active(races, now) or fallback_latest(races)
        if not ev:
            return {"empty": "当前没有竞赛活动"}
        lb_url = ev.get("leaderboard")
        if not lb_url:
            return {"empty": "该活动暂无排行榜"}
        entries = await fetch_leaderboard_paginated(lb_url, rows)
        head = f"竞赛「{(ev.get('name') or '').strip()}」排行榜（最快用时，越短越好）"
        scoring = "GameTime"
        meta_url = ev.get("metadata")
        meta = await _safe(fetch_body(meta_url)) if meta_url else None
        if meta and meta.get("mapURL"):
            img = await _safe(_asset_data_url(meta["mapURL"])) or ""
    elif kind == "boss":
        bosses = await fetch_body(URL_BOSSES)
        ev = pick_active(bosses, now) or fallback_latest(bosses)
        if not ev:
            return {"empty": "当前没有 Boss 活动"}
        elite = variant == "elite"
        url_key = "leaderboard_elite_players_1" if elite else "leaderboard_standard_players_1"
        url = ev.get(url_key)
        if not url:
            return {"empty": "该活动暂无此模式的排行榜"}
        entries = await fetch_leaderboard_paginated(url, rows)
        label = "精英" if elite else "标准"
        scoring = str(ev.get("eliteScoringType" if elite else "normalScoringType") or "")
        mode_cn = SCORING_CN.get(scoring, scoring or "?")
        head = f"Boss「{(ev.get('name') or '').strip()}」{label}排行榜（{mode_cn}）"
        if ev.get("bossTypeURL"):
            img = await _safe(_asset_data_url(ev["bossTypeURL"])) or ""
    elif kind == "rush":
        # Boss Rush 暂无公开排行榜接口（events 仅提供摘要），返回提示
        try:
            events = await fetch_body(URL_EVENTS)
            rush_list = [e for e in events if isinstance(e, dict) and e.get("type") == "bossRush"]
        except Exception:
            rush_list = []
        ev = pick_active(rush_list, now) or fallback_latest(rush_list)
        if not ev:
            return {"empty": "当前没有 Boss Rush 活动"}
        return {"empty": "Boss Rush 排行榜暂未开放（NK 仅在 /btd6/events 提供摘要，详细榜单待开放）"}
    else:  # ct
        cts = await fetch_body(URL_CT)
        ev = pick_active(cts, now) or fallback_latest(cts)
        if not ev:
            return {"empty": "当前没有争夺领土活动"}
        team = variant == "team"
        url_key = "leaderboard_team" if team else "leaderboard_player"
        url = ev.get(url_key)
        if not url:
            return {"empty": "该活动暂无此榜的排行榜"}
        entries = await fetch_leaderboard_paginated(url, rows)
        label = "战队" if team else "个人"
        head = f"争夺领土 {label}排行榜（领土积分，越高越好）"

    top = list(entries or [])[:rows]
    rows_out = [
        (i, str(e.get("displayName") or "?").strip(), fmt_score(scoring, e.get("score")))
        for i, e in enumerate(top, 1)
    ]
    return {
        "head": head, "status": event_status_line(ev, now), "entries": rows_out,
        "img": img,
    }


def leaderboard_text(col: dict) -> str:
    if col.get("empty"):
        return col["empty"]
    lines = [col["head"], f"   {col['status']}", ""]
    if not col["entries"]:
        lines.append("（暂无上榜数据）")
    for i, name, score_txt in col["entries"]:
        prefix = MEDALS.get(i, f"{i}.")
        lines.append(f"{prefix} {name} — {score_txt}")
    return "\n".join(lines)


# ---------------- 活动规则 ----------------


def _mult_txt(key: str, value) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return ""
    if abs(v - 1.0) < 1e-9:
        return ""
    return f"{key} ×{v:g}"


def bloon_mod_lines(mods: dict | None) -> list[str]:
    out = []
    speed_parts = [
        t for t in (
            _mult_txt("气球速度", (mods or {}).get("speedMultiplier")),
            _mult_txt("MOAB速度", (mods or {}).get("moabSpeedMultiplier")),
            _mult_txt("Boss速度", (mods or {}).get("bossSpeedMultiplier")),
            _mult_txt("再生速度", (mods or {}).get("regrowRateMultiplier")),
        ) if t
    ]
    if speed_parts:
        out.append("、".join(speed_parts))
    hm = (mods or {}).get("healthMultipliers") or {}
    hp_parts = [
        t for t in (
            _mult_txt("气球血量", hm.get("bloons")),
            _mult_txt("MOAB血量", hm.get("moabs")),
            _mult_txt("Boss血量", hm.get("boss")),
        ) if t
    ]
    if hp_parts:
        out.append("、".join(hp_parts))
    if mods and mods.get("allCamo"):
        out.append("全体隐身")
    if mods and mods.get("allRegen"):
        out.append("全体再生")
    return out


def _cap_items(items: list, limit: int = 8) -> str:
    shown = items[:limit]
    tail = f" …等{len(items)}项" if len(items) > limit else ""
    return "、".join(shown) + tail


def tower_limit_lines(towers: list) -> list[str]:
    banned, limited, pathed, heroes = [], [], [], []
    for t in towers or []:
        raw = str(t.get("tower") or "").strip()
        if not raw or raw == "ChosenPrimaryHero":  # 内部占位符，非真实塔
            continue
        name = tower_cn(raw)
        mx = t.get("max")
        blocked = {
            p: n for p in (1, 2, 3)
            if (n := int(t.get(f"path{p}NumBlockedTiers") or 0)) != 0
        }
        if bool(t.get("isHero")):
            if isinstance(mx, (int, float)) and mx > 0:  # 允许的英雄（如 max=99 表示仅此英雄）
                heroes.append(name)
            continue
        if isinstance(mx, (int, float)):
            if mx == 0:
                banned.append(name)
                continue
            if 0 < mx < 99:
                limited.append(f"{name}×{int(mx)}")
        if blocked:
            detail = "、".join(f"路{p}禁{n}层" for p, n in sorted(blocked.items()))
            pathed.append(f"{name}（{detail}）")
    lines = []
    if banned:
        lines.append(f"🚫 塔禁用：{_cap_items(banned)}")
    if limited:
        lines.append(f"🔢 塔限购：{_cap_items(limited)}")
    if pathed:
        lines.append(f"🧱 路径限制：{_cap_items(pathed)}")
    if heroes:
        lines.append(f"🦸 英雄限定：{_cap_items(heroes)}")
    return lines


def _rules_lines(meta: dict, prefix: str) -> list[str]:
    diff = cn(meta.get("difficulty"), DIFFICULTY_CN)
    mode = cn(meta.get("mode"), MODE_CN)
    map_name = str(meta.get("map") or "?").strip()
    cash = int(meta.get("startingCash") or 0)
    lives = int(meta.get("lives") or 0)
    rounds = f"{int(meta.get('startRound') or 0)}–{int(meta.get('endRound') or 0)}"
    max_towers = int(meta.get("maxTowers") or 0)
    max_paragons = int(meta.get("maxParagons") or 0)

    lines = [f"{prefix}「{(meta.get('name') or '').strip()}」规则"]
    lines.append(f"🗺 地图：{map_name}｜难度：{diff}" + (f"｜模式：{mode}" if mode else ""))
    lines.append(f"💰 初始资金 {cash:,}｜❤️ 生命 {lives:,}｜回合 {rounds}")
    towers_cap = "无限制" if max_towers >= 9999 else f"{max_towers:,}"
    paragon_part = "禁止 Paragon" if max_paragons == 0 else f"Paragon 上限 {max_paragons}"
    lines.append(f"🐒 塔位上限 {towers_cap}｜{paragon_part}")

    bans = [label for key, label in FLAG_LABELS if meta.get(key)]
    if bans:
        lines.append(f"🚫 禁用：{'、'.join(bans)}")
    mod_lines = bloon_mod_lines(meta.get("_bloonModifiers"))
    if mod_lines:
        lines.append(f"气球强化：{'；'.join(mod_lines)}")
    lines += tower_limit_lines(meta.get("_towers"))
    return lines


def format_rules(meta: dict, prefix: str) -> str:
    return "\n".join(_rules_lines(meta, prefix))


async def collect_rules(kind: str, variant: str) -> dict:
    now = bucket_now()
    if kind == "race":
        races = await fetch_body(URL_RACES)
        ev = pick_active(races, now) or fallback_latest(races)
        if not ev:
            return {"empty": "当前没有竞赛活动"}
        meta = await fetch_body(ev["metadata"])
        prefix = "竞赛"
        scoring_raw = "GameTime"
        scoring_cn = "最快用时"
        side_img = ""
    elif kind == "boss":
        bosses = await fetch_body(URL_BOSSES)
        ev = pick_active(bosses, now) or fallback_latest(bosses)
        if not ev:
            return {"empty": "当前没有 Boss 活动"}
        elite = variant != "standard"
        meta_url = ev.get("metadataElite") if elite else ev.get("metadataStandard")
        if not meta_url:
            return {"empty": "该活动暂无此模式的规则数据"}
        meta = await fetch_body(meta_url)
        label = "精英" if elite else "标准"
        prefix = f"Boss·{label}"
        scoring_raw = str(ev.get("eliteScoringType" if elite else "normalScoringType") or "")
        scoring_cn = SCORING_CN.get(scoring_raw, scoring_raw or "?")
        side_img = await _safe(_asset_data_url(ev.get("bossTypeURL"))) if ev.get("bossTypeURL") else ""
    elif kind == "ct":
        return {"empty": "争夺领土暂无通用规则数据，请使用 .btd6活动 或 .btd6排行 领土 查询"}
    else:
        return {"empty": "暂不支持该活动类型的规则查询"}
    map_img = await _safe(_asset_data_url(meta.get("mapURL"))) if meta.get("mapURL") else ""
    return {
        "prefix": prefix, "meta": meta, "map_img": map_img or "",
        "side_img": side_img or "", "scoring_raw": scoring_raw,
        "scoring_cn": scoring_cn, "ev": ev,
    }


def rules_text(col: dict) -> str:
    if col.get("empty"):
        return col["empty"]
    return format_rules(col["meta"], col["prefix"])


# ---------------- 自制地图 ----------------

MAP_FILTERS = {
    "最新": "newest", "newest": "newest",
    "热门": "trending", "trending": "trending",
    "点赞": "mostLiked", "mostliked": "mostLiked", "mostLiked": "mostLiked",
}
FILTER_LABEL = {"newest": "最新", "trending": "热门", "mostLiked": "最多点赞"}


async def collect_maps(filt: str, rows: int) -> dict:
    items = await fetch_body(URL_MAP_FILTER.format(filt))
    top = list(items or [])[:rows]

    async def detail(m: dict) -> tuple[str, int, int]:
        meta = await _safe(fetch_body(m["metadata"])) if m.get("metadata") else None
        if not meta:
            return "", 0, 0
        img = await _safe(_asset_data_url(meta.get("mapURL"))) if meta.get("mapURL") else ""
        return img or "", int(meta.get("plays") or 0), int(meta.get("upvotes") or 0)

    details = await asyncio.gather(*(detail(m) for m in top)) if top else []
    entries = [
        (i, str(m.get("name") or "?").strip(), fmt_date(m.get("createdAt")),
         img, plays, upvotes)
        for i, (m, (img, plays, upvotes)) in enumerate(zip(top, details, strict=True), 1)
    ]
    return {"label": FILTER_LABEL[filt], "entries": entries}


def maps_text(col: dict) -> str:
    lines = [f"自制地图 · {col['label']} Top{len(col['entries'])}", ""]
    if not col["entries"]:
        lines.append("（暂无地图数据）")
    for i, name, created, *_rest in col["entries"]:
        lines.append(f"{i}. {name}（{created}）")
    return "\n".join(lines)


# ---------------- 每日挑战 / 远征 / 玩家档案 ----------------


def _daily_prefix(label: str, advanced: bool) -> str:
    """'Standard 2936: Shadow's Challenge' → '每日标准·第2936期'。"""
    issue = str(label or "").split(":")[0].replace("Advanced", "").replace("Standard", "").strip()
    kind = "每日高级" if advanced else "每日标准"
    return f"{kind}·第{issue}期" if issue.isdigit() else kind


async def collect_daily(advanced: bool) -> dict:
    items = await fetch_body(URL_DAILY)
    want = "Advanced" if advanced else "Standard"
    ev = next((x for x in items if str(x.get("name") or "").startswith(want)), None)
    if not ev:
        return {"empty": "暂无每日挑战数据"}
    meta = await fetch_body(ev["metadata"])
    map_img = await _safe(_asset_data_url(meta.get("mapURL"))) if meta.get("mapURL") else ""
    return {
        "prefix": _daily_prefix(str(ev.get("name") or ""), advanced),
        "meta": meta, "map_img": map_img or "", "side_img": "", "scoring_cn": "固定种子",
    }


_ODYSSEY_DIFFS = (("easy", "简单"), ("medium", "中等"), ("hard", "困难"))


_ODYSSEY_POWER_CN = {
    "BananaFarmer": "香蕉农场", "BananaFarmerPro": "专业香蕉农场",
    "CamoTrap": "迷彩陷阱", "CashDrop": "现金掉落", "CaveMonkey": "洞穴猴",
    "DartTime": "飞镖时间", "EnergisingTotem": "增能图腾", "GlueTrap": "胶水陷阱",
    "MoabMine": "MOAB 地雷", "MonkeyBoost": "猴子强化", "MonkeyBoostPro": "专业猴子强化",
    "Pontoon": "浮桥", "PortableLake": "便携湖", "PortableLakePro": "专业便携湖",
    "RoadSpikes": "道路钉刺", "SheRa": "She Ra", "Skeletor": "Skeletor",
    "SuperMonkeyBeacon": "超级猴信标", "SuperMonkeyStorm": "超级猴风暴",
    "SwordOfPower": "力量之剑", "TechBot": "科技机器人", "TechBotPrime": "专业科技机器人",
    "Thrive": "繁荣", "BattleCat": "战斗猫",
}


def _odyssey_power_name(raw: str) -> str:
    raw = str(raw or "").strip()
    return _ODYSSEY_POWER_CN.get(raw, raw)


def _odyssey_power_icon(raw: str) -> str:
    """远征力量名称 → PowerIcon 本地图标；未知新力量安全降级。"""
    raw = str(raw or "").strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", raw):
        return ""
    return _ui_asset_data_url(f"{raw}Icon.png")


def _odyssey_upgrade_caps(t: dict) -> str:
    """由 path*NumBlockedTiers 计算三路最大开放层级，例如 0/0/1 → 5-5-4；英雄固定满级不显示。"""
    if not isinstance(t, dict) or t.get("isHero"):
        return ""
    def cap(blocked) -> int:
        try:
            n = int(blocked or 0)
        except (TypeError, ValueError):
            return 5
        return 0 if n == -1 else max(0, 5 - n)
    return f"{cap(t.get('path1NumBlockedTiers'))}-{cap(t.get('path2NumBlockedTiers'))}-{cap(t.get('path3NumBlockedTiers'))}"


def _odyssey_top_icon(kind: str) -> str:
    """顶部蓝丝带用小图标：kind ∈ {lives, seats, towers}；缺失返回空串。"""
    mapping = {
        "lives":  ("game", "UI_LivesIcon.webp"),
        "seats":  ("game", "UI_HeroSeat.webp"),
        "towers": ("ui",   "monkey-cap.png"),
    }
    item = mapping.get(kind)
    if not item:
        return ""
    src, fname = item
    if src == "game":
        return _game_asset_data_url(fname)
    return _ui_asset_data_url(fname)


# 当前远征的完成奖杯数：游戏内活动页显示，开放 API 的 _rewards 未包含，逐期转录
_ODYSSEY_TROPHY = {"mt7bsc6c": 15}

_ODYSSEY_REWARD_ICON = {
    # reward 标签 → (source, filename)；game 用 .webp, ui 用 .png
    "MonkeyMoney": ("game", "BloonjaminsIcon.webp"),
    "Trophy":      ("game", "UI_TrophyIcon.webp"),
}


def _odyssey_reward_icon(kind: str, sub: str) -> str:
    """奖励图标：MonkeyMoney / Trophy 用本地素材；Power/Insta 由调用方决定。"""
    base = _ODYSSEY_REWARD_ICON.get(kind)
    if base:
        src, fname = base
        if src == "game":
            return _game_asset_data_url(fname)
        return _ui_asset_data_url(fname)
    return ""


def _odyssey_map_icons() -> dict[str, str]:
    """岛屿行用小图标：金币 / 开始回合 / 难度脸谱（按当前难度选取）。"""
    icons = {
        "coin":   _game_asset_data_url("UI_CoinIcon.webp"),
        "play":   _ui_asset_data_url("start-round.png"),
    }
    return icons


_odyssey_thumb_mem: OrderedDict[str, str] = OrderedDict()
_odyssey_thumb_lock = threading.Lock()


def _odyssey_thumbnail_data_url(cache_key: str, data_url: str) -> str:
    """将逐岛大地图压成卡片尺寸缩略图，避免渲染器解码原始大图造成高负载。"""
    if not data_url or not data_url.startswith("data:image/"):
        return data_url
    key = str(cache_key or "") or hashlib.md5(data_url.encode("utf-8")).hexdigest()
    # 缩略图现在经 to_thread 并发执行，缓存读写必须持锁
    with _odyssey_thumb_lock:
        hit = _odyssey_thumb_mem.get(key)
        if hit:
            _odyssey_thumb_mem.move_to_end(key)
            return hit
    try:
        from PIL import Image
        raw = base64.b64decode(data_url.split(",", 1)[1], validate=True)
        with Image.open(io.BytesIO(raw)) as source:
            image = source.convert("RGB")
            resampling = getattr(Image, "Resampling", Image).LANCZOS
            image.thumbnail((330, 206), resampling)
            buf = io.BytesIO()
            image.save(buf, format="JPEG", quality=84, optimize=True)
        thumb = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return data_url
    with _odyssey_thumb_lock:
        _odyssey_thumb_mem[key] = thumb
        _odyssey_thumb_mem.move_to_end(key)
        _prune_ordered(_odyssey_thumb_mem, 64)
    return thumb


def _reward_txt(rewards: list) -> str:
    out = []
    for r in rewards or []:
        s = str(r)
        if s.startswith("MonkeyMoney:"):
            out.append(f"猴币×{s.split(':', 1)[1]}")
        elif s.startswith("InstaMonkey:"):
            out.append(f"即时猴·{tower_cn(s.split(':')[1])}")
        elif s.startswith("Power:"):
            out.append(f"力量·{s.split(':', 1)[1]}")
        else:
            out.append(s.replace(":", "·"))
    return "、".join(out) if out else "无"


async def collect_odyssey() -> dict:
    now = bucket_now()
    items = await fetch_body(URL_ODYSSEY)
    ev = pick_active(items, now) or pick_next(items, now) or fallback_latest(items)
    if not ev:
        return {"empty": "当前没有远征活动"}

    async def collect_diff(d: str) -> tuple[str, dict]:
        url = ev.get(f"metadata_{d}")
        meta = await _safe(fetch_body(url)) if url else None
        if meta is not None:
            # 游戏内活动页的奖励含完成奖杯，开放 API 未返回——按期转录补齐
            trophy = _ODYSSEY_TROPHY.get(str(ev.get("id") or ""))
            if trophy:
                rewards = [r for r in (meta.get("_rewards") or [])
                           if not str(r).startswith("Trophy:")]
                rewards.append(f"Trophy:{trophy}")
                meta["_rewards"] = rewards
        maps = []
        maps_url = (meta or {}).get("maps")
        if maps_url:
            mres = await _safe(fetch_body(maps_url))
            if isinstance(mres, dict):
                maps = mres.get("body") or []
            elif isinstance(mres, list):
                maps = mres

        async def map_entry(mp: dict) -> dict:
            map_url = mp.get("mapURL")
            source_img = await _safe(_asset_data_url(map_url)) if map_url else ""
            # PIL 缩放/编码是重 CPU 同步操作，放线程池避免卡住事件循环
            img = await asyncio.to_thread(_odyssey_thumbnail_data_url, map_url, source_img or "")
            # 保留逐岛规则字段，图片卡片需要用它显示回合、难度、模式和强化状态。
            return {
                "name": str(mp.get("name") or "?").strip(),
                "map": str(mp.get("map") or "").strip(),
                "img": img or "",
                "difficulty": str(mp.get("difficulty") or "").strip(),
                "mode": str(mp.get("mode") or "").strip(),
                "startingCash": int(mp.get("startingCash") or 0),
                "startRound": int(mp.get("startRound") or 0),
                "endRound": int(mp.get("endRound") or 0),
                "lives": int(mp.get("lives") or 0),
                "maxLives": int(mp.get("maxLives") or 0),
                "maxTowers": int(mp.get("maxTowers") or 0),
                "maxParagons": int(mp.get("maxParagons") or 0),
                "roundSets": mp.get("roundSets") or [],
                "_bloonModifiers": mp.get("_bloonModifiers") or {},
                "disableMK": bool(mp.get("disableMK")),
                "disablePowers": bool(mp.get("disablePowers")),
                "disableInstas": bool(mp.get("disableInstas")),
                "disableSelling": bool(mp.get("disableSelling")),
                "noContinues": bool(mp.get("noContinues")),
                "disableDoubleCash": bool(mp.get("disableDoubleCash")),
            }

        entries = await asyncio.gather(*(map_entry(mp) for mp in maps[:5]))
        return d, {"meta": meta, "maps": entries}

    collected = await asyncio.gather(*(collect_diff(d) for d, _label in _ODYSSEY_DIFFS))
    return {"ev": ev, "diffs": dict(collected)}


def _odyssey_meta_lines(meta: dict | None) -> list[str]:
    if not meta:
        return ["（该难度数据缺失）"]
    powers = meta.get("_availablePowers") or []
    usable_powers = [p.get("power") for p in powers if isinstance(p, dict) and p.get("max")]
    towers = meta.get("_availableTowers") or []
    lines = [f"初始生命 {int(meta.get('startingHealth') or 0):,}"]
    if meta.get("isExtreme"):
        lines.append("极限模式")
    if towers:
        lines.append(f"可用塔 {len(towers)} 种")
    if powers:
        shown = "、".join(_odyssey_power_name(str(x)) for x in usable_powers[:6])
        tail = f" 等{len(usable_powers)}种" if len(usable_powers) > 6 else ""
        lines.append(f"力量：{shown}{tail}")
    return lines


def odyssey_text(col: dict) -> str:
    if col.get("empty"):
        return col["empty"]
    ev, diffs = col["ev"], col["diffs"]
    state = _STATE_TXT[_state_of(ev, bucket_now())]
    lines = [
        "🏰 远征活动",
        f"{(ev.get('name') or '').strip()}（{state}）",
        _fmt_range(ev),
        (ev.get("description") or "").strip(),
        "",
    ]
    for d, label in _ODYSSEY_DIFFS:
        diff = diffs.get(d) or {}
        meta = diff.get("meta")
        lines.append(f"【{label}】")
        lines += [f"  {x}" for x in _odyssey_meta_lines(meta)]
        rewards = (meta or {}).get("_rewards") or []
        if rewards:
            lines.append(f"  {_reward_txt(rewards)}")
        maps = diff.get("maps") or []
        if maps:
            lines.append(f"  🗺 地图：{'、'.join(m['name'] for m in maps)}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _odyssey_img(data_url: str, cls: str, fallback: str, alt: str = "") -> str:
    if data_url:
        return f"<img class='{cls}' src='{_esc(data_url)}' alt='{_esc(alt)}'/>"
    return f"<span class='{cls}-fallback'>{_esc(fallback)}</span>"


def _odyssey_tower_lookup(meta: dict) -> dict[str, dict]:
    return {
        str(t.get("tower") or "").strip(): t
        for t in meta.get("_availableTowers") or []
        if isinstance(t, dict) and str(t.get("tower") or "").strip()
    }


def _odyssey_tower_card(raw: str, is_hero: bool, count_text: str = "",
                        classes: str = "", category: str = "",
                        upgrade_caps: str = "",
                        badge_pos: str = "right") -> str:
    name = _tower_display_name(raw, is_hero)
    icon = _tower_icon(raw, is_hero)
    cat = category or _tower_category(raw, is_hero)
    card_classes = "ody-unit-card" + (" hero" if is_hero else "") + f" cat-{cat}"
    if classes:
        card_classes += " " + classes
    if upgrade_caps:
        card_classes += " with-caps"
    content = _odyssey_img(icon, "ody-unit-icon", name, name)
    if not icon:
        content = f"<span class='ody-unit-fallback'>{_esc(name)}</span>"
    if count_text:
        qcls = "ody-unit-quantity" + (" left" if badge_pos == "left" else "")
        content += f"<span class='{qcls}'>{_esc(count_text)}</span>"
    if upgrade_caps:
        content += f"<span class='ody-unit-caps'>{_esc(upgrade_caps)}</span>"
    return f"<div class='ody-unit-wrap'><div class='{card_classes}' title='{_esc(name)}'>{content}</div></div>"


def _odyssey_default_crew_html(meta: dict) -> str:
    available = _odyssey_tower_lookup(meta)
    defaults = [x for x in meta.get("_defaultTowers") or [] if isinstance(x, dict)]
    hero_item = next((x for x in defaults if available.get(str(x.get("name") or ""), {}).get("isHero")), None)
    hero_html = "<span class='ody-unit-fallback'>无英雄</span>"
    if hero_item:
        raw = str(hero_item.get("name") or "").strip()
        info = available.get(raw) or {}
        quantity = int(hero_item.get("quantity") or 0)
        denom = int(info.get("max") or 1)
        hero_html = _odyssey_tower_card(raw, True, f"{quantity}/{denom}", "big",
                                        category="hero")

    # 对照游戏内活动页：英雄左侧放大，猴子单行排列在右侧，圆形分类色底 + 左上角数量角标
    tower_html = []
    for item in defaults:
        raw = str(item.get("name") or "").strip()
        if not raw or item is hero_item:
            continue
        info = available.get(raw) or {}
        quantity = int(item.get("quantity") or 0)
        max_count = info.get("max")
        denom = int(max_count) if isinstance(max_count, (int, float)) and max_count > 0 else quantity
        img = _tower_portrait(raw)
        face = (f"<img src='{img}' style='width:32px;height:32px;object-fit:contain;'/>" if img
                else f"<span style='font-size:8px;color:#ffffff;line-height:10px;'>{_esc(_tower_cn(raw))}</span>")
        tower_html.append(
            "<span style='position:relative;display:inline-block;width:44px;margin:14px 3px 0 0;"
            "vertical-align:top;text-align:center;'>"
            "<span style='position:absolute;top:-8px;left:-3px;z-index:2;background:#1596d2;"
            "border:2px solid #e7f8ff;border-radius:6px;color:#ffffff;font-size:10px;line-height:14px;"
            "padding:0 4px;font-weight:900;'>{}/{}".format(quantity, denom) + "</span>"
            "<span style='display:flex;align-items:center;justify-content:center;width:40px;height:40px;"
            "border-radius:50%;background:linear-gradient(180deg,#8fc460,#4d8f3c);"
            "border:2px solid rgba(0,0,0,.3);box-shadow:inset 0 1px 0 rgba(255,255,255,.4);'>" + face + "</span></span>")
    return ("<div class='ody-panel ody-crew-panel'>"
            "<div class='ody-ribbon ody-panel-title'><span>默认队伍</span></div>"
            "<div class='ody-crew-body'><div class='ody-crew-hero'>" + hero_html +
            "</div><div class='ody-crew-grid-cell'><div class='ody-default-grid' style='white-space:nowrap;'>"
            + "".join(tower_html) + "</div></div></div></div>")


def _odyssey_available_html(meta: dict) -> str:
    towers = [t for t in meta.get("_availableTowers") or []
              if isinstance(t, dict) and str(t.get("tower") or "").strip() and t.get("max") != 0]
    heroes = [t for t in towers if t.get("isHero")]
    regular = [t for t in towers if not t.get("isHero")]
    hero_html = "".join(
        _odyssey_tower_card(str(t.get("tower")), True, "", "available",
                            category="hero",
                            upgrade_caps=_odyssey_upgrade_caps(t),
                            badge_pos="left")
        for t in heroes
    )
    tower_html = "".join(
        _odyssey_tower_card(
            str(t.get("tower")), False,
            str(int(t.get("max"))) if isinstance(t.get("max"), (int, float)) and t.get("max") > 0 else "∞",
            "available",
            category=_tower_category(str(t.get("tower")), False),
            upgrade_caps=_odyssey_upgrade_caps(t),
            badge_pos="left",
        )
        for t in regular
    )
    powers = [p for p in meta.get("_availablePowers") or []
              if isinstance(p, dict) and str(p.get("power") or "").strip() and p.get("max")]
    power_html = []
    for power in powers:
        raw = str(power.get("power") or "").strip()
        icon = _odyssey_power_icon(raw)
        name = _odyssey_power_name(raw)
        icon_html = _odyssey_img(icon, "ody-power-icon", name, name)
        if not icon:
            icon_html = f"<span class='ody-power-fallback'>{_esc(name)}</span>"
        count = int(power.get("max") or 0)
        power_html.append(
            f"<div class='ody-power-wrap'><div class='ody-power-tile' title='{_esc(name)}'>"
            f"{icon_html}<span class='ody-power-count'>{count}</span></div></div>"
        )
    # 面板直接作为表格单元格：同行单元格天然等高，三面板底部严格对齐
    return ("<div class='ody-available'>"
            "<div class='ody-panel ody-av-panel heroes'>"
            f"<div class='ody-av-title ody-av-title-dark'>可用英雄：</div>"
            f"<div class='ody-hero-grid'>{hero_html or '—'}</div></div>"
            "<div class='ody-panel ody-av-panel towers'>"
            f"<div class='ody-av-title ody-av-title-dark'>可用猴子：</div>"
            f"<div class='ody-tower-grid'>{tower_html or '—'}</div></div>"
            "<div class='ody-panel ody-av-panel powers'>"
            f"<div class='ody-av-title ody-av-title-dark'>可用力量：</div>"
            f"<div class='ody-power-grid'>{''.join(power_html) or '—'}</div></div>"
            "</div>")


def _odyssey_rewards_html(rewards: list) -> str:
    items = []
    for reward in rewards or []:
        raw = str(reward or "")
        if raw.startswith("MonkeyMoney:"):
            value = raw.split(":", 1)[1]
            icon = _odyssey_reward_icon("MonkeyMoney", value)
            icon_html = _odyssey_img(icon, "ody-reward-icon", "💵", "猴币")
            if not icon:
                icon_html = "<span class='ody-reward-emoji'>💵</span>"
            items.append(f"<div class='ody-reward-cell'>{icon_html}"
                         f"<div class='ody-reward-value'>{_esc(value)}</div></div>")
        elif raw.startswith("Trophy:"):
            value = raw.split(":", 1)[1]
            icon = _odyssey_reward_icon("Trophy", value)
            icon_html = _odyssey_img(icon, "ody-reward-icon", "🏆", "奖杯")
            if not icon:
                icon_html = "<span class='ody-reward-emoji'>🏆</span>"
            items.append(f"<div class='ody-reward-cell'>{icon_html}"
                         f"<div class='ody-reward-value'>{_esc(value)}</div></div>")
        elif raw.startswith("Power:"):
            power = raw.split(":", 1)[1]
            icon = _odyssey_power_icon(power)
            icon_html = _odyssey_img(icon, "ody-reward-icon", _odyssey_power_name(power), _odyssey_power_name(power))
            if not icon:
                icon_html = "<span class='ody-reward-emoji'>⚡</span>"
            items.append(f"<div class='ody-reward-cell'>{icon_html}"
                         f"<div class='ody-reward-value'>{_esc(_odyssey_power_name(power))}</div></div>")
        elif raw.startswith("InstaMonkey:"):
            spec = raw.split(":", 1)[1]
            tower, _, tiers = spec.partition(",")
            tier_txt = "-".join(tiers) if tiers else ""
            icon = _tower_icon(tower, False)
            icon_html = _odyssey_img(icon, "ody-reward-icon", tower_cn(tower), tower_cn(tower))
            value_txt = "即时猴" + (f" {tier_txt}" if tier_txt else "")
            items.append(f"<div class='ody-reward-cell'>{icon_html}"
                         f"<div class='ody-reward-value'>{_esc(value_txt)}</div></div>")
    return ("<div class='ody-panel ody-reward-panel'>"
            "<div class='ody-ribbon ody-panel-title'><span>奖励</span></div>"
            "<div class='ody-reward-grid'>" + ("".join(items) or "<div class='ody-reward-value'>无</div>") +
            "</div></div>")


def _odyssey_map_rule_text(mp: dict) -> str:
    modifiers = _race_modifier_items(mp.get("_bloonModifiers"))
    details = [f"{label} {value}" for label, value, _icon in modifiers]
    custom_rounds = [str(x) for x in mp.get("roundSets") or [] if str(x).casefold() != "default"]
    if custom_rounds:
        details.append("自定义回合")
    for key, label in FLAG_LABELS:
        if mp.get(key):
            details.append(label)
    return "默认规则 · 无强化" if not details else " · ".join(details)


def _odyssey_maps_html(maps: list) -> str:
    icons = _odyssey_map_icons()
    rows = []
    for mp in maps or []:
        thumb = (f"<img class='ody-map-img' src='{_esc(mp['img'])}' alt='{_esc(mp.get('map') or mp.get('name') or '')}'/>"
                 if mp.get("img") else "<div class='ody-map-empty'>暂无地图图像</div>")
        difficulty = cn(mp.get("difficulty"), DIFFICULTY_CN) or "未知难度"
        mode = cn(mp.get("mode"), MODE_CN) or "标准"
        start_round = int(mp.get("startRound") or 0)
        end_round = int(mp.get("endRound") or 0)
        rounds = f"{start_round}/{end_round}" if start_round or end_round else "—"
        rule = _odyssey_map_rule_text(mp)
        # 难度脸谱：Beginner/Intermediate/Advanced/Expert 对应四张脸谱
        diff_face = _ui_asset_data_url(f"Map{_ODIFF_TO_BTN.get(mp.get('difficulty'), 'Beginner')}Btn.png")
        coin_img = _odyssey_img(icons.get("coin", ""), "ody-mini-icon", "🪙", "金币")
        if not icons.get("coin"):
            coin_img = "<span class='ody-coin'>🪙</span>"
        play_img = _odyssey_img(icons.get("play", ""), "ody-mini-icon", "▶", "开始")
        if not icons.get("play"):
            play_img = "<span class='ody-play'>▶</span>"
        diff_img = _odyssey_img(diff_face, "ody-mini-icon", "●", difficulty)
        if not diff_face:
            diff_img = "<span class='ody-diff'>●</span>"
        rows.append(
            "<div class='ody-map-row'><div class='ody-map-img-cell'>" + thumb +
            "</div><div class='ody-map-info'>"
            "<div class='ody-map-meta'>"
            f"<div class='ody-map-meta-item'>{coin_img} {int(mp.get('startingCash') or 0):,}</div>"
            f"<div class='ody-map-meta-item'>{play_img}{_esc(rounds)}</div>"
            f"<div class='ody-map-meta-item'>{diff_img}{_esc(difficulty)} / {_esc(mode)}</div>"
            "</div>"
            f"<div class='ody-map-rule'>{_esc(rule)}</div>"
            "</div></div>"
        )
    return "<div class='ody-maps'>" + ("".join(rows) or "<div class='ody-panel ody-map-empty'>暂无远征地图</div>") + "</div>"


_ODIFF_TO_BTN = {"Beginner": "Beginner", "Easy": "Beginner",
                  "Intermediate": "Intermediate", "Medium": "Intermediate",
                  "Advanced": "Advanced", "Hard": "Advanced",
                  "Expert": "Expert", "Impoppable": "Expert"}


def _odyssey_card_height(meta: dict | None, maps_count: int) -> int:
    """远征单难度卡片高度估算（与 _odyssey_shell 各分区高度对应）；
    handle_odyssey 用它取三难度最大值做统一画布，保证 QQ 预览显示宽度一致。"""
    if not meta:
        return 260
    at = [t for t in meta.get("_availableTowers") or []
          if isinstance(t, dict) and t.get("max") != 0]
    heroes = sum(1 for t in at if t.get("isHero"))
    regular = len(at) - heroes
    power_count = sum(1 for p in meta.get("_availablePowers") or []
                      if isinstance(p, dict) and p.get("max"))
    rows = max(1, -(-heroes // 2), -(-regular // 4), -(-power_count // 3))
    is_ext = bool(meta.get("isExtreme"))
    return (8 + 38 + 24 + 18 + (22 if is_ext else 0) + 216 + 10
            + max(245, 42 + rows * 82) + 10 + 32 + 10
            + max(1, maps_count) * 130 + 18)


def odyssey_diff_html(col: dict, d: str, label: str) -> str:
    """远征单张难度卡片：按游戏内远征页布局展示队伍、奖励、猴子、力量和逐岛规则。"""
    if col.get("empty"):
        return _odyssey_shell(f"<div class='ody-panel ody-map-empty'>{_esc(col['empty'])}</div>", 260, theme="tan")
    ev = col["ev"]
    diff = col["diffs"].get(d) or {}
    meta = diff.get("meta")
    if not meta:
        return _odyssey_shell(f"<div class='ody-event'>{_esc((ev.get('name') or '').strip())} · {label}难度</div>"
                              "<div class='ody-panel ody-map-empty'>（该难度数据缺失）</div>", 260, theme="tan")

    lives = int(meta.get("startingHealth") or 0)
    seats = int(meta.get("maxMonkeySeats") or 0)
    towers_cap = int(meta.get("maxMonkeysOnBoat") or 0)
    state = _STATE_TXT[_state_of(ev, bucket_now())]
    event_name = (ev.get("name") or "远征活动").strip()
    description = (ev.get("description") or "").strip()
    is_extreme = bool(meta.get("isExtreme"))
    lives_icon = _odyssey_top_icon("lives")
    seats_icon = _odyssey_top_icon("seats")
    towers_icon = _odyssey_top_icon("towers")
    lives_img = f"<img class='ody-ribbon-icon' src='{_esc(lives_icon)}'/>" if lives_icon else "❤"
    seats_img = f"<img class='ody-ribbon-icon' src='{_esc(seats_icon)}'/>" if seats_icon else "🪑"
    towers_img = f"<img class='ody-ribbon-icon' src='{_esc(towers_icon)}'/>" if towers_icon else "🐵"
    top = (
        "<div class='ody-ribbons'>"
        f"<div class='ody-ribbon-cell'><div class='ody-ribbon'>{lives_img} 生命：{lives}</div></div>"
        f"<div class='ody-ribbon-cell'><div class='ody-ribbon'>{seats_img} 猴位：{seats}</div></div>"
        f"<div class='ody-ribbon-cell'><div class='ody-ribbon'>{towers_img} 猴子上限：{towers_cap}</div></div>"
        "</div>"
        + ("<div class='ody-extreme-badge'>极限模式</div>" if is_extreme else "")
        + "<div class='ody-top-grid'><div class='ody-top-cell crew'>"
        f"{_odyssey_default_crew_html(meta)}"
        "</div><div class='ody-top-cell reward'>"
        f"{_odyssey_rewards_html(meta.get('_rewards') or [])}"
        "</div></div>"
        f"{_odyssey_available_html(meta)}"
        "<div style='text-align:center;margin:14px auto 8px;'>"
        "<span style='position:relative;display:inline-block;padding:4px 26px;"
        "background:linear-gradient(180deg,#46c8f1 0%,#129ed0 56%,#087eaf 100%);"
        "border:2px solid #076b99;border-radius:2px;color:#ffffff;font-size:15px;"
        "font-weight:900;letter-spacing:0.5px;text-shadow:0 2px 0 #075b8b;"
        "box-shadow:inset 0 1px 0 rgba(255,255,255,.75),0 2px 0 rgba(0,59,79,.28);'>岛屿规则</span></div>"
        f"{_odyssey_maps_html(diff.get('maps') or [])}"
        + f"<div class='ody-event-desc' style='margin-top:6px;text-align:center;'>"
        + f"{_esc(event_name)} · {_esc(label)}难度 · {_esc(_fmt_range(ev))} · {_esc(state)}"
        + (f" · {_esc(description)}" if description else "") + "</div>"
    )
    height = _odyssey_card_height(meta, len(diff.get("maps") or []))
    # 由调用方传入统一高度时，直接使用以保证三图在 QQ 预览中显示宽度一致（QQ 按最大边缩放，较矮的图会被等比放大导致视觉宽度不一）
    uh = diff.get("_unified_h")
    if isinstance(uh, int) and uh > height:
        height = uh
    return _odyssey_shell(top, height, theme="tan")


_PLAYER_ID_RE = re.compile(r"[0-9a-f]{40,}")


def _extract_player_id(arg: str) -> str:
    m = _PLAYER_ID_RE.search(arg or "")
    return m.group(0) if m else ""


async def collect_player(pid: str) -> dict:
    body = await fetch_body(URL_USERS + pid)
    if not isinstance(body, dict) or not body.get("displayName"):
        return {"empty": "未找到该玩家，请确认 ID 是否正确"}
    banner = await _safe(_asset_data_url(body.get("bannerURL"))) if body.get("bannerURL") else ""
    avatar = await _safe(_asset_data_url(body.get("avatarURL"))) if body.get("avatarURL") else ""
    return {"p": body, "banner": banner or "", "avatar": avatar or ""}


def player_text(col: dict) -> str:
    if col.get("empty"):
        return col["empty"]
    p = col["p"]
    popped = p.get("bloonsPopped") or {}
    gp = p.get("gameplay") or {}
    vr = p.get("veteranRank") or 0
    lines = [
        f"🐒 {_esc(p.get('displayName'))}",
        f"等级 {p.get('rank')}" + (f"（老兵 {vr}）" if vr else "")
        + f" · 粉丝 {fmt_cn_num(p.get('followers'))}",
        f"最高回合 {p.get('highestRound')} · CHIMPS {gp.get('highestRoundCHIMPS', '—')}"
        f" · 成就 {p.get('achievements')}",
        f"最常用猴：{tower_cn(str(p.get('mostExperiencedMonkey') or ''))}",
        "",
        " popped：",
        f"  总气球 {fmt_cn_num(popped.get('bloonsPopped'))} · Boss {fmt_cn_num(popped.get('bossesPopped'))}",
        f"  MOAB {fmt_cn_num(popped.get('moabsPopped'))} · ZOMG {fmt_cn_num(popped.get('zomgsPopped'))}"
        f" · 陶瓷 {fmt_cn_num(popped.get('ceramicsPopped'))}",
        f"  迷彩 {fmt_cn_num(popped.get('camosPopped'))} · 金气球 {fmt_cn_num(popped.get('goldenBloonsPopped'))}",
        "",
        f"局数 {fmt_cn_num(gp.get('gameCount'))} · 胜场 {fmt_cn_num(gp.get('gamesWon'))}"
        f" · 挑战完成 {fmt_cn_num(gp.get('challengesCompleted'))}",
        f"累计猴币 {fmt_cn_num(gp.get('cashEarned'))} · 奖杯 {fmt_cn_num(gp.get('totalTrophiesEarned'))}",
    ]
    return "\n".join(lines)


def player_html(col: dict) -> str:
    if col.get("empty"):
        body = f'<div class="panel"><div class="empty">{_esc(col["empty"])}</div></div>'
        return _shell(body, 300)
    p = col["p"]
    popped = p.get("bloonsPopped") or {}
    gp = p.get("gameplay") or {}
    vr = p.get("veteranRank") or 0

    banner = (f"<div class='pbanner'><img src='{_esc(col['banner'])}'/></div>" if col.get("banner") else "")
    avatar = (f"<div class='pavatar'><img src='{_esc(col['avatar'])}'/></div>" if col.get("avatar") else "")
    vr_txt = f" · 老兵 {_esc(vr)}" if vr else ""
    rank = _esc(p.get("rank") or "—")
    followers = _esc(fmt_cn_num(p.get("followers")))
    most_used = _esc(tower_cn(str(p.get("mostExperiencedMonkey") or "")))
    head = (f"<div class='panel'>{banner}"
            f"<div class='phead'>{avatar}"
            f"<div class='ptext'><div class='big'>{_esc(p.get('displayName'))}</div>"
            f"<div class='sub'>等级 {rank}{vr_txt} · 粉丝 {followers}"
            f" · 最常用猴 {most_used}</div></div></div></div>")

    def stat_panel(title: str, pairs: list[tuple[str, str]]) -> str:
        rows = "".join(
            f"<div class='st'>{_esc(k)} <b>{_esc(v)}</b></div>" for k, v in pairs
        )
        return f"<div class='panel'><div class='ptitle'>{_esc(title)}</div>{rows}</div>"

    body = head + (
        stat_panel("关键数据", [
            ("最高回合", str(p.get("highestRound") or "—")),
            ("CHIMPS 最高", str(gp.get("highestRoundCHIMPS") or "—")),
            ("成就", str(p.get("achievements") or "—")),
            ("累计猴币", fmt_cn_num(gp.get("cashEarned"))),
        ])
        + stat_panel("气球战报", [
            ("总气球", fmt_cn_num(popped.get("bloonsPopped"))),
            ("Boss 气球", fmt_cn_num(popped.get("bossesPopped"))),
            ("MOAB", fmt_cn_num(popped.get("moabsPopped"))),
            ("金气球", fmt_cn_num(popped.get("goldenBloonsPopped"))),
        ])
        + stat_panel("游戏历程", [
            ("局数 / 胜场", f"{fmt_cn_num(gp.get('gameCount'))} / {fmt_cn_num(gp.get('gamesWon'))}"),
            ("挑战完成", fmt_cn_num(gp.get("challengesCompleted"))),
            ("奖杯", fmt_cn_num(gp.get("totalTrophiesEarned"))),
            ("Odyssey 星", fmt_cn_num(gp.get("totalOdysseyStars"))),
        ])
    )
    return _shell(body, 20 + (280 if col.get("banner") else 0) + 230 + 3 * 285 + 40)


# ---------------- 参数解析 ----------------

RUSH_WORDS = {"rush", "冲刺", "bossrush", "bossRush", "冲刺赛"}
RACE_WORDS = {"race", "races", "竞速", "竞赛"}
BOSS_WORDS = {"boss", "bosses", "首领", "boss战", "魔王"}
CT_WORDS = {"ct", "领土", "争夺", "争夺领土"}
STANDARD_WORDS = {"standard", "标准", "普通"}
ELITE_WORDS = {"elite", "精英"}
PLAYER_WORDS = {"player", "个人", "玩家"}
TEAM_WORDS = {"team", "战队", "团队"}


def parse_kind(tokens: list[str]) -> str | None:
    for t in tokens:
        k = t.lower()
        if k in RUSH_WORDS:
            return "rush"
        if k in RACE_WORDS:
            return "race"
        if k in BOSS_WORDS:
            return "boss"
        if k in CT_WORDS:
            return "ct"
    return None


def parse_variant(tokens: list[str], default: str) -> str:
    for t in tokens:
        k = t.lower()
        if k in ELITE_WORDS:
            return "elite"
        if k in STANDARD_WORDS:
            return "standard"
        if k in TEAM_WORDS:
            return "team"
        if k in PLAYER_WORDS:
            return "player"
    return default


def parse_rows(tokens: list[str]) -> int:
    rows = DEFAULT_ROWS
    for t in tokens:
        if t.isdigit():
            rows = max(1, min(int(t), MAX_ROWS))
    return rows


def lb_page_size(kind: str) -> int:
    return LB_PAGE_SIZES.get(kind, 50)


def parse_lb_page(tokens: list[str]) -> int | None:
    """解析 P 页码：支持 'P2' / 'p2' / 'P 2' 两种写法。"""
    for i, t in enumerate(tokens):
        if t.lower() == "p" and i + 1 < len(tokens) and tokens[i + 1].isdigit():
            try:
                n = int(tokens[i + 1])
                if 1 <= n <= LB_MAX_PAGE:
                    return n
            except ValueError:
                pass
        if re.fullmatch(r"[pP]\d+", t):
            try:
                n = int(t[1:])
                if 1 <= n <= LB_MAX_PAGE:
                    return n
            except ValueError:
                pass
    return None


def parse_lb_rank(tokens: list[str]) -> int | None:
    """解析排名数字：纯数字且不是 P 页码的一部分。"""
    for i, t in enumerate(tokens):
        if t.lower() == "p":
            continue
        if re.fullmatch(r"[pP]\d+", t):
            continue
        if t.isdigit():
            if i > 0 and tokens[i - 1].lower() == "p":
                continue  # P 后的数字已作为页码
            try:
                n = int(t)
                if 1 <= n <= LB_MAX_RANK:
                    return n
            except ValueError:
                pass
    return None


def lb_page_url(base: str, page: int) -> str:
    if page <= 1:
        return base
    return f"{base}?page={page}"


async def fetch_leaderboard_page(start_url: str, page: int) -> list:
    """拉取指定页的排行榜（单页，不做分页累积）。"""
    url = lb_page_url(start_url, page)
    body = await fetch_body(url)
    return body if isinstance(body, list) else []


async def collect_leaderboard_page(kind: str, variant: str, page: int) -> dict:
    """拉取并整理指定页的排行榜；页码越界返回 empty。"""
    if kind == "rush":
        # Boss Rush 暂无公开排行榜接口，显式返回空榜，避免落入 CT 分支取到错误数据
        return {"empty": "Boss Rush 排行榜暂未开放（NK 仅在 /btd6/events 提供摘要，详细榜单待开放）"}
    now = bucket_now()
    scoring = ""
    img = ""
    page = max(1, min(page, LB_MAX_PAGE))
    size = lb_page_size(kind)
    if kind == "race":
        races = await fetch_body(URL_RACES)
        ev = pick_active(races, now) or fallback_latest(races)
        if not ev:
            return {"empty": "当前没有竞赛活动"}
        start_url = ev.get("leaderboard") or ""
        if not start_url:
            return {"empty": "该活动暂无排行榜"}
        entries = await fetch_leaderboard_page(start_url, page)
        head = f"竞赛「{(ev.get('name') or '').strip()}」排行榜 第{page}页（{size}人/页）"
        scoring = "GameTime"
        meta = await _safe(fetch_body(ev["metadata"])) if ev.get("metadata") else None
        if meta and meta.get("mapURL"):
            img = await _safe(_asset_data_url(meta["mapURL"])) or ""
    elif kind == "boss":
        bosses = await fetch_body(URL_BOSSES)
        ev = pick_active(bosses, now) or fallback_latest(bosses)
        if not ev:
            return {"empty": "当前没有 Boss 活动"}
        elite = variant == "elite"
        url_key = "leaderboard_elite_players_1" if elite else "leaderboard_standard_players_1"
        start_url = ev.get(url_key) or ""
        if not start_url:
            return {"empty": "该活动暂无此模式的排行榜"}
        entries = await fetch_leaderboard_page(start_url, page)
        label = "精英" if elite else "标准"
        scoring = str(ev.get("eliteScoringType" if elite else "normalScoringType") or "")
        mode_cn = SCORING_CN.get(scoring, scoring or "?")
        head = f"Boss「{(ev.get('name') or '').strip()}」{label}排行榜 第{page}页（{size}人/页，{mode_cn}）"
        if ev.get("bossTypeURL"):
            img = await _safe(_asset_data_url(ev["bossTypeURL"])) or ""
    else:  # ct
        cts = await fetch_body(URL_CT)
        ev = pick_active(cts, now) or fallback_latest(cts)
        if not ev:
            return {"empty": "当前没有争夺领土活动"}
        team = variant == "team"
        url_key = "leaderboard_team" if team else "leaderboard_player"
        start_url = ev.get(url_key) or ""
        if not start_url:
            return {"empty": "该活动暂无此榜的排行榜"}
        entries = await fetch_leaderboard_page(start_url, page)
        label = "战队" if team else "个人"
        head = f"争夺领土 {label}排行榜 第{page}页（{size}人/页）"
    if not entries:
        return {"empty": f"第{page}页暂无数据（排行榜可能不足 {(page-1)*size+1} 人）"}
    start_rank = (page - 1) * size + 1
    rows_out = [
        (start_rank + i, str(e.get("displayName") or "?").strip(), fmt_score(scoring, e.get("score")))
        for i, e in enumerate(entries)
    ]
    return {
        "head": head, "status": event_status_line(ev, now), "entries": rows_out,
        "img": img, "page": page,
    }


async def fetch_rank_entry(kind: str, variant: str, rank: int) -> dict | None:
    """取指定排名的单条排行榜记录（用于玩家档案查询）。"""
    if kind == "rush":
        # Boss Rush 暂无公开排行榜接口，显式返回空结果，避免落入 CT 分支取到错误数据
        return None
    size = lb_page_size(kind)
    page = (rank - 1) // size + 1
    idx = (rank - 1) % size
    now = bucket_now()
    start_url = ""
    if kind == "race":
        races = await fetch_body(URL_RACES)
        ev = pick_active(races, now) or fallback_latest(races)
        if not ev:
            return None
        start_url = ev.get("leaderboard") or ""
    elif kind == "boss":
        bosses = await fetch_body(URL_BOSSES)
        ev = pick_active(bosses, now) or fallback_latest(bosses)
        if not ev:
            return None
        elite = variant == "elite"
        url_key = "leaderboard_elite_players_1" if elite else "leaderboard_standard_players_1"
        start_url = ev.get(url_key) or ""
    else:  # ct
        cts = await fetch_body(URL_CT)
        ev = pick_active(cts, now) or fallback_latest(cts)
        if not ev:
            return None
        team = variant == "team"
        url_key = "leaderboard_team" if team else "leaderboard_player"
        start_url = ev.get(url_key) or ""
    if not start_url:
        return None
    entries = await fetch_leaderboard_page(start_url, page)
    if 0 <= idx < len(entries):
        return entries[idx]
    return None


# ---------------- 图片卡片（weasyprint 渲染管线 + 内容哈希缓存） ----------------

CARD_W = 900

_SEC_COLOR = {"race": "#4a90d9", "boss": "#d95c5c", "ct": "#d9a94e"}
_MEDAL_COLOR = {1: "#e8b339", 2: "#a0a4ad", 3: "#cd8c52"}

_bg_cache: dict[str, str] = {}


def _bg_data_url() -> str:
    """256px 竖向渐变条，CSS 拉伸铺满整页；避免逐像素生成整页大图（渲染慢的主因之一）。"""
    hit = _bg_cache.get("bg")
    if hit:
        return hit
    from PIL import Image

    top, bottom = (249, 248, 250), (243, 241, 246)
    strip = Image.new("RGB", (1, 256))
    for y in range(256):
        t = y / 255
        strip.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    import io

    buf = io.BytesIO()
    strip.save(buf, "PNG")
    url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    _bg_cache["bg"] = url
    return url


def _esc(value) -> str:
    return html_mod.escape(str(value), quote=True)


def _state_of(ev: dict, now_ms: int) -> str:
    start, end = int(ev.get("start") or 0), int(ev.get("end") or 0)
    if now_ms < start:
        return "up"
    if now_ms < end:
        return "on"
    return "off"


def _pick_section(items: list, now_ms: int):
    return pick_active(items, now_ms) or pick_next(items, now_ms) or fallback_latest(items)


def _shell(body: str, h: int) -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
@page {{ size: {CARD_W}px {h}px; margin: 0; }}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ width: {CARD_W}px; height: {h}px; font-family: "WenQuanYi Micro Hei", "Noto Sans CJK SC", sans-serif;
       background-image: url({_bg_data_url()}); background-size: 100% 100%; }}
.card {{ padding: 26px 40px; }}
.panel {{ background: #ffffff; border-radius: 26px; margin-top: 16px; padding: 14px 36px;
          border: 1px solid #eceaf0; }}
.panel:first-child {{ margin-top: 0; }}
.phead {{ display: table; width: 100%; }}
.ptext {{ display: table-cell; vertical-align: middle; }}
.pimg {{ display: table-cell; vertical-align: middle; width: 200px; text-align: right; }}
.pimg img {{ max-width: 185px; max-height: 118px; border-radius: 12px; }}
.ptitle {{ font-size: 27px; font-weight: 700; color: #221f2e; padding: 8px 0 2px; }}
.big {{ font-size: 31px; font-weight: 700; color: #221f2e; padding: 6px 0 2px; word-break: break-all; }}
.sub {{ font-size: 22px; color: #8e8a96; padding-top: 2px; }}
.li {{ font-size: 23px; color: #55515f; padding: 11px 0; border-bottom: 1px solid #f0eef3; line-height: 1.45; word-break: break-all; }}
.li:last-child {{ border-bottom: none; }}
.hl {{ font-weight: 600; }}
.row {{ display: table; width: 100%; padding: 17px 0; border-bottom: 1px solid #efedf1; }}
.row:last-child {{ border-bottom: none; }}
.rank {{ display: table-cell; vertical-align: middle; width: 64px; font-size: 26px; font-weight: 700; color: #c9c5cf; }}
.name {{ display: table-cell; vertical-align: middle; padding: 0 18px; font-size: 27px; font-weight: 600;
         color: #221f2e; word-break: break-all; }}
.score {{ display: table-cell; vertical-align: middle; text-align: right; width: 200px;
          font-size: 28px; font-weight: 700; color: #221f2e; }}
.date {{ display: table-cell; vertical-align: middle; text-align: right; width: 200px;
         font-size: 20px; color: #8e8a96; }}
.empty {{ text-align: center; font-size: 24px; color: #a5a1ab; padding: 26px 0; }}
.evname {{ font-size: 36px; font-weight: 700; color: #221f2e; word-break: break-all; }}
.evpre {{ font-size: 22px; font-weight: 600; color: #7b68c8; padding-bottom: 3px; }}
.evsub {{ font-size: 24px; color: #8e8a96; padding-top: 4px; }}
.ticon {{ display: table-cell; width: 110px; vertical-align: middle; }}
.ticon img {{ width: 92px; border-radius: 14px; }}
.mapcell {{ display: table-cell; width: 420px; vertical-align: top; }}
.mapcell img {{ width: 400px; border-radius: 14px; border: 3px solid #d9d4e2; }}
.nomap {{ width: 400px; height: 200px; line-height: 200px; text-align: center; font-size: 60px;
          background: #f1eff3; border-radius: 14px; }}
.stats {{ display: table-cell; vertical-align: top; padding-left: 20px; }}
.scol {{ display: table-cell; width: 50%; vertical-align: top; }}
.st {{ font-size: 21px; color: #55515f; padding: 8px 0 8px 14px; white-space: nowrap; }}
.st b {{ color: #221f2e; font-size: 23px; }}
.duo {{ display: table; width: 100%; }}
.dcell {{ display: table-cell; width: 50%; vertical-align: top; }}
.dcell .panel {{ margin-left: 0; margin-right: 0; }}
.dcell:first-child .panel {{ margin-right: 8px; }}
.dcell:last-child .panel {{ margin-left: 8px; }}
.mkgrid {{ text-align: center; padding: 4px 0 0; }}
.mkwrap {{ display: inline-block; margin: 4px 3px; vertical-align: top; }}
.mk {{ position: relative; width: 96px; }}
.mk img {{ width: 94px; height: 94px; border-radius: 14px; border: 2px solid #e8e5ee; }}
.mk .nm {{ font-size: 16px; color: #55515f; padding-top: 3px; text-align: center;
           white-space: nowrap; overflow: hidden; }}
.mk .bd {{ position: absolute; top: 0; left: 0; width: 94px; height: 94px;
           text-align: center; background: rgba(215, 52, 52, .50); border-radius: 14px; }}
.mk .bd span {{ display: block; margin-top: 14px; font-size: 56px; font-weight: 700;
                line-height: 66px; color: #ffffff; }}
.mk .lim {{ position: absolute; top: 3px; right: 3px; background: #f5b800; color: #ffffff;
            font-size: 20px; font-weight: 700; border-radius: 9px; padding: 1px 8px; }}
.mk .pth {{ position: absolute; top: 60px; left: 0; width: 94px; text-align: center; }}
.mk .pth span {{ background: rgba(47, 111, 217, .92); color: #ffffff; font-size: 19px;
                 font-weight: 700; border-radius: 8px; padding: 2px 8px; }}
.mk.txt {{ width: 96px; min-height: 94px; background: #f1eff3; border-radius: 14px;
           font-size: 19px; color: #55515f; text-align: center; padding: 24px 2px;
           box-sizing: border-box; }}
.mktxt {{ font-size: 15px; color: #8e8a96; text-align: center; padding-top: 2px; }}
.pimg.lg {{ width: 250px; }}
.pimg.lg img {{ width: 235px; max-height: 235px; }}
.mrow {{ display: table; width: 100%; padding: 12px 0; border-bottom: 1px solid #efedf1; }}
.mrow:last-child {{ border-bottom: none; }}
.mrow .rank {{ display: table-cell; vertical-align: middle; width: 56px; font-size: 24px;
               font-weight: 700; color: #c9c5cf; }}
.mthumb {{ display: table-cell; vertical-align: middle; width: 150px; }}
.mthumb img {{ width: 138px; border-radius: 10px; border: 2px solid #e8e5ee; }}
.nomap-s {{ width: 138px; height: 92px; line-height: 88px; text-align: center; font-size: 40px;
            background: #f1eff3; border-radius: 10px; }}
.mname {{ display: table-cell; vertical-align: middle; padding-left: 18px; font-size: 25px;
          font-weight: 600; color: #221f2e; word-break: break-all; text-align: left; }}
.msub {{ font-size: 18px; color: #8e8a96; font-weight: 400; padding-top: 4px; }}
.pbanner img {{ width: 100%; border-radius: 14px; }}
.pavatar {{ display: table-cell; width: 140px; vertical-align: middle; }}
.pavatar img {{ width: 112px; border-radius: 16px; }}
.bimg {{ display: table-cell; width: 180px; height: 112px; vertical-align: middle; text-align: center; }}
.bimg img {{ max-width: 160px; max-height: 106px; border-radius: 12px; }}
.bimg-ph {{ width: 158px; height: 104px; line-height: 100px; text-align: center; font-size: 54px;
            background: #f1eff3; border-radius: 12px; }}
.btext {{ display: table-cell; vertical-align: middle; padding-left: 20px; }}
.brow {{ display: table; width: 100%; }}
.bname {{ display: table-cell; font-size: 30px; font-weight: 700; color: #221f2e;
          word-break: break-all; }}
.badge {{ display: table-cell; text-align: right; vertical-align: middle; width: 128px; }}
.badge span {{ display: inline-block; padding: 4px 16px; border-radius: 18px; font-size: 20px;
               font-weight: 600; color: #ffffff; }}
.st-on {{ background: #2f9e63; }}
.st-up {{ background: #e08a2e; }}
.st-off {{ background: #a5a1ab; }}
.hrow {{ padding: 15px 0 13px; border-bottom: 1px dashed #e0dce8; }}
.hrow:last-child {{ border-bottom: none; }}
.chip {{ display: inline-block; background: #7b68c8; color: #ffffff; font-size: 21px;
         font-weight: 700; padding: 6px 16px; border-radius: 18px; word-break: break-all;
         letter-spacing: 0.5px; }}
.hdesc {{ font-size: 21px; color: #6f6b78; padding-top: 9px; }}
.bdates {{ font-size: 21px; color: #8e8a96; padding-top: 5px; }}
.bscore {{ font-size: 22px; color: #55515f; padding-top: 7px; }}
.bscore b {{ color: #221f2e; }}
</style></head>
<body><div class="card">
{body}
</div></body></html>"""


ODYSSEY_CARD_W = 800


def _odyssey_shell(body: str, h: int, theme: str = "teal") -> str:
    """通用外壳（teal 青色为多数活动卡使用）；远征难度卡传 theme="tan" 复刻游戏内米色纸张。"""
    page_bg = "#d5c295" if theme == "tan" else "#0b7180"
    text_color = "#4a3c28" if theme == "tan" else "#ffffff"
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
@page {{ size: {ODYSSEY_CARD_W}px {h}px; margin: 0; background: {page_bg}; }}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html, body {{ width: {ODYSSEY_CARD_W}px; height: {h}px; color: {text_color};
        font-family: "WenQuanYi Micro Hei", "Noto Sans CJK SC", sans-serif;
        background: {page_bg}; margin: 0; padding: 0; overflow: hidden; }}
.ody-page {{ width: {ODYSSEY_CARD_W}px; height: {h}px; padding: 0;
             background: transparent; box-sizing: border-box; }}
.ody-paper {{ width: {ODYSSEY_CARD_W}px; height: {h}px; padding: 0 0 8px; overflow: hidden; margin: 0;
              background: {page_bg}; border: 0; border-radius: 3px;
              box-sizing: border-box;
              box-shadow: 0 2px 0 rgba(0,0,0,.18), inset 0 0 0 1px #b99470; }}

/* ===== 顶部三条蓝丝带（带切口 + 阴影 + 图标） ===== */
.ody-ribbons {{ display: table; width: 100%; height: 38px; table-layout: fixed; text-align: center; }}
.ody-ribbon-cell {{ display: table-cell; width: 33.333%; padding: 0 5px; vertical-align: middle; }}
.ody-ribbon {{ position: relative; display: inline-block; height: 31px; padding: 4px 18px; color: #ffffff;
               font-size: 15px; line-height: 21px; font-weight: 900; white-space: nowrap;
               text-shadow: 0 2px 0 #075b8b; letter-spacing: 0.5px;
               background: linear-gradient(180deg, #46c8f1 0%, #129ed0 56%, #087eaf 100%);
               border: 2px solid #076b99; border-radius: 2px;
               box-shadow: inset 0 1px 0 rgba(255,255,255,.75), 0 2px 0 rgba(0,59,79,.28); }}
.ody-ribbon::before, .ody-ribbon::after {{ content: ""; position: absolute; top: 100%; width: 0; height: 0;
                                          border-style: solid; }}
.ody-ribbon::before {{ left: -2px; border-width: 0 0 6px 6px; border-color: transparent transparent #053a55 transparent; }}
.ody-ribbon::after  {{ right: -2px; border-width: 6px 6px 0 0; border-color: #053a55 transparent transparent transparent; }}
.ody-ribbon-icon {{ display: inline-block; width: 18px; height: 18px; margin-right: 4px; vertical-align: -4px;
                    object-fit: contain; filter: drop-shadow(0 1px 0 #075b8b); }}

/* 面板通用：棕底圆角 + 内/外阴影 */
.ody-panel {{ position: relative; background: #a58a6e; border: 1px solid rgba(81,52,30,.18); border-radius: 9px;
              box-shadow: inset 0 1px 0 rgba(255,244,222,.30), 0 2px 0 rgba(84,53,30,.20); padding-top: 22px; }}
/* 面板顶部蓝丝带标题（默认队伍 / 奖励 / 岛屿规则 等） */
.ody-panel-title, .ody-section-banner {{ position: absolute; top: -10px; left: 12px; height: 26px; padding: 0 22px;
                                          color: #ffffff; font-size: 14px; line-height: 26px; font-weight: 900;
                                          text-shadow: 0 1px 0 #075b8b; letter-spacing: 0.5px;
                                          background: linear-gradient(180deg, #46c8f1 0%, #129ed0 56%, #087eaf 100%);
                                          border: 2px solid #076b99; border-radius: 2px;
                                          box-shadow: inset 0 1px 0 rgba(255,255,255,.75), 0 2px 0 rgba(0,59,79,.28); }}
.ody-panel-title::before, .ody-panel-title::after,
.ody-section-banner::before, .ody-section-banner::after {{ content: ""; position: absolute; top: 100%; width: 0; height: 0;
                                                          border-style: solid; }}
.ody-panel-title::before, .ody-section-banner::before {{ left: -2px; border-width: 0 0 6px 6px;
                                                          border-color: transparent transparent #053a55 transparent; }}
.ody-panel-title::after,  .ody-section-banner::after  {{ right: -2px; border-width: 6px 6px 0 0;
                                                          border-color: #053a55 transparent transparent transparent; }}
.ody-paper > .ody-section-banner:first-child {{ margin-top: 0; }}
.ody-section-banner {{ position: relative; top: 0; left: 0; display: block; width: max-content; margin: 14px auto 6px;
                        padding: 0 24px; font-size: 15px; height: 30px; line-height: 30px; }}

/* 活动名行 */
.ody-event {{ height: 24px; padding: 6px 16px 0; color: #5a4530; font-size: 15px; line-height: 20px;
              font-weight: 900; text-align: center; white-space: nowrap; overflow: hidden; }}
.ody-event-desc {{ padding: 0 24px 2px; color: #70543c; font-size: 12px; line-height: 16px; font-weight: 700;
                   text-align: center; font-style: italic; max-height: 32px; overflow: hidden; }}
.ody-extreme-badge {{ margin: 2px auto 0; width: max-content; padding: 1px 12px; color: #ffffff;
                      font-size: 12px; line-height: 18px; font-weight: 900; letter-spacing: 0.5px;
                      background: linear-gradient(180deg, #ff6b3a 0%, #d63a14 56%, #a02800 100%);
                      border: 2px solid #7a1a00; border-radius: 10px;
                      box-shadow: inset 0 1px 0 rgba(255,255,255,.5), 0 1px 0 rgba(0,0,0,.25); }}

/* 上下两栏：队伍 + 奖励 */
.ody-top-grid {{ display: table; width: 100%; padding: 6px 13px 0; table-layout: fixed; }}
.ody-top-cell {{ display: table-cell; vertical-align: top; }}
.ody-top-cell.crew {{ width: 70%; padding-right: 8px; }}
.ody-top-cell.reward {{ width: 30%; padding-left: 8px; }}

/* ===== 默认队伍 ===== */
.ody-crew-panel {{ min-height: 204px; padding: 8px 8px 8px; }}
.ody-crew-body {{ display: flex; align-items: flex-start; gap: 10px; width: 100%; min-height: 169px; }}
.ody-crew-hero {{ flex: none; width: 96px; padding-top: 8px; text-align: center; }}
.ody-crew-grid-cell {{ flex: 1; min-width: 0; padding: 14px 0 0 0; white-space: nowrap; }}
.ody-default-grid {{ text-align: left; white-space: nowrap; }}
.ody-default-grid .ody-unit-wrap {{ width: 46px; height: 58px; }}
.ody-default-grid .ody-unit-card {{ width: 44px; height: 54px; border-radius: 9px; }}

/* ===== 塔卡（通用：可用 + 默认队伍 共用） ===== */
.ody-unit-wrap {{ display: inline-block; width: 55px; height: 67px; margin: 0 1px 3px; vertical-align: top; }}
.ody-unit-card {{ position: relative; width: 55px; height: 64px; overflow: hidden; border: 2px solid rgba(0,0,0,.35);
                  border-radius: 11px;
                  background: radial-gradient(circle at 50% 30%, #f5a82a, #dc7410 75%);
                  box-shadow: inset 0 1px 0 rgba(255,255,255,.65), 0 2px 0 rgba(0,0,0,.25); }}
/* 分类底色（远征可选区按塔分类上色） */
.ody-unit-card.cat-primary  {{ background: linear-gradient(180deg, #69b1ec 0%, #3a7ad6 75%, #1a4d8c 100%);
                                border-color: #143a6e; }}
.ody-unit-card.cat-military {{ background: linear-gradient(180deg, #6cd083 0%, #3aa05a 75%, #1f6a3b 100%);
                                border-color: #144a28; }}
.ody-unit-card.cat-magic    {{ background: linear-gradient(180deg, #c684e0 0%, #9a4cc7 75%, #5e2587 100%);
                                border-color: #3d1660; }}
.ody-unit-card.cat-support  {{ background: linear-gradient(180deg, #f0b265 0%, #e08a2a 75%, #9a5710 100%);
                                border-color: #6b3a0a; }}
.ody-unit-card.cat-hero     {{ background: linear-gradient(180deg, #ffe94d 0%, #ffc516 78%, #dd9300 100%);
                                border-color: #8a5a00; }}
.ody-unit-card.hero {{ background: linear-gradient(180deg, #ffe94d 0%, #ffc516 78%, #dd9300 100%); }}
.ody-unit-card img {{ display: block; width: 51px; height: 51px; margin: 0 auto; object-fit: contain;
                       filter: drop-shadow(0 1px 0 rgba(0,0,0,.15)); }}
.ody-unit-card.big {{ width: 88px; height: 108px; border-radius: 16px;
                       background: linear-gradient(180deg, #f0a24a 0%, #d97b1c 75%, #b25e10 100%); }}
.ody-unit-card.big img {{ width: 88px; height: 88px; }}
/* 数量徽章（默认队伍用：右上） */
.ody-unit-quantity {{ position: absolute; right: 1px; top: 1px; min-width: 25px; padding: 1px 3px;
                      color: #ffffff; background: #1879bf; border: 2px solid #e8f7ff; border-radius: 12px;
                      font-size: 10px; line-height: 12px; font-weight: 900; text-align: center;
                      text-shadow: 0 1px 0 #144e75; }}
/* 数量徽章（可用区用：左上） */
.ody-unit-quantity.left {{ right: auto; left: 1px; }}
/* 升级上限（可用塔卡片底部橙字） */
.ody-unit-caps {{ position: absolute; left: 0; right: 0; bottom: 1px; text-align: center;
                  color: #ffb84d; font-size: 11px; line-height: 13px; font-weight: 900;
                  text-shadow: 0 1px 0 #2a1500, 0 0 2px rgba(0,0,0,.6); letter-spacing: 0.5px; }}
.ody-unit-fallback {{ color: #fff; font-size: 9px; line-height: 11px; font-weight: 900; text-align: center;
                      padding: 14px 2px; word-break: break-all; text-shadow: 0 1px 0 #000; }}

/* ===== 奖励面板 ===== */
.ody-reward-panel {{ min-height: 204px; padding: 16px 8px 8px; }}
.ody-reward-grid {{ display: table; width: 100%; height: 150px; table-layout: fixed; text-align: center; }}
.ody-reward-cell {{ display: table-cell; width: 33.333%; vertical-align: middle; }}
.ody-reward-icon {{ display: block; width: 64px; height: 64px; margin: 0 auto 4px; object-fit: contain;
                    filter: drop-shadow(0 2px 0 rgba(0,0,0,.25)); }}
.ody-reward-emoji {{ display: block; height: 64px; font-size: 42px; line-height: 64px; }}
.ody-reward-value {{ color: #ffffff; font-size: 15px; line-height: 19px; font-weight: 900;
                     text-shadow: 0 2px 0 #5d4631; }}

/* ===== 可用英雄/猴子/力量 ===== */
.ody-available {{ display: table; width: 100%; padding: 12px 7px 0; table-layout: fixed;
                  border-spacing: 7px 0; }}
.ody-av-panel {{ display: table-cell; vertical-align: top; padding: 14px 7px 8px; }}
.ody-av-panel.heroes {{ width: 21%; }}
.ody-av-panel.towers {{ width: 47%; }}
.ody-av-panel.powers {{ width: 32%; }}
.ody-av-title {{ height: 22px; color: #ffffff; font-size: 15px; line-height: 22px; font-weight: 900;
                 text-shadow: 0 1px 0 #58432f; text-align: center; white-space: nowrap; }}
.ody-av-title-dark {{ color: #3d2a1a; text-shadow: 0 1px 0 rgba(255,244,222,.4); margin: 2px 0 6px; }}

/* 默认队伍里的小塔卡（不论塔分类）都用金色底，与游戏内一致 */
.ody-crew-panel .ody-unit-card,
.ody-crew-panel .ody-unit-card.cat-primary,
.ody-crew-panel .ody-unit-card.cat-military,
.ody-crew-panel .ody-unit-card.cat-magic,
.ody-crew-panel .ody-unit-card.cat-support {{
  background: radial-gradient(circle at 50% 30%, #f5a82a 0%, #dc7410 75%);
  border-color: #b4740d;
}}

/* 英雄网格 */
.ody-hero-grid {{ text-align: center; padding-top: 4px; }}
.ody-hero-grid .ody-unit-wrap {{ width: 62px; height: 84px; margin: 0 1px 6px; }}
.ody-hero-grid .ody-unit-card {{ width: 60px; height: 76px; border-radius: 8px; }}
.ody-hero-grid .ody-unit-card img {{ width: 56px; height: 58px; }}

/* 猴子网格（含升级上限） */
.ody-tower-grid {{ text-align: center; padding-top: 4px; }}
.ody-tower-grid .ody-unit-wrap {{ width: 67px; height: 84px; margin: 0 1px 4px; }}
.ody-tower-grid .ody-unit-card {{ width: 65px; height: 78px; border-radius: 6px; }}
.ody-tower-grid .ody-unit-card img {{ width: 63px; height: 60px; }}

/* ===== 力量（六边形 + 蓝色圆形计数） ===== */
.ody-power-grid {{ text-align: center; padding-top: 4px; }}
.ody-power-wrap {{ display: inline-block; width: 46px; height: 50px; margin: 0 0 4px; vertical-align: top; }}
.ody-power-tile {{ position: relative; width: 42px; height: 42px; margin: 3px auto 0; overflow: hidden;
                   background: linear-gradient(145deg, #ffd64a 0%, #f6a70d 58%, #d86b05 100%);
                   border: 2px solid #9b5b0b;
                   /* 六边形切口 */
                   clip-path: polygon(50% 0, 100% 25%, 100% 75%, 50% 100%, 0 75%, 0 25%);
                   box-shadow: inset 0 2px 0 rgba(255,255,255,.55), 0 2px 0 rgba(78,49,15,.28); }}
.ody-power-tile img {{ display: block; width: 32px; height: 32px; margin: 4px auto 0; object-fit: contain; }}
.ody-power-count {{ position: absolute; left: -5px; top: -5px; width: 18px; height: 18px; padding-top: 2px;
                    color: #ffffff; background: #1596d2; border: 2px solid #e7f8ff; border-radius: 50%;
                    font-size: 9px; line-height: 12px; font-weight: 900; text-shadow: 0 1px 0 #135d8b;
                    z-index: 2; }}
.ody-power-fallback {{ padding-top: 19px; color: #fff; font-size: 9px; line-height: 11px; font-weight: 900;
                       text-shadow: 0 1px 0 #5b3212; }}

/* ===== 岛屿规则 ===== */
.ody-maps {{ padding: 4px 13px 0; }}
.ody-map-row {{ display: table; width: 100%; min-height: 118px; margin-bottom: 8px; padding: 6px 8px;
                table-layout: fixed; background: #aa937b; border-radius: 8px;
                box-shadow: inset 0 1px 0 rgba(255,244,222,.24), 0 2px 0 rgba(84,53,30,.20); }}
.ody-map-img-cell {{ position: relative; display: table-cell; width: 178px; vertical-align: middle; }}
.ody-map-img {{ display: block; width: 168px; height: 105px; object-fit: cover; border: 3px solid #f8b900;
                border-radius: 7px; box-shadow: 0 1px 0 #70430f; }}
.ody-map-empty {{ width: 168px; height: 105px; padding-top: 38px; color: #684d37; text-align: center;
                  background: #c0aa91; border: 3px solid #f8b900; border-radius: 7px; font-size: 13px; font-weight: 900; }}
.ody-map-overlay {{ position: absolute; top: 6px; left: 50%; transform: translateX(-50%);
                    color: #ffffff; font-size: 14px; line-height: 18px; font-weight: 900;
                    text-shadow: 0 1px 0 #5a3a14; letter-spacing: 1px; }}
.ody-map-info {{ display: table-cell; vertical-align: middle; padding: 0 8px 0 6px; }}
.ody-map-meta {{ display: table; width: 100%; table-layout: fixed; }}
.ody-map-meta-item {{ display: table-cell; vertical-align: middle; width: 33.333%; color: #ffffff;
                      font-size: 14px; line-height: 20px; font-weight: 900;
                      text-shadow: 0 1px 0 #68513d; white-space: nowrap; padding: 4px 0; }}
.ody-mini-icon {{ display: inline-block; width: 22px; height: 22px; margin-right: 4px; vertical-align: -5px;
                  object-fit: contain; filter: drop-shadow(0 1px 0 rgba(0,0,0,.35)); }}
.ody-coin {{ color: #ffd329; font-size: 18px; vertical-align: -1px; text-shadow: 0 1px 0 #7b5311; }}
.ody-play {{ display: inline-block; width: 22px; height: 22px; margin-right: 4px; color: #ffffff;
             background: #1ec84d; border: 2px solid #087d2c; border-radius: 50%; font-size: 13px;
             line-height: 18px; text-align: center; text-shadow: none; vertical-align: -5px; }}
.ody-diff {{ display: inline-block; width: 22px; height: 22px; margin-right: 4px; color: #ffffff;
             background: #27a8d4; border: 2px solid #08708f; border-radius: 50%; font-size: 11px;
             line-height: 18px; text-align: center; text-shadow: none; vertical-align: -5px; }}
.ody-map-rule {{ padding-top: 8px; color: #ffffff; font-size: 15px; line-height: 20px; font-weight: 900;
                 text-align: center; text-shadow: 0 1px 0 #68513d; }}
.ody-map-sub {{ color: #f4dfc2; font-size: 11px; line-height: 15px; font-weight: 700; }}
/* 活动总览 / 排行榜 / 自制地图 复用游戏风格 */
.ov-panel {{ min-height: 120px; padding: 18px 10px 10px; margin-bottom: 8px; }}
.ov-row {{ display: table; width: 100%; min-height: 110px; table-layout: fixed; }}
.ov-img-cell {{ display: table-cell; width: 170px; vertical-align: middle; }}
.ov-img {{ display: block; width: 160px; height: 100px; object-fit: cover; border: 3px solid #f8b900; border-radius: 7px; box-shadow: 0 1px 0 #70430f; }}
.ov-img-boss {{ display: block; width: 160px; height: 100px; object-fit: contain; background: #ffffff; border: 3px solid #f8b900; border-radius: 7px; box-shadow: 0 1px 0 #70430f; }}
.ov-img-ph {{ width: 160px; height: 100px; line-height: 100px; text-align: center; font-size: 36px; background: #c0aa91; border: 3px solid #f8b900; border-radius: 7px; }}
.ov-info {{ display: table-cell; vertical-align: middle; padding: 0 8px 0 6px; }}
.ov-name {{ color: #ffffff; font-size: 17px; line-height: 21px; font-weight: 900; text-shadow: 0 1px 0 #68513d; word-break: break-all; }}
.ov-badge {{ display: inline-block; margin-left: 6px; padding: 1px 8px; border-radius: 10px; font-size: 11px; line-height: 15px; font-weight: 900; color: #ffffff; vertical-align: middle; text-shadow: 0 1px 0 rgba(0,0,0,.25); }}
.ov-dates {{ color: #f4dfc2; font-size: 12px; line-height: 16px; font-weight: 700; padding-top: 3px; }}
.ov-score {{ color: #ffffff; font-size: 13px; line-height: 17px; font-weight: 900; text-shadow: 0 1px 0 #68513d; padding-top: 3px; }}
.ov-score b {{ color: #ffd700; text-shadow: 0 1px 0 #5a3a14; }}
.lb-header {{ text-align: center; padding: 6px 0 4px; }}
.lb-title {{ color: #5a4530; font-size: 16px; line-height: 20px; font-weight: 900; word-break: break-all; }}
.lb-subtitle {{ color: #70543c; font-size: 12px; line-height: 16px; font-weight: 700; padding-top: 2px; }}
.lb-panel {{ min-height: 80px; padding: 14px 8px 8px; }}
.lb-row {{ display: table; width: 100%; min-height: 48px; margin-bottom: 6px; padding: 6px 8px; table-layout: fixed; background: #aa937b; border-radius: 6px; box-shadow: inset 0 1px 0 rgba(255,244,222,.24), 0 1px 0 rgba(84,53,30,.15); }}
.lb-rank {{ display: table-cell; width: 48px; vertical-align: middle; font-size: 18px; font-weight: 900; text-align: center; }}
.lb-name {{ display: table-cell; vertical-align: middle; padding: 0 8px; font-size: 14px; font-weight: 700; color: #ffffff; text-shadow: 0 1px 0 #68513d; word-break: break-all; }}
.lb-score {{ display: table-cell; width: 140px; vertical-align: middle; text-align: right; font-size: 14px; font-weight: 900; color: #ffd700; text-shadow: 0 1px 0 #5a3a14; white-space: nowrap; }}
.lb-empty {{ text-align: center; color: #684d37; font-size: 14px; font-weight: 900; padding: 18px 0; }}
.map-panel {{ min-height: 80px; padding: 14px 8px 8px; }}

/* ===== 活动总览 v2：图标列表样式（与原 ov-panel 风格分离，避免影响排行榜/远征） ===== */
.ev-row {{ display: table; width: 100%; min-height: 56px; padding: 6px 0;
          border-bottom: 1px solid rgba(255, 255, 255, 0.18); table-layout: fixed; }}
.ev-icon-cell {{ display: table-cell; width: 56px; vertical-align: middle; text-align: center; }}
.ev-icon {{ width: 44px; height: 44px; border-radius: 50%; object-fit: cover;
           border: 2px solid #f8b900; background: #1a3354; vertical-align: middle; }}
.ev-name {{ display: table-cell; vertical-align: middle; padding: 0 10px;
            color: #fff7d4; font-size: 15px; font-weight: 900;
            text-shadow: 0 1px 2px rgba(0, 0, 0, 0.85);
            word-break: break-all; }}
.ev-dates {{ display: table-cell; vertical-align: middle; width: 220px;
            color: #ffe66b; font-size: 12px; line-height: 1.4; font-weight: 700; padding: 0 8px;
            text-shadow: 0 1px 1px rgba(0, 0, 0, 0.7); }}
.ev-dates div {{ white-space: nowrap; }}
.ev-right {{ display: table-cell; vertical-align: middle; width: 130px; text-align: right;
             font-size: 14px; font-weight: 900; letter-spacing: 0.5px; padding: 0 14px 0 0;
             text-shadow: 0 1px 2px rgba(0, 0, 0, 0.85); }}
.ev-right-on {{ color: #2ee66b; text-shadow: 0 1px 1px rgba(0, 0, 0, 0.9); }}
.ev-right-up {{ color: #ffd84d; text-shadow: 0 1px 1px rgba(0, 0, 0, 0.9); }}
.ev-right-off {{ color: #c9c2b0; }}
.ev-empty {{ color: #fff7d4; font-size: 12px; font-weight: 700; padding: 14px 18px;
            background: rgba(0, 0, 0, 0.25); border-radius: 4px; line-height: 1.5;
            text-shadow: 0 1px 1px rgba(0, 0, 0, 0.8); }}
.ev-spacer {{ height: 8px; }}
</style></head>
<body><div class="ody-page"><div class="ody-paper">{body}</div></div></body></html>"""


RACE_CARD_W = 836


def _race_shell(body: str, h: int) -> str:
    """截图风格的 BTD6 挑战详情页；仅规则/竞速卡使用，避免影响其他卡片。"""
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
@page {{ size: {RACE_CARD_W}px {h}px; margin: 0; background: #699bd9; }}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ width: {RACE_CARD_W}px; height: {h}px; color: #ffffff;
        font-family: "WenQuanYi Micro Hei", "Noto Sans CJK SC", sans-serif;
       background-color: #699bd9; }}
.race-page {{ width: {RACE_CARD_W}px; min-height: {h}px; }}
.race-frame {{ margin: 0; background: #699bd9; border: 0; border-radius: 0; overflow: hidden;
               box-shadow: inset 0 2px 0 rgba(255,255,255,.18); }}
.race-topbar {{ height: 111px; background: linear-gradient(180deg, #25a9c5 0%, #138aae 58%, #0e789f 100%);
                box-shadow: inset 0 2px 0 rgba(255,255,255,.20), inset 0 -3px 0 rgba(7,70,105,.28); }}
.race-head {{ display: table; width: 100%; height: 111px; table-layout: fixed; }}
.race-emblem-cell {{ display: table-cell; width: 92px; vertical-align: middle; padding-left: 7px; }}
.race-emblem {{ width: 82px; height: 82px; border-radius: 50%; border: 3px solid #a96b20;
                background: #27476d; overflow: hidden;
                box-shadow: inset 0 2px 0 rgba(255,255,255,.32), 0 3px 0 #794b17, 0 5px 8px rgba(0,0,0,.28); }}
.race-emblem img {{ display: block; width: 100%; height: 100%; object-fit: contain; object-position: center; }}
.race-emblem-fallback {{ width: 100%; height: 100%; text-align: center; padding-top: 12px;
                          color: #ffd400; font-size: 37px; line-height: 42px; }}
.race-title-cell {{ display: table-cell; vertical-align: middle; text-align: center; }}
.race-title {{ color: #ffffff; font-size: 22px; line-height: 26px; font-weight: 900;
               font-family: "WenQuanYi Micro Hei", "Noto Sans CJK SC", "DejaVu Sans", sans-serif;
               -webkit-text-stroke: .6px #111;
               text-shadow: 1px 1px 0 #0a456b, -1px 1px 0 #0a456b, 0 2px 0 #0a456b,
                            0 4px 0 #0a456b, 0 6px 7px rgba(0,0,0,.40); }}
.race-subtitle {{ color: #ffffff; font-size: 16px; line-height: 22px; font-weight: 900;
                   font-family: "WenQuanYi Micro Hei", "Noto Sans CJK SC", "DejaVu Sans", sans-serif;
                   -webkit-text-stroke: .5px #111;
                   text-shadow: 1px 1px 0 #0a456b, -1px 1px 0 #0a456b, 0 2px 0 #0a456b,
                                0 4px 5px rgba(0,0,0,.34); }}
.race-time {{ color: #ffe66b; font-size: 13px; line-height: 19px; font-weight: 900;
               font-family: "WenQuanYi Micro Hei", "Noto Sans CJK SC", sans-serif;
               -webkit-text-stroke: .25px #111;
               text-shadow: 1px 1px 0 #7a4100, -1px 1px 0 #7a4100, 0 2px 0 #7a4100,
                            0 4px 5px rgba(0,0,0,.35); }}
.race-content {{ padding: 8px 18px 20px; }}
.race-layout {{ display: table; width: 100%; height: 248px; table-layout: fixed; }}
.race-map {{ display: table-cell; width: 368px; height: 248px; vertical-align: top; }}
.race-map img {{ display: block; width: 368px; height: 248px; object-fit: cover; border: 2px solid #d6e1ee;
                 border-radius: 4px; }}
.race-map-empty {{ width: 368px; height: 248px; border: 2px solid #d6e1ee; border-radius: 4px;
                   background: #537faf; text-align: center; padding-top: 90px; font-size: 48px; }}
.race-stats-cell {{ display: table-cell; vertical-align: top; padding-left: 26px; }}
.race-stats {{ height: 187px; margin-top: 31px; padding: 10px 8px; border-radius: 6px;
               background: linear-gradient(180deg, #527fb5 0%, #436fa7 52%, #3a6397 100%);
               border: 1px solid rgba(255,255,255,.28); border-top-width: 2px; border-bottom: 3px solid #315a8d;
               box-shadow: inset 0 2px 0 rgba(255,255,255,.22), inset 0 -3px 0 rgba(15,48,84,.26),
                           0 4px 0 #294f7e, 0 7px 9px rgba(0,0,0,.22); }}
.race-stat-col {{ display: table-cell; width: 50%; vertical-align: top; }}
.race-stat {{ display: table; width: 100%; height: 42px; table-layout: fixed; }}
.race-stat-icon-cell {{ display: table-cell; width: 38px; vertical-align: middle; text-align: center; }}
.race-stat-icon {{ width: 33px; height: 33px; object-fit: contain; }}
.race-stat-fallback {{ display: inline-block; width: 31px; height: 31px; font-size: 27px; line-height: 31px;
                       text-align: center; }}
.race-stat-copy {{ display: table-cell; vertical-align: middle; padding-left: 2px; }}
.race-stat-label {{ color: #ffffff; font-size: 10px; line-height: 11px; font-weight: 900; white-space: nowrap;
                     text-shadow: 1px 1px 0 #163e68, -1px 1px 0 #163e68, 0 2px 3px rgba(0,0,0,.32); }}
.race-stat-value {{ color: #ffffff; font-size: 12px; line-height: 13px; font-weight: 900; white-space: nowrap;
                     text-shadow: 1px 1px 0 #163e68, -1px 1px 0 #163e68, 0 2px 3px rgba(0,0,0,.32); }}
.race-monkey-section {{ margin-top: 12px; padding: 8px 10px 9px; border-radius: 8px;
                        background: linear-gradient(180deg, #3c78b3 0%, #326aa4 55%, #2a5c91 100%);
                        border: 1px solid rgba(255,255,255,.30); border-top-width: 2px; border-bottom: 3px solid #214b79;
                        box-shadow: inset 0 2px 0 rgba(255,255,255,.18), inset 0 -4px 0 rgba(10,42,78,.20),
                                    0 4px 0 #294f7e, 0 7px 9px rgba(0,0,0,.18); }}
.race-options {{ display: table; width: 100%; height: 25px; table-layout: fixed; }}
.race-available {{ display: table-cell; vertical-align: middle; width: 54%; color: #ffffff; font-size: 15px;
                    line-height: 20px; font-weight: 900;
                    text-shadow: 1px 1px 0 #315a8d, -1px 1px 0 #315a8d, 0 2px 0 #315a8d,
                                 0 3px 4px rgba(0,0,0,.25); }}
.mkgrid.race-mkgrid {{ width: 100%; height: 245px; padding-top: 5px; text-align: left; overflow: hidden; }}
.race-mkwrap {{ display: inline-block; width: 64px; height: 79px; margin: 0 2px 3px 0; vertical-align: top; }}
.race-mk {{ position: relative; width: 64px; height: 79px; border-radius: 5px; border: 2px solid rgba(0,0,0,.18);
            text-align: center; overflow: hidden; box-shadow: inset 0 2px 0 rgba(255,255,255,.38),
                        inset 0 -3px 0 rgba(0,0,0,.14), 0 3px 0 rgba(34,77,125,.55),
                        0 5px 6px rgba(0,0,0,.12); }}
.race-mk.primary {{ background: linear-gradient(180deg, #a7e4fa 0%, #8bcfee 75%, #74b7dc 100%); }}
.race-mk.military {{ background: linear-gradient(180deg, #baf5a3 0%, #a2ef8f 75%, #81d873 100%); }}
.race-mk.magic {{ background: linear-gradient(180deg, #d2b4ff 0%, #b990f7 75%, #9d70df 100%); }}
.race-mk.support {{ background: linear-gradient(180deg, #ffe0ae 0%, #f6ce95 75%, #dfa96b 100%); }}
.race-mk.hero {{ background: linear-gradient(180deg, #fff78b 0%, #ffed00 75%, #e5c900 100%); }}
.race-mk img {{ display: block; width: 60px; height: 60px; margin: 1px auto 0; object-fit: contain; }}
.race-mk .race-lim {{ position: absolute; right: 1px; top: 1px; min-width: 23px; padding: 1px 3px;
                      border-radius: 4px; color: #ffffff; background: #ee7700; font-size: 11px; line-height: 15px;
                      font-weight: 900; text-shadow: 1px 1px 0 #6f2a00; }}
.race-mk .race-path {{ position: absolute; left: 3px; right: 3px; bottom: 2px; border-radius: 3px; color: #ffffff;
                       background: rgba(31, 84, 145, .9); font-size: 10px; line-height: 14px; font-weight: 900; }}
.race-mk-fallback {{ color: #223d62; font-size: 11px; line-height: 14px; font-weight: 900; padding: 20px 2px; word-break: break-all; }}
.race-bottom {{ display: table; width: 100%; margin-top: 15px; height: 113px; table-layout: fixed; }}
.race-bottom-left {{ display: table-cell; width: 34%; vertical-align: top; padding-left: 6px; }}
.race-bottom-right {{ display: table-cell; width: 66%; vertical-align: top; padding-left: 49px; padding-right: 3px; }}
.race-bottom-panel {{ height: 113px; padding: 7px 10px; border-radius: 6px;
                       background: linear-gradient(180deg, #527fb5 0%, #436fa7 52%, #3a6397 100%);
                       border: 1px solid rgba(255,255,255,.28); border-top-width: 2px; border-bottom: 3px solid #315a8d;
                       box-shadow: inset 0 2px 0 rgba(255,255,255,.22), inset 0 -3px 0 rgba(15,48,84,.26),
                                   0 4px 0 #294f7e, 0 7px 9px rgba(0,0,0,.22); }}
.race-bottom-title {{ color: #ffffff; font-size: 16px; line-height: 20px; text-align: center; font-weight: 900;
                      text-shadow: 1px 1px 0 #163e68, -1px 1px 0 #163e68, 0 2px 0 #163e68,
                                   0 4px 5px rgba(0,0,0,.34); }}
.race-default {{ padding-top: 8px; text-align: center; color: #ffffff; font-size: 15px; line-height: 20px; font-weight: 900;
                 text-shadow: 1px 1px 0 #163e68, -1px 1px 0 #163e68, 0 2px 0 #163e68,
                              0 4px 5px rgba(0,0,0,.32); }}
.race-mod-grid {{ width: 100%; padding-top: 4px; }}
.race-mod-row {{ display: table; width: 100%; table-layout: fixed; }}
.race-mod-item {{ display: table-cell; width: 50%; height: 35px; vertical-align: middle; }}
.race-mod-icon-cell {{ display: table-cell; width: 32px; vertical-align: middle; text-align: center; }}
.race-mod-icon {{ display: inline-block; width: 29px; height: 29px; object-fit: contain; }}
.race-mod-icon-fallback {{ display: inline-block; width: 29px; height: 29px; color: #ffe66b; font-size: 22px; line-height: 29px; text-align: center; }}
.race-mod-copy {{ display: table-cell; vertical-align: middle; padding-left: 2px; }}
.race-mod-label {{ color: #ffffff; font-size: 9px; line-height: 11px; font-weight: 900; white-space: nowrap;
                   text-shadow: 1px 1px 0 #163e68, -1px 1px 0 #163e68, 0 2px 3px rgba(0,0,0,.32); }}
.race-mod-value {{ color: #ffe66b; font-size: 11px; line-height: 13px; font-weight: 900;
                   text-shadow: 1px 1px 0 #7a4100, -1px 1px 0 #7a4100, 0 2px 3px rgba(0,0,0,.28); }}
.race-mod-default {{ padding-top: 15px; text-align: center; color: #ffffff; font-size: 15px; line-height: 20px; font-weight: 900;
                     text-shadow: 1px 1px 0 #163e68, -1px 1px 0 #163e68, 0 2px 3px rgba(0,0,0,.32); }}
.race-rule-row {{ display: table; width: 100%; height: 72px; margin-top: 2px; table-layout: fixed; }}
.race-rule-item {{ display: table-cell; vertical-align: middle; width: 50%; }}
.race-rule-item.race-rule-single {{ width: 100%; }}
.race-rule-icon {{ display: table-cell; width: 56px; vertical-align: middle; }}
.race-rule-icon img {{ width: 54px; height: 54px; object-fit: contain; }}
.race-rule-fallback {{ display: inline-block; width: 50px; height: 50px; font-size: 38px; line-height: 50px; }}
.race-rule-copy {{ display: table-cell; vertical-align: middle; padding-left: 3px; color: #ffffff; font-size: 10px; line-height: 12px; font-weight: 900;
                   text-shadow: 1px 1px 0 #163e68, -1px 1px 0 #163e68, 0 2px 3px rgba(0,0,0,.32); }}
.race-limit-value {{ font-size: 13px; }}
.race-custom-panel {{ min-height: 92px; margin: 15px 6px 0; padding: 7px 10px; border-radius: 6px;
                       background: linear-gradient(180deg, #527fb5 0%, #436fa7 52%, #3a6397 100%);
                       border: 1px solid rgba(255,255,255,.28); border-top-width: 2px; border-bottom: 3px solid #315a8d;
                       box-shadow: inset 0 2px 0 rgba(255,255,255,.22), inset 0 -3px 0 rgba(15,48,84,.26),
                                   0 4px 0 #294f7e, 0 7px 9px rgba(0,0,0,.22); }}
.race-custom-title {{ color: #ffffff; font-size: 16px; line-height: 20px; text-align: center; font-weight: 900;
                       text-shadow: 1px 1px 0 #163e68, -1px 1px 0 #163e68, 0 2px 0 #163e68,
                                    0 4px 5px rgba(0,0,0,.34); }}
.race-custom-body {{ display: table; width: 100%; min-height: 54px; table-layout: fixed; }}
.race-custom-icon {{ display: table-cell; width: 58px; vertical-align: top; padding-top: 4px; text-align: center; }}
.race-custom-icon img {{ width: 48px; height: 48px; object-fit: contain; }}
.race-custom-copy {{ display: table-cell; vertical-align: top; color: #ffffff; font-size: 13px; line-height: 18px;
                     font-weight: 900; text-shadow: 1px 1px 0 #163e68, -1px 1px 0 #163e68,
                                  0 2px 3px rgba(0,0,0,.32); }}
.race-round-set-name {{ color: #ffe66b; font-size: 13px; line-height: 18px; font-weight: 900;
                        text-shadow: 1px 1px 0 #7a4100, -1px 1px 0 #7a4100, 0 2px 3px rgba(0,0,0,.28); }}
.race-round-lines {{ margin-top: 2px; }}
.race-round-line {{ line-height: 20px; white-space: nowrap; }}
.race-round-wave {{ display: inline-block; min-width: 70px; color: #ffffff; }}
.race-round-desc {{ color: #ffffff; }}
.race-bloon-icon {{ display: inline-block; width: 28px; height: 28px; margin: 0 3px; vertical-align: middle;
                    border-radius: 50%; background: rgba(255,255,255,.12);
                    box-shadow: inset 0 1px 0 rgba(255,255,255,.35), 0 2px 0 rgba(15,48,84,.35); }}
.race-bloon-icon img {{ display: block; width: 28px; height: 28px; object-fit: contain; }}
.race-bloon-fallback {{ display: inline-block; color: #ffe66b; }}
.compat-data {{ display: none; }}
</style></head>
<body><div class="race-page"><div class="race-frame">{body}</div></div></body></html>"""


def _race_ui_img(fname: str, fallback: str, cls: str) -> str:
    url = _ui_asset_data_url(fname)
    if url:
        return f"<img class='{cls}' src='{_esc(url)}'/>"
    fallback_class = {
        "race-stat-icon": "race-stat-fallback",
        "race-rule-icon-img": "race-rule-fallback",
    }.get(cls, f"{cls}-fallback")
    return f"<span class='{fallback_class}'>{_esc(fallback)}</span>"


def _fmt_range(ev: dict) -> str:
    s = datetime.fromtimestamp(int(ev.get("start") or 0) / 1000, tz=_SH)
    e = datetime.fromtimestamp(int(ev.get("end") or 0) / 1000, tz=_SH)
    return f"{s.year}/{s.month}/{s.day} - {e.year}/{e.month}/{e.day}"


_BOSS_EVENT_ASSETS = {
    "bloonarius": "boss-bloonarius.png",
    "lych": "boss-lych.png",
    "vortex": "boss-vortex.png",
    "dreadbloon": "boss-dreadbloon.png",
    "phayze": "boss-phayze.png",
    "blastapopoulos": "boss-blastapopoulos.png",
}

_RACE_TITLE_CN = {
    "three mines back around": "三矿往返",
}


def _boss_event_asset(ev: dict | None) -> str:
    raw = str((ev or {}).get("bossType") or "").strip().lower()
    return _BOSS_EVENT_ASSETS.get(raw, "")


def _race_emblem(ev: dict | None, side_img: str) -> str:
    """Boss 使用首领徽章，普通竞速使用游戏内竞速奖杯图标。"""
    asset = _boss_event_asset(ev)
    if asset and _ui_asset_data_url(asset):
        return _race_ui_img(asset, "🐒", "race-emblem-img")
    if side_img:
        return f"<img class='race-emblem-img' src='{_esc(side_img)}' alt='首领'/>"
    race_asset = "RaceIcon.png"
    if _ui_asset_data_url(race_asset):
        return _race_ui_img(race_asset, "🏆", "race-emblem-img")
    return "<div class='race-emblem-fallback'>⚑</div>"


def _race_title(name: str, ev: dict | None, side_img: str) -> str:
    raw = str(name or "").strip()
    if side_img and ev:
        boss = boss_cn(ev.get("bossType"))
        tier = re.search(r"(\d+)\s*$", raw)
        return f"{boss} {tier.group(1)}" if tier else boss
    return _RACE_TITLE_CN.get(raw.casefold(), raw) or "气球塔防6挑战"


def _race_time_line(ev: dict | None) -> str:
    if not ev:
        return ""
    try:
        start, end = int(ev.get("start") or 0), int(ev.get("end") or 0)
        if start <= 0 or end <= start:
            return ""
    except (TypeError, ValueError):
        return ""
    # 只显示活动的固定时间范围。倒计时随时间流逝不断变化，会让本应长期
    # 不变的竞速规则卡片不断生成新 HTML，从而失去持久缓存的意义。
    return f"活动时间：{_fmt_range(ev)}"


_ROUND_SET_CN = {
    "default": "默认回合",
    "phayze": "幻影回合",
    "dreadbloon": "恐惧气球岩回合",
    "vortex": "漩涡回合",
    "lych": "巫妖回合",
    "bloonarius": "膨胀气球神回合",
    "blastapopoulos": "爆裂魔炎回合",
}


_ROUND_SET_DETAILS = {
    # Ninja Kiwi 的 metadata 只给出 roundSets 名称；这些是游戏内该回合组
    # 对第 40/60/80/100 回合的固定替换内容（Phayze/Bloonarius/
    # Blastapopoulos 共用这一组变更）。
    "phayze": (
        ("第40回合", "MOAB级气球替换为 6 个陶瓷气球"),
        ("第60回合", "BFB 替换为 6 个 MOAB"),
        ("第80回合", "ZOMG 替换为 6 个 BFB"),
        ("第100回合", "BAD 替换为 4 个 ZOMG、6 个 DDT"),
    ),
    "bloonarius": (
        ("第40回合", "MOAB级气球替换为 6 个陶瓷气球"),
        ("第60回合", "BFB 替换为 6 个 MOAB"),
        ("第80回合", "ZOMG 替换为 6 个 BFB"),
        ("第100回合", "BAD 替换为 4 个 ZOMG、6 个 DDT"),
    ),
    "blastapopoulos": (
        ("第40回合", "MOAB级气球替换为 6 个陶瓷气球"),
        ("第60回合", "BFB 替换为 6 个 MOAB"),
        ("第80回合", "ZOMG 替换为 6 个 BFB"),
        ("第100回合", "BAD 替换为 4 个 ZOMG、6 个 DDT"),
    ),
}


def _custom_round_set_keys(meta: dict) -> list[str]:
    raw = meta.get("roundSets")
    values = [raw] if isinstance(raw, str) else raw if isinstance(raw, (list, tuple)) else []
    return [str(value or "").strip() for value in values
            if str(value or "").strip() and str(value or "").strip().casefold() != "default"]


def _custom_round_sets(meta: dict) -> list[str]:
    """API 的 roundSets 中除 default 外的回合组，返回卡片可读的中文名称。"""
    return [_ROUND_SET_CN.get(key.casefold(), key) for key in _custom_round_set_keys(meta)]


def _custom_round_details(meta: dict) -> list[tuple[str, str]]:
    """把已知回合组展开为“第几回合：出现什么气球”，未知组保留可读降级。"""
    details = []
    for key in _custom_round_set_keys(meta):
        known = _ROUND_SET_DETAILS.get(key.casefold())
        if known:
            details.extend(known)
        else:
            name = _ROUND_SET_CN.get(key.casefold(), key)
            details.append(("回合组", f"{name}：API 未提供逐回合明细"))
    return details


_ROUND_BLOON_ICON_FILES = {
    "MOAB级气球": "Moab.png",
    "陶瓷气球": "Ceramic.png",
    "MOAB": "Moab.png",
    "BFB": "Bfb.png",
    "ZOMG": "Zomg.png",
    "BAD": "Bad.png",
    "DDT": "DdtCamo.png",
}
_ROUND_BLOON_TOKEN_RE = re.compile("|".join(
    re.escape(token) for token in sorted(_ROUND_BLOON_ICON_FILES, key=len, reverse=True)
))


def _round_bloon_icon(token: str) -> str:
    """回合明细中的气球名称 → 本地图标，素材缺失时保留文字降级。"""
    fname = _ROUND_BLOON_ICON_FILES.get(token, "")
    url = _ui_asset_data_url(fname)
    if not url:
        return f"<span class='race-bloon-fallback'>{_esc(token)}</span>"
    return (f"<span class='race-bloon-icon' title='{_esc(token)}'>"
            f"<img src='{_esc(url)}' alt='{_esc(token)}'/></span>")


def _round_detail_desc_html(description: str) -> str:
    """转义普通文字，并把已知气球名替换为本地透明图标。"""
    text = str(description or "")
    chunks = []
    pos = 0
    for match in _ROUND_BLOON_TOKEN_RE.finditer(text):
        chunks.append(_esc(text[pos:match.start()]))
        chunks.append(_round_bloon_icon(match.group(0)))
        pos = match.end()
    chunks.append(_esc(text[pos:]))
    return "".join(chunks)


def _race_modifier_items(mods: dict | None) -> list[tuple[str, str, str]]:
    """将气球强化整理为（中文名称、倍率/状态、图标文件）。"""
    mods = mods or {}
    items = []

    def add(label: str, value, increase_icon: str, decrease_icon: str) -> None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return
        if abs(number - 1.0) < 1e-9:
            return
        icon = increase_icon if number > 1 else decrease_icon
        items.append((label, f"×{number:g}", icon))

    add("气球速度", mods.get("speedMultiplier"), "FasterBloonsIcon.png", "SlowerBloonsIcon.png")
    add("重型气球速度", mods.get("moabSpeedMultiplier"), "FasterMoabIcon.png", "SlowerMoabIcon.png")
    add("首领速度", mods.get("bossSpeedMultiplier"), "FasterBossIcon.png", "SlowerBossIcon.png")
    add("再生速度", mods.get("regrowRateMultiplier"), "RegrowRateIncreaseIcon.png", "RegrowRateDecreaseIcon.png")
    health = mods.get("healthMultipliers") or {}
    add("气球血量", health.get("bloons"), "BloonBoostIcon.png", "BloonDecreaseHPIcon.png")
    add("重型气球血量", health.get("moabs"), "MoabBoostIcon.png", "MoabDecreaseHPIcon.png")
    add("首领血量", health.get("boss"), "BossBoostIcon.png", "BossDecreaseHPIcon.png")
    if mods.get("allCamo"):
        items.append(("全体隐身", "启用", "AllCamoIcon.png"))
    if mods.get("allRegen"):
        items.append(("全体再生", "启用", "AllRegenIcon.png"))
    return items


def _race_modifier_html(mods: dict | None) -> str:
    items = _race_modifier_items(mods)
    if not items:
        return "<div class='race-mod-default'>默认</div>"
    rows = []
    for start in range(0, len(items), 2):
        cells = []
        for label, value, icon in items[start:start + 2]:
            value_html = f"<div class='race-mod-value'>{_esc(value)}</div>" if value else ""
            cells.append(
                "<div class='race-mod-item'><div class='race-mod-icon-cell'>"
                f"{_race_ui_img(icon, '⚡', 'race-mod-icon')}"
                f"</div><div class='race-mod-copy'><div class='race-mod-label'>{_esc(label)}</div>"
                f"{value_html}</div></div>"
            )
        if len(cells) == 1:
            cells.append("<div class='race-mod-item'></div>")
        rows.append("<div class='race-mod-row'>" + "".join(cells) + "</div>")
    return "<div class='race-mod-grid'>" + "".join(rows) + "</div>"


_STATE_TXT = {"on": "进行中", "up": "未开始", "off": "已结束"}


def _banner(kind: str, items: list, ptitle: str, now_ms: int, img: str, race_meta: dict) -> tuple[str, int]:
    ev = _pick_section(items, now_ms)
    color = _SEC_COLOR[kind]
    if not ev:
        inner = (f"<div class='ptitle' style='color:{color}'>{_esc(ptitle)}</div>"
                 "<div class='empty'>暂无数据</div>")
        return f"<div class='panel'>{inner}</div>", 175
    name = (ev.get("name") or "").strip()
    if kind == "boss":
        bt = boss_cn(ev.get("bossType"))
        if bt:
            name = f"{name}（{bt}）"
    state = _state_of(ev, now_ms)
    if kind == "race":
        score = f"总分：<b>{int(ev.get('totalScores') or 0):,}</b>"
    elif kind == "boss":
        score = (f"总分：标准 <b>{int(ev.get('totalScores_standard') or 0):,}</b>"
                 f" · 精英 <b>{int(ev.get('totalScores_elite') or 0):,}</b>")
    else:
        score = (f"参与：个人 <b>{int(ev.get('totalScores_player') or 0):,}</b>"
                 f" · 战队 <b>{int(ev.get('totalScores_team') or 0):,}</b>")
    img_cell = (f"<div class='bimg'><img src='{_esc(img)}'/></div>" if img
                else "<div class='bimg'><div class='bimg-ph'></div></div>")
    tint = {"race": "#eef4ff", "boss": "#fdeeec", "ct": "#fbf5e8"}.get(kind, "#ffffff")
    html = (f"<div class='panel banner' style='border-left:10px solid {color};"
            f"background:linear-gradient(90deg, {tint}, #ffffff 45%)'>"
            f"<div class='phead'>{img_cell}"
            f"<div class='btext'>"
            f"<div class='brow'><span class='bname'>{_esc(name)}</span>"
            f"<span class='badge'><span class='st-{state}'>{_STATE_TXT[state]}</span></span></div>"
            f"<div class='bdates'>{_esc(_fmt_range(ev))}</div>"
            f"<div class='bscore'>{score}</div>"
            f"</div></div></div>")
    return html, 210


def _overview_panel_html(
    ev: dict, kind: str, now: int, race_map: str, boss_img: str,
    race_map_by_id: dict | None = None, boss_img_by_id: dict | None = None,
) -> str:
    """单场活动的卡片面板（与旧版 overview_html 保持同样式，用于三段式复用）。"""
    ptitle_map = {"race": "每周竞赛", "boss": "Boss 事件", "ct": "争夺领土", "odyssey": "远征"}
    ptitle = ptitle_map.get(kind, kind)
    name = (ev.get("name") or ptitle).strip()
    if kind == "boss":
        bt = boss_cn(ev.get("bossType"))
        if bt:
            name = f"{name}（{bt}）"
    state = _state_of(ev, now)
    badge_cls = {"on": "st-on", "up": "st-up", "off": "st-off"}[state]
    # 优先用带背景的完整瓦片（512×230，含图标+文字），缺失再回退旧的单图/地图逻辑
    img = ""
    eid = str(ev.get("id") or "")
    if kind == "race":
        img = _ui_asset_data_url("race-tile.png") or ""
        if not img:
            if race_map_by_id and eid in race_map_by_id:
                img = race_map_by_id[eid] or ""
            else:
                img = race_map or ""
    elif kind == "boss":
        img = _ui_asset_data_url("boss-tile.png") or ""
        if not img:
            if boss_img_by_id and eid in boss_img_by_id:
                img = boss_img_by_id[eid] or ""
            else:
                img = boss_img or ""
    elif kind == "rush":
        img = _ui_asset_data_url("boss-rush.png") or _ui_asset_data_url("boss-event.png") or ""
    elif kind == "odyssey":
        img = _ui_asset_data_url("odyssey-tile.png") or _ui_asset_data_url("odyssey-event.png") or _ui_asset_data_url("BTD6_OdysseyEventBtn.png") or ""
    elif kind == "ct":
        img = _ui_asset_data_url("ct-tile.png") or _ui_asset_data_url("ct-event.png") or _ui_asset_data_url("BTD6_ContestedTerritoryEventBtn.png") or ""
    # 瓦片带背景（512×230）用 cover 铺满更协调
    if img:
        cls = "ov-img"
        img_cell = f"<img class='{cls}' src='{_esc(img)}'/>"
    else:
        ph = "🏰" if kind == "odyssey" else ("⚔️" if kind == "ct" else "🎈")
        img_cell = f"<div class='ov-img-ph'>{ph}</div>"
    if kind == "race":
        score = f"参与 <b>{int(ev.get('totalScores') or 0):,}</b>"
    elif kind == "boss":
        score = f"标准 <b>{int(ev.get('totalScores_standard') or 0):,}</b> · 精英 <b>{int(ev.get('totalScores_elite') or 0):,}</b>"
    elif kind == "rush":
        # Boss Rush uses events summary, no detailed score yet
        score = f"Boss Rush · {_fmt_range(ev)}"
    elif kind == "odyssey":
        desc = (ev.get("description") or "").strip()
        if desc:
            short = desc[:36] + ("…" if len(desc) > 36 else "")
            score = _esc(short)
        else:
            score = "远征活动"
    else:
        score = f"个人 <b>{int(ev.get('totalScores_player') or 0):,}</b> · 战队 <b>{int(ev.get('totalScores_team') or 0):,}</b>"
    return (
        f"<div class='ody-panel ov-panel'><div class='ody-ribbon ody-panel-title'><span>{_esc(ptitle)}</span></div>"
        f"<div class='ov-row'><div class='ov-img-cell'>{img_cell}</div>"
        f"<div class='ov-info'><div class='ov-name'>{_esc(name)}<span class='ov-badge {badge_cls}'>{_STATE_TXT[state]}</span></div>"
        f"<div class='ov-dates'>{_esc(_fmt_range(ev))}</div>"
        f"<div class='ov-score'>{score}</div></div></div></div>"
    )


# NK /btd6/events 的 name 字段为英文固定模板名，这里统一汉化
_EVENT_NAME_CN = {
    "A Boss Rush Event": "Boss 竞速冲刺",
    "A Contested Territory": "争夺领土",
    "A Social Season Event": "社交赛季活动",
    "A Boss Event": "Boss 战活动",
    "A Race Event": "竞速活动",
    "An Odyssey Event": "远征活动",
}


def overview_html(data: dict) -> str:
    """活动总览：三段式 - 正在进行 / 即将开始 / 已结束(近5)，从上到下排列。"""
    now = data["now"]
    races = data.get("races") or []
    bosses = data.get("bosses") or []
    cts = data.get("cts") or []
    odysseys = data.get("odysseys") if isinstance(data.get("odysseys"), list) else []
    rush = data.get("rush") if isinstance(data.get("rush"), list) else []
    ongoing, upcoming, ended = _classify_overview_events(races, bosses, cts, now, odysseys, rush)
    parts: list[str] = []
    total_h = 6

    def _row_html(ev: dict, kind: str, state: str) -> str:
        """单行活动：圆形图标 + 名称 + 日期范围 + 右侧状态。"""
        # 图标：按 kind 取最匹配的圆形方块图（已有 race-tile/boss-tile/odyssey-tile/ct-tile 为横幅，回退旧 boss-* 与 odyssey-event/ct-event 方形）
        icon_data = ""
        if kind == "race":
            icon_data = _ui_asset_data_url("boss-bloonarius.png") or _ui_asset_data_url("boss-event-official.png") or _ui_asset_data_url("race-tile.png") or ""
        elif kind == "boss":
            bt = str(ev.get("bossType") or "").strip().lower()
            boss_icon_map = {
                "bloonarius": "boss-bloonarius.png",
                "lych": "boss-lych.png",
                "vortex": "boss-vortex.png",
                "dreadbloon": "boss-dreadbloon.png",
                "phayze": "boss-phayze.png",
                "blastapopoulos": "boss-blastapopoulos.png",
            }
            asset = boss_icon_map.get(bt, "")
            icon_data = _ui_asset_data_url(asset) or _ui_asset_data_url("boss-event-official.png") or _ui_asset_data_url("boss-tile.png") or ""
        elif kind == "odyssey":
            icon_data = _ui_asset_data_url("odyssey-event.png") or _ui_asset_data_url("odyssey-tile.png") or ""
        elif kind == "rush":
            icon_data = _ui_asset_data_url("boss-rush.png") or _ui_asset_data_url("boss-event.png") or _ui_asset_data_url("boss-tile.png") or ""
        elif kind == "ct":
            icon_data = _ui_asset_data_url("ct-event.png") or _ui_asset_data_url("ct-tile.png") or ""
        # 默认占位（彩色圆底白字）
        if not icon_data:
            ph = {"race": "🏁", "boss": "👹", "odyssey": "🏰", "ct": "⚔️"}.get(kind, "🎈")
            icon_html = f"<div class='ev-icon' style='background:#3a5a7a'>{ph}</div>"
        else:
            icon_html = f"<img class='ev-icon' src='{_esc(icon_data)}'/>"
        # 名称：按 kind 翻译为中文，保留 NK API 原文作小字副标题
        raw_name = (ev.get("name") or "").strip()
        generic = _EVENT_NAME_CN.get(raw_name)
        if kind == "race":
            name = "每周竞速活动" if generic else f"每周竞赛 · {raw_name or 'Race Event'}"
        elif kind == "boss":
            bt_raw = str(ev.get("bossType") or "").strip()
            if not bt_raw:
                bt_raw = re.sub(r"\d+$", "", str(ev.get("id") or "").split("_", 1)[0])
            bt_cn = boss_cn(bt_raw)
            if not bt_cn or bt_cn == bt_raw:
                bt_cn = _BOSS_CN.get(bt_raw, bt_raw)
            if generic:
                name = f"Boss 战 · {bt_cn}" if bt_cn else generic
            else:
                name = f"{raw_name}（{bt_cn}）" if bt_cn and bt_cn not in raw_name else (raw_name or "Boss 事件")
        elif kind == "odyssey":
            name = "远征活动" if generic else f"远征 · {raw_name or 'Odyssey Event'}"
        elif kind == "rush":
            name = "Boss 竞速冲刺" if generic else f"Boss Rush · {raw_name}"
        elif kind == "ct":
            name = "争夺领土（CT）"
        else:
            name = generic or raw_name or "未知活动"
        # 日期
        s, e = int(ev.get("start") or 0), int(ev.get("end") or 0)
        date1 = fmt_date(s)
        date2 = fmt_date(e)
        # 状态（中文）
        if state == "on":
            if s and e:
                now_s = now
                days_left = max(0, int((e - now_s) / 86400000))
                if days_left >= 1:
                    right = f"剩余 {days_left} 天"
                else:
                    hours_left = max(0, int((e - now_s) / 3600000))
                    right = f"剩余 {hours_left} 小时"
            else:
                right = "进行中"
            right_cls = "ev-right-on"
        elif state == "up":
            right = "即将开始"
            right_cls = "ev-right-up"
        else:
            right = "已结束"
            right_cls = "ev-right-off"
        return (
            f"<div class='ev-row'>"
            f"<div class='ev-icon-cell'>{icon_html}</div>"
            f"<div class='ev-name'>{_esc(name)}</div>"
            f"<div class='ev-dates'><div>开始 {_esc(date1)}</div><div>结束 {_esc(date2)}</div></div>"
            f"<div class='ev-right {right_cls}'>{_esc(right)}</div>"
            f"</div>"
        )

    def _section(title: str, items: list, empty_text: str = "暂无活动") -> tuple[str, int]:
        """三段式：标题横幅 + 列表或空态。返回 (html, height)."""
        h = []
        h.append(f'<div class="ody-ribbon ody-section-banner"><span>{_esc(title)}</span></div>')
        height = 48
        if not items:
            h.append(f"<div class='ev-empty'>{_esc(empty_text)}</div>")
            height += 56
        else:
            for ev, kind in items:
                s, e = int(ev.get("start") or 0), int(ev.get("end") or 0)
                state = "on" if s <= now < e else ("up" if now < s else "off")
                h.append(_row_html(ev, kind, state))
                height += 58
        return "".join(h), height

    # 正在进行
    h1, total_h = _section(f"🟢 进行中 ({len(ongoing)})", ongoing,
                            empty_text="暂无进行中的活动")
    parts.append(h1)
    parts.append('<div class="ev-spacer"></div>')
    total_h += 12

    # 即将开始
    h2, add = _section(
        f"🟡 即将开始 ({len(upcoming)})", upcoming,
        empty_text="暂无即将开始的活动",
    )
    parts.append(h2)
    total_h += add + 12
    parts.append('<div class="ev-spacer"></div>')

    # 已结束（近10，按你图显示更多）
    ended_show = ended[:10] if ended else []
    h3, add = _section(f"⚪ 已结束 · 近 {len(ended_show)} 场",
                        [(ev, kind) for ev, kind in ended_show],
                        empty_text="暂无已结束的活动")
    parts.append(h3)
    total_h += add

    return _odyssey_shell("\n".join(parts), total_h + 17)


_RUSH_BOSSES = [
    # 默认 Boss Rush 轮换（依 Bloons Wiki 与实测轮换），可按 ev["id"] 自定义
    ("Bloonarius",    "boss-bloonarius.png",    "1f479"),
    ("Lych",          "boss-lych.png",          "1f480"),
    ("Dreadbloon",    "boss-dreadbloon.png",    "1faa8"),
    ("Phayze",        "boss-phayze.png",        "1f47b"),
    ("Blastapopoulos","boss-blastapopoulos.png","1f525"),
]



def math_ceil(v: float) -> int:
    return int(v) if v == int(v) else int(v) + 1

# Boss 名 → 本地头像（Diamondback 走 nkstatic 官方立绘 Portrait.webp）
_RUSH_BOSS_ART = {name: png for name, png, _emoji in _RUSH_BOSSES}
_RUSH_MAX_MONKEYS = 30  # 每场战斗猴子数量上限（游戏内活动页固定值，转录）

_REWARD_LABELS = {
    "MonkeyMoney": "猴币", "Trophy": "奖杯", "TeamTrophy": "战队奖杯",
    "CollectionEvent": "收集事件", "RandomPower": "随机强化", "RandomInstaMonkey": "随机香蕉",
}

_BOSS_CN = {
    "Bloonarius": "布隆纳里乌斯", "Lych": "莱奇", "Dreadbloon": "恐惧布隆",
    "Phayze": "法泽", "Blastapopoulos": "爆破波普利斯", "Diamondback": "菱背龙",
}

# 塔中文与立绘（立绘来自 assets/game/000-*.webp）
_TOWER_CN = {
    "DartMonkey": "飞镖猴", "BoomerangMonkey": "回旋镖猴", "BombShooter": "炸弹射手",
    "TackShooter": "钉射手", "IceMonkey": "冰冻猴", "GlueGunner": "胶水猴",
    "SniperMonkey": "狙击猴", "MonkeySub": "潜艇猴", "MonkeyBuccaneer": "海盗猴",
    "MonkeyAce": "飞机猴", "HeliPilot": "直升机猴", "MortarMonkey": "迫击炮猴",
    "DartlingGunner": "机枪猴", "WizardMonkey": "巫师猴", "SuperMonkey": "超级猴",
    "NinjaMonkey": "忍者猴", "Alchemist": "炼金术士", "Druid": "德鲁伊",
    "BananaFarm": "香蕉农场", "SpikeFactory": "尖刺工厂", "MonkeyVillage": "猴子村庄",
    "EngineerMonkey": "工程师猴", "BeastHandler": "驯兽师", "Mermonkey": "人鱼猴",
    "Desperado": "亡命徒猴", "Skywarden": "巡天守卫",
}
_TOWER_PORTRAIT_SPECIAL = {"WizardMonkey": "000-Wizard.webp"}

# 英雄中文与头像（assets/game/*Portrait.webp）
_HERO_CN = {
    "Quincy": "昆西", "Gwendolin": "格温多林", "StrikerJones": "斯特里克·琼斯",
    "ObynGreenfoot": "奥宾·格林富特", "CaptainChurchill": "丘吉尔上尉",
    "Benjamin": "本杰明", "PatFusty": "帕特·福斯蒂", "Ezili": "伊泽莉",
    "Adora": "阿多拉", "AdmiralBrickell": "布里克尔上将", "Etienne": "艾蒂安",
    "Sauda": "绍达", "Psi": "普赛", "Geraldo": "杰拉尔多", "Corvus": "科尔乌斯",
    "Rosalia": "罗莎莉亚", "Silas": "塞拉斯", "DanDMonke": "丹迪猴",
}

# 遗物中文（Monkey Knowledge）
_RELIC_CN = {
    "StartingStash": "起始储备", "ExtraEmpowered": "额外赋能", "BoxOfChocolates": "巧克力礼盒",
    "BoxOfMonkey": "猴子礼盒", "MarchingBoots": "行军靴", "HeroBoost": "英雄增幅",
    "BiggerBloonSabotage": "大气球破坏", "RoundingUp": "清剿收尾", "ManaBulwark": "法力壁垒",
    "Regeneration": "再生", "Restoration": "修复", "DurableShots": "耐用射击",
    "AlchemistTouch": "炼金之触", "DeepHeat": "深层灼热", "Sharpsplosion": "尖刺爆破",
    "RoyalTreatment": "皇家礼遇", "HardBaked": "硬烤", "Fortifried": "酥脆加固",
    "MoabClash": "MOAB碰撞", "CamoFlogged": "隐形鞭笞", "BrokenHeart": "碎心",
    "GoingTheDistance": "行至千里", "Heartless": "无情", "FlintTips": "燧石弹头",
    "Abilitized": "技能化", "AirAndSea": "空海协同", "ElDorado": "黄金国",
    "CamoTrap": "隐形陷阱", "Thrive": "繁茂", "SuperMonkeyStorm": "超级猴风暴",
    "MonkeyBoost": "猴子增压", "RoadSpikes": "路钉", "MoabMine": "MOAB地雷",
    "GlueTrap": "胶水陷阱", "Techbot": "科技猴",
}


def _tower_cn(t: str) -> str:
    return _TOWER_CN.get(t, t)


def _tower_portrait(t: str) -> str:
    return _game_asset_data_url(_TOWER_PORTRAIT_SPECIAL.get(t, f"000-{t}.webp"))


def _rush_tower_category(t: str) -> str:
    return (rushgen.load_constants()["towersInOrder"].get(t) or {}).get("category", "")


# 分类底色：同类猴子同色（Primary 红 / Military 绿 / Magic 紫 / Support 橙）
_TOWER_CAT_COLORS = {
    "Primary": ("#d4604a", "#9c3a2a"), "Military": ("#7ba84e", "#4d7530"),
    "Magic": ("#9a6fe0", "#6440a8"), "Support": ("#e0a95f", "#b0782f"),
}


def _tower_cat_grad(t: str) -> tuple:
    return _TOWER_CAT_COLORS.get(_rush_tower_category(t), ("#8d8279", "#57504a"))


def _hero_cn(h: str) -> str:
    return _HERO_CN.get(h, h)


def _hero_portrait(h: str) -> str:
    return _game_asset_data_url(f"{h}Portrait.webp")


def _relic_cn(r: str) -> str:
    return _RELIC_CN.get(r, r)


def _rush_stage_rewards_text(reward_str: str) -> str:
    """把 StageRewards 配置串（K:V#K:V）转成中文摘要。"""
    parts = []
    for chunk in (reward_str or "").split("#"):
        if ":" not in chunk:
            continue
        k, v = chunk.split(":", 1)
        parts.append(f"{_REWARD_LABELS.get(k, k)} {v}")
    return " · ".join(parts)


def _rush_rewards_pairs(reward_str: str) -> list:
    """把 StageRewards 配置串解析为 [(中文标签, 数值)]（不含收集事件）。"""
    pairs = []
    for chunk in (reward_str or "").split("#"):
        if ":" not in chunk:
            continue
        k, v = chunk.split(":", 1)
        if k == "CollectionEvent":
            continue  # 用户要求去掉收集事件
        pairs.append((_REWARD_LABELS.get(k, k), v))
    return pairs


async def collect_rush() -> dict:
    """拉 /btd6/events 取 bossRush 摘要，用 rushgen 从活动种子生成逐阶段配置。

    NK 开放 API 不提供 Boss Rush 明细（/btd6/bossRush 为 404），逐阶段数据由
    rushgen（BTD6 API Explorer Lucy 逆向算法的 Python 移植）从活动 ID 确定性生成。
    """
    now = bucket_now()
    events = await fetch_body(URL_EVENTS)
    rush_list = [e for e in events if isinstance(e, dict) and e.get("type") == "bossRush"]
    ev = pick_active(rush_list, now) or pick_next(rush_list, now) or fallback_latest(rush_list)
    if not ev:
        return {"empty": "当前没有 Boss Rush 活动"}
    br = rushgen.load_constants()["bossRush"]
    scores = br.get("StageScores") or []
    rewards = br.get("StageRewards") or []
    gen = rushgen.generate_boss_rush(str(ev.get("id") or ""))
    islands = []
    for st in gen["stages"]:
        idx = st["stage"] - 1
        boss_name = st["boss"]
        boss_url = _game_asset_data_url(f"{boss_name}Portrait.webp")
        if not boss_url:
            art = _RUSH_BOSS_ART.get(boss_name)
            boss_url = (_ui_asset_data_url(art) if art else "") or _ui_asset_data_url("boss-event.png") or ""
        map_img = _game_asset_data_url(f"MapSelect{st['map']}Button.webp")
        islands.append({
            "stage": st["stage"],
            "name": f"Island {st['stage']} · {boss_name}",
            "map": boss_name,
            "map_name": st["map"],
            "map_img": map_img,
            "boss": boss_name,
            "kills": scores[idx] if idx < len(scores) else 0,
            "rewards": _rush_rewards_pairs(rewards[idx] if idx < len(rewards) else ""),
            "reward_text": _rush_stage_rewards_text(rewards[idx] if idx < len(rewards) else ""),
            "towers": st["towers"],
            "removed": st["removed"],
            "relics": st["relics"],
            "new_relic": st["newRelic"],
            "img": boss_url,
        })
    hero = gen.get("hero")
    return {"ev": ev,
            "hero": "ChosenPrimaryHero" if hero == "ChosenPrimaryHero" else (hero or ""),
            "diffs": {"default": {"meta": {"isExtreme": False}, "maps": islands}}}


def _rush_text(col: dict) -> str:
    """模仿 odyssey_text：文字版阶段路线。"""
    if col.get("empty"):
        return col["empty"]
    ev, diffs = col["ev"], col["diffs"]
    state = _STATE_TXT[_state_of(ev, bucket_now())]
    lines = [
        "🏝️ Boss Rush",
        f"{(ev.get('name') or '').strip() or 'Boss Rush'}（{state}）",
        _fmt_range(ev),
        "",
    ]
    diff = diffs.get("default") or {}
    for mp in diff.get("maps") or []:
        towers = "、".join(_tower_cn(t) for t in mp["towers"])
        lines.append(f"  第{mp['stage']}阶段 · {_BOSS_CN.get(mp['boss'], mp['boss'])} · 击杀 {mp['kills']}")
        lines.append(f"    地图：{mp['map_name']}")
        lines.append(f"    塔池：{towers}")
        lines.append(f"    奖励：{mp['reward_text']}")
        if mp.get("removed"):
            lines.append(f"    移除：{'、'.join(_tower_cn(t) for t in mp['removed'])}")
    hero = col.get("hero") or ""
    lines.append("英雄：" + ("队长自选" if hero == "ChosenPrimaryHero" else _hero_cn(hero)))
    return "\n".join(lines).rstrip()


def _rush_shell(body: str, h: int) -> str:
    """Boss Rush 专属外壳：复刻游戏内活动页的深色底版式。"""
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
@page {{ size: {ODYSSEY_CARD_W}px {h}px; margin: 0; background: #4a453c; }}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html, body {{ width: {ODYSSEY_CARD_W}px; height: {h}px; color: #e8dcc0;
        font-family: "WenQuanYi Micro Hei", "Noto Sans CJK SC", sans-serif;
        background: #4a453c; overflow: hidden; }}
.rush-pool {{ display: flex; flex-wrap: wrap; justify-content: center; margin-bottom: 12px; }}
.rush-cell {{ position: relative; width: 88px; height: 88px; border-radius: 8px;
              border: 1px solid rgba(0,0,0,.28); display: flex; align-items: center;
              justify-content: center; margin: 0 3px 3px 0;
              box-shadow: inset 0 1px 0 rgba(255,255,255,.35), 0 1px 0 rgba(0,0,0,.25); }}
.rush-cell img {{ width: 74px; height: 74px; object-fit: contain;
                  filter: drop-shadow(0 1px 1px rgba(0,0,0,.35)); }}
.rush-cell i.slash {{ position: absolute; inset: 0;
                      background: linear-gradient(135deg, transparent 42%, #e0442e 46%, #e0442e 54%, transparent 58%); }}
.rush-stage {{ position: relative; display: flex; gap: 12px; background: #3f3b33;
               border: 1px solid #2b2823; border-radius: 10px; padding: 10px 12px;
               margin-bottom: 10px; box-shadow: inset 0 1px 0 rgba(255,255,255,.06), 0 1px 0 rgba(0,0,0,.2); }}
.rush-map {{ position: relative; flex: none; width: 168px; height: 120px; }}
.rush-mapimg {{ width: 100%; height: 100%; object-fit: cover; border-radius: 6px; }}
.rush-mapname {{ position: absolute; left: 0; right: 0; bottom: 0; text-align: center;
                 background: rgba(20,18,14,.78); color: #ffd964; font-size: 13px;
                 font-weight: 900; line-height: 24px; letter-spacing: 1px; }}
.rush-stagebanner {{ position: absolute; top: -8px; left: -6px; padding: 2px 10px; z-index: 2;
                     background: linear-gradient(180deg,#ff9a3d,#e2611b); border: 2px solid #93400f;
                     border-radius: 4px; color: #ffffff; font-size: 13px; font-weight: 900;
                     box-shadow: 0 2px 0 rgba(0,0,0,.35); }}
.rush-bossimg {{ position: absolute; right: 26px; bottom: 22px; width: 86px; object-fit: contain;
                 z-index: 3; transform: scaleX(-1); filter: drop-shadow(0 2px 3px rgba(0,0,0,.45)); }}
.rush-mid {{ flex: 1; min-width: 0; }}
.rush-chips {{ display: flex; flex-wrap: wrap; gap: 6px; }}
.rush-chip {{ display: flex; align-items: center; gap: 5px; padding: 3px 10px; border-radius: 5px;
              background: #514c41; border: 1px solid #2c2924; color: #e8dcc0;
              font-size: 12px; line-height: 18px; font-weight: 700; white-space: nowrap; }}
.rush-chip img {{ width: 15px; height: 15px; object-fit: contain; }}
.rush-relics {{ display: flex; gap: 12px; margin-top: 9px; align-items: flex-start; }}
.rush-relic {{ position: relative; width: 48px; height: 48px; border-radius: 50%; background: #33302a;
               border: 2px solid #6b6355; display: flex; align-items: center; justify-content: center; }}
.rush-relic img {{ width: 40px; height: 40px; object-fit: contain; }}
.rush-relic.new {{ border-color: #ffc63d; box-shadow: 0 0 0 1px #ffc63d; }}
.rush-newribbon {{ position: absolute; top: -10px; right: -12px; width: 34px; object-fit: contain; }}
.rush-lost {{ flex: none; width: 200px; background: #35322b; border: 1px solid #26231f;
              border-radius: 6px; padding: 6px 8px; }}
.rush-lost-title {{ color: #e05545; font-size: 11px; font-weight: 900; margin-bottom: 6px; }}
.rush-lost-body {{ display: flex; flex-wrap: wrap; gap: 3px; align-items: center; }}
.rush-none {{ color: #9a9284; font-size: 11px; }}
.rush-note {{ margin-top: 4px; text-align: center; font-size: 10px; color: #857e70; }}
</style></head><body>{body}</body></html>"""


def _rush_diff_html(col: dict, d: str = "default", label: str = "") -> str:
    """Boss Rush 卡片：1:1 复刻游戏内活动页版式（塔池墙 + 逐阶段行，全汉化）。"""
    if col.get("empty"):
        return _rush_shell(f"<div style='padding:30px;text-align:center;font-size:15px;'>{_esc(col['empty'])}</div>", 160)
    ev = col["ev"]
    maps = ((col.get("diffs") or {}).get("default") or {}).get("maps") or []

    state = _state_of(ev, bucket_now())
    event_name = (ev.get("name") or "Boss Rush").strip() or "Boss Rush"

    # ---- 顶部塔池墙（同类同色底块，对照截图两行猴子头像） ----
    pool = maps[0]["towers"] if maps else []
    pool_cells = []
    for t in pool:
        img = _tower_portrait(t)
        c0, c1 = _tower_cat_grad(t)
        face = (f"<img src='{img}' alt='{_esc(_tower_cn(t))}' style='width:74px;height:74px;"
                f"object-fit:contain;filter:drop-shadow(0 1px 1px rgba(0,0,0,.35));'/>" if img
                else f"<div style='font-size:10px;color:#e8dcc0;'>{_esc(_tower_cn(t))}</div>")
        pool_cells.append(
            f"<div class='rush-cell' style='background:linear-gradient(180deg,{c0},{c1});'>{face}</div>")
    pool_block = f"<div class='rush-pool'>{''.join(pool_cells)}</div>"

    skull = ("<svg width='14' height='14' viewBox='0 0 24 24' fill='#e05545' style='flex:none;'>"
             "<path d='M12 2C7 2 3 6 3 11c0 2.4 1.2 4.5 3 5.7V19c0 1.1.9 2 2 2h1v-2h2v2h2v-2h2v2h1c1.1 0 2-.9 2-2v-2.3"
             "c1.8-1.2 3-3.3 3-5.7 0-5-4-9-9-9zM8.5 13a1.8 1.8 0 110-3.6 1.8 1.8 0 010 3.6zm7 0a1.8 1.8 0 110-3.6"
             " 1.8 1.8 0 010 3.6z'/></svg>")
    coin_icon = _game_asset_data_url("UI_CoinIcon.webp")
    new_ribbon = _game_asset_data_url("NewRibbon.webp")

    rows = []
    for mp in maps:
        boss_cn = _BOSS_CN.get(mp["boss"], mp["boss"])
        rewards = mp.get("rewards") or []
        mm = next((v for k, v in rewards if k == "猴币"), "-")
        tr = next((v for k, v in rewards if k == "奖杯"), "-")
        tt = next((v for k, v in rewards if k == "战队奖杯"), "-")
        extra = " · ".join(f"{k} {v}" for k, v in rewards if k not in ("猴币", "奖杯", "战队奖杯"))

        # 遗物圆牌：官方遗物图标 + 金环 + NEW 缎带
        relic_badges = []
        for r in mp["relics"]:
            is_new = r == mp.get("new_relic")
            r_img = _game_asset_data_url(f"{r}.webp")
            inner = (f"<img src='{r_img}' alt='{_esc(_relic_cn(r))}'/>" if r_img
                     else f"<span style='font-size:9px;'>{_esc(_relic_cn(r))}</span>")
            ribbon = (f"<img class='rush-newribbon' src='{new_ribbon}' alt='新'/>"
                      if is_new and new_ribbon else
                      ("<div class='rush-newtag'>新</div>" if is_new else ""))
            relic_badges.append(
                f"<div class='rush-relic{' new' if is_new else ''}'>{inner}{ribbon}</div>")

        # 损失猴子：与顶部塔池同样的大立绘底块 + 红斜杠
        lost_body = []
        for t in mp.get("removed") or []:
            img = _tower_portrait(t)
            c0, c1 = _tower_cat_grad(t)
            if img:
                lost_body.append(
                    f"<div class='rush-cell' style='background:linear-gradient(180deg,{c0},{c1});'>"
                    f"<img src='{img}' alt='{_esc(_tower_cn(t))}' style='filter:grayscale(.6);opacity:.85;'/>"
                    f"<i class='slash'></i></div>")
            else:
                lost_body.append(
                    f"<span style='padding:1px 6px;border-radius:4px;background:rgba(255,90,70,.14);"
                    f"border:1px solid rgba(255,90,70,.4);font-size:10px;color:#ff9a8a;"
                    f"line-height:16px;text-decoration:line-through;'>{_esc(_tower_cn(t))}</span>")
        lost_inner = "".join(lost_body) if lost_body else "<span class='rush-none'>暂无</span>"

        rows.append(
            "<div class='rush-stage'>"
            "<div class='rush-map'>"
            + (f"<img class='rush-mapimg' src='{_esc(mp['map_img'])}' alt='{_esc(mp['map_name'])}'/>"
               if mp.get("map_img") else
               f"<div class='rush-mapimg' style='background:linear-gradient(160deg,#3f6f5e,#26473c);'></div>")
            + f"<div class='rush-mapname'>{_esc(mp['map_name'])}</div>"
            f"<div class='rush-stagebanner'>第 {mp['stage']} 阶段</div>"
            + (f"<img class='rush-bossimg' src='{_esc(mp['img'])}' alt='{_esc(boss_cn)}'/>" if mp.get("img") else "")
            + "</div>"
            + "<div class='rush-mid'>"
            + "<div class='rush-chips'>"
            + f"<span class='rush-chip'>{skull}<span style='color:#ffb0a0;'>{mp['kills']} 击杀需求</span></span>"
            + (f"<span class='rush-chip'><img src='{coin_icon}'/>{mm} 猴币</span>" if coin_icon
               else f"<span class='rush-chip'>{mm} 猴币</span>")
            + f"<span class='rush-chip' style='background:#3f5a78;border-color:#2b3f57;color:#bfe3ff;'>"
            + f"🐵 {_RUSH_MAX_MONKEYS} 最大猴子数量</span>"
            + (f"<span class='rush-chip'>奖杯 {tr} · 战队奖杯 {tt}"
               + (f" · {extra}" if extra else "") + "</span>" if (tr != "-" or tt != "-" or extra) else "")
            + "</div>"
            + f"<div class='rush-relics'>{''.join(relic_badges)}</div>"
            + "</div>"
            + f"<div class='rush-lost'><div class='rush-lost-title'>损失猴子：</div>"
            + f"<div class='rush-lost-body'>{lost_inner}</div></div>"
            + "</div>"
        )

    body = (pool_block
            + "".join(rows)
            + f"<div class='rush-note'>{_esc(event_name)} · {_esc(_fmt_range(ev))} · "
            + f"阶段配置由活动种子确定性生成，与游戏内一致</div>")
    # 高度：塔池墙(2行×91+12) + 阶段行×152 + 底部注释30
    height = 200 + len(maps) * 152 + 30
    return _rush_shell(body, height)

def _rush_reward_part(reward_text: str, key: str) -> str:
    """从奖励摘要里取出某一货币的数值（如「猴币 100」→ 100）。"""
    for part in (reward_text or "").split(" · "):
        if part.startswith(key):
            return part[len(key):].strip()
    return "-"

def _ct_html(ev: dict, tiles: list, now: int) -> str:
    """争夺领土专属卡片：模仿竞速 UI，展示当前 CT 事件的领地与时间。"""
    name = (ev.get("id") or "CT").strip()
    state = _state_of(ev, now)
    # 统计 tiles
    total = len(tiles) if isinstance(tiles, list) else 0
    # 取前 6 个 tile 展示
    tile_preview = ""
    if tiles:
        # 简化：显示 tile 类型统计
        from collections import Counter
        cnt = Counter(x.get("type") for x in tiles if isinstance(x, dict))
        preview = " / ".join(f"{k}:{v}" for k, v in list(cnt.items())[:4])
        tile_preview = f"领地 {total} 块 · {preview}" if preview else f"领地 {total} 块"
    else:
        tile_preview = "领地数据加载中"
    ct_img = _ui_asset_data_url("ct-event.png") or _ui_asset_data_url("ct-tile.png") or ""
    emblem = f"<img class='race-emblem-img' src='{_esc(ct_img)}'/>" if ct_img else "<div class='race-emblem-fallback'>🏴</div>"
    title = "争夺领土"
    subtitle = f"CT {name}"
    time_line = _fmt_range(ev)
    player = int(ev.get("totalScores_player") or 0)
    team = int(ev.get("totalScores_team") or 0)
    body = (f"<div class='race-topbar'><div class='race-head'><div class='race-emblem-cell'><div class='race-emblem'>{emblem}</div></div>"
            f"<div class='race-title-cell'><div class='race-title'>{_esc(title)}</div><div class='race-subtitle'>{_esc(subtitle)}</div><div class='race-time'>{_esc(time_line)} · {_STATE_TXT[state]}</div></div></div></div>"
            f"<div class='race-content'><div class='race-layout'><div class='race-map'><div class='race-map-empty'>🗺️</div></div>"
            f"<div class='race-stats-cell'><div class='race-stats'><div class='race-stat-col'>"
            f"<div class='race-stat'><div class='race-stat-icon-cell'>{_race_ui_img('ct-event.png', '🏴', 'race-stat-icon')}</div><div class='race-stat-copy'><div class='race-stat-label'>个人参与</div><div class='race-stat-value'>{player:,}</div></div></div>"
            f"<div class='race-stat'><div class='race-stat-icon-cell'>{_race_ui_img('ct-tile.png', '🧩', 'race-stat-icon')}</div><div class='race-stat-copy'><div class='race-stat-label'>战队参与</div><div class='race-stat-value'>{team:,}</div></div></div>"
            f"</div><div class='race-stat-col'>"
            f"<div class='race-stat'><div class='race-stat-icon-cell'>{_race_ui_img('ct-event.png', '⏱', 'race-stat-icon')}</div><div class='race-stat-copy'><div class='race-stat-label'>活动状态</div><div class='race-stat-value'>{_STATE_TXT[state]}</div></div></div>"
            f"<div class='race-stat'><div class='race-stat-icon-cell'>{_race_ui_img('ct-tile.png', '🗺️', 'race-stat-icon')}</div><div class='race-stat-copy'><div class='race-stat-label'>领地</div><div class='race-stat-value'>{total} 块</div></div></div>"
            f"</div></div></div></div></div>"
            f"<div class='race-monkey-section'><div class='race-options'><div class='race-available'>{_esc(tile_preview)}</div></div></div></div>")
    return _race_shell(body, 620)


def leaderboard_html(col: dict) -> str:
    """排行榜：远征纸张 + 蓝丝带 + 棕面板 + 奖牌配色。"""
    if col.get("empty"):
        return _odyssey_shell(f"<div class='ody-panel ov-panel'><div class='lb-empty'>{_esc(col['empty'])}</div></div>", 180)
    img = col.get("img") or ""
    img_tag = f"<img class='ov-img' src='{_esc(img)}'/>" if img else ""
    # 头部信息面板
    head_info = f"<div class='lb-header'><div class='lb-title'>{_esc(col['head'])}</div><div class='lb-subtitle'>{_esc(col['status'])}</div></div>"
    if img_tag:
        head_body = f"<div class='ov-row'><div class='ov-info'>{head_info}</div><div class='ov-img-cell'>{img_tag}</div></div>"
    else:
        head_body = head_info
    head_panel = f"<div class='ody-panel ov-panel'><div class='ody-ribbon ody-panel-title'><span>赛事信息</span></div>{head_body}</div>"
    entries = col["entries"]
    if entries:
        rows = []
        for i, name, score_txt in entries:
            medal = _MEDAL_COLOR.get(i)
            rank_style = f"color:{medal}" if medal else "color:#ffe9a8"
            # 前三名用金银铜底，需深色文字保证对比度
            row_bg = ""
            name_extra = ""
            score_extra = ""
            if i == 1:
                row_bg = "background: linear-gradient(90deg, #ffe9a8 0%, #d9b95c 100%);"
                rank_style = "color:#8a6d00"
                name_extra = "color:#5a3a00;text-shadow:0 1px 0 rgba(255,255,255,.6)"
                score_extra = "color:#7a5a00;text-shadow:0 1px 0 rgba(255,255,255,.5)"
            elif i == 2:
                row_bg = "background: linear-gradient(90deg, #e8e8e8 0%, #a8a8a8 100%);"
                rank_style = "color:#555"
                name_extra = "color:#2e2e2e;text-shadow:0 1px 0 rgba(255,255,255,.6)"
                score_extra = "color:#333;text-shadow:0 1px 0 rgba(255,255,255,.5)"
            elif i == 3:
                row_bg = "background: linear-gradient(90deg, #e8c9a8 0%, #b08050 100%);"
                rank_style = "color:#6b3a00"
                name_extra = "color:#4a2a00;text-shadow:0 1px 0 rgba(255,255,255,.5)"
                score_extra = "color:#5a3a00;text-shadow:0 1px 0 rgba(255,255,255,.5)"
            rows.append(
                f"<div class='lb-row' style='{row_bg}'><div class='lb-rank' style='{rank_style}'>{i:02d}</div>"
                f"<div class='lb-name' style='{name_extra}'>{_esc(name)}</div>"
                f"<div class='lb-score' style='{score_extra}'>{_esc(score_txt)}</div></div>"
            )
        rows_html = "".join(rows)
    else:
        rows_html = "<div class='lb-empty'>（暂无上榜数据）</div>"
    rows_panel = f"<div class='ody-panel lb-panel'><div class='ody-ribbon ody-panel-title'><span>排名</span></div>{rows_html}</div>"
    body = f"<div class='ody-ribbon ody-section-banner'><span>排行榜</span></div>{head_panel}{rows_panel}"
    h = 60 + 140 + max(len(entries), 1) * 56 + 40
    return _odyssey_shell(body, h)


def _path_max_txt(blocked: dict) -> str:
    """路径限制 → 3-2-3（每条路可升到的最高层数，BTD6 每路满级 5 层）。
    封锁值 -1 表示整路禁用 → 显示 0（与游戏/BTD6 API Explorer 一致）。"""
    def cap(p: int) -> str:
        n = int(blocked.get(p, 0) or 0)
        return "0" if n == -1 else str(max(0, 5 - n))
    return "-".join(cap(p) for p in (1, 2, 3))


def _monkey_cell(raw: str, is_hero: bool, mx, blocked: dict) -> str:
    """猴子限制网格中的一个格子：立绘 + 限购/路径角标（禁用的塔不进网格）。"""
    icon = _tower_icon(raw, is_hero)
    name = _tower_display_name(raw, is_hero)
    limited = isinstance(mx, (int, float)) and 0 < mx < 99
    if not icon:
        tags = []
        if limited:
            tags.append(f"×{int(mx)}")
        if blocked:
            tags.append(_path_max_txt(blocked))
        line = f"<div class='mk txt'>{_esc(name)}" \
               + (f"<br>{_esc('  '.join(tags))}" if tags else "") + "</div>"
        return f"<div class='mkwrap'>{line}</div>"
    cell = f"<div class='mk'><img src='{_esc(icon)}'/><div class='nm'>{_esc(name)}</div>"
    if limited:
        cell += f"<div class='lim'>×{int(mx)}</div>"
    if blocked:
        cell += f"<div class='pth'><span>{_path_max_txt(blocked)}</span></div>"
    cell += "</div>"
    return f"<div class='mkwrap'>{cell}</div>"


def _monkey_grid(towers: list) -> str:
    """渲染可用/受限猴子网格（禁用的塔直接移除；兼容规则 _towers 与远征 _availableTowers）。"""
    cells = []
    for t in towers or []:
        raw = str(t.get("tower") or "").strip()
        if not raw or raw == "ChosenPrimaryHero":
            continue
        mx = t.get("max")
        if mx == 0:
            continue  # 禁用：直接不显示
        blocked = {
            p: n for p in (1, 2, 3)
            if (n := int(t.get(f"path{p}NumBlockedTiers") or 0)) != 0
        }
        is_hero = bool(t.get("isHero"))
        limited = isinstance(mx, (int, float)) and 0 < mx < 99
        if not limited and not blocked and not (is_hero and mx):
            continue  # 无限制的普通塔不上图
        cells.append(_monkey_cell(raw, is_hero, mx, blocked))
    if not cells:
        return "<div class='empty'>本活动没有猴子限制</div>"
    return "<div class='mkgrid'>" + "".join(cells) + "</div>"


def _daily_monkey_grid(meta: dict) -> str:
    """每日挑战：全塔总览——官方 _towers 里有记录的塔带限制角标，
    未记录的塔表示无限制（正常可放、可满级），max=0 的塔灰化并标记禁用。"""
    restrictions = {}
    for t in meta.get("_towers") or []:
        raw = str(t.get("tower") or "").strip()
        if raw and raw != "ChosenPrimaryHero":
            restrictions[raw] = t
    constants = rushgen.load_constants()
    cells = []
    for name in constants["towersInOrder"]:
        entry = dict(restrictions.get(name) or {"tower": name})
        entry.setdefault("tower", name)
        try:
            if float(entry.get("max")) == 0:
                entry["_banned"] = True
        except (TypeError, ValueError):
            pass
        cells.append(_race_monkey_cell(entry))
    if not cells:
        return "<div class='mkgrid race-mkgrid'><div class='race-mk-fallback'>无</div></div>"
    return "<div class='mkgrid race-mkgrid' style='height:auto;overflow:visible;'>" + "".join(cells) + "</div>"

def _race_visible_towers(towers: list) -> list[dict]:
    """截图中的默认视图：显示所有可用/限购塔，隐藏 max=0 与占位英雄。"""
    visible = []
    for tower in towers or []:
        raw = str(tower.get("tower") or "").strip()
        if not raw or raw == "ChosenPrimaryHero":
            continue
        try:
            if float(tower.get("max")) == 0:
                continue
        except (TypeError, ValueError):
            pass
        visible.append(tower)
    return sorted(visible, key=lambda tower: _RACE_TOWER_ORDER_INDEX.get(
        str(tower.get("tower") or "").strip(), len(_RACE_TOWER_ORDER)
    ))


def _race_monkey_cell(tower: dict) -> str:
    raw = str(tower.get("tower") or "").strip()
    is_hero = bool(tower.get("isHero"))
    mx = tower.get("max")
    icon = _tower_icon(raw, is_hero)
    name = _tower_display_name(raw, is_hero)
    category = _tower_category(raw, is_hero)
    try:
        max_num = float(mx)
    except (TypeError, ValueError):
        max_num = None
    banned = bool(tower.get("_banned"))
    limited = max_num is not None and 0 < max_num < 99
    blocked = {
        p: n for p in (1, 2, 3)
        if (n := int(tower.get(f"path{p}NumBlockedTiers") or 0)) != 0
    }
    tags = []
    if limited:
        tags.append(f"×{int(max_num)}")
    if blocked:
        tags.append(_path_max_txt(blocked))
    if banned:
        tags.append("禁用")
    title = name + (f" · {' '.join(tags)}" if tags else "")
    grey = " style='filter:grayscale(.85);opacity:.82;'" if banned else ""
    if icon:
        cell = f"<div class='race-mk {category}' title='{_esc(title)}'{grey}>"
        cell += f"<img src='{_esc(icon)}' alt='{_esc(name)}'/>"
    else:
        cell = f"<div class='race-mk {category}' title='{_esc(title)}'{grey}>"
        cell += f"<div class='race-mk-fallback'>{_esc(name)}</div>"
    if banned:
        cell += f"<div class='race-path' style='background:#8f2f22;'>禁用</div>"
    else:
        if limited:
            cell += f"<div class='race-lim'>×{int(max_num)}</div>"
        if blocked:
            cell += f"<div class='race-path'>{_esc(_path_max_txt(blocked))}</div>"
    cell += "</div>"
    return f"<div class='race-mkwrap'>{cell}</div>"
def _race_monkey_grid(towers: list) -> str:
    cells = [_race_monkey_cell(tower) for tower in _race_visible_towers(towers)]
    if not cells:
        return "<div class='mkgrid race-mkgrid'><div class='race-mk-fallback'>无</div></div>"
    return "<div class='mkgrid race-mkgrid'>" + "".join(cells) + "</div>"


def _stat(label: str, value: str) -> str:
    return f"<div class='st'>{_esc(label)} <b>{_esc(value)}</b></div>"


def rules_html(col: dict) -> str:
    if col.get("empty"):
        body = ("<div class='race-topbar'></div>"
                f"<div class='race-content'><div class='race-map-empty'>{_esc(col['empty'])}</div></div>")
        return _race_shell(body, 330)
    meta = col["meta"]
    name = (meta.get("name") or "").strip()
    diff = cn(meta.get("difficulty"), DIFFICULTY_CN)
    mode = cn(meta.get("mode"), MODE_CN)
    scoring = col.get("scoring_cn") or ""
    subtitle_parts = [diff, mode, scoring]
    subtitle = " - ".join(part for part in subtitle_parts if part)
    side_img = col.get("side_img") or ""
    ev = col.get("ev")
    map_img = col.get("map_img") or ""
    max_towers = int(meta.get("maxTowers") or 0)
    towers_cap = "无限制" if max_towers >= 9999 else f"{max_towers:,}"
    paragon_limit = int(meta.get("maxParagons") or 0)
    boss_label = "首领事件" if side_img else "竞速事件"
    boss_asset = _boss_event_asset(ev) if side_img else ""
    custom_round_sets = _custom_round_sets(meta)

    def stat(icon: str, fallback: str, label: str, value: str = "") -> str:
        value_html = f"<div class='race-stat-value'>{_esc(value)}</div>" if value else ""
        return ("<div class='race-stat'><div class='race-stat-icon-cell'>"
                f"{_race_ui_img(icon, fallback, 'race-stat-icon')}"
                f"</div><div class='race-stat-copy'><div class='race-stat-label'>{_esc(label)}</div>"
                f"{value_html}</div></div>")

    stat_left = "".join([
        stat("cash.png", "🪙", "初始资金", f"{int(meta.get('startingCash') or 0):,}"),
        stat("heart.png", "❤", "初始生命", f"{int(meta.get('lives') or 0):,}"),
        stat("heart.png", "❤", "最大生命", f"{int(meta.get('maxLives') or 0):,}"),
        stat(boss_asset, "🐒" if side_img else "⚑", boss_label),
    ])
    stat_right = "".join([
        stat("start-round.png", "▶", "开始回合", str(int(meta.get('startRound') or 0))),
        stat("end-round.png", "⏭", "结束回合", str(int(meta.get('endRound') or 0))),
        stat("monkey-cap.png", "🐒", "最大猴子", towers_cap),
        stat("fastest-time.png", "⏱", "最快用时"),
    ])
    emblem = _race_emblem(ev, side_img)
    title = _race_title(name, ev, side_img)
    time_line = _race_time_line(ev)
    time_html = f"<div class='race-time'>{_esc(time_line)}</div>" if time_line else ""
    body = ("<div class='race-topbar'><div class='race-head'>"
            f"<div class='race-emblem-cell'><div class='race-emblem'>{emblem}</div></div>"
            f"<div class='race-title-cell'><div class='race-title'>{_esc(title)}</div>"
            f"<div class='race-subtitle'>{_esc(subtitle)}</div>"
            f"{time_html}</div>"
            "</div></div>")
    map_img_html = (f"<img src='{_esc(map_img)}' alt='{_esc(meta.get('map') or 'map')}'/>" if map_img
                    else "<div class='race-map-empty'>🗺</div>")
    body += ("<div class='race-content'><div class='race-layout'>"
             f"<div class='race-map'>{map_img_html}</div>"
             f"<div class='race-stats-cell'><div class='race-stats'><div class='race-stat-col'>{stat_left}</div>"
             f"<div class='race-stat-col'>{stat_right}</div></div></div></div>"
              "<div class='race-monkey-section'><div class='race-options'>"
              "<div class='race-available'>可用猴子：</div></div>"
              f"{_daily_monkey_grid(meta) if str(meta.get('id') or '').startswith('rot') else _race_monkey_grid(meta.get('_towers'))}</div>")

    modifier_html = _race_modifier_html(meta.get("_bloonModifiers"))
    custom_round_details = _custom_round_details(meta)
    custom_rule_item = ""
    paragon_rule_class = "race-rule-item race-rule-single"
    if custom_round_sets:
        custom_rule_item = ("<div class='race-rule-item'>"
                            f"<div class='race-rule-icon'>{_race_ui_img('custom-rounds.png', '❓', 'race-rule-icon-img')}</div>"
                            "<div class='race-rule-copy'>自定义回合</div></div>")
        paragon_rule_class = "race-rule-item"
    bottom = ("<div class='race-bottom'><div class='race-bottom-left'><div class='race-bottom-panel'>"
              "<div class='race-bottom-title'>强化</div>"
              f"{modifier_html}</div></div>"
              "<div class='race-bottom-right'><div class='race-bottom-panel'>"
              "<div class='race-bottom-title'>规则</div><div class='race-rule-row'>"
              f"{custom_rule_item}<div class='{paragon_rule_class}'>"
              f"<div class='race-rule-icon'>{_race_ui_img('paragon.png', '◉', 'race-rule-icon-img')}</div>"
              f"<div class='race-rule-copy'>神级猴上限<br><span class='race-limit-value'>{paragon_limit}</span></div>"
              "</div></div></div></div></div>")
    if custom_round_sets:
        custom_text = "、".join(custom_round_sets)
        custom_group_line = f"<div class='race-round-set-name'>启用回合组：{_esc(custom_text)}</div>"
        custom_detail_lines = "".join(
            f"<div class='race-round-line'><span class='race-round-wave'>{_esc(wave)}</span>"
            f"<span class='race-round-desc'>：{_round_detail_desc_html(description)}</span></div>"
            for wave, description in custom_round_details
        )
        bottom += ("<div class='race-custom-panel'><div class='race-custom-title'>自定义回合</div>"
                   "<div class='race-custom-body'>"
                   f"<div class='race-custom-icon'>{_race_ui_img('custom-rounds.png', '❓', 'race-custom-icon-img')}</div>"
                   f"<div class='race-custom-copy'>{custom_group_line}"
                   f"<div class='race-round-lines'>{custom_detail_lines}</div></div>"
                   "</div></div>")
    body += bottom
    visible_count = len(_race_visible_towers(meta.get("_towers")))
    grid_rows = max(3, -(-visible_count // 11))
    compat = _rules_compat_html(meta, col.get("prefix") or "", scoring, ev)
    body += compat
    custom_panel_height = 0
    if custom_round_sets:
        # 面板高度跟随“回合组名称 + 明细行”增长，避免四行内容被截图裁掉。
        custom_line_count = len(custom_round_details) + 1
        custom_panel_height = max(92, 14 + 20 + max(54, custom_line_count * 30)) + 8
    # race-monkey-section 新增了内边距、描边和底部阴影，需要同步计入画布高度。
    frame_height = (822 + max(0, grid_rows - 3) * 84
                    + (15 + custom_panel_height + 1 if custom_panel_height else 0))
    return _race_shell(body, frame_height + 8)


def _rules_compat_html(meta: dict, prefix: str, scoring: str, ev: dict | None) -> str:
    """保留旧卡片中的中文可检索信息，不改变新卡片的视觉布局。"""
    lines = [
        "猴子限制",
        f"初始资金 {int(meta.get('startingCash') or 0):,}",
        f"初始生命 {int(meta.get('lives') or 0):,}",
        f"最快用时 {scoring or '—'}",
        "气球强化 " + ("；".join(bloon_mod_lines(meta.get("_bloonModifiers"))) or "默认"),
        "禁用项 " + ("、".join(label for key, label in FLAG_LABELS if meta.get(key)) or "无"),
    ]
    for tower in _race_visible_towers(meta.get("_towers")):
        raw = str(tower.get("tower") or "").strip()
        name = _tower_display_name(raw, bool(tower.get("isHero")))
        tags = []
        try:
            max_num = float(tower.get("max"))
        except (TypeError, ValueError):
            max_num = None
        if max_num is not None and 0 < max_num < 99:
            tags.append(f"×{int(max_num)}")
        blocked = {
            p: n for p in (1, 2, 3)
            if (n := int(tower.get(f"path{p}NumBlockedTiers") or 0)) != 0
        }
        if blocked:
            tags.append(_path_max_txt(blocked))
        lines.append(name + (" " + " ".join(tags) if tags else ""))
    if ev:
        state = _state_of(ev, bucket_now())
        lines.extend([_STATE_TXT[state], _fmt_range(ev)])
    return f"<div class='compat-data'>{_esc('；'.join(lines))}</div>"


def maps_html(col: dict) -> str:
    """自制地图：远征纸张 + 岛屿行风格。"""
    entries = col["entries"]
    label = col["label"]
    header = f"<div class='ody-ribbon ody-section-banner'><span>自制地图 · {_esc(label)} Top{len(entries)}</span></div>"
    if not entries:
        return _odyssey_shell(header + "<div class='ody-panel ov-panel'><div class='lb-empty'>（暂无地图数据）</div></div>", 180)
    rows = []
    for i, name, created, img, plays, upvotes in entries:
        thumb = (f"<img class='ody-map-img' src='{_esc(img)}'/>" if img else "<div class='ody-map-empty'>🗺</div>")
        rows.append(
            f"<div class='ody-map-row'><div class='ody-map-img-cell'>{thumb}</div>"
            f"<div class='ody-map-info'>"
            f"<div class='ov-name'>#{i:02d} {_esc(name)}</div>"
            f"<div class='ody-map-meta'>"
            f"<div class='ody-map-meta-item'><span class='ody-coin'>▶</span> 游玩 {plays:,}</div>"
            f"<div class='ody-map-meta-item'><span class='ody-diff'>♥</span> 点赞 {upvotes:,}</div>"
            f"<div class='ody-map-meta-item'>{_esc(created)}</div>"
            f"</div></div></div>"
        )
    body = header + f"<div class='ody-panel map-panel'>{''.join(rows)}</div>"
    h = 60 + max(len(entries), 1) * 125 + 40
    return _odyssey_shell(body, min(h, 2600))


def help_html() -> str:
    """帮助菜单卡片：按分组列出全部命令。"""
    panels = []
    total_h = 40
    for title, rows in HELP_GROUPS:
        rows_html = "".join(
            f"<div class='hrow'><span class='chip'>{_esc(cmd)}</span>"
            f"<div class='hdesc'>{_esc(desc)}</div></div>"
            for cmd, desc in rows
        )
        panels.append(
            f"<div class='panel'><div class='ptitle' "
            f"style='border-left:8px solid #7b68c8;padding-left:14px'>"
            f"{_esc(title)}</div>{rows_html}</div>"
        )
        total_h += 60 + len(rows) * 106
    return _shell("".join(panels), total_h + 40)


# ---------------- 渲染（内容哈希缓存 + 线程池 + 全局信号量） ----------------


def _render_card_sync(prefix: str, html: str) -> str:
    # 用完整 HTML 的指纹作为数据/版式版本号：命令每次仍会先请求并整理
    # API 数据，只有指纹变化或本地 PNG 不存在时才重新渲染。
    persistent = prefix in PERSISTENT_CARD_PREFIXES
    card_dir = os.path.join(CACHE_DIR, "cards") if persistent else CACHE_DIR
    os.makedirs(card_dir, exist_ok=True)
    key = hashlib.md5(html.encode("utf-8")).hexdigest()[:20]
    path = os.path.join(card_dir, f"{prefix}_{key}.png")
    if os.path.isfile(path):
        return path  # 同内容直接复用，持久卡片不会因普通 TTL 被清理
    # common.render_html_to_png 自带 TTL 清理；渲染到临时子目录，避免它
    # 把 cards/ 中长期保留的 PNG 当作普通临时文件清掉。
    render_dir = os.path.join(CACHE_DIR, ".render")
    os.makedirs(render_dir, exist_ok=True)
    tmp = render_html_to_png(html, prefix, render_dir, max_age=CARD_MAX_AGE, dpi=CARD_DPI)
    os.replace(tmp, path)
    if persistent:
        _prune_cache_files(card_dir, ".png", PERSISTENT_CARD_FILES, PERSISTENT_CARD_BYTES, {path})
    else:
        _prune_cache_files(CACHE_DIR, ".png", MAX_CARD_FILES, MAX_CARD_BYTES, {path})
    return path


async def _render_card(prefix: str, html_fn) -> str:
    """构建 HTML 与渲染全程放在 worker 线程，经全局信号量串行化，避免阻塞事件循环。"""

    def _job() -> str:
        return _render_card_sync(prefix, html_fn())

    async with RENDER_SEM:
        return await asyncio.to_thread(_job)


async def _send_card(matcher, prefix: str, html_fn, text_fn) -> None:
    try:
        path = await _render_card(prefix, html_fn)
    except Exception:
        _logger.warning("BTD6 卡片渲染失败，回退文本消息", exc_info=True)
        await matcher.finish(MessageSegment.text(text_fn()))
    await matcher.finish(MessageSegment.image(Path(path).as_uri()))


async def _finish_multi_cards(matcher, cards: list[tuple[str, object, object]]) -> None:
    """多卡片流式发送：逐张渲染，前 N-1 张 send、最后一张 finish；
    单张渲染失败回退该张的文本，不影响其余卡片。
    cards: [(prefix, html_fn, text_fn), ...]"""
    for idx, (prefix, html_fn, text_fn) in enumerate(cards):
        last = idx == len(cards) - 1
        try:
            path = await _render_card(prefix, html_fn)
        except Exception:
            _logger.warning("BTD6 多卡渲染失败，回退文本 prefix=%s", prefix, exc_info=True)
            msg = MessageSegment.text(text_fn())
            await (matcher.finish(msg) if last else matcher.send(msg))
            continue
        msg = MessageSegment.image(Path(path).as_uri())
        await (matcher.finish(msg) if last else matcher.send(msg))


# ---------------- 活动历史归档（NK 各列表只保留近几期，本地落盘补长历史） ----------------

HISTORY_FILE = os.path.join(os.path.dirname(__file__), "history.json")
HISTORY_MAX_PER_KIND = 500  # 每类最多归档期数（按 start 降序裁剪，单条 ~0.5KB，上限 ~1MB）
HISTORY_KIND_CN = {"race": "竞速", "boss": "Boss", "ct": "争夺领土",
                   "odyssey": "远征", "daily": "每日挑战"}
_history_lock = threading.RLock()


def _load_history() -> dict:
    data = load_json_state(HISTORY_FILE, _history_lock)
    return data if isinstance(data, dict) else {}


def _merge_history(kind: str, items: list) -> bool:
    """按 id 合并该类活动到归档；有新增返回 True。start 降序存储并裁剪上限。"""
    if not items:
        return False
    with _history_lock:
        data = _load_history()
        arc = data.get(kind) if isinstance(data.get(kind), list) else []
        by_id = {str(x.get("id") or ""): x for x in arc if isinstance(x, dict)}
        changed = False
        for it in items:
            if not isinstance(it, dict):
                continue
            iid = str(it.get("id") or "")
            if iid and iid not in by_id:
                by_id[iid] = it
                changed = True
        if not changed:
            return False
        merged = sorted(by_id.values(),
                        key=lambda x: int(x.get("start") or 0), reverse=True)[:HISTORY_MAX_PER_KIND]
        data[kind] = merged
        save_json_state(HISTORY_FILE, data, _history_lock)
        return True


async def _archive_events(data: dict | None = None) -> None:
    """预热顺带归档：把竞速/Boss/CT/远征/每日列表合并进本地 history.json，
    弥补 NK API 只保留近 3~16 期的限制（文件小、增量合并，失败不影响预热）。"""
    try:
        if data is None:
            data = await collect_overview()
        for kind, key in (("race", "races"), ("boss", "bosses"), ("ct", "cts")):
            items = data.get(key) or []
            if items:
                await _safe(asyncio.to_thread(_merge_history, kind, items))
        ody = await _safe(fetch_body(URL_ODYSSEY))
        if isinstance(ody, list) and ody:
            await _safe(asyncio.to_thread(_merge_history, "odyssey", ody))
        daily = await _safe(fetch_body(URL_DAILY))
        if isinstance(daily, list) and daily:
            await _safe(asyncio.to_thread(_merge_history, "daily", daily))
    except Exception:
        _logger.warning("BTD6 活动归档失败", exc_info=True)


# ---------------- 后台预热：数据 + 素材 + 热门卡片 ----------------

_prewarm_running = False


async def _prewarm_once() -> None:
    """周期预热（瘦身后只做两类事，全部容错）：
    1) 归档活动列表到 history.json（复用本轮 overview，仅额外拉远征/每日两个列表）；
    2) 仅当竞赛/Boss 进行中时预热榜单卡——分数分钟级变化且查询最频繁，值得周期渲染。
    总览/规则/远征/每日内容只在刷新点变化，内容哈希缓存保证"首查渲染、后续秒回"，无需预热。"""
    global _prewarm_running
    if _prewarm_running:
        return
    _prewarm_running = True
    try:
        data = await collect_overview()
        now = data["now"]
        await _safe(_archive_events(data))
        jobs = []
        race = _pick_section(data["races"], now)
        boss = _pick_section(data["bosses"], now)
        ct = _pick_section(data["cts"], now)
        # odyssey and rush via events
        rush_list = data.get("rush") or []
        rush_ev = _pick_section(rush_list, now) if rush_list else None
        ody_list = data.get("odysseys") or []
        ody_ev = _pick_section(ody_list, now) if ody_list else None
        if race:
            # 行数与用户默认查询参数一致，预热渲染才能命中缓存
            lb = await _safe(collect_leaderboard("race", "", LB_DEFAULT_ROWS))
            if lb and not lb.get("empty"):
                jobs.append(_render_card("btd6lb", lambda: leaderboard_html(lb)))
        if boss:
            blb = await _safe(collect_leaderboard("boss", "standard", LB_DEFAULT_ROWS))
            if blb and not blb.get("empty"):
                jobs.append(_render_card("btd6lb", lambda: leaderboard_html(blb)))
        if ct:
            # CT 榜单预热（个人榜）
            ct_lb = await _safe(collect_leaderboard("ct", "player", LB_DEFAULT_ROWS))
            if ct_lb and not ct_lb.get("empty"):
                jobs.append(_render_card("btd6lb", lambda: leaderboard_html(ct_lb)))
        if rush_ev:
            # 1:1 模仿 handle_odyssey：collect_rush → _rush_diff_html
            try:
                col = await collect_rush()
                if not col.get("empty"):
                    rush_html = _rush_diff_html(col)
                    jobs.append(_render_card("btd6rush", lambda: rush_html))
            except Exception:
                _logger.warning("BTD6 预热 Rush 卡渲染失败", exc_info=True)
        if ody_ev:
            # 远征三难度取当前其一预热
            try:
                ody_col = await collect_odyssey()
                # 取 easy 作为代表预热
                ody_html = odyssey_diff_html(ody_col, "easy", "简单")
                jobs.append(_render_card("btd6ody", lambda: ody_html))
            except Exception:
                _logger.warning("BTD6 预热远征卡渲染失败", exc_info=True)
        # 逐张渲染并在间隔让出信号量：用户查询优先于预热渲染
        for i, job in enumerate(jobs):
            await _safe(job)
            if i < len(jobs) - 1:
                await asyncio.sleep(1.0)
        # 帮助菜单纯静态，兜底再渲一次（首次 _btd6_warm_on_connect 失败时仍能补上）
        if not jobs:
            await _safe(_render_card("btd6help", help_html))
    except Exception:
        _logger.warning("BTD6 预热异常", exc_info=True)
    finally:
        _prewarm_running = False


@scheduler.scheduled_job("cron", hour=4, minute=0, id="btd6_prewarm",
                         timezone="Asia/Shanghai")
async def btd6_prewarm_job():
    """每天 04:00 固定预热：归档活动列表 + 预热进行中竞赛/Boss/CT/Rush/远征的热门卡片。
    由每周按需改为每天定点，确保跨周活动切换后首日内即有缓存。"""
    await _prewarm_once()


# 启动预热：连上 bot 后先跑一轮（榜单/归档即时可用），后续按 6 小时节奏
_register_warmup = getattr(get_driver(), "on_bot_connect", get_driver().on_startup)


@_register_warmup
async def _btd6_warm_on_connect(bot=None) -> None:
    await asyncio.sleep(5)  # 等 NapCat 连接稳定后再拉数据/归档/预热榜单
    # 帮助菜单是纯静态卡片，预先渲染到持久缓存目录，确保 .btd6 / .btd6帮助 首屏直接复用。
    # 与 _prewarm_once 并发：help 不依赖网络/信号量，但走同一 RENDER_SEM 串行化，所以放到
    # _prewarm_once 之后避免抢用户首查的渲染位。
    await _prewarm_once()
    try:
        await _render_card("btd6help", help_html)
        _logger.info("BTD6 帮助菜单已预渲染到本地")
    except Exception:
        _logger.warning("BTD6 帮助菜单预渲染失败", exc_info=True)


# ---------------- 活动刷新推送（群自动播报） ----------------
BTD6_PUSH_STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")
_BTD6_PUSH_LOCK = threading.RLock()
_BTD6_PUSH_KINDS = ("race", "boss", "ct", "odyssey", "daily", "rush")

def _load_push_state() -> dict:
    return load_json_state(BTD6_PUSH_STATE_FILE, _BTD6_PUSH_LOCK)

def _save_push_state(data: dict) -> None:
    save_json_state(BTD6_PUSH_STATE_FILE, data, _BTD6_PUSH_LOCK)

def _push_groups() -> set[int]:
    data = _load_push_state()
    groups = data.get("groups", []) if isinstance(data.get("groups"), list) else []
    out = set()
    for gid in groups:
        try:
            gid = int(gid)
        except (TypeError, ValueError):
            continue
        if gid > 0:
            out.add(gid)
    return out

def _last_pushed() -> dict:
    data = _load_push_state()
    lp = data.get("last_pushed", {}) if isinstance(data.get("last_pushed"), dict) else {}
    return {k: str(v) for k, v in lp.items() if k in _BTD6_PUSH_KINDS}

def _set_last_pushed(kind: str, ev_id: str) -> None:
    with _BTD6_PUSH_LOCK:  # 读改写全程持锁，避免并发推送互相覆盖记录
        data = load_json_state(BTD6_PUSH_STATE_FILE, _BTD6_PUSH_LOCK)
        lp = data.get("last_pushed", {}) if isinstance(data.get("last_pushed"), dict) else {}
        lp[kind] = str(ev_id)
        data["last_pushed"] = lp
        save_json_state(BTD6_PUSH_STATE_FILE, data, _BTD6_PUSH_LOCK)

def _push_change_group(group_id: int, enabled: bool) -> bool:
    with _BTD6_PUSH_LOCK:
        data = load_json_state(BTD6_PUSH_STATE_FILE, _BTD6_PUSH_LOCK)
        groups = data.get("groups", []) if isinstance(data.get("groups"), list) else []
        s = set()
        for gid in groups:
            try:
                s.add(int(gid))
            except (TypeError, ValueError):
                continue
        changed = (group_id not in s) if enabled else (group_id in s)
        if enabled:
            s.add(group_id)
        else:
            s.discard(group_id)
        if changed:
            data["groups"] = sorted(s)
            save_json_state(BTD6_PUSH_STATE_FILE, data, _BTD6_PUSH_LOCK)
        return changed

async def _btd6_push_kind(kind: str) -> None:
    """精准采样：仅检查单类活动是否刚刷新，减少 99% 空轮询（原每5分钟全量检查 288次/日 → 现仅刷新点后3次/类）。"""
    groups = _push_groups()
    if not groups:
        return
    try:
        get_bot()  # 无已连接 bot 时直接跳过本轮采样
    except Exception:
        return
    now = bucket_now()
    real_now = int(time.time() * 1000)  # 窗口比较用真实时间：start 带秒级偏移时桶取整会恒判"未在窗口内"而漏推
    # 12 分钟窗口：:10 采样点距刷新点恰为 10min，10min 窗口会漏掉第三次容错采样
    window_ms = 12 * 60 * 1000
    last = _last_pushed()
    # 单类检查（只取列表判断 id/start，重量级元数据/素材留到确认推送后再拉）
    try:
        ev = None
        if kind == "race":
            data = await collect_overview()
            ev = _pick_section(data["races"], now)
        elif kind == "boss":
            data = await collect_overview()
            ev = _pick_section(data["bosses"], now)
        elif kind == "ct":
            data = await collect_overview()
            ev = pick_active(data["cts"], now) or pick_next(data["cts"], now) or fallback_latest(data["cts"])
        elif kind == "odyssey":
            items = await _safe(fetch_body(URL_ODYSSEY)) or []
            ev = pick_active(items, now) or pick_next(items, now) or fallback_latest(items)
        elif kind == "rush":
            data = await collect_overview()
            ev = _pick_section(data.get("rush") or [], now)
        elif kind == "daily":
            items = await _safe(fetch_body(URL_DAILY)) or []
            ev = next((x for x in items if str(x.get("name") or "").startswith("Standard")), None)
        if not isinstance(ev, dict):
            return
        ev_id = str(ev.get("id") or ev.get("name") or "")
        label = str(ev.get("name") or "")
        if not ev_id or last.get(kind) == ev_id:
            return
        start = int(ev.get("start") or 0)
        if kind == "daily":
            # daily 无 start，以 id 变化即视为刷新
            pass
        elif not start or not (0 <= real_now - start < window_ms):
            # 非窗口期内且非首次配置则跳过；首次配置 30 分钟内补发
            if last.get(kind) or not start or not (0 <= real_now - start < 30 * 60 * 1000):
                return
        # 确认要推送后才拉取重量级数据并渲染发送
        await _btd6_push_single(kind, ev, ev_id, label, groups)
    except Exception:
        _logger.warning("BTD6 精准推送 kind=%s 失败", kind, exc_info=True)

async def _btd6_push_single(kind: str, ev: dict, ev_id: str, label: str, groups: set[int]) -> None:
    try:
        bot = get_bot()
    except Exception:
        return
    try:
        if kind in ("race", "ct", "rush"):
            data = await collect_overview()
            path = await _render_card("btd6ov", lambda: overview_html(data))
            text = f"🎮 BTD6 { {'race':'竞速','ct':'争夺领土','rush':'Boss Rush'}[kind] }已刷新：{label}" if label else f"🎮 BTD6 {kind} 已刷新"
        elif kind == "odyssey":
            col = await collect_odyssey()
            data = await collect_overview()
            path = await _render_card("btd6ov", lambda: overview_html(data))
            text = f"🏰 远征已刷新：{label}" if label else "🏰 远征已刷新"
            sent_any = False
            for d, lab in _ODYSSEY_DIFFS:
                try:
                    p = await _render_card("btd6ody", lambda d=d, lab=lab: odyssey_diff_html(col, d, lab))
                    for gid in groups:
                        try:
                            await bot.send_group_msg(group_id=gid, message=MessageSegment.image(Path(p).as_uri()))
                            sent_any = True
                            await asyncio.sleep(0.6)
                        except Exception:
                            _logger.warning("BTD6 远征分图推送到群 %s 失败", gid, exc_info=True)
                except Exception:
                    _logger.warning("BTD6 远征 %s 渲染失败", lab, exc_info=True)
            for gid in groups:
                try:
                    await bot.send_group_msg(group_id=gid, message=MessageSegment.text(text) + MessageSegment.image(Path(path).as_uri()))
                    sent_any = True
                    await asyncio.sleep(0.5)
                except Exception:
                    _logger.warning("BTD6 推送到群 %s 失败 kind=%s", gid, kind, exc_info=True)
            if sent_any:
                # 分图与总览任一发送成功即标记，避免部分成功时下轮重发整组刷屏；全部失败时保留待推状态
                _set_last_pushed(kind, ev_id)
            _logger.info("BTD6 精准推送 %s %s 到 %d 群（成功标记：%s）", kind, ev_id, len(groups), sent_any)
            return
        elif kind == "daily":
            # 每日普通+高级双版本一起推送
            pushed_any = False
            for adv in (False, True):
                col = await _safe(collect_daily(adv))
                if not col or col.get("empty"):
                    continue
                path = await _render_card("btd6dailya" if adv else "btd6daily", lambda c=col: rules_html(c))
                text = f"📅 每日挑战已刷新·{_daily_prefix(label, adv)}"
                for gid in groups:
                    try:
                        await bot.send_group_msg(group_id=gid, message=MessageSegment.text(text) + MessageSegment.image(Path(path).as_uri()))
                        pushed_any = True
                        await asyncio.sleep(0.6)
                    except Exception:
                        _logger.warning("BTD6 推送到群 %s 失败 kind=%s adv=%s", gid, kind, adv, exc_info=True)
            if pushed_any:
                # 至少一个群发送成功才标记已推送；全部失败时保留待推状态，下次采样重试
                _set_last_pushed(kind, ev_id)
                _logger.info("BTD6 精准推送 %s %s 到 %d 群", kind, ev_id, len(groups))
            return
        elif kind == "boss":
            # Boss 标准+精英双版本：先推总览，再推两套详细规则
            data = await collect_overview()
            path = await _render_card("btd6ov", lambda: overview_html(data))
            text = f"🎮 BTD6 Boss已刷新：{label}" if label else "🎮 BTD6 Boss 已刷新"
            sent_any = False
            for gid in groups:
                try:
                    await bot.send_group_msg(group_id=gid, message=MessageSegment.text(text) + MessageSegment.image(Path(path).as_uri()))
                    sent_any = True
                    await asyncio.sleep(0.5)
                except Exception:
                    _logger.warning("BTD6 推送到群 %s 失败 kind=%s", gid, kind, exc_info=True)
            for variant, vlab in [("standard", "标准"), ("elite", "精英")]:
                col = await _safe(collect_rules("boss", variant))
                if not col or col.get("empty"):
                    continue
                p2 = await _render_card(f"btd6rule_{variant}", lambda c=col: rules_html(c))
                vt = f"Boss·{vlab}规则：{label}" if label else f"Boss·{vlab}规则"
                for gid in groups:
                    try:
                        await bot.send_group_msg(group_id=gid, message=MessageSegment.text(vt) + MessageSegment.image(Path(p2).as_uri()))
                        sent_any = True
                        await asyncio.sleep(0.6)
                    except Exception:
                        _logger.warning("BTD6 Boss %s 推送到群 %s 失败", vlab, gid, exc_info=True)
            if sent_any:
                # 全部发送失败时不标记，下次采样窗口重试
                _set_last_pushed(kind, ev_id)
                _logger.info("BTD6 精准推送 %s %s 到 %d 群", kind, ev_id, len(groups))
            return
        else:
            return
        sent_any = False
        for gid in groups:
            try:
                await bot.send_group_msg(group_id=gid, message=MessageSegment.text(text) + MessageSegment.image(Path(path).as_uri()))
                sent_any = True
                await asyncio.sleep(0.5)
            except Exception:
                _logger.warning("BTD6 推送到群 %s 失败 kind=%s", gid, kind, exc_info=True)
        if sent_any:
            # 至少一个群发送成功才标记已推送；全部失败时下次采样重试
            _set_last_pushed(kind, ev_id)
        _logger.info("BTD6 精准推送 %s %s 到 %d 群（成功标记：%s）", kind, ev_id, len(groups), sent_any)
    except Exception:
        _logger.warning("BTD6 推送 kind=%s 失败", kind, exc_info=True)

# 精准采样：已知刷新点后 0/5/10 分钟各一次（3 次容错，覆盖 API 延迟）
# 竞速 周四10:00 持续97h
@scheduler.scheduled_job("cron", hour=10, minute=0, id="btd6_push_race_0", timezone="Asia/Shanghai")
async def btd6_push_race_0(): await _btd6_push_kind("race")
@scheduler.scheduled_job("cron", hour=10, minute=5, id="btd6_push_race_5", timezone="Asia/Shanghai")
async def btd6_push_race_5(): await _btd6_push_kind("race")
@scheduler.scheduled_job("cron", hour=10, minute=10, id="btd6_push_race_10", timezone="Asia/Shanghai")
async def btd6_push_race_10(): await _btd6_push_kind("race")
# Boss 周五10:00 持续121h
@scheduler.scheduled_job("cron", hour=10, minute=0, id="btd6_push_boss_0", timezone="Asia/Shanghai")
async def btd6_push_boss_0(): await _btd6_push_kind("boss")
@scheduler.scheduled_job("cron", hour=10, minute=5, id="btd6_push_boss_5", timezone="Asia/Shanghai")
async def btd6_push_boss_5(): await _btd6_push_kind("boss")
@scheduler.scheduled_job("cron", hour=10, minute=10, id="btd6_push_boss_10", timezone="Asia/Shanghai")
async def btd6_push_boss_10(): await _btd6_push_kind("boss")
# CT 周二06:00 持续168h（双周刷新，样本含周三08:00 特殊场，兼顾）+ 周三08:00 兜底
@scheduler.scheduled_job("cron", hour=6, minute=0, id="btd6_push_ct_0", timezone="Asia/Shanghai")
async def btd6_push_ct_0(): await _btd6_push_kind("ct")
@scheduler.scheduled_job("cron", hour=6, minute=5, id="btd6_push_ct_5", timezone="Asia/Shanghai")
async def btd6_push_ct_5(): await _btd6_push_kind("ct")
@scheduler.scheduled_job("cron", hour=6, minute=10, id="btd6_push_ct_10", timezone="Asia/Shanghai")
async def btd6_push_ct_10(): await _btd6_push_kind("ct")
@scheduler.scheduled_job("cron", hour=8, minute=0, id="btd6_push_ct_w0", timezone="Asia/Shanghai")
async def btd6_push_ct_w0(): await _btd6_push_kind("ct")
@scheduler.scheduled_job("cron", hour=8, minute=5, id="btd6_push_ct_w5", timezone="Asia/Shanghai")
async def btd6_push_ct_w5(): await _btd6_push_kind("ct")
@scheduler.scheduled_job("cron", hour=8, minute=10, id="btd6_push_ct_w10", timezone="Asia/Shanghai")
async def btd6_push_ct_w10(): await _btd6_push_kind("ct")
# 远征 周三10:00 持续144h
@scheduler.scheduled_job("cron", hour=10, minute=0, id="btd6_push_ody_0", timezone="Asia/Shanghai")
async def btd6_push_ody_0(): await _btd6_push_kind("odyssey")
@scheduler.scheduled_job("cron", hour=10, minute=5, id="btd6_push_ody_5", timezone="Asia/Shanghai")
async def btd6_push_ody_5(): await _btd6_push_kind("odyssey")
@scheduler.scheduled_job("cron", hour=10, minute=10, id="btd6_push_ody_10", timezone="Asia/Shanghai")
async def btd6_push_ody_10(): await _btd6_push_kind("odyssey")

# Boss Rush 隔周三 22:00 UTC -> 周四 06:00 CST，与 CT 交替，改为每天 06:00 定点检测
@scheduler.scheduled_job("cron", hour=6, minute=0, id="btd6_push_rush_0", timezone="Asia/Shanghai")
async def btd6_push_rush_0(): await _btd6_push_kind("rush")
@scheduler.scheduled_job("cron", hour=6, minute=5, id="btd6_push_rush_5", timezone="Asia/Shanghai")
async def btd6_push_rush_5(): await _btd6_push_kind("rush")
@scheduler.scheduled_job("cron", hour=6, minute=10, id="btd6_push_rush_10", timezone="Asia/Shanghai")
async def btd6_push_rush_10(): await _btd6_push_kind("rush")

# 每日 16:00 持续24h（按用户指定，普通+高级双版本）
@scheduler.scheduled_job("cron", hour=16, minute=0, id="btd6_push_daily_0", timezone="Asia/Shanghai")
async def btd6_push_daily_0(): await _btd6_push_kind("daily")
@scheduler.scheduled_job("cron", hour=16, minute=5, id="btd6_push_daily_5", timezone="Asia/Shanghai")
async def btd6_push_daily_5(): await _btd6_push_kind("daily")
@scheduler.scheduled_job("cron", hour=16, minute=10, id="btd6_push_daily_10", timezone="Asia/Shanghai")
async def btd6_push_daily_10(): await _btd6_push_kind("daily")


# ---------------- 命令处理 ----------------

help_cmd = on_command("btd6", priority=5, block=True)
help_alias_cmd = on_command("btd6帮助", priority=5, block=True)
events_cmd = on_command("btd6活动", priority=5, block=True)
ct_cmd = on_command("btd6领土", priority=5, block=True)
rush_cmd = on_command("btd6冲刺", priority=5, block=True)
lb_cmd = on_command("btd6排行", priority=5, block=True)
rules_cmd = on_command("btd6竞速", priority=5, block=True)
maps_cmd = on_command("btd6地图", priority=5, block=True)
daily_cmd = on_command("btd6每日", priority=5, block=True)
odyssey_cmd = on_command("btd6远征", priority=5, block=True)
player_cmd = on_command("btd6玩家", priority=5, block=True)
push_on_cmd = on_command("btd6推送开启", priority=5, block=True)
push_off_cmd = on_command("btd6推送关闭", priority=5, block=True)
push_status_cmd = on_command("btd6推送状态", priority=5, block=True)
hist_cmd = on_command("btd6历史", priority=5, block=True)
prewarm_cmd = on_command("btd6预热", priority=5, block=True)


@help_cmd.handle()
async def handle_help(event: MessageEvent):
    await _enforce_cooldown(help_cmd, event, "help")
    # 兼容 ".btd6 CT"（含空格）直接查看争夺领土/活动总览
    _plain = event.get_plaintext().strip().lower()
    _arg = _plain.replace(".btd6", "", 1).strip() if _plain.startswith(".btd6") else _plain
    if _arg in {"ct", "领土", "争夺", "争夺领土"}:
        try:
            data = await collect_overview()
        except Exception:
            _logger.exception("BTD6 CT 总览获取失败")
            await help_cmd.finish("⚠️ 获取 BTD6 活动信息失败，请稍后再试")
        await _send_card(help_cmd, "btd6ov", lambda: overview_html(data), lambda: overview_text(data))
        return
    await _send_card(help_cmd, "btd6help", help_html, lambda: HELP_TEXT)


@help_alias_cmd.handle()
async def handle_help_alias(event: MessageEvent):
    await _enforce_cooldown(help_alias_cmd, event, "help")
    await _send_card(help_alias_cmd, "btd6help", help_html, lambda: HELP_TEXT)


@events_cmd.handle()
async def handle_events(event: MessageEvent):
    await _enforce_cooldown(events_cmd, event, "events")
    try:
        data = await collect_overview()
    except Exception:
        _logger.exception("BTD6 活动总览获取失败")
        await events_cmd.finish("⚠️ 获取 BTD6 活动信息失败，请稍后再试")
    await _send_card(events_cmd, "btd6ov", lambda: overview_html(data), lambda: overview_text(data))


@ct_cmd.handle()
async def handle_ct(event: MessageEvent):
    await _enforce_cooldown(ct_cmd, event, "events")
    try:
        cts = await fetch_body(URL_CT)
        ev = _pick_section(cts, bucket_now()) or (cts[0] if cts else None)
        if not ev:
            await ct_cmd.finish("当前没有争夺领土活动")
        tiles = []
        if ev.get("tiles"):
            try:
                tiles_data = await fetch_body(ev["tiles"])
                if isinstance(tiles_data, dict) and "tiles" in tiles_data:
                    tiles = tiles_data["tiles"]
                elif isinstance(tiles_data, list):
                    tiles = tiles_data
            except Exception:
                tiles = []
        now = bucket_now()
        html = _ct_html(ev, tiles, now)
        text = f"争夺领土 {ev.get('id')} | {_fmt_range(ev)} | {_STATE_TXT[_state_of(ev, now)]} | 个人 {int(ev.get('totalScores_player') or 0):,} / 战队 {int(ev.get('totalScores_team') or 0):,} | 领地 {len(tiles)} 块"
    except Exception as e:
        # 无活动时上方 finish() 抛出的 FinishedException 不能当错误吞掉
        from nonebot.exception import FinishedException
        if isinstance(e, FinishedException):
            raise
        _logger.exception("BTD6 CT 获取失败")
        await ct_cmd.finish("⚠️ 获取争夺领土失败，请稍后再试")
    await _send_card(ct_cmd, "btd6ct", lambda: html, lambda: text)


@rush_cmd.handle()
async def handle_rush(event: MessageEvent):
    """模仿 handle_odyssey：fetch → collect → _send_card，使用 _rush_diff_html（1:1 odyssey_diff_html）。"""
    await _enforce_cooldown(rush_cmd, event, "rush")
    try:
        col = await collect_rush()
    except Exception:
        _logger.exception("Boss Rush 获取失败")
        await rush_cmd.finish("⚠️ 获取 Boss Rush 失败，请稍后再试")
        return
    if col.get("empty"):
        await rush_cmd.finish(col["empty"])
    await _send_card(rush_cmd, "btd6rush", lambda: _rush_diff_html(col), lambda: _rush_text(col))


@lb_cmd.handle()
async def handle_leaderboard(event: MessageEvent):
    await _enforce_cooldown(lb_cmd, event, "leaderboard")
    tokens = event.get_plaintext().split()[1:]
    kind = parse_kind(tokens)
    if kind is None:
        await lb_cmd.finish(LB_USAGE)
    page = parse_lb_page(tokens)
    rank = parse_lb_rank(tokens) if page is None else None

    # 排名查询：数字直接视为名次，返回该名次玩家的档案
    if rank is not None:
        rank = max(1, min(rank, LB_MAX_RANK))
        has_variant_word = any(
            t.lower() in ELITE_WORDS or t.lower() in STANDARD_WORDS or t.lower() in PLAYER_WORDS or t.lower() in TEAM_WORDS
            for t in tokens
        )
        # Boss/CT 未显式指定子榜时，双榜各取一名玩家一起返回
        if kind in ("boss", "ct") and not has_variant_word:
            variants = {"boss": ("standard", "elite"), "ct": ("player", "team")}[kind]
            cards = []
            for variant in variants:
                try:
                    entry = await fetch_rank_entry(kind, variant, rank)
                    if not entry:
                        continue
                    pid = _extract_player_id(str(entry.get("profile") or ""))
                    if not pid:
                        _logger.warning("排名 %s 的记录缺少 profile，跳过 variant=%s", rank, variant)
                        continue
                    col = await collect_player(pid)
                    if col.get("empty"):
                        continue
                    cards.append((
                        f"btd6pl_rank_{variant}",
                        lambda c=col: player_html(c),
                        lambda c=col, v=variant: f"第 {rank} 名（{v}）· " + player_text(c),
                    ))
                except Exception:
                    _logger.exception("BTD6 排名玩家获取失败 kind=%s variant=%s rank=%s", kind, variant, rank)
            if not cards:
                await lb_cmd.finish(f"⚠️ 未找到第 {rank} 名的玩家（排行榜可能不足 {rank} 人或档案缺失）")
            await _finish_multi_cards(lb_cmd, cards)
            return
        # 单榜单排名查询
        variant = parse_variant(tokens, {"boss": "standard", "ct": "player"}.get(kind, ""))
        try:
            entry = await fetch_rank_entry(kind, variant, rank)
            if not entry:
                await lb_cmd.finish(f"⚠️ 未找到第 {rank} 名的玩家（排行榜可能不足 {rank} 人）")
            pid = _extract_player_id(str(entry.get("profile") or ""))
            if not pid:
                # displayName 为 NK API 外部字段且玩家可自设昵称（防 CQ 码注入）：MessageSegment.text 包裹
                await lb_cmd.finish(
                    f"⚠️ 第 {rank} 名玩家 "
                    + MessageSegment.text(str(entry.get("displayName") or "未知"))
                    + " 的档案链接缺失，无法查询"
                )
            col = await collect_player(pid)
            if col.get("empty"):
                await lb_cmd.finish(col["empty"])
        except Exception as e:
            # 已 finish 的异常直接抛出
            from nonebot.exception import FinishedException
            if isinstance(e, FinishedException):
                raise
            _logger.exception("BTD6 排名玩家获取失败 kind=%s variant=%s rank=%s", kind, variant, rank)
            await lb_cmd.finish("⚠️ 获取该名次玩家信息失败，请稍后再试")
        await _send_card(lb_cmd, "btd6pl_rank", lambda: player_html(col), lambda: f"第 {rank} 名 · " + player_text(col))
        return

    # 分页查询：P2 / P 2 / p2
    if page is not None:
        page = max(1, min(page, LB_MAX_PAGE))
        variants = {"boss": ("standard", "elite"), "ct": ("player", "team")}.get(kind)
        if variants:
            has_variant_word = any(
                t.lower() in ELITE_WORDS or t.lower() in STANDARD_WORDS or t.lower() in PLAYER_WORDS or t.lower() in TEAM_WORDS
                for t in tokens
            )
            if has_variant_word:
                variant = parse_variant(tokens, {"boss": "standard", "ct": "player"}[kind])
                try:
                    col = await collect_leaderboard_page(kind, variant, page)
                except Exception:
                    _logger.exception("BTD6 排行榜分页获取失败 kind=%s variant=%s page=%s", kind, variant, page)
                    await lb_cmd.finish("⚠️ 获取 BTD6 排行榜失败，请稍后再试")
                await _send_card(lb_cmd, f"btd6lb_p{page}", lambda: leaderboard_html(col), lambda: leaderboard_text(col))
                return
            cards = []
            for variant in variants:
                try:
                    c = await collect_leaderboard_page(kind, variant, page)
                    if not c.get("empty"):
                        cards.append((f"btd6lb_{variant}_p{page}", lambda c=c: leaderboard_html(c), lambda c=c: leaderboard_text(c)))
                except Exception:
                    _logger.exception("BTD6 排行榜分页获取失败 kind=%s variant=%s page=%s", kind, variant, page)
            if not cards:
                await lb_cmd.finish("⚠️ 获取 BTD6 排行榜失败，请稍后再试")
            await _finish_multi_cards(lb_cmd, cards)
            return
        variant = parse_variant(tokens, {"boss": "standard", "ct": "player"}.get(kind, ""))
        try:
            col = await collect_leaderboard_page(kind, variant, page)
        except Exception:
            _logger.exception("BTD6 排行榜分页获取失败 kind=%s variant=%s page=%s", kind, variant, page)
            await lb_cmd.finish("⚠️ 获取 BTD6 排行榜失败，请稍后再试")
        await _send_card(lb_cmd, f"btd6lb_p{page}", lambda: leaderboard_html(col), lambda: leaderboard_text(col))
        return

    # 默认：前50名
    rows = LB_DEFAULT_ROWS
    variants = {"boss": ("standard", "elite"), "ct": ("player", "team")}.get(kind)
    if variants:
        cards = []
        for variant in variants:
            try:
                c = await collect_leaderboard(kind, variant, rows)
                if not c.get("empty"):
                    cards.append((f"btd6lb_{variant}",
                                  lambda c=c: leaderboard_html(c), lambda c=c: leaderboard_text(c)))
            except Exception:
                _logger.exception("BTD6 排行榜获取失败 kind=%s variant=%s", kind, variant)
        if not cards:
            await lb_cmd.finish("⚠️ 获取 BTD6 排行榜失败，请稍后再试")
        await _finish_multi_cards(lb_cmd, cards)
        return
    variant = parse_variant(tokens, {"boss": "standard", "ct": "player"}.get(kind, ""))
    try:
        col = await collect_leaderboard(kind, variant, rows)
    except Exception:
        _logger.exception("BTD6 排行榜获取失败")
        await lb_cmd.finish("⚠️ 获取 BTD6 排行榜失败，请稍后再试")
    await _send_card(lb_cmd, "btd6lb", lambda: leaderboard_html(col), lambda: leaderboard_text(col))


@rules_cmd.handle()
async def handle_rules(event: MessageEvent):
    await _enforce_cooldown(rules_cmd, event, "rules")
    tokens = event.get_plaintext().split()[1:]
    kind = parse_kind(tokens) or "race"
    # 多版本一起发：boss→标准+精英，其余单版本
    if kind == "boss":
        cards = []
        for variant in ("standard", "elite"):
            try:
                c = await collect_rules(kind, variant)
                if not c.get("empty"):
                    cards.append((f"btd6rule_{variant}",
                                  lambda c=c: rules_html(c), lambda c=c: rules_text(c)))
            except Exception:
                _logger.exception("BTD6 规则获取失败 kind=%s variant=%s", kind, variant)
        if not cards:
            await rules_cmd.finish("⚠️ 获取 BTD6 规则失败，请稍后再试")
        await _finish_multi_cards(rules_cmd, cards)
        return
    variant = "" if kind == "race" else \
        ("elite" if any(t.lower() in ELITE_WORDS for t in tokens) else "standard")
    try:
        col = await collect_rules(kind, variant)
    except Exception:
        _logger.exception("BTD6 规则获取失败")
        await rules_cmd.finish("⚠️ 获取 BTD6 规则失败，请稍后再试")
    await _send_card(rules_cmd, "btd6rule", lambda: rules_html(col), lambda: rules_text(col))


@maps_cmd.handle()
async def handle_maps(event: MessageEvent):
    await _enforce_cooldown(maps_cmd, event, "maps", "heavy")
    tokens = event.get_plaintext().split()[1:]
    filt = "newest"
    for t in tokens:
        mapped = MAP_FILTERS.get(t.lower())
        if mapped:
            filt = mapped
    rows = min(parse_rows(tokens), 20)
    try:
        col = await collect_maps(filt, rows)
    except Exception:
        _logger.exception("BTD6 地图列表获取失败")
        await maps_cmd.finish("⚠️ 获取 BTD6 自制地图失败，请稍后再试")
    await _send_card(maps_cmd, "btd6map", lambda: maps_html(col), lambda: maps_text(col))


@daily_cmd.handle()
async def handle_daily(event: MessageEvent):
    await _enforce_cooldown(daily_cmd, event, "daily")
    # 统一双版本一起发（普通+高级），忽略参数区分
    cards = []
    for adv in (False, True):
        try:
            c = await collect_daily(adv)
            if not c.get("empty"):
                cards.append(("btd6dailya" if adv else "btd6daily",
                              lambda c=c: rules_html(c), lambda c=c: rules_text(c)))
        except Exception:
            _logger.exception("BTD6 每日挑战获取失败 adv=%s", adv)
    if not cards:
        await daily_cmd.finish("⚠️ 获取 BTD6 每日挑战失败，请稍后再试")
    await _finish_multi_cards(daily_cmd, cards)


@odyssey_cmd.handle()
async def handle_odyssey(event: MessageEvent):
    await _enforce_cooldown(odyssey_cmd, event, "odyssey", "heavy")
    try:
        col = await collect_odyssey()
    except Exception:
        _logger.exception("BTD6 远征获取失败")
        await odyssey_cmd.finish("⚠️ 获取 BTD6 远征信息失败，请稍后再试")
    if col.get("empty"):
        await odyssey_cmd.finish(col["empty"])
    # 统一三图尺寸：QQ 预览按最大边等比缩放，像素高度不同会导致显示宽度视觉不一；取三难度最大高度作为统一画布高度
    try:
        _unified_h = max(_odyssey_card_height((col["diffs"].get(_d) or {}).get("meta"),
                                              len((col["diffs"].get(_d) or {}).get("maps") or []))
                         for _d, _lab in _ODYSSEY_DIFFS)
        for _d, _ in _ODYSSEY_DIFFS:
            if _d in col["diffs"]:
                col["diffs"][_d]["_unified_h"] = _unified_h
    except Exception:
        _logger.debug("BTD6 远征统一高度计算失败，使用各自高度", exc_info=True)
    # 流式渲染：渲染一张立刻发送一张，避免三张全部渲染完才首图可见；单张 weasyprint 渲染约 1-3s，三张串行合计 3-9s，流式可让首图 1-3s 内到达。
    for idx, (d, lab) in enumerate(_ODYSSEY_DIFFS):
        try:
            t0 = time.monotonic()
            path = await _render_card("btd6ody", lambda d=d, lab=lab: odyssey_diff_html(col, d, lab))
            _logger.info("BTD6 远征 %s 渲染 %.2fs -> %s", lab, time.monotonic() - t0, path)
        except Exception:
            _logger.warning("BTD6 远征卡片渲染失败，回退文本消息", exc_info=True)
            await odyssey_cmd.finish(MessageSegment.text(odyssey_text(col)))
        if idx < len(_ODYSSEY_DIFFS) - 1:
            await odyssey_cmd.send(MessageSegment.image(Path(path).as_uri()))
        else:
            await odyssey_cmd.finish(MessageSegment.image(Path(path).as_uri()))


@player_cmd.handle()
async def handle_player(event: MessageEvent):
    await _enforce_cooldown(player_cmd, event, "player", "heavy")
    tokens = event.get_plaintext().split()[1:]
    pid = _extract_player_id(" ".join(tokens))
    if not pid:
        await player_cmd.finish("用法：.btd6玩家 <玩家ID>\nID 是排行榜玩家链接末尾的长串十六进制（40+ 位）")
    try:
        col = await collect_player(pid)
    except Exception:
        _logger.exception("BTD6 玩家档案获取失败")
        await player_cmd.finish("⚠️ 获取 BTD6 玩家档案失败，请稍后再试")
    await _send_card(player_cmd, "btd6pl", lambda: player_html(col), lambda: player_text(col))


@push_on_cmd.handle()
async def handle_push_on(event: MessageEvent):
    if not is_owner(event):
        await push_on_cmd.finish("❌ 仅机器人主人可开启活动推送")
    gid = getattr(event, "group_id", None)
    if gid is None:
        await push_on_cmd.finish("请在需要推送的群内发送此命令")
    changed = _push_change_group(int(gid), True)
    await push_on_cmd.finish("✅ 本群已开启 BTD6 活动自动推送（竞速/Boss/CT/远征/每日 刷新时推送）" if changed else "本群已在推送列表中")


@push_off_cmd.handle()
async def handle_push_off(event: MessageEvent):
    if not is_owner(event):
        await push_off_cmd.finish("❌ 仅机器人主人可关闭活动推送")
    gid = getattr(event, "group_id", None)
    if gid is None:
        await push_off_cmd.finish("请在群内发送此命令")
    changed = _push_change_group(int(gid), False)
    await push_off_cmd.finish("✅ 本群已关闭 BTD6 活动自动推送" if changed else "本群未在推送列表中")


@push_status_cmd.handle()
async def handle_push_status(event: MessageEvent):
    if not is_owner(event):
        await push_status_cmd.finish("❌ 仅机器人主人可查看推送状态")
    groups = _push_groups()
    last = _last_pushed()
    lines = ["📋 BTD6 推送状态"]
    lines.append(f"推送群数：{len(groups)}" + (f"（{', '.join(str(g) for g in sorted(groups))}）" if groups else "（未配置）"))
    if last:
        lines.append("最近推送：")
        for k in _BTD6_PUSH_KINDS:
            if k in last:
                lines.append(f"  {k}: {last[k][:24]}")
    else:
        lines.append("最近推送：无")
    lines.append("")
    lines.append("命令：.btd6推送开启 / .btd6推送关闭（仅主人）")
    # ev_id 等字段来自 NK API（防 CQ 码注入）：整段经 MessageSegment.text 发送
    await push_status_cmd.finish(MessageSegment.text("\n".join(lines)))


_HIST_KIND_WORDS = {"竞速": "race", "race": "race", "boss": "boss", "首领": "boss",
                    "领土": "ct", "ct": "ct", "争夺": "ct", "远征": "odyssey",
                    "odyssey": "odyssey", "每日": "daily", "daily": "daily"}


@hist_cmd.handle()
async def handle_history(event: MessageEvent):
    """查询本地归档的历史活动：.btd6历史 [竞速|boss|领土|远征|每日] [数量]。
    NK API 各列表只保留近几期（Boss 仅 3 期），本命令读的是预热顺带落盘的 history.json。"""
    await _enforce_cooldown(hist_cmd, event, "history")
    tokens = event.get_plaintext().split()[1:]
    kind = ""
    for t in tokens:
        k = _HIST_KIND_WORDS.get(t.lower()) or _HIST_KIND_WORDS.get(t)
        if k:
            kind = k
            break
    rows = parse_rows(tokens)
    # 归档文件读取（含磁盘 IO 与锁）放到线程池，避免卡住事件循环
    hist = await asyncio.to_thread(_load_history)
    now = int(time.time() * 1000)
    kinds = [kind] if kind else ["race", "boss", "ct", "odyssey", "daily"]
    lines = ["🗂 BTD6 活动历史归档"]
    any_data = False
    for k in kinds:
        items = [x for x in (hist.get(k) or []) if isinstance(x, dict)]
        if not items:
            continue
        any_data = True
        items.sort(key=lambda x: int(x.get("start") or 0), reverse=True)
        lines.append(f"【{HISTORY_KIND_CN[k]}】共 {len(items)} 期")
        for ev in items[:rows]:
            s = int(ev.get("start") or 0)
            e = int(ev.get("end") or 0)
            name = str(ev.get("name") or "").strip() or str(ev.get("id") or "?")
            if s and e:
                state = "进行中" if s <= now < e else ("未开始" if now < s else "已结束")
                lines.append(f"  {fmt_date(s)} ~ {fmt_date(e)} {state} {name}")
            else:
                lines.append(f"  {name}")
    if not any_data:
        lines.append("（归档为空，随预热每轮自动积累）")
    # 活动名等来自 NK API 历史归档（防 CQ 码注入）：整段经 MessageSegment.text 发送
    await hist_cmd.finish(MessageSegment.text("\n".join(lines)))


@prewarm_cmd.handle()
async def handle_prewarm(event: MessageEvent):
    if not is_owner(event):
        await prewarm_cmd.finish("❌ 仅主人可手动预热")
    await _enforce_cooldown(prewarm_cmd, event, "prewarm", "heavy")
    if _prewarm_running:
        await prewarm_cmd.finish("⏳ 预热已在进行中，请稍候")
    await prewarm_cmd.send("⏳ 开始手动预热（活动/榜单/素材）...")
    try:
        await _prewarm_once()
        await _render_card("btd6help", help_html)
    except Exception as e:
        from nonebot.exception import FinishedException
        if isinstance(e, FinishedException):
            raise
        _logger.exception("手动预热失败")
        # 异常信息含外部输入内容（防 CQ 码注入）：经 MessageSegment.text 发送
        await prewarm_cmd.finish(MessageSegment.text(f"⚠️ 预热失败: {e}"))
        return
    await prewarm_cmd.finish("✅ 预热完成（活动已归档，热门榜单/帮助已刷新）")
