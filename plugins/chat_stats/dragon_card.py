import asyncio
import base64
import html as html_mod
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from nonebot import get_driver

from common import RENDER_SEM, close_http_clients, get_http_client, gradient_background, render_html_to_png

CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
_SH = ZoneInfo("Asia/Shanghai")  # 与数据统计口径保持同一时区，避免海外部署时标题日期错一天

ACCENT = "#D9A94E"


@get_driver().on_shutdown
async def _close_avatar_http() -> None:
    await close_http_clients()


async def _fetch_avatar(user_id: int) -> bytes | None:
    url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
    try:
        r = await get_http_client(timeout=6).get(url, follow_redirects=True)
        if r.status_code == 200 and r.content:
            return r.content
    except Exception:
        pass
    return None


def _row_html(rank: int, name: str, cnt: int, max_cnt: int, av_b64: str | None) -> str:
    ratio = max(cnt / max_cnt, 0.02) if max_cnt else 0
    name_esc = html_mod.escape(name, quote=True)  # 防止昵称注入 HTML
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
        <div class="name">{'<span class="c1">👑</span>' if rank == 0 else ''}{name_esc}</div>
        <div class="bar"><div class="bf" style="width:{ratio * 100:.1f}%"></div></div>
      </div>
      <div class="num"><b>{cnt}</b><span>条</span></div>
    </div>"""


def _render(rows: list, avatars: dict) -> str:
    bg = gradient_background(900, 800)
    total = sum(c for _, _, c in rows)
    max_cnt = max(c for _, _, c in rows) or 1
    body = "".join(
        _row_html(rank, name, cnt, max_cnt,
                  base64.b64encode(avatars[uid]).decode() if avatars.get(uid) else None)
        for rank, (uid, name, cnt) in enumerate(rows)
    )
    today = datetime.now(_SH).date()
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

    return render_html_to_png(html, "dragon", CACHE_DIR, max_age=24 * 60 * 60)


async def build_card_async(rows: list) -> str:
    results = await asyncio.gather(*(_fetch_avatar(uid) for uid, _, _ in rows))
    avatars = {uid: data for (uid, _, _), data in zip(rows, results) if data}
    # weasyprint 渲染经全局渲染信号量串行化，避免小机器上并发渲染打爆内存
    async with RENDER_SEM:
        return await asyncio.to_thread(_render, rows, avatars)
