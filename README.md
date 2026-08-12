# QQ Bot (NoneBot2)

基于 [NoneBot2](https://nonebot.dev/) 的 QQ 群聊机器人，通过 OneBot V11 协议连接 [NapCatQQ](https://github.com/NapNeko/NapCatQQ) 等 OneBot 实现。

## 功能

| 插件 | 功能 |
|------|------|
| `fun` | 抽签 / 今日运势 |
| `owstats` | 守望先锋国服战绩查询（战报 / 段位 / 强度 / 总结） |
| `chat_stats` | 群聊统计（龙王 / 词云）+ 每日词云推送 |
| `news` | 每日新闻推送（60秒读懂世界 + 百度热搜回退） |
| `holiday_countdown` | 周末/节假日倒计时 + 每日推送 |
| `auto_chat` | @机器人 AI 聊天（支持 Poke 戳一戳） |
| `request_manager` | 好友/进群申请处理、关键字自动通过、私聊转发 |
| `repeater` | 自动复读 |
| `cmd_stats` | 每日指令使用统计 |
| `group_leave` | 退群通知 |
| `help` | 帮助菜单 |

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt   # 见下方依赖清单
```

依赖：`nonebot2`、`nonebot-adapter-onebot`、`nonebot-plugin-apscheduler`、`httpx`、`PIL`、`psycopg`、`psycopg-pool`、`weasyprint`

### 2. 配置

复制 `.env.example` 为 `.env` 并填写：

```bash
cp .env.example .env
```

- `QQBOT_OWNER`：机器人主人的 QQ 号（管理权限）

各插件按需创建状态文件（未配置则功能不启用）：
- `plugins/chat_stats/db.json`：PostgreSQL 连接串，如 `{"dsn": "postgresql://user:pass@127.0.0.1:5432/qqbot"}`
- `plugins/auto_chat/config.json`：AI 接口密钥，如 `{"api_key": "sk-xxx"}`

### 3. 运行

```bash
python bot.py
```

连接 OneBot V11 WebSocket（默认 `127.0.0.1:8080`）。

## 目录结构

```
bot.py                入口
common.py             公共工具（权限检查、图片缓存等）
plugins/
  ├── auto_chat/      AI 聊天
  ├── chat_stats/     群聊统计 + 存储层
  ├── cmd_stats/      指令统计
  ├── fun/            抽签运势
  ├── group_leave/    退群通知
  ├── help/           帮助菜单
  ├── holiday_countdown/ 节假日倒计时
  ├── news/           新闻推送
  ├── owstats/        守望先锋战绩
  ├── repeater/       自动复读
  └── request_manager/ 申请管理
```

## 说明

- 本仓库仅包含代码，**不含任何运行配置**（QQ 号、群号、数据库密码、API 密钥等均已排除，见 `.gitignore`）
- 各插件独立运行，删除对应插件目录即可禁用功能
