---
feature: audit-round1
status: designed
updated: 2026-09-12
branch: fix/audit-round1
commits: 
---

# 代码审查修复（Round 1）

## Report

## [S1] Problem

多 agent 全库审查发现可利用越权、OW 中继状态机竞态/串台、事件循环阻塞、资源泄漏与行为缺陷。需在不改密钥文件、不动供应商轮换的前提下，一次性修完高/中/低危代码问题。

## [S2] Design

### 安全与权限
- 移除 `TEST_PRIVILEGED_GROUPS` 的整群 owner 提权：默认空集；仅当环境变量 `QQBOT_TEST_PRIVILEGED_GROUPS` 显式配置且非空时，且同时匹配 `QQBOT_TEST_OWNER_UIDS`（可选 QQ 白名单）才放行。默认生产路径 `is_owner()` 与 help 的 `TEST_GROUP_IDS` 不再包含 864213945。
- 不修改任何含密钥的 config/bak 文件（用户明确要求只提醒）。

### OW 中继状态机（owstats）
- **派发互斥**：`_dispatch_next` 使用 `asyncio.Lock`；pop 队列后立刻写入 `_task_current` 再 `await` 发送；发送失败恢复 `_task_current=None` 并插回队首。
- **迟到消息隔离**：任务超时/领取覆盖/手动重置时记录 `discarded_until` 时间窗（默认 45s）与来源 seq；窗口内来自 RELAY_BOT 的非领取消息直接丢弃并打日志，避免写入下一任务 `first_segs`。
- **冷却接线**：`.战报/.段位/.强度/.总结` 入队前调用 `_check_cooldown`；冷却中提示剩余秒数并不入队。
- **future 结算**：`ow_reset`、冻结上限淘汰、领取超时统一 `set_result((None, False))`（若未 done）。
- **flush 任务引用**：模块级 `set` 持有 `_flush_first_after_wait` 的 asyncio.Task，done 回调移除。
- **领取竞态**：发出领取 @ 时若存在在途任务，先 requeue 并进入 `discarded_until` 窗口；领取期间 `_dispatch_next` 空转（已有）。
- **auto_chat 不吃中继消息**：relay 命中中继群查询机器人消息时 `matcher.block = True`；auto_chat 对 `RELAY_GROUP_ID` 直接 return。

### 公共层（common）
- `FONTS`：Noto 候选链 `custom/noto` → `opentype/noto` → 首个存在的 bold/regular。
- `get_http_client`：若池内 client `is_closed`，先 pop 再注册新实例。
- 渲染：独立 `ThreadPoolExecutor`（max_workers=2）执行 weasyprint/PIL 阻塞段，总超时后取消并允许新任务，不占默认 executor。
- 昵称失败：NapCat 失败不写入 300s 负缓存（或 TTL 缩到 30s）。

### 业务插件
- **chat_stats**：流式统计路径的 `jieba` 分词改为分批 `asyncio.to_thread`。
- **on_duty**：状态 load/save 走 `asyncio.to_thread`；规则冷加载缓存后不在热路径同步扫盘（已有缓存则保持）。
- **holiday_countdown**：改为与 news 一致——任一成功即写 `last_push_date`，失败群记 pending 清单待补发，catchup 不全量重推。
- **what_to_eat**：`_last_reply` 加上限（如 2000）+ FIFO 淘汰。
- **btd6 nkapi**：`json.dumps` 估体积挪到 `to_thread`；`_stitch_pngs` 用 try/finally close Image。
- **catchup**：cmd_stats / daily_words 启动时若今日未跑且已过触发点，补跑一次（对齐 news 模式）。
- **error_notify**：`datetime.now(ZoneInfo("Asia/Shanghai"))`。

### 明确不做（Out of scope 见下）
- 供应商密钥轮换、删除 bak 密钥文件
- NapCat 改用户、sshd_config
- 未在审查中列出的新功能

## [S3] Out of Scope

- 修改 `plugins/*/config.json*`、`db.json` 等含密钥文件内容
- 系统级：NapCat 以 root 运行、SSH PermitRootLogin、PostgreSQL 密码轮换、ts3 公网暴露
- 文档与 DEPLOY.md 的 HTTP_PROXY 对齐（仅代码注释/低优先级，可不做则记 Report）
- 改变 OW 对方协议或增加 message_id 关联（仅用时间窗隔离）

## Tasks

- [ ] T1: 移除测试群整群 owner 提权 — acceptance: 生产路径下 864213945 群成员 is_owner 为 False；有测试 (covers: S2)
- [ ] T2: common 字体候选路径 — acceptance: opentype 路径下 noto 可加载；缺省不抛未捕获异常 (covers: S2)
- [ ] T3: get_http_client 关闭实例替换 — acceptance: is_closed 时返回新 client 且不泄漏 orphan (covers: S2)
- [ ] T4: 渲染独立线程池 — acceptance: 超时不阻塞默认 executor；RENDER_SEM 仍限流 (covers: S2)
- [ ] T5: 昵称负缓存策略 — acceptance: 获取失败不长期缓存 user_id 字符串 (covers: S2)
- [ ] T6: owstats 派发锁 + 先占槽再发送 — acceptance: 并发 _dispatch_next 仅一条 in-flight；失败可恢复 (covers: S2)
- [ ] T7: owstats 迟到消息隔离窗 — acceptance: 超时后的 text+image 不完成新任务；有回归测试 (covers: S2; depends: T6)
- [ ] T8: owstats 冷却接线 — acceptance: 10s 内重复查询被拒绝并提示 (covers: S2)
- [ ] T9: ow_reset/冻结淘汰 future 结算 — acceptance: dropped future 为 done 且 result (None, False) (covers: S2)
- [ ] T10: flush task 强引用 — acceptance: 模块持有 set，done 后移除 (covers: S2)
- [ ] T11: 领取 @ 隔离在途迟到消息 — acceptance: 领取窗口内非领取回复不污染冻结交付 (covers: S2; depends: T7)
- [ ] T12: auto_chat 排除中继群 + relay block — acceptance: 中继群查询机器人消息不再触发 auto_chat (covers: S2)
- [ ] T13: chat_stats jieba 下线程 — acceptance: 统计路径无同步 jieba.cut 在事件循环（可测 to_thread 调用或拆纯函数） (covers: S2)
- [ ] T14: on_duty 状态 IO 下线程 — acceptance: create/join/leave 读写不直接 sync fsync (covers: S2)
- [ ] T15: holiday 部分失败不重复全推 — acceptance: 任一群成功写 last_push_date；失败群可单独补 (covers: S2)
- [ ] T16: what_to_eat _last_reply 上限 — acceptance: 超过上限淘汰最旧 (covers: S2)
- [ ] T17: btd6 nkapi dumps 下线程 + stitch close — acceptance: 大 body 不在事件循环 dumps；Image 正确 close (covers: S2)
- [ ] T18: cmd_stats/daily_words 启动 catchup — acceptance: 当日过点且未跑则补一次 (covers: S2)
- [ ] T19: error_notify 时区 — acceptance: 使用 Asia/Shanghai (covers: S2)
- [ ] T20: 全量测试与 ruff — acceptance: pytest 全绿、ruff 通过 (covers: S2; depends: T1-T19)
