import asyncio
import json

import pytest
import httpx
from conftest import MessageEvent

from helpers import load_plugin

auto_chat = load_plugin("auto_chat")


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


class _FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return _FakeResponse(self.payload)


def test_clean_msg_strips_cq_at():
    ev = MessageEvent(plain="[CQ:at,qq=1837204555] 你好呀")
    assert auto_chat._clean_msg(ev) == "你好呀"


def test_get_memory_maxlen():
    mod = auto_chat
    mem = mod._get_memory(("g1", "u1"))
    for i in range(30):
        mem.append({"role": "user", "content": str(i)})
    assert len(mem) == 20
    assert mem[-1]["content"] == "29"


def test_group_memory_key_is_shared_but_private_memory_isolated():
    mod = auto_chat
    assert mod._memory_key("123", "alice") == ("group", "123")
    assert mod._memory_key("123", "bob") == ("group", "123")
    assert mod._memory_key("0", "alice") == ("private", "alice")
    assert mod._memory_key("0", "bob") == ("private", "bob")


def test_ai_reply_uses_shared_group_memory(monkeypatch):
    mod = auto_chat
    calls = []

    async def fake_completion(messages, max_tokens=300, timeout=30):
        calls.append(messages)
        return "收到啦"

    monkeypatch.setattr(mod, "chat_completion", fake_completion)
    mod._memory.clear()
    mod._memory_last_seen.clear()

    asyncio.run(mod._ai_reply("key", "alice", "123", "第一句话"))
    asyncio.run(mod._ai_reply("key", "bob", "123", "第二句话"))

    assert any(m["content"] == "第一句话" for m in calls[1])
    assert ("group", "123") in mod._memory
    assert ("123", "alice") not in mod._memory


class _Sender:
    def __init__(self, card=None, nickname=None):
        self.card = card
        self.nickname = nickname


def test_sender_name_prefers_card_then_nickname_then_qq():
    mod = auto_chat
    ev = MessageEvent(plain="hi", user_id=123, sender=_Sender(card=" 小明 ", nickname="笨蛋明"))
    assert mod._sender_name(ev) == "小明"
    ev2 = MessageEvent(plain="hi", user_id=123, sender=_Sender(card="", nickname="笨蛋明"))
    assert mod._sender_name(ev2) == "笨蛋明"
    ev3 = MessageEvent(plain="hi", user_id=123, sender=_Sender())
    assert mod._sender_name(ev3) == "123"
    ev4 = MessageEvent(plain="hi", user_id=123)
    assert mod._sender_name(ev4) == "123"


def test_ai_reply_prefixes_sender_in_group_context(monkeypatch):
    mod = auto_chat
    calls = []

    async def fake_completion(messages, max_tokens=300, timeout=30):
        calls.append(messages)
        return "收到啦"

    monkeypatch.setattr(mod, "chat_completion", fake_completion)
    mod._memory.clear()
    mod._memory_last_seen.clear()

    asyncio.run(mod._ai_reply("key", "alice", "123", "第一句话", "小明"))
    asyncio.run(mod._ai_reply("key", "bob", "123", "第二句话", "小红"))

    # 发给 AI 的最新一条消息带发言人前缀
    assert calls[0][-1]["content"] == "小明: 第一句话"
    assert calls[1][-1]["content"] == "小红: 第二句话"
    # 历史上下文保留了不同人的发言，AI 能分清多说话人
    assert any(m["content"] == "小明: 第一句话" for m in calls[1])
    assert any(m["content"] == "小红: 第二句话" for m in mod._memory[("group", "123")])


def test_ai_reply_private_chat_has_no_prefix(monkeypatch):
    mod = auto_chat
    calls = []

    async def fake_completion(messages, max_tokens=300, timeout=30):
        calls.append(messages)
        return "嗯嗯"

    monkeypatch.setattr(mod, "chat_completion", fake_completion)
    mod._memory.clear()
    mod._memory_last_seen.clear()

    asyncio.run(mod._ai_reply("key", "alice", "0", "私聊内容", ""))

    assert calls[0][-1]["content"] == "私聊内容"


def test_ai_poke_reply_includes_sender_name(monkeypatch):
    mod = auto_chat
    calls = []

    async def fake_completion(messages, max_tokens=300, timeout=30):
        calls.append(messages)
        return "呀！"

    monkeypatch.setattr(mod, "chat_completion", fake_completion)
    mod._memory.clear()
    mod._memory_last_seen.clear()

    asyncio.run(mod._ai_poke_reply("key", "42", "0", "小明"))

    assert "小明" in calls[0][-1]["content"]
    mem = list(mod._memory[("private", "42")])
    assert any("小明" in m["content"] for m in mem)


