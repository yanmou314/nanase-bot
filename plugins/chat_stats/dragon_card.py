import asyncio
import base64
import io
import os
import random
import subprocess
import time
from datetime import date

import httpx
from PIL import Image, ImageDraw, ImageFilter
from weasyprint import HTML

CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")

ACCENT = "#D9A94E"


async def _fetch_avatar(user_id: int) -> bytes | None:
    url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
    try:
        async with httpx.AsyncClient(timeout=6, follow_redirects=True) as client:
            r = await client.get(url)
            if r.status_code == 200 and r.content:
                return r.content
    except Exception:
        pass
    return None


def _background() -> str:
    W, H = 900, 800
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    top, bottom = (249, 248, 250), (243, 241, 246)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _row_html(rank: int, name: str, cnt: int, max_cnt: int, total: int, av_b64: str | None) -> str:
    ratio = max(cnt / max_cnt, 0.02) if max_cnt else 0
    share = cnt / total * 100 if total else 0
    if av_b64:
        avatar = (
            f'<div class="avatar" style="background-image:url(data:image/jpeg;base64,{av_b64});">'
            f'<span class="ring"></span></div>'
        )
    else:
        avatar = f'<div class="avatar noimg">{rank + 1}</div>'
    return f"""
    <div class="row">
      <div class="rank">{rank + 1:02d}</div>
      {avatar}
      <div class="mid">
        <div class="name">{'<span class="c1">👑</span>' if rank == 0 else ''}{name}</div>
        <div class="bar"><div class="bf" style="width:{ratio * 100:.1f}%"></div></div>
      </div>
      <div class="num"><b>{cnt}</b><span>条</span></div>
    </div>"""


def _render(rows: list, avatars: dict) -> str:
    bg = _background()
    total = sum(c for _, _, c in rows)
    max_cnt = max(c for _, _, c in rows) or 1
    body = "".join(
        _row_html(rank, name, cnt, max_cnt, total,
                  base64.b64encode(avatars[uid]).decode() if avatars.get(uid) else None)
        for rank, (uid, name, cnt) in enumerate(rows)
    )
    today = date.today()
    week = "一二三四五六日"[today.weekday()]

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
@page {{ size: 900px 800px; margin: 0; }}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ width: 900px; height: 800px; font-family: "Noto Sans CJK SC", sans-serif;
       background-image: url({bg}); background-size: cover; }}
.card {{ padding: 26px 40px; }}
.head {{ display: table; width: 100%; margin-top: 8px; }}
.head .t {{ display: table-cell; vertical-align: middle; font-size: 42px; font-weight: 700;
            color: #1f1d24; letter-spacing: 2px; }}
.head .t .crown {{ font-size: 30px; margin-right: 10px; }}
.head .d {{ display: table-cell; vertical-align: middle; text-align: right;
            font-size: 21px; color: #8e8a96; }}
.panel {{ background: #ffffff; border-radius: 26px; margin-top: 18px; padding: 10px 44px 14px;
          box-shadow: 0 20px 55px rgba(40, 30, 50, .10); }}
.row {{ display: table; width: 100%; padding: 26px 0; border-bottom: 1px solid #efedf1; }}
.row:last-child {{ border-bottom: none; }}
.rank {{ display: table-cell; vertical-align: middle; width: 62px;
         font-size: 30px; font-weight: 700; color: #c9c5cf; }}
.avatar {{ display: table-cell; vertical-align: middle; width: 92px; height: 92px;
            border-radius: 50%; background-size: cover; background-position: center;
            background-color: #efedf1; position: relative; }}
.avatar.noimg {{ background: linear-gradient(160deg, #e8e6ec, #d8d5dd); text-align: center;
                  line-height: 92px; font-size: 30px; font-weight: 700; color: #a39faa; }}
.mid {{ display: table-cell; vertical-align: middle; padding: 0 30px; }}
.name {{ font-size: 32px; font-weight: 700; color: #1f1d24; }}
.name .c1 {{ font-size: 22px; margin-right: 8px; }}
.bar {{ height: 5px; background: #f1eff3; border-radius: 3px; margin-top: 12px; overflow: hidden; }}
.bf {{ height: 100%; border-radius: 3px; background: #d9a94e; }}
.num {{ display: table-cell; vertical-align: middle; text-align: right; width: 110px; }}
.num b {{ font-size: 40px; font-weight: 700; color: #1f1d24; }}
.num span {{ font-size: 20px; color: #8e8a96; margin-left: 4px; }}
.foot {{ text-align: right; font-size: 19px; color: #a5a1ab; margin-top: 16px; }}
</style></head>
<body>
  <div class="card">
    <div class="head">
      <div class="t"><span class="crown">👑</span>今日龙王</div>
      <div class="d">{today.year}年{today.month}月{today.day}日 · 周{week}</div>
    </div>
    <div class="panel">{body}</div>
    <div class="foot">今日共 {total} 条消息 · 数据实时统计</div>
  </div>
</body></html>"""

    os.makedirs(CACHE_DIR, exist_ok=True)
    stamp = int(time.time() * 1000)
    tmp_pdf = os.path.join(CACHE_DIR, f"dragon_{stamp}.pdf")
    path = os.path.join(CACHE_DIR, f"dragon_{stamp}.png")
    HTML(string=html).write_pdf(tmp_pdf)
    subprocess.run(
        ["pdftoppm", "-png", "-r", "192", "-singlefile", tmp_pdf, path[:-4]],
        check=True, capture_output=True,
    )
    os.remove(tmp_pdf)
    return path


async def build_card_async(rows: list) -> str:
    avatars = {}
    for rank, (uid, name, cnt) in enumerate(rows):
        data = await _fetch_avatar(uid)
        if data:
            avatars[uid] = data
    return await asyncio.to_thread(_render, rows, avatars)
