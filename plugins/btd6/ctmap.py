"""CT（争夺领土）地图布局：六边形棋盘 id ↔ 轴向坐标（BTD6 API Explorer ct.min.js 移植）。

CT 棋盘是 7 环六边形（中心 MRX + 6 环共 163 张地图格 + 第 7 环 6 个出生点，
NK tiles API 共 169 格）。格子 id 三位大写字母：方向前缀(A-F) + 行字母 + 环字母
（环 1→G … 7→A，中心 MRX）；出生点 id = {队伍}{A}{A}（如 AAA/DAH 不存在，
FAG 因与出生点歧义更名为 FAH）。坐标与绘制几何均与站点 renderCTMap 一致。
"""
import math

# 轴向六方向（q,r）与格子 id 前缀，顺序即站点 buildCTGrid 的定义
_DIRECTIONS = ((1, 0), (1, -1), (0, -1), (-1, 0), (-1, 1), (0, 1))
_PREFIX = ("A", "B", "C", "D", "E", "F")

# 出生点队伍色（站点 teamColors；M=中心）
TEAM_COLORS = {
    "A": "#9C55E4", "B": "#E978AA", "C": "#00DD6B",
    "D": "#04A6F3", "E": "#F7D302", "F": "#F4413F", "M": "#B9E546",
}

# 模式底色（站点 updateCTBackground gameType 分支）
GAMETYPE_COLORS = {
    2: "#E12900",   # 竞速 Race
    4: "#9F00FF",   # Boss
    8: "#FFCC00",   # 最少现金 LeastCash
    9: "#35DA00",   # 最少层数 LeastTiers
}
GAMETYPE_DEFAULT_COLOR = "#5c6b73"

GAMETYPE_CN = {2: "竞速", 4: "Boss", 8: "最少现金", 9: "最少层数"}

# Boss 格 bossBloon 索引 → Boss 名（Constants.json bossesInOrder）
BOSSES_IN_ORDER = ("Bloonarius", "Lych", "Vortex", "Dreadbloon",
                   "Phayze", "Blastapopoulos", "Diamondback")

SQRT3 = math.sqrt(3)


def _ring_tiles(t: int) -> list[dict]:
    """第 t 环的轴向坐标序列（与站点 _ring 生成顺序一致，供 id↔坐标映射）。"""
    if t == 0:
        return [{"q": 0, "r": 0}]
    node = {"q": _DIRECTIONS[0][0] * t, "r": _DIRECTIONS[0][1] * t}
    order = (4, 3, 2, 1, 0, 5)
    out: list[dict] = []
    for i in order:
        for _ in range(t):
            out.append(dict(node))
            node = {"q": node["q"] + _DIRECTIONS[i][0], "r": node["r"] + _DIRECTIONS[i][1]}
    return out


def _row_letter(r: int) -> str:
    """行偏移 u → 中间字母：0→A，正数 A,C,E…，负数 B,D,F…（站点 s()）。"""
    if r == 0:
        return "A"
    t = abs(r)
    n = 2 * t - 1 if r > 0 else 2 * t
    return chr(ord("A") + n)


def _u_offset(e: int) -> int:
    """环内步进偏移（站点 i()）：0→0，奇数向上取整取正、偶数取负。"""
    if e == 0:
        return 0
    return math.ceil(e / 2) * (1 if e % 2 else -1)


def _ring_letter(e: int) -> str:
    """环号 → 环字母：1→G … 7→A（站点 r()）。"""
    return chr(ord("G") - (e - 1))


def build_ct_grid() -> dict:
    """重建棋盘 id ↔ 轴向坐标映射（FAG→FAH 更名与站点一致）。

    返回 {"tiles": {id: (q,r)}, "spawns": [{"team","q","r","id"}]}；
    tiles 不含 6 个出生点（站点逻辑：第 7 环每方向跳过步进 0 的位置）。
    """
    coord_to_id: dict[tuple, str] = {(0, 0): "MRX"}
    id_to_coord: dict[str, tuple] = {"MRX": (0, 0)}
    for e in range(1, 8):
        ring = _ring_tiles(e)
        c = _ring_letter(e)
        p = len(ring)
        for a in range(6):
            r = a * e
            steps = e - 1 if e == 7 else e
            for h in range(steps):
                u = _u_offset(h + 1 if e == 7 else h)
                m = ring[(r + u) % p]
                bid = _PREFIX[a] + _row_letter(u) + c
                if bid == "FAG":
                    bid = "FAH"
                coord_to_id[(m["q"], m["r"])] = bid
                id_to_coord[bid] = (m["q"], m["r"])
    ring7 = _ring_tiles(7)
    spawns = []
    for a in range(6):
        s = ring7[a * 7]
        spawns.append({"team": _PREFIX[a], "q": s["q"], "r": s["r"],
                       "id": f"{_PREFIX[a]}AA"})
    return {"tiles": id_to_coord, "spawns": spawns}


def hex_position(q: int, r: int, size: float) -> tuple[float, float]:
    """轴向坐标 → 中心点像素（站点 l()，镜像翻正便于布局）。"""
    return SQRT3 * (q + r / 2) * size, size * 1.5 * r


def board_metrics(tiles: dict, spawns: list, size: float, pad: float = 14.0):
    """整张棋盘的包围盒 → (width, height, offset_x, offset_y)。

    tiles/spawns 的中心点经 hex_position 计算后，包围盒外扩 size+pad。
    """
    xs: list[float] = []
    ys: list[float] = []
    for q, r in list(tiles.values()) + [(s["q"], s["r"]) for s in spawns]:
        x, y = hex_position(q, r, size)
        xs.extend((x - size, x + size))
        ys.extend((y - size, y + size))
    left, top = min(xs) - pad, min(ys) - pad
    right, bottom = max(xs) + pad, max(ys) + pad
    return right - left, bottom - top, left, top


