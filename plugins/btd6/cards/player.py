"""玩家档案卡片。"""
from . import common
from .. import i18n, util


def _top3_names(d: dict, name_fn=None) -> str:
    """字典 {名: 数值} 取 Top3，中文名 + 紧凑数字。"""
    try:
        items = [(k, v) for k, v in (d or {}).items() if isinstance(v, (int, float))]
    except (AttributeError, TypeError):
        return "—"
    items.sort(key=lambda kv: kv[1], reverse=True)
    fn = name_fn or (lambda x: x)
    parts = [f"{fn(k)}{util.fmt_cn_num(v)}" for k, v in items[:3]]
    return " / ".join(parts) or "—"


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
    extra_h = 0
    sv = col.get("save") or {}
    if isinstance(sv, dict) and sv:
        txp = sv.get("towerXP") or {}
        top_xp = sorted(
            ((k, v) for k, v in txp.items() if isinstance(v, (int, float))),
            key=lambda kv: kv[1], reverse=True)[:3]
        top_xp_txt = " / ".join(
            f"{i18n.tower_cn(k)}{util.fmt_cn_num(v)}" for k, v in top_xp) or "—"
        stats = (col["p"].get("stats") or {})
        save_panels = [
            ("存档总览", [
                ("经验", str(util.fmt_cn_num(sv.get("xp")))),
                ("老兵经验", str(util.fmt_cn_num(sv.get("veteranXp")))),
                ("猴币", str(util.fmt_cn_num(sv.get("monkeyMoney")))),
                ("奖杯 当前/历史",
                 f"{util.fmt_cn_num(sv.get('trophies'))}/{util.fmt_cn_num(sv.get('lifetimeTrophies'))}"),
            ]),
            ("对局历程", [
                ("对局数", str(util.fmt_cn_num(sv.get("gamesPlayed")))),
                ("最高回合", str(sv.get("highestSeenRound") or "—")),
                ("竞速参赛", str(util.fmt_cn_num(sv.get("totalRacesEntered")))),
                ("每日完成", str(util.fmt_cn_num(sv.get("totalDailyChallengesCompleted")))),
                ("远征完成", str(util.fmt_cn_num(sv.get("totalCompletedOdysseys")))),
            ]),
            ("收藏进度", [
                ("成就已领取", str(len(sv.get("achievementsClaimed") or []))),
                ("知识点", str(sv.get("knowledgePoints") if sv.get("knowledgePoints") is not None else "—")),
                ("主英雄", str(i18n.tower_cn(str(sv.get("primaryHero") or "")))),
                ("塔经验 Top3", top_xp_txt),
                ("帕拉贡 Top3", _top3_names(stats.get("paragonsPurchasedByName"), i18n.tower_cn)),
                ("技能使用 Top3", _top3_names(stats.get("abilitiesActivatedByName"))),
            ]),
        ]
        for title, rows in save_panels:
            body += stat_panel(title, rows)
        extra_h = sum(285 + (len(rows) - 4) * 45 for _, rows in save_panels)
    return common._shell(body, 20 + (280 if col.get("banner") else 0) + 230 + 3 * 285 + 40 + extra_h)


PROFILE_CARD_W = 900


