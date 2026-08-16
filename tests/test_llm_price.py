import asyncio

from helpers import BOT_ROOT, load_plugin

llm_price = load_plugin("llm_price")


def _exec_with_env(monkeypatch, env):
    """以指定环境变量重新执行插件源码，验证模块级 env 解析逻辑。"""
    path = BOT_ROOT / "plugins" / "llm_price" / "__init__.py"
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    ns = {"__name__": "llm_price_env_probe", "__file__": str(path)}
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), ns)
    return ns


def _sample_api():
    return {
        "openai": {"models": {
            "gpt-x": {"cost": {"input": 1, "output": 2}},
            "bad-price": {"cost": {"input": -1, "output": 2}},
            "no-cost": {"name": "meta"},
        }},
        "legacy": {"models": {
            "old-unit": {"cost": {"input": 0.000001, "output": 0.000002, "unit": "usd_per_token"}},
        }},
        "not-a-provider": "oops",
    }


def test_fetch_parses_models_dev(monkeypatch):
    mod = llm_price

    async def fake():
        return _sample_api()
    monkeypatch.setattr(mod, "_fetch_api_json", fake)
    out = asyncio.run(mod.fetch_models_dev())
    full = out["openai/gpt-x"]
    assert full["in_rmb"] == 1 * mod.USD_CNY
    assert full["out_rmb"] == 2 * mod.USD_CNY
    assert out["gpt-x"] is full  # 短名别名指向同一价格对象
    assert "openai/bad-price" not in out  # 负价剔除
    assert "openai/no-cost" not in out  # 缺价剔除
    legacy = out["legacy/old-unit"]
    assert legacy["in_rmb"] == 0.000001 * mod.M_TOKENS * mod.USD_CNY


def test_build_rows_prefers_live_over_cache():
    mod = llm_price
    mid, vendor, name = mod.MODELS[0]
    prices = {mid: {"in_rmb": 1.5, "out_rmb": 3.0}}
    cache = {"models": {mid: {"in_rmb": 9.9, "out_rmb": 9.9}, "junk": {"in_rmb": "x"}}}
    rows = mod.build_rows(prices, cache)
    row = next(r for r in rows if r[1] == name)
    assert row[2] == 1.5 and row[3] == 3.0 and row[4] is False


def test_build_rows_falls_back_to_cache():
    mod = llm_price
    mid, vendor, name = mod.MODELS[0]
    cache = {"models": {mid: {"in_rmb": 7.0, "out_rmb": 8.0}}}
    rows = mod.build_rows({}, cache)
    row = next(r for r in rows if r[1] == name)
    assert row[2] == 7.0 and row[3] == 8.0 and row[4] is True
    # 坏缓存条目被跳过
    rows_junk = mod.build_rows({}, {"models": {"junk": {"in_rmb": None}}})
    assert all(r[1] != "junk" for r in rows_junk)


def test_usd_cny_env_override(monkeypatch):
    ns = _exec_with_env(monkeypatch, {"USD_CNY": "8.0"})
    assert ns["USD_CNY"] == 8.0


def test_usd_cny_invalid_env(monkeypatch):
    ns = _exec_with_env(monkeypatch, {"USD_CNY": "abc"})
    assert ns["USD_CNY"] == 7.2


def test_proxy_empty_forces_direct(monkeypatch):
    ns = _exec_with_env(monkeypatch, {"MODELS_DEV_PROXY": ""})
    assert ns["HTTP_PROXY"] is None


def test_proxy_default():
    assert llm_price.HTTP_PROXY == "http://127.0.0.1:7890"
