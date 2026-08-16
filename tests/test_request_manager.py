import json
import time

from helpers import load_plugin

request_manager = load_plugin("request_manager")


def test_save_and_load_keywords(monkeypatch, tmp_path):
    mod = request_manager
    f = tmp_path / "auto_approve.json"
    monkeypatch.setattr(mod, "CONFIG_FILE", str(f))
    saved = mod._save_keywords(123, ["老玩家", "老玩家", "内部"], merge=True)
    assert saved == ["老玩家", "内部"]
    assert mod._load_keywords(123) == ["老玩家", "内部"]
    assert mod._load_keywords(456) == []


def test_save_keywords_merge_keeps_existing(monkeypatch, tmp_path):
    mod = request_manager
    f = tmp_path / "auto_approve.json"
    monkeypatch.setattr(mod, "CONFIG_FILE", str(f))
    mod._save_keywords(111, ["a"])
    merged = mod._save_keywords(111, ["b", "a"], merge=True)
    assert merged == ["a", "b"]


def test_save_keywords_replace(monkeypatch, tmp_path):
    mod = request_manager
    f = tmp_path / "auto_approve.json"
    monkeypatch.setattr(mod, "CONFIG_FILE", str(f))
    mod._save_keywords(111, ["a", "b"])
    replaced = mod._save_keywords(111, ["c"], merge=False)
    assert replaced == ["c"]
    # 文件内容为合法 JSON 且写法原子（无 .tmp 残留）
    data = json.loads(f.read_text(encoding="utf-8"))
    assert data == {"111": ["c"]}
    assert not (tmp_path / "auto_approve.json.tmp").exists()


def test_load_keywords_bad_file(monkeypatch, tmp_path):
    mod = request_manager
    f = tmp_path / "auto_approve.json"
    f.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(mod, "CONFIG_FILE", str(f))
    assert mod._load_keywords(1) == []


def test_purge_pending_ttl():
    mod = request_manager
    mod._pending.clear()
    mod._pending["flag_old"] = {"ts": time.time() - mod._PENDING_TTL - 1}
    mod._pending["flag_new"] = {"ts": time.time()}
    mod._purge_pending()
    assert "flag_old" not in mod._pending
    assert "flag_new" in mod._pending
    mod._pending.clear()


def test_owner_decision_regex_patterns():
    # 引用通知文本解析用的两个正则，直接验证模式行为
    import re
    quoted_friend = "🔔 好友申请\n👤 申请人：123456（08-16 12:00）"
    quoted_group = "🔔 有人申请进群\n🏘 群号：654321\n👤 申请人：123456（08-16 12:00）"
    m = re.search(r"申请人[:：](\d+)", quoted_friend)
    assert m and m.group(1) == "123456"
    g = re.search(r"群号[:：](\d+)", quoted_group)
    assert g and g.group(1) == "654321"