# 格子类型底色（站点 updateCTBackground tileType 分支：Game Types/Heroes 预设用）
TILE_TYPE_COLORS = {
    "Relic": "#D621E7", "Banner": "#1855A5",
    "Regular": "#B9E546", "TeamFirstCapture": "#B9E546",
}
TILE_TYPE_DEFAULT_COLOR = "#888888"


def tile_type_color(tile_type: str) -> str:
    return TILE_TYPE_COLORS.get(str(tile_type or ""), TILE_TYPE_DEFAULT_COLOR)


def tile_heroes(gd: dict) -> list[str]:
    """simplifyTowerData 移植：dcModel 中 isHero 且 max!=0 的英雄名列表。"""
    items = ((gd.get("dcModel") or {}).get("towers") or {}).get("_items") or []
    return [str(it.get("tower") or "") for it in items
            if it.get("isHero") and it.get("max") != 0]


def hero_icon_name(gd: dict) -> str:
    """站点英雄层图标选择：单英雄→该英雄；无英雄→未选择；多英雄/自选→全体。"""
    heroes = tile_heroes(gd)
    if len(heroes) == 1 and heroes[0] != "ChosenPrimaryHero":
        return f"HeroIcon{heroes[0]}"
    if not heroes:
        return "NoHeroSelected"
    return "AllHeroesIcon"


def gametype_icon(gd: dict) -> tuple[str, int]:
    """站点 getCTGameTypeIconPath 移植：模式图标文件名与 Boss 层数。"""
    sub = (gd.get("subGameType"))
    if sub == 2:
        return "EventRaceBtn", 0
    if sub == 4:
        idx = int(((gd.get("bossData") or {}).get("bossBloon")) or 0)
        boss = BOSSES_IN_ORDER[idx] if 0 <= idx < len(BOSSES_IN_ORDER) else ""
        tier = int(((gd.get("bossData") or {}).get("TierCount")) or 0)
        return f"{boss}EventIcon", tier
    if sub == 8:
        return "LeastCashIcon", 0
    if sub == 9:
        return "LeastTiersIcon", 0
    return "", 0


# 地图主题（Constants.json mapsInOrder，Default 预设地形纹理用）
MAP_THEMES = {
 "#ouch": "City",
 "AdorasTemple": "Grass",
 "AlpineRun": "Snow",
 "AncientPortal": "DarkGrass",
 "AnotherBrick": "City",
 "Ascent": "Grass",
 "Balance": "Grass",
 "Bazaar": "Dirt",
 "BloodyPuddles": "DarkGrass",
 "BloonariusPrime": "DarkGrass",
 "CandyFalls": "Grass",
 "Cargo": "City",
 "Carved": "DarkGrass",
 "CastleRevenge": "City",
 "Chutes": "Grass",
 "Cornfield": "City",
 "CoveredGarden": "City",
 "Cracked": "Dirt",
 "Cubism": "City",
 "DarkCastle": "DarkGrass",
 "DarkDungeons": "DarkGrass",
 "DarkPath": "Snow",
 "Downstream": "Grass",
 "EnchantedGlade": "DarkGrass",
 "Encrypted": "DarkGrass",
 "EndOfTheRoad": "Dirt",
 "Erosion": "Water",
 "FiringRange": "Dirt",
 "FloodedValley": "Water",
 "FourCircles": "City",
 "FrozenOver": "Snow",
 "Geared": "City",
 "GlacialTrail": "Snow",
 "Haunted": "DarkGrass",
 "Hedge": "City",
 "HighFinance": "City",
 "InTheLoop": "DarkGrass",
 "Infernal": "MountainCave",
 "KartsNDarts": "City",
 "LastResort": "Snow",
 "Logs": "Grass",
 "LostCrevasse": "Snow",
 "LotusIsland": "Water",
 "LuminousCove": "Water",
 "Mesa": "MountainCave",
 "MiddleOfTheRoad": "Dirt",
 "MidnightMansion": "City",
 "MoonLanding": "MountainCave",
 "MuddyPuddles": "Dirt",
 "MushroomGrotto": "DarkGrass",
 "OffTheCoast": "Water",
 "OneTwoTree": "Snow",
 "ParkPath": "Grass",
 "PartyParade": "City",
 "PatsPond": "Water",
 "Peninsula": "Water",
 "Polyphemus": "Water",
 "Quad": "City",
 "Quarry": "Dirt",
 "QuietStreet": "Snow",
 "Rake": "Grass",
 "Ravine": "DarkGrass",
 "Resort": "Dirt",
 "Sanctuary": "MountainCave",
 "Scrapyard": "Dirt",
 "Skates": "Snow",
 "SkullTweak": "MountainCave",
 "SpaPits": "Grass",
 "SpiceIslands": "Water",
 "Spillway": "City",
 "SpringSpring": "Water",
 "Streambed": "Grass",
 "SulfurSprings": "MountainCave",
 "SunkenColumns": "MountainCave",
 "SunsetGulch": "Dirt",
 "TheCabin": "DarkGrass",
 "ThreeMinesAround": "Dirt",
 "Tinkerton": "City",
 "TownCentre": "Grass",
 "TreeStump": "Grass",
 "TrickyTracks": "City",
 "Tutorial": "Grass",
 "Underground": "MountainCave",
 "WaterPark": "City",
 "WinterPark": "Snow",
 "Workshop": "City",
 "XFactor": "DarkGrass"
}
