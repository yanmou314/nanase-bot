# qq-bot 🤖

基于 [NoneBot2](https://nonebot.dev/) + [OneBot V11](https://github.com/botuniverse/onebot-11) 的 QQ 群聊机器人，通过 [NapCat](https://github.com/NapNeko/NapCatQQ) 连接 QQ，提供 AI 聊天、群聊统计、游戏战绩查询、新闻推送等丰富功能。

## ✨ 功能总览

### 🎲 娱乐
| 指令 | 说明 |
|------|------|
| `.rp`（运势 / 抽签 / 求签） | 今日运势签文 |

### 🎮 守望先锋（国服）查询
| 指令 | 说明 |
|------|------|
| `.绑定 名字#数字` | 绑定你的战网 ID（一次即可） |
| `.战报 [ID]` | 近期战绩图 |
| `.段位 [ID]` | 段位历史图 |
| `.强度 [ID]` | 强度分析图 |
| `.总结 [ID] [今日\|昨日\|本周]` | 上分总结图 |
| `.我的ID` / `.解绑` | 查看 / 解除绑定 |

> 绑定后 `[ID]` 可省略；加 ID 可查询他人战绩。
> 查询耗时超过 30 秒会自动发送提醒消息，避免等待焦虑。

### 📊 群聊统计
| 指令 | 说明 |
|------|------|
| `.龙王` | 今日发言最多的人 |
| `.词频 [N]` | 今日热词词云图 |

### 📰 新闻推送（仅群主可用）
| 指令 | 说明 |
|------|------|
| `.新闻开启` | 开启每日新闻（每天 8:00 以图片卡片推送） |
| `.新闻关闭` | 关闭每日新闻 |
| `.新闻测试` | 立即发送新闻图片测试 |
| `.新闻状态` | 查看推送状态 |

> 新闻源：60秒读懂世界，失败自动降级百度热搜。

### 📊 词频推送（仅群主可用）
| 指令 | 说明 |
|------|------|
| `.词频开启` | 开启每日词频（每天 8:05 推送昨日热词词云图） |
| `.词频关闭` | 关闭每日词频推送 |
| `.词频状态` | 查看推送状态 |

### 🖥 服务器状态（仅群主可用）
| 指令 | 说明 |
|------|------|
| `.服务器` | 服务器 CPU / 内存 / 磁盘 / 负载 / 服务状态卡片图 |

### ✨ 自动功能（无需指令）
- **AI 聊天**：@机器人 触发，接入大模型 API（失败时降级青云客 / 固定回复），自定义人设；被戳有随机卖萌回复
- **复读机**：群内同一消息 / 图片连发 3 次自动复读
- **退群通知**：有人退群 / 被踢时自动播报
- **入群审批**：好友申请 / 进群邀请通知群主，私聊回复「同意 / 拒绝」处理
- **私聊转发**：非群主私聊内容自动转发给群主
- **群消息统计**：后台记录所有发言（仅保留 30 天，每日凌晨 3:00 自动清理），供龙王 / 词频使用

## 🛠 技术栈

- Python 3.9+
- [NoneBot2](https://nonebot.dev/) - 机器人框架
- [OneBot V11 Adapter](https://github.com/nonebot/adapter-onebot) - 协议适配
- [NapCatQQ](https://github.com/NapNeko/NapCatQQ) - QQ 协议端
- [nonebot-plugin-apscheduler](https://github.com/nonebot/plugin-apscheduler) - 定时任务
- Pillow - 图片渲染（词云图 / 新闻卡片 / 状态卡片）

## 📁 目录结构

```
qq-bot/
├── bot.py                  # 入口
├── common.py               # 共享工具（权限判断、图片保存等）
├── pyproject.toml          # 插件加载配置
├── plugins/
│   ├── auto_chat/          # AI 聊天 + 戳一戳
│   ├── chat_stats/         # 群聊统计（龙王/词频 + 定时清理）
│   ├── fun/                # 运势抽签
│   ├── group_leave/        # 退群通知
│   ├── help/               # 指令菜单
│   ├── news/               # 每日新闻图片推送
│   ├── owstats/            # 守望先锋战绩查询（带超时提醒）
│   ├── repeater/           # 复读机
│   ├── request_manager/    # 入群/好友审批、私聊转发
│   └── server_status/      # 服务器状态卡片
```

## 🚀 部署

### 1. 安装依赖

```bash
pip install nonebot2 nonebot-adapter-onebot nonebot-plugin-apscheduler pillow httpx
```

### 2. 配置 NapCat

安装并启动 [NapCatQQ](https://github.com/NapNeko/NapCatQQ)，配置正向 WebSocket 连接指向机器人端口（默认 `<PRIVATE_IP>:8080`）。

### 3. 配置机器人

编辑 `.env`：

```ini
DRIVER=~fastapi+~httpx+~websockets
HOST=<PRIVATE_IP>
PORT=8080
LOG_LEVEL=INFO
COMMAND_START=["."]
```

### 4. 配置群主

在 `common.py` 中修改：

```python
OWNER = "你的QQ号"
```

### 5. 配置 AI 聊天（可选）

编辑 `plugins/auto_chat/config.json`：

```json
{"api_key": "你的 API Key"}
```

### 6. 中文字体（图片渲染必需）

确保服务器安装了字体（新闻卡片 / 词云图 / 状态卡片渲染依赖）：

```bash
apt install fonts-noto-cjk
mkdir -p /usr/share/fonts/custom && cp 字体文件.ttf /usr/share/fonts/custom/
```

### 7. 启动

```bash
python bot.py
```

### systemd 服务（推荐）

```ini
[Unit]
Description=NoneBot2 QQ Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/path/to/qq-bot
ExecStart=/path/to/venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now qqbot
```

## 🔒 安全说明

- `.env`、`config.json`、`bindings.json`、数据库文件已在 `.gitignore` 中排除，不会提交到仓库
- 所有管理指令（新闻 / 词频推送 / 服务器状态）均校验群主身份（QQ 号与 `OWNER` 一致）
- 聊天记录仅保留 30 天，自动清理

## 📄 License

MIT
