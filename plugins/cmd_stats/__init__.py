"""指令使用统计：每天 00:00 汇总前一天指令使用情况并发送图片到所有已开启的群。"""
import asyncio
import html as html_mod
import logging
import os
import threading
import time
from collections import OrderedDict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from nonebot import get_bot, get_driver, on_command
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageSegment
from nonebot_plugin_apscheduler import scheduler

from common import gradient_background, is_owner, load_json_state, render_html_to_png, save_json_state
from plugins.chat_stats.db_pg import exec

_logger = logging.getLogger(__name__)
_SH = ZoneInfo("Asia/Shanghai")

TOP_CMDS = 10
TOP_USERS = 5
NICK_TTL = 300
NICK_CACHE_MAX = 10000
CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "push_config.json")
ACCENT = "#D9A94E"
GREEN = "#4ECDC4"
_config_lock = threading.RLock()  # 必须可重入：_add_group/_remove_group 持锁时内部会再调保存

stats_on_cmd = on_command("统计开启", priority=5, block=True)
stats_off_cmd = on_command("统计关闭", priority=5, block=True)
stats_status_cmd = on_command("统计状态", priority=5, block=True)

_COMMAND_START = tuple(get_driver().config.command_start)
_CMD_PREFIX = _COMMAND_START[0] if _COMMAND_START else "."
CMD_LIKE = _CMD_PREFIX + "%"  # 与 .env 的 COMMAND_START 保持一致

_name_cache: OrderedDict = OrderedDict()
_name_ts: dict = {}


def _load_config() -> dict:
    data = load_json_state(CONFIG_FILE, _config_lock)
    # 旧格式 {"group_id": 123} 自动迁移到多群格式
    if "groups" not in data and data.get("group_id"):
        try:
            old_gid = int(data["group_id"])
        except (TypeError, ValueError):
            old_gid = 0
        data["groups"] = [old_gid] if old_gid > 0 else []
        data.pop("group_id", None)
        save_json_state(CONFIG_FILE, data, _config_lock)
    if not isinstance(data.get("groups"), list):
        data["groups"] = []
    return data


def _target_groups() -> list[int]:
    out = []
    for g in _load_config().get("groups", []):
        try:
            gid = int(g)
        except (TypeError, ValueError):
            continue  # 脏配置跳过，不让单个坏值拖垮整个日报任务
        if gid > 0:
            out.append(gid)
    return out


def _valid_groups(data: dict) -> set[int]:
    groups = set()
    for value in data.get("groups", []):
        try:
            gid = int(value)
        except (TypeError, ValueError):
            continue
        if gid > 0:
            groups.add(gid)
    return groups


def _add_group(gid: int) -> None:
    with _config_lock:
        data = load_json_state(CONFIG_FILE, _config_lock)
        groups = _valid_groups(data)
        groups.add(gid)
        data["groups"] = sorted(groups)
        save_json_state(CONFIG_FILE, data, _config_lock)


def _remove_group(gid: int) -> None:
    with _config_lock:
        data = load_json_state(CONFIG_FILE, _config_lock)
        groups = _valid_groups(data)
        groups.discard(gid)
        data["groups"] = sorted(groups)
        save_json_state(CONFIG_FILE, data, _config_lock)


def _prev_day() -> str:
    return (datetime.now(_SH).date() - timedelta(days=1)).isoformat()


def _day_label(day: str) -> str:
    d = date.fromisoformat(day)
    return f"{d.year}年{d.month}月{d.day}日"


async def _collect(day: str) -> dict:
    summary_rows, command_rows, user_rows = await asyncio.gather(
        exec(
            "SELECT COUNT(*), COUNT(DISTINCT group_id), COUNT(DISTINCT user_id) "
            "FROM messages WHERE day=%s AND text LIKE %s",
            (day, CMD_LIKE),
        ),
        exec(
            "SELECT split_part(trim(text), ' ', 1) AS command, COUNT(*), MIN(id) "
            "FROM messages WHERE day=%s AND text LIKE %s "
            "GROUP BY command ORDER BY COUNT(*) DESC, MIN(id) ASC LIMIT %s",
            (day, CMD_LIKE, TOP_CMDS),
        ),
        exec(
            "SELECT user_id, COUNT(*), (array_agg(group_id ORDER BY id))[1], MIN(id) "
            "FROM messages WHERE day=%s AND text LIKE %s "
            "GROUP BY user_id ORDER BY COUNT(*) DESC, MIN(id) ASC LIMIT %s",
            (day, CMD_LIKE, TOP_USERS),
        ),
    )
    total, groups, users = summary_rows[0] if summary_rows else (0, 0, 0)
    cmds = [(str(command), count) for command, count, _ in command_rows]
    users_top = [(user_id, count) for user_id, count, _, _ in user_rows]
    user_groups = {user_id: group_id for user_id, _, group_id, _ in user_rows}
    return {
        "total": total,
        "groups": groups,
        "users": users,
        "cmds": cmds,
        "users_top": users_top,
        "user_groups": user_groups,
    }


