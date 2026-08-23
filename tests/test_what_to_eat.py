import asyncio
import random

from conftest import GroupMessageEvent

from helpers import load_plugin

wte = load_plugin("what_to_eat")


def test_hit_matches_question_variants():
    for text in ("今天吃什么", "晚上吃啥?", "中午到底吃什么啊", "  明天吃啥  "):
        assert wte.hit(text)


def test_hit_ignores_plain_statements_and_commands():
    for text in ("吃饭了", "吃了吗", "我想吃火锅", "吃点好的", "", ".吃什么"):
        assert not wte.hit(text)


def test_food_pool_has_50_unique_items():
    assert len(wte.FOODS) == 50
    assert len(set(wte.FOODS)) == 50
    for f in wte.FOODS:
        assert isinstance(f, str) and f.strip()
        assert f == f.strip()  # 纯文字，无前缀空白/图标


def test_allow_cooldown_window():
    assert wte.allow(123, now=100.0)
    wte._last_reply[123] = 100.0
    assert not wte.allow(123, now=109.9)
    assert wte.allow(123, now=110.0)


def test_pick_returns_food_from_pool():
    random.seed(42)
    for _ in range(50):
        assert wte.pick() in wte.FOODS


def test_pick_is_random_enough():
    random.seed(42)
    assert len({wte.pick() for _ in range(300)}) > 20


def test_handler_replies_and_respects_cooldown():
    handler = wte.eater.handlers[0]
    wte._last_reply.pop(65535, None)
    wte.eater.sent.clear()

    ev = GroupMessageEvent(plain="今天吃什么", user_id=10001, group_id=65535, message=[])
    asyncio.run(handler(ev))
    assert 65535 in wte._last_reply
    # 实际发送的内容以"吃"开头，且推荐结果来自 FOODS（而非只看内部状态）
    assert len(wte.eater.sent) == 1
    sent = str(wte.eater.sent[0])
    assert sent.startswith("吃")
    assert sent[1:] in wte.FOODS

    # 冷却期内同群再问：不再发送
    asyncio.run(handler(GroupMessageEvent(plain="晚上吃啥", user_id=10002, group_id=65535, message=[])))
    assert len(wte.eater.sent) == 1

    # 未命中文本的群不回复
    miss = GroupMessageEvent(plain="吃了吗", user_id=10001, group_id=65536, message=[])
    asyncio.run(handler(miss))
    assert 65536 not in wte._last_reply
    assert len(wte.eater.sent) == 1
