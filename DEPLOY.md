# qq-bot 部署说明

从零重建生产环境所需的全部步骤（以 Ubuntu 24.04 为例）。
本文档与 2026-08-27 安全加固及 2026-08-29 内存保护调整后的**实际生产配置**对齐。

## 1. 系统依赖

```bash
apt update
apt install -y python3.12 python3.12-venv acl \
    libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf2.0-0 \  # weasyprint
    poppler-utils \                                          # pdftoppm（PDF→PNG）
    fonts-wqy-microhei \                                     # btd6 卡片用 glyf 中文字体（子集化比 CFF 快数倍）
    postgresql                                               # 群聊统计（可选）
```

自定义字体（common.py 的 FONTS 引用，缺失会导致卡片渲染失败回退纯文本）：

```bash
mkdir -p /usr/share/fonts/custom
# ZCOOLKuaiLe-Regular.ttf → /usr/share/fonts/custom/
# NotoSansCJK-Bold.ttc / NotoSansCJK-Regular.ttc → /usr/share/fonts/opentype/noto/
fc-cache -f
```

## 2. 专用用户（不要用 root 跑 bot）

```bash
useradd -r -m -d /home/qqbot -s /usr/sbin/nologin qqbot
```

## 3. 代码与虚拟环境

```bash
cd /opt && git clone <repo> bot && cd bot
python3.12 -m venv venv
venv/bin/pip install -r requirements.txt
```

`.env` 必填 `QQBOT_OWNER=主人QQ号` 与 `ONEBOT_ACCESS_TOKEN`（与 NapCat 侧一致）。
**权限必须收紧**（生产标准：660 root:root + qqbot 用户 ACL）：

```bash
cp .env.example .env && vim .env
chown root:root .env && chmod 660 .env
setfacl -m u:qqbot:r .env
```

## 4. 数据库（仅 chat_stats 需要）

```bash
sudo -u postgres createuser bot
sudo -u postgres createdb qqbot -O bot
sudo -u postgres psql -c "ALTER USER bot PASSWORD '...'"
echo '{"dsn": "postgresql://bot:...@127.0.0.1:5432/qqbot"}' > plugins/chat_stats/db.json
```

表结构由插件首次启动自动创建，无需手工建表。

## 5. 按需启用插件

各插件运行时按需创建配置文件（不存在则该功能不启用）：

| 插件 | 配置文件 | 示例 |
|------|----------|------|
| `chat_stats` | `plugins/chat_stats/db.json` | `{"dsn": "postgresql://user:pass@127.0.0.1:5432/qqbot"}` |
| `auto_chat` | `plugins/auto_chat/config.json` | `{"api_key": "sk-xxx"}` |
| `news` | `plugins/news/ai_config.json` | `{"api_key": "..."}`（AI 晨报，可选） |

**含密钥的配置文件统一按 `.env` 的标准处理**（660 root:root + `setfacl -m u:qqbot:rw`），
均已被 .gitignore 排除，禁止提交进 git。

## 6. NapCatQQ（OneBot V11 实现）

**方向**：NoneBot（bot.py）监听 `127.0.0.1:8080`（反向 WebSocket 服务端），
NapCat 作为**客户端**连接 `ws://127.0.0.1:8080/onebot/v11/ws`。排障时先看哪一侧没连上。
在 NapCat 侧配置 access token 并写入 `.env` 的 `ONEBOT_ACCESS_TOKEN`。

## 7. 代理（访问外网 API 需要）

```bash
systemctl start clash   # mihomo，HTTP 代理端口 7890
```

systemd unit 中为 bot 进程显式注入代理（mihomo 按规则分流，国内 API 直连）：

```ini
Environment=HTTP_PROXY=http://127.0.0.1:7890
Environment=HTTPS_PROXY=http://127.0.0.1:7890
```

## 8. systemd 服务（生产配置）

基础单元保持简洁，所有调参/加固通过 **drop-in**（`/etc/systemd/system/<unit>.d/*.conf`）覆盖。

### 8.1 qqbot.service（NoneBot2，专用用户 + 沙箱）

```ini
# /etc/systemd/system/qqbot.service
[Unit]
Description=NoneBot2 QQ Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/bot
ExecStart=/opt/bot/venv/bin/python /opt/bot/bot.py
Restart=always
RestartSec=5
Environment=HTTP_PROXY=http://127.0.0.1:7890
Environment=HTTPS_PROXY=http://127.0.0.1:7890

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/qqbot.service.d/security.conf
# 2026-08-27 安全加固：专用用户 + 进程沙箱（覆盖基础单元的 User=root）
[Service]
User=qqbot
Group=qqbot
NoNewPrivileges=true
ProtectSystem=full
PrivateTmp=true
MemoryHigh=500M
MemoryMax=700M
```

```ini
# /etc/systemd/system/qqbot.service.d/oom-protect.conf
# OOMScoreAdjust=200 为有意设置：发生系统级 OOM 时优先牺牲 qqbot（Restart=always，数秒即恢复），
# 以保护更难恢复的 napcat（重启后需重新登录）。请勿按"文件名保护 qqbot"的直觉改为负值。
[Service]
OOMScoreAdjust=200
```

### 8.2 napcat.service（NapCatQQ 协议端）

