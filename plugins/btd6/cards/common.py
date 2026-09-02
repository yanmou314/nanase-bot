"""卡片公共件：HTML 转义、背景图、四种外壳（通用/远征/竞速）与竞速强化部件。"""
import base64

from .. import assets, rushgen, util


CARD_W = 900


_bg_cache: dict[str, str] = {}


def _bg_data_url() -> str:
    """256px 竖向渐变条，CSS 拉伸铺满整页；避免逐像素生成整页大图（渲染慢的主因之一）。"""
    hit = _bg_cache.get("bg")
    if hit:
        return hit
    from PIL import Image

    top, bottom = (0, 43, 86), (0, 59, 119)
    strip = Image.new("RGB", (1, 256))
    for y in range(256):
        t = y / 255
        strip.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    import io

    buf = io.BytesIO()
    strip.save(buf, "PNG")
    url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    _bg_cache["bg"] = url
    return url


def _shell(body: str, h: int) -> str:
    """详情页风格外壳（近黑深蓝底 + #213753 面板）；玩家档案/帮助菜单使用。

    几何尺寸（字号/内边距）与旧浅色版一致，player/help 的高度估算不受影响。"""
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
@page {{ size: {CARD_W}px {h}px; margin: 0; background: linear-gradient(180deg, #46c8f1 0%, #129ed0 56%, #087eaf 100%); }}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ width: {CARD_W}px; height: {h}px; font-family: "WenQuanYi Micro Hei", "Noto Sans CJK SC", sans-serif;
       background: linear-gradient(180deg, #46c8f1 0%, #129ed0 56%, #087eaf 100%); color: #ffffff; }}
.card {{ padding: 26px 40px; }}
.panel {{ background: #213753; border-radius: 18px; margin-top: 16px; padding: 14px 36px;
          border: 1px solid #2f4a6d; }}
.panel:first-child {{ margin-top: 0; }}
.phead {{ display: table; width: 100%; }}
.ptext {{ display: table-cell; vertical-align: middle; }}
.pimg {{ display: table-cell; vertical-align: middle; width: 200px; text-align: right; }}
.pimg img {{ max-width: 185px; max-height: 118px; border-radius: 12px; }}
.ptitle {{ font-size: 27px; font-weight: 700; color: #ffffff; padding: 8px 0 2px; }}
.big {{ font-size: 31px; font-weight: 700; color: #ffffff; padding: 6px 0 2px; word-break: break-all; }}
.sub {{ font-size: 22px; color: #9fb6d4; padding-top: 2px; }}
.li {{ font-size: 23px; color: #cfe0f2; padding: 11px 0; border-bottom: 1px solid #2f4a6d; line-height: 1.45; word-break: break-all; }}
.li:last-child {{ border-bottom: none; }}
.hl {{ font-weight: 600; }}
.row {{ display: table; width: 100%; padding: 17px 0; border-bottom: 1px solid #2f4a6d; }}
.row:last-child {{ border-bottom: none; }}
.rank {{ display: table-cell; vertical-align: middle; width: 64px; font-size: 26px; font-weight: 700; color: #6d84a3; }}
.name {{ display: table-cell; vertical-align: middle; padding: 0 18px; font-size: 27px; font-weight: 600;
         color: #ffffff; word-break: break-all; }}
.score {{ display: table-cell; vertical-align: middle; text-align: right; width: 200px;
          font-size: 28px; font-weight: 700; color: #ffffff; }}
.date {{ display: table-cell; vertical-align: middle; text-align: right; width: 200px;
         font-size: 20px; color: #9fb6d4; }}
.empty {{ text-align: center; font-size: 24px; color: #8fa8c4; padding: 26px 0; }}
.evname {{ font-size: 36px; font-weight: 700; color: #ffffff; word-break: break-all; }}
.evpre {{ font-size: 22px; font-weight: 600; color: #7fd0e8; padding-bottom: 3px; }}
.evsub {{ font-size: 24px; color: #9fb6d4; padding-top: 4px; }}
.ticon {{ display: table-cell; width: 110px; vertical-align: middle; }}
.ticon img {{ width: 92px; border-radius: 14px; }}
.mapcell {{ display: table-cell; width: 420px; vertical-align: top; }}
.mapcell img {{ width: 400px; border-radius: 14px; border: 3px solid #699bd9; }}
.nomap {{ width: 400px; height: 200px; line-height: 200px; text-align: center; font-size: 60px;
          background: #344d6c; border-radius: 14px; }}
.stats {{ display: table-cell; vertical-align: top; padding-left: 20px; }}
.scol {{ display: table-cell; width: 50%; vertical-align: top; }}
.st {{ font-size: 21px; color: #cfe0f2; padding: 8px 0 8px 14px; white-space: nowrap; }}
.st b {{ color: #ffffff; font-size: 23px; }}
.duo {{ display: table; width: 100%; }}
.dcell {{ display: table-cell; width: 50%; vertical-align: top; }}
.dcell .panel {{ margin-left: 0; margin-right: 0; }}
.dcell:first-child .panel {{ margin-right: 8px; }}
.dcell:last-child .panel {{ margin-left: 8px; }}
.mkgrid {{ text-align: center; padding: 4px 0 0; }}
.mkwrap {{ display: inline-block; margin: 4px 3px; vertical-align: top; }}
.mk {{ position: relative; width: 96px; }}
.mk img {{ width: 94px; height: 94px; border-radius: 14px; border: 2px solid #699bd9; }}
.mk .nm {{ font-size: 16px; color: #cfe0f2; padding-top: 3px; text-align: center;
           white-space: nowrap; overflow: hidden; }}
.mk .bd {{ position: absolute; top: 0; left: 0; width: 94px; height: 94px;
           text-align: center; background: rgba(215, 52, 52, .50); border-radius: 14px; }}
.mk .bd span {{ display: block; margin-top: 14px; font-size: 56px; font-weight: 700;
                line-height: 66px; color: #ffffff; }}
.mk .lim {{ position: absolute; top: 3px; right: 3px; background: #006e7f; color: #ffffff;
            font-size: 20px; font-weight: 700; border-radius: 9px; padding: 1px 8px; }}
.mk .pth {{ position: absolute; top: 60px; left: 0; width: 94px; text-align: center; }}
.mk .pth span {{ background: rgba(0, 110, 127, .92); color: #ffffff; font-size: 19px;
                 font-weight: 700; border-radius: 8px; padding: 2px 8px; }}
.mk.txt {{ width: 96px; min-height: 94px; background: #344d6c; border-radius: 14px;
           font-size: 19px; color: #cfe0f2; text-align: center; padding: 24px 2px;
           box-sizing: border-box; }}
.mktxt {{ font-size: 15px; color: #9fb6d4; text-align: center; padding-top: 2px; }}
.pimg.lg {{ width: 250px; }}
.pimg.lg img {{ width: 235px; max-height: 235px; }}
.mrow {{ display: table; width: 100%; padding: 12px 0; border-bottom: 1px solid #2f4a6d; }}
.mrow:last-child {{ border-bottom: none; }}
.mrow .rank {{ display: table-cell; vertical-align: middle; width: 56px; font-size: 24px;
               font-weight: 700; color: #6d84a3; }}
.mthumb {{ display: table-cell; vertical-align: middle; width: 150px; }}
.mthumb img {{ width: 138px; border-radius: 10px; border: 2px solid #699bd9; }}
.nomap-s {{ width: 138px; height: 92px; line-height: 88px; text-align: center; font-size: 40px;
            background: #344d6c; border-radius: 10px; }}
.mname {{ display: table-cell; vertical-align: middle; padding-left: 18px; font-size: 25px;
          font-weight: 600; color: #ffffff; word-break: break-all; text-align: left; }}
.msub {{ font-size: 18px; color: #9fb6d4; font-weight: 400; padding-top: 4px; }}
.pbanner img {{ width: 100%; border-radius: 14px; }}
.pavatar {{ display: table-cell; width: 140px; vertical-align: middle; }}
.pavatar img {{ width: 112px; border-radius: 50%; }}
.bimg {{ display: table-cell; width: 180px; height: 112px; vertical-align: middle; text-align: center; }}
.bimg img {{ max-width: 160px; max-height: 106px; border-radius: 12px; }}
.bimg-ph {{ width: 158px; height: 104px; line-height: 100px; text-align: center; font-size: 54px;
            background: #344d6c; border-radius: 12px; }}
.btext {{ display: table-cell; vertical-align: middle; padding-left: 20px; }}
.brow {{ display: table; width: 100%; }}
.bname {{ display: table-cell; font-size: 30px; font-weight: 700; color: #ffffff;
          word-break: break-all; }}
.badge {{ display: table-cell; text-align: right; vertical-align: middle; width: 128px; }}
.badge span {{ display: inline-block; padding: 4px 16px; border-radius: 18px; font-size: 20px;
               font-weight: 600; color: #ffffff; }}
.st-on {{ background: #2f9e63; }}
.st-up {{ background: #e08a2e; }}
.st-off {{ background: #6d84a3; }}
.hrow {{ padding: 15px 0 13px; border-bottom: 1px dashed #2f4a6d; }}
.hrow:last-child {{ border-bottom: none; }}
.chip {{ display: inline-block; background: #11a6c5; color: #ffffff; font-size: 21px;
         font-weight: 700; padding: 6px 16px; border-radius: 18px; word-break: break-all;
         letter-spacing: 0.5px; }}
.hdesc {{ font-size: 21px; color: #cfe0f2; padding-top: 9px; }}
.bdates {{ font-size: 21px; color: #9fb6d4; padding-top: 5px; }}
.bscore {{ font-size: 22px; color: #cfe0f2; padding-top: 7px; }}
.bscore b {{ color: #ffffff; }}
</style></head>
<body><div class="card">
{body}
</div></body></html>"""


ODYSSEY_CARD_W = 800


def _odyssey_shell(body: str, h: int, theme: str = "teal") -> str:
    """远征卡片外壳：羊皮纸面板体系；页面背景沿用原版主题色（tan 羊皮纸 / teal 深青）。

    theme 参数与原版一致：主卡用 tan（#e4d0bc 底 + 深棕字），空态等用默认 teal。"""
    page_bg = "#e4d0bc" if theme == "tan" else "#0b7180"
    text_color = "#4a3c28" if theme == "tan" else "#ffffff"
    muted = "#70543c" if theme == "tan" else "#cfe0f2"
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
@page {{ size: {ODYSSEY_CARD_W}px {h}px; margin: 0; background: {page_bg}; }}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html, body {{ width: {ODYSSEY_CARD_W}px; height: {h}px; color: {text_color};
        font-family: "WenQuanYi Micro Hei", "Noto Sans CJK SC", sans-serif;
        background: {page_bg}; margin: 0; padding: 0; overflow: hidden; }}
.ody-page {{ width: {ODYSSEY_CARD_W}px; height: {h}px; padding: 12px 12px 14px;
             background: {page_bg}; box-sizing: border-box; }}
.ody-paper {{ width: 100%; min-height: 100%; padding: 0; overflow: hidden; margin: 0;
              background: {page_bg}; border: 0; box-sizing: border-box; }}

/* ===== 羊皮纸标题横幅 ===== */
.ody-title-banner {{ background: linear-gradient(180deg, #b9a284 0%, #ab927c 42%, #936e44 100%);
                    border: 3px solid #6e4f2c; border-radius: 10px; padding: 10px 16px 9px;
                    text-align: center;
                    box-shadow: inset 0 1px 0 rgba(255,246,236,.35), 0 2px 0 rgba(20,8,2,.35); }}
.ody-title {{ color: #fff6ec; font-size: 22px; line-height: 28px; font-weight: 900;
              text-shadow: 0 2px 0 #4a3218; word-break: break-all; }}
.ody-title-sub {{ color: #f4e4cd; font-size: 13px; line-height: 18px; font-weight: 700;
                  padding-top: 2px; text-shadow: 0 1px 0 #4a3218; }}

/* ===== 难度选项卡（选中黄 #fecb00） ===== */
.ody-tabs {{ display: table; width: 100%; margin-top: 10px; table-layout: fixed; }}
.ody-tab-cell {{ display: table-cell; width: 33.333%; padding: 0 4px; vertical-align: middle; }}
.ody-tab {{ display: table; width: 100%; height: 58px; table-layout: fixed;
            background: #ded5c0; border: 2px solid #9a8a6a; border-radius: 10px;
            box-shadow: inset 0 1px 0 rgba(255,255,255,.4), 0 2px 0 rgba(20,8,2,.28); }}
.ody-tab.sel {{ background: linear-gradient(180deg, #ffd83d 0%, #fecb00 58%, #e6ad00 100%);
                border-color: #b98a00; }}
.ody-tab-icon-cell {{ display: table-cell; width: 62px; text-align: center; vertical-align: middle; }}
.ody-tab-icon {{ width: 40px; height: 40px; object-fit: contain; }}
.ody-tab-fallback {{ font-size: 30px; line-height: 40px; }}
.ody-tab-label {{ display: table-cell; vertical-align: middle; text-align: left; color: #4a3218;
                  font-size: 18px; font-weight: 900; letter-spacing: 1px; }}

/* ===== 顶部三条蓝丝带（带切口 + 阴影 + 图标） ===== */
.ody-ribbons {{ display: table; width: 100%; height: 38px; table-layout: fixed; text-align: center; }}
.ody-ribbon-cell {{ display: table-cell; width: 33.333%; padding: 0 5px; vertical-align: middle; }}
.ody-ribbon {{ position: relative; display: inline-block; height: 31px; padding: 4px 18px; color: #ffffff;
               font-size: 15px; line-height: 21px; font-weight: 900; white-space: nowrap;
               text-shadow: 0 2px 0 #075b8b; letter-spacing: 0.5px;
               background: linear-gradient(180deg, #46c8f1 0%, #129ed0 56%, #087eaf 100%);
               border: 2px solid #076b99; border-radius: 2px;
               box-shadow: inset 0 1px 0 rgba(255,255,255,.75), 0 2px 0 rgba(0,59,79,.28); }}
.ody-ribbon::before, .ody-ribbon::after {{ content: ""; position: absolute; top: 100%; width: 0; height: 0;
                                          border-style: solid; }}
.ody-ribbon::before {{ left: -2px; border-width: 0 0 6px 6px; border-color: transparent transparent #053a55 transparent; }}
.ody-ribbon::after  {{ right: -2px; border-width: 6px 6px 0 0; border-color: #053a55 transparent transparent transparent; }}
.ody-ribbon-icon {{ display: inline-block; width: 18px; height: 18px; margin-right: 4px; vertical-align: -4px;
                    object-fit: contain; filter: drop-shadow(0 1px 0 #075b8b); }}

/* 面板通用：棕底圆角 + 内/外阴影 */
.ody-panel {{ position: relative; background: #ab927c; border: 1px solid rgba(81,52,30,.18); border-radius: 9px;
              box-shadow: inset 0 1px 0 rgba(255,244,222,.30), 0 2px 0 rgba(84,53,30,.20); padding-top: 22px; }}
/* 面板顶部蓝丝带标题（默认队伍 / 奖励 / 岛屿规则 等） */
.ody-panel-title, .ody-section-banner {{ position: absolute; top: -10px; left: 12px; height: 26px; padding: 0 22px;
                                          color: #ffffff; font-size: 14px; line-height: 26px; font-weight: 900;
                                          text-shadow: 0 1px 0 #075b8b; letter-spacing: 0.5px;
                                          background: linear-gradient(180deg, #46c8f1 0%, #129ed0 56%, #087eaf 100%);
                                          border: 2px solid #076b99; border-radius: 2px;
                                          box-shadow: inset 0 1px 0 rgba(255,255,255,.75), 0 2px 0 rgba(0,59,79,.28); }}
.ody-panel-title::before, .ody-panel-title::after,
.ody-section-banner::before, .ody-section-banner::after {{ content: ""; position: absolute; top: 100%; width: 0; height: 0;
                                                          border-style: solid; }}
.ody-panel-title::before, .ody-section-banner::before {{ left: -2px; border-width: 0 0 6px 6px;
                                                          border-color: transparent transparent #053a55 transparent; }}
.ody-panel-title::after,  .ody-section-banner::after  {{ right: -2px; border-width: 6px 6px 0 0;
                                                          border-color: #053a55 transparent transparent transparent; }}
.ody-paper > .ody-section-banner:first-child {{ margin-top: 0; }}
.ody-section-banner {{ position: relative; top: 0; left: 0; display: block; width: max-content; margin: 14px auto 6px;
                        padding: 0 24px; font-size: 15px; height: 30px; line-height: 30px; }}

/* 活动名行 */
.ody-event {{ height: 24px; padding: 6px 16px 0; color: #5a4530; font-size: 15px; line-height: 20px;
              font-weight: 900; text-align: center; white-space: nowrap; overflow: hidden; }}
.ody-event-desc {{ padding: 0 24px 2px; color: {muted}; font-size: 12px; line-height: 16px; font-weight: 700;
                   text-align: center; max-height: 32px; overflow: hidden; }}
.ody-extreme-badge {{ margin: 2px auto 0; width: max-content; padding: 1px 12px; color: #ffffff;
                      font-size: 12px; line-height: 18px; font-weight: 900; letter-spacing: 0.5px;
                      background: linear-gradient(180deg, #ff6b3a 0%, #d63a14 56%, #a02800 100%);
                      border: 2px solid #7a1a00; border-radius: 10px;
                      box-shadow: inset 0 1px 0 rgba(255,255,255,.5), 0 1px 0 rgba(0,0,0,.25); }}

/* 上下两栏：队伍 + 奖励 */
.ody-top-grid {{ display: table; width: 100%; padding: 6px 13px 0; table-layout: fixed; }}
.ody-top-cell {{ display: table-cell; vertical-align: top; }}
.ody-top-cell.crew {{ width: 70%; padding-right: 8px; }}
.ody-top-cell.reward {{ width: 30%; padding-left: 8px; }}

/* ===== 默认队伍 ===== */
.ody-crew-panel {{ min-height: 204px; padding: 8px 8px 8px; }}
.ody-crew-body {{ display: flex; align-items: stretch; gap: 0; width: 100%; min-height: 169px; }}
.ody-crew-hero .ody-unit-wrap {{ width: 84px; height: 104px; margin: 27px 12px 0 0; }}
.ody-crew-hero {{ flex: none; width: 88px; display: flex; align-items: center; justify-content: center;
                  background: linear-gradient(180deg, #b58a52 0%, #a37c48 55%, #936e44 100%);
                  border-radius: 8px 0 0 8px;
                  box-shadow: inset 0 1px 0 rgba(255,255,255,.15); }}
.ody-crew-grid-cell {{ flex: 1; min-width: 0; padding: 12px 0 8px 10px; white-space: nowrap;
                        background: #936e44;
                        border-radius: 0 8px 8px 0;
                        box-shadow: inset 0 1px 0 rgba(255,244,222,.25); }}
.ody-default-grid {{ text-align: left; white-space: nowrap; }}
.ody-default-grid .ody-unit-wrap {{ width: 46px; height: 58px; }}
.ody-default-grid .ody-unit-card {{ width: 44px; height: 54px; border-radius: 9px; }}

/* ===== 塔卡（通用：可用 + 默认队伍 共用） ===== */
.ody-unit-wrap {{ display: inline-block; width: 55px; height: 67px; margin: 0 1px 3px; vertical-align: top; }}
.ody-unit-card {{ position: relative; width: 55px; height: 64px; overflow: hidden; border: 2px solid rgba(0,0,0,.35);
                  border-radius: 11px;
                  background: radial-gradient(circle at 50% 30%, #f5a82a, #dc7410 75%);
                  box-shadow: inset 0 1px 0 rgba(255,255,255,.65), 0 2px 0 rgba(0,0,0,.25); }}
/* 分类底色（远征可用区按塔分类上色，与 Explorer 网格一致） */
.ody-unit-card.cat-primary  {{ background: linear-gradient(180deg, #7cc4f4 0%, #4aa8e8 58%, #2d7cc0 100%);
                                border-color: #ffffff; }}
.ody-unit-card.cat-military {{ background: linear-gradient(180deg, #8fd07f 0%, #58b64f 58%, #358a3f 100%);
                                border-color: #ffffff; }}
.ody-unit-card.cat-magic    {{ background: linear-gradient(180deg, #c89aec 0%, #9c5fd4 58%, #7a3fb8 100%);
                                border-color: #ffffff; }}
.ody-unit-card.cat-support  {{ background: linear-gradient(180deg, #f6c169 0%, #e8940f 58%, #c06f0a 100%);
                                border-color: #ffffff; }}
.ody-unit-card.cat-hero     {{ background: linear-gradient(180deg, #ffe95c 0%, #fecb00 58%, #dd9f00 100%);
                                border-color: #ffffff; }}
.ody-unit-card.hero {{ background: linear-gradient(180deg, #ffe95c 0%, #fecb00 58%, #dd9f00 100%); }}
.ody-unit-card img {{ display: block; width: 51px; height: 51px; margin: 0 auto; object-fit: contain;
                       filter: drop-shadow(0 1px 0 rgba(0,0,0,.15)); }}
.ody-crew-panel .ody-unit-card.big {{ width: 84px; height: 104px; border-radius: 14px;
                       border: 3px solid #eab607;
                       background: linear-gradient(180deg, #f2e3bd 0%, #e5cd9d 70%, #d5b87e 100%); }}
.ody-unit-card.big img {{ width: 80px; height: 80px; }}
/* 数量徽章（默认队伍用：右上） */
.ody-unit-quantity {{ position: absolute; right: 1px; top: 1px; min-width: 25px; padding: 1px 3px;
                      color: #ffffff; background: #006e7f; border: 2px solid #ffffff; border-radius: 12px;
                      font-size: 10px; line-height: 12px; font-weight: 900; text-align: center;
                      text-shadow: 0 1px 0 #033a44; }}
/* 数量徽章（可用区用：左上） */
.ody-unit-quantity.left {{ right: auto; left: 1px; }}
/* 升级上限（可用塔卡片底部红底白字，如 4-4-4） */
.ody-unit-caps {{ position: absolute; left: 1px; right: 1px; bottom: 2px; text-align: center;
                  color: #ffffff; background: rgba(166, 39, 26, .80); border-radius: 4px;
                  font-size: 10px; line-height: 13px; font-weight: 900;
                  text-shadow: 0 1px 0 rgba(40,6,2,.8); letter-spacing: 0.5px; }}
.ody-unit-fallback {{ color: #fff; font-size: 9px; line-height: 11px; font-weight: 900; text-align: center;
                      padding: 14px 2px; word-break: break-all; text-shadow: 0 1px 0 #000; }}

/* ===== 奖励面板 ===== */
.ody-reward-panel {{ min-height: 204px; padding: 16px 8px 8px; }}
.ody-reward-grid {{ display: table; width: 100%; height: 150px; table-layout: fixed; text-align: center; }}
.ody-reward-cell {{ display: table-cell; width: 33.333%; vertical-align: middle; }}
.ody-reward-icon {{ display: block; width: 64px; height: 64px; margin: 0 auto 4px; object-fit: contain;
                    filter: drop-shadow(0 2px 0 rgba(0,0,0,.25)); }}
.ody-reward-emoji {{ display: block; height: 64px; font-size: 42px; line-height: 64px; }}
.ody-reward-value {{ color: #ffffff; font-size: 15px; line-height: 19px; font-weight: 900;
                     text-shadow: 0 2px 0 #5d4631; }}

/* ===== 可用英雄/猴子/力量 ===== */
.ody-available {{ display: table; width: 100%; padding: 12px 7px 0; table-layout: fixed;
                  border-spacing: 7px 0; }}
.ody-av-panel {{ display: table-cell; vertical-align: top; padding: 14px 7px 8px; }}
.ody-av-panel.heroes {{ width: 21%; }}
.ody-av-panel.towers {{ width: 47%; }}
.ody-av-panel.powers {{ width: 32%; }}
.ody-av-title {{ height: 22px; color: #ffffff; font-size: 15px; line-height: 22px; font-weight: 900;
                 text-shadow: 0 1px 0 #58432f; text-align: center; white-space: nowrap; }}
.ody-av-title-dark {{ color: #3d2a1a; text-shadow: 0 1px 0 rgba(255,244,222,.4); margin: 2px 0 6px; }}

/* 默认队伍里的小塔卡（不论塔分类）都用金色底，与游戏内一致 */
.ody-crew-panel .ody-unit-card,
.ody-crew-panel .ody-unit-card.cat-primary,
.ody-crew-panel .ody-unit-card.cat-military,
.ody-crew-panel .ody-unit-card.cat-magic,
.ody-crew-panel .ody-unit-card.cat-support {{
  background: radial-gradient(circle at 50% 30%, #f5a82a 0%, #dc7410 75%);
  border-color: #b4740d;
}}

/* 英雄网格 */
.ody-hero-grid {{ text-align: center; padding-top: 4px; }}
.ody-hero-grid .ody-unit-wrap {{ width: 62px; height: 84px; margin: 0 1px 6px; }}
.ody-hero-grid .ody-unit-card {{ width: 60px; height: 76px; border-radius: 8px; }}
.ody-hero-grid .ody-unit-card img {{ width: 56px; height: 58px; }}

/* 猴子网格（含升级上限） */
.ody-tower-grid {{ text-align: center; padding-top: 4px; }}
.ody-tower-grid .ody-unit-wrap {{ width: 67px; height: 84px; margin: 0 1px 4px; }}
.ody-tower-grid .ody-unit-card {{ width: 65px; height: 78px; border-radius: 6px; }}
.ody-tower-grid .ody-unit-card img {{ width: 63px; height: 60px; }}

/* ===== 力量（六边形 + 蓝色圆形计数） ===== */
.ody-power-grid {{ text-align: center; padding-top: 4px; }}
.ody-power-wrap {{ display: inline-block; width: 46px; height: 50px; margin: 0 0 4px; vertical-align: top; }}
.ody-power-tile {{ position: relative; width: 42px; height: 42px; margin: 3px auto 0; overflow: hidden;
                   background: linear-gradient(145deg, #ffd64a 0%, #f6a70d 58%, #d86b05 100%);
                   border: 2px solid #9b5b0b;
                   /* 六边形切口 */
                   clip-path: polygon(50% 0, 100% 25%, 100% 75%, 50% 100%, 0 75%, 0 25%);
                   box-shadow: inset 0 2px 0 rgba(255,255,255,.55), 0 2px 0 rgba(78,49,15,.28); }}
.ody-power-tile img {{ display: block; width: 32px; height: 32px; margin: 4px auto 0; object-fit: contain; }}
.ody-power-count {{ position: absolute; left: -5px; top: -5px; width: 18px; height: 18px; padding-top: 2px;
                    color: #ffffff; background: #006e7f; border: 2px solid #ffffff; border-radius: 50%;
                    font-size: 9px; line-height: 12px; font-weight: 900; text-shadow: 0 1px 0 #033a44;
                    z-index: 2; }}
.ody-power-fallback {{ padding-top: 19px; color: #fff; font-size: 9px; line-height: 11px; font-weight: 900;
                       text-shadow: 0 1px 0 #5b3212; }}

/* ===== 岛屿规则 ===== */
.ody-maps {{ padding: 4px 13px 0; }}
.ody-map-row {{ display: table; width: 100%; min-height: 118px; margin-bottom: 8px; padding: 6px 8px;
                table-layout: fixed; background: #ab927c; border-radius: 8px;
                box-shadow: inset 0 1px 0 rgba(255,244,222,.24), 0 2px 0 rgba(84,53,30,.20); }}
.ody-map-img-cell {{ position: relative; display: table-cell; width: 178px; vertical-align: middle; }}
.ody-map-img {{ display: block; width: 168px; height: 105px; object-fit: cover; border: 3px solid #f8b900;
                border-radius: 7px; box-shadow: 0 1px 0 #70430f; }}
.ody-map-empty {{ width: 168px; height: 105px; padding-top: 38px; color: #684d37; text-align: center;
                  background: #c0aa91; border: 3px solid #f8b900; border-radius: 7px; font-size: 13px; font-weight: 900; }}
.ody-map-overlay {{ position: absolute; top: 6px; left: 50%; transform: translateX(-50%);
                    color: #ffffff; font-size: 14px; line-height: 18px; font-weight: 900;
                    text-shadow: 0 1px 0 #5a3a14; letter-spacing: 1px; }}
.ody-map-info {{ display: table-cell; vertical-align: middle; padding: 0 8px 0 6px; }}
.ody-map-meta {{ display: table; width: 100%; table-layout: fixed; }}
.ody-map-meta-item {{ display: table-cell; vertical-align: middle; width: 33.333%; color: #ffffff;
                      font-size: 14px; line-height: 20px; font-weight: 900;
                      text-shadow: 0 1px 0 #68513d; white-space: nowrap; padding: 4px 0; }}
.ody-mini-icon {{ display: inline-block; width: 22px; height: 22px; margin-right: 4px; vertical-align: -5px;
                  object-fit: contain; filter: drop-shadow(0 1px 0 rgba(0,0,0,.35)); }}
.ody-coin {{ color: #ffd329; font-size: 18px; vertical-align: -1px; text-shadow: 0 1px 0 #7b5311; }}
.ody-play {{ display: inline-block; width: 22px; height: 22px; margin-right: 4px; color: #ffffff;
             background: #1ec84d; border: 2px solid #087d2c; border-radius: 50%; font-size: 13px;
             line-height: 18px; text-align: center; text-shadow: none; vertical-align: -5px; }}
.ody-diff {{ display: inline-block; width: 22px; height: 22px; margin-right: 4px; color: #ffffff;
             background: #27a8d4; border: 2px solid #08708f; border-radius: 50%; font-size: 11px;
             line-height: 18px; text-align: center; text-shadow: none; vertical-align: -5px; }}
.ody-map-rule {{ padding-top: 8px; color: #ffffff; font-size: 15px; line-height: 20px; font-weight: 900;
                 text-align: center; text-shadow: 0 1px 0 #68513d;
                 white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
/* 规则行换行会使行高超出 _odyssey_card_height 按 maps_count 的预算，溢出内容流入 PDF
   第 2 页被 pdftoppm -singlefile 静默丢弃，故单行省略号截断 */
.ody-map-sub {{ color: #f4dfc2; font-size: 11px; line-height: 15px; font-weight: 700; }}
.compat-data {{ display: none; }}
</style></head>
<body><div class="ody-page"><div class="ody-paper">{body}</div></div></body></html>"""


RACE_CARD_W = 836


def _race_shell(body: str, h: int) -> str:
    """BTD6 API Explorer 详情页风格外壳：近黑深蓝底 + 标题条 + 主面板 + 亮蓝网格。

    设计 token（取样自站点截图）：详情底 #000b16 / 主面板 #213753 / 内嵌 #344d6c /
    标题条 #234a6a / 网格亮蓝 #699bd9 / 数量角标 #006e7f / 按钮青 #11a6c5。"""
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
@page {{ size: {RACE_CARD_W}px {h}px; margin: 0; background: linear-gradient(180deg, #46c8f1 0%, #129ed0 56%, #087eaf 100%); }}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ width: {RACE_CARD_W}px; height: {h}px; color: #ffffff;
        font-family: "WenQuanYi Micro Hei", "Noto Sans CJK SC", sans-serif;
       background: linear-gradient(180deg, #46c8f1 0%, #129ed0 56%, #087eaf 100%); }}
.race-page {{ width: {RACE_CARD_W}px; min-height: {h}px; }}
.race-frame {{ margin: 0; padding: 12px 14px 16px; }}

/* 标题条：深蓝圆角通栏，左圆形徽章 + 居中两行标题 */
.race-topbar {{ background: linear-gradient(180deg, #2b5a80 0%, #234a6a 100%);
                border: 1px solid #3d648c; border-radius: 14px; padding: 12px 14px;
                box-shadow: inset 0 1px 0 rgba(255,255,255,.14), 0 2px 0 rgba(2,10,20,.4); }}
.race-head {{ display: table; width: 100%; table-layout: fixed; }}
.race-emblem-cell {{ display: table-cell; width: 96px; vertical-align: middle; }}
.race-emblem {{ width: 78px; height: 78px; margin: 0 auto; border-radius: 50%;
                border: 3px solid #0d2036; background: #16324e; overflow: hidden; }}
.race-emblem img {{ display: block; width: 100%; height: 100%; object-fit: cover; }}
.race-emblem-fallback {{ width: 100%; height: 100%; text-align: center; padding-top: 13px;
                          color: #ffd400; font-size: 38px; line-height: 44px; }}
.race-title-cell {{ display: table-cell; vertical-align: middle; text-align: center; }}
.race-title {{ color: #ffffff; font-size: 27px; line-height: 34px; font-weight: 900;
               text-shadow: 0 2px 0 rgba(4,16,30,.55); word-break: break-all; }}
.race-subtitle {{ color: #dceafc; font-size: 18px; line-height: 24px; font-weight: 700; padding-top: 3px; }}
.race-time {{ color: #ffd84d; font-size: 14px; line-height: 19px; font-weight: 700; padding-top: 3px; }}

/* 主内容面板 #213753：地图预览 + 属性内嵌面板 */
.race-content {{ margin-top: 12px; padding: 14px; background: #213753;
                 border: 1px solid #2f4a6d; border-radius: 14px; }}
.race-layout {{ display: table; width: 100%; table-layout: fixed; }}
.race-map {{ display: table-cell; width: 372px; vertical-align: top; }}
.race-map img {{ display: block; width: 372px; height: 248px; object-fit: cover;
                 border: 3px solid #699bd9; border-radius: 12px; background: #16324e; }}
.race-map-empty {{ width: 372px; height: 248px; border: 3px solid #699bd9; border-radius: 12px;
                   background: #344d6c; text-align: center; padding-top: 92px; font-size: 48px; }}
.race-stats-cell {{ display: table-cell; vertical-align: top; padding-left: 14px; }}
.race-stats {{ display: table; width: 100%; background: #344d6c; border: 1px solid #43618a;
               border-radius: 12px; padding: 8px 12px; table-layout: fixed;
               box-shadow: inset 0 1px 0 rgba(255,255,255,.08); }}
.race-stat-col {{ display: table-cell; width: 50%; vertical-align: top; }}
.race-stat {{ display: table; width: 100%; table-layout: fixed; padding: 7px 0; }}
.race-stat-icon-cell {{ display: table-cell; width: 42px; vertical-align: middle; text-align: center; }}
.race-stat-icon {{ width: 34px; height: 34px; object-fit: contain; }}
.race-stat-fallback {{ display: inline-block; width: 34px; height: 34px; font-size: 26px;
                       line-height: 34px; text-align: center; }}
.race-stat-copy {{ display: table-cell; vertical-align: middle; padding-left: 4px; }}
.race-stat-label {{ color: #ffffff; font-size: 13px; line-height: 17px; font-weight: 900; white-space: nowrap;
                     text-shadow: 0 1px 0 rgba(4,16,30,.6); }}
.race-stat-value {{ color: #ffffff; font-size: 15px; line-height: 19px; font-weight: 900; white-space: nowrap;
                     text-shadow: 0 1px 0 rgba(4,16,30,.6); }}

/* 猴子可用区：白色大写粗体分区标题 + 分类色瓦片网格 */
.race-monkey-section {{ margin-top: 12px; }}
.race-options {{ display: table; width: 100%; table-layout: fixed; }}
.race-available {{ display: table-cell; vertical-align: middle; color: #ffffff; font-size: 17px;
                    line-height: 22px; font-weight: 900; padding: 0 0 8px;
                    text-shadow: 0 1px 0 rgba(4,16,30,.6); }}
.mkgrid.race-mkgrid {{ width: 100%; padding-top: 2px; text-align: left; overflow: visible; }}
.race-mkwrap {{ display: inline-block; width: 64px; height: 84px; margin: 0 3px 4px 0; vertical-align: top; }}
.race-mk {{ position: relative; width: 64px; height: 79px; border-radius: 10px; border: 2px solid #ffffff;
            text-align: center; overflow: hidden;
            box-shadow: inset 0 2px 0 rgba(255,255,255,.35), 0 2px 0 rgba(4,16,30,.35); }}
.race-mk.primary {{ background: linear-gradient(180deg, #7cc4f4 0%, #4aa8e8 58%, #2d7cc0 100%); }}
.race-mk.military {{ background: linear-gradient(180deg, #8fd07f 0%, #58b64f 58%, #358a3f 100%); }}
.race-mk.magic {{ background: linear-gradient(180deg, #c89aec 0%, #9c5fd4 58%, #7a3fb8 100%); }}
.race-mk.support {{ background: linear-gradient(180deg, #f6c169 0%, #e8940f 58%, #c06f0a 100%); }}
.race-mk.hero {{ background: linear-gradient(180deg, #ffe95c 0%, #fecb00 58%, #dd9f00 100%); }}
.race-mk img {{ display: block; width: 58px; height: 58px; margin: 2px auto 0; object-fit: contain;
                filter: drop-shadow(0 1px 1px rgba(0,0,0,.35)); }}
.race-mk .race-lim {{ position: absolute; left: 2px; top: 2px; min-width: 20px; height: 22px; padding: 0 4px;
                      border-radius: 11px; color: #ffffff; background: #006e7f;
                      border: 2px solid #ffffff; font-size: 12px; line-height: 18px; font-weight: 900;
                      text-align: center; text-shadow: 0 1px 0 #033a44; }}
.race-mk .race-path {{ position: absolute; left: 2px; right: 2px; bottom: 4px; color: #ffffff;
                       font-size: 12px; line-height: 15px; font-weight: 900; text-align: center;
                       background: rgba(166, 39, 26, .78); border-radius: 6px; letter-spacing: .5px;
                       text-shadow: 0 1px 0 rgba(40,6,2,.8); }}
.race-mk-fallback {{ color: #ffffff; font-size: 11px; line-height: 14px; font-weight: 900;
                     padding: 18px 3px 0; word-break: break-all; text-shadow: 0 1px 0 rgba(0,0,0,.55); }}

/* 底部 MODIFIERS / RULES 并排分区 */
.race-bottom {{ display: table; width: 100%; margin-top: 12px; table-layout: fixed; }}
.race-bottom-left {{ display: table-cell; width: 46%; vertical-align: top; padding-right: 6px; }}
.race-bottom-right {{ display: table-cell; width: 54%; vertical-align: top; padding-left: 6px; }}
.race-panel-head {{ background: #344d6c; border: 1px solid #4d6b92; border-radius: 10px;
                    color: #ffffff; font-size: 17px; line-height: 22px; font-weight: 900;
                    text-align: center; padding: 7px 0;
                    text-shadow: 0 1px 0 rgba(4,16,30,.6); }}
.race-panel-body {{ margin-top: 8px; background: rgba(11,22,38,.45); border: 1px solid #2f4a6d;
                    border-radius: 10px; padding: 9px 10px; min-height: 64px; }}
.race-mod-grid {{ width: 100%; }}
.race-mod-row {{ display: table; width: 100%; table-layout: fixed; }}
.race-mod-item {{ display: table-cell; width: 50%; height: 34px; vertical-align: middle; }}
.race-mod-icon-cell {{ display: table-cell; width: 32px; vertical-align: middle; text-align: center; }}
.race-mod-icon {{ display: inline-block; width: 29px; height: 29px; object-fit: contain; }}
.race-mod-icon-fallback {{ display: inline-block; width: 29px; height: 29px; color: #ffd84d; font-size: 22px; line-height: 29px; text-align: center; }}
.race-mod-copy {{ display: table-cell; vertical-align: middle; padding-left: 2px; }}
.race-mod-label {{ color: #ffffff; font-size: 11px; line-height: 13px; font-weight: 900; white-space: nowrap;
                   text-shadow: 0 1px 0 rgba(4,16,30,.6); }}
.race-mod-value {{ color: #ffd84d; font-size: 12px; line-height: 14px; font-weight: 900;
                   text-shadow: 0 1px 0 rgba(60,32,0,.6); }}
.race-mod-default {{ padding-top: 12px; text-align: center; color: #dceafc; font-size: 14px; font-weight: 700; }}
.race-rule-row {{ display: table; width: 100%; min-height: 54px; table-layout: fixed; }}
.race-rule-item {{ display: table-cell; vertical-align: middle; width: 50%; }}
.race-rule-item.race-rule-single {{ width: 100%; }}
.race-rule-icon {{ display: table-cell; width: 50px; vertical-align: middle; }}
.race-rule-icon img {{ width: 46px; height: 46px; object-fit: contain; }}
.race-rule-fallback {{ display: inline-block; width: 44px; height: 44px; font-size: 34px; line-height: 44px; }}
.race-rule-copy {{ display: table-cell; vertical-align: middle; padding-left: 4px; color: #ffffff;
                   font-size: 12px; line-height: 15px; font-weight: 900;
                   text-shadow: 0 1px 0 rgba(4,16,30,.6); }}
.race-limit-value {{ font-size: 15px; color: #ffd84d; }}
.race-custom-panel {{ min-height: 92px; margin-top: 10px; padding: 8px 10px;
                      background: rgba(11,22,38,.45); border: 1px solid #2f4a6d; border-radius: 10px; }}
.race-custom-title {{ color: #ffffff; font-size: 15px; line-height: 20px; text-align: center; font-weight: 900;
                      text-shadow: 0 1px 0 rgba(4,16,30,.6); }}
.race-custom-body {{ display: table; width: 100%; min-height: 54px; table-layout: fixed; }}
.race-custom-icon {{ display: table-cell; width: 56px; vertical-align: top; padding-top: 4px; text-align: center; }}
.race-custom-icon img {{ width: 48px; height: 48px; object-fit: contain; }}
.race-custom-copy {{ display: table-cell; vertical-align: top; color: #ffffff; font-size: 13px; line-height: 18px;
                     font-weight: 900; text-shadow: 0 1px 0 rgba(4,16,30,.6); }}
.race-round-set-name {{ color: #ffd84d; font-size: 13px; line-height: 18px; font-weight: 900; }}
.race-round-lines {{ margin-top: 2px; }}
.race-round-line {{ line-height: 20px; white-space: nowrap; }}
.race-round-wave {{ display: inline-block; min-width: 70px; color: #ffffff; }}
.race-round-desc {{ color: #ffffff; }}
.race-bloon-icon {{ display: inline-block; width: 28px; height: 28px; margin: 0 3px; vertical-align: middle; }}
.race-bloon-icon img {{ display: block; width: 28px; height: 28px; object-fit: contain; }}
.race-bloon-fallback {{ display: inline-block; color: #ffd84d; }}
.compat-data {{ display: none; }}
</style></head>
<body><div class="race-page"><div class="race-frame">{body}</div></div></body></html>"""


def _race_ui_img(fname: str, fallback: str, cls: str) -> str:
    # 兼容调用方直接传 data: URL（如已解析的素材）；文件名才走本地素材解析
    url = fname if fname.startswith("data:") else assets._ui_asset_data_url(fname)
    if url:
        return f"<img class='{cls}' src='{util._esc(url)}'/>"
    fallback_class = {
        "race-stat-icon": "race-stat-fallback",
        "race-rule-icon-img": "race-rule-fallback",
    }.get(cls, f"{cls}-fallback")
    return f"<span class='{fallback_class}'>{util._esc(fallback)}</span>"


def _race_modifier_items(mods: dict | None) -> list[tuple[str, str, str]]:
    """将气球强化整理为（中文名称、倍率/状态、图标文件）。"""
    mods = mods or {}
    items = []

    def add(label: str, value, increase_icon: str, decrease_icon: str) -> None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return
        if abs(number - 1.0) < 1e-9:
            return
        icon = increase_icon if number > 1 else decrease_icon
        items.append((label, f"×{number:g}", icon))

    add("气球速度", mods.get("speedMultiplier"), "FasterBloonsIcon.png", "SlowerBloonsIcon.png")
    add("重型气球速度", mods.get("moabSpeedMultiplier"), "FasterMoabIcon.png", "SlowerMoabIcon.png")
    add("首领速度", mods.get("bossSpeedMultiplier"), "FasterBossIcon.png", "SlowerBossIcon.png")
    add("再生速度", mods.get("regrowRateMultiplier"), "RegrowRateIncreaseIcon.png", "RegrowRateDecreaseIcon.png")
    health = mods.get("healthMultipliers") or {}
    add("气球血量", health.get("bloons"), "BloonBoostIcon.png", "BloonDecreaseHPIcon.png")
    add("重型气球血量", health.get("moabs"), "MoabBoostIcon.png", "MoabDecreaseHPIcon.png")
    add("首领血量", health.get("boss"), "BossBoostIcon.png", "BossDecreaseHPIcon.png")
    if mods.get("allCamo"):
        items.append(("全体隐身", "启用", "AllCamoIcon.png"))
    if mods.get("allRegen"):
        items.append(("全体再生", "启用", "AllRegenIcon.png"))
    return items


def _race_modifier_html(mods: dict | None) -> str:
    items = _race_modifier_items(mods)
    if not items:
        return "<div class='race-mod-default'>默认</div>"
    rows = []
    for start in range(0, len(items), 2):
        cells = []
        for label, value, icon in items[start:start + 2]:
            value_html = f"<div class='race-mod-value'>{util._esc(value)}</div>" if value else ""
            cells.append(
                "<div class='race-mod-item'><div class='race-mod-icon-cell'>"
                f"{_race_ui_img(icon, '⚡', 'race-mod-icon')}"
                f"</div><div class='race-mod-copy'><div class='race-mod-label'>{util._esc(label)}</div>"
                f"{value_html}</div></div>"
            )
        if len(cells) == 1:
            cells.append("<div class='race-mod-item'></div>")
        rows.append("<div class='race-mod-row'>" + "".join(cells) + "</div>")
    return "<div class='race-mod-grid'>" + "".join(rows) + "</div>"


def _rush_tower_category(t: str) -> str:
    return (rushgen.load_constants()["towersInOrder"].get(t) or {}).get("category", "")


# 分类底色：同类猴子同色（取样自 Explorer 网格：初级蓝 / 军事绿 / 魔法紫 / 支援橙 / 英雄黄）
_TOWER_CAT_COLORS = {
    "Primary": ("#4aa8e8", "#2d7cc0"), "Military": ("#58b64f", "#358a3f"),
    "Magic": ("#9c5fd4", "#7a3fb8"), "Support": ("#e8940f", "#c06f0a"),
    "Hero": ("#fecb00", "#dd9f00"),
}


def _tower_cat_grad(t: str) -> tuple:
    return _TOWER_CAT_COLORS.get(_rush_tower_category(t), ("#8d8279", "#57504a"))


def _list_shell(body: str, h: int) -> str:
    """列表页风格外壳：原版蓝色渐变底；活动总览/排行榜/自制地图使用。

    内含行式倒计时列表（ev-*）、榜单行（lb-*）与地图行（ody-map-img 复用名，
    类名保持与远征壳一致以兼容既有测试与调用方）。"""
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
@page {{ size: {ODYSSEY_CARD_W}px {h}px; margin: 0; background: #46c8f1; }}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html {{ background: #46c8f1; }}
body {{ width: {ODYSSEY_CARD_W}px; height: {h}px; color: #ffffff;
        font-family: "WenQuanYi Micro Hei", "Noto Sans CJK SC", sans-serif;
        background: linear-gradient(180deg, #46c8f1 0%, #129ed0 56%, #087eaf 100%); }}
.list-page {{ width: {ODYSSEY_CARD_W}px; height: {h}px; padding: 12px 14px 16px; box-sizing: border-box; }}

/* ===== 活动总览：分区面板 + 行式倒计时列表 ===== */
.ev-section {{ background: #213753; border: 1px solid #2f4a6d; border-radius: 12px;
               padding: 10px 10px 8px; }}
.ev-banner {{ background: #344d6c; border: 1px solid #43618a; border-radius: 8px;
              color: #ffffff; font-size: 16px; line-height: 22px; font-weight: 900;
              padding: 7px 12px; margin-bottom: 6px;
              text-shadow: 0 1px 0 rgba(2,10,20,.5); }}
.ev-row {{ display: table; width: 100%; min-height: 56px; margin-bottom: 5px; padding: 5px 6px;
          background: #344d6c; border-radius: 8px; table-layout: fixed;
          box-shadow: inset 0 1px 0 rgba(255,255,255,.07); }}
.ev-icon-cell {{ display: table-cell; width: 56px; vertical-align: middle; text-align: center; }}
.ev-icon {{ width: 44px; height: 44px; border-radius: 50%; object-fit: cover;
           border: 2px solid #699bd9; background: #16324e; vertical-align: middle; }}
.ev-name {{ display: table-cell; vertical-align: middle; padding: 0 10px;
            color: #ffffff; font-size: 15px; font-weight: 900;
            text-shadow: 0 1px 0 rgba(2,10,20,.6);
            word-break: break-all; }}
.ev-dates {{ display: table-cell; vertical-align: middle; width: 200px;
            color: #cfe0f2; font-size: 12px; line-height: 1.4; font-weight: 700; padding: 0 8px; }}
.ev-dates div {{ white-space: nowrap; }}
.ev-right {{ display: table-cell; vertical-align: middle; width: 130px; text-align: right;
             font-size: 14px; font-weight: 900; letter-spacing: 0.5px; padding: 0 10px 0 0; }}
.ev-right-on {{ color: #7dff8a; text-shadow: 0 1px 0 rgba(2,10,20,.7); }}
.ev-right-up {{ color: #ffd84d; text-shadow: 0 1px 0 rgba(2,10,20,.7); }}
.ev-right-off {{ color: #9fb6d4; }}
.ev-empty {{ color: #cfe0f2; font-size: 12px; font-weight: 700; padding: 14px 18px;
            background: rgba(11,22,38,.45); border-radius: 8px; line-height: 1.5; }}
.ev-spacer {{ height: 10px; }}

/* ===== 排行榜 ===== */
.lb-head {{ background: #213753; border: 1px solid #2f4a6d; border-radius: 12px; padding: 12px 16px; }}
.lb-title {{ color: #ffffff; font-size: 20px; line-height: 26px; font-weight: 900; word-break: break-all; }}
.lb-subtitle {{ color: #ffd84d; font-size: 14px; line-height: 19px; font-weight: 700; padding-top: 3px; }}
.lb-note {{ color: #9fb6d4; font-size: 12px; line-height: 17px; font-weight: 700; padding-top: 3px; }}
.lb-panel {{ background: #213753; border: 1px solid #2f4a6d; border-radius: 12px;
             padding: 10px 10px 4px; margin-top: 10px; }}
.lb-row {{ display: table; width: 100%; min-height: 48px; margin-bottom: 6px; padding: 5px 8px;
           table-layout: fixed; background: #344d6c; border-radius: 9px;
           box-shadow: inset 0 1px 0 rgba(255,255,255,.07); }}
.lb-rank {{ display: table-cell; width: 62px; vertical-align: middle; text-align: center; }}
.lb-rank span {{ display: inline-block; min-width: 42px; padding: 0 6px; height: 36px; line-height: 36px;
                 border-radius: 8px; background: #213753; color: #cfe0f2;
                 font-size: 17px; font-weight: 900; letter-spacing: .5px; }}
.lb-name {{ display: table-cell; vertical-align: middle; padding: 0 8px; font-size: 15px; font-weight: 700;
            color: #ffffff; text-shadow: 0 1px 0 rgba(2,10,20,.5); word-break: break-all; }}
.lb-score {{ display: table-cell; width: 150px; vertical-align: middle; text-align: right; font-size: 15px;
             font-weight: 900; color: #ffffff; text-shadow: 0 1px 0 rgba(2,10,20,.5); white-space: nowrap; }}
.lb-empty {{ text-align: center; color: #cfe0f2; font-size: 14px; font-weight: 700; padding: 18px 0; }}

/* ===== 自制地图行 ===== */
.map-panel {{ background: #213753; border: 1px solid #2f4a6d; border-radius: 12px;
              padding: 10px 10px 4px; margin-top: 10px; }}
.map-row {{ display: table; width: 100%; min-height: 112px; margin-bottom: 8px; padding: 7px 8px;
            table-layout: fixed; background: #344d6c; border-radius: 10px;
            box-shadow: inset 0 1px 0 rgba(255,255,255,.07); }}
.map-img-cell {{ display: table-cell; width: 180px; vertical-align: middle; }}
.ody-map-img {{ display: block; width: 168px; height: 100px; object-fit: cover;
                border: 3px solid #699bd9; border-radius: 8px; background: #16324e; }}
.ody-map-empty {{ width: 168px; height: 100px; padding-top: 36px; color: #9fb6d4; text-align: center;
                  background: #16324e; border: 3px solid #699bd9; border-radius: 8px;
                  font-size: 13px; font-weight: 900; }}
.map-info {{ display: table-cell; vertical-align: middle; padding: 0 8px 0 10px; }}
.map-name {{ color: #ffffff; font-size: 16px; line-height: 21px; font-weight: 900;
             text-shadow: 0 1px 0 rgba(2,10,20,.55); word-break: break-all; }}
.map-meta {{ display: table; width: 100%; table-layout: fixed; padding-top: 4px; }}
.map-meta-item {{ display: table-cell; vertical-align: middle; width: 33.333%; color: #cfe0f2;
                  font-size: 13px; line-height: 19px; font-weight: 700; white-space: nowrap; padding: 3px 0; }}
.compat-data {{ display: none; }}
</style></head>
<body><div class="list-page">
{body}
</div></body></html>"""
