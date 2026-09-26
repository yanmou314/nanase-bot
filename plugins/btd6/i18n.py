"""中文语料层：全插件唯一规范翻译表与文案常量（含归一化查找函数）。"""
import logging

_logger = logging.getLogger(__name__)

# 译名表是手工转录的（随游戏版本更新需同步）：查表未命中回退原文时
# 按"类别:名称"去重告警一次，提醒维护者补表，避免静默退化无人察觉。
_warned_unknown: set[str] = set()
_WARNED_UNKNOWN_MAX = 256


def _warn_unknown(kind: str, raw: str) -> None:
    if not raw:
        return
    key = f"{kind}:{raw}"
    if key in _warned_unknown:
        return
    if len(_warned_unknown) >= _WARNED_UNKNOWN_MAX:
        _warned_unknown.clear()  # 防御性上限：异常数据源刷屏时不无限增长
    _warned_unknown.add(key)
    _logger.warning("BTD6 语料表未收录%s「%s」（游戏更新后需同步 i18n.py）", kind, raw)


HELP_GROUPS = [
    ("活动", [
        (".btd6活动", "当前竞赛/Boss/争夺领土/远征总览（三段式）"),
        (".btd6每日", "今日每日挑战（标准+高级+Coop 一起返回）"),
        (".btd6竞速", "竞赛活动规则详情"),
        (".btd6boss", "Boss 活动规则详情（标准+精英一起返回）"),
        (".btd6远征", "当前远征 Odyssey"),
        (".btd6ct", "争夺领土详情（六边形领土地图）"),
        (".btd6rush", "Boss Rush 冲刺"),
        (".btd6收集", "收集活动 Featured Insta 计划表（每8小时轮换4种）"),
    ]),
    ("排行与档案", [
        (".btd6排行 竞赛|boss|领土 [P页码|排名]", "排行榜：默认前25；P2=第2页；数字=该名次玩家档案（Boss双榜/领土双榜自动返回）"),
        (".btd6玩家 <OAK>", "玩家档案（含存档数据，OAK 在游戏账号设置里生成）"),
        (".btd6地图 最新|热门|点赞 [数量]", "自制地图榜单"),
        (".btd6历史 [竞速|boss|领土|远征|每日] [数量]", "本地归档的历史活动（API 只保留近几期）"),
        (".btd6预热", "手动预热全部活动（仅主人）"),
    ]),
]

HELP_TEXT = """🐒 BTD6 情报站（气球塔防6）
.btd6活动 — 当前竞赛/Boss/争夺领土/远征总览（三段式：进行中/即将开始/已结束）
.btd6每日 — 今日每日挑战（标准+高级+Coop 一起返回）
.btd6竞速 — 竞赛活动规则详情
.btd6boss — Boss 活动规则详情（标准+精英一起返回）
.btd6远征 — 当前远征活动
.btd6ct — 争夺领土详情（六边形领土地图：逐格地图/模式/遗物/出生点）
.btd6rush — Boss Rush 冲刺
.btd6收集 — 收集活动 Featured Insta 计划表（每8小时轮换4种精选即时猴）
.btd6排行 竞赛|boss|领土 [P页码|排名] — 排行榜：默认前25；P2=第2页；数字=该名次玩家档案（Boss标准+精英、领土个人+战队一起返回）
.btd6玩家 <OAK> — 玩家档案（OAK 查询，含存档数据；OAK 在游戏设置→账号中生成，请私聊使用）
.btd6地图 最新|热门|点赞 [数量] — 自制地图榜单
.btd6历史 [竞速|boss|领土|远征|每日] [数量] — 本地归档的历史活动（API 只保留近几期）
.btd6预热 — 手动预热全部活动（仅主人）
数据源：Ninja Kiwi 官方开放数据接口"""
LB_USAGE = "用法：.btd6排行 竞赛|boss|领土 [P页码|排名] [标准|普通|精英]\n例：.btd6排行 boss — 前25\n.btd6排行 boss精英 — 精英前25\n.btd6排行 boss 3 — 第3名（标准+精英各一张玩家卡）\n.btd6排行 boss精英 3 / .btd6排行 boss精英3 — 仅精英第3名玩家档案\n.btd6排行 竞赛 7 — 竞赛第7名玩家档案"


# ---------------- 常用名词翻译（全插件唯一规范表：大写驼峰主键，查找时归一化） ----------------