async def _fetch_name(bot, group_id: int, user_id: int) -> str:
    key = (group_id, user_id)
    now = time.time()
    cached = _name_cache.get(key)
    if cached and now - _name_ts.get(key, 0) < NICK_TTL:
        _name_cache.move_to_end(key)
        return cached
    try:
        info = await bot.get_group_member_info(group_id=group_id, user_id=user_id)
        name = info.get("card") or info.get("nickname") or str(user_id)
    except Exception:
        name = str(user_id)
    _name_cache[key] = name
    _name_ts[key] = now
    _name_cache.move_to_end(key)
    while len(_name_cache) > NICK_CACHE_MAX:  # 简单 LRU，防止长期运行内存增长
        old = _name_cache.popitem(last=False)
        _name_ts.pop(old[0], None)
    return name


async def _build_stats(day: str) -> dict:
    data = await _collect(day)
    bot = get_bot()
    users_named = []
    for uid, cnt in data["users_top"]:
        gid = data["user_groups"].get(uid)
        name = await _fetch_name(bot, gid, uid) if gid else str(uid)
        users_named.append((name, cnt))
    data["users_named"] = users_named
    return data


def _row_html(rank: int, name: str, cnt: int, max_cnt: int, color: str) -> str:
    ratio = max(cnt / max_cnt, 0.02) if max_cnt else 0
    name_esc = html_mod.escape(name, quote=True)  # 防止昵称/指令文本注入 HTML
    return f"""
    <div class="row">
      <div class="rank">{rank + 1:02d}</div>
      <div class="mid">
        <div class="name">{name_esc}</div>
        <div class="bar"><div class="bf" style="width:{ratio * 100:.1f}%;background:{color}"></div></div>
      </div>
      <div class="num"><b>{cnt}</b><span>次</span></div>
    </div>"""


def _render(day_label: str, data: dict) -> str:
    cmds = data["cmds"]
    users_named = data["users_named"]
    cmd_h = len(cmds) * 66 if cmds else 80
    user_h = len(users_named) * 66 if users_named else 0
    w, h = 900, 380 + cmd_h + user_h
    bg = gradient_background(w, h)
    max_cmd = max((c for _, c in cmds), default=0) or 1
    max_user = max((c for _, c in users_named), default=0) or 1
    if cmds:
        cmds_html = "".join(
            _row_html(i, name, cnt, max_cmd, ACCENT) for i, (name, cnt) in enumerate(cmds)
        )
    else:
        cmds_html = '<div class="empty">当天没有指令使用记录</div>'
    if users_named:
        users_html = "".join(
            _row_html(i, name, cnt, max_user, GREEN) for i, (name, cnt) in enumerate(users_named)
        )
    else:
        users_html = '<div class="empty">—</div>'

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
@page {{ size: {w}px {h}px; margin: 0; }}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ width: {w}px; height: {h}px; font-family: "Noto Sans CJK SC", sans-serif;
       background-image: url({bg}); background-size: cover; }}
.card {{ padding: 26px 40px; }}
.head {{ display: table; width: 100%; margin-top: 8px; }}
.head .t {{ display: table-cell; vertical-align: middle; font-size: 40px; font-weight: 700;
            color: #1f1d24; letter-spacing: 2px; }}
.head .d {{ display: table-cell; vertical-align: middle; text-align: right;
            font-size: 21px; color: #8e8a96; }}
.summary {{ font-size: 22px; color: #6f6b78; margin-top: 14px; }}
.summary b {{ color: #1f1d24; }}
.panel {{ background: #ffffff; border-radius: 26px; margin-top: 16px; padding: 12px 40px 14px;
          box-shadow: 0 20px 55px rgba(40, 30, 50, .10); }}
.ptitle {{ font-size: 26px; font-weight: 700; color: #1f1d24; padding: 10px 0 4px; }}
.row {{ display: table; width: 100%; padding: 18px 0; border-bottom: 1px solid #efedf1; }}
.row:last-child {{ border-bottom: none; }}
.rank {{ display: table-cell; vertical-align: middle; width: 56px;
         font-size: 26px; font-weight: 700; color: #c9c5cf; }}
.mid {{ display: table-cell; vertical-align: middle; padding: 0 24px; }}
.name {{ font-size: 28px; font-weight: 700; color: #1f1d24; }}
.bar {{ height: 5px; background: #f1eff3; border-radius: 3px; margin-top: 10px; overflow: hidden; }}
.bf {{ height: 100%; border-radius: 3px; background: #d9a94e; }}
.num {{ display: table-cell; vertical-align: middle; text-align: right; width: 100px; }}
.num b {{ font-size: 34px; font-weight: 700; color: #1f1d24; }}
.num span {{ font-size: 19px; color: #8e8a96; margin-left: 4px; }}
.empty {{ text-align: center; font-size: 24px; color: #a5a1ab; padding: 30px 0; }}
.foot {{ text-align: right; font-size: 18px; color: #a5a1ab; margin-top: 14px; }}
</style></head>
<body>
  <div class="card">
    <div class="head">
      <div class="t">📊 指令使用统计</div>
      <div class="d">{day_label}</div>
    </div>
    <div class="summary">共 <b>{data['total']}</b> 次指令 · <b>{data['groups']}</b> 个群 · <b>{data['users']}</b> 人使用</div>
    <div class="panel">
      <div class="ptitle">🏆 热门指令 TOP {len(cmds)}</div>
      {cmds_html}
    </div>
    <div class="panel">
      <div class="ptitle">👤 指令达人 TOP {len(users_named)}</div>
      {users_html}
    </div>
    <div class="foot">每天 00:00 自动统计 · 数据来源群聊记录</div>
  </div>
</body></html>"""

    return render_html_to_png(html, "cmd", CACHE_DIR, max_age=7 * 24 * 60 * 60)


async def _run_daily() -> str:
    day = _prev_day()
    data = await _build_stats(day)
    return await asyncio.to_thread(_render, _day_label(day), data)




@scheduler.scheduled_job("cron", hour=0, minute=0, id="daily_cmd_stats", timezone="Asia/Shanghai")
async def daily_cmd_stats_job():
    groups = _target_groups()
    if not groups:
        return
    try:
        path = await _run_daily()
        bot = get_bot()
    except Exception:
        _logger.exception("每日指令统计生成失败")
        return
    for group_id in groups:
        try:
            await bot.send_group_msg(group_id=group_id, message=MessageSegment.image("file://" + path))
        except Exception:
            _logger.exception("指令统计推送到群 %s 失败", group_id)


# ---------------- 多群开关命令 ----------------
@stats_on_cmd.handle()
async def stats_on(event: GroupMessageEvent):
    if not is_owner(event):
        await stats_on_cmd.finish("❌ 你没有权限使用此功能")
    _add_group(event.group_id)
    await stats_on_cmd.finish("✅ 本群已开启每日指令统计推送（每天 00:00 发送）")


@stats_off_cmd.handle()
async def stats_off(event: GroupMessageEvent):
    if not is_owner(event):
        await stats_off_cmd.finish("❌ 你没有权限使用此功能")
    _remove_group(event.group_id)
    await stats_off_cmd.finish("✅ 本群已关闭每日指令统计推送")


@stats_status_cmd.handle()
async def stats_status(event: GroupMessageEvent):
    if not is_owner(event):
        await stats_status_cmd.finish("❌ 你没有权限使用此功能")
    groups = _target_groups()
    if groups:
        await stats_status_cmd.finish(f"📈 每日指令统计已开启于 {len(groups)} 个群（每天 00:00 发送）：\n{'、'.join(map(str, groups))}")
    await stats_status_cmd.finish("📈 每日指令统计：未开启")
