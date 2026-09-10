"""ow_patch 单测：解析/去重/切块纯逻辑（渲染与网络不进单测）。"""
from datetime import datetime

from helpers import load_plugin

ow = load_plugin("ow_patch")

_FIXTURE = """
<div class="PatchNotes-list"><div class="PatchNotes-body">
<div class="PatchNotes-patch PatchNotes-live">
<h3 class="PatchNotes-patchTitle">《守望先锋》补丁说明——2026年5月27日</h3>
<div class="PatchNotes-section PatchNotes-section-generic_update">
<h4 class="PatchNotes-sectionTitle">错误修复</h4>
<div class="PatchNotesGeneralUpdate-description">
<ul><li>修复了商城问题。</li></ul>
</div></div></div>
<div class="PatchNotes-patch PatchNotes-live">
<h3 class="PatchNotes-patchTitle">《守望先锋》补丁说明——2026年5月22日</h3>
<div class="PatchNotes-section PatchNotes-section-hero_update">
<h4 class="PatchNotes-sectionTitle">角斗领域更新</h4>
<div class="PatchNotesHeroUpdate-header"><img class="PatchNotesHeroUpdate-icon" src="https://ld5.res.netease.com/images/a.png">
<div class="PatchNotesHeroUpdate-name">飞天猫</div></div>
<ul><li>伤害从100提高至120。</li></ul>
<img class="PatchNotesAbilityUpdate-icon" src="https://ld5.res.netease.com/images/b.png">
<img src="https://evil.example.com/x.png">
</div></div>
<div class="PatchNotes-patch PatchNotes-live">
<h3 class="PatchNotes-patchTitle">不是补丁的块</h3>
<p>无日期标题，应被跳过。</p>
</div>
</div></div>
<script>var x = 1;</script>
"""


def _parse():
    return ow.parse_month_page(_FIXTURE, 2026, 5, "https://ow.blizzard.cn/u")


def test_parse_two_patches_skip_undated():
    sections = _parse()
    assert len(sections) == 2
    assert sections[0]["title"] == "《守望先锋》补丁说明——2026年5月27日"
    assert sections[0]["date"] == "2026-05-27"
    assert sections[1]["date"] == "2026-05-22"
    assert sections[0]["key"].startswith("2026-05|")
    assert sections[0]["hash"] and sections[0]["hash"] != sections[1]["hash"]


def test_parse_images_allowlist():
    sections = _parse()
    assert sections[1]["images"] == [
        "https://ld5.res.netease.com/images/a.png",
        "https://ld5.res.netease.com/images/b.png",
    ]
    assert sections[1]["img_kinds"] == ["hero", "ability"]
    assert "evil.example.com" not in sections[1]["body_html"]
    assert 'data-owimg="0"' in sections[1]["body_html"]
    assert 'data-owimg="1"' in sections[1]["body_html"]


def test_fill_images_restores_kind_class():
    sections = _parse()
    uris = ["data:hero", "data:ability"]
    out = ow._fill_images(sections[1]["body_html"], uris, sections[1]["img_kinds"])
    assert '<img class="ow-hero" src="data:hero">' in out
    assert '<img class="ow-ability" src="data:ability">' in out
    # 下载失败的占位直接删掉
    out2 = ow._fill_images(sections[1]["body_html"], [None, None], sections[1]["img_kinds"])
    assert "data-owimg" not in out2 and "<img" not in out2


def test_parse_keeps_structure_and_escapes():
    sections = _parse()
    assert "<h4" in sections[0]["body_html"] and "<li>" in sections[0]["body_html"]
    assert "<script" not in sections[0]["body_html"]


def test_diff_new_and_hash_change():
    sections = _parse()
    assert len(ow._diff_new(sections, {})) == 2
    seen = {s["key"]: {"hash": s["hash"]} for s in sections}
    assert ow._diff_new(sections, seen) == []
    seen[sections[0]["key"]]["hash"] = "deadbeef"
    assert [s["key"] for s in ow._diff_new(sections, seen)] == [sections[0]["key"]]


def test_target_months_rollover():
    assert ow._target_months(datetime(2026, 9, 9)) == [(2026, 9)]
    assert ow._target_months(datetime(2026, 9, 2)) == [(2026, 9), (2026, 8)]
    assert ow._target_months(datetime(2026, 1, 1)) == [(2026, 1), (2025, 12)]


def test_split_sections():
    body = "<p>导语</p><h4>A</h4><p>a</p><h4>B</h4><p>b</p>"
    chunks = ow._split_sections(body)
    assert len(chunks) == 3
    assert ow._split_sections("<p>只有一段</p>") == ["<p>只有一段</p>"]


