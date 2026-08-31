"""取数层：NK 开放 API 的 URL/请求边界/内存 TTL 缓存（stale-while-revalidate）/冷却表。

依赖：util（桶周期常量）、common（http 客户端）。上层模块一律经
``nkapi.<名字>`` 模块属性访问，保证 monkeypatch(btd6.nkapi.xxx) 生效。
"""
import asyncio
import json
import logging
import threading
import time
from collections import OrderedDict
from urllib.parse import urlsplit

from common import get_http_client

from . import util

_logger = logging.getLogger(__name__)


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


CACHE_TTL = util.BUCKET_PERIOD_MIN * 60     # 数据缓存与桶周期对齐，保证同窗内查询命中同一份内容哈希


MAX_JSON_BYTES = 8_000_000
MAX_STALE_ITEMS = 256
MAX_JSON_MEM_BYTES = 32_000_000   # JSON body 内存字节预算：按 _cache 键求和，超限从 _cache 最旧淘汰（连带 _stale/旁路字典）


URL_HOSTS = {"data.ninjakiwi.com", "static-api.nkstatic.com"}
REQUEST_LIMIT = asyncio.Semaphore(8)


_COOLDOWN_SECONDS = {
    "default": 3.0,
    "heavy": 10.0,
}
_COOLDOWN_MAX_ITEMS = 4096
_cooldowns: OrderedDict[str, float] = OrderedDict()
_cooldown_lock = threading.Lock()


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


def _release_cooldown(event, command: str) -> None:
    """查询/渲染失败、最终未产出有效回复的路径上回滚冷却，允许用户立即重试。
    只允许在失败路径调用，成功路径不得回滚。"""
    with _cooldown_lock:
        _cooldowns.pop(_cooldown_key(event, command), None)


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
_stale_at: dict[str, float] = {}      # url → 最近一次成功写入缓存的时间（monotonic 秒），用于 _stale_age
_cache_sizes: dict[str, int] = {}     # url → body 近似字节数（字节预算只按 _cache 键求和，见 _cache_put）
_lb_next_cache: dict[str, str | None] = {}  # url → 排行榜信封的 next 链接（随信封解析缓存，避免为读 next 重复请求同一 URL）
_stale_served: set[str] = set()       # 本次请求实际以 stale 旧数据响应的 url（用于过期提示文案）
_refreshing: set[str] = set()
_inflight: dict[str, asyncio.Task] = {}  # 冷缓存首查的在途任务表：同 URL 并发首查共享同一请求（与 assets._asset_inflight 同思路）
_refresh_fail_counts: dict[str, int] = {}  # url → 后台刷新连续失败次数（成功后清零）
_refresh_tasks: set[asyncio.Task] = set()  # 持有后台刷新任务引用，防止任务被 GC 中途回收
_cache_lock = threading.Lock()

STALE_WARN_SECONDS = 24 * 3600
STALE_WARN_TEXT = "（数据已 24+ 小时未刷新，可能过期）"


def _evict(url: str, *, drop_cache: bool = False, drop_stale: bool = False) -> None:
    """统一清理入口：把 url 从缓存与各旁路字典按需移除（需在持有 _cache_lock 时调用）。

    - drop_cache=True：从 _cache 与 _cache_sizes 移除（TTL 过期 / 条数与预算驱逐）；
      字节预算只按 _cache 计，弹出即同步回落，杜绝"过期条目永久占账"的死账；
    - drop_stale=True：从 _stale 与 _stale_at 移除（预算 / stale 条数驱逐）；
    - TTL 自然过期只 drop_cache，保留 _stale 及其伴随字典以维持 SWR 语义；
    - 仅当 url 从 _cache 与 _stale 都移除后才清全部旁路字典
      （_lb_next_cache/_stale_served/_refresh_fail_counts），避免悬挂引用无计数回收。
    """
    if drop_cache:
        _cache.pop(url, None)
        _cache_sizes.pop(url, None)
    if drop_stale:
        _stale.pop(url, None)
        _stale_at.pop(url, None)
    if url not in _cache and url not in _stale:
        _lb_next_cache.pop(url, None)
        _stale_served.discard(url)
        _refresh_fail_counts.pop(url, None)


def _cache_get(url: str):
    with _cache_lock:
        hit = _cache.get(url)
        if not hit:
            return None
        if hit[0] > time.monotonic():
            _cache.move_to_end(url)
            return hit[1]
        # TTL 过期弹出：同步清 _cache_sizes（预算只按 _cache 计），
        # 保留 _stale 及其伴随字典维持 SWR
        _evict(url, drop_cache=True)
    return None


