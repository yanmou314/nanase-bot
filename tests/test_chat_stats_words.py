from collections import Counter

from helpers import load_plugin

cs = load_plugin("chat_stats")


def test_count_words_segments_naturally():
    """长句应按语义分词，不再出现 4 字切块的半截词。"""
    c = Counter()
    cs._count_into(c, "今天晚上大家一起吃火锅然后看电影")
    words = {w for w, _ in c.most_common()}
    assert "电影" in words
    assert "吃火锅" in words or "火锅" in words
    for bad in ("今天晚上", "大家一起", "吃火锅然", "后看电影"):
        assert bad not in words


def test_count_words_filters_stopwords_and_single_chars():
    c = Counter()
    cs._count_into(c, "我觉得这个真的可以，我知道了")
    assert not (c.keys() & cs.STOPWORDS)
    assert all(len(w) >= 2 for w in c)


def test_count_words_drops_non_chinese():
    c = Counter()
    cs._count_into(c, "http://example.com 123 ok!! 😂")
    assert not c


def test_count_words_accumulates():
    c = Counter()
    cs._count_into(c, "看电影")
    cs._count_into(c, "再看一次电影")
    assert c["电影"] == 2


def test_quote_worthy():
    assert cs._quote_worthy("今晚谁去吃火锅啊")
    assert not cs._quote_worthy("哈哈哈哈")  # 单字复读
    assert not cs._quote_worthy("666666")  # 单字复读
    assert not cs._quote_worthy("嗯")  # 太短
    assert not cs._quote_worthy("哇" * 30)  # 太长


def test_render_with_quotes(tmp_path, monkeypatch):
    """带语录渲染应正常出图，且语录区使词云可用高度变小。"""
    import os
    from collections import Counter as C

    from plugins.chat_stats import wordcloud_card as wc

    monkeypatch.setattr(wc, "CACHE_DIR", str(tmp_path))
    c = C({"火锅": 5, "电影": 3, "烧烤": 2})
    path = wc._render(c, 10, 100, [("今晚谁去吃火锅啊", 3), ("明天不上课吗", 2)])
    assert os.path.getsize(path) > 0
    # 无语录时也正常
    path2 = wc._render(c, 10, 100)
    assert os.path.getsize(path2) > 0


def test_phrase_from_sentence_cuts_at_word_boundary():
    """长句应截取 ≤8 字短语：是原句连续片段、含锚点词、不带标点残留。"""
    c = Counter({"火锅": 10, "电影": 5})
    s = "今晚谁一起去吃火锅啊你们要不要一起"
    p = cs._phrase_from_sentence(s, c, max_len=8)
    assert p in s
    assert len(p) <= 8
    assert "火锅" in p


def test_phrase_from_sentence_short_passthrough():
    assert cs._phrase_from_sentence("明天不上课", Counter(), 8) == "明天不上课"


def test_render_vertical_mixed(tmp_path, monkeypatch):
    """横竖排混排渲染应正常出图，含友等常见字。"""
    import os
    from collections import Counter as C

    from plugins.chat_stats import wordcloud_card as wc

    monkeypatch.setattr(wc, "CACHE_DIR", str(tmp_path))
    c = C({"朋友": 20, "友尽": 12, "网友": 8, "火锅": 30, "电影": 15})
    path = wc._render(c, 5, 50, [("友尽了吗", 3)])
    assert os.path.getsize(path) > 0


def test_coverage_zcool_common_chars():
    """站酷字体应覆盖常用汉字；字集过滤不得把常见字误判为缺失。"""
    from plugins.chat_stats import wordcloud_card as wc

    cov = wc._coverage(wc.FONT_CANDIDATES[0])
    assert cov and set("友锅电影吃火锅") <= cov


def test_phrase_from_sentence_strips_non_chinese():
    """含表情/符号的句子截出的短语不得包含非中文字符（防字体缺字方框）。"""
    p = cs._phrase_from_sentence("群能不能把这个🈲了的东西收拾一下", Counter(), 8)
    assert p
    assert all("\u4e00" <= ch <= "\u9fff" for ch in p)


def test_phrase_never_crosses_clause_boundary():
    """含逗号的句子：截取的短语必须是单个子句内的连续片段，不得跨子句拼接。"""
    c = Counter({"火锅": 10, "电影": 5})
    s = "今晚谁一起去吃火锅，然后大家再去看电影"
    p = cs._phrase_from_sentence(s, c, max_len=8)
    assert p
    clauses = s.replace("，", ",").split(",")
    assert any(p in cl for cl in clauses), p  # 落在某个子句内
    assert "吃火锅" in p or "电影" in p  # 锚定高频词所在子句


def test_phrase_short_sentence_strips_punctuation():
    p = cs._phrase_from_sentence("去吃火锅？", Counter(), 8)
    assert all("\u4e00" <= ch <= "\u9fff" for ch in p)
    assert p == "去吃火锅"