def test_sender_name_by_id_fetch_and_fallback():
    class _Bot:
        async def get_group_member_info(self, group_id=None, user_id=None):
            return {"card": "群名片", "nickname": "昵称"}

        async def get_stranger_info(self, user_id=None):
            return {"card": "", "nickname": "陌生人昵称"}

    class _BadBot:
        async def get_stranger_info(self, user_id=None):
            raise RuntimeError("boom")

    mod = auto_chat
    assert asyncio.run(mod._sender_name_by_id(_Bot(), 1, 99)) == "群名片"
    assert asyncio.run(mod._sender_name_by_id(_Bot(), 1, 0)) == "陌生人昵称"
    assert asyncio.run(mod._sender_name_by_id(_BadBot(), 7, 0)) == "7"


def test_system_prompt_documents_multi_speaker_format():
    assert "昵称: 内容" in auto_chat.SYSTEM
    assert "不同昵称代表不同的群友" in auto_chat.SYSTEM


def test_get_memory_ttl_eviction():
    mod = auto_chat
    mod._get_memory(("g2", "u2"))  # 建立条目
    assert ("g2", "u2") in mod._memory
    mod._memory_last_seen[("g2", "u2")] -= mod._MEMORY_TTL + 10  # 置为过期
    mod._get_memory(("g3", "u3"))  # 触发 TTL 清理
    assert ("g2", "u2") not in mod._memory


@pytest.fixture(autouse=True)
def _restore_memory_state():
    yield
    auto_chat._memory.clear()
    auto_chat._memory_last_seen.clear()


def test_get_memory_capacity_eviction(monkeypatch):
    mod = auto_chat
    monkeypatch.setattr(mod, "_MEMORY_MAX_KEYS", 5)
    for i in range(5):
        mod._get_memory(("gx", f"u{i}"))
    assert len(mod._memory) == 5
    mod._get_memory(("gx", "new"))
    assert len(mod._memory) == 5
    assert ("gx", "new") in mod._memory
    assert ("gx", "u0") not in mod._memory  # 最久未访问的被淘汰


def test_load_key_missing_config(monkeypatch, tmp_path):
    mod = auto_chat
    monkeypatch.setattr(mod, "CFG_FILE", str(tmp_path / "nope.json"))
    monkeypatch.setattr(mod, "_cached_key_mtime", -1.0)
    assert mod._load_key() == ""


def test_load_key_reads_config(monkeypatch, tmp_path):
    mod = auto_chat
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"api_key": " sk-test "}), encoding="utf-8")
    monkeypatch.setattr(mod, "CFG_FILE", str(cfg))
    monkeypatch.setattr(mod, "_cached_key_mtime", -1.0)
    assert mod._load_key() == "sk-test"


def test_get_api_key_public_alias(monkeypatch, tmp_path):
    mod = auto_chat
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"api_key": "sk-pub"}), encoding="utf-8")
    monkeypatch.setattr(mod, "CFG_FILE", str(cfg))
    monkeypatch.setattr(mod, "_cached_key_mtime", -1.0)
    assert mod.get_api_key() == "sk-pub"


def test_chat_completion_no_key(monkeypatch, tmp_path):
    mod = auto_chat
    monkeypatch.setattr(mod, "CFG_FILE", str(tmp_path / "nope.json"))
    monkeypatch.setattr(mod, "_cached_key_mtime", -1.0)
    with pytest.raises(RuntimeError):
        asyncio.run(mod.chat_completion([{"role": "user", "content": "hi"}]))


def test_chat_completion_ok(monkeypatch):
    import asyncio

    mod = auto_chat
    client = _FakeClient({"choices": [{"message": {"content": "  你好哦  "}}]})
    monkeypatch.setattr(mod, "_load_key", lambda: "sk-1")
    monkeypatch.setattr(mod, "_get_http_client", lambda: client)

    reply = asyncio.run(mod.chat_completion([{"role": "user", "content": "hi"}], max_tokens=77))
    assert reply == "你好哦"
    (call,) = client.calls
    assert call["headers"]["Authorization"] == "Bearer sk-1"
    assert call["json"]["model"] == mod.MODEL
    assert call["json"]["max_tokens"] == 77
    assert call["json"]["thinking"] == {"type": "disabled"}


def test_model_is_switched_back_to_deepseek():
    assert auto_chat.MODEL == "deepseek-v4-flash"


def test_system_prompt_has_hard_simplified_chinese_rule():
    assert "所有回复必须使用简体中文" in auto_chat.SYSTEM
    assert "禁止整段使用日文" in auto_chat.SYSTEM
    assert "每次最多使用 1～2 个" in auto_chat.SYSTEM


def test_timeout_notifies_owner_with_cooldown(monkeypatch):
    class _Bot:
        def __init__(self):
            self.calls = []

        async def send_private_msg(self, **kwargs):
            self.calls.append(kwargs)

    mod = auto_chat
    bot = _Bot()
    monkeypatch.setattr(mod, "_OWNER", "123456")
    monkeypatch.setattr(mod, "_last_timeout_notice", 0.0)

    asyncio.run(mod._notify_owner_timeout(bot))
    asyncio.run(mod._notify_owner_timeout(bot))

    assert len(bot.calls) == 1
    assert bot.calls[0]["user_id"] == 123456
    # 新文案：说明 AI 接口超时/不可用并提示检查连通性，不再提青云客
    text = str(bot.calls[0]["message"])
    assert "超时" in text
    assert "本次未回复" in text
    assert "连通性" in text
    assert "青云客" not in text