# Boss 事件：总览/规则/每日推送与 Boss Rush 卡共用一套译名。
# 历史上有两套不一致的表（总览"幻影" vs rush 卡"法泽"），已合并：以官方简体译名为准。
BOSS_CN = {
    "Bloonarius": "布隆纳留斯", "Lych": "巫妖", "Vortex": "漩涡",
    "Dreadbloon": "恐怖气球", "Phayze": "菲茨", "Blastapopoulos": "轰炸飞艇",
    "Diamondback": "菱背",
    # NK API 历史拼写变体
    "Blastapopolous": "轰炸飞艇",
}
DIFFICULTY_CN = {
    # 地图分级
    "Beginner": "初级", "Intermediate": "中级", "Advanced": "高级", "Expert": "专家",
    # 游戏内难度（活动元数据用）
    "Easy": "简单", "Medium": "中等", "Hard": "困难",
}
# 译名对齐 BWIKI《游戏模式》：https://wiki.biligame.com/btd6/游戏模式
# Impoppable BWIKI 作「不可击破」（又译极难）；Half Cash 作「金币减半」；
# CHIMPS BWIKI 作「超猩星」（国际服客户端烂译「点击」，社区亦常直接写 CHIMPS）。
MODE_CN = {
    "Standard": "标准", "Reverse": "反向", "Apopalypse": "天启",
    "Half Cash": "金币减半", "HalfCash": "金币减半", "HalfMoney": "金币减半",
    "Double HP": "双倍血量", "DoubleHP": "双倍血量",
    "CHIMPS": "超猩星", "Clicks": "超猩星",
    "Deflation": "放气",
    "Impoppable": "不可击破",
    "DoubleMoabHealth": "双倍生命MOAB", "DoubleHpMoabs": "双倍生命MOAB",
    "AlternateBloonsRounds": "替代气球回合", "AlternateBloons": "替代气球回合", "ABR": "替代气球回合",
    "Only": "仅限",
    "MagicMonkeysOnly": "仅魔法", "MagicOnly": "仅魔法",
    "PrimaryMonkeysOnly": "仅初级", "PrimaryOnly": "仅初级",
    "MilitaryMonkeysOnly": "仅军事", "MilitaryOnly": "仅军事",
    "DoubleCash": "双倍现金",
    "Sandbox": "沙盒",
}
# 归一化别名：去空格 + 小写（兼容 "Half Cash" / "HalfCash" 等 API 写法）
_MODE_CN_FLAT = {k.replace(" ", "").lower(): v for k, v in MODE_CN.items()}
MAP_CN = {
    # Beginner（译名对齐 B 站气球塔防6 WIKI；Frozen Over 游戏内为「冰封三尺」）
    "Tutorial": "教程", "MonkeyMeadow": "猴子草甸",
    "InTheLoop": "循环", "SkullTweak": "骷髅改",
    "ThreeMinesAround": "三圈矿道", "SpaPits": "水疗温泉",
    "Tinkerton": "工匠坊",
    "TreeStump": "树桩", "TownCentre": "镇中心", "MiddleOfTheRoad": "道路中间",
    "OneTwoTree": "一二杉", "Scrapyard": "废料场", "TheCabin": "小木屋",
    "Resort": "度假胜地", "Skates": "滑冰", "LotusIsland": "莲花岛",
    "CandyFalls": "糖果瀑布", "WinterPark": "冬季公园", "Carved": "鬼脸南瓜",
    "ParkPath": "公园路径", "AlpineRun": "高山竞速", "FrozenOver": "冰封三尺",
    "Cubism": "立体主义", "FourCircles": "四圈跑道", "Hedge": "树篱",
    "Logs": "原木", "EndOfTheRoad": "道路尽头",
    # Intermediate
    "LostCrevasse": "失落冰隙", "LuminousCove": "夜光海湾", "AncientPortal": "古代传送门",
    "SulfurSprings": "硫磺泉", "WaterPark": "水上乐园", "Polyphemus": "独眼巨人",
    "CoveredGarden": "隐蔽的花园", "Quarry": "采石场", "QuietStreet": "静谧街道",
    "BloonariusPrime": "布隆纳留斯精英", "Balance": "平衡", "Encrypted": "已加密",
    "Bazaar": "集市", "AdorasTemple": "阿多拉神庙", "SpringSpring": "春意盎然",
    "KartsNDarts": "飞镖卡丁车", "MoonLanding": "登月", "Haunted": "鬼屋",
    "Downstream": "顺流而下", "FiringRange": "靶场", "Cracked": "龟裂之地",
    "Streambed": "河床", "Chutes": "滑槽", "Rake": "耙",
    "SpiceIslands": "香料群岛",
    # Advanced
    "Ascent": "攀升", "MushroomGrotto": "蘑菇洞窟", "PartyParade": "派对游行",
    "SunsetGulch": "日落峡谷", "EnchantedGlade": "魔法林地", "LastResort": "破釜沉舟",
    "CastleRevenge": "城堡复仇", "DarkPath": "黑暗之径", "Erosion": "侵蚀",
    "MidnightMansion": "午夜豪宅", "SunkenColumns": "凹陷的柱子", "XFactor": "X因子",
    "Mesa": "桌子山", "Geared": "齿轮传动", "Spillway": "泄洪道", "Cargo": "货运",
    "PatsPond": "帕特的池塘", "Peninsula": "半岛", "HighFinance": "高级金融",
    "AnotherBrick": "另一块砖", "OffTheCoast": "海岸", "Cornfield": "玉米地",
    "Underground": "地下",
    # Expert
    "TrickyTracks": "棘手的轨道", "GlacialTrail": "冰河之径", "DarkDungeons": "黑暗地下城",
    "Sanctuary": "避难所", "Ravine": "峡谷", "FloodedValley": "水淹山谷",
    "Infernal": "炼狱", "BloodyPuddles": "血腥水坑", "Workshop": "工坊",
    "Quad": "方院", "DarkCastle": "黑暗城堡", "MuddyPuddles": "泥泞的水坑",
    "#ouch": "哇是个#",
}
SCORING_CN = {"GameTime": "最快用时", "LeastCash": "最少现金", "LeastTiers": "最少升级"}