```ini
# /etc/systemd/system/napcat.service
[Unit]
Description=NapCatQQ Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
ExecStart=/usr/bin/xvfb-run -a /root/Napcat/opt/QQ/qq --no-sandbox
Restart=always
RestartSec=10
KillMode=process

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/napcat.service.d/20-resource-limits.conf
[Service]
KillMode=control-group
MemoryAccounting=yes
MemoryHigh=550M
MemoryMax=750M
MemorySwapMax=128M
OOMPolicy=stop
TimeoutStopSec=30s
```

> 注意：napcat 目前仍以 root 运行（Electron/xvfb 依赖复杂，历史上两次审计确认暂时保留），
> 这是全系统已知的最大攻击面，中期应迁移出 /root 并降权 + 沙箱。

### 8.3 内存守护（memory-monitor + napcat-guard）

两个 oneshot 服务由 timer 驱动，脚本放在 `/usr/local/sbin/`（700 root:root）：

- **memory-monitor**（每分钟）：可用内存 < 200MB 告警并列 top5 进程；< 100MB 重启 napcat；
  每分钟记录 napcat 内存趋势（脚本结尾 `exit 0`，保证 oneshot 不出现假失败）。
- **napcat-guard**（每 2 分钟）：仅在 napcat **is-failed** 时拉起（崩溃重启由 systemd Restart=always 接管，
  人为 stop 不会被拉回）；napcat 内存 ≥ 650M（681574400 字节）时优雅重启。

```ini
# /etc/systemd/system/memory-monitor.service
[Unit]
Description=Memory Monitor
After=local-fs.target
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/memory-monitor

# /etc/systemd/system/memory-monitor.timer
[Unit]
Description=Run memory monitor every minute
[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
AccuracySec=5s
[Install]
WantedBy=timers.target

# /etc/systemd/system/napcat-guard.service
[Unit]
Description=NapCat resource guard
After=napcat.service
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/napcat-guard

# /etc/systemd/system/napcat-guard.timer
[Unit]
Description=Run NapCat resource guard periodically
[Timer]
OnBootSec=5min
OnUnitActiveSec=2min
AccuracySec=15s
[Install]
WantedBy=timers.target
```

> `Persistent=true` 只对 OnCalendar 生效，单调定时器（OnBootSec/OnUnitActiveSec）不要写。

**内存保护分层（1.6G 总内存）**：

| 层级 | qqbot | napcat | 行为 |
|------|-------|--------|------|
| MemoryHigh（节流） | 500M | 550M | 超过后限制分配速度 |
| guard 阈值（优雅重启） | — | 650M | 每 2 分钟检查，优雅重启 |
| MemoryMax（内核强杀） | 700M | 750M | 兜底，前两层都失效才触发 |

**人工维护 napcat 前先上维护锁**（否则 guard 只在 is-failed 时才干预，stop 可正常保持停机）：

```bash
touch /run/napcat-maintenance   # 维护前
rm /run/napcat-maintenance      # 维护后恢复守护
```

### 8.4 qqbot 每日定时重启（释放内存碎片）

```ini
# /etc/systemd/system/qqbot-restart.service
[Unit]
Description=Daily restart of qqbot to free memory
[Service]
Type=oneshot
ExecStart=/usr/bin/systemctl restart qqbot

# /etc/systemd/system/qqbot-restart.timer
[Unit]
Description=Daily qqbot restart timer
[Timer]
OnCalendar=*-*-* 05:00:00
Persistent=true
[Install]
WantedBy=timers.target
```

### 8.5 启用

```bash
systemctl daemon-reload
systemctl enable --now memory-monitor.timer napcat-guard.timer qqbot-restart.timer
systemctl enable --now qqbot
journalctl -u qqbot -f   # 查看日志
```

## 9. 已知运维行为（不要误判为故障）

- **每天 05:00** qqbot 定时重启（qqbot-restart.timer），1 秒内恢复，属日常维护。
- **系统安全更新日**（apt-daily-upgrade，通常清晨）：unattended-upgrades 升级 libc 等库后
  needrestart 会自动重启使用旧库的服务（可能连带 napcat / postgresql / ssh）。
  napcat 约 1 分钟内快速登录恢复，不是崩溃。
- napcat 内存基线约 540M，接近 MemoryHigh=550M 属正常（QQ/Electron 本身体量大）。

## 10. 日常维护

```bash
cd /opt/bot && git pull                     # 更新代码
venv/bin/pip install -r requirements.txt    # 依赖变更时
venv/bin/python -m pytest -q                # 跑测试（无需数据库）
systemctl restart qqbot                     # 重启 bot（napcat 会自动重连）
```

- 服务：`systemctl start qqbot`；代理：`systemctl start clash`
- 入口：bot.py；插件目录：plugins/（plugins-disabled/ 不会被加载）
- **error_notify 插件必须加载成功**，否则 bot.py 会拒绝启动（告警链路缺失时宁可不跑）

## 11. 安全注意事项

- `.env` 与所有含密钥的插件配置：660 root:root + qqbot ACL，禁止入 git；
  一旦发生过宽松权限暴露（如 644），**必须轮换密钥**而不只是收紧权限。
- AUDIT / REVIEW / SECURITY-FIX 等审计文档含服务器 IP 与运维细节，已被 .gitignore 排除，
  不要提交到远端仓库。
- SSH 仅密钥登录（服务器 qqbot.pem），保持 `PermitRootLogin`/`PasswordAuthentication` 收紧配置。
