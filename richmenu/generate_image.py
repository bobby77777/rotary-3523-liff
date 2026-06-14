#!/usr/bin/env python3
"""Generate 3 LINE rich menu images for 3523 District (home / profile / admin tabs)."""
import os, urllib.request
from PIL import Image, ImageDraw, ImageFont

# ── Canvas ────────────────────────────────────────────────────────────────────
W, H  = 2500, 1686
TAB_H = 175            # tab bar height
CY    = TAB_H          # content start Y
CH    = H - TAB_H      # 1511 – content height
LW    = 1666           # left block width
RW    = W - LW         # 834  – right column width
BH    = CH // 2        # 755  – top button height (bottom = 756)

# ── Assets ────────────────────────────────────────────────────────────────────
FONT     = "/System/Library/Fonts/STHeiti Medium.ttc"
FA_CACHE = os.path.expanduser("~/.cache/fa-solid-900.otf")
FA_URL   = ("https://raw.githubusercontent.com/FortAwesome/Font-Awesome"
            "/6.x/otfs/Font%20Awesome%206%20Free-Solid-900.otf")

# ── Palette ───────────────────────────────────────────────────────────────────
ROTARY_BLUE = (30,  58, 138)
ROTARY_GOLD = (255, 215,   0)
WHITE       = (255, 255, 255)
GRAY_100    = (243, 244, 246)
GRAY_200    = (229, 231, 235)
GRAY_500    = (107, 114, 128)

TABS = [
    ("home",    "首頁活動"),
    ("profile", "個人中心"),
    ("admin",   "主委專區"),
]

# FA6 Free Solid codepoints
FA = {
    "ribbon":   "",
    "calendar": "",
    "globe":    "",
    "id-badge": "",
    "history":  "",
    "upload":   "",
    "chart":    "",
    "camera":   "",
    "headset":  "",
}

CONTENT = {
    "home": {
        "bg_top":    (15,  23,  65),
        "bg_bottom": (49,  46, 129),
        "tag":       "即將舉辦",
        "title":     "年度活動大螢幕\n行事曆即時連動",
        "sub":       "點擊大螢幕一鍵報名",
        "icon":      "ribbon",
        "icon_color": ROTARY_GOLD,
        "rt": {"label": "年度行事曆",   "icon": "calendar",
               "ic": (16, 185, 129), "ib": (209, 250, 229)},
        "rb": {"label": "地區官網",     "icon": "globe",
               "ic": (245, 158,  11), "ib": (254, 243, 199)},
    },
    "profile": {
        "bg_top":    (17,  94,  89),
        "bg_bottom": ( 6, 148, 105),
        "tag":       "LIFF 身份識別已綁定",
        "title":     "我的「活動報到 QR」",
        "sub":       "出示給接待主委掃描完成報到",
        "icon":      "id-badge",
        "icon_color": (167, 243, 208),
        "rt": {"label": "報名與繳費紀錄", "icon": "history",
               "ic": (99, 102, 241), "ib": (224, 231, 255)},
        "rb": {"label": "上傳繳費憑證",   "icon": "upload",
               "ic": (245, 158,  11), "ib": (254, 243, 199)},
    },
    "admin": {
        "bg_top":    (15, 23, 42),
        "bg_bottom": (30, 41, 59),
        "tag":       "主委專用",
        "title":     "報名/繳費\n即時動態統計報表",
        "sub":       "依分區及創社先後自動排序",
        "icon":      "chart",
        "icon_color": (253, 224,  71),
        "rt": {"label": "現場報到相機",   "icon": "camera",
               "ic": (16, 185, 129), "ib": (209, 250, 229)},
        "rb": {"label": "秘書處後台支援", "icon": "headset",
               "ic": (107, 114, 128), "ib": (243, 244, 246)},
    },
}


def get_fa_font(size):
    if not os.path.exists(FA_CACHE):
        os.makedirs(os.path.dirname(FA_CACHE), exist_ok=True)
        print("Downloading FontAwesome 6 Free Solid OTF...")
        try:
            urllib.request.urlretrieve(FA_URL, FA_CACHE)
            print(f"  Saved → {FA_CACHE}")
        except Exception as e:
            print(f"  Download failed: {e}")
            return None
    try:
        return ImageFont.truetype(FA_CACHE, size)
    except Exception as e:
        print(f"  FA font load failed: {e}")
        return None


def load_font(size):
    try:
        return ImageFont.truetype(FONT, size, index=0)
    except Exception:
        return ImageFont.load_default()


def vgrad(draw, x0, y0, x1, y1, c0, c1):
    h = y1 - y0
    for i in range(h):
        t = i / max(h - 1, 1)
        color = tuple(int(c0[j] + (c1[j] - c0[j]) * t) for j in range(3))
        draw.line([(x0, y0 + i), (x1 - 1, y0 + i)], fill=color)