# 归一化别名：去空格 + 小写（兼容 NK API 的 CamelCase 与带空格两种写法；已核实
# 上游 metadata.map 为 CamelCase 无空格格式，如 "AdorasTemple"/"ThreeMinesAround"）
_MAP_CN_FLAT = {k.replace(" ", "").lower(): v for k, v in MAP_CN.items()}

TOWER_CN = {
    "DartMonkey": "飞镖猴", "BoomerangMonkey": "回旋镖猴", "BombShooter": "炸弹射手",
    "TackShooter": "钉子射手", "IceMonkey": "冰猴", "GlueGunner": "胶水枪手",
    "SniperMonkey": "狙击猴", "MonkeySub": "潜艇猴", "MonkeyBuccaneer": "海盗猴",
    "MonkeyAce": "飞机猴",
    "HeliPilot": "直升机猴", "MortarMonkey": "迫击炮猴", "DartlingGunner": "连发枪手",
    "WizardMonkey": "巫师猴", "SuperMonkey": "超级猴", "NinjaMonkey": "忍者猴",
    "Alchemist": "炼金术士", "Druid": "德鲁伊", "BananaFarm": "香蕉农场",
    "EngineerMonkey": "工程师猴", "SpikeFactory": "尖刺工厂", "MonkeyVillage": "猴村",
    "BeastHandler": "驯兽师", "Mermonkey": "人鱼猴", "Desperado": "亡命徒猴",
    "Skywarden": "天空守卫",
}
HERO_CN = {
    "Quincy": "昆西", "Gwendolin": "格温多林", "StrikerJones": "琼斯",
    "ObynGreenfoot": "奥宾", "CaptainChurchill": "丘吉尔", "Benjamin": "本杰明",
    "Ezili": "伊兹莉", "PatFusty": "帕特", "Adora": "阿朵拉", "AdmiralBrickell": "布里克",
    "Etienne": "艾蒂安", "Sauda": "绍达", "Psi": "赛", "Geraldo": "杰拉尔多",
    "Corvus": "科沃斯", "Rosalia": "罗莎莉娅", "Silas": "塞拉斯", "DanDMonke": "丹迪猴",
}
# 归一化别名：去空格 + 小写（兼容 NK API 的 "Dart Monkey"/"DartMonkey" 两种写法）
_TOWER_CN_FLAT = {k.replace(" ", "").lower(): v for k, v in TOWER_CN.items()}
_HERO_CN_FLAT = {k.replace(" ", "").lower(): v for k, v in HERO_CN.items()}
_BOSS_CN_FLAT = {k.replace(" ", "").lower(): v for k, v in BOSS_CN.items()}

