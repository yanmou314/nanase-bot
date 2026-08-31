"""Boss Rush 阶段数据生成器（Python 移植版）。

NK 开放 API 不提供 Boss Rush 逐阶段配置（/btd6/bossRush 为 404）。
BTD6 API Explorer（btd6apiexplorer.github.io，v3.0.0 2026-08-27）的维护者
Lucy 逆向了游戏内的生成逻辑：给定活动 ID 作为种子，用 .NET System.Random
兼容的 PRNG 确定性地生成每阶段的地图/Boss/塔池/遗物。本模块是该逻辑的
Python 等价实现，与其 JS 版本逐调用对齐（含失败重试消耗随机数的顺序）。

数据来源：随插件打包的 rushdata.json（自 Constants.json v3.0.0 裁剪）。
游戏更新随机参数后需同步更新该文件。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

_logger = logging.getLogger(__name__)

_DATA_PATH = os.path.join(os.path.dirname(__file__), "rushdata.json")
_CONSTANTS: dict | None = None

WATER_MAPS = {"Peninsula", "SpiceIslands"}
WATER_TOWERS = {"MonkeySub", "MonkeyBuccaneer"}
_DIFF_NAMES = {0: "Beginner", 1: "Intermediate", 2: "Advanced", 3: "Expert"}
_DIFF_IDX = {"Beginner": 0, "Intermediate": 1, "Advanced": 2, "Expert": 3}

# rushdata.json（裁剪自 Constants.json v3.0.0）的关键字段：缺失说明数据文件
# 版本过期或损坏，生成结果将不可信，加载时 warning 提示（不抛异常，保底可渲染）。
_REQUIRED_TOP_KEYS = ("bossRush", "mapsInOrder", "towersInOrder", "heroesInOrder")
_REQUIRED_BR_KEYS = ("StageScores", "StageRewards", "RandomSettings")


def _validate_constants(data: dict) -> None:
    if not isinstance(data, dict):
        _logger.warning("rushdata.json 结构异常：顶层不是对象，Boss Rush 数据可能过期")
        return
    missing_top = [k for k in _REQUIRED_TOP_KEYS if k not in data]
    if missing_top:
        _logger.warning("rushdata.json 缺少关键字段: %s（数据版本可能过期）", "、".join(missing_top))
    br = data.get("bossRush")
    if isinstance(br, dict):
        missing_br = [k for k in _REQUIRED_BR_KEYS if k not in br]
        if missing_br:
            _logger.warning("rushdata.json bossRush 缺少字段: %s", "、".join(missing_br))
        # 阶段数由 StageScores 长度决定，必须为非空列表（空表会生成 0 阶段）
        scores = br.get("StageScores")
        if not isinstance(scores, list) or not scores:
            _logger.warning("rushdata.json bossRush.StageScores 必须为非空列表，Boss Rush 阶段数据可能过期")
        rs = br.get("RandomSettings")
        if not isinstance(rs, dict) or not isinstance(rs.get("TowerSettings"), dict):
            _logger.warning("rushdata.json bossRush.RandomSettings/TowerSettings 异常，塔池生成可能不准")
        if isinstance(rs, dict):
            # 生成路径实际用到的两个池：为空时 generate_bosses_br/generate_relics 会退化
            for key in ("AvailableBosses", "RelicChances"):
                if key in rs and not rs.get(key):
                    _logger.warning("rushdata.json bossRush.RandomSettings.%s 为空，Boss Rush 生成可能失败", key)
    elif br is not None:
        _logger.warning("rushdata.json bossRush 不是对象，Boss Rush 数据可能过期")


def load_constants() -> dict:
    global _CONSTANTS
    if _CONSTANTS is None:
        with open(_DATA_PATH, encoding="utf-8") as f:
            _CONSTANTS = json.load(f)
        _validate_constants(_CONSTANTS)
    return _CONSTANTS


class CompatPrng:
    """BTD6 游戏侧 PRNG，等价 .NET System.Random（Knuth 减法法）。"""

    def __init__(self, seed: int):
        self._seed_array: list[int] | None = None
        self._inext = 0
        self._inextp = 0
        self.ensure_initialized(seed)

    def ensure_initialized(self, seed: int) -> None:
        if self._seed_array is None:
            self.initialize(seed)

    def initialize(self, seed: int) -> None:
        seed_array: list[int] = [0] * 56
        max_val = 2147483647
        sub = 161803398 - (max_val if seed == -2147483648 else abs(seed))
        if sub < 0:
            sub += max_val
        seed_array[55] = sub
        mj = 1
        mk = 0
        for _i in range(1, 55):
            mk += 21
            if mk >= 55:
                mk -= 55
            seed_array[mk] = mj
            mj = sub - mj
            if mj < 0:
                mj += max_val
            sub = seed_array[mk]
        for _k in range(1, 5):
            for i in range(1, 56):
                ii = i + 30
                if ii >= 55:
                    ii -= 55
                seed_array[i] -= seed_array[1 + ii]
                if seed_array[i] < 0:
                    seed_array[i] += max_val
        self._seed_array = seed_array
        self._inext = 0
        self._inextp = 21

    def internal_sample(self) -> int:
        max_val = 2147483647
        sa = self._seed_array
        inext = self._inext + 1
        if inext >= 56:
            inext = 1
        inextp = self._inextp + 1
        if inextp >= 56:
            inextp = 1
        ret = sa[inext] - sa[inextp]
        if ret == max_val:
            ret -= 1
        if ret < 0:
            ret += max_val
        sa[inext] = ret
        self._inext = inext
        self._inextp = inextp
        return ret

    def sample(self) -> float:
        return self.internal_sample() * (1 / 2147483647)


class DotNetRandomCompatSeed:
    def __init__(self, seed: int):
        self._prng = CompatPrng(int(seed) & 0xFFFFFFFF if seed < 0 else int(seed))

    def next_double(self) -> float:
        return self._prng.sample()

    def next(self, e: int, t: int | None = None) -> int:
        if t is None:
            t = e
            e = 0
        if t <= e:
            return e
        span = t - e
        return min(int(self._prng.sample() * span) + e, t - 1)


def convert_boss_rush_seed(seed_str: str) -> int:
    chars = seed_str.lower()[::-1]
    total = 0
    for a, ch in enumerate(chars):
        v = "0123456789abcdefghijklmnopqrstuvwxyz".find(ch)
        if v < 0:
            raise ValueError(f"Invalid character in seed string: {ch}")
        total += v * 36 ** a
    while total > 2147483647:
        total //= 10
    return total


def reservoir_pick(rng: DotNetRandomCompatSeed, items: list, first: Any = None) -> Any:
    chosen = first
    count = 0
    for item in items:
        count += 1
        if rng.next(count) == 0:
            chosen = item
    return chosen


def weighted_index(rng: DotNetRandomCompatSeed, weights: list) -> int:
    total = sum(weights)
    roll = rng.next_double() * total
    cum = 0
    for i, w in enumerate(weights):
        cum += w
        if roll <= cum:
            return i
    raise RuntimeError("weightedIndex: no index selected")


def weighted_item(rng: DotNetRandomCompatSeed, items: list, weight_fn) -> Any:
    if not items:
        return None
    pairs = [(item, weight_fn(item)) for item in items]
    total = sum(w for _, w in pairs)
    if total <= 0:
        return None
    roll = rng.next_double() * total
    cum = 0
    chosen = None
    for item, w in pairs:
        chosen = item
        cum += w
        if roll <= cum:
            return item
    return chosen


def weighted_from_map(rng: DotNetRandomCompatSeed, weights: dict) -> Any:
    total = sum(weights.values())
    roll = rng.next_double() * total
    cum = 0
    chosen = None
    for item, w in weights.items():
        chosen = item
        cum += w
        if roll <= cum:
            return item
    return chosen


def roll_difficulty(rng: DotNetRandomCompatSeed, stage_idx: int, excluded: set) -> int:
    chances = load_constants()["bossRush"]["RandomSettings"]["MapDifficultyChances"]
    table = chances[stage_idx] if stage_idx < len(chances) else chances[-1]
    weights = [0 if d in excluded else float(table.get(_DIFF_NAMES[d], 0)) for d in range(4)]
    return weighted_index(rng, weights)


def generate_maps(rng: DotNetRandomCompatSeed, count: int) -> list[dict]:
    constants = load_constants()
    banned = set(constants["bossRush"]["RandomSettings"].get("BannedMaps") or [])
    result: list[dict] = []
    pool = [
        {"mapId": map_id, "difficulty": _DIFF_IDX[info["difficulty"]],
         "hasWater": bool(info.get("hasWater")), "isStandard": True}
        for map_id, info in constants["mapsInOrder"].items()
        if info.get("difficulty") in _DIFF_IDX
    ]
    while len(result) < count:
        candidates: list[dict] = []
        prev_roll = None
        excluded: set = set()
        for _ in range(100):
            if len(candidates):
                break
            if prev_roll is not None:
                excluded.add(prev_roll)
            prev_roll = roll_difficulty(rng, len(result), excluded)
            candidates = [
                c for c in pool
                if c["isStandard"] and c["difficulty"] == prev_roll
                and c["mapId"] not in banned
                and not any(c["mapId"] == r["mapId"] for r in result)
            ]
        if not candidates:
            raise RuntimeError("generateMaps: no candidate found")
        result.append(candidates[rng.next(len(candidates))])
    return result


def generate_bosses_br(rng: DotNetRandomCompatSeed, count: int) -> list[str]:
    available = load_constants()["bossRush"]["RandomSettings"].get("AvailableBosses") or []
    out: list[str] = []
    pool = list(available)
    while len(out) < count:
        if not pool:
            pool = list(available)
            last = out[-1]
            try:
                pool.pop(pool.index(last))
            except ValueError:
                pass
        pick = reservoir_pick(rng, pool)
        if pick is None:
            raise RuntimeError("generateBossesBR: empty pool")
        pool.remove(pick)
        out.append(pick)
    return out


def _read_category_lists(cat: dict) -> list[list[str]]:
    def get(key: str) -> list[str]:
        return [str(x) for x in (cat.get(key) or [])]
    return [get("Primary"), get("Military"), get("Magic"), get("Support"), get("AllTowers")]


def get_boss_special_towers(rng: DotNetRandomCompatSeed, boss: str, chance_map: dict) -> list[str]:
    spec = load_constants()["bossRush"]["RandomSettings"].get("BossSpecialTowers") or {}
    entries = spec.get(boss) or []
    if not entries:
        return []
    lists = [_read_category_lists(x) for x in entries]
    chosen: list[str] = []
    p1 = p2 = p3 = p4 = False
    chance_keys = set(chance_map.keys())
    for cat in lists:
        reuse_first = any(x in chosen for x in cat[0] + cat[1] + cat[2] + cat[3])
        effective = lists[0] if reuse_first else cat
        pool: list[str] = []
        if not p1:
            pool += effective[0]
        if not p2:
            pool += effective[1]
        if not p3:
            pool += effective[2]
        if not p4:
            pool += effective[3]
        pool += effective[4]
        existing = set(chosen)
        item = weighted_item(
            rng,
            [x for x in pool if x not in existing and x in chance_keys],
            lambda e: chance_map.get(e, 0),
        )
        if item is not None:
            chosen.append(item)
            p1 = item in effective[0]
            p2 = item in effective[1]
            p3 = item in effective[2]
            p4 = item in effective[3]
    return chosen


def is_valid_tower_set(towers: list[str], tower_settings: dict, map_id: str) -> bool:
    if len(towers) < 3:
        return False
    flags = tower_settings
    has_lead = any(flags.get(t, {}).get("canPopLead") for t in towers)
    has_camo = any(flags.get(t, {}).get("canPopCamo") for t in towers)
    cheap = sum(1 for t in towers if flags.get(t, {}).get("isCheapTower"))
    if not (has_lead and has_camo and cheap > 1):
        return False
    if map_id in WATER_MAPS and not any(t in WATER_TOWERS for t in towers):
        return False
    return True


def pick_hero(rng: DotNetRandomCompatSeed) -> str | None:
    constants = load_constants()
    br = constants["bossRush"]
    override = (br.get("Overrides") or {}).get("Hero")
    if override:
        return override if override == "ChosenPrimaryHero" or override in constants["heroesInOrder"] else None
    rs = br["RandomSettings"]
    banned = set(rs.get("BannedHeroes") or [])
    pool: list[str] = []
    if "ChosenPrimaryHero" not in banned:
        pool.append("ChosenPrimaryHero")
    pool += [h for h in constants["heroesInOrder"] if h not in banned]
    return weighted_item(rng, pool, lambda e: rs.get("HeroChances", {}).get(e, 0))


def generate_stage_towers(rng: DotNetRandomCompatSeed, boss: str, map_entry: dict,
                          prev_towers: list[str] | None) -> list[str]:
    constants = load_constants()
    rs = constants["bossRush"]["RandomSettings"]
    tower_settings = rs["TowerSettings"]
    all_towers = list(constants["towersInOrder"])
    # JS 侧传入的是「塔名 -> 概率值」的 Map，而非原始配置
    chance_map = {t: (tower_settings.get(t, {}).get("chance") or 0) for t in tower_settings}
    special = get_boss_special_towers(rng, boss, chance_map)
    banned = set(rs.get("BannedTowers") or [])
    eligible = [t for t in all_towers if t not in banned and (tower_settings.get(t, {}).get("chance") or 0) > 0]

    def by_chance(t: str) -> float:
        return tower_settings.get(t, {}).get("chance") or 0

    chosen: list[str] = []
    if prev_towers is not None:
        chosen += prev_towers
        target = len(prev_towers) + (rs.get("StageTowerIncrement") or 0)
        # JS 从 towersInOrder 顺序过滤（顺序影响加权随机结果，必须一致）
        pool = [t for t in all_towers
                if t in special and t not in chosen and by_chance(t) > 0]
        if pool:
            item = weighted_item(rng, pool, by_chance)
            if item is not None:
                chosen.append(item)
        while len(chosen) < target:
            item = weighted_item(rng, [t for t in eligible if t not in chosen], by_chance)
            if item is None:
                break
            chosen.append(item)
        return chosen

    in_pool = set(eligible)
    base_override = (constants["bossRush"].get("Overrides") or {}).get("BaseTowerSet")
    chosen += [str(t) for t in (base_override or []) if t and t in in_pool]
    if not chosen:
        # JS: c.push(...s.filter((e=>i.includes(e))))，同样按 towersInOrder 顺序
        special_set = set(special)
        chosen += [t for t in all_towers if t in special_set]
    if not chosen:
        lead = [t for t in eligible if tower_settings.get(t, {}).get("canPopLead")]
        camo = [t for t in eligible if tower_settings.get(t, {}).get("canPopCamo")]
        first = weighted_item(rng, lead, by_chance)
        if first is not None:
            chosen.append(first)
        if first is None or first not in camo:
            second = weighted_item(rng, camo, by_chance)
            if second is not None and second not in chosen:
                chosen.append(second)
    if map_entry["mapId"] in WATER_MAPS and not any(t in WATER_TOWERS for t in chosen):
        item = weighted_item(rng, [t for t in eligible if t in WATER_TOWERS], by_chance)
        if item is not None:
            chosen.append(item)
    final_count = rs.get("FinalStageTowerCount") or 8
    while len(chosen) < final_count:
        item = weighted_item(rng, [t for t in eligible if t not in chosen], by_chance)
        if item is None:
            break
        chosen.append(item)
    hero = pick_hero(rng)
    if hero is not None:
        chosen.insert(0, hero)
    return chosen


def generate_towers(rng: DotNetRandomCompatSeed, count: int,
                    maps: list[dict], bosses: list[str]) -> list[list[str]]:
    constants = load_constants()
    tower_settings = constants["bossRush"]["RandomSettings"]["TowerSettings"]
    final_map = maps[count - 1]
    final_boss = bosses[count - 1]
    final_set: list[str] | None = None
    for _ in range(100):
        attempt = generate_stage_towers(rng, final_boss, final_map, None)
        if is_valid_tower_set(attempt, tower_settings, final_map["mapId"]):
            final_set = attempt
            break
    if final_set is None:
        raise RuntimeError("generateTowers: no valid final tower set after 100 attempts")
    sets = [final_set]
    for idx in range(count - 2, -1, -1):
        sets.insert(0, generate_stage_towers(rng, bosses[idx], maps[idx], sets[0]))
    return sets


def generate_relics(rng: DotNetRandomCompatSeed, count: int, relic_names: list[str]) -> list[list[str]]:
    rs = load_constants()["bossRush"]["RandomSettings"]
    banned = set(rs.get("BannedRelics") or [])
    chances = rs.get("RelicChances") or {}
    pool = {name: float(chances.get(name, 0)) for name in relic_names if name not in banned}
    out: list[list[str]] = []
    while len(out) < count:
        carried = list(out[-1]) if out else []
        if sum(pool.values()) > 0:
            pick = weighted_from_map(rng, pool)
            if pick is not None:
                del pool[pick]
                carried.append(pick)
        out.append(carried)
    return out


def generate_boss_rush(event_id: str) -> dict:
    """给定活动 ID，生成完整阶段配置（与 BTD6 API Explorer 逻辑逐调用对齐）。"""
    constants = load_constants()
    br = constants["bossRush"]
    rs = br["RandomSettings"]
    stage_count = len(br.get("StageScores") or [])
    seed_num = convert_boss_rush_seed(event_id)
    rng = DotNetRandomCompatSeed(seed_num)
    maps = generate_maps(rng, stage_count)
    bosses = generate_bosses_br(rng, stage_count)
    towers = generate_towers(rng, stage_count, maps, bosses)
    relics = generate_relics(rng, stage_count, list((rs.get("RelicChances") or {}).keys()))
    hero_ids = set(constants["heroesInOrder"])
    first = towers[0][0] if towers[0] else None
    hero = first if first is not None and (first == "ChosenPrimaryHero" or first in hero_ids) else None

    order = {t: i for i, t in enumerate(constants["towersInOrder"])}

    def sort_key(t: str) -> int:
        return order.get(t, 99)

    per_stage = [(s[1:] if hero is not None else list(s)) for s in towers]
    per_stage = [sorted(s, key=sort_key) for s in per_stage]
    stages = []
    for idx in range(stage_count):
        prev = set(per_stage[idx - 1]) if idx > 0 else None
        cur = set(per_stage[idx])
        stage_relics = relics[idx]
        if idx == 0:
            new_relic = stage_relics[-1] if stage_relics else None
        else:
            new_relic = stage_relics[-1] if len(stage_relics) > len(relics[idx - 1]) else None
        stages.append({
            "stage": idx + 1,
            "map": maps[idx]["mapId"],
            "boss": bosses[idx],
            "towers": per_stage[idx],
            "removed": sorted([t for t in (prev or set()) if t not in cur], key=sort_key) if prev else [],
            "relics": stage_relics,
            "newRelic": new_relic,
        })
    return {
        "seed": event_id,
        "numericSeed": seed_num,
        "hero": hero,
        "availableTowers": per_stage[0],
        "stages": stages,
    }
