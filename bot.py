import os

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

nonebot.init(
    apscheduler_config={
        "apscheduler.job_defaults.misfire_grace_time": 3600,
        "apscheduler.job_defaults.coalesce": False,
        "apscheduler.job_defaults.max_instances": 1,
    }
)
driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

nonebot.load_from_toml("pyproject.toml")

if __name__ == "__main__":
    nonebot.run()
