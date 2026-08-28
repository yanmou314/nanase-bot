"""按文件路径加载插件模块（绕过 nonebot 插件加载器）。"""
import importlib.util
from pathlib import Path
import sys

BOT_ROOT = Path(__file__).resolve().parents[1]


def load_plugin(name: str):
    path = BOT_ROOT / "plugins" / name / "__init__.py"
    mod_name = f"plugin_{name}"
    # 始终从源码重新加载，避免 importlib 的 spec 缓存
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_module(relpath: str, mod_name: str):
    """加载插件目录内的非 __init__ 模块，如 chat_stats/db_pg.py。"""
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, BOT_ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod
