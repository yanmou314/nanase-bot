"""按文件路径加载插件模块（绕过 nonebot 插件加载器）。"""
import importlib.util
from pathlib import Path

BOT_ROOT = Path(__file__).resolve().parents[1]


def load_plugin(name: str):
    path = BOT_ROOT / "plugins" / name / "__init__.py"
    mod_name = f"plugin_{name}"
    if mod_name in __import__("sys").modules:
        return __import__("sys").modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    __import__("sys").modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_module(relpath: str, mod_name: str):
    """加载插件目录内的非 __init__ 模块，如 chat_stats/db_pg.py。"""
    if mod_name in __import__("sys").modules:
        return __import__("sys").modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, BOT_ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    __import__("sys").modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod
