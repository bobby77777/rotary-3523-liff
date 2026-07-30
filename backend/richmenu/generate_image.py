#!/usr/bin/env python3
"""Generate 4 LINE rich menu images for 3523 District (home / profile / ebooks / admin tabs)."""
import os, urllib.request
from PIL import Image, ImageDraw, ImageFont

# ── Canvas ────────────────────────────────────────────────────────────────────
W, H  = 2500, 1686
TAB_H = 175
CY    = TAB_H
CH    = H - TAB_H      # 1511
LW    = 1666
RW    = W - LW         # 834
BH    = CH // 2        # 755

# ── Assets ────────────────────────────────────────────────────────────────────
FONT     = "/System/Library/Fonts/STHeiti Medium.ttc"
FA_CACHE = os.path.expanduser("~/.cache/fa-solid-900.otf")
FA_URL   = ("https://raw.githubusercontent.com/FortAwesome/Font-Awesome"
            "/6.x/otfs/Font%20Awesome%206%20Free-Solid-900.otf")

# ── Palette ───────────────────────────────────────────────────────────────────
ROTARY_BLUE    = (30,  58, 138)
ROTARY_GOLD    = (255, 215,   0)
ROTARY_EMERALD = (16, 185, 129)
WHITE       = (255, 255, 255)
GRAY_100    = (243, 244, 246)
GRAY_200    = (229, 231, 235)
GRAY_500    = (107, 114, 128)

TABS = [
    ("home",    "首頁活動"),
    ("profile", "個人中心"),
    ("ebooks",  "大會刊物"),
    ("admin",   "主委專區"),
]

CLUB_TABS = [
    ("home",    "首頁活動"),
    ("profile", "個人中心"),
    ("ebooks",  "社內刊物"),
    ("admin",   "社務專區"),
]

# FA6 Free Solid codepoints
FA = {
    "ribbon":     "",
    "calendar":   "",
    "globe":      "",
    "id-badge":   "",
    "history":    "",
    "upload":     "",
    "chart":      "",
    "camera":           "",
    "headset":          "",
    "microphone-lines": "",
    "book-open":  "",
    "award":      "",
    "heart":      "",
    "clipboard-user": "",
    "house-flag": "",
    "images": "",
    "clipboard-list": "",
    "chart-pie": "",
    "file-invoice-dollar": "",
    "users-gear": "",
    "file-invoice": "",
    "book": "",
}

CONTENT = {
    "home": {
        "bg_top":    (15,  23,  65),
        "bg_bottom": (49,  46, 129),
        "tag":       "即將舉辦",
        "title":     "年度活動大螢幕\n行事曆即時連動",
        "sub":       "點擊大螢幕一鍵報名",
        "cta":       "點擊報名 →",
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
        "cta":       "點擊查看 →",
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
        "tag":       "主委 / 秘書處 專屬",
        "title":     "活動報名與繳費\n即時統計看板",
        "sub":       "依分區及創社先後自動排序",
        "cta":       "進入看板 →",
        "icon":      "chart",
        "icon_color": (253, 224,  71),
        "rt": {"label": "貴賓介紹",       "icon": "microphone-lines",
               "ic": (147,  51, 234), "ib": (243, 232, 255)},
        "rb": {"label": "後台支援",       "icon": "headset",
               "ic": (107, 114, 128), "ib": (243, 244, 246)},
    },
}

EBOOKS_BUTTONS = [
    {"label": "活動手冊",  "sub": "線上翻閱指南",
     "icon": "book-open", "ic": (59, 130, 246), "ib": (239, 246, 255)},
    {"label": "年度成果冊", "sub": "回顧榮耀時刻",
     "icon": "award",     "ic": (245, 158,  11), "ib": (255, 251, 235)},
    {"label": "捐贈報告",  "sub": "公益愛心紀錄",
     "icon": "heart",     "ic": (236,  72, 153), "ib": (253, 242, 248)},
]

# ── Club-scope (社內專區) content — emerald / teal identity ─────────────────────
CLUB_CONTENT = {
    "home": {
        "bg_top":    (6,   78,  59),   # emerald-900
        "bg_bottom": (17, 94,  89),    # teal-800
        "tag":       "本社專屬",
        "title":     "本社例會與活動\n出席即時累計",
        "sub":       "點擊查看社內行事曆",
        "cta":       "點擊報名 →",
        "icon":      "house-flag",
        "icon_color": (167, 243, 208),
        "rt": {"label": "出席累計",     "icon": "clipboard-user",
               "ic": (79, 70, 229),  "ib": (224, 231, 255)},
        "rb": {"label": "本社官網",     "icon": "globe",
               "ic": (13, 148, 136), "ib": (204, 251, 241)},
    },
    "profile": {
        "bg_top":    (30,  41, 138),   # indigo-800
        "bg_bottom": (29,  78, 216),   # blue-700
        "tag":       "LIFF 身份識別已綁定",
        "title":     "我的「活動報到 QR」",
        "sub":       "出示給接待社友掃描完成報到",
        "cta":       "點擊查看 →",
        "icon":      "id-badge",
        "icon_color": (191, 219, 254),
        "rt": {"label": "報名與繳費紀錄", "icon": "history",
               "ic": (79, 70, 229),  "ib": (224, 231, 255)},
        "rb": {"label": "上傳繳費憑證",   "icon": "upload",
               "ic": (245, 158,  11), "ib": (254, 243, 199)},
    },
}

CLUB_EBOOKS_BUTTONS = [
    {"label": "社刊 / 週報", "sub": "本社出版品",
     "icon": "book",           "ic": (13, 148, 136), "ib": (204, 251, 241)},
    {"label": "活動花絮相簿", "sub": "精彩回顧",
     "icon": "images",         "ic": (245, 158,  11), "ib": (255, 251, 235)},
    {"label": "理監事會議紀錄", "sub": "社務決議",
     "icon": "clipboard-list", "ic": (99, 102, 241), "ib": (238, 242, 255)},
]

# Club admin 2×2 grid tiles (dark slate cards, per simulator)
CLUB_ADMIN_TILES = [
    {"label": "社友出席率", "icon": "chart-pie",           "ic": (92, 140, 243)},
    {"label": "社務對帳",   "icon": "file-invoice-dollar", "ic": (125, 213, 121)},
    {"label": "理監事專區", "icon": "users-gear",          "ic": (247, 194,  68)},
    {"label": "社友社費",   "icon": "file-invoice",        "ic": (235, 103, 144)},
]


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


def draw_tab_bar(draw, active_id, fn, tabs=TABS, accent=ROTARY_GOLD):
    n = len(tabs)
    base_w = W // n
    # last tab absorbs remainder
    tab_widths = [base_w] * (n - 1) + [W - base_w * (n - 1)]
    x = 0
    for i, (tid, tname) in enumerate(tabs):
        tw = tab_widths[i]
        is_active = (tid == active_id)
        draw.rectangle([x, 0, x + tw - 1, TAB_H - 1],
                       fill=WHITE if is_active else GRAY_100)
        if is_active:
            draw.rectangle([x, TAB_H - 8, x + tw - 1, TAB_H - 1], fill=accent)
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

    tag = cfg["tag"]
    tb = draw.textbbox((0, 0), tag, font=fn_tag)
    pad_x, pad_y = 32, 14
    bx, by = 65, CY + 70
    bw, bh = tb[2] - tb[0] + pad_x * 2, tb[3] - tb[1] + pad_y * 2
    draw.rounded_rectangle([bx, by, bx + bw, by + bh], radius=14, fill=ROTARY_GOLD)
    draw.text((bx + pad_x, by + pad_y), tag, font=fn_tag, fill=ROTARY_BLUE)

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

    lines = cfg["title"].split("\n")
    y_title = cy_ic + cr + 55
    for line in lines:
        tb2 = draw.textbbox((0, 0), line, font=fn_title)
        draw.text((cx - (tb2[2] - tb2[0]) // 2, y_title), line,
                  font=fn_title, fill=WHITE)
        y_title += (tb2[3] - tb2[1]) + 18

    tb3 = draw.textbbox((0, 0), cfg["sub"], font=fn_sub)
    draw.text((cx - (tb3[2] - tb3[0]) // 2, y_title + 12),
              cfg["sub"], font=fn_sub, fill=(170, 200, 230))

    draw.text((65, H - 95), cfg.get("cta", "點擊報名 →"), font=fn_sub, fill=ROTARY_GOLD)


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


def draw_admin_left(draw, cfg, fn_tag, fn_title, fn_sub, fn_label):
    """Admin tab: statistics dashboard layout (badge + title + mini buttons + yellow CTA bar)."""
    vgrad(draw, 0, CY, LW, H, cfg["bg_top"], cfg["bg_bottom"])

    # Role badge (left-aligned, amber)
    tag = cfg["tag"]
    tb = draw.textbbox((0, 0), tag, font=fn_tag)
    pad_x, pad_y = 32, 14
    bx, by = 65, CY + 80
    bw = tb[2] - tb[0] + pad_x * 2
    bh = tb[3] - tb[1] + pad_y * 2
    draw.rounded_rectangle([bx, by, bx + bw, by + bh], radius=14, fill=ROTARY_GOLD)
    draw.text((bx + pad_x, by + pad_y), tag, font=fn_tag, fill=ROTARY_BLUE)

    # Title (left-aligned, white, large)
    title_y = by + bh + 60
    for line in cfg["title"].split("\n"):
        draw.text((65, title_y), line, font=fn_title, fill=WHITE)
        tb2 = draw.textbbox((0, 0), line, font=fn_title)
        title_y += (tb2[3] - tb2[1]) + 20

    # Subtitle (subdued blue-white)
    tb3 = draw.textbbox((0, 0), cfg["sub"], font=fn_sub)
    draw.text((65, title_y + 8), cfg["sub"], font=fn_sub, fill=(140, 180, 220))
    sub_bottom = title_y + 8 + (tb3[3] - tb3[1])

    # Two mini decorative action buttons
    btn_gap  = 30
    btn_h    = 140
    btn_w    = (LW - 65 - 65 - btn_gap) // 2
    btn_y    = sub_bottom + 80
    MINI_BTNS = [
        ("報名統計",   (35, 50, 90)),
        ("繳費核對",   (50, 35, 100)),
    ]
    for i, (label, fill) in enumerate(MINI_BTNS):
        bx2 = 65 + i * (btn_w + btn_gap)
        draw.rounded_rectangle(
            [bx2, btn_y, bx2 + btn_w, btn_y + btn_h], radius=22, fill=fill)
        tb4 = draw.textbbox((0, 0), label, font=fn_sub)
        cx = bx2 + btn_w // 2
        cy = btn_y + btn_h // 2
        draw.text((cx - (tb4[2] - tb4[0]) // 2,
                   cy - (tb4[3] - tb4[1]) // 2), label, font=fn_sub, fill=WHITE)

    # Full-width yellow CTA bar at bottom
    bar_h = 160
    bar_y = H - 60 - bar_h
    draw.rounded_rectangle([65, bar_y, LW - 65, bar_y + bar_h], radius=26, fill=ROTARY_GOLD)
    cta = cfg.get("cta", "進入看板 →")
    tb5 = draw.textbbox((0, 0), cta, font=fn_label)
    cx = LW // 2
    cy = bar_y + bar_h // 2
    draw.text((cx - (tb5[2] - tb5[0]) // 2,
               cy - (tb5[3] - tb5[1]) // 2), cta, font=fn_label, fill=ROTARY_BLUE)


def draw_ebooks_tab(draw, fn_label, fn_sub, fa_sm, buttons=EBOOKS_BUTTONS):
    """3 equal columns for the ebooks tab."""
    col_w = W // 3
    col_widths = [col_w, col_w, W - col_w * 2]

    x = 0
    for i, btn in enumerate(buttons):
        cw = col_widths[i]
        draw.rectangle([x, CY, x + cw - 1, H - 1], fill=WHITE)
        if i > 0:
            draw.line([(x, CY), (x, H)], fill=GRAY_200, width=3)

        cx    = x + cw // 2
        cy_ic = CY + CH * 38 // 100
        cr    = 185

        glow = tuple(min(255, c + 25) for c in btn["ib"])
        draw.ellipse([cx - cr - 20, cy_ic - cr - 20,
                      cx + cr + 20, cy_ic + cr + 20], fill=glow)
        draw.ellipse([cx - cr, cy_ic - cr, cx + cr, cy_ic + cr], fill=btn["ib"])
        if fa_sm:
            draw.text((cx, cy_ic), FA.get(btn["icon"], ""), font=fa_sm,
                      fill=btn["ic"], anchor="mm")

        tb = draw.textbbox((0, 0), btn["label"], font=fn_label)
        lx = cx - (tb[2] - tb[0]) // 2
        ly = cy_ic + cr + 40
        draw.text((lx, ly), btn["label"], font=fn_label, fill=(20, 20, 20))

        tb2 = draw.textbbox((0, 0), btn["sub"], font=fn_sub)
        draw.text((cx - (tb2[2] - tb2[0]) // 2, ly + (tb[3] - tb[1]) + 20),
                  btn["sub"], font=fn_sub, fill=GRAY_500)

        x += cw


def draw_club_admin_grid(draw, fn_label, fa_lg):
    """Club-scope admin tab: full-width 2×2 tile grid on dark slate (per simulator)."""
    BG   = (35, 42, 53)     # #232a35
    CARD = (52, 62, 79)     # #343e4f
    BORDER = (72, 86, 107)  # #48566b
    draw.rectangle([0, CY, W, H], fill=BG)

    pad  = 40
    gap  = 40
    gx0, gy0 = pad, CY + pad
    gx1, gy1 = W - pad, H - pad
    cell_w = (gx1 - gx0 - gap) // 2
    cell_h = (gy1 - gy0 - gap) // 2

    for i, tile in enumerate(CLUB_ADMIN_TILES):
        r, c = divmod(i, 2)
        x0 = gx0 + c * (cell_w + gap)
        y0 = gy0 + r * (cell_h + gap)
        x1, y1 = x0 + cell_w, y0 + cell_h
        draw.rounded_rectangle([x0, y0, x1, y1], radius=32, fill=CARD, outline=BORDER, width=3)
        cx = (x0 + x1) // 2
        cy_ic = y0 + cell_h * 38 // 100
        if fa_lg:
            draw.text((cx, cy_ic), FA.get(tile["icon"], ""), font=fa_lg,
                      fill=tile["ic"], anchor="mm")
        tb = draw.textbbox((0, 0), tile["label"], font=fn_label)
        draw.text((cx - (tb[2] - tb[0]) // 2, cy_ic + 190),
                  tile["label"], font=fn_label, fill=WHITE)


def generate(scope, tab_id):
    img  = Image.new("RGB", (W, H), WHITE)
    draw = ImageDraw.Draw(img)

    fn_tab   = load_font(72)   # slightly smaller to fit 4 tabs
    fn_tag   = load_font(50)
    fn_title = load_font(122)
    fn_sub   = load_font(62)
    fn_label = load_font(78)
    fa_lg    = get_fa_font(205)
    fa_sm    = get_fa_font(135)

    is_club = (scope == "club")
    tabs   = CLUB_TABS if is_club else TABS
    accent = ROTARY_EMERALD if is_club else ROTARY_GOLD
    draw_tab_bar(draw, tab_id, fn_tab, tabs=tabs, accent=accent)

    if tab_id == "ebooks":
        buttons = CLUB_EBOOKS_BUTTONS if is_club else EBOOKS_BUTTONS
        draw_ebooks_tab(draw, fn_label, fn_sub, fa_sm, buttons=buttons)
    elif tab_id == "admin":
        if is_club:
            draw_club_admin_grid(draw, fn_label, fa_lg)
        else:
            cfg = CONTENT["admin"]
            draw_admin_left(draw, cfg, fn_tag, fn_title, fn_sub, fn_label)
            draw_right_btn(draw, cfg["rt"], CY, BH, fn_label, fa_sm)
            draw_right_btn(draw, cfg["rb"], CY + BH, CH - BH, fn_label, fa_sm, border_top=True)
    else:
        cfg = (CLUB_CONTENT if is_club else CONTENT)[tab_id]
        draw_left_block(draw, cfg, fn_tag, fn_title, fn_sub, fa_lg)
        draw_right_btn(draw, cfg["rt"], CY, BH, fn_label, fa_sm)
        draw_right_btn(draw, cfg["rb"], CY + BH, CH - BH, fn_label, fa_sm, border_top=True)

    prefix = "richmenu_c_" if is_club else "richmenu_"
    out = f"{prefix}{tab_id}.jpg"
    img.save(out, "JPEG", quality=95)
    print(f"Saved {out}  ({W}×{H})")


if __name__ == "__main__":
    for tab_id, _ in TABS:
        generate("district", tab_id)
    for tab_id, _ in CLUB_TABS:
        generate("club", tab_id)
