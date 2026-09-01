"""中文语料层：全插件唯一规范翻译表与文案常量（含归一化查找函数）。"""


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


# ---------------- 常用名词翻译（全插件唯一规范表：大写驼峰主键，查找时归一化） ----------------

# Boss 事件：总览/规则/每日推送与 Boss Rush 卡共用一套译名。
# 历史上有两套不一致的表（总览"幻影" vs rush 卡"法泽"），已合并：以官方简体译名为准。
BOSS_CN = {
    "Bloonarius": "膨胀气球神", "Lych": "巫妖", "Vortex": "漩涡",
    "Dreadbloon": "恐惧气球岩", "Phayze": "菲茨", "Blastapopoulos": "爆裂魔炎",
    "Diamondback": "菱背龙",
    # NK API 历史拼写变体
    "Blastapopolous": "爆裂魔炎",
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

# 归一化别名：去空格 + 小写（兼容 NK API 的 CamelCase 与带空格两种写法；已核实
# 上游 metadata.map 为 CamelCase 无空格格式，如 "AdorasTemple"/"ThreeMinesAround"）
_MAP_CN_FLAT = {k.replace(" ", "").lower(): v for k, v in MAP_CN.items()}

TOWER_CN = {
    "DartMonkey": "飞镖猴", "BoomerangMonkey": "回旋镖猴", "BombShooter": "炸弹射手",
    "TackShooter": "钉子射手", "IceMonkey": "冰猴", "GlueGunner": "胶水枪手",
    "SniperMonkey": "狙击猴", "MonkeySub": "潜艇猴", "MonkeyBuccaneer": "海盗猴",
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
    return mapping.get(raw, raw)


def boss_cn(boss_type: str) -> str:
    raw = str(boss_type or "").strip()
    return _BOSS_CN_FLAT.get(raw.replace(" ", "").lower()) or BOSS_CN.get(raw) or raw


def tower_cn(name: str) -> str:
    flat = str(name or "").replace(" ", "").lower()
    return _TOWER_CN_FLAT.get(flat) or _HERO_CN_FLAT.get(flat) or name


def map_cn(name: str) -> str:
    """地图内部名 → 中文译名（MAP_CN 表，FLAT 归一化查找）；查不到回退原名。"""
    raw = str(name or "").strip()
    return _MAP_CN_FLAT.get(raw.replace(" ", "").lower()) or raw


def hero_cn(name: str) -> str:
    flat = str(name or "").replace(" ", "").lower()
    return _HERO_CN_FLAT.get(flat) or name


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