def draw_tab_bar(draw, active_id, fn):
    tab_widths = [833, 833, 834]
    x = 0
    for i, (tid, tname) in enumerate(TABS):
        tw = tab_widths[i]
        is_active = (tid == active_id)
        draw.rectangle([x, 0, x + tw - 1, TAB_H - 1],
                       fill=WHITE if is_active else GRAY_100)
        if is_active:
            draw.rectangle([x, TAB_H - 8, x + tw - 1, TAB_H - 1], fill=ROTARY_GOLD)
        if i > 0:
            draw.line([(x, 0), (x, TAB_H)], fill=GRAY_200, width=3)
        color = ROTARY_BLUE if is_active else GRAY_500
        tb = draw.textbbox((0, 0), tname, font=fn)
        cx = x + tw // 2
        cy = TAB_H // 2
        draw.text((cx - (tb[2] - tb[0]) // 2, cy - (tb[3] - tb[1]) // 2),
                  tname, font=fn, fill=color)
        x += tw
    draw.line([(0, TAB_H), (W, TAB_H)], fill=GRAY_200, width=3)


def draw_left_block(draw, cfg, fn_tag, fn_title, fn_sub, fa_lg):
    vgrad(draw, 0, CY, LW, H, cfg["bg_top"], cfg["bg_bottom"])

    # Yellow tag badge
    tag = cfg["tag"]
    tb = draw.textbbox((0, 0), tag, font=fn_tag)
    pad_x, pad_y = 32, 14
    bx, by = 65, CY + 70
    bw, bh = tb[2] - tb[0] + pad_x * 2, tb[3] - tb[1] + pad_y * 2
    draw.rounded_rectangle([bx, by, bx + bw, by + bh], radius=14, fill=ROTARY_GOLD)
    draw.text((bx + pad_x, by + pad_y), tag, font=fn_tag, fill=ROTARY_BLUE)

    # Icon circle
    cx    = LW // 2
    cy_ic = CY + CH * 43 // 100
    cr    = 170
    glow  = tuple(min(255, c + 28) for c in cfg["bg_bottom"])
    draw.ellipse([cx - cr - 22, cy_ic - cr - 22,
                  cx + cr + 22, cy_ic + cr + 22], fill=glow)
    ic_fill = tuple(int(c * 0.28 + cfg["bg_bottom"][j] * 0.72)
                    for j, c in enumerate(cfg["icon_color"]))
    draw.ellipse([cx - cr, cy_ic - cr, cx + cr, cy_ic + cr], fill=ic_fill)
    if fa_lg:
        draw.text((cx, cy_ic), FA.get(cfg["icon"], ""), font=fa_lg,
                  fill=cfg["icon_color"], anchor="mm")

    # Title lines
    lines = cfg["title"].split("\n")
    y_title = cy_ic + cr + 55
    for line in lines:
        tb2 = draw.textbbox((0, 0), line, font=fn_title)
        draw.text((cx - (tb2[2] - tb2[0]) // 2, y_title), line,
                  font=fn_title, fill=WHITE)
        y_title += (tb2[3] - tb2[1]) + 18

    # Subtitle
    tb3 = draw.textbbox((0, 0), cfg["sub"], font=fn_sub)
    draw.text((cx - (tb3[2] - tb3[0]) // 2, y_title + 12),
              cfg["sub"], font=fn_sub, fill=(170, 200, 230))

    # CTA line at bottom
    cta = "點擊報名 →"
    draw.text((65, H - 95), cta, font=fn_sub, fill=ROTARY_GOLD)


def draw_right_btn(draw, btn, y0, btn_h, fn_label, fa_sm, border_top=False):
    x0, x1, y1 = LW, W, y0 + btn_h
    draw.rectangle([x0, y0, x1 - 1, y1 - 1], fill=WHITE)
    draw.line([(x0, y0), (x0, y1)], fill=GRAY_200, width=3)
    if border_top:
        draw.line([(x0, y0), (x1, y0)], fill=GRAY_200, width=3)

    cx    = x0 + RW // 2
    cy_ic = y0 + btn_h * 40 // 100
    cr    = 115
    glow  = tuple(min(255, c + 20) for c in btn["ib"])
    draw.ellipse([cx - cr - 16, cy_ic - cr - 16,
                  cx + cr + 16, cy_ic + cr + 16], fill=glow)
    draw.ellipse([cx - cr, cy_ic - cr, cx + cr, cy_ic + cr], fill=btn["ib"])
    if fa_sm:
        draw.text((cx, cy_ic), FA.get(btn["icon"], ""), font=fa_sm,
                  fill=btn["ic"], anchor="mm")

    tb = draw.textbbox((0, 0), btn["label"], font=fn_label)
    draw.text((cx - (tb[2] - tb[0]) // 2, cy_ic + cr + 32),
              btn["label"], font=fn_label, fill=(30, 30, 30))


def generate(tab_id):
    cfg = CONTENT[tab_id]
    img  = Image.new("RGB", (W, H), WHITE)
    draw = ImageDraw.Draw(img)

    fn_tab   = load_font(82)
    fn_tag   = load_font(50)
    fn_title = load_font(122)
    fn_sub   = load_font(62)
    fn_label = load_font(78)
    fa_lg    = get_fa_font(205)
    fa_sm    = get_fa_font(125)

    draw_tab_bar(draw, tab_id, fn_tab)
    draw_left_block(draw, cfg, fn_tag, fn_title, fn_sub, fa_lg)
    draw_right_btn(draw, cfg["rt"], CY, BH, fn_label, fa_sm)
    draw_right_btn(draw, cfg["rb"], CY + BH, CH - BH, fn_label, fa_sm, border_top=True)

    out = f"richmenu_{tab_id}.jpg"
    img.save(out, "JPEG", quality=95)
    print(f"Saved {out}  ({W}×{H})")


if __name__ == "__main__":
    for tab_id, _ in TABS:
        generate(tab_id)
