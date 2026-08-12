<div align="center">

# 🤖 QQ Bot

基于 [NoneBot2](https://nonebot.dev/) 的 QQ 群聊机器人，通过 OneBot V11 协议对接 [NapCatQQ](https://github.com/NapNeko/NapCatQQ) 等实现。

**抽签运势 · 守望先锋战绩 · 群聊统计 · 每日新闻 · 节假日倒计时 · AI 聊天 · 申请管理**

</div>

---

## ✨ 功能特性

| 类别 | 功能 | 说明 |
|------|------|------|
| 🎲 娱乐 | 抽签 / 今日运势 | 每日每用户固定运势，支持多别名 |
| 🎮 守望先锋 | 战报 / 段位 / 强度 / 总结 | 国服数据，绑定一次即可查询 |
| 📊 群聊统计 | 龙王 / 词云 | 数据库统计 + 图片卡片渲染 |
| 📰 新闻推送 | 每日新闻速览 | 60秒读懂世界主源 + 百度热搜回退 |
| ⏳ 倒计时 | 周末 / 节假日 | 图片卡片 + 每日 17:00 群推送 |
| 💬 AI 聊天 | @机器人 对话 | 角色扮演 + 多用户记忆 + 并发控制 |
| 🛡 申请管理 | 好友 / 进群申请 | 关键字自动通过 + 私聊审批 |
| 🔁 自动复读 | 群消息复读 | 去重 + 冷却 + 状态持久化 |
| 📈 指令统计 | 每日使用报告 | 定时生成统计图推送 |
| 🚪 退群通知 | 自动播报 | 随机文案 |
| ❓ 帮助菜单 | 命令总览 | 按权限区分公开 / 管理菜单 |

## 🏗 架构

```
┌──────────┐   OneBot V11    ┌────────────┐
│   QQ 群   │ ◄────────────► │  NapCatQQ  │
└──────────┘   WebSocket     └─────┬──────┘
                                   │ 127.0.0.1:8080
                          ┌────────▼─────────┐
                          │  NoneBot2 机器人  │
                          │      bot.py      │
                          └────────┬─────────┘
                 ┌─────────────────┼──────────────────┐
                 │                 │                  │
        ┌────────▼──────┐  ┌───────▼───────┐  ┌───────▼────────┐
        │   PostgreSQL   │  │  AI API       │  │  OW 数据服务    │
        │  (聊天记录)     │  │  (DeepSeek等) │  │  (Overstats)   │
        └───────────────┘  └───────────────┘  └────────────────┘
```

## 🚀 快速开始

### 环境要求

- Python ≥ 3.9
- OneBot V11 实现（如 [NapCatQQ](https://github.com/NapNeko/NapCatQQ)）
- PostgreSQL（可选，仅群聊统计需要）

### 1. 安装

```bash
git clone https://github.com/yanmou314/qq-bot.git
cd qq-bot
pip install -r requirements.txt
```

> 📦 weasyprint 依赖系统库，Ubuntu 需安装：`apt install libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf2.0-0 poppler-utils`

### 2. 配置

```bash
cp .env.example .env
```

编辑 `.env`：

```ini
# 机器人主人的 QQ 号（拥有全部管理权限）
QQBOT_OWNER=你的QQ号
```

### 3. 按需启用插件

各插件运行时按需创建配置文件（不存在则该功能不启用）：

| 插件 | 配置文件 | 示例 |
|------|----------|------|
| `chat_stats` | `plugins/chat_stats/db.json` | `{"dsn": "postgresql://user:pass@127.0.0.1:5432/qqbot"}` |
| `auto_chat` | `plugins/auto_chat/config.json` | `{"api_key": "sk-xxx"}` |

### 4. 运行

```bash
python bot.py
```

启动后连接 OneBot V11 WebSocket（默认 `127.0.0.1:8080`），在群里发送 `.帮助` 查看完整菜单。

## 🛠 插件命令一览

### 公开命令（所有群成员）

| 命令 | 说明 |
|------|------|
| `.rp` / `.抽签` / `.运势` | 抽签 + 今日运势 |
| `.绑定 名字#数字` | 绑定守望先锋战网 ID |
| `.战报 [ID]` / `.段位 [ID]` / `.强度 [ID]` | 战绩 / 段位 / 强度图 |
| `.总结 [ID] [今日\|昨日\|本周]` | 上分总结图 |
| `.我的ID` / `.解绑` | 查看 / 解除绑定 |
| `.倒计时` | 下一个周末和节假日倒计时 |
| `@机器人 + 消息` | 与 AI 角色聊天 |

### 管理命令（仅主人可见，发送 `.帮助` 查看完整列表）

| 类别 | 命令 |
|------|------|
| 📊 统计 | `.龙王` `.词云 [N]` |
| 📰 推送 | `.新闻开启/关闭/测试/状态` `.词云开启/关闭/状态` `.倒计时开启/关闭/状态/测试` |
| 🛡 进群 | `.自动通过 关键字` `.自动通过关闭/查看/数量` |

## 🗂 目录结构

```
bot.py                      入口
common.py                   公共工具（权限检查、图片缓存、资源清理）
pyproject.toml              NoneBot2 插件注册
requirements.txt            依赖清单
.env.example                环境变量模板
plugins/
  ├── auto_chat/            AI 聊天（角色扮演 + 多用户记忆）
  ├── chat_stats/           群聊统计（龙王/词云）+ PostgreSQL 存储层
  ├── cmd_stats/            每日指令使用统计
  ├── fun/                  抽签运势
  ├── group_leave/          退群通知
  ├── help/                 帮助菜单
  ├── holiday_countdown/    节假日倒计时
  ├── news/                 每日新闻推送
  ├── owstats/              守望先锋战绩查询
  ├── repeater/             自动复读
  └── request_manager/      好友/进群申请管理
```

## 🚢 生产部署（systemd）

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

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now qqbot
journalctl -u qqbot -f    # 查看日志
```

## 🛡 安全说明

- 本仓库**仅包含代码**，不含任何运行配置
- `.gitignore` 已排除全部敏感文件：`.env`、`db.json`、`config.json`、`bindings.json`、`state.json` 等（含 QQ 号 / 群号 / 密钥 / 聊天记录）
- 部署时请自行保管好配置文件，不要提交到仓库
- 所有 HTML 渲染已做转义与外部资源拦截（防 XSS / SSRF）

## 📜 技术栈

- [NoneBot2](https://nonebot.dev/) · [OneBot V11](https://onebot.dev/)
- PostgreSQL · psycopg3
- Pillow · weasyprint · APScheduler
- httpx

## 📄 License

本项目仅供学习交流使用，请遵守所在地区的法律法规与平台使用规范。
