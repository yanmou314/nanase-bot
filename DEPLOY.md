# qq-bot 部署说明

从零重建生产环境所需的全部步骤（以 Ubuntu 24.04 为例）。

## 1. 系统依赖

```bash
apt update
apt install -y python3.12 python3.12-venv \
    libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf2.0-0 \  # weasyprint
    poppler-utils \                                          # pdftoppm（PDF→PNG）
    postgresql                                               # 群聊统计（可选）
```

自定义字体（common.py 的 FONTS 引用，缺失会导致卡片渲染失败回退纯文本）：

```bash
mkdir -p /usr/share/fonts/custom
# ZCOOLKuaiLe-Regular.ttf → /usr/share/fonts/custom/
# NotoSansCJK-Bold.ttc / NotoSansCJK-Regular.ttc → /usr/share/fonts/opentype/noto/
fc-cache -f
```

## 2. 代码与虚拟环境

```bash
cd /opt && git clone <repo> bot && cd bot
python3.12 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env && vim .env   # 必填 QQBOT_OWNER=主人QQ号
```

## 3. 数据库（仅 chat_stats 需要）

```bash
sudo -u postgres createuser bot
sudo -u postgres createdb qqbot -O bot
sudo -u postgres psql -c "ALTER USER bot PASSWORD '...'"
echo '{"dsn": "postgresql://bot:...@127.0.0.1:5432/qqbot"}' > plugins/chat_stats/db.json
```

表结构由插件首次启动自动创建，无需手工建表。

## 4. 按需启用插件

各插件运行时按需创建配置文件（不存在则该功能不启用）：

| 插件 | 配置文件 | 示例 |
|------|----------|------|
| `chat_stats` | `plugins/chat_stats/db.json` | `{"dsn": "postgresql://user:pass@127.0.0.1:5432/qqbot"}` |
| `auto_chat` | `plugins/auto_chat/config.json` | `{"api_key": "sk-xxx"}` |
| `news` | `plugins/news/ai_config.json` | `{"api_key": "..."}`（AI 晨报，可选） |

敏感配置建议 `chmod 600`，且均已被 .gitignore 排除。

## 5. NapCatQQ（OneBot V11 实现）

NapCatQQ 监听 `127.0.0.1:8080`（反向 WebSocket），bot.py 默认连接该地址。
在 NapCat 侧配置 access token 并写入 `.env` 的 `ONEBOT_ACCESS_TOKEN`。

## 6. 代理（访问外网 API 需要）

```bash
systemctl start clash   # mihomo，HTTP 代理端口 7890
```

systemd unit 中为 bot 进程显式注入代理（mihomo 按规则分流，国内 API 直连）：

```ini
Environment=HTTP_PROXY=http://127.0.0.1:7890
Environment=HTTPS_PROXY=http://127.0.0.1:7890
```

## 7. systemd 服务

```ini
# /etc/systemd/system/qqbot.service
[Unit]
Description=NoneBot2 QQ Bot
After=network-online.target

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

```bash
systemctl daemon-reload
systemctl enable --now qqbot
journalctl -u qqbot -f   # 查看日志
```

## 8. 日常维护

```bash
cd /opt/bot && git pull                     # 更新代码
venv/bin/pip install -r requirements.txt    # 依赖变更时
venv/bin/python -m pytest -q                # 跑测试（无需数据库）
systemctl restart qqbot
```

- 服务：`systemctl start qqbot`；代理：`systemctl start clash`
- 入口：bot.py；插件目录：plugins/（plugins-disabled/ 不会被加载）