def _body_size(body) -> int:
    """JSON body 的近似字节量（缓存写入时算一次，用于内存预算）。"""
    try:
        return len(json.dumps(body, ensure_ascii=False))
    except (TypeError, ValueError):
        try:
            return len(str(body))
        except Exception:
            return 0


def _cache_put(url: str, body) -> None:
    size = _body_size(body)
    with _cache_lock:
        _cache[url] = (time.monotonic() + CACHE_TTL, body)
        _cache.move_to_end(url)
        _stale[url] = body
        _stale.move_to_end(url)
        _stale_at[url] = time.monotonic()
        _cache_sizes[url] = size
        _stale_served.discard(url)  # 拿到新数据后不再提示过期
        # 条数驱逐（_cache）：预算只按 _cache 计，弹出即清 _cache_sizes；
        # 条目若仍在 _stale 中，旁路字典保留（SWR 仍可服务旧数据）
        while len(_cache) > MAX_STALE_ITEMS:
            oldest, _ = _cache.popitem(last=False)
            _evict(oldest, drop_cache=True)
        # 条数驱逐（_stale）：随淘汰同步回收 _lb_next_cache/_stale_served/
        # _refresh_fail_counts（原先无计数回收的卫生问题）
        while len(_stale) > MAX_STALE_ITEMS:
            oldest, _ = _stale.popitem(last=False)
            _evict(oldest, drop_stale=True)
        # 字节预算：total 只按 _cache 键求和，超限从 _cache 最旧淘汰（连带 _stale，
        # 全部旁路字典经 _evict 一并清理）
        total = sum(_cache_sizes.get(u, 0) for u in _cache)
        while total > MAX_JSON_MEM_BYTES and _cache:
            oldest, _ = _cache.popitem(last=False)
            total -= _cache_sizes.get(oldest, 0)
            _evict(oldest, drop_cache=True, drop_stale=True)


def _stale_age(url: str) -> float | None:
    """该 URL 数据距今多少秒未成功刷新；从未写入过返回 None。"""
    with _cache_lock:
        stamp = _stale_at.get(url)
    if stamp is None:
        return None
    return max(0.0, time.monotonic() - stamp)


def _stale_warn(*urls: str) -> str:
    """文本/卡片输出使用超 24h 的 stale 数据时，返回附加在文案末尾的提示。"""
    for url in urls:
        if url in _stale_served:
            age = _stale_age(url)
            if age is not None and age > STALE_WARN_SECONDS:
                return STALE_WARN_TEXT
    return ""


async def _http_get(url: str, timeout: float):
    _validate_url(url)
    client = get_http_client(20)
    async with REQUEST_LIMIT:
        return await client.get(url, timeout=timeout)


async def _refresh_url(url: str) -> None:
    """后台刷新过期缓存；失败保留旧数据（跨境网络抖动时不拖慢回复），warning 记录连续失败。"""
    if url in _refreshing:
        return
    _refreshing.add(url)
    try:
        r = await _http_get(url, 20)
        r.raise_for_status()
        if len(r.content or b"") > MAX_JSON_BYTES:
            raise ValueError("BTD6 API 响应过大")
        data = r.json()
        if isinstance(data, dict) and data.get("error") == "No Scores Available":
            # 暂无分数属空态而非故障：写空缓存，失败计数清零
            _cache_put(url, [])
            with _cache_lock:
                _lb_next_cache[url] = None
            _refresh_fail_counts.pop(url, None)
            return
        if isinstance(data, dict) and data.get("success") and data.get("body") is not None:
            _cache_put(url, data["body"])
            with _cache_lock:
                _lb_next_cache[url] = data.get("next")
    except Exception:
        fails = _refresh_fail_counts.get(url, 0) + 1
        _refresh_fail_counts[url] = fails
        _logger.warning("BTD6 后台刷新失败（保留旧缓存） url=%s 连续失败 %d 次", url, fails, exc_info=True)
    else:
        _refresh_fail_counts.pop(url, None)
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
        # 记录"本次响应来自旧缓存"，供 _stale_warn 在输出层附加过期提示
        with _cache_lock:
            _stale_served.add(url)
        task = asyncio.create_task(_refresh_url(url))
        _refresh_tasks.add(task)
        task.add_done_callback(_refresh_tasks.discard)
        return stale
    # 冷缓存并发去重：检查与注册之间无 await（事件循环原子），首查并发共享同一任务
    task = _inflight.get(url)
    if task is None:
        task = asyncio.create_task(_fetch_body_remote(url))
        _inflight[url] = task
        task.add_done_callback(lambda t, u=url: _inflight.pop(u, None) if _inflight.get(u) is t else None)
    return await task


