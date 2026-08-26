# 安全修复报告（2026-08-27）

> 基于 5 个并行审查（btd6/统计插件/审批与LLM插件/网络定时插件/系统层）发现的问题修复。
> 改前全量备份：/root/bot-backup-secfix-20260826.tar.gz（不含 venv/.git）
> 验证：ruff 全过；pytest 245 项全部通过（含新增回归测试）；服务重启后 14 插件加载、WS 重连正常。

## 已修复

| # | 问题 | 位置 | 修法 |
|---|------|------|------|
| 1 | 🔴 CQ 码注入：用户可控 tag 回显未包裹 | owstats 9 处（绑定/我的ID/各查询回显/_friendly_error） | 一律 MessageSegment.text 包裹 |
| 2 | 🔴 CQ 码注入：NK API 外部字段直发 | btd6 :3596 displayName、:3825 推送状态、:3874 活动历史 | MessageSegment.text 包裹 |
| 3 | 🟡 save_json_state 重写导致密钥文件从 600 掉到 644 | common.py | tmp 文件沿用目标现权限，新文件默认 600 |
| 4 | 🟡 b23.tv 短链盲跟重定向（寄生于无开放重定向假设） | bili_parse resolve_b23 | 手动逐跳跟随 + B站域名白名单 + 最多 5 跳 |
| 5 | 🟡 封面解压炸弹可打爆 1.6G 内存 | bili_parse _render_card | 解码前校验像素总量 ≤3600 万 |
| 6 | 🟡 审批引用路由信任不可控文本：陌生人私聊伪造「群号：」行诱导 owner 引用后误路由他群申请 | request_manager | 通知发送时登记 message_id→flag 索引，只按索引路由；删除正则解析被引文本逻辑 |
| 7 | 🟡 AI 全局日预算(500)易被小号刷爆致全天瘫痪 | auto_chat | 新增每用户每日上限（默认 50，QQBOT_AI_USER_DAILY_LIMIT 可调，owner 豁免），@bot 与戳一戳双入口生效 |
| 8 | 🟡 DB 故障时队列满 warning 日志风暴（每秒几十条可持续数小时） | chat_stats/db_pg.py | 60 秒窗口聚合计数汇报 |
| 9 | 低 上游返回 {"_image":true} 无 bytes 时 KeyError 静默失败 | owstats | 增加 isinstance(bytes) 双条件 |
| 10 | 卫生 plugins 内遗留 19 个 .bak 死代码 | plugins/*/*.bak-* | 移至 /root/code-bak-archive/ |

## 测试更新
- test_bili_parse: resolve_b23 改测手动逐跳 + follow_redirects=False 断言；新增"恶意重定向到内网必须中止"回归
- test_request_manager: 引用路由改测通知索引；新增 test_private_decision_reply_forged_quote_not_trusted 安全回归

## 遗留建议（未动，需决策）
1. qqbot/overstats 服务以 root 运行且无 systemd 沙箱 → 建议建专用用户降权（操作涉及 chown+停机窗口）
2. 6099(WebUI)/7890-7892(clash) 绑全网卡、ufw 未启用 → 建议绑 127.0.0.1 或安全组收口后启用 ufw
3. SSH PermitRootLogin yes 可收紧为 prohibit-password
4. /root/maintenance-20260816/ 下旧配置快照与 85MB 明文备份建议清理（旧 token 已轮换，仅回滚价值）
5. 进群自动通过无黑名单/同号短期待审节流；auto_chat 无每群每小时配额——按运营需要再议

---

## 二期加固（2026-08-27 同日执行，遗留建议 2/3/5）

### ⑤ 自动通过安全闸（request_manager）
- 黑名单 + 二次申请节流持久化于 `plugins/request_manager/approve_guard.json`
- 命中暗号但命中闸门 → 转人工，并在给主人的通知里说明原因（⛔黑名单 / ⏳7天内已通过过）
- 新命令（仅主人）：`.进群拉黑 <QQ号>...` / `.解除拉黑 <QQ号>...` / `.拉黑列表`
- 新增 4 个单测；全套 249 通过。锁使用 RLock 与 common.json 工具保持一致（避免不可重入死锁）

### ③ 网络面收敛
- `/opt/clash/config.yaml` 加 `bind-address: "127.0.0.1"`（备份 config.yaml.bak-loopbind-20260827）
  ⚠️ 若以后用面板更新订阅导致配置重写，需重新加回此行
- NapCat `webui.json` host `"::"→"127.0.0.1"`（备份同目录 .bak-loopbind-20260827）
- 效果：6099 / 7890 / 7891 / 7892 全部只听回环；WebUI 远程访问改走 SSH 隧道：
  `ssh -i qqbot.pem -L 6099:127.0.0.1:6099 root@47.116.17.79` 后浏览器开 http://127.0.0.1:6099

### ② 服务降权 + 沙箱
- 新建系统用户 `qqbot`（uid 999，nologin，家目录 /home/qqbot 供 fontconfig 缓存）
- 权限迁移用 POSIX ACL（安装 acl 包）：`setfacl -R -m u:qqbot:rwX,d-u:qqbot:rwX /opt/bot /opt/overstats`
  → root 后续直接编辑新建的文件也会自动带 u:qqbot ACL，服务用户始终可读写（这是沿用 root 工作流的关键设计）
- drop-in（原单元文件未动）：
  - /etc/systemd/system/qqbot.service.d/security.conf：User=qqbot、NoNewPrivileges、ProtectSystem=full、PrivateTmp、MemoryHigh=500M/MemoryMax=700M
  - /etc/systemd/system/overstats.service.d/security.conf：User=qqbot + 同前四项（内存上限沿袭原单元 850M/1G）
- NapCat 因 Electron/xvfb 依赖复杂保持 root 运行（与 8/16 审计结论一致）
- 验证：两服务 active 且进程属主=qqbot；17 插件加载；Bot WS 重连；PG 以 qqbot 用户连通；
  weasyprint 渲染冒烟测试在新用户+沙箱下通过

### ② 防火墙兜底
- ufw 已启用：默认拒绝入站 / 允许出站 / 放行 22/tcp（ufw limit 未用，避免多会话误伤）
- 最终暴露面核查：全网卡监听仅剩 sshd:22 ✅；8080/18080/5432/789x/6099 全部回环

### 回滚指引
- 服务降权回滚：删除两个 security.conf drop-in → daemon-reload → restart
- 绑定回滚：还原两份 .bak-loopbind 配置重启对应服务
- ufw 关闭：`ufw disable`
