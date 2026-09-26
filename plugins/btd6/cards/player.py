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
    lb_badge = ""
    if col.get("lb_rank"):
        mode = util._esc(col.get("lb_variant_cn") or "")
        lb_badge = (
            f"<div class='panel lb-rank-badge'>排行榜第 {util._esc(col['lb_rank'])} 名"
            + (f" · {mode}" if mode else "") + "</div>"
        )
    head = (lb_badge + f"<div class='panel'>{banner}"
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
{common._font_face_css()}
/* 数字描边：Explorer 同款，不用伪加粗 */
.pf-topnum, .pf-medal .n, .pf-lvon, .pf-mval, .pf-curnum, .pf-foln {{
  font-weight: 400 !important; letter-spacing: 0 !important;
  text-shadow: 1px 0 0 #000,-1px 0 0 #000,0 1px 0 #000,0 -1px 0 #000,
              1px 1px 0 #000,-1px -1px 0 #000,-1px 1px 0 #000,1px -1px 0 #000;
}}
@page {{ size: {PROFILE_CARD_W}px {h}px; margin: 0; background: #6aa9d4; }}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ width: {PROFILE_CARD_W}px; height: {h}px; color: #ffffff;
        font-family: 'Luckiest Guy', 'ZCOOL QingKe HuangYou', 'WenQuanYi Micro Hei', 'Noto Sans CJK SC', sans-serif;
        background: linear-gradient(180deg, #8ec9e8 0%, #6aa9d4 55%, #4f93c4 100%); }}
.pf-page {{ padding: 22px 24px; }}
.pf-panel {{ background: #1e3a5c; border-radius: 14px; padding: 16px 20px;
             border: 1px solid #16304f;
             box-shadow: inset 0 1px 0 rgba(255,255,255,.08), 0 2px 0 rgba(10,30,55,.35); }}
.lb-rank-badge {{ color: #fff; background: linear-gradient(180deg,#2f8fce 0%,#1a5f96 100%);
  border: 2px solid #082f4f; border-radius: 10px; margin-bottom: 8px; padding: 8px 14px;
  font-size: 18px; font-weight: 900; text-align: center;
  text-shadow: 0 2px 0 #062a44; }}
.pf-title {{ color: #ffffff; font-size: 27px; line-height: 34px; font-weight: 900;
             text-align: center; letter-spacing: 2px; margin-bottom: 10px;
             text-shadow: 0 2px 0 #0d2138, 0 3px 4px rgba(0,0,0,.35); }}
.pf-banner {{ height: 150px; border-radius: 12px; background-size: cover;
              background-position: center; background-color: #16304f; }}
.pf-headgrid {{ display: table; width: 100%; margin-top: 14px; table-layout: fixed; }}
.pf-avcell {{ display: table-cell; width: 128px; vertical-align: middle; text-align: center; }}
.pf-avcell img {{ width: 112px; height: 112px; border-radius: 22px; border: 3px solid #ffd964;
                  background: #16304f; }}
.pf-hstrip-av .pf-av-fallback {{ width:78px; height:78px; line-height:74px; font-size:32px; }}
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
.pf-col {{ display: table-cell; vertical-align: top; width: 50%; }}
.pf-col.left {{ width: 48%; padding-right: 8px; }}
.pf-col.right {{ width: 50%; padding-left: 8px; }}
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
/* 奖章每行 5 枚 */
/* 奖章：缩小，一行 6 枚；数字压在图标右下角 */
.pf-medals {{ text-align: left; padding-top: 6px; padding-left: 2px; line-height: 0; }}
.pf-medal {{ display: inline-block; position: relative; width: 56px; height: 58px;
             margin: 1px 2px 4px 0; vertical-align: top; }}
.pf-medal img {{ display: block; width: 54px; height: 54px; margin: 0; }}
/* 奖章数字：Explorer .medal-text — Luckiest Guy 24px + 粗描边，叠在图标右下略靠中 */
.pf-medal .n {{ position: absolute; right: 2px; bottom: 2px; z-index: 5;
                color: #ffffff; font-size: 26px; line-height: 26px; font-weight: 400;
                font-family: 'Luckiest Guy', Arial, sans-serif;
                text-shadow:2px 0 0 #000,-2px 0 0 #000,0 2px 0 #000,0 -2px 0 #000,1.5px 1.5px 0 #000,-1.5px 1.5px 0 #000,1.5px -1.5px 0 #000,-1.5px -1.5px 0 #000,1px 0 0 #000,-1px 0 0 #000,0 1px 0 #000,0 -1px 0 #000,1px 2px 0 #000,-1px 2px 0 #000,2px 1px 0 #000,2px -1px 0 #000,-2px 1px 0 #000,-2px -1px 0 #000,0 3px 0 #000; }}
.pf-name{{text-transform:uppercase;}}
/* 顶部横幅条：背景=玩家 banner，一排 头像|名字|等级|老兵|粉丝 */
.pf-hstrip {{ position:relative; display:table; width:100%; height:104px; margin:0 0 12px;
             table-layout:fixed; border-radius:12px; overflow:hidden;
             background:#16304f center/cover no-repeat;
             border:2px solid #082f4f; box-shadow:0 2px 0 rgba(10,30,55,.35); }}
.pf-hstrip-av {{ display:table-cell; width:96px; vertical-align:middle; text-align:center;
                background:rgba(8,24,44,.35); }}
.pf-hstrip-av img {{ width:78px; height:78px; border-radius:12px; border:3px solid #ffd964;
                    background:#16304f; }}
.pf-hstrip-name {{ display:table-cell; vertical-align:middle; padding:0 10px 0 6px;
                  color:#ffffff; font-size:22px; font-weight:400; letter-spacing:1px;
                  text-shadow:1px 0 0 #000,-1px 0 0 #000,0 1px 0 #000,0 -1px 0 #000;
                  background:rgba(8,24,44,.28); word-break:break-all; }}
.pf-hstrip-lvl {{ display:table-cell; width:72px; vertical-align:middle; text-align:center;
                 background:rgba(8,24,44,.22); }}
.pf-hstrip-fol {{ display:table-cell; width:150px; vertical-align:middle; text-align:center;
                 background:rgba(8,24,44,.35); padding:0 8px; }}
.pf-vetstar {{ }}
.pf-htab {{display:table; width:100%; margin-top:14px; table-layout:fixed;}}
.pf-hav {{display:table-cell; width:118px; vertical-align:middle; text-align:center;}}
.pf-hav img {{width:112px; height:112px; border-radius:22px; border:3px solid #ffd964; background:#16304f;}}
.pf-hmid {{display:table-cell; vertical-align:top; padding:0 0 0 2px;}}
.pf-hright {{display:table-cell; width:300px; vertical-align:top;}}
.pf-fol {{text-align:right; color:#9fb6d4; font-size:16px; line-height:20px; font-weight:700; letter-spacing:2px;}}
.pf-foln {{text-align:right; color:#ffffff; font-size:24px; line-height:28px; font-weight:900;
          text-shadow:0 2px 0 #0d2138;}}
.pf-lvrow {{display:table; width:100%; margin-top:2px; table-layout:fixed;}}
.pf-lvstar {{display:table-cell; width:68px; vertical-align:middle; text-align:center; height:68px; background-repeat:no-repeat; background-position:center; background-size:contain; position:relative;}}
.pf-lvstar img {{width:52px; height:52px;}}
.pf-lvon {{color:#ffffff; font-size:32px; line-height:68px; font-weight:400; text-align:center;
            text-shadow:2px 0 0 #0d2138, -2px 0 0 #0d2138, 0 2px 0 #0d2138, 0 -2px 0 #0d2138,
                        1px 1px 0 #0d2138, -1px 1px 0 #0d2138, 1px -1px 0 #0d2138, -1px -1px 0 #0d2138,
                        0 3px 4px rgba(0,0,0,.55);}}
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
/* TOP 区：Explorer 同款，每行 3 枚、左对齐 */
.pf-topgrid {{text-align:left; padding:2px 0 6px; line-height:0;}}
.pf-topcell {{display:inline-block; width:32%; height:128px; margin:2px 1% 2px 0;
             vertical-align:top; text-align:center;
             background-repeat:no-repeat; background-position:center top; background-size:contain;
             line-height:20px;}}
.pf-topimg {{display:block; height:108px; max-width:120px; margin:4px auto 0; object-fit:contain;}}
.pf-topnum {{color:#ffffff; font-size:20px; line-height:24px; font-weight:400;
            text-shadow:1px 0 0 #000,-1px 0 0 #000,0 1px 0 #000,0 -1px 0 #000,
                        1px 1px 0 #000,-1px -1px 0 #000;}}
/* 猴塔/模范/技能数量：压在图标右下角（参考 Explorer 奖章） */
/* 英雄/猴塔数字：方框正好右下角 */
.pf-topbadge {{ position:absolute; right:2px; bottom:2px; z-index:6;
              color:#ffffff; font-size:26px; line-height:26px; font-weight:400;
              font-family: 'Luckiest Guy', Arial, sans-serif;
              text-shadow:2px 0 0 #000,-2px 0 0 #000,0 2px 0 #000,0 -2px 0 #000,1.5px 1.5px 0 #000,-1.5px 1.5px 0 #000,1.5px -1.5px 0 #000,-1.5px -1.5px 0 #000,1px 0 0 #000,-1px 0 0 #000,0 1px 0 #000,0 -1px 0 #000,1px 2px 0 #000,-1px 2px 0 #000,2px 1px 0 #000,2px -1px 0 #000,-2px 1px 0 #000,-2px -1px 0 #000,0 3px 0 #000; }}
.pf-topcell.has-frame {{ position:relative; }}
.pf-topcell.has-frame .pf-topimg {{ margin-bottom:0; }}
.pf-col.left {{ width: 49%; }}
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
    # 排行榜点名等精简档案：只留等级图标，去掉经验条/无数据货币/SHOW ALL
    compact = bool(col.get("lb_rank") or col.get("compact_profile"))

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
    av_html = (f"<img src='{util._esc(av)}'/>") if av else "<div class='pf-av-fallback'>?</div>"
    # 无 OAK 存档时（排行榜点名 / 公开档案）用公开 rank 字段
    if sv and (sv.get("xp") is not None or sv.get("veteranXp") is not None):
        lvl, _, _ = _collect.profile_rank_info(sv)
        vet, _, _ = _collect.profile_veteran_info(sv)
        has_vet_xp = int(sv.get("veteranXp") or 0) > 0
    else:
        try:
            lvl = int(public.get("rank") or 1)
        except (TypeError, ValueError):
            lvl = 1
        try:
            vet = int(public.get("veteranRank") or 0)
        except (TypeError, ValueError):
            vet = 0
        has_vet_xp = vet > 0
    _lvl_bg = _assets._site_asset_data_url("UI/LvlHolder.webp")
    _lvl_style = (f" style='background-image:url(&quot;{util._esc(_lvl_bg)}&quot;);'") if _lvl_bg else ""
    lvl_cell = f"<div class='pf-lvstar'{_lvl_style}><div class='pf-lvon'>{lvl}</div></div>"
    _vet_bg = _assets._site_asset_data_url("UI/LvlHolderVeteran.webp")
    _vet_style = (f" style='background-image:url(&quot;{util._esc(_vet_bg)}&quot;);'") if _vet_bg else ""
    # 老兵星：有等级就显示（公开 rank 有 veteranRank）
    vet_cell = ""
    if has_vet_xp or vet:
        vet_cell = f"<div class='pf-lvstar pf-vetstar'{_vet_style}><div class='pf-lvon'>{vet}</div></div>"
    bn_style = f" style='background-image:url(&quot;{util._esc(bn)}&quot;);'" if bn else ""
    # 一排：头像 | 名字 | 等级 | 老兵 | 粉丝（参考 Explorer 玩家条）
    head = (
        f"<div class='pf-hstrip'{bn_style}>"
        f"<div class='pf-hstrip-av'>{av_html}</div>"
        f"<div class='pf-hstrip-name'>{util._esc(str(p.get('displayName') or '').upper())}</div>"
        f"<div class='pf-hstrip-lvl'>{lvl_cell}</div>"
        f"<div class='pf-hstrip-lvl'>{vet_cell}</div>"
        f"<div class='pf-hstrip-fol'>"
        f"<div class='pf-fol'>粉丝</div>"
        f"<div class='pf-foln'>{util._esc(util.fmt_cn_num(p.get('followers')))}</div>"
        f"</div></div>"
    )
    head_h = 110
    if col.get("lb_rank"):
        mode = util._esc(col.get("lb_variant_cn") or "")
        parts.append("<div class='pf-panel' style='background:linear-gradient(180deg,#2f8fce,#1a5f96);"
                     "border:2px solid #082f4f;text-align:center;padding:10px 12px;"
                     "color:#ffffff;font-size:18px;font-weight:900;"
                     "text-shadow:0 2px 0 #062a44;'>"
                     f"排行榜第 {util._esc(col['lb_rank'])} 名"
                     + (f" · {mode}" if mode else "") + "</div>")
        head_h += 52
    parts.append(head)

    # ---- 左列 快捷统计 ----
    quick = _collect.profile_quick_stats(sv, public)
    qrows = []
    for icon, text in quick:
        durl = _assets._site_asset_data_url(icon)
        img = (f"<img src='{util._esc(durl)}'/>") if durl else ""
        qrows.append(
            "<div class='pf-qrow'><div class='pf-qicon'>" + img + "</div>"
            + "<div class='pf-qtxt'>" + util._esc(text) + "</div></div>")
    left = ("<div class='pf-panel'><div class='pf-title'>快捷统计</div>"
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

    cur = ""
    if compact and not sv:
        cur = ("<div class='pf-panel' style='margin-top:14px;'>"
               "<div class='pf-title'>奖章</div>")
    else:
        cur = ("<div class='pf-panel' style='margin-top:14px;'>"
               "<div class='pf-title'>货币与奖章</div>"
               "<div class='pf-cur'>"
               + _cur(mm, ("$" + _pf_num(sv.get("monkeyMoney"))), "#bfff3c")
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
    left_h += (64 + med_rows * 84) if (compact and not sv) else (80 + 100 + 64 + med_rows * 84)

    # ---- 左列 TOP 区 ----
    tops = _collect.profile_tops(public)

    def _topcell(bg, img, num):
        style = (f" style='background-image:url(&quot;{util._esc(bg)}&quot;);'") if bg else ""
        pic = (f"<img class='pf-topimg' src='{util._esc(img)}'/>") if img else ""
        # 有容器底时数字放框内左上角角标（Explorer towerTopLeft）；英雄无容器则放图下
        # 数字一律压在图标右下角（与奖章一致）
        return (f"<div class='pf-topcell has-frame'" + style + ">" + pic
                + f"<div class='pf-topbadge'>{num:,}</div></div>")

    def _topsec(title, cells, purple=False):
        # 注意：format 必须作用在完整字符串上；中途 + show 会让 .format 只吃到最后一段
        show = "" if compact else "<div class='pf-showall'>SHOW ALL</div>"
        cls = " p" if purple else ""
        return (
            f"<div class='pf-panel' style='margin-top:14px;padding-left:10px;padding-right:10px;'>"
            f"<div class='pf-ribbon{cls}'><span>{util._esc(title)}</span></div>"
            f"{show}"
            f"<div class='pf-topgrid'>"
            + "".join(cells) + "</div></div>"
        )

    def _tower_bg():
        return _assets._site_asset_data_url("UI/InstaTowersContainer.webp")

    def _tower_img(t):
        n = "Wizard" if t == "WizardMonkey" else t
        u = _assets._site_asset_data_url(f"UI/InstaContainer/000-{n}.webp")
        if not u:
            u = _assets._game_asset_data_url(f"000-{n}.webp")
        return u

    # 英雄也带外框（Explorer 同款金色容器），避免只有立绘显得“框不见了”
    _hero_bg = _assets._site_asset_data_url("UI/InstaTowersContainerGold.webp")
    hcells = [(_topcell(_hero_bg, _assets._site_asset_data_url(f"Portrait/{h}Portrait.webp"), n))
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
    left += _topsec("热门英雄", hcells)
    left += _topsec("热门猴塔", tcells)
    left += _topsec("热门模范", pcells, purple=True)
    left += _topsec("热门技能", acells)
    left_h += 4 * 260

    # ---- 右列 主要游戏数据 ----
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

    right = ("<div class='pf-panel'><div class='pf-title'>主要游戏数据</div>"
             + "".join(_mrow(k, v) for k, v in main_rows) + "</div>"
             + "<div class='pf-panel' style='margin-top:14px;'>"
               "<div class='pf-title'>API 专属数据</div>"
             + "".join(_mrow(k, v) for k, v in api_rows) + "</div>")
    right_h = 70 + len(main_rows) * 34 + 14 + 64 + len(api_rows) * 34
    rogue = _collect.profile_rogue(sv)
    if rogue:
        right += ("<div class='pf-panel' style='margin-top:14px;'>"
                  "<div class='pf-title'>Rogue Legends 数据</div>"
                  + "".join(_mrow(k, _pf_num(v)) for k, v in rogue) + "</div>")
        right_h += 14 + 64 + len(rogue) * 34
    frontier = _collect.profile_frontier(sv)
    if frontier:
        right += ("<div class='pf-panel' style='margin-top:14px;'>"
                  "<div class='pf-title'>Frontier Legends 数据</div>"
                  "<div class='pf-sub' style='text-align:center;'>数据为全存档累计</div>"
                  + "".join(_mrow(k, _pf_num(v)) for k, v in frontier) + "</div>")
        right_h += 14 + 64 + 30 + len(frontier) * 34
    # 左右栏底边对齐：把高度差摊到较矮一侧各面板的 padding-bottom
    # 左右底边对齐：高度差摊到较矮栏各面板；差很小时也补 6px 防止视觉错位
    def _pad_side(html: str, first_title: str, need: int) -> str:
        n = max(1, html.count("pf-panel"))
        extra = max(4, min(28, int(need / n)))
        html = html.replace(
            f"<div class='pf-panel'><div class='pf-title'>{first_title}</div>",
            f"<div class='pf-panel' style='padding-bottom:{extra}px;'><div class='pf-title'>{first_title}</div>",
            1,
        )
        html = html.replace(
            "style='margin-top:14px;'",
            f"style='margin-top:14px;padding-bottom:{extra}px;'",
        )
        return html

    if left_h >= right_h:
        right = _pad_side(right, "主要游戏数据", left_h - right_h)
    else:
        left = _pad_side(left, "快捷统计", right_h - left_h)
    col_h = max(left_h, right_h)
    parts.append(
        "<div class='pf-cols'>"
        f"<div class='pf-col left' style='min-height:{col_h}px;'>{left}</div>"
        f"<div class='pf-col right' style='min-height:{col_h}px;'>{right}</div>"
        "</div>")

    total_h = 44 + head_h + max(left_h, right_h) + 80 + 360
    return _profile_shell("".join(parts), total_h)
