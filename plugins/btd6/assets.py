"""素材层：官方 CDN 图片/本地游戏立绘/UI 图标 → data: URL，内存 + 落盘两级缓存。"""
import asyncio
import base64
import hashlib
import io
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path

from . import i18n, nkapi

_logger = logging.getLogger(__name__)


CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
ASSET_DIR = os.path.join(CACHE_DIR, "assets")


ASSET_TTL = 7 * 24 * 60 * 60  # 素材落盘缓存时长（版本更新才会变）


MAX_ASSET_MEM_ITEMS = 256
MAX_GAME_MEM_ITEMS = 128


MAX_ASSET_FILES = 256
MAX_ASSET_BYTES = 64_000_000


def _prune_cache_files(directory: str, suffix: str, max_files: int, max_bytes: int,
                       protected: set[str] | None = None) -> None:
    protected = protected or set()
    try:
        # 顺手清扫写盘崩溃残留的 .tmp（不在 suffix 预算内，正常清理看不见它们）；
        # 保留 1 小时内的，避免误删正在进行中的写入
        now = time.time()
        for stale_tmp in Path(directory).glob(f"*{suffix}.tmp"):
            if stale_tmp.is_file() and now - stale_tmp.stat().st_mtime > 3600:
                try:
                    stale_tmp.unlink()
                except OSError:
                    pass
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
MAX_ASSET_MEM_BYTES = 24_000_000


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


# 同 URL 并发冷读的去重表：url → 进行中的下载 Task（参照 nkapi._refreshing 的模式；
# 仅 async 路径，渲染线程内的同步 _game_asset_data_url 不走这里）
_asset_inflight: dict[str, "asyncio.Task[str]"] = {}


async def _asset_data_url(url: str, max_bytes: int = 3_000_000) -> str:
    """下载图片素材并返回 data: URL；内存 + 落盘两级缓存（渲染管线只允许 data: URL）。
    同 URL 并发冷读共享同一下载任务，避免重复请求与重复写盘。"""
    if not url:
        return ""
    try:
        nkapi._validate_url(url)
    except ValueError:
        return ""
    with _asset_mem_lock:
        hit = _asset_mem.get(url)
        if hit:
            _asset_mem.move_to_end(url)
            return hit
    task = _asset_inflight.get(url)
    if task is None:
        task = asyncio.create_task(_asset_data_url_uncached(url, max_bytes))
        _asset_inflight[url] = task
        task.add_done_callback(lambda _t, u=url: _asset_inflight.pop(u, None))
    return await task


async def _asset_data_url_uncached(url: str, max_bytes: int) -> str:
    """冷读路径：读落盘缓存，未命中则下载、校验并写盘。"""
    key = hashlib.md5(url.encode()).hexdigest()
    path = os.path.join(ASSET_DIR, key + ".txt")

    def _read_disk_cache() -> str:
        try:
            if time.time() - os.path.getmtime(path) < ASSET_TTL:
                with open(path, encoding="ascii") as f:
                    return f.read()
        except (OSError, UnicodeError):
            pass
        return ""

    data_url = await asyncio.to_thread(_read_disk_cache)
    if data_url.startswith("data:image/"):
        _remember_asset(url, data_url)
        return data_url
    r = await nkapi._http_get(url, 15)
    r.raise_for_status()
    data = getattr(r, "content", b"") or b""
    if not data or len(data) > max_bytes:
        return ""
    mime = _sniff_mime(data)
    if mime not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
        return ""
    data_url = f"data:{mime};base64," + base64.b64encode(data).decode("ascii")

    def _write_disk_cache() -> None:
        tmp = path + ".tmp"
        try:
            os.makedirs(ASSET_DIR, exist_ok=True)
            with open(tmp, "w", encoding="ascii") as f:
                f.write(data_url)
            os.replace(tmp, path)
            _prune_cache_files(ASSET_DIR, ".txt", MAX_ASSET_FILES, MAX_ASSET_BYTES, {path})
        except OSError:
            _logger.warning("BTD6 素材缓存写入失败", exc_info=True)
            try:
                # 清理残留的 .tmp：既不计入 256 文件/64MB 预算（只 glob *.txt），也无人删除
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass

    await asyncio.to_thread(_write_disk_cache)
    _remember_asset(url, data_url)
    return data_url


# ---------------- 本地游戏素材（塔/英雄立绘，来自 BTD6 API Explorer 资源库） ----------------

GAME_ASSET_DIR = os.path.join(os.path.dirname(__file__), "assets", "game")
_game_mem: OrderedDict[str, str] = OrderedDict()
# 本地素材内存缓存的读写锁（渲染走线程池，dict 访问须与 _asset_mem 一样持锁）
_local_mem_lock = threading.Lock()


def _remember_game_asset(fname: str, data_url: str) -> None:
    with _local_mem_lock:
        _game_mem[fname] = data_url
        _game_mem.move_to_end(fname)
        nkapi._prune_ordered(_game_mem, MAX_GAME_MEM_ITEMS)


def _game_asset_data_url(fname: str) -> str:
    """读取本地游戏素材并转 data: URL；缺失返回空串（卡片自动降级为文字）。"""
    if not fname or not re.fullmatch(r"[A-Za-z0-9_-]+\.webp", fname):
        return ""
    with _local_mem_lock:
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
    with _local_mem_lock:
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
    with _local_mem_lock:
        _ui_mem[fname] = url
        _ui_mem.move_to_end(fname)
        nkapi._prune_ordered(_ui_mem, 32)
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
        return f"{i18n.tower_cn(base)}·皮肤"
    return i18n.tower_cn(raw)


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
        nkapi._prune_ordered(_odyssey_thumb_mem, 64)
    return thumb


_BOSS_EVENT_ASSETS = {
    "bloonarius": "boss-bloonarius.png",
    "lych": "boss-lych.png",
    "vortex": "boss-vortex.png",
    "dreadbloon": "boss-dreadbloon.png",
    "phayze": "boss-phayze.png",
    "blastapopoulos": "boss-blastapopoulos.png",
}


def _boss_event_asset(ev: dict | None) -> str:
    raw = str((ev or {}).get("bossType") or "").strip().lower()
    return _BOSS_EVENT_ASSETS.get(raw, "")


_RUSH_BOSSES = [
    # 默认 Boss Rush 轮换（依 Bloons Wiki 与实测轮换），可按 ev["id"] 自定义
    ("Bloonarius",    "boss-bloonarius.png",    "1f479"),
    ("Lych",          "boss-lych.png",          "1f480"),
    ("Dreadbloon",    "boss-dreadbloon.png",    "1faa8"),
    ("Phayze",        "boss-phayze.png",        "1f47b"),
    ("Blastapopoulos","boss-blastapopoulos.png","1f525"),
]


# Boss 名 → 本地头像（Diamondback 走 nkstatic 官方立绘 Portrait.webp）
_RUSH_BOSS_ART = {name: png for name, png, _emoji in _RUSH_BOSSES}


# 塔中文与立绘统一使用上方规范表 TOWER_CN/HERO_CN/BOSS_CN（tower_cn/boss_cn/hero_cn 查找）；
# 立绘文件名特例（向导猴素材文件名是 Wizard 而非 WizardMonkey）
_TOWER_PORTRAIT_SPECIAL = {"WizardMonkey": "000-Wizard.webp"}


def _tower_portrait(t: str) -> str:
    return _game_asset_data_url(_TOWER_PORTRAIT_SPECIAL.get(t, f"000-{t}.webp"))


def _hero_portrait(h: str) -> str:
    return _game_asset_data_url(f"{h}Portrait.webp")
