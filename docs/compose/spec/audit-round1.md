---
feature: audit-round1
status: delivered
updated: 2026-09-12
branch: fix/audit-round1
commits: f8cd7f09ad75f9fb5badffcff42bb3076a81f905..134c7cb3f161a4c98afdbf477ad2e9364d9d952f
---

# 代码审查修复（Round 1）

## Report

**What was built** — 在 `fix/audit-round1` 上完成多 agent 审查发现的高/中/低危代码修复：取消测试群整群 owner 提权（默认空集，可用环境变量显式启用）；OW 中继增加派发锁、先占槽再发送、45s 迟到消息隔离窗（窗内禁止再派发，避免下一任务回复被误丢）、查询冷却接线、future 结算与 flush 强引用；auto_chat 排除中继群并 block 后续 matcher。common 层修复 Noto 字体路径候选、已关闭 HTTP client 替换、渲染独立线程池（max_workers=2）、昵称失败 30s 负缓存。业务侧：chat_stats jieba 分词与 on_duty 状态 IO 改为 `to_thread`；holiday 部分失败记 `failed_groups` 且不重复全量重推；what_to_eat 链接表上限；btd6 nkapi 体积估算与 CT 拼图 PIL 句柄关闭；cmd_stats/词云启动 catchup；error_notify 使用 Asia/Shanghai；ow_patch 中继群跳过改硬编码常量（不再依赖特权群集合）。未改任何密钥/bak 文件（用户要求只提醒）。

**Verification** — `cd /opt/bot/.worktrees/fix-audit && /opt/bot/venv/bin/python -m pytest -q` → **459 passed**（基线 424）；`ruff check` 触及文件 → pass。独立 reviewer 结论 pass-with-nits，C1（隔离窗误伤下一任务）已修复并补回归测试 `test_dispatch_blocked_during_discard_window`。

**Journey log** —
1. 测试群整群 owner 曾导致 `ow_patch._RELAY_SKIP` 空集 IndexError；改为插件内硬编码 `RELAY_GROUP_ID`。
2. 子 agent 改写 `common.py` 时误将 CRLF 转成 LF，造成整文件 diff；已恢复 CRLF，真实内容变更约 +86/−11。
3. 迟到消息隔离窗与「超时后立即 `_dispatch_next`」组合会丢弃下一任务合法回复；改为窗内禁派发，由下一轮 sweep 续跑。
4. 并行实现必须按文件集切分所有权；跨文件副作用（如 `_RELAY_SKIP` 依赖 `TEST_PRIVILEGED_GROUPS`）需编排者收口。
5. 密钥轮换与 `.bak` 清理明确 Out of Scope，需人工在供应商侧处理。

## [S1] Problem

多 agent 全库审查发现可利用越权、OW 中继状态机竞态/串台、事件循环阻塞、资源泄漏与行为缺陷。

## [S2] Design

### 安全与权限
- `TEST_PRIVILEGED_GROUPS` 默认空；`QQBOT_TEST_PRIVILEGED_GROUPS` / `QQBOT_TEST_OWNER_UIDS` 可选启用；help 不再默认导出 864213945。

### OW 中继
- `_dispatch_next`：`asyncio.Lock` + 先写 `_task_current` 再 await 发送。
- 迟到隔离窗 45s：超时/领取覆盖/重置后打开；窗内丢弃非领取图；**窗内禁止派发**。
- 四命令接 `_check_cooldown`；`_settle_future`；flush Task 强引用；relay `matcher.block`；auto_chat 排除中继群。

### 公共层
- FONTS 候选 custom→opentype；closed client pop；`_RENDER_EXECUTOR(2)`；昵称失败 TTL 30s。

### 业务插件
- jieba/on_duty 下线程；holiday failed_groups；what_to_eat cap 2000；nkapi async size；PIL close；cmd_stats/词云 catchup；error_notify 上海时区；ow_patch `RELAY_GROUP_ID=864213945`。

## [S3] Out of Scope

- 修改含密钥的 config/bak；供应商轮换；NapCat/sshd/PG 密码/TS3 暴露；HTTP_PROXY 与文档对齐。

## Tasks

- [x] T1: 移除测试群整群 owner 提权
- [x] T2: common 字体候选路径
- [x] T3: get_http_client 关闭实例替换
- [x] T4: 渲染独立线程池
- [x] T5: 昵称负缓存策略
- [x] T6: owstats 派发锁 + 先占槽
- [x] T7: 迟到消息隔离窗
- [x] T8: 冷却接线
- [x] T9: future 结算
- [x] T10: flush 强引用
- [x] T11: 领取覆盖隔离
- [x] T12: auto_chat 排除中继群
- [x] T13: chat_stats jieba 下线程
- [x] T14: on_duty IO 下线程
- [x] T15: holiday 部分失败不重复全推
- [x] T16: what_to_eat 上限
- [x] T17: btd6 nkapi/stitch
- [x] T18: cmd_stats catchup
- [x] T19: error_notify 时区
- [x] T20: 全量测试与 ruff（459 passed）
