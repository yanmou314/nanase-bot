<div align="center">

# 🤖 QQ Bot

[![CI](https://github.com/yanmou314/nanase-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/yanmou314/nanase-bot/actions/workflows/ci.yml)

基于 [NoneBot2](https://nonebot.dev/) 的 QQ 群聊机器人，通过 OneBot V11 协议对接 [NapCatQQ](https://github.com/NapNeko/NapCatQQ) 等实现。

**抽签运势 · 守望先锋战绩 · 群聊统计 · 每日晨报 · 节假日倒计时 · AI 聊天 · 申请管理**

</div>

---

## ✨ 功能特性

| 类别 | 功能 | 说明 |
|------|------|------|
| 🎲 娱乐 | 抽签 / 今日运势 | 每日每用户固定运势（同人同日结果不变） |
| 🎮 守望先锋 | 战报 / 段位 / 强度 / 总结 | 国服数据，绑定一次即可查询；查询冷却 + 后台预热 + 排队提示 |
| 📊 群聊统计 | 龙王 / 词云 | PostgreSQL 统计 + 图片卡片渲染，每日 00:02 自动推送昨日词云，每日 03:00 清理过期数据 |
| 📰 每日晨报 | 早安问候 + 昨日新闻 | 每日 07:00 推送：日期 + 一言 + 农历节气 + 昨日新闻（60秒读懂世界，百度热搜回退），支持配置智谱 API Key 生成 AI 问候 |
| ⏳ 倒计时 | 周末 / 节假日 | 图片卡片 + 每日 17:00 群推送，内置 2026 年法定假期与调休数据（按年份写在代码里，跨年需补充） |
| 💬 AI 聊天 | @机器人 对话 / 戳一戳 | 西野七濑人设（[人设表](docs/西野七濑人设表.md)）+ 多用户记忆；群聊携带发言人昵称可分辨多人 |
| 🔀 随机插话 | 群聊随机回复 | 概率可调、每群 60 秒最小间隔，复用 AI 聊天接口 |
| 🍚 吃什么 | 被动推荐 | 群里聊到「吃什么 / 吃啥」随机推荐一种食物（带冷却防刷屏） |
| 🛡 申请管理 | 好友 / 进群申请 | 关键字自动通过（不区分大小写）；人工审批：群里直接回复「同意/拒绝」（无需@）或私聊回复；配置与审批结果均只私发主人 |
| 🚨 报错通知 | 运行异常监控 | 插件处理报错 / 定时任务失败自动私聊通知主人（含插件名、错误、位置，同类错误 10 分钟冷却） |
| 🔁 自动复读 | 群消息复读 | 去重 + 冷却 + 状态持久化（只存消息哈希） |
| 📈 指令统计 | 每日使用报告 | 每日 00:05 定时生成统计图推送 |
| 🚪 进出群播报 | 欢迎 / 退群通知 | 入群欢迎、退群随机文案并播报逗留时长 |
| ❓ 帮助菜单 | 命令总览 | 公开菜单全群可见；主人完整菜单自动私发，不在群内暴露管理命令 |
| 💸 价格对比 | 大模型价格图 | 主流大模型 API 价格实时对比（OpenRouter）*（插件当前已停用，见 `plugins-disabled/`）* |

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
        │  (聊天记录)     │  │ (opencode.ai  │  │  (Overstats)   │
        │               │  │  · 智谱·一言)  │  │                │
        └───────────────┘  └───────────────┘  └────────────────┘
```

## 🚀 快速开始

### 环境要求

- Python ≥ 3.10
- OneBot V11 实现（如 [NapCatQQ](https://github.com/NapNeko/NapCatQQ)）
- PostgreSQL（可选，仅群聊统计需要）

### 1. 安装

```bash
git clone https://github.com/yanmou314/nanase-bot.git
cd nanase-bot
pip install -r requirements.txt
```

> 📦 weasyprint 依赖系统库，Ubuntu 需安装：`apt install libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf2.0-0 poppler-utils`

### 2. 配置

```bash
cp .env.example .env
```

编辑 `.env`（模板已含以下默认值，通常只需改 QQBOT_OWNER）：

```ini
# NoneBot2 驱动配置
DRIVER=~fastapi+~httpx+~websockets
HOST=127.0.0.1        # 反向 WebSocket 监听地址（NapCat 连这里）
PORT=8080
LOG_LEVEL=INFO
COMMAND_START=["."]   # 命令前缀，决定用 .帮助 还是 /帮助

# 机器人主人的 QQ 号（拥有全部管理权限）
QQBOT_OWNER=你的QQ号

# 若 NapCatQQ 侧配置了 access token，需增加：
# ONEBOT_ACCESS_TOKEN=与NapCat一致
```

### 3. 按需启用插件

各插件运行时按需创建配置文件（不存在则该功能不启用）：

| 插件 | 配置文件 | 示例 |
|------|----------|------|
| `chat_stats` | `plugins/chat_stats/db.json` | `{"dsn": "postgresql://user:pass@127.0.0.1:5432/qqbot"}` |
| `auto_chat` | `plugins/auto_chat/config.json` | `{"api_key": "sk-xxx"}`（opencode.ai） |
| `news` | `plugins/news/ai_config.json` | `{"api_key": "..."}`（智谱，也可用 `.新闻key` 命令配置） |

其余插件（词云/晨报/倒计时/插话等）用对应的管理命令在群内开关，状态自动持久化。

### 4. 运行

```bash
python bot.py
```

启动后在 `127.0.0.1:8080` 监听反向 WebSocket，NapCatQQ 连接后在群里发送 `.帮助` 查看完整菜单。

## 🛠 插件命令一览

命令前缀由 `.env` 的 `COMMAND_START` 决定（默认模板为 `.`，下文以 `.` 为例）。

### 公开命令（所有群成员）

| 命令 | 说明 |
|------|------|
| `.rp` / `.抽签` / `.运势` / `.求签` / `.今日运势` / `.今日运气` | 抽签 + 今日运势 |
| `.绑定 名字#数字`（`.bind`） | 绑定守望先锋战网 ID |
| `.战报 [ID]`（`.战绩图` / `.report`） | 最近对局战绩图 |
| `.段位 [ID]`（`.段位历史` / `.rank`） | 段位历史走势图 |
| `.强度 [ID]`（`.强度分析` / `.strength`） | 英雄强度分析图 |
| `.总结 [ID] [今日\|昨日\|本周]`（`.上分总结`） | 上分总结图 |
| `.我的ID`（`.我的绑定` / `.myid`）/ `.解绑`（`.unbind`） | 查看 / 解除绑定 |
| `.倒计时`（`.周末倒计时`） | 下一个周末和节假日倒计时 |
| `@机器人 + 消息` | 与 AI 角色聊天 |

### 被动功能（无需命令）

| 触发 | 行为 |
|------|------|
| 群里聊到「吃什么 / 吃啥」 | 随机推荐一种食物（每群有冷却） |
| 群内连续相同消息 | 概率复读 |
| 戳一戳机器人 | AI 上下文回复 |
| 入群 / 退群 | 欢迎语 / 退群播报（含逗留时长） |

### 管理命令（仅主人）

| 类别 | 命令 |
|------|------|
| 📊 统计 | `.龙王` `.词云 [N]` `.词云开启/关闭/状态` `.统计开启/关闭/状态`（指令日报） |
| 📰 晨报 | `.新闻开启/关闭/测试/状态` `.新闻key <智谱APIkey>` |
| ⏳ 倒计时 | `.倒计时开启/关闭/状态/测试` |
| 🔀 插话 | `.插话开启`（`.插话on`）`.插话关闭`（`.插话off`）`.插话状态` `.插话概率` |
| 🛡 进群 | `.自动通过 关键字`（`.自动同意`）`.自动通过关闭/查看/数量`；申请审批直接回复「同意/拒绝」 |

> 管理命令与帮助菜单的完整版会自动**私发**主人（含进群暗号），不在群内回显。

## 🗂 目录结构

```
bot.py                      入口
common.py                   公共工具（权限、原子状态读写、图片缓存、渲染管线、HTTP 单例）
pyproject.toml              NoneBot2 插件注册 + ruff/pytest 配置
requirements.txt            依赖清单
.env.example                环境变量模板
docs/                       附加文档（AI 人设表等）
plugins/
  ├── auto_chat/            AI 聊天（角色扮演 + 多用户记忆）
  ├── chat_stats/           群聊统计（龙王/词云）+ PostgreSQL 存储层
  ├── cmd_stats/            每日指令使用统计
  ├── error_notify/         插件/定时任务报错私聊通知
  ├── fun/                  抽签运势
  ├── group_leave/          进出群播报
  ├── help/                 帮助菜单
  ├── holiday_countdown/    节假日倒计时
  ├── news/                 每日晨报
  ├── owstats/              守望先锋战绩查询
  ├── random_chat/          随机插话
  ├── repeater/             自动复读
  ├── request_manager/      好友/进群申请管理
  └── what_to_eat/          今天吃什么（被动触发）

plugins-disabled/           已停用插件（不会被加载；含 llm_price 大模型价格对比）
tests/                      测试（stub 模拟 NoneBot，无需真实环境）
```

## 🧪 运行测试

```bash
pip install -r requirements.txt
pip install pytest
python -m pytest -q
```

测试使用内置 stub 模拟 NoneBot 环境（当前 149 项），无需安装真实 NoneBot、数据库或 NapCat。推送到 GitHub 后 Actions 会在 Python 3.10/3.12 的干净环境自动安装依赖并跑测试 + ruff 检查（见 `.github/workflows/ci.yml`）。

## 🚢 生产部署

最小 systemd 示例（完整步骤——系统库、字体、数据库、代理、NapCat 对接——见 [DEPLOY.md](DEPLOY.md)）：

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
systemctl daemon-reload
systemctl enable --now qqbot
journalctl -u qqbot -f    # 查看日志
```

## 🛡 安全说明

- 本仓库**仅包含代码**，不含任何运行配置
- `.gitignore` 已排除全部敏感文件：`.env`、`db.json`、`config.json`、`ai_config.json`、`bindings.json`、各类 `state.json` 等（含 QQ 号 / 群号 / 密钥 / 聊天记录）
- 状态文件损坏时自动备份为 `.corrupt-*` 再重建，不会静默清空配置
- 部署时请自行保管好配置文件，不要提交到仓库
- 所有 HTML 渲染已做转义与外部资源拦截（防 XSS / SSRF）
- 一言 / 新闻 / AI 生成等外部内容发送时一律以纯文本段发送（防 CQ 码注入）

## 📜 技术栈

- [NoneBot2](https://nonebot.dev/) · [OneBot V11](https://onebot.dev/)
- PostgreSQL · psycopg3（异步连接池 + 批量写入）
- Pillow · weasyprint · APScheduler
- httpx

## 📄 License

本项目仅供学习交流使用，请遵守所在地区的法律法规与平台使用规范。