class _FlakyClient:
    """前 fail_times 次抛网络异常，之后返回正常 payload。"""

    def __init__(self, fail_times, payload=None):
        self.fail_times = fail_times
        self.payload = payload or {"choices": [{"message": {"content": "成功回复"}}]}
        self.calls = 0

    async def post(self, url, headers=None, json=None, timeout=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise httpx.ConnectError("connection refused")
        return _FakeResponse(self.payload)


def test_chat_completion_retries_then_succeeds(monkeypatch):
    mod = auto_chat
    client = _FlakyClient(fail_times=2)
    monkeypatch.setattr(mod, "_load_key", lambda: "test-key")
    monkeypatch.setattr(mod, "_get_http_client", lambda: client)
    monkeypatch.setattr(mod, "_RETRY_DELAY", 0)

    out = asyncio.run(mod.chat_completion([{"role": "user", "content": "hi"}]))

    assert out == "成功回复"
    assert client.calls == 3


def test_chat_completion_gives_up_after_three_failures(monkeypatch):
    mod = auto_chat
    client = _FlakyClient(fail_times=99)
    monkeypatch.setattr(mod, "_load_key", lambda: "test-key")
    monkeypatch.setattr(mod, "_get_http_client", lambda: client)
    monkeypatch.setattr(mod, "_RETRY_DELAY", 0)

    with pytest.raises(httpx.ConnectError):
        asyncio.run(mod.chat_completion([{"role": "user", "content": "hi"}]))

    assert client.calls == 3  # 第 3 次失败后不再请求


def test_chat_completion_retries_on_empty_content(monkeypatch):
    mod = auto_chat
    client = _FlakyClient(fail_times=1, payload={"choices": [{"message": {"content": "  "}}]})
    # 第 1 次返回空白，第 2 次起返回默认成功 payload
    client.payloads = [{"choices": [{"message": {"content": "  "}}]}, {"choices": [{"message": {"content": "成功回复"}}]}]

    async def post(url, headers=None, json=None, timeout=None):
        client.calls += 1
        return _FakeResponse(client.payloads[min(client.calls - 1, 1)])

    client.post = post
    monkeypatch.setattr(mod, "_load_key", lambda: "test-key")
    monkeypatch.setattr(mod, "_get_http_client", lambda: client)
    monkeypatch.setattr(mod, "_RETRY_DELAY", 0)

    out = asyncio.run(mod.chat_completion([{"role": "user", "content": "hi"}]))

    assert out == "成功回复"
    assert client.calls == 2


class _StatusClient:
    """按序返回指定状态码的 HTTPStatusError，状态码耗尽后返回成功 payload。"""

    def __init__(self, codes, payload=None):
        self.codes = list(codes)
        self.payload = payload or {"choices": [{"message": {"content": "成功回复"}}]}
        self.calls = 0

    async def post(self, url, headers=None, json=None, timeout=None):
        self.calls += 1
        if self.codes:
            code = self.codes.pop(0)
            request = httpx.Request("POST", "https://api.test/chat")
            response = httpx.Response(code, request=request)

            class _Err:
                def raise_for_status(self):
                    raise httpx.HTTPStatusError(
                        f"HTTP {code}", request=request, response=response,
                    )

            return _Err()
        return _FakeResponse(self.payload)


def test_chat_completion_4xx_not_retried(monkeypatch):
    # 401 等鉴权/请求格式类错误不可恢复：只请求 1 次即抛，不浪费重试
    mod = auto_chat
    client = _StatusClient([401])
    monkeypatch.setattr(mod, "_load_key", lambda: "test-key")
    monkeypatch.setattr(mod, "_get_http_client", lambda: client)
    monkeypatch.setattr(mod, "_RETRY_DELAY", 0)

    with pytest.raises(httpx.HTTPStatusError) as exc:
        asyncio.run(mod.chat_completion([{"role": "user", "content": "hi"}]))

    assert exc.value.response.status_code == 401
    assert client.calls == 1


def test_chat_completion_5xx_and_429_are_retried(monkeypatch):
    # 5xx 与 429 属可重试错误：重试后成功
    mod = auto_chat
    client = _StatusClient([500, 429])
    monkeypatch.setattr(mod, "_load_key", lambda: "test-key")
    monkeypatch.setattr(mod, "_get_http_client", lambda: client)
    monkeypatch.setattr(mod, "_RETRY_DELAY", 0)

    out = asyncio.run(mod.chat_completion([{"role": "user", "content": "hi"}]))

    assert out == "成功回复"
    assert client.calls == 3
