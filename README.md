# QQ Bot（NoneBot2 + NapCat + Overstats）

基于 **NoneBot2** 的 QQ 群机器人，支持娱乐、守望先锋国服战绩、群聊统计、每日新闻/词云推送、AI 聊天（西野七濑人设）等功能。

## 架构

```
┌─────────┐  OneBot V11 (WS)  ┌──────────┐
│  NapCat │ ◄───────────────► │ NoneBot2 │  群消息处理 / 插件系统
│ (QQ协议)│                    └────┬─────┘
└─────────┘                         │
                          ┌─────────┴──────────┐
                          │   PostgreSQL        │  群聊统计存储
                          └─────────────────────┘
                          ┌─────────────────────┐
                          │  Overstats 服务      │  守望先锋国服数据
                          │  (18080 端口)        │  (网易大神上游)
                          └─────────────────────┘
                          ┌─────────────────────┐
                          │  OpenCode Go API     │  AI 聊天 (DeepSeek V4)
                          └─────────────────────┘
```

## 功能列表

| 插件 | 命令 | 说明 |
|------|------|------|
| **fun** | `.rp` | 抽签 + 今日运势（按 QQ+日期 每日固定） |
| **owstats** | `.绑定` `.战报` `.段位` `.强度` `.总结` `.我的ID` `.解绑` | 守望先锋国服战绩查询（串行队列、@回复、显示用时） |
| **chat_stats** | `.龙王` `.词云` `.词云开启/关闭/状态` | 群聊统计（PostgreSQL 存储）、词云图（中心螺旋算法）、每日 8:00 推送 |
| **news** | `.新闻开启/关闭/测试/状态` | 每日 8:00 推送前一天新闻总结（Pillow 渲染图片） |
| **auto_chat** | @机器人聊天、戳一戳 | DeepSeek V4 Flash（OpenCode Go 订阅）、西野七濑人设、上下文记忆 |
| **repeater** | 自动 | 复读机：同一句话/图连续 3 次自动复读 |
| **request_manager** | 自动 + 私聊指令 | 好友/加群申请通知与审批（引用消息同意/拒绝）、私聊转发 |
| **group_leave** | 自动 | 退群/被踢自动提示 |
| **server_status** | `.服务器` | 服务器状态卡片（CPU/内存/磁盘/服务） |
| **help** | `.帮助` | 指令菜单（管理功能仅群主可见） |

## 目录结构

```
/opt/bot/
├── bot.py               # NoneBot 入口
├── common.py            # 共享工具（OWNER 常量、图片/缓存工具）
├── pyproject.toml       # NoneBot 配置（plugin_dirs）
├── .env.example         # 环境变量示例
└── plugins/
    ├── auto_chat/       # AI 聊天
    ├── chat_stats/      # 群聊统计 + 词云
    ├── fun/             # 运势
    ├── group_leave/     # 退群提示
    ├── help/            # 帮助菜单
    ├── news/            # 每日新闻
    ├── owstats/         # 守望先锋战绩
    ├── repeater/        # 复读机
    ├── request_manager/ # 申请审批
    └── server_status/   # 服务器状态
```

## 部署

### 环境要求

- Ubuntu 20.04+（本部署基于 Ubuntu 24.04）
- Python 3.10+
- Node.js 20+（NapCat Shell 需要）
- PostgreSQL 14+

### 步骤

1. **安装依赖**：
   ```bash
   python3 -m venv venv
   ./venv/bin/pip install -r requirements.txt
   ```

2. **安装 NapCat**（QQ 协议端）：
   ```bash
   curl -o napcat.sh https://nclatest.znin.net/NapNeko/NapCat-Installer/main/script/install.sh && bash napcat.sh --docker n --cli n --proxy 1
   ```
   启动后通过 WebUI（6099 端口）扫码登录 QQ 小号。

3. **配置 Overstats**（守望先锋国服数据服务）：
   - 部署 [Overstats](https://github.com/AddOneSecondL/Overstats) 到本地 18080 端口
   - 需要网易大神账号的 role_id + token（见项目 Faststart.md）

4. **配置 AI 聊天**（可选）：
   - `plugins/auto_chat/config.json` 填入 OpenCode Go API Key：
     ```json
     {"api_key": "sk-xxx"}
     ```

5. **配置数据库**：
   ```sql
   CREATE USER qqbot WITH PASSWORD 'your_password';
   CREATE DATABASE qqbot OWNER qqbot;
   ```
   写入 `plugins/chat_stats/db.json`：
   ```json
   {"dsn": "postgresql://qqbot:your_password@<PRIVATE_IP>:5432/qqbot"}
   ```

6. **启动**：
   ```bash
   cd /opt/bot
   ./venv/bin/python bot.py
   ```

## systemd 服务

```ini
# /etc/systemd/system/qqbot.service
[Unit]
Description=NoneBot2 QQ Bot
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/bot
ExecStart=/opt/bot/venv/bin/python /opt/bot/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now qqbot
```

## 数据存储

- **群聊统计**：PostgreSQL（`messages` 表，30 天自动清理）
- **绑定关系**：`plugins/owstats/bindings.json`
- **推送状态**：`plugins/news/state.json`、`plugins/chat_stats/words_state.json`

## 安全说明

- 所有密钥（AI API Key、数据库密码）存放在插件目录的 `config.json`/`db.json`，已在 `.gitignore` 中排除，**切勿提交到仓库**
- 修改群主 QQ：`common.py` 中的 `OWNER` 常量
- 指令前缀：`.env` 中的 `COMMAND_START`（默认 `.`）

## 致谢

- [NoneBot2](https://nonebot.dev/) - 聊天机器人框架
- [NapCatQQ](https://github.com/NapNeko/NapCatQQ) - QQ 协议端
- [Overstats](https://github.com/AddOneSecondL/Overstats) - 守望先锋国服数据服务
- [OpenCode](https://opencode.ai/) - AI 模型网关