def test_build_patch_html_has_title_and_source():
    html = ow.build_patch_html("标题T", "2026-05-27", "https://ow.blizzard.cn/u", "<p>x</p>")
    assert "标题T" in html and "https://ow.blizzard.cn/u" in html and "<p>x</p>" in html


def test_mem_ok_shape():
    ok, reason = ow._mem_ok()
    assert isinstance(ok, bool) and isinstance(reason, str)


def test_trim_trailing_blank():
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (200, 1000), "white")
    d = ImageDraw.Draw(im)
    d.rectangle((10, 10, 100, 300), fill="black")
    cut = ow._trim_trailing_blank(im)
    assert cut is not None and cut.height < 500 and cut.height >= 300


def test_trim_full_blank_page_returns_none():
    from PIL import Image

    assert ow._trim_trailing_blank(Image.new("RGB", (200, 1000), "white")) is None


def test_trim_content_to_bottom_keeps_page():
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (200, 1000), "white")
    d = ImageDraw.Draw(im)
    d.rectangle((0, 900, 199, 990), fill="black")
    cut = ow._trim_trailing_blank(im)
    assert cut is not None and cut.height == 1000


def test_trim_near_white_bg_counts_as_blank():
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (200, 1000), "white")
    d = ImageDraw.Draw(im)
    d.rectangle((0, 100, 199, 200), fill="black")  # 顶部有点内容
    d.rectangle((0, 300, 199, 999), fill=(241, 242, 246))  # 正文底色大块空白
    cut = ow._trim_trailing_blank(im)
    assert cut is not None and cut.height < 400


class _FakeBot:
    """只实现 send_group_msg 的假 Bot（conftest 的 Bot stub 只有私聊接口）。"""

    def __init__(self):
        self.sent = []

    async def send_group_msg(self, group_id=None, message=None, **kwargs):
        self.sent.append((group_id, message))
        return {"message_id": 1}


def test_send_images_skips_relay_group():
    import asyncio

    bot = _FakeBot()
    relay = sorted(ow._RELAY_SKIP)[0]
    sent = asyncio.run(ow._send_images(bot, [relay, "12345"], ["p.png"]))
    assert sent == ["12345"]
    assert [g for g, _ in bot.sent] == [12345]


def test_send_images_force_bypasses_relay_skip():
    import asyncio

    bot = _FakeBot()
    relay = sorted(ow._RELAY_SKIP)[0]
    sent = asyncio.run(ow._send_images(bot, [relay], ["p.png"], force=True))
    assert sent == [relay]
    assert [g for g, _ in bot.sent] == [int(relay)]


def _band_image():
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (200, 1000), "white")
    d = ImageDraw.Draw(im)
    d.rectangle((10, 50, 190, 150), fill="black")  # 头部内容
    # 中间 500px 纯白分页空白
    d.rectangle((10, 800, 190, 900), fill="black")  # 尾部内容
    return im


def test_collapse_blank_bands():
    im = _band_image()
    out = ow._collapse_blank_bands(im)
    # 中间空白 151..799（649px）→48px，尾部 901..999（99px）→48px：199 + 149 = 348
    assert out.height == 348


def test_collapse_keeps_small_gaps():
    from PIL import Image, ImageDraw

    # 149px 级中等空白统一压到 48px：199 + 149*3 = 646
    im = Image.new("RGB", (200, 1000), "white")
    d = ImageDraw.Draw(im)
    d.rectangle((10, 50, 190, 150), fill="black")
    d.rectangle((10, 300, 190, 400), fill="black")
    d.rectangle((10, 550, 190, 650), fill="black")
    d.rectangle((10, 800, 190, 900), fill="black")
    out = ow._collapse_blank_bands(im)
    assert out.height == 646


def test_collapse_ignores_tiny_gaps():
    from PIL import Image, ImageDraw

    # 59px 微间距低于阈值，原样保留
    im = Image.new("RGB", (200, 1000), "white")
    d = ImageDraw.Draw(im)
    d.rectangle((10, 50, 190, 150), fill="black")
    d.rectangle((10, 210, 190, 990), fill="black")
    out = ow._collapse_blank_bands(im)
    assert out.height == 1000


def test_row_blank():
    from PIL import Image

    im = Image.new("RGB", (120, 10), "white")
    px = im.load()
    assert ow._row_blank(px, 120, 5, 255, 255, 255) is True
    px[60, 5] = (35, 38, 47)  # 文字黑
    assert ow._row_blank(px, 120, 5, 255, 255, 255) is False
    # 正文底色算空白
    im2 = Image.new("RGB", (120, 10), (241, 242, 246))
    assert ow._row_blank(im2.load(), 120, 5, 255, 255, 255) is True
