"""OW国服补丁说明推送：每小时轮询官网补丁页，新补丁渲染单张长截图发到订阅群。

只发图片不发文字。长截图失败重试 3 次后回落分块多图，再失败回落纯标题卡。
资源护栏：渲染前验内存、输入封顶、时间封顶，任何情况下不把服务拖死。
"""
import asyncio
import base64
import hashlib
import html as _html
import io
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from nonebot import get_bot, get_driver, on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent, MessageSegment
from nonebot_plugin_apscheduler import scheduler

from common import (
    FONTS,
    OWNER,
    RENDER_SEM,
    cleanup_cache,
    close_http_clients,
    get_http_client,
    is_owner,
    load_json_state,
    save_json_state,
)

_logger = logging.getLogger(__name__)
_SH = ZoneInfo("Asia/Shanghai")

STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")
CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
_LOCK = threading.RLock()

PATCH_MONTH_URL = "https://ow.blizzard.cn/news/patch-notes/live/{year}/{month:02d}/"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# ---- 资源护栏（小机器保命参数） ----
_MEM_MIN_SYSTEM_MB = 250  # 系统可用内存低于此值，本轮跳过
_MEM_MIN_SERVICE_MB = 200  # 本服务 cgroup 配额剩余低于此值，本轮跳过
_FETCH_TIMEOUT = 20  # 补丁页抓取超时（秒）
_IMG_TIMEOUT = 15  # 单张官图下载超时（秒）
_MAX_IMG_COUNT = 80  # 单个补丁最多内嵌官图数（9月刊实测55张；文件/页数/内存三道闸还在）
_MAX_IMG_BYTES = 2 * 1024 * 1024  # 单张官图下载上限
_IMG_DISPLAY_WIDTH = 720  # 内嵌图压到的显示宽度
_RENDER_TIMEOUT = 150  # 单次渲染硬上限（秒）
_JOB_DEADLINE = 480  # 整轮推送死线（秒）
_MAX_PDF_PAGES = 25  # PDF 超此页数不拼长图，直接分块
_MAX_STITCH_HEIGHT = 25000  # 拼合高度上限（px）
_MAX_FILE_BYTES = 8 * 1024 * 1024  # 成图文件上限
_RENDER_ATTEMPTS = 3  # 长截图偶发失败重试次数
_MAX_CHUNK_IMAGES = 3  # 分块回落最多正文图数（+1 张标题卡）
RELAY_GROUP_ID = 864213945  # 任务中继群（与 owstats.RELAY_GROUP_ID 保持一致）
_RELAY_SKIP = {str(RELAY_GROUP_ID)}  # 任务中继群默认不推
_SEND_SOURCE_LINK = True  # 图后追加官网原文链接（QQ 会展开成官方卡片）；不需要就改 False

_IMG_ALLOW_HOSTS = ("ld5.res.netease.com", "nie.res.netease.com")
_TITLE_RE = re.compile(r"(\d{1,2})月(\d{1,2})日")

sub_on_cmd = on_command("ow补丁订阅", priority=5, block=True)
sub_off_cmd = on_command("ow补丁退订", priority=5, block=True)
sub_status_cmd = on_command("ow补丁状态", priority=5, block=True)
sub_test_cmd = on_command("ow补丁测试", priority=5, block=True)


def _get_http_client() -> httpx.AsyncClient:
    # 统一走 common 的按超时缓存单例，由 owstats 注册的 on_shutdown 统一关闭
    return get_http_client(30)


@get_driver().on_shutdown
async def _close_shared_http_clients() -> None:
    await close_http_clients()


# ---------------- state ----------------

def _load_state() -> dict:
    # common 的 load_json_state：损坏先备份 .corrupt-<ts> 再返回 {}，避免静默丢订阅
    data = load_json_state(STATE_FILE, _LOCK)
    if not isinstance(data.get("groups"), list):
        data["groups"] = []
    if not isinstance(data.get("seen"), dict):
        data["seen"] = {}
    return data


def _save_state(data: dict) -> None:
    # common 的 save_json_state：tmp + fsync + os.replace 原子写
    save_json_state(STATE_FILE, data, _LOCK)


def _mutate_state(mutate) -> dict:
    """load → mutate → save 同锁完成，避免推送标记与订阅命令互相覆盖。"""
    with _LOCK:
        state = _load_state()
        mutate(state)
        _save_state(state)
        return state


# ---------------- 资源护栏 ----------------

def _mem_ok() -> tuple[bool, str]:
    """渲染前验血：系统可用内存与本服务配额余量，任一不足都跳过本轮。"""
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    avail_mb = int(line.split()[1]) // 1024
                    if avail_mb < _MEM_MIN_SYSTEM_MB:
                        return False, f"系统可用内存仅 {avail_mb}MB"
                    break
    except OSError:
        pass  # 读不到不挡路，只记日志由调用方处理
    for cur_p, max_p in (
        ("/sys/fs/cgroup/system.slice/qqbot.service/memory.current",
         "/sys/fs/cgroup/system.slice/qqbot.service/memory.max"),
        ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.max"),
    ):
        try:
            with open(cur_p, encoding="utf-8") as f:
                cur = int(f.read().strip())
            with open(max_p, encoding="utf-8") as f:
                raw = f.read().strip()
            if raw == "max":
                break  # 无配额限制，看系统水位即可
            headroom_mb = (int(raw) - cur) // (1024 * 1024)
            if headroom_mb < _MEM_MIN_SERVICE_MB:
                return False, f"服务内存余量仅 {headroom_mb}MB"
            break
        except (OSError, ValueError):
            continue
    return True, ""


