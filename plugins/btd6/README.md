# btd6 插件模块地图

单文件（原 5127 行 `__init__.py`）拆分为分层包。**依赖单向，禁止循环导入**：

```
util(纯工具) ──┐
i18n(中文语料) ┤
nkapi(取数层) ─┼─→ assets(素材) ─→ collect(采集) ─→ textfmt(文本) ─┐
               │                              └─→ cards(卡片渲染) ─┼─→ push(后台任务) ─→ handlers(命令)
               └──────────────────────────────────────────────────┘
```

| 模块 | 职责 |
| --- | --- |
| `util.py` | 时间/状态/格式化纯工具（fmt_*、pick_*、bucket_now、_state_of、_esc 等），零包内依赖 |
| `nkapi.py` | 取数层：URL 常量、`_validate_url`、`_http_get`、`fetch_body`、`_refresh_url`、缓存/`_stale_*`/`_lb_next_cache`、字节预算、`fetch_leaderboard_paginated`、`REQUEST_LIMIT`、冷却表（`_enforce_cooldown`/`_release_cooldown`）、日志 OAK 脱敏（`_mask_url`） |
| `i18n.py` | 全部中文语料：BOSS_CN/TOWER_CN/HERO_CN + `_*_CN_FLAT` 归一化查找、`relic_cn`、`_EVENT_NAME_CN`、`_ODYSSEY_DIFFS`、HELP/LB 文案；查表未命中按"类别:名称"去重告警一次（`_warn_unknown`，提醒同步语料表） |
| `assets.py` | 素材层：CDN 图/本地立绘/UI 图标/site 站点风素材（OAK 档案页 `_site_asset_data_url`）→ data: URL，内存+落盘缓存与字节预算、`_tower_icon`/`_boss_event_asset` 等映射；每日挑战用官方 DailyChallengeBtn 图标（取自 BTD6 API Explorer） |
| `collect.py` | 采集层：`collect_overview/daily/daily_coop/rules/maps/odyssey/player/player_oak/rush/collectevent/ct/leaderboard(_page)`、`site_data()` 与 `profile_*`（OAK 档案页的网站口径统计）、`_coop_pick`（Co-op 选期：createdAt ≤ 当前取最新）、`_challenge_map_img`、`fetch_leaderboard_page`、`fetch_rank_entry`、`_safe` |
| `textfmt.py` | 文本渲染：`build_overview`、`_single_event_text`、`format_rules`、排行/地图/远征/玩家/rush/收集活动/CT 文本；`odyssey_text(only=)` 支持只输出部分难度（部分卡片渲染失败时的回退） |
| `cards/` | 渲染层：`common`(外壳 CSS)/`overview`/`leaderboard`/`odyssey`/`rules`/`rush`/`player`/`collectevent`/`ctmap`/`help`；`__init__` 为渲染管线（`_render_card`/`_send_card`/`_finish_multi_cards`，内容哈希缓存）并统一再导出 |
| `push.py` | 后台任务：history.json 归档、`_prewarm_once` 预热、活动刷新精准推送（race/boss/ct/odyssey/daily/coop/rush 七类，各类仅采样自己的列表接口）与全部 apscheduler 定时任务 |
| `handlers.py` | 18 个 nonebot matcher/命令 handler 与参数解析（`parse_kind` 等）；规则命令已拆分：`.btd6竞速`（竞赛规则）与 `.btd6boss`（Boss 标准+精英双卡）；`.btd6每日` 一次并发取标准+高级+Coop 三卡；命令参数词表在此 |
| `rushgen.py` | Boss Rush 阶段数据生成器（独立，未拆分改动） |
| `instagen.py` | 收集活动 Featured Insta 计划表生成器（独立）：活动 ID 种子 → 洗牌 → 8 小时轮换 4 塔，BTD6 API Explorer 算法移植 |
| `ctmap.py` | CT 领土六边形棋盘布局（独立）：格子 id ↔ 轴向坐标、出生点、模式/队伍配色，ct.min.js 移植 |
| `roundsets.py` | 自定义回合组明细：本地快照（`assets/data/roundsets/*.json`）与 DefaultRoundSet 逐回合对比，展开为"第N回合：改为…"中文摘要 |
| `__init__.py` | 入口：包语义 shim（`__path__` 补齐，E402 豁免见 pyproject）+ 全量再导出（保持 `btd6.<名字>` 可访问）+ 导入 handlers/push 触发注册 |

## 约定

- **跨模块引用一律模块属性访问**（`from . import nkapi` + `nkapi.fetch_body(...)`），保证
  `monkeypatch.setattr(btd6.nkapi.xxx, ...)` 在任意调用点生效。例外：`handlers.py` 中模块
  `cards` 以 `cards as cards_mod` 别名导入（局部列表变量 `cards` 会遮蔽模块名）。
- **数据路径**统一基于包目录解析（`os.path.dirname(__file__)`）：`cache/`、`assets/`、
  `history.json`、`state.json` 位置与拆分前完全一致。
- **手工维护的数据快照**（游戏更新后需同步，未收录名称会在运行日志告警一次）：
  `rushdata.json`（Constants v3.0.0 裁剪）、`site_data.json`（站点裁剪常量）、
  `i18n.py` 语料表、`instagen.COLLECTION_TOWER_ORDER`、`assets/data/roundsets/`。
- 测试 patch 目标：`btd6.nkapi.*`（fetch_body/_http_get/get_http_client/MAX_JSON_MEM_BYTES）、
  `btd6.assets.*`（ASSET_DIR/GAME_ASSET_DIR/CACHE_DIR）、`btd6.cards.*`（_render_card/render_html_to_png）、
  `btd6.collect.*`（collect_*）、`btd6.push.*`（get_bot/HISTORY_FILE/BTD6_PUSH_STATE_FILE/_prewarm_running/_archive_events）。