def _profile_shell(body, h):
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
@page {{ size: {PROFILE_CARD_W}px {h}px; margin: 0; background: #6aa9d4; }}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ width: {PROFILE_CARD_W}px; height: {h}px; color: #ffffff;
        font-family: "WenQuanYi Micro Hei", "Noto Sans CJK SC", sans-serif;
        background: linear-gradient(180deg, #8ec9e8 0%, #6aa9d4 55%, #4f93c4 100%); }}
.pf-page {{ padding: 22px 24px; }}
.pf-panel {{ background: #1e3a5c; border-radius: 14px; padding: 16px 20px;
             border: 1px solid #16304f;
             box-shadow: inset 0 1px 0 rgba(255,255,255,.08), 0 2px 0 rgba(10,30,55,.35); }}
.pf-title {{ color: #ffffff; font-size: 27px; line-height: 34px; font-weight: 900;
             text-align: center; letter-spacing: 2px; margin-bottom: 10px;
             text-shadow: 0 2px 0 #0d2138, 0 3px 4px rgba(0,0,0,.35); }}
.pf-banner {{ height: 150px; border-radius: 12px; background-size: cover;
              background-position: center; background-color: #16304f; }}
.pf-headgrid {{ display: table; width: 100%; margin-top: 14px; table-layout: fixed; }}
.pf-avcell {{ display: table-cell; width: 128px; vertical-align: middle; text-align: center; }}
.pf-avcell img {{ width: 112px; height: 112px; border-radius: 22px; border: 3px solid #ffd964;
                  background: #16304f; }}
.pf-av-fallback {{ width: 112px; height: 112px; margin: 0 auto; border-radius: 22px;
                   background: #16304f; color: #ffd964; font-size: 44px; line-height: 106px;
                   font-weight: 900; text-align: center; border: 3px solid #ffd964; }}
.pf-namecell {{ display: table-cell; vertical-align: middle; padding-left: 14px; }}
.pf-name {{ color: #ffffff; font-size: 34px; line-height: 40px; font-weight: 900; word-break: break-all;
            text-shadow: 0 2px 0 #0d2138, 0 3px 5px rgba(0,0,0,.4); }}
.pf-sub {{ color: #9fb6d4; font-size: 20px; line-height: 26px; font-weight: 700; padding-top: 4px; }}
.pf-rankrow {{ display: table; width: 100%; margin-top: 10px; table-layout: fixed; }}
.pf-starcell {{ display: table-cell; width: 64px; vertical-align: middle; text-align: center; }}
.pf-starcell img {{ width: 56px; height: 56px; }}
.pf-rankcell {{ display: table-cell; vertical-align: middle; padding-left: 8px; }}
.pf-lvtxt {{ color: #ffffff; font-size: 24px; line-height: 30px; font-weight: 900;
             text-shadow: 0 2px 0 #0d2138; }}
.pf-pill {{ display: inline-block; margin-left: 10px; padding: 2px 16px; border-radius: 16px;
            background: linear-gradient(180deg, #ffd964 0%, #f0a000 100%); color: #5a3a00;
            font-size: 21px; line-height: 28px; font-weight: 900; vertical-align: 3px; }}
.pf-bar {{ height: 28px; margin-top: 6px; border-radius: 14px; background: #0e2138;
           border: 1px solid #081627; overflow: hidden; }}
.pf-barfill {{ height: 26px; border-radius: 13px; text-align: center; color: #ffffff;
               font-size: 18px; line-height: 26px; font-weight: 900; white-space: nowrap;
               text-shadow: 0 1px 0 rgba(0,0,0,.5); }}
.pf-barfill.vet {{ background: linear-gradient(180deg, #a64dff 0%, #6a2bd9 60%, #4a1da3 100%); }}
.pf-barfill.lv {{ background: linear-gradient(180deg, #46c8f1 0%, #129ed0 60%, #087eaf 100%); }}
.pf-cols {{ display: table; width: 100%; margin-top: 14px; table-layout: fixed; }}
.pf-col {{ display: table-cell; vertical-align: top; }}
.pf-col.left {{ width: 404px; padding-right: 7px; }}
.pf-col.right {{ padding-left: 7px; }}
.pf-qrow {{ display: table; width: 100%; padding: 5px 0; table-layout: fixed; }}
.pf-qicon {{ display: table-cell; width: 54px; vertical-align: middle; text-align: center; }}
.pf-qicon img {{ width: 44px; height: 44px; }}
.pf-qtxt {{ display: table-cell; vertical-align: middle; color: #ffffff; font-size: 21px;
            line-height: 26px; font-weight: 700; text-shadow: 0 1px 0 #0d2138; }}
.pf-mrow {{ display: table; width: 100%; padding: 4px 0; table-layout: fixed; }}
.pf-mlab {{ display: table-cell; vertical-align: middle; color: #d7e6f5; font-size: 20px;
            line-height: 26px; font-weight: 500; }}
.pf-mval {{ display: table-cell; width: 170px; vertical-align: middle; text-align: right;
            color: #38e1ff; font-size: 20px; line-height: 26px; font-weight: 900;
            white-space: nowrap; text-shadow: 0 1px 0 #0d2138; }}
.pf-cur {{ display: table; width: 100%; margin-top: 6px; table-layout: fixed; text-align: center; }}
.pf-curcell {{ display: table-cell; vertical-align: middle; }}
.pf-curcell img {{ width: 50px; height: 50px; vertical-align: middle; }}
.pf-curnum {{ display: inline-block; vertical-align: middle; font-size: 28px; font-weight: 900;
              margin-left: 8px; text-shadow: 0 2px 0 #0d2138; }}
.pf-medals {{ text-align: center; padding-top: 10px; }}
.pf-medal {{ display: inline-block; width: 76px; margin: 4px 1px; vertical-align: top; }}
.pf-medal img {{ display: block; width: 46px; height: 46px; margin: 0 auto; }}
.pf-medal .n {{ color: #ffffff; font-size: 17px; line-height: 22px; font-weight: 900;
                text-shadow: 0 1px 0 #0d2138; }}
.pf-name{{text-transform:uppercase;}}
.pf-htab {{display:table; width:100%; margin-top:14px; table-layout:fixed;}}
.pf-hav {{display:table-cell; width:132px; vertical-align:middle; text-align:center;}}
.pf-hav img {{width:112px; height:112px; border-radius:22px; border:3px solid #ffd964; background:#16304f;}}
.pf-hmid {{display:table-cell; vertical-align:top; padding:0 8px;}}
.pf-hright {{display:table-cell; width:300px; vertical-align:top;}}
.pf-fol {{text-align:right; color:#9fb6d4; font-size:16px; line-height:20px; font-weight:700; letter-spacing:2px;}}
.pf-foln {{text-align:right; color:#ffffff; font-size:24px; line-height:28px; font-weight:900;
          text-shadow:0 2px 0 #0d2138;}}
.pf-lvrow {{display:table; width:100%; margin-top:8px; table-layout:fixed;}}
.pf-lvstar {{display:table-cell; width:64px; vertical-align:middle; text-align:center; height:64px; background-repeat:no-repeat; background-position:center; background-size:contain;}}
.pf-lvstar img {{width:52px; height:52px;}}
.pf-lvon {{color:#ffffff; font-size:25px; line-height:64px; font-weight:900; text-align:center; text-shadow:1px 0 0 #0d2138, -1px 0 0 #0d2138, 0 1px 0 #0d2138, 0 -1px 0 #0d2138, 0 2px 3px rgba(0,0,0,.5);}}
.pf-lvbar {{display:table-cell; vertical-align:middle; padding-left:6px;}}
.pf-ribbon {{margin:12px 6px 8px; padding:5px 10px; text-align:center; border-radius:10px;
            background:linear-gradient(180deg,#8a6a1f 0%,#5c4512 60%,#3a2c0a 100%);
            border:1px solid #c9a13b;}}
.pf-ribbon span {{color:#ffffff; font-size:22px; line-height:28px; font-weight:900; letter-spacing:2px;
                 text-shadow:0 2px 0 #0d2138;}}
.pf-ribbon.p {{background:linear-gradient(180deg,#6a3fb5 0%,#472a7d 60%,#2c1a4e 100%);
              border-color:#9a6ff0;}}
.pf-showall {{text-align:right; color:#9fb6d4; font-size:15px; line-height:20px; font-weight:700;
             padding-right:8px;}}
.pf-topgrid {{text-align:center; padding-bottom:4px;}}
.pf-topcell {{display:inline-block; width:118px; margin:4px 3px; vertical-align:top; text-align:center;
             background-repeat:no-repeat; background-position:center top; background-size:contain;
             padding:6px 0 4px;}}
.pf-topimg {{height:88px;}}
.pf-topnum {{color:#ffffff; font-size:22px; line-height:26px; font-weight:900;
            text-shadow:0 2px 0 #0d2138;}}
.pf-col.left {{width:440px;}}
.pf-medal {{width:76px; margin:4px 1px;}}
</style></head><body><div class="pf-page">{body}</div></body></html>"""


def _pf_num(v):
    if isinstance(v, bool):
        return str(int(v))
    if isinstance(v, (int, float)):
        return f"{int(v):,}"
    return "—"


def _img_wide_enough(data_url, min_w):
    try:
        import base64 as _b64
        import io as _bio
        from PIL import Image as _Image
        raw = _b64.b64decode(data_url.split(",", 1)[1])
        with _Image.open(_bio.BytesIO(raw)) as im:
            return im.width >= min_w
    except Exception:
        return False


def player_oak_html(col):
    from .. import collect as _collect
    from .. import assets as _assets
    if col.get("empty"):
        return _profile_shell(
            "<div class='pf-panel'><div class='pf-title'>玩家档案</div>"
            "<div class='pf-qtxt' style='text-align:center'>" + util._esc(col["empty"]) + "</div></div>", 300)
    p = col["p"]
    sv = col.get("save") or {}
    public = p

    parts = []
    # ---- 头部（横排：头像 | 名字+等级 | 粉丝+老兵） ----
    av = _assets._site_asset_data_url("ProfileAvatar/{}.webp".format((p.get("avatar") or "").strip()))
    if not av:
        av = col.get("avatar") or ""
    bn = _assets._site_asset_data_url("ProfileBanner/{}.webp".format((p.get("banner") or "").strip()))
    if not bn:
        bn = col.get("banner") or ""
    if bn and not _img_wide_enough(bn, 400):
        bn = ""
    banner_div = ""
    av_html = (f"<img src='{util._esc(av)}'/>") if av else "<div class='pf-av-fallback'>?</div>"
    lvl, lv_xp, lv_goal = _collect.profile_rank_info(sv)
    vet, vet_xp, vet_goal = _collect.profile_veteran_info(sv)
    _lvl_bg = _assets._site_asset_data_url("UI/LvlHolder.webp")
    _lvl_style = (f" style='background-image:url(&quot;{util._esc(_lvl_bg)}&quot;);'") if _lvl_bg else ""
    lvl_cell = f"<div class='pf-lvstar'{_lvl_style}><div class='pf-lvon'>{lvl}</div></div>"
    if lv_xp is None:
        lv_bar = ("<div class='pf-bar'><div class='pf-barfill lv' style='width:100%;'>"
                  "Max Level</div></div>")
    else:
        pct = max(2, min(100, round(lv_xp * 100 / max(1, lv_goal))))
        lv_bar = f"<div class='pf-bar'><div class='pf-barfill lv' style='width:{pct}%;'>{lv_xp:,}/{lv_goal:,}</div></div>"
    _vet_bg = _assets._site_asset_data_url("UI/LvlHolderVeteran.webp")
    _vet_style = (f" style='background-image:url(&quot;{util._esc(_vet_bg)}&quot;);'") if _vet_bg else ""
    vet_row = ""
    if int(sv.get("veteranXp") or 0) > 0:
        pct = max(2, min(100, round(vet_xp * 100 / max(1, vet_goal))))
        vet_row = (
            "<div class='pf-lvrow'><div class='pf-lvstar'" + _vet_style + ">"
            f"<div class='pf-lvon'>{vet}</div></div>"
            + "<div class='pf-lvbar'><div class='pf-bar'>"
              f"<div class='pf-barfill vet' style='width:{pct}%;'>{vet_xp:,}/{vet_goal:,}</div>"
              "</div></div></div>")
    head = (
        "<div class='pf-panel'>" + banner_div
        + "<div class='pf-htab'><div class='pf-hav'>" + av_html + "</div>"
        + "<div class='pf-hmid'>"
        + "<div class='pf-name'>" + util._esc(str(p.get("displayName") or "").upper()) + "</div>"
        + "<div class='pf-lvrow'>" + lvl_cell
        + "<div class='pf-lvbar'>" + lv_bar + "</div></div>"
        + "</div>"
        + "<div class='pf-hright'>"
        + "<div class='pf-fol'>FOLLOWERS</div>"
        + "<div class='pf-foln'>" + util._esc(util.fmt_cn_num(p.get("followers"))) + "</div>"
        + vet_row + "</div></div></div>")
    parts.append(head)
    head_h = 36 + 150 + 24 + 76 * (1 if int(sv.get("veteranXp") or 0) > 0 else 0)

    # ---- 左列 QUICK STATS ----
    quick = _collect.profile_quick_stats(sv, public)
    qrows = []
    for icon, text in quick:
        durl = _assets._site_asset_data_url(icon)
        img = (f"<img src='{util._esc(durl)}'/>") if durl else ""
        qrows.append(
            "<div class='pf-qrow'><div class='pf-qicon'>" + img + "</div>"
            + "<div class='pf-qtxt'>" + util._esc(text) + "</div></div>")
    left = ("<div class='pf-panel'><div class='pf-title'>QUICK STATS</div>"
            + "".join(qrows) + "</div>")
    left_h = 70 + len(qrows) * 54

    # ---- 左列 CURRENCY & MEDALS ----
    mm = _assets._site_asset_data_url("UI/BloonjaminsIcon.webp")
    kn = _assets._site_asset_data_url("UI/KnowledgeIcon.webp")
    tr = _assets._site_asset_data_url("UI/TrophyIcon.webp")

    def _cur(durl, num, color):
        img = (f"<img src='{util._esc(durl)}'/>") if durl else ""
        return ("<div class='pf-curcell'>" + img
                + "<span class='pf-curnum' style='color:" + color + ";'>" + num + "</span></div>")

    cur = ("<div class='pf-panel' style='margin-top:14px;'>"
           "<div class='pf-title'>CURRENCY &amp; MEDALS</div>"
           "<div class='pf-cur'>"
           + _cur(mm, "$" + _pf_num(sv.get("monkeyMoney")), "#bfff3c")
           + _cur(kn, _pf_num(sv.get("knowledgePoints")), "#d48aff")
           + _cur(tr, _pf_num(sv.get("trophies")), "#ffc93c")
           + "</div>")
    medals = _collect.profile_medals(public)
    med_cells = []
    for icon, n in medals:
        durl = _assets._site_asset_data_url(icon)
        img = (f"<img src='{util._esc(durl)}'/>") if durl else "<div style='height:62px;'></div>"
        med_cells.append(
            "<div class='pf-medal'>" + img + "<div class='n'>" + f"{n:,}" + "</div></div>")
    cur += "<div class='pf-medals'>" + "".join(med_cells) + "</div></div>"
    left += cur
    med_rows = max(1, -(-len(med_cells) // 5))
    left_h += 80 + 100 + 64 + med_rows * 84

    # ---- 左列 TOP 区 ----
    tops = _collect.profile_tops(public)

    def _topcell(bg, img, num):
        style = (f" style='background-image:url(&quot;{util._esc(bg)}&quot;);'") if bg else ""
        pic = (f"<img class='pf-topimg' src='{util._esc(img)}'/>") if img else ""
        return ("<div class='pf-topcell'" + style + ">" + pic
                + "<div class='pf-topnum'>" + f"{num:,}" + "</div></div>")

    def _topsec(title, cells, purple=False):
        return ("<div class='pf-panel' style='margin-top:14px;'>"
                "<div class='pf-ribbon{}'><span>{}</span></div>"
                "<div class='pf-showall'>SHOW ALL</div>"
                "<div class='pf-topgrid'>".format(" p" if purple else "", title)
                + "".join(cells) + "</div></div>")

    def _tower_bg():
        return _assets._site_asset_data_url("UI/InstaTowersContainer.webp")

    def _tower_img(t):
        n = "Wizard" if t == "WizardMonkey" else t
        u = _assets._site_asset_data_url(f"UI/InstaContainer/000-{n}.webp")
        if not u:
            u = _assets._game_asset_data_url(f"000-{n}.webp")
        return u

    hcells = [(_topcell("", _assets._site_asset_data_url(f"Portrait/{h}Portrait.webp"), n))
              for h, n in tops["heroes"][:3]]
    tcells = [(_topcell(_tower_bg(), _tower_img(t), n)) for t, n in tops["towers"][:3]
              if _tower_img(t)]
    if len(tcells) < min(3, len(tops["towers"])):
        tcells = [(_topcell(_tower_bg(), _tower_img(t), n)) for t, n in tops["towers"][:3]]
    pcells = [(_topcell(_assets._site_asset_data_url("UI/ParagonContainer.webp"),
                        _assets._site_asset_data_url(f"TowerIcon/Paragon-{t}.webp"), n))
              for t, n in tops["paragons"][:3]]
    yb = _assets._site_asset_data_url("UI/YellowBtn.webp")
    acells = [(_topcell(yb, _assets._site_asset_data_url(f"AbilityIcon/{ic}.webp"), n))
              for _name, ic, n in tops["abilities"][:3]]
    left += _topsec("TOP HEROES", hcells)
    left += _topsec("TOP TOWERS", tcells)
    left += _topsec("TOP PARAGONS", pcells, purple=True)
    left += _topsec("TOP ABILITIES", acells)
    left_h += 4 * (110 + 150)

    # ---- 右列 MAIN GAME STATS ----
    gp = public.get("gameplay") or {}
    bp = public.get("bloonsPopped") or {}
    st = public.get("stats") or {}
    ach_total = int((_collect.site_data().get("achievementsTotal")) or 162)
    most = str(public.get("mostExperiencedMonkey") or "")
    _txp = sv.get("towerXP") if isinstance(sv.get("towerXP"), dict) else {}
    most_xp = _txp.get(most)
    main_rows = [
        ("进行对局", _pf_num(gp.get("gameCount"))),
        ("获胜对局", _pf_num(gp.get("gamesWon"))),
        ("历史最高回合", _pf_num(gp.get("highestRound"))),
        ("CHIMPS最高回合", _pf_num(gp.get("highestRoundCHIMPS"))),
        ("通缩最高回合", _pf_num(gp.get("highestRoundDeflation"))),
        ("放置猴子", _pf_num(gp.get("monkeysPlaced"))),
        ("累计击破", _pf_num(bp.get("bloonsPopped"))),
        ("合作击破", _pf_num(bp.get("coopBloonsPopped"))),
        ("迷彩击破", _pf_num(bp.get("camosPopped"))),
        ("铅击破", _pf_num(bp.get("leadsPopped"))),
        ("紫击破", _pf_num(bp.get("purplesPopped"))),
        ("再生击破", _pf_num(bp.get("regrowsPopped"))),
        ("陶瓷击破", _pf_num(bp.get("ceramicsPopped"))),
        ("MOAB击破", _pf_num(bp.get("moabsPopped"))),
        ("BFB击破", _pf_num(bp.get("bfbsPopped"))),
        ("ZOMG击破", _pf_num(bp.get("zomgsPopped"))),
        ("DDT击破", _pf_num(bp.get("ddtsPopped"))),
        ("BAD击破", _pf_num(bp.get("badsPopped"))),
        ("漏气球", _pf_num(bp.get("bloonsLeaked"))),
        ("累计现金", _pf_num(gp.get("cashEarned"))),
        ("赠送现金", _pf_num(gp.get("coopCashGiven"))),
        ("技能使用", _pf_num(gp.get("abilitiesUsed"))),
        ("能量使用", _pf_num(gp.get("powersUsed"))),
        ("速生猴使用", _pf_num(gp.get("instaMonkeysUsed"))),
        ("每日宝箱", _pf_num(gp.get("dailyRewards"))),
        ("挑战完成", _pf_num(gp.get("challengesCompleted"))),
        ("成就", f"{_pf_num(public.get('achievements'))}/{ach_total}"),
        ("远征完成", _pf_num(gp.get("totalOdysseysCompleted"))),
        ("历史奖杯", _pf_num(gp.get("totalTrophiesEarned"))),
        ("死灵复活", _pf_num(bp.get("necroBloonsReanimated"))),
        ("变形药剂使用", _pf_num(bp.get("transformingTonicsUsed"))),
        ("最常用猴", util._esc(i18n.tower_cn(most))),
        ("最常用猴经验", _pf_num(most_xp)),
        ("速生收藏", f"{_pf_num(gp.get('instaMonkeyCollection'))}/{64 * 26}"),
        ("收藏宝箱开启", _pf_num(gp.get("collectionChestsOpened"))),
        ("金气球击破", _pf_num(bp.get("goldenBloonsPopped"))),
    ]
    api_rows = [
        ("出售猴子塔", _pf_num(st.get("totalTowersSold"))),
        ("每日挑战完成", _pf_num(sv.get("totalDailyChallengesCompleted"))),
        ("每日连击", _pf_num(sv.get("consecutiveDailyChallengesCompleted"))),
        ("竞速参赛", _pf_num(sv.get("totalRacesEntered"))),
        ("CT占领", _pf_num(st.get("ctCapturedTiles"))),
        ("挑战已玩", _pf_num(sv.get("challengesPlayed"))),
        ("挑战分享", _pf_num(sv.get("challengesShared"))),
        ("续关使用", _pf_num(sv.get("continuesUsed"))),
    ]

    def _mrow(k, v):
        return ("<div class='pf-mrow'><div class='pf-mlab'>" + util._esc(k) + "</div>"
                + "<div class='pf-mval'>" + v + "</div></div>")

    right = ("<div class='pf-panel'><div class='pf-title'>MAIN GAME STATS</div>"
             + "".join(_mrow(k, v) for k, v in main_rows) + "</div>"
             + "<div class='pf-panel' style='margin-top:14px;'>"
               "<div class='pf-title'>API Exclusive Stats</div>"
             + "".join(_mrow(k, v) for k, v in api_rows) + "</div>")
    right_h = 70 + len(main_rows) * 34 + 14 + 64 + len(api_rows) * 34
    rogue = _collect.profile_rogue(sv)
    if rogue:
        right += ("<div class='pf-panel' style='margin-top:14px;'>"
                  "<div class='pf-title'>ROGUE LEGENDS STATS</div>"
                  + "".join(_mrow(k, _pf_num(v)) for k, v in rogue) + "</div>")
        right_h += 14 + 64 + len(rogue) * 34
    frontier = _collect.profile_frontier(sv)
    if frontier:
        right += ("<div class='pf-panel' style='margin-top:14px;'>"
                  "<div class='pf-title'>FRONTIER LEGENDS STATS</div>"
                  "<div class='pf-sub' style='text-align:center;'>数据为全存档累计</div>"
                  + "".join(_mrow(k, _pf_num(v)) for k, v in frontier) + "</div>")
        right_h += 14 + 64 + 30 + len(frontier) * 34
    parts.append(
        "<div class='pf-cols'><div class='pf-col left'>" + left + "</div>"
        "<div class='pf-col right'>" + right + "</div></div>")

    total_h = 44 + head_h + max(left_h, right_h) + 18 + 30
    return _profile_shell("".join(parts), total_h)