FLAG_LABELS = [
    ("disableMK", "猴子知识"), ("disablePowers", "力量道具"), ("disableInstas", "即时塔"),
    ("disableSelling", "卖塔"), ("noContinues", "重开续命"), ("disableDoubleCash", "双倍启动现金"),
]


def cn(value, mapping: dict) -> str:
    raw = str(value or "").strip()
    hit = mapping.get(raw)
    if hit:
        return hit
    # 扁平化回退：去空格 + 小写，兼容 API CamelCase / 带空格两种写法
    flat = {k.replace(" ", "").lower(): v for k, v in mapping.items()} if mapping else {}
    return flat.get(raw.replace(" ", "").lower(), raw)


def mode_cn(value: str) -> str:
    """游戏模式内部名 → 中文（MODE_CN，FLAT 归一化查找）；查不到回退原名。"""
    return cn(value, MODE_CN)


def boss_cn(boss_type: str) -> str:
    raw = str(boss_type or "").strip()
    hit = _BOSS_CN_FLAT.get(raw.replace(" ", "").lower()) or BOSS_CN.get(raw)
    if not hit:
        _warn_unknown("Boss", raw)
        return raw
    return hit


def tower_cn(name: str) -> str:
    flat = str(name or "").replace(" ", "").lower()
    hit = _TOWER_CN_FLAT.get(flat) or _HERO_CN_FLAT.get(flat)
    if not hit:
        _warn_unknown("塔/英雄", str(name or "").strip())
        return str(name or "")
    return hit


def map_cn(name: str) -> str:
    """地图内部名 → 中文译名（MAP_CN 表，FLAT 归一化查找）；查不到回退原名。"""
    raw = str(name or "").strip()
    hit = _MAP_CN_FLAT.get(raw.replace(" ", "").lower())
    if not hit:
        # MAP_CN 只覆盖部分地图（activity 常用图），快照外新图不告警：
        # rushgen.mapsInOrder 全量表在 rushdata.json，此处回退原名即可
        return raw
    return hit


def hero_cn(name: str) -> str:
    flat = str(name or "").replace(" ", "").lower()
    hit = _HERO_CN_FLAT.get(flat)
    if not hit:
        _warn_unknown("英雄", str(name or "").strip())
        return str(name or "")
    return hit


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
    "Techbot": "科技机器人",  # CT 社区数据集 daily_powers 的拼写变体
    "Thrive": "繁荣", "BattleCat": "战斗猫",
}


def _odyssey_power_name(raw: str) -> str:
    raw = str(raw or "").strip()
    hit = _ODYSSEY_POWER_CN.get(raw)
    if not hit:
        _warn_unknown("力量", raw)
        return raw
    return hit


def relic_cn(name: str) -> str:
    """遗物内部名 → 中文译名；未收录时告警一次并回退原名。"""
    raw = str(name or "").strip()
    hit = _RELIC_CN.get(raw)
    if not hit:
        _warn_unknown("遗物", raw)
        return raw
    return hit


_RACE_TITLE_CN = {
    "three mines back around": "三矿往返",
}


_ROUND_SET_CN = {
    "default": "默认回合",
    "phayze": "幻影回合",
    "dreadbloon": "恐惧气球岩回合",
    "vortex": "漩涡回合",
    "lych": "巫妖回合",
    "bloonarius": "膨胀气球神回合",
    "blastapopoulos": "爆裂魔炎回合",
    "diamondback": "菱背回合",
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


# NK /btd6/events 的 name 字段为英文固定模板名，这里统一汉化
_EVENT_NAME_CN = {
    "A Boss Rush Event": "Boss 竞速冲刺",
    "A Contested Territory": "争夺领土",
    "A Social Season Event": "社交赛季活动",
    "A Boss Event": "Boss 战活动",
    "A Race Event": "竞速活动",
    "An Odyssey Event": "远征活动",
    "A Collectables Event": "收集活动",
}


_REWARD_LABELS = {
    "MonkeyMoney": "猴币", "Trophy": "奖杯", "TeamTrophy": "战队奖杯",
    "CollectionEvent": "收集事件", "RandomPower": "随机强化", "RandomInstaMonkey": "随机香蕉",
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
