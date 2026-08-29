import logging
import os
import sys

from dotenv import load_dotenv

_BOT_ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(_BOT_ROOT)
load_dotenv(os.path.join(_BOT_ROOT, ".env"))

_logger = logging.getLogger("qqbot.bot")

import nonebot  # noqa: E402  # 需先 chdir 并加载 .env
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter  # noqa: E402

_OWNER = os.getenv("QQBOT_OWNER", "")
if not _OWNER.isdigit():
    raise RuntimeError(
        "QQBOT_OWNER 未配置或不是纯数字 QQ 号，请在 .env 中设置后再启动"
    )

nonebot.init(
    apscheduler_config={
        "apscheduler.job_defaults.misfire_grace_time": 3600,
        "apscheduler.job_defaults.coalesce": False,
        "apscheduler.job_defaults.max_instances": 1,
    }
)
driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

# 先把 apscheduler 注册为正式插件：plugins/ 内插件直接 import 它当普通模块用，
# 若将来接入 require 它的第三方插件，也需要它先完成插件注册
nonebot.load_plugin("nonebot_plugin_apscheduler")

nonebot.load_from_toml(os.path.join(_BOT_ROOT, "pyproject.toml"))

# nonebot 对插件加载失败只记日志不抛错，error_notify 若随依赖一起静默加载失败，
# 整条告警链路就消失了。这里显式断言关键插件已加载，缺失则崩溃退出
# （systemd Restart=always 会重试，宁可崩溃循环也不静默降级）。
_loaded = {p.name for p in nonebot.get_loaded_plugins()}
_missing = [name for name in ("error_notify",) if name not in _loaded]
if _missing:
    _logger.critical("关键插件加载失败：%s，告警链路缺失，拒绝启动", "、".join(_missing))
    sys.exit(1)

if __name__ == "__main__":
    nonebot.run()
