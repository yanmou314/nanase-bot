import os
import random
import shutil
import socket
import subprocess
import time
from datetime import datetime

from nonebot import get_bot, on_command
from nonebot.adapters.onebot.v11 import MessageEvent, MessageSegment
from nonebot_plugin_apscheduler import scheduler
from PIL import Image, ImageDraw, ImageFont

from common import OWNER, is_owner

server_cmd = on_command("服务器", aliases={"服务器状态", "状态", "服务器信息"}, priority=5, block=True)

CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
FONT_BOLD = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
FONT_REG = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
FONT_FUN = "/usr/share/fonts/custom/ZCOOLKuaiLe-Regular.ttf"

GREEN = (90, 200, 120)
ORANGE = (255, 170, 60)
RED = (255, 90, 90)
DARK = (60, 55, 80)
GRAY = (150, 145, 165)

MEM_WARN_PCT = 85
MEM_CHECK_INTERVAL = 10
_last_warn_ts = 0.0
_warn_cooldown = 600
_warned_high = False


def _cpu_usage() -> float:
    def sample():
        with open("/proc/stat") as f:
            parts = f.readline().split()
        vals = [int(x) for x in parts[1:]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        return idle, sum(vals)

    i1, t1 = sample()
    time.sleep(0.5)
    i2, t2 = sample()
    return max(0.0, min(100.0, (1 - (i2 - i1) / max(1, t2 - t1)) * 100))


def _proc_int(path: str, key: str) -> int:
    try:
        with open(path, "r") as f:
            for line in f:
                if line.startswith(key):
                    return int(line.split()[1])
    except Exception:
        pass
    return 0


def _mem_info() -> tuple:
    total = _proc_int("/proc/meminfo", "MemTotal:")
    free = _proc_int("/proc/meminfo", "MemFree:")
    buffers = _proc_int("/proc/meminfo", "Buffers:")
    cached = _proc_int("/proc/meminfo", "Cached:")
    used = max(0, total - free - buffers - cached)
    return used, total


def _loadavg() -> str:
    try:
        with open("/proc/loadavg") as f:
            return " ".join(f.read().split()[:3])
    except Exception:
        return "-"


def _uptime() -> str:
    try:
        with open("/proc/uptime") as f:
            sec = float(f.read().split()[0])
        d, r = divmod(int(sec), 86400)
        h, m = divmod(r, 3600)
        m //= 60
        return f"{d}天{h}小时{m}分" if d else f"{h}小时{m}分"
    except Exception:
        return "-"


def _service_status(name: str) -> str:
    try:
        out = subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True, timeout=5)
        return "运行中" if out.stdout.strip() == "active" else "异常"
    except Exception:
        return "未知"


def _status_color(v: float) -> tuple:
    if v < 60:
        return GREEN
    if v < 85:
        return ORANGE
    return RED


def _round_rect(draw: ImageDraw.ImageDraw, box, radius: int, fill):
    x0, y0, x1, y1 = box
    r = min(radius, (x1 - x0) // 2, (y1 - y0) // 2)
    draw.rounded_rectangle([x0, y0, x1, y1], radius=r, fill=fill)


def _render(data: dict) -> str:
    W, H = 880, 1040
    img = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(img, "RGBA")

    top, bottom = (255, 241, 248), (233, 245, 255)
    for y in range(H):
        t = y / H
        draw.line([(0, y), (W, y)],
                  fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))

    rnd = random.Random(7)
    pastel = ["#FFD3E0", "#C9F0FF", "#FFF3C4", "#D8F3DC", "#E7D9FF", "#FFE8D6"]
    for _ in range(80):
        x, y, r = rnd.randint(0, W), rnd.randint(0, H), rnd.randint(6, 30)
        draw.ellipse([x - r, y - r, x + r, y + r], fill=pastel[rnd.randint(0, 5)] + f"{rnd.randint(30, 80):02X}")

    f_title = ImageFont.truetype(FONT_FUN, 52)
    f_date = ImageFont.truetype(FONT_REG, 24)
    f_lab = ImageFont.truetype(FONT_REG, 26)
    f_val = ImageFont.truetype(FONT_BOLD, 28)
    f_small = ImageFont.truetype(FONT_REG, 22)
    f_head = ImageFont.truetype(FONT_BOLD, 28)

    draw.text((48, 44), "服务器状态", font=f_title, fill=DARK)
    draw.text((48, 112), data["time"], font=f_date, fill=GRAY)

    y = 170

    def info_row(label: str, value: str, color=DARK):
        nonlocal y
        draw.text((48, y), label, font=f_lab, fill=GRAY)
        draw.text((W - 48 - draw.textlength(value, font=f_val), y), value, font=f_val, fill=color)
        y += 48

    info_row("系统", data["os"])
    info_row("主机", data["host"])
    info_row("开机", data["boot"] + " · 已运行 " + data["uptime"])

    y += 10

    def bar(label: str, pct: float, used: str):
        nonlocal y
        draw.text((48, y), label, font=f_lab, fill=GRAY)
        draw.text((W - 48 - draw.textlength(used, font=f_val), y), used, font=f_val, fill=DARK)
        y += 40
        _round_rect(draw, [48, y, W - 48, y + 26], 13, (255, 255, 255))
        color = _status_color(pct)
        w = int((W - 96) * max(0.0, min(1.0, pct / 100)))
        if w > 0:
            _round_rect(draw, [48, y, 48 + w, y + 26], 13, color)
        draw.text((W - 48 - draw.textlength(f"{pct:.1f}%", font=f_small), y + 2), f"{pct:.1f}%",
                  font=f_small, fill=color)
        y += 56

    bar("CPU 使用率", data["cpu"], f"{data['cpu']:.1f}%")
    bar("内存占用", data["mem_pct"], data["mem"])
    bar("磁盘占用", data["disk_pct"], data["disk"])

    draw.text((48, y), "负载 (1/5/15min)", font=f_lab, fill=GRAY)
    draw.text((W - 48 - draw.textlength(data["load"], font=f_val), y), data["load"], font=f_val, fill=DARK)
    y += 56

    y += 6
    draw.text((48, y), "服务状态", font=f_head, fill=DARK)
    y += 44
    for name, status in data["services"].items():
        color = GREEN if status == "运行中" else RED
        draw.ellipse([54, y + 8, 66, y + 20], fill=color)
        draw.text((82, y), name, font=f_lab, fill=GRAY)
        draw.text((W - 48 - draw.textlength(status, font=f_val), y), status, font=f_val, fill=color)
        y += 46

    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"status_{int(time.time() * 1000)}.png")
    img.save(path, "PNG")
    return path


def _collect_data() -> dict:
    cpu = _cpu_usage()
    used_kb, total_kb = _mem_info()
    mem_pct = used_kb / total_kb * 100
    disk = shutil.disk_usage("/")
    disk_pct = disk.used / disk.total * 100
    uptime = _uptime()
    host = socket.gethostname()
    try:
        with open("/etc/os-release") as f:
            os_name = "Linux"
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    os_name = line.split("=", 1)[1].strip().strip('"')
                    break
    except Exception:
        os_name = "Linux"

    boot_time = "unknown"
    try:
        btime = _proc_int("/proc/stat", "btime")
        if btime:
            boot_time = datetime.fromtimestamp(btime).strftime("%m-%d %H:%M")
    except Exception:
        pass

    return {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "os": os_name,
        "host": host,
        "boot": boot_time,
        "uptime": uptime,
        "cpu": cpu,
        "mem": f"{used_kb / 1024 / 1024:.1f}G / {total_kb / 1024 / 1024:.1f}G",
        "mem_pct": mem_pct,
        "disk": f"{disk.used / 1024**3:.1f}G / {disk.total / 1024**3:.1f}G",
        "disk_pct": disk_pct,
        "load": _loadavg(),
        "services": {
            "机器人服务 (qqbot)": _service_status("qqbot"),
            "NapCat (napcat)": _service_status("napcat"),
            "守望数据 (overstats)": _service_status("overstats"),
        },
    }


@scheduler.scheduled_job("cron", hour="*", minute=0, id="hourly_status_push", timezone="Asia/Shanghai")
async def hourly_status_push():
    try:
        path = await __import__("asyncio").to_thread(_render, _collect_data())
        bot = get_bot()
        await bot.send_group_msg(group_id=<PRIVATE_NUMBER>, message=MessageSegment.image("file://" + path))
    except Exception:
        pass


@server_cmd.handle()
async def server_status(event: MessageEvent):
    if not is_owner(event):
        await server_cmd.finish("❌ 你没有权限使用此功能")

    data = _collect_data()
    path = _render(data)
    await server_cmd.finish(MessageSegment.image("file://" + path))


def _top_mem_processes(n: int = 3) -> list:
    try:
        out = subprocess.run(
            ["ps", "-eo", "rss,comm", "--sort=-rss", "--no-headers"],
            capture_output=True, text=True, timeout=5,
        )
        rows = []
        for line in out.stdout.strip().split("\n")[:n]:
            parts = line.strip().split(None, 1)
            if len(parts) == 2:
                rows.append(f"{parts[1]} {int(parts[0]) // 1024}M")
        return rows
    except Exception:
        return []


@scheduler.scheduled_job("interval", minutes=MEM_CHECK_INTERVAL, id="mem_monitor", timezone="Asia/Shanghai")
async def mem_monitor():
    global _last_warn_ts, _warned_high
    used_kb, total_kb = _mem_info()
    pct = used_kb / total_kb * 100
    now = time.time()
    if pct >= MEM_WARN_PCT:
        if not _warned_high or now - _last_warn_ts >= _warn_cooldown:
            _last_warn_ts = now
            _warned_high = True
            procs = "\n".join(_top_mem_processes()) or "无"
            msg = (
                f"⚠️ 服务器内存告警\n"
                f"📊 使用率：{pct:.1f}%（{used_kb / 1024 / 1024:.1f}G / {total_kb / 1024 / 1024:.1f}G）\n"
                f"💣 占用 TOP3：\n{procs}\n"
                f"建议：清理进程或重启服务，必要时升配内存"
            )
            try:
                bot = get_bot()
                await bot.send_private_msg(user_id=int(OWNER), message=msg)
            except Exception:
                pass
    else:
        if _warned_high:
            _warned_high = False
            try:
                bot = get_bot()
                await bot.send_private_msg(
                    user_id=int(OWNER),
                    message=f"✅ 内存已恢复正常（{pct:.1f}%）",
                )
            except Exception:
                pass
