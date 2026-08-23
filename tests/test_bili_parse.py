import asyncio

import pytest

from conftest import GroupMessageEvent
from helpers import load_plugin

bili = load_plugin("bili_parse")

_INFO = {
    "bvid": "BV1GJ411x7h7",
    "title": "【官方 MV】Never Gonna Give You Up",
    "pic": "https://i0.hdslb.com/bfs/archive/abc.jpg",
    "owner": "RickAstleyVEVO",
    "tname": "音乐综合",
    "videos": 1,
    "desc": "1987年经典单曲 Official MV",
    "view": 12345678,
    "danmaku": 40210,
    "like": 999999,
    "coin": 8800,
    "favorite": 56000,
    "reply": 4300,
    "share": 12345,
    "duration": 213,
    "pubdate": 1577835803,
}


@pytest.fixture(autouse=True)
def _reset():
    bili._group_last.clear()
    bili._recent.clear()
    bili._info_cache.clear()
    bili.bili_matcher.sent.clear()
    yield


def test_extract_ids_from_plain_and_link():
    assert bili.extract_ids("看这个 BV1GJ411x7h7 好笑") == ["BV1GJ411x7h7"]
    assert bili.extract_ids("https://www.bilibili.com/video/BV1GJ411x7h7?p=1") == ["BV1GJ411x7h7"]
    assert bili.extract_ids("av170001 经典") == ["av170001"]
    assert bili.extract_ids("AV170001 大写") == ["av170001"]


def test_extract_ids_dedupes_and_ignores_false_positives():
    assert bili.extract_ids("BV1GJ411x7h7 和 BV1GJ411x7h7") == ["BV1GJ411x7h7"]
    for text in ("导航avx", "hav1234", "观察1av2345", "普通聊天没有编号", ""):
        assert bili.extract_ids(text) == []


def test_fmt_helpers():
    assert bili._fmt_count(9999) == "9999"
    assert bili._fmt_count(12345) == "1.2万"
    assert bili._fmt_duration(213) == "3:33"
    assert bili._fmt_duration(3725) == "1:02:05"


def test_build_card_contains_info_and_image():
    msg = bili.build_card(_INFO)
    segs = list(msg)
    assert segs[0].type == "image" and segs[0].data["file"] == _INFO["pic"]
    text = str(segs[1])
    assert _INFO["title"] in text
    assert _INFO["owner"] in text
    assert _INFO["tname"] in text          # 分区
    assert "1.2万" in text                  # 分享数
    assert "1234.6万" in text               # 播放数
    assert "3:33" in text                   # 时长
    assert "2020-01-01" in text             # 发布日期（Asia/Shanghai）
    assert _INFO["desc"] in text            # 简介
    assert "全2P" not in text               # 单P不显示多P标记


def test_build_card_multi_part_and_long_desc():
    multi = dict(_INFO, videos=3, desc="第一行\n第二行 " + "很长的简介" * 30)
    text = str(list(bili.build_card(multi))[1])
    assert "（全3P）" in text
    desc_line = text.split("简介：")[1].split("\n")[0]
    assert "很长的简介" in desc_line and "…" in desc_line  # 压单行 + 截断
    assert len(desc_line) <= 70

    no_desc = dict(_INFO, desc="", tname="")
    text2 = str(list(bili.build_card(no_desc))[1])
    assert "简介" not in text2 and "｜" not in text2  # 空简介/空分区整行省略


def _ev(text, group=100):
    return GroupMessageEvent(plain=text, user_id=1, group_id=group, message=[])


def test_handler_sends_card(monkeypatch):
    calls = []

    async def fake_fetch(vid):
        calls.append(vid)
        return dict(_INFO)

    monkeypatch.setattr(bili, "fetch_info", fake_fetch)
    asyncio.run(bili.bili_matcher.handlers[0](_ev("看下 BV1GJ411x7h7")))
    assert calls == ["BV1GJ411x7h7"]
    assert len(bili.bili_matcher.sent) == 1
    sent = bili.bili_matcher.sent[0]
    assert list(sent)[0].type == "image"


def test_handler_skips_commands(monkeypatch):
    called = []
    monkeypatch.setattr(bili, "fetch_info", lambda vid: called.append(vid))
    asyncio.run(bili.bili_matcher.handlers[0](_ev(".战报 BV1GJ411x7h7")))
    asyncio.run(bili.bili_matcher.handlers[0](_ev("/bv BV1GJ411x7h7")))
    assert called == []
    assert not bili.bili_matcher.sent


def test_handler_dedupes_same_video_in_group(monkeypatch):
    async def fake_fetch(vid):
        return dict(_INFO)

    monkeypatch.setattr(bili, "fetch_info", fake_fetch)
    handler = bili.bili_matcher.handlers[0]
    asyncio.run(handler(_ev("BV1GJ411x7h7")))
    # 同视频在去重窗口内：不重复发送（未来时间戳模拟"刚发过"）
    bili._group_last.clear()  # 排除群冷却因素，单测去重
    bili._recent[("100", "BV1GJ411x7h7")] = 1e12
    asyncio.run(handler(_ev("再看 BV1GJ411x7h7")))
    assert len(bili.bili_matcher.sent) == 1


def test_handler_group_cooldown_blocks_other_video(monkeypatch):
    async def fake_fetch(vid):
        return dict(_INFO)

    monkeypatch.setattr(bili, "fetch_info", fake_fetch)
    handler = bili.bili_matcher.handlers[0]
    asyncio.run(handler(_ev("BV1GJ411x7h7")))
    bili._recent.clear()  # 排除去重因素，单测群冷却
    asyncio.run(handler(_ev("BV1fsk1v7pvX")))  # 不同视频，但同群 30 秒内
    assert len(bili.bili_matcher.sent) == 1
    bili._group_last.clear()  # 冷却过后恢复
    asyncio.run(handler(_ev("BV1fsk1v7pvX")))
    assert len(bili.bili_matcher.sent) == 2


def test_handler_ignores_plain_text_without_ids(monkeypatch):
    called = []
    monkeypatch.setattr(bili, "fetch_info", lambda vid: called.append(vid))
    asyncio.run(bili.bili_matcher.handlers[0](_ev("今天天气不错")))
    assert called == []


def test_handler_fetch_failure_silent(monkeypatch):
    async def fake_fetch(vid):
        return None

    monkeypatch.setattr(bili, "fetch_info", fake_fetch)
    asyncio.run(bili.bili_matcher.handlers[0](_ev("BV1GJ411x7h7")))
    assert not bili.bili_matcher.sent
    assert not bili._group_last  # 失败不占用群冷却


def test_resolve_b23_extracts_from_final_url(monkeypatch):
    class _Resp:
        url = "https://www.bilibili.com/video/BV1GJ411x7h7/?spm=x"

    class _Client:
        async def get(self, url, **kw):
            assert "b23.tv" in url
            return _Resp()

    monkeypatch.setattr(bili, "get_http_client", lambda t: _Client())
    assert asyncio.run(bili.resolve_b23("https://b23.tv/abc123")) == "BV1GJ411x7h7"
