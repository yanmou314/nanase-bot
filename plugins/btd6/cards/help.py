"""帮助菜单卡片（纯静态）。"""
from . import common
from .. import i18n, util


def help_html() -> str:
    """帮助菜单卡片：按分组列出全部命令。"""
    panels = []
    total_h = 40
    for title, rows in i18n.HELP_GROUPS:
        rows_html = "".join(
            f"<div class='hrow'><span class='chip'>{util._esc(cmd)}</span>"
            f"<div class='hdesc'>{util._esc(desc)}</div></div>"
            for cmd, desc in rows
        )
        panels.append(
            f"<div class='panel'><div class='ptitle' "
            f"style='border-left:8px solid #11a6c5;padding-left:14px'>"
            f"{util._esc(title)}</div>{rows_html}</div>"
        )
        total_h += 60 + len(rows) * 106
    return common._shell("".join(panels), total_h + 40)
