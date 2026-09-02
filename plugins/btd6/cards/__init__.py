"""渲染层入口：内容哈希缓存 + 线程池 + 全局信号量的卡片渲染管线。

各卡片 HTML 构建函数按卡片分文件（common/overview/leaderboard/odyssey/
rules/rush/player/help），在此统一再导出供 push/handlers 经 ``cards.<名>`` 访问。
"""
import asyncio
import hashlib
import logging
import os
from pathlib import Path

# 注意：此处 `common` 是顶层 opt/bot/common.py（渲染管线/信号量），
# `.common`（cards/common.py）是本渲染层的 HTML 外壳模块，二者重名勿混
from common import RENDER_SEM, RENDER_TOTAL_TIMEOUT, render_html_to_png
from nonebot.adapters.onebot.v11 import MessageSegment

from . import common, collectevent, ctmap, leaderboard, odyssey, overview, rules, rush

from .. import assets

from .common import (  # noqa: F401
    CARD_W,
    ODYSSEY_CARD_W,
    RACE_CARD_W,
    _TOWER_CAT_COLORS,
    _bg_cache,
    _bg_data_url,
    _list_shell,
    _odyssey_shell,
    _race_modifier_html,
    _race_modifier_items,
    _race_shell,
    _race_ui_img,
    _rush_tower_category,
    _shell,
    _tower_cat_grad,
)

from .collectevent import (  # noqa: F401
    collectevent_html,
)

from .ctmap import (  # noqa: F401
    CT_PRESET_CARDS,
    ctmap_html,
    ctmap_preset_html,
    ct_tile_html,
)

from .leaderboard import (  # noqa: F401
    _MEDAL_COLOR,
    leaderboard_html,
    maps_html,
)

from .odyssey import (  # noqa: F401
    _ODIFF_TO_BTN,
    _ODYSSEY_REWARD_ICON,
    _odyssey_available_html,
    _odyssey_card_height,
    _odyssey_default_crew_html,
    _odyssey_img,
    _odyssey_map_icons,
    _odyssey_map_rule_text,
    _odyssey_maps_html,
    _odyssey_power_icon,
    _odyssey_reward_icon,
    _odyssey_rewards_html,
    _odyssey_top_icon,
    _odyssey_tower_card,
    _odyssey_tower_lookup,
    _odyssey_upgrade_caps,
    odyssey_diff_html,
)

from .overview import (  # noqa: F401
    _SEC_COLOR,
    overview_html,
)

from .player import (  # noqa: F401
    player_html,
)

from .rules import (  # noqa: F401
    _ROUND_BLOON_ICON_FILES,
    _ROUND_BLOON_TOKEN_RE,
    _ct_html,
    _custom_round_details,
    _custom_round_set_keys,
    _custom_round_sets,
    _daily_monkey_grid,
    _monkey_cell,
    _monkey_grid,
    _path_max_txt,
    _race_emblem,
    _race_monkey_cell,
    _race_monkey_grid,
    _race_time_line,
    _race_title,
    _race_visible_towers,
    _round_bloon_icon,
    _round_detail_desc_html,
    _rules_compat_html,
    _stat,
    rules_html,
)

from .rush import (  # noqa: F401
    _RUSH_MAX_MONKEYS,
    _relic_cn,
    _rush_diff_html,
    _rush_shell,
)

from .help import (  # noqa: F401
    help_html,
)

_logger = logging.getLogger(__name__)


CARD_MAX_AGE = 6 * 60 * 60   # 仅作用于 .render 临时渲染目录；最终 PNG 的回收靠条数/体积驱逐（见 _render_card_sync）


CARD_DPI = 120                # 渲染分辨率：小机器上 144 → 120 明显提速，QQ 显示足够
# 规则与远征卡片的数据通常会持续数天甚至数周不变，单独放在持久缓存目录。
# 帮助菜单（btd6help）内容完全静态、不依赖任何 API/时间，必须持久化以保证
# 首屏秒回；其余实时内容（排行榜/每日/总览）仍使用普通缓存，避免旧数据占用空间。
# 匹配按 "_" 前第一段做前缀归类：btd6rule_standard/btd6rule_elite（Boss 双榜规则卡）
# 归入 btd6rule；rush 数据同样持续数天且不含倒计时，一并持久化
PERSISTENT_CARD_PREFIXES = {"btd6rule", "btd6ody", "btd6help", "btd6rush"}
PERSISTENT_CARD_FILES = 128
PERSISTENT_CARD_BYTES = 256_000_000


MAX_CARD_FILES = 128
MAX_CARD_BYTES = 64_000_000


# ---------------- 渲染（内容哈希缓存 + 线程池 + 全局信号量） ----------------


def _render_card_sync(prefix: str, html: str) -> str:
    # 用完整 HTML 的指纹作为数据/版式版本号：命令每次仍会先请求并整理
    # API 数据，只有指纹变化或本地 PNG 不存在时才重新渲染。
    # 按 "_" 前第一段归类，使 btd6rule_standard 等变体命中持久池
    persistent = prefix.split("_", 1)[0] in PERSISTENT_CARD_PREFIXES
    card_dir = os.path.join(assets.CACHE_DIR, "cards") if persistent else assets.CACHE_DIR
    os.makedirs(card_dir, exist_ok=True)
    key = hashlib.md5(html.encode("utf-8")).hexdigest()[:20]
    path = os.path.join(card_dir, f"{prefix}_{key}.png")
    if os.path.isfile(path):
        return path  # 同内容直接复用，持久卡片不会因普通 TTL 被清理
    # common.render_html_to_png 自带 TTL 清理；渲染到临时子目录，避免它
    # 把 cards/ 中长期保留的 PNG 当作普通临时文件清掉。
    render_dir = os.path.join(assets.CACHE_DIR, ".render")
    os.makedirs(render_dir, exist_ok=True)
    tmp = render_html_to_png(html, prefix, render_dir, max_age=CARD_MAX_AGE, dpi=CARD_DPI)
    os.replace(tmp, path)
    if persistent:
        assets._prune_cache_files(card_dir, ".png", PERSISTENT_CARD_FILES, PERSISTENT_CARD_BYTES, {path})
    else:
        assets._prune_cache_files(assets.CACHE_DIR, ".png", MAX_CARD_FILES, MAX_CARD_BYTES, {path})
    return path


async def _render_card(prefix: str, html_fn) -> str:
    """构建 HTML 与渲染全程放在 worker 线程，经全局信号量串行化，避免阻塞事件循环。

    wait_for 总超时保护与顶层 render_html_to_png_async 一致：weasyprint 挂死时
    释放 RENDER_SEM（全站唯一渲染槽），避免所有插件渲染永久冻结（无法强杀线程，
    但僵死线程只占 executor 槽位）。不可改用 render_html_to_png_async——它内部
    会再次获取 RENDER_SEM，直接死锁。"""

    def _job() -> str:
        return _render_card_sync(prefix, html_fn())

    async with RENDER_SEM:
        return await asyncio.wait_for(asyncio.to_thread(_job), timeout=RENDER_TOTAL_TIMEOUT)


async def _send_card(matcher, prefix: str, html_fn, text_fn) -> None:
    try:
        path = await _render_card(prefix, html_fn)
    except Exception:
        _logger.warning("BTD6 卡片渲染失败，回退文本消息", exc_info=True)
        await matcher.finish(MessageSegment.text(text_fn()))
    await matcher.finish(MessageSegment.image(Path(path).as_uri()))


async def _finish_multi_cards(matcher, cards: list[tuple[str, object, object]]) -> None:
    """多卡片流式发送：逐张渲染，前 N-1 张 send、最后一张 finish；
    单张渲染失败回退该张的文本，不影响其余卡片。
    cards: [(prefix, html_fn, text_fn), ...]"""
    for idx, (prefix, html_fn, text_fn) in enumerate(cards):
        last = idx == len(cards) - 1
        try:
            path = await _render_card(prefix, html_fn)
        except Exception:
            _logger.warning("BTD6 多卡渲染失败，回退文本 prefix=%s", prefix, exc_info=True)
            msg = MessageSegment.text(text_fn())
            await (matcher.finish(msg) if last else matcher.send(msg))
            continue
        msg = MessageSegment.image(Path(path).as_uri())
        await (matcher.finish(msg) if last else matcher.send(msg))


__all__ = [
    'CARD_W',
    'ODYSSEY_CARD_W',
    'RACE_CARD_W',
    '_MEDAL_COLOR',
    '_ODIFF_TO_BTN',
    '_ODYSSEY_REWARD_ICON',
    '_ROUND_BLOON_ICON_FILES',
    '_ROUND_BLOON_TOKEN_RE',
    '_RUSH_MAX_MONKEYS',
    '_SEC_COLOR',
    '_TOWER_CAT_COLORS',
    '_bg_cache',
    '_bg_data_url',
    '_ct_html',
    '_list_shell',    '_custom_round_details',
    '_custom_round_set_keys',
    '_custom_round_sets',
    '_daily_monkey_grid',
    '_monkey_cell',
    '_monkey_grid',
    '_odyssey_available_html',
    '_odyssey_card_height',
    '_odyssey_default_crew_html',
    '_odyssey_img',
    '_odyssey_map_icons',
    '_odyssey_map_rule_text',
    '_odyssey_maps_html',
    '_odyssey_power_icon',
    '_odyssey_reward_icon',
    '_odyssey_rewards_html',
    '_odyssey_shell',
    '_odyssey_top_icon',
    '_odyssey_tower_card',
    '_odyssey_tower_lookup',
    '_odyssey_upgrade_caps',
    '_path_max_txt',
    '_race_emblem',
    '_race_modifier_html',
    '_race_modifier_items',
    '_race_monkey_cell',
    '_race_monkey_grid',
    '_race_shell',
    '_race_time_line',
    '_race_title',
    '_race_ui_img',
    '_race_visible_towers',
    '_relic_cn',
    '_round_bloon_icon',
    '_round_detail_desc_html',
    '_rules_compat_html',
    '_rush_diff_html',
    '_rush_shell',
    '_rush_tower_category',
    '_shell',
    '_stat',
    '_tower_cat_grad',
    'common',
    'collectevent',
    'collectevent_html',
    'ctmap',
    'ctmap_html',
    'ctmap_preset_html',
    'CT_PRESET_CARDS',
    'help_html',
    'leaderboard',
    'leaderboard_html',
    'maps_html',
    'odyssey',
    'odyssey_diff_html',
    'overview',
    'overview_html',
    'player_html',
    'rules',
    'rules_html',
    'rush',
]
