"""收集活动（Collection Event）Featured Insta 计划表生成器（独立，未拆分改动）。

NK 开放 API 只提供收集活动的起止时间，不提供每轮 Featured Insta 名单。
游戏内菜单的计划表由活动 ID 种子确定性生成，本模块是对 BTD6 API Explorer
（insta.min.js）逆向算法的 Python 移植，已对照游戏内截图逐轮核验：

    seed      = GetSeedLong(event_id)          # ID 逐字符十进制拼接 → 截断18位 → 有符号64位整数取绝对值
    order     = ShuffleSeeded(seed, towerOrder)  # Lehmer RNG（16807 乘子，模 2^31-1）驱动的 Fisher-Yates
    rotation  = GetPossibleInstaMonkeys(page)  # 每轮 4 塔，页长 ceil(len/4) 带回绕偏移
    时刻      = event.start + 8小时 * 轮序

64 位语义与 JS BigInt 对齐：乘法回绕 toLong（截断到无符号 64 位再落到有符号），
取模是 JS 语义（余数符号随被除数），ShuffleSeeded 依赖该语义跳过越界交换。
"""
import math

ROTATION_MS = 288 * 100_000  # 每轮 8 小时（288e5 毫秒）

# Constants.json collection.towerOrder（v49.x 数据版本转录，游戏加新塔需同步）
COLLECTION_TOWER_ORDER = [
    "Alchemist", "BananaFarm", "BombShooter", "BoomerangMonkey", "DartMonkey",
    "Druid", "GlueGunner", "HeliPilot", "IceMonkey", "MonkeyAce",
    "MonkeyBuccaneer", "MonkeySub", "MonkeyVillage", "NinjaMonkey", "SniperMonkey",
    "SpikeFactory", "SuperMonkey", "TackShooter", "WizardMonkey", "MortarMonkey",
    "EngineerMonkey", "DartlingGunner", "BeastHandler", "Mermonkey", "Desperado",
    "Skywarden",
]

_TWO64 = 1 << 64
_TWO63 = 1 << 63
_MOD_PM = 2147483647  # 2^31 - 1


def _to_long(value: int) -> int:
    """JS toLong：截断到无符号 64 位，再映射回有符号 64 位。"""
    value &= _TWO64 - 1
    if value >= _TWO63:
        value -= _TWO64
    return value


def _long_abs(value: int) -> int:
    """JS longAbs：-2^63 原样返回（BigInt 字面量边界语义）。"""
    if value == -_TWO63:
        return value
    return -value if value < 0 else value


def _js_mod(value: int, mod: int) -> int:
    """JS BigInt % ：余数符号跟随被除数（Python % 恒非负，不能直接用）。"""
    r = abs(value) % mod
    return -r if value < 0 else r


def _i64(text: str) -> int:
    """JS I64：十进制数字串的 64 位回绕累加解析。"""
    total = 0
    for ch in text:
        digit = ord(ch) - 48
        total = _to_long(_to_long(10 * total) + digit)
    return total


def get_seed_long(event_id: str) -> int:
    """活动 ID → 种子：逐字符 charCode 十进制拼接，超 18 位截断，64 位取绝对值。"""
    text = "".join(str(ord(c)) for c in str(event_id))
    if len(text) > 18:
        text = text[:18]
    return _long_abs(_i64(text))


class _SeededRandom:
    """Lehmer 随机数生成器（16807 乘子），与游戏内种子随机一致。"""

    def __init__(self, seed: int):
        if seed < 0:
            seed = -seed
        self.seed = seed

    def next(self) -> int:
        self.seed = _to_long(16807 * self.seed)
        self.seed = _js_mod(self.seed, _MOD_PM)
        return self.seed

    def range(self, low: int, high: int) -> int:
        if low == high:
            return low
        return low + _js_mod(self.next(), high - low)


def shuffle_seeded(seed: int, items: list) -> list:
    """Fisher-Yates 洗牌（正序循环版）；JS 语义下 range 可能取负，越界交换跳过。"""
    rng = _SeededRandom(seed)
    out = list(items)
    size = len(out)
    for i in range(size):
        j = rng.range(i, size)
        if j < 0 or j >= size:
            continue
        out[i], out[j] = out[j], out[i]
    return out


def possible_instas(page: int, order: list[str]) -> list[str]:
    """GetPossibleInstaMonkeys 移植：页长 ceil(len/4)，超页回绕并累加偏移，取 4 塔。"""
    if not order:
        return []
    total = len(order)
    page_size = math.ceil(0.25 * total)
    n, extra = page, 0
    while page_size < n:
        n -= page_size
        extra += 1
    return [order[(k + extra + 4 * n) % total] for k in range(4)]


def generate_collection_schedule(event_id: str, start_ms: int, end_ms: int) -> dict:
    """由活动 ID/起止生成整期计划表：rotations[轮序] = 4 塔名列表。"""
    seed = get_seed_long(event_id)
    order = shuffle_seeded(seed, COLLECTION_TOWER_ORDER)
    count = max(0, math.ceil((end_ms - start_ms) / ROTATION_MS))
    rotations = {i: possible_instas(i, order) for i in range(count)}
    return {"id": str(event_id), "seed": seed, "order": order, "rotations": rotations}


def rotation_start(start_ms: int, index: int) -> int:
    return start_ms + ROTATION_MS * index