class _TooBig(Exception):
    """确定性超限（页数/高度/文件体积）：重试也不可能变小，直接走回落。"""


# ---------------- 补丁页解析（stdlib，无新依赖） ----------------

_ALLOW_TAGS = {"p", "h3", "h4", "ul", "ol", "li", "div", "span", "strong", "em", "b", "i", "br", "img"}


def _has_class(attrs: dict, token: str) -> bool:
    return token in (attrs.get("class") or "").split()


class _PatchPageParser(HTMLParser):
    """抽官网补丁月页：div.PatchNotes-patch 为一节，h3.PatchNotes-patchTitle 为标题。

    只保留白名单标签、转义文本、官图替换为占位符，正文原样拼回 sanitized HTML。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.patches: list[dict] = []
        self._cur: dict | None = None
        self._depth = 0
        self._in_title = False
        self._skip = 0  # script/style 嵌套深度

    def _finish(self) -> None:
        title = " ".join((self._cur or {}).get("title", "").split())
        body = "".join((self._cur or {}).get("parts", []))
        if self._cur is not None and (title or body.strip()):
            self.patches.append({
                "title": title,
                "body_html": body,
                "images": self._cur["images"],
                "img_kinds": self._cur.get("img_kinds", []),
            })
        self._cur = None
        self._depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style"):
            self._skip += 1
            return
        if self._skip:
            return
        ad = dict(attrs)
        if tag == "div" and _has_class(ad, "PatchNotes-patch"):
            if self._cur is not None:
                self._finish()  # 防御：上节没闭合就开了新节
            self._cur = {"title": "", "parts": [], "images": []}
            self._depth = 1
            return
        if self._cur is None:
            return
        if tag == "div":
            self._depth += 1
        if tag == "h3" and _has_class(ad, "PatchNotes-patchTitle"):
            self._in_title = True
            return
        if tag not in _ALLOW_TAGS:
            return
        if tag == "img":
            src = (ad.get("src") or "").strip()
            try:
                host = (urlsplit(src).netloc or "").lower()
            except ValueError:
                host = ""
            if src.startswith("https://") and host in _IMG_ALLOW_HOSTS:
                idx = len(self._cur["images"])
                self._cur["images"].append(src)
                # 记下官图在原文里的角色：英雄头像 / 技能图标 / 正文配图，
                # 渲染时按角色定尺寸（否则技能图标会被放大到全文宽）
                cls = ad.get("class") or ""
                if "PatchNotesHeroUpdate-icon" in cls.split():
                    kind = "hero"
                elif "PatchNotesAbilityUpdate-icon" in cls.split():
                    kind = "ability"
                else:
                    kind = "body"
                self._cur.setdefault("img_kinds", []).append(kind)
                self._cur["parts"].append(f'<img data-owimg="{idx}">')
            return
        if tag == "br":
            self._cur["parts"].append("<br>")
            return
        if tag in ("div", "h3", "h4", "span"):
            cls = _html.escape(ad.get("class") or "", quote=True)
            self._cur["parts"].append(f"<{tag} class=\"{cls}\">" if cls else f"<{tag}>")
        else:
            self._cur["parts"].append(f"<{tag}>")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            if self._skip:
                self._skip -= 1
            return
        if self._skip or self._cur is None:
            return
        if tag == "h3" and self._in_title:
            self._in_title = False
            return
        if tag == "div":
            self._depth -= 1
            if self._depth <= 0:
                self._finish()
                return
        if tag in _ALLOW_TAGS and tag not in ("img", "br"):
            self._cur["parts"].append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._skip or self._cur is None:
            return
        if self._in_title:
            self._cur["title"] += data
        elif data.strip():
            self._cur["parts"].append(_html.escape(data))
        else:
            self._cur["parts"].append(" ")

    def handle_entityref(self, name: str) -> None:
        if self._skip or self._cur is None:
            return
        chunk = f"&{name};"
        if self._in_title:
            self._cur["title"] += chunk
        else:
            self._cur["parts"].append(chunk)

    def handle_charref(self, name: str) -> None:
        if self._skip or self._cur is None:
            return
        chunk = f"&#{name};"
        if self._in_title:
            self._cur["title"] += chunk
        else:
            self._cur["parts"].append(chunk)

    def close(self) -> None:
        if self._cur is not None:
            self._finish()
        super().close()


def parse_month_page(page_html: str, year: int, month: int, source_url: str) -> list[dict]:
    """解析补丁月页，返回 [{key,title,date,body_html,images,hash,source}]（按页内顺序）。

    非补丁标题块跳过；非法日期跳过。key 含年月，天然隔离跨月重名。
    """
    parser = _PatchPageParser()
    parser.feed(page_html)
    parser.close()
    out = []
    for item in parser.patches:
        m = _TITLE_RE.search(item["title"])
        if not m:
            continue
        try:
            day = date(year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            continue
        key = f"{year}-{month:02d}|{item['title']}"
        digest = hashlib.sha256(item["body_html"].encode("utf-8")).hexdigest()[:16]
        out.append({
            "key": key,
            "title": item["title"],
            "date": day.isoformat(),
            "body_html": item["body_html"],
            "images": item["images"],
            "img_kinds": item.get("img_kinds", []),
            "hash": digest,
            "source": source_url,
        })
    return out


def _target_months(now: datetime) -> list[tuple[int, int]]:
    """本月必查；每月 1~3 号附带查上月，防跨月补丁漏推。"""
    months = [(now.year, now.month)]
    if now.day <= 3:
        first = date(now.year, now.month, 1)
        prev = first - timedelta(days=1)
        months.append((prev.year, prev.month))
    return months


# ---------------- 官图下载（白名单域 + 封顶，失败降级） ----------------

def _jpeg_bytes(data: bytes) -> bytes | None:
    """官图压到显示宽度转 JPEG；失败返回 None（调用方降级为纯文字）。"""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGB")
            w, h = im.size
            if w > _IMG_DISPLAY_WIDTH:
                im = im.resize((_IMG_DISPLAY_WIDTH, max(1, round(h * _IMG_DISPLAY_WIDTH / w))))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=82)
            return buf.getvalue()
    except Exception:
        return None


async def _download_images(client: httpx.AsyncClient, urls: list[str]) -> list[str | None]:
    """与正文占位符一一对应的 data-URI（失败/超限位置为 None）。"""
    sem = asyncio.Semaphore(4)

    async def _one(url: str) -> str | None:
        async with sem:
            try:
                r = await client.get(
                    url, timeout=_IMG_TIMEOUT,
                    headers={"Referer": "https://ow.blizzard.cn/", "User-Agent": _UA},
                )
                r.raise_for_status()
                if len(r.content) > _MAX_IMG_BYTES or not r.content:
                    return None
                raw = await asyncio.to_thread(_jpeg_bytes, bytes(r.content))
                if not raw:
                    return None
                b64 = base64.b64encode(raw).decode()
                return f"data:image/jpeg;base64,{b64}"
            except Exception:
                return None

    picks = urls[:_MAX_IMG_COUNT]
    got = await asyncio.gather(*(_one(u) for u in picks))
    return list(got) + [None] * max(0, len(urls) - len(picks))


def _fill_images(body_html: str, data_uris: list[str | None],
                 kinds: list[str] | None = None) -> str:
    """占位符替换为内嵌图；下载失败的占位直接删掉，不断整个推送。

    按官图角色带 class（ow-hero/ow-ability），CSS 按角色定尺寸，
    否则技能图标会被放大到全文宽。
    """
    kinds = kinds or []

    def _sub(m: re.Match) -> str:
        try:
            idx = int(m.group(1))
            uri = data_uris[idx]
        except (ValueError, IndexError):
            return ""
        if not uri:
            return ""
        kind = kinds[idx] if idx < len(kinds) else "body"
        if kind in ("hero", "ability"):
            return f'<img class="ow-{kind}" src="{uri}">'
        return f'<img src="{uri}">'

    return re.sub(r'<img data-owimg="(\d+)">', _sub, body_html)


# ---------------- HTML 组装 ----------------

_CSS = """
@page{size:A4;margin:0;}
html{margin:0;padding:0;}
body{margin:0;background:#f1f2f6;font-family:"Noto Sans CJK SC","Noto Sans SC","WenQuanYi Micro Hei",sans-serif;color:#23262f;}
.card{margin:0 auto;max-width:940px;background:#fff;}
.hero{background:linear-gradient(135deg,#f99e1a 0%,#e8632c 60%,#43484c 130%);color:#fff;padding:24px 32px 18px;}
.hero .kicker{font-size:15px;letter-spacing:4px;opacity:.9;}
.hero h1{font-size:30px;margin:8px 0 4px;line-height:1.35;}
.hero .meta{font-size:14px;opacity:.92;}
.body{padding:6px 30px 8px;font-size:16px;line-height:1.6;}
.body h4{font-size:21px;color:#e8632c;margin:18px 0 8px;padding-left:12px;border-left:5px solid #f99e1a;}
.body p{margin:7px 0;}
.body ul,.body ol{margin:6px 0;padding-left:26px;}
.body li{margin:4px 0;}
.body img{max-width:100%;border-radius:8px;margin:6px 0;}
.PatchNotesHeroUpdate-header{margin:16px 0 8px;}
.PatchNotesHeroUpdate-header img{width:76px;height:auto;border-radius:10px;margin:0 0 4px;}
.PatchNotesHeroUpdate-name{font-size:20px;font-weight:700;}
.PatchNotesAbilityUpdate{margin:10px 0;}
img.ow-hero{width:76px;height:auto;border-radius:10px;margin:6px 0 2px;}
img.ow-ability{height:60px;width:auto;margin:4px 0;}
.PatchNotesAbilityUpdate-name{font-size:17px;font-weight:700;}
.dev{color:#6b7280;font-size:14.5px;background:#f7f7f9;border-radius:8px;padding:6px 12px;margin:6px 0;}
.footer{padding:14px 30px 20px;color:#8a8f9c;font-size:13px;border-top:1px dashed #e2e4ea;}
"""


def _esc(text: str) -> str:
    return _html.escape(text or "")


def build_patch_html(title: str, date_str: str, source_url: str, body_html: str) -> str:
    """全文 HTML（含标题卡与尾图，拼长图用）。"""
    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        f"<style>{_CSS}</style></head><body><div class=\"card\">"
        "<div class=\"hero\"><div class=\"kicker\">守望先锋 · 国服补丁说明</div>"
        f"<h1>{_esc(title)}</h1>"
        f"<div class=\"meta\">{_esc(date_str)} · 来源 国服官网</div></div>"
        f"<div class=\"body\">{body_html}</div>"
        "<div class=\"footer\">"
        f"<div>原文链接：{_esc(source_url)}</div>"
        f"<div>由群机器人自动推送 · {_esc(datetime.now(_SH).strftime('%Y-%m-%d %H:%M'))}</div>"
        "</div></div></body></html>"
    )


def _split_sections(body_html: str) -> list[str]:
    """按 h4 切块（分块回落用）；切不出多块就整体返回。"""
    parts = re.split(r"(?=<h4[^>]*>)", body_html)
    chunks = [p for p in (s.strip() for s in parts) if p]
    return chunks if len(chunks) > 1 else [body_html]


# ---------------- 渲染：PDF 分页 → 拼长图 ----------------

def _write_pdf(html: str, pdf_path: str) -> None:
    from weasyprint import HTML

    HTML(string=html).write_pdf(pdf_path)


def _pdf_to_pngs(pdf_path: str, out_prefix: str) -> list[str]:
    """pdftoppm 按页出图（A4@120dpi≈992px宽，手机可读）。"""
    subprocess.run(
        ["pdftoppm", "-png", "-r", "120", "-f", "1", "-l", str(_MAX_PDF_PAGES + 1),
         pdf_path, out_prefix],
        check=True, capture_output=True, timeout=120,
    )
    pages = sorted(
        p for p in os.listdir(os.path.dirname(out_prefix) or ".")
        if p.startswith(os.path.basename(out_prefix)) and p.endswith(".png")
    )
    return [os.path.join(os.path.dirname(out_prefix), p) for p in pages]


def _trim_trailing_blank(page):
    """裁掉末页底部的大片空白（取底色做差找内容下边界，留 24px breathing room）。

    PDF 分页经常剩半页空白，直接拼进长图会很难看。全白页则整页丢掉。
    """
    from PIL import Image, ImageChops

    w, h = page.size
    if h <= 48:
        return page
    # 底色取底部边缘众数（单角像素可能正好落在内容上）
    samples = [page.getpixel((x, h - 1)) for x in range(0, w, max(1, w // 20))]
    samples += [page.getpixel((0, y)) for y in range(max(0, h - 60), h, 10)]
    samples += [page.getpixel((w - 1, y)) for y in range(max(0, h - 60), h, 10)]
    bg = max(set(samples), key=samples.count)
    solid = Image.new("RGB", (w, h), bg)
    diff = ImageChops.difference(page.convert("RGB"), solid)
    # 阈值 32：正文底色 #f1f2f6 与白底差约 13，必须算空白；
    # 真实内容（文字/橙色标题/配图）差值都在 100 以上，不受影响
    mask = diff.convert("L").point(lambda v: 255 if v > 32 else 0)
    bbox = mask.getbbox()
    if bbox is None:
        return None  # 整页空白
    bottom = min(h, bbox[3] + 24)
    if bottom >= h - 8:
        return page  # 内容顶到底，无需裁
    return page.crop((0, 0, w, max(48, bottom)))


_MIN_COLLAPSE_GAP = 60  # 高于此的空白带统一压到 _COLLAPSE_KEEP，段落节奏更均匀
_COLLAPSE_KEEP = 48  # 压空白带时保留的呼吸间距（约 3 行字高，段落感还在）


def _collapse_blank_bands(img):
    """压掉拼合图里的空白带（分页断行顶出来的 60~300px 一律压到 48px）。

    只动“整行底色”的带子，阈值与 _trim_trailing_blank 同源（32/通道和 96）。
    压的是纯空白行，不可能切到字；段落之间仍保留 48px 呼吸间距。
    返回新图；无空白带时原样返回。
    """
    w, h = img.size
    if h <= _MIN_COLLAPSE_GAP:
        return img
    px = img.load()
    corners = [px[5, 5], px[w - 6, 5], px[5, h - 6], px[w - 6, h - 6]]
    bg = max(set(corners), key=corners.count)
    br, bgg, bb = bg
    blank_runs: list[tuple[int, int]] = []
    y = 0
    while y < h:
        ishort = y
        while ishort < h and _row_blank(px, w, ishort, br, bgg, bb):
            ishort += 1
        if ishort - y >= _MIN_COLLAPSE_GAP:
            blank_runs.append((y, ishort))
        y = ishort + 1 if ishort < h else h
    if not blank_runs:
        return img
    from PIL import Image as _Image

    keep_ranges: list[tuple[int, int]] = []
    prev = 0
    for a, b in blank_runs:
        keep_ranges.append((prev, a + _COLLAPSE_KEEP))
        prev = b
    keep_ranges.append((prev, h))
    new_h = sum(b - a for a, b in keep_ranges)
    canvas = _Image.new("RGB", (w, new_h), bg)
    y = 0
    for a, b in keep_ranges:
        if b > a:
            canvas.paste(img.crop((0, a, w, b)), (0, y))
            y += b - a
    _logger.info("ow补丁压掉 %d 处分页空白，共省 %dpx", len(blank_runs), h - new_h)
    return canvas


def _row_blank(px, w: int, y: int, br: int, bgg: int, bb: int) -> bool:
    for x in range(0, w, 6):
        r, g, b = px[x, y]
        if abs(r - br) + abs(g - bgg) + abs(b - bb) > 96:
            return False
    return True


def _stitch_pngs(pngs: list[str], out_path: str) -> str:
    """纵向拼长图；超高/超大抛 _TooBig 走回落。"""
    from PIL import Image

    ims = [Image.open(p) for p in pngs]
    try:
        trimmed = []
        for i, im in enumerate(ims):
            if i == len(ims) - 1 and len(ims) > 1:
                cut = _trim_trailing_blank(im)
                if cut is None:
                    _logger.info("ow补丁拼合丢弃全空白末页")
                    continue
                trimmed.append(cut)
            else:
                trimmed.append(im)
        ims = trimmed
        w = max(im.width for im in ims)
        h = sum(round(im.height * w / im.width) for im in ims)
        canvas = Image.new("RGB", (w, h), "white")
        y = 0
        for im in ims:
            if im.width != w:
                im = im.resize((w, round(im.height * w / im.width)))
            canvas.paste(im, (0, y))
            y += im.height
        # 先压分页空白带再卡高度：只算真实内容高度，虚胖不触发回落
        collapsed = _collapse_blank_bands(canvas)
        if collapsed is not canvas:
            canvas.close()
            canvas = collapsed
        if canvas.height > _MAX_STITCH_HEIGHT:
            raise _TooBig(f"拼合高度 {canvas.height}px 超限")
        tmp = out_path + ".tmp"
        canvas.save(tmp, "PNG", optimize=True)
        if os.path.getsize(tmp) > _MAX_FILE_BYTES:
            raise _TooBig("成图超 8MB")
        os.replace(tmp, out_path)
        return out_path
    finally:
        for im in ims:
            try:
                im.close()
            except Exception:
                pass


async def _render_html_pages(html: str, workdir: str, tag: str) -> list[str]:
    """HTML → PDF → 分页 PNG（在 RENDER_SEM 内串行，防打爆内存）。"""
    async with RENDER_SEM:
        pdf = os.path.join(workdir, f"{tag}.pdf")
        await asyncio.wait_for(asyncio.to_thread(_write_pdf, html, pdf), _RENDER_TIMEOUT)
        pages = await asyncio.to_thread(_pdf_to_pngs, pdf, os.path.join(workdir, tag))
        if not pages:
            raise RuntimeError("PDF 未产出任何页面")
        return pages


def _cache_path(name: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    cleanup_cache(CACHE_DIR, max_age=7 * 24 * 3600)
    return os.path.join(CACHE_DIR, name)


async def _render_long_image(patch: dict, data_uris: list, attempts: int = _RENDER_ATTEMPTS) -> list[str]:
    """单张长截图；偶发失败重试 attempts 次，确定性超限直接抛 _TooBig。"""
    digest = hashlib.sha256((patch["key"] + patch["hash"]).encode()).hexdigest()[:12]
    dest = _cache_path(f"patch_{digest}.png")
    if os.path.isfile(dest) and os.path.getsize(dest) > 0:
        return [dest]
    html = build_patch_html(patch["title"], patch["date"], patch["source"],
                            _fill_images(patch["body_html"], data_uris,
                                         patch.get("img_kinds")))
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        workdir = tempfile.mkdtemp(prefix="owpatch_", dir=CACHE_DIR)
        try:
            pages = await _render_html_pages(html, workdir, "long")
            if len(pages) > _MAX_PDF_PAGES:
                raise _TooBig(f"PDF {len(pages)} 页超限")
            try:
                return [await asyncio.to_thread(_stitch_pngs, pages, dest)]
            except _TooBig:
                raise
            except Exception as exc:
                last_exc = exc
                _logger.warning("ow补丁长图拼合失败（第 %d/%d 次）: %s", attempt, attempts, exc)
        except _TooBig:
            raise
        except Exception as exc:
            last_exc = exc
            _logger.warning("ow补丁长图渲染失败（第 %d/%d 次）: %r", attempt, attempts, exc)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        if attempt < attempts:
            await asyncio.sleep(2 * attempt)
    assert last_exc is not None
    raise last_exc


async def _render_chunked_images(patch: dict, data_uris: list) -> list[str]:
    """分块回落：标题卡 + 最多 _MAX_CHUNK_IMAGES 张正文块图。"""
    digest = hashlib.sha256((patch["key"] + patch["hash"]).encode()).hexdigest()[:12]
    dests = []
    chunks = _split_sections(_fill_images(
        patch["body_html"], data_uris, patch.get("img_kinds")))[:_MAX_CHUNK_IMAGES]
    for i, chunk in enumerate(chunks):
        dest = _cache_path(f"patch_{digest}_c{i}.png")
        if os.path.isfile(dest) and os.path.getsize(dest) > 0:
            dests.append(dest)
            continue
        html = build_patch_html(f"{patch['title']}（{i + 1}/{len(chunks)}）",
                                patch["date"], patch["source"], chunk)
        workdir = tempfile.mkdtemp(prefix="owpatch_", dir=CACHE_DIR)
        try:
            pages = await _render_html_pages(html, workdir, f"c{i}")
            if len(pages) > 6:
                _logger.warning("ow补丁分块 %d 共 %d 页，仅拼前 6 页", i, len(pages))
            dests.append(await asyncio.to_thread(_stitch_pngs, pages[:6], dest))
        except _TooBig:
            _logger.warning("ow补丁分块 %d 仍超限，跳过该块", i)
        except Exception:
            _logger.warning("ow补丁分块 %d 渲染失败", i, exc_info=True)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
    title_card = await asyncio.to_thread(
        _render_title_card, patch["title"], patch["date"], patch["source"], digest)
    return [title_card, *dests] if dests else [title_card]


def _render_title_card(title: str, date_str: str, source_url: str, digest: str) -> str:
    """最后一招：纯 Pillow 标题卡（零 weasyprint 开销，一定能出图）。"""
    from PIL import Image, ImageDraw, ImageFont

    dest = _cache_path(f"patch_{digest}_t.png")
    if os.path.isfile(dest) and os.path.getsize(dest) > 0:
        return dest
    try:
        title_font = ImageFont.truetype(FONTS["noto_bold"], 44)
        body_font = ImageFont.truetype(FONTS["noto_reg"], 26)
    except OSError:
        raise RuntimeError("标题卡字体缺失") from None
    width, margin = 1080, 64
    cw = width - margin * 2
    meas = Image.new("RGB", (width, 10), "white")
    md = ImageDraw.Draw(meas)

    def _wrap(text: str, font, max_w: int) -> list[str]:
        lines, cur = [], ""
        for ch in text:
            if md.textlength(cur + ch, font=font) <= max_w or not cur:
                cur += ch
            else:
                lines.append(cur)
                cur = ch
        if cur:
            lines.append(cur)
        return lines or [""]

    rows = [(line, title_font, 64, (31, 35, 45)) for line in _wrap(title, title_font, cw)]
    rows += [(line, body_font, 42, (90, 94, 106)) for line in _wrap(
        f"{date_str} · 国服官网补丁说明", body_font, cw)]
    rows += [(line, body_font, 40, (90, 94, 106)) for line in _wrap(
        f"原文：{source_url}", body_font, cw)]
    rows.append(("正文图片生成失败，请点击原文链接查看", body_font, 40, (200, 90, 44)))
    height = 56 + sum(r[2] for r in rows) + 48
    img = Image.new("RGB", (width, height), (244, 246, 250))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((18, 18, width - 18, height - 18), radius=24,
                        fill="white", outline=(224, 228, 236), width=2)
    y = 48
    for line, font, lh, fill in rows:
        d.text((margin, y), line, font=font, fill=fill)
        y += lh
    tmp = dest + ".tmp"
    img.save(tmp, "PNG")
    os.replace(tmp, dest)
    return dest


# ---------------- 抓取 → 去重 ----------------

async def _fetch_month(client: httpx.AsyncClient, year: int, month: int) -> list[dict]:
    url = PATCH_MONTH_URL.format(year=year, month=month)
    try:
        r = await client.get(url, timeout=_FETCH_TIMEOUT, headers={"User-Agent": _UA})
        if r.status_code == 404:
            return []  # 未来月份页不存在属正常
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return []
        raise
    # 站点固定 utf-8：不用 r.text 的嗅探编码，误判会导致标题乱码、正则匹配不到、
    # 整轮静默判空（empty 路径不告警），还查不出来
    return parse_month_page(r.content.decode("utf-8", errors="replace"), year, month, url)


async def _collect_latest() -> list[dict]:
    """抓目标月份页并按日期排序；调用方负责去重比对。"""
    now = datetime.now(_SH)
    client = _get_http_client()
    sections: list[dict] = []
    for year, month in _target_months(now):
        try:
            sections.extend(await _fetch_month(client, year, month))
        except Exception:
            _logger.warning("ow补丁抓取 %d-%02d 失败", year, month, exc_info=True)
    sections.sort(key=lambda s: (s["date"], s["key"]))
    return sections


def _diff_new(sections: list[dict], seen: dict) -> list[dict]:
    """未见过、或标题相同但正文哈希变了的都算新。"""
    news = []
    for s in sections:
        old = seen.get(s["key"])
        if old is None or (isinstance(old, dict) and old.get("hash") != s["hash"]):
            news.append(s)
    return news


# 官方常见写法：热补丁（在线修正）单独成条，同时改到旧补丁页正文。
# 插件若把两类都当“新”推，会出现热补丁 + 老补丁连发。
_HOTFIX_RE = re.compile(r"在线修正|热修复|热补丁|紧急修复|紧急更新|hotfix", re.I)


def _is_hotfix(patch: dict) -> bool:
    """标题 / 首节 h4 / 导语含热修措辞 → 判定为热补丁条目。"""
    title = patch.get("title") or ""
    if _HOTFIX_RE.search(title):
        return True
    body = patch.get("body_html") or ""
    m = re.search(r"<h4[^>]*>(.*?)</h4>", body, re.I | re.S)
    if m:
        first_h4 = re.sub(r"<[^>]+>", "", m.group(1))
        if _HOTFIX_RE.search(first_h4):
            return True
    lead = re.sub(r"<[^>]+>", " ", body[:600])
    return bool(_HOTFIX_RE.search(lead))


def _select_push_list(fresh: list[dict], seen: dict) -> list[dict]:
    """同轮多条待推时筛选，避免「热补丁 + 老补丁」连发。

    - 混有热补丁：只推热补丁（旧页哈希变更、更老补丁静默记已读）；
    - 无热修标记但新 key 与旧页哈希变更同轮出现：只推全新 key；
    - 其余情况保持原行为，整段 fresh 交给推送方。
    """
    if len(fresh) <= 1:
        return list(fresh)
    new_keys = [p for p in fresh if seen.get(p["key"]) is None]
    hash_changed = [p for p in fresh if seen.get(p["key"]) is not None]
    hotfixes = [p for p in fresh if _is_hotfix(p)]
    if hotfixes and len(hotfixes) < len(fresh):
        return hotfixes
    if new_keys and hash_changed:
        return new_keys
    return list(fresh)


# ---------------- 推送 ----------------

_push_running = False


async def _send_images(bot: Bot, groups: list[str], paths: list[str],
                      force: bool = False) -> list[str]:
    """逐群顺序发多图保序，群之间并发；返回送达的群 ID 列表。

    force=True 时跳过中继群过滤（仅手动测试命令用，自动推送永远不过滤失效）。
    """

    async def _one(gid: str) -> str | None:
        try:
            for i, path in enumerate(paths):
                await bot.send_group_msg(
                    group_id=int(gid), message=MessageSegment.image("file://" + path))
                if i < len(paths) - 1:
                    await asyncio.sleep(1)
            return gid
        except Exception:
            _logger.warning("ow补丁发送到群 %s 失败", gid, exc_info=True)
            return None

    targets = list(groups) if force else [g for g in groups if g not in _RELAY_SKIP]
    if len(targets) != len(groups):
        _logger.info("ow补丁跳过中继群推送")
    results = await asyncio.gather(*(_one(g) for g in targets))
    return [gid for gid in results if gid]


async def _push_patch(bot: Bot, groups: list[str], patch: dict, attempts: int,
                      force: bool = False) -> bool:
    """单个补丁：长截图(重试)→分块→标题卡；有群送达返回 True。"""
    client = _get_http_client()
    try:
        data_uris = await _download_images(client, patch["images"])
    except Exception:
        _logger.warning("ow补丁官图下载失败，降级纯文字版", exc_info=True)
        data_uris = []
    paths: list[str] = []
    try:
        try:
            paths = await _render_long_image(patch, data_uris, attempts=attempts)
        except _TooBig as e:
            _logger.warning("ow补丁超限走分块：%s", e)
            paths = await _render_chunked_images(patch, data_uris)
        except Exception:
            _logger.warning("ow补丁长图 %d 次均失败，走分块", attempts, exc_info=True)
            try:
                paths = await _render_chunked_images(patch, data_uris)
            except Exception:
                _logger.warning("ow补丁分块失败，走标题卡", exc_info=True)
                digest = hashlib.sha256(
                    (patch["key"] + patch["hash"]).encode()).hexdigest()[:12]
                paths = [await asyncio.to_thread(
                    _render_title_card, patch["title"], patch["date"], patch["source"], digest)]
    except Exception:
        _logger.exception("ow补丁渲染链路异常")
        return False
    if not groups:
        return False
    sent = await _send_images(bot, groups, paths, force=force)
    if not sent:
        return False
    if _SEND_SOURCE_LINK:
        # 官网直链：QQ 会展开成官方链接卡片（标题+摘要+头图），算“官方搬运”的一部分
        for gid in sent:
            try:
                await bot.send_group_msg(
                    group_id=int(gid), message=MessageSegment.text(patch["source"]))
            except Exception:
                _logger.warning("ow补丁原文链接发送到群 %s 失败", gid, exc_info=True)
    return True


def _mark_seen(patch: dict) -> None:
    def _do(state: dict) -> None:
        seen = state.setdefault("seen", {})
        seen[patch["key"]] = {
            "hash": patch["hash"], "title": patch["title"],
            "date": patch["date"], "source": patch["source"],
        }
        # 状态瘦身：只保留最近 60 条，防止无限增长
        if len(seen) > 60:
            for k in sorted(seen)[:-60]:
                seen.pop(k, None)
    _mutate_state(_do)


async def _notify_owner(text: str) -> None:
    if not OWNER.isdigit():
        return
    try:
        bot = get_bot()
        await bot.send_private_msg(
            user_id=int(OWNER), message=MessageSegment.text(text))
    except Exception:
        _logger.warning("ow补丁主人通知发送失败", exc_info=True)


async def _poll_once() -> str:
    """一轮检查：返回 spent|empty|skipped-lowmem|failed 供日志与测试断言。"""
    global _push_running
    if _push_running:
        return "skipped-running"
    _push_running = True
    try:
        ok, reason = await asyncio.to_thread(_mem_ok)
        if not ok:
            _logger.warning("ow补丁本轮跳过：%s", reason)

            def _do(state: dict) -> None:
                state["lowmem_skips"] = int(state.get("lowmem_skips") or 0) + 1
            st = _mutate_state(_do)
            if st.get("lowmem_skips") == 3:
                await _notify_owner(f"⚠️ ow补丁因内存不足已连续跳过 3 轮（{reason}），请检查服务器内存")
            return "skipped-lowmem"
        try:
            sections = await asyncio.wait_for(_collect_latest(), _JOB_DEADLINE)
        except asyncio.TimeoutError:
            _logger.warning("ow补丁抓取超 %ds 死线", _JOB_DEADLINE)
            return await _note_fail()
        with _LOCK:
            seen = dict(_load_state().get("seen") or {})
        fresh = _diff_new(sections, seen)
        _mutate_state(lambda s: s.update(
            last_check=datetime.now(_SH).strftime("%Y-%m-%d %H:%M:%S")))
        if not fresh:
            _mutate_state(lambda s: s.update(consec_fail=0, lowmem_skips=0))
            return "empty"
        try:
            bot = get_bot()
        except Exception:
            _logger.warning("ow补丁拿不到 Bot 实例")
            return await _note_fail()
        groups = [str(g) for g in (_load_state().get("groups") or []) if str(g)]
        if not groups:
            for p in fresh:
                _mark_seen(p)  # 无订阅群：只记已读，避免攒一堆待推
            return "empty"
        selected = _select_push_list(fresh, seen)
        selected_keys = {p["key"] for p in selected}
        if len(selected) != len(fresh):
            skipped = [p["title"] for p in fresh if p["key"] not in selected_keys]
            _logger.info(
                "ow补丁同轮只推 %d/%d 条（筛掉老补丁/非热修）：%s | 跳过：%s",
                len(selected), len(fresh),
                "；".join(p["title"] for p in selected) or "-",
                "；".join(skipped) or "-",
            )
            for p in fresh:
                if p["key"] not in selected_keys:
                    _mark_seen(p)  # 静默记已读，避免下轮再入选
        ok_all = True
        for patch in selected[-3:]:  # 单轮最多推 3 节，防积压一次性刷屏
            if not await _push_patch(bot, groups, patch, _RENDER_ATTEMPTS):
                ok_all = False
                continue
            _mark_seen(patch)
        if ok_all:
            _mutate_state(lambda s: s.update(consec_fail=0, lowmem_skips=0))
            return "spent"
        return await _note_fail()
    finally:
        _push_running = False


async def _note_fail() -> str:
    def _do(state: dict) -> None:
        state["consec_fail"] = int(state.get("consec_fail") or 0) + 1
    st = _mutate_state(_do)
    if st.get("consec_fail") == 3:
        await _notify_owner("⚠️ ow补丁连续 3 轮推送失败，请检查官网连通性与渲染管线")
    return "failed"


@scheduler.scheduled_job("interval", hours=1, id="ow_patch_poll",
                          timezone="Asia/Shanghai", max_instances=1,
                          coalesce=True, misfire_grace_time=900)
async def ow_patch_job():
    await _poll_once()


# 启动补发：内存 jobstore 重启丢计划，连上后若距上次检查超 30 分钟就跑一次
_register_catchup = getattr(get_driver(), "on_bot_connect", get_driver().on_startup)


@_register_catchup
async def _ow_patch_catchup(bot: Bot | None = None) -> None:
    try:
        with _LOCK:
            last = str(_load_state().get("last_check") or "")
        if last:
            try:
                dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_SH)
                if (datetime.now(_SH) - dt).total_seconds() < 30 * 60:
                    return
            except ValueError:
                pass
        await _poll_once()
    except Exception:
        _logger.exception("ow补丁启动补发失败")


@sub_on_cmd.handle()
async def _on(event: MessageEvent):
    if not is_owner(event):
        await sub_on_cmd.finish("❌ 你没有权限使用此功能")
    if not isinstance(event, GroupMessageEvent):
        await sub_on_cmd.finish("请在要订阅的群里发送此命令")
    if str(event.group_id) in _RELAY_SKIP:
        await sub_on_cmd.finish("❌ 任务中继群不推送补丁（防刷屏），请换目标群订阅")

    def _do(state: dict) -> None:
        groups = {str(g) for g in (state.get("groups") or [])}
        groups.add(str(event.group_id))
        state["groups"] = sorted(groups)
    _mutate_state(_do)
    await sub_on_cmd.finish("✅ 本群已订阅OW国服补丁推送\n新补丁说明将以长截图形式自动发送（每小时检查一次）")


@sub_off_cmd.handle()
async def _off(event: MessageEvent):
    if not is_owner(event):
        await sub_off_cmd.finish("❌ 你没有权限使用此功能")
    if not isinstance(event, GroupMessageEvent):
        await sub_off_cmd.finish("请在要退订的群里发送此命令")

    def _do(state: dict) -> None:
        groups = {str(g) for g in (state.get("groups") or [])}
        groups.discard(str(event.group_id))
        state["groups"] = sorted(groups)
    _mutate_state(_do)
    await sub_off_cmd.finish("✅ 本群已退订OW国服补丁推送")


@sub_status_cmd.handle()
async def _status(event: MessageEvent):
    if not is_owner(event):
        await sub_status_cmd.finish("❌ 你没有权限使用此功能")
    with _LOCK:
        state = _load_state()
    groups = [str(g) for g in (state.get("groups") or [])]
    seen = state.get("seen") or {}
    last = str(state.get("last_check") or "从未")
    latest = ""
    if seen:
        k = sorted(seen)[-1]
        latest = f"\n最新已知：{seen[k].get('title', k)}"
    await sub_status_cmd.finish(
        f"🛡️ OW国服补丁推送\n订阅群：{'、'.join(groups) if groups else '无'}"
        f"\n上次检查：{last}{latest}")


@sub_test_cmd.handle()
async def _test(event: MessageEvent):
    if not is_owner(event):
        await sub_test_cmd.finish("❌ 你没有权限使用此功能")
    if not isinstance(event, GroupMessageEvent):
        await sub_test_cmd.finish("请在群里使用此命令")
    try:
        sections = await asyncio.wait_for(_collect_latest(), _JOB_DEADLINE)
    except Exception:
        await sub_test_cmd.finish("补丁页抓取失败，请稍后再试")
        return
    if not sections:
        await sub_test_cmd.finish("当前月份暂无补丁说明")
        return
    patch = sections[-1]
    try:
        bot = get_bot()
    except Exception:
        await sub_test_cmd.finish("拿不到 Bot 实例，请稍后再试")
        return
    # 先回一句，避免 20~60 秒渲染期间用户以为没反应重复刷命令
    try:
        await sub_test_cmd.send("🛠️ 正在抓取并生成补丁长图，请稍候…")
    except Exception:
        pass
    # 手动测试是明确的人工指令，不受中继群过滤限制；自动推送仍跳过中继群
    ok = await _push_patch(bot, [str(event.group_id)], patch, attempts=1, force=True)
    if not ok:
        await sub_test_cmd.finish("测试推送失败，详情见日志/主人私聊告警")
        return
    # 测试只发图不记已读：正式推送不受影响
