import os

from dotenv import load_dotenv

_BOT_ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(_BOT_ROOT)
load_dotenv(os.path.join(_BOT_ROOT, ".env"))

import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

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

# 先把 apscheduler 注册为正式插件：plugins/ 内插件直接 import 它，
# 第三方插件（如 nonebot_plugin_parser）则 require 它，要求其已完成插件注册
nonebot.load_plugin("nonebot_plugin_apscheduler")

nonebot.load_from_toml(os.path.join(_BOT_ROOT, "pyproject.toml"))

if __name__ == "__main__":
    nonebot.run()
