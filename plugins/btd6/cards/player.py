"""玩家档案卡片。"""
from . import common
from .. import i18n, util


def player_html(col: dict) -> str:
    if col.get("empty"):
        body = f'<div class="panel"><div class="empty">{util._esc(col["empty"])}</div></div>'
        return common._shell(body, 300)
    p = col["p"]
    popped = p.get("bloonsPopped") or {}
    gp = p.get("gameplay") or {}
    vr = p.get("veteranRank") or 0

    banner = (f"<div class='pbanner'><img src='{util._esc(col['banner'])}'/></div>" if col.get("banner") else "")
    avatar = (f"<div class='pavatar'><img src='{util._esc(col['avatar'])}'/></div>" if col.get("avatar") else "")
    vr_txt = f" · 老兵 {util._esc(vr)}" if vr else ""
    rank = util._esc(p.get("rank") or "—")
    followers = util._esc(util.fmt_cn_num(p.get("followers")))
    most_used = util._esc(i18n.tower_cn(str(p.get("mostExperiencedMonkey") or "")))
    head = (f"<div class='panel'>{banner}"
            f"<div class='phead'>{avatar}"
            f"<div class='ptext'><div class='big'>{util._esc(p.get('displayName'))}</div>"
            f"<div class='sub'>等级 {rank}{vr_txt} · 粉丝 {followers}"
            f" · 最常用猴 {most_used}</div></div></div></div>")

    def stat_panel(title: str, pairs: list[tuple[str, str]]) -> str:
        rows = "".join(
            f"<div class='st'>{util._esc(k)} <b>{util._esc(v)}</b></div>" for k, v in pairs
        )
        return f"<div class='panel'><div class='ptitle'>{util._esc(title)}</div>{rows}</div>"

    body = head + (
        stat_panel("关键数据", [
            ("最高回合", str(p.get("highestRound") or "—")),
            ("CHIMPS 最高", str(gp.get("highestRoundCHIMPS") or "—")),
            ("成就", str(p.get("achievements") or "—")),
            ("累计猴币", util.fmt_cn_num(gp.get("cashEarned"))),
        ])
        + stat_panel("气球战报", [
            ("总气球", util.fmt_cn_num(popped.get("bloonsPopped"))),
            ("Boss 气球", util.fmt_cn_num(popped.get("bossesPopped"))),
            ("MOAB", util.fmt_cn_num(popped.get("moabsPopped"))),
            ("金气球", util.fmt_cn_num(popped.get("goldenBloonsPopped"))),
        ])
        + stat_panel("游戏历程", [
            ("局数 / 胜场", f"{util.fmt_cn_num(gp.get('gameCount'))} / {util.fmt_cn_num(gp.get('gamesWon'))}"),
            ("挑战完成", util.fmt_cn_num(gp.get("challengesCompleted"))),
            ("奖杯", util.fmt_cn_num(gp.get("totalTrophiesEarned"))),
            ("Odyssey 星", util.fmt_cn_num(gp.get("totalOdysseyStars"))),
        ])
    )
    return common._shell(body, 20 + (280 if col.get("banner") else 0) + 230 + 3 * 285 + 40)