async def _fetch_body_remote(url: str):
    """冷缓存的实际取数路径（仅由 fetch_body 经 _inflight 调用）。"""
    r = await _http_get(url, 20)
    r.raise_for_status()
    if len(r.content or b"") > MAX_JSON_BYTES:
        raise ValueError("BTD6 API 响应过大")
    data = r.json()
    if not isinstance(data, dict) or not data.get("success") or data.get("body") is None:
        error = data.get("error") if isinstance(data, dict) else "响应格式异常"
        if error == "No Scores Available":
            # NK API 对暂无分数的榜单返回 success=false + 该文案，属空态而非故障：
            # 返回空列表并写入缓存（next=None 终止分页），避免对无分数活动反复请求
            _cache_put(url, [])
            with _cache_lock:
                _lb_next_cache[url] = None
            return []
        raise RuntimeError(f"NK API 返回异常: {error}")
    body = data["body"]
    _cache_put(url, body)
    with _cache_lock:
        _lb_next_cache[url] = data.get("next")  # 信封里的 next 一并缓存，分页无需重复请求
    return body


async def fetch_leaderboard_paginated(start_url: str, rows: int, touched: set[str] | None = None) -> list:
    """分页拉取排行榜至多 rows 条，自动跟随 next 链接（竞赛 50/页，Boss/CT 25/页）。

    body 统一经 fetch_body（缓存/SWR）；next 随信封解析一并缓存（_lb_next_cache），
    因此同一 URL 不会出现"先 fetch_body 再 _http_get 全量拉一遍只为读 next"的重复请求；
    仅当 body 命中缓存但 next 未知（旧缓存条目/其他路径写入）时，才补一次信封请求读 next。
    传入 touched 集合时，本次实际请求过的每个页 URL（含 start_url）都会写入其中，
    供调用方把实际被 stale 服务的页一并纳入过期提示判定。
    """
    entries: list = []
    url: str | None = start_url
    seen: set[str] = set()
    while url and len(entries) < rows and url not in seen:
        seen.add(url)
        if touched is not None:
            touched.add(url)
        try:
            body = await fetch_body(url)
        except Exception:
            # fetch_body 失败：回退为一次 _http_get 拿完整信封（body + next）并手动写缓存
            try:
                r = await _http_get(url, 20)
                r.raise_for_status()
                if len(r.content or b"") > MAX_JSON_BYTES:
                    raise ValueError("BTD6 API 响应过大")
                data = r.json()
                if not isinstance(data, dict) or not data.get("success") or data.get("body") is None:
                    raise RuntimeError("NK API 返回异常")
                body = data["body"]
                next_url = data.get("next")
                _cache_put(url, body)
                with _cache_lock:
                    _lb_next_cache[url] = next_url
                if isinstance(body, list):
                    entries.extend(body)
                    if len(entries) < rows:
                        url = next_url
                        continue
                break
            except Exception:
                _logger.warning("排行榜分页拉取失败 url=%s", url, exc_info=True)
                break
        if not isinstance(body, list):
            break
        entries.extend(body)
        if len(entries) >= rows:
            break
        with _cache_lock:
            next_known = url in _lb_next_cache
            next_url = _lb_next_cache.get(url)
        if next_known:
            url = next_url
            continue
        # body 命中缓存但 next 未知（旧缓存条目/其他路径写入）：补一次信封请求读 next。
        # 校验与 fetch_body 主路径对齐：体积上限 + success 信封检查
        try:
            r = await _http_get(url, 20)
            r.raise_for_status()
            if len(r.content or b"") > MAX_JSON_BYTES:
                raise ValueError("BTD6 API 响应过大")
            data = r.json()
            if not isinstance(data, dict) or not data.get("success"):
                next_url = None
            else:
                next_url = data.get("next")
            with _cache_lock:
                _lb_next_cache[url] = next_url
        except Exception:
            _logger.warning("排行榜分页 next 获取失败 url=%s", url, exc_info=True)
            break
        url = next_url
    return entries[:rows]


def lb_page_size(kind: str) -> int:
    return LB_PAGE_SIZES.get(kind, 50)


def lb_page_url(base: str, page: int) -> str:
    if page <= 1:
        return base
    return f"{base}?page={page}"
