"""本地回合组明细数据（快照自 BTD6 API Explorer 的 data/roundsets，游戏版本数据）。

NK 开放 API 的 challenge metadata 只给 roundSets 名称，不给逐回合内容；
这里用本地快照把自定义回合组与默认回合组逐回合对比，展开为
“第 N 回合：改为 …”的中文明细。数据文件 assets/data/roundsets/{名称}.json，
与站点文件同名，后续可整目录替换来更新游戏版本。
"""
import json
import os

_DIR = os.path.join(os.path.dirname(__file__), "assets", "data", "roundsets")
_DEFAULT_SET = "DefaultRoundSet"
_MAX_ROWS = 12  # 明细行数上限，超出时折叠为汇总行

# 数据里的基础气球名 → 中文（写法与 _ROUND_BLOON_ICON_FILES 的图标 token 对齐）
_BLOON_CN = {
    "red": "红气球", "blue": "蓝气球", "green": "绿气球", "yellow": "黄气球",
    "pink": "粉气球", "white": "白气球", "black": "黑气球", "purple": "紫气球",
    "lead": "铅气球", "zebra": "斑马气球", "rainbow": "彩虹气球",
    "ceramic": "陶瓷气球", "moab": "MOAB", "bfb": "BFB", "zomg": "ZOMG",
    "ddt": "DDT", "bad": "BAD",
    # 活动特殊气球（Boss Rush / 任务等）
    "aura": "光环气球", "diamond": "钻石气球", "dynamite": "炸药气球",
    "glass": "玻璃气球", "retribution": "天罚气球", "ringleader": "头目气球",
}
_TRAIT_CN_ORDER = ("fortified", "regrow", "camo")
_TRAIT_CN = {"fortified": "加固", "regrow": "再生", "camo": "隐形"}

_cache: dict = {}
_names: set | None = None


def _available_names() -> set:
    global _names
    if _names is None:
        try:
            _names = {f[:-5] for f in os.listdir(_DIR) if f.endswith(".json")}
        except OSError:
            _names = set()
    return _names


def load_set(name: str) -> dict | None:
    """按名（大小写不敏感）读取回合组快照，返回 {"rounds":[...]}；缺失返回 None。"""
    key = str(name or "").strip()
    if not key:
        return None
    if key not in _cache:
        data = None
        if key in _available_names():
            try:
                with open(os.path.join(_DIR, key + ".json"), encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                data = None
        _cache[key] = data
    return _cache[key]


def _parse_bloon(raw) -> tuple[str, tuple[str, ...]]:
    """'CeramicRegrowFortifiedCamo' → ('陶瓷气球', ('加固', '再生', '隐形'))。"""
    s = str(raw or "").strip()
    traits = []
    low = s.lower()
    changed = True
    while changed:
        changed = False
        for t in _TRAIT_CN_ORDER:
            if low.endswith(t):
                low = low[: -len(t)]
                traits.append(t)
                changed = True
                break
    base = _BLOON_CN.get(low, s if not traits else low)
    ordered = tuple(_TRAIT_CN[t] for t in _TRAIT_CN_ORDER if t in traits)
    return base, ordered


def _group_summary(groups: list) -> str:
    """bloonGroups → “6×陶瓷气球、4×加固ZOMG”这类中文摘要（同名合并，保持出现顺序）。"""
    order: list[tuple[str, tuple[str, ...]]] = []
    counts: dict[tuple[str, tuple[str, ...]], int] = {}
    for g in groups or []:
        base, traits = _parse_bloon(g.get("bloon"))
        key = (base, traits)
        if key not in counts:
            order.append(key)
            counts[key] = 0
        try:
            counts[key] += int(g.get("count") or 0)
        except (TypeError, ValueError):
            pass
    parts = []
    for base, traits in order:
        name = ("".join(traits)) + base if traits else base
        n = counts[(base, traits)]
        parts.append(f"{n}×{name}" if n != 1 else name)
    return "、".join(parts)


def _group_key(groups: list) -> tuple:
    """把一回合的气球组归一化为可比较的键（忽略出场时间，只看种类与数量）。"""
    sig = sorted((str(g.get("bloon") or ""), int(g.get("count") or 0)) for g in groups or [])
    return tuple(sig)


def describe(name: str) -> list[tuple[str, str]]:
    """回合组名 → [(“第N回合”, “改为：…”)]；无本地数据或无改动时返回空列表。

    与 DefaultRoundSet 逐回合对比得出被修改的回合；超过 _MAX_ROWS 行时
    其余折叠为一行汇总，避免小卡片被上百行撑爆。
    """
    data = load_set(name)
    if not isinstance(data, dict):
        return []
    base_rows = {r.get("roundNumber"): r.get("bloonGroups") or []
                 for r in (load_set(_DEFAULT_SET) or {}).get("rounds") or []}
    changed = []
    for r in data.get("rounds") or []:
        rn = r.get("roundNumber")
        if rn not in base_rows:
            continue  # 快照里默认组没有该回合（超 140 回合的自定义组），无法对比
        if _group_key(r.get("bloonGroups")) == _group_key(base_rows[rn]):
            continue
        desc = _group_summary(r.get("bloonGroups"))
        if desc:
            changed.append((f"第{rn}回合", f"改为：{desc}"))
    if len(changed) > _MAX_ROWS:
        extra = len(changed) - _MAX_ROWS
        changed = changed[:_MAX_ROWS] + [("…", f"另有 {extra} 个回合被修改")]
    return changed
