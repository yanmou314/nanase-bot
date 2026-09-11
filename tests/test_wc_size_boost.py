import os
from collections import Counter

import pytest
from helpers import load_plugin

load_plugin("chat_stats")
from plugins.chat_stats import wordcloud_card as wc


def _pixel_coverage(path):
    from PIL import Image
    img = Image.open(path).convert("L")
    w, h = img.size
    dark = sum(1 for v in img.getdata() if v < 250)
    return dark / (w * h)


def test_size_boost_boundaries():
    assert wc._size_boost(0) == 1.0
    assert wc._size_boost(60) == 1.0
    assert wc._size_boost(200) == 1.0


def test_size_boost_small_groups_scaled():
    b21 = wc._size_boost(21)
    assert 1.9 < b21 <= 2.0
    assert wc._size_boost(10) >= b21 >= wc._size_boost(30) > wc._size_boost(59) > 1.0


@pytest.mark.skipif(
    not any(
        os.path.isfile(p)
        for p in (
            "/usr/share/fonts/custom/ZCOOLKuaiLe-Regular.ttf",
            "/usr/share/fonts/custom/ZCOOLQingKeHuangYou-Regular.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        )
    ),
    reason="CJK 字体未安装（CI runner 无生产字体，像素覆盖率断言无意义）",
)
def test_render_small_counter_covers_panel(monkeypatch, tmp_path):
    monkeypatch.setattr(wc, "CACHE_DIR", str(tmp_path))
    c = Counter({
        "快点": 2, "嘎达": 1, "搞搞": 1, "数字": 1, "小团体": 1,
        "说实话": 1, "没想到": 1, "气球": 1, "合作": 1, "游戏": 1,
        "完成": 1, "感觉": 1, "冰冰": 1, "睡觉": 1, "上班": 1,
        "中考": 1, "有人": 1, "这么": 1, "五个": 1, "小时": 1,
        "医学生": 1,
    })
    assert len(c) == 21
    boosted = _pixel_coverage(wc._render(c, min(40, len(c)), 16, None))
    monkeypatch.setattr(wc, "_size_boost", lambda n: 1.0)
    plain = _pixel_coverage(wc._render(c, min(40, len(c)), 16, None))
    assert boosted > plain * 1.5
    assert boosted > 0.12
