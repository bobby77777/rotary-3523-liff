import hashlib
import hmac
import base64
import json
import logging
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from . import db, line_api
from .config import APP_BASE_URL, LINE_CHANNEL_SECRET, LIFF_URL

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.ensure_message_store()
    db.ensure_registrations_table()
    db.ensure_admin_users_table()
    db.ensure_user_state_table()
    db.ensure_user_roles_table()
    db.ensure_event_guests_table()
    db.ensure_golf_scores_table()
    db.ensure_club_dues_table()
    db.ensure_bulletin_editors_table()
    db.ensure_bulletin_content_table()
    db.ensure_personal_information_columns()
    yield


app = FastAPI(lifespan=lifespan)

# LIFF is served from a separate static origin and calls the API endpoints below.
# Auth is per-request via the LINE userId header, not cookies, so a wildcard origin
# is acceptable here (no ambient credentials to steal).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _verify_line_signature(body: bytes, signature: str) -> bool:
    digest = hmac.new(LINE_CHANNEL_SECRET.encode(), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature)


# ── Event schedule ────────────────────────────────────────────────────────────

# 地區活動的 PDF 走 Google Drive：把上傳後的分享連結填進 pdf_url，留空則前端顯示「PDF準備中」。
# （只有本社例會會連到社刊，故地區月例會/聯合例會也走 pdf_url。）
_EVENT_SCHEDULE = [
    {"id": 101, "date": "2024-11-22", "weekday": "星期五", "title": "RF GMS 扶輪獎助金管理研討會",     "location": "政大公企中心 2F",        "chair": "蔡輝彬 P.P. Stanley", "time": "13:30-17:30", "type": "地區會議", "fee": "免費",     "pdf_url": ""},
    {"id": 102, "date": "2025-02-25", "weekday": "星期二", "title": "DTTS 地區團隊訓練研討會",         "location": "漢來飯店",                "chair": "王維宏 P.P. JoeWang", "time": "13:00-17:00", "type": "訓練研討", "fee": "NT$300",   "pdf_url": ""},
    {"id": 103, "date": "2025-03-22", "weekday": "星期六", "title": "PETS 社長當選人訓練研討會",       "location": "美福飯店",                "chair": "許顥譪 P.P. Anthony", "time": "10:00-16:30", "type": "訓練研討", "fee": "NT$500",   "pdf_url": ""},
    {"id": 104, "date": "2025-05-24", "weekday": "星期六", "title": "DTA 地區訓練講習會 (合併 CTTS)", "location": "大直典華",                "chair": "蔡圻 P.P. Chigo",    "time": "10:00-16:30", "type": "訓練研討", "fee": "NT$500",   "pdf_url": ""},
    {"id": 105, "date": "2025-07-01", "weekday": "星期二", "title": "總監暨社長聯合就職典禮",         "location": "漢來飯店",                "chair": "蔡圻 P.P. Chigo",    "time": "11:00-14:00", "type": "年度慶典", "fee": "NT$1,200", "pdf_url": ""},
    {"id": 106, "date": "2025-07-14", "weekday": "星期一", "title": "總監盃高爾夫球比賽",             "location": "老淡水高爾夫球場",        "chair": "林星煌 P.P. Star",    "time": "整天",        "type": "地區運動", "fee": "NT$3,500", "pdf_url": ""},
    {"id": 107, "date": "2026-06-15", "weekday": "星期一", "title": "地區青少年交換(RYE)講習會",      "location": "台北福華大飯店",          "chair": "陳俊宇 P.P. RYE",    "time": "10:00-15:00", "type": "講習培訓", "fee": "免費",     "pdf_url": ""},
    {"id": 108, "date": "2026-10-24", "weekday": "星期六", "title": "第九屆地區年會暨職業服務論壇",   "location": "台北萬豪酒店 5樓萬豪廳", "chair": "張秘書長",            "time": "09:00-17:30", "type": "地區年會", "fee": "NT$2,000", "pdf_url": ""},
    {"id": 109, "date": "2026-08-15", "weekday": "星期六", "title": "3523 地區月例會",                "location": "圓山飯店",                "chair": "地區秘書處",          "time": "12:00-14:00", "type": "月例會",   "fee": "NT$800",   "pdf_url": ""},
    {"id": 110, "date": "2026-09-20", "weekday": "星期日", "title": "社際聯合例會暨餐敘",            "location": "君品酒店",                "chair": "輪值主委",            "time": "11:30-14:00", "type": "聯合活動", "fee": "NT$600",   "pdf_url": ""},
]

# ── Golf (新貝利亞 / New Peoria) ──────────────────────────────────────────────
GOLF_PARS = [4, 3, 5, 4, 4, 3, 4, 5, 4, 4, 4, 3, 5, 4, 4, 3, 5, 4]  # par 72
# New Peoria: 6 visible holes, the other 12 are "hidden" and drive the handicap.
_GOLF_VISIBLE = {2, 5, 8, 11, 14, 17}  # 0-indexed (holes 3,6,9,12,15,18)


def _new_peoria(scores: list[int]) -> dict:
    gross = sum(scores)
    hidden_sum = sum(s for i, s in enumerate(scores) if i not in _GOLF_VISIBLE)
    par_total = sum(GOLF_PARS)
    handicap = max(0.0, round(hidden_sum * 1.5 - par_total, 1))
    return {
        "gross": gross,
        "out": sum(scores[:9]),
        "in": sum(scores[9:]),
        "handicap": handicap,
        "net": round(gross - handicap, 1),
        "par": par_total,
    }


# ── Roles & viewpoint scope ───────────────────────────────────────────────────

_ROLE_NAMES = {
    "member":           "一般社友（無管理權限）",
    "chair_club_golf":  "本社高球主委（社內球隊）",
    "chair_club_admin": "社長 / 秘書（社內最高權限）",
    "chair_rye":        "地區青少年交換(RYE)主委",
    "chair_golf":       "總監盃高爾夫球主委",
    "chair_annual":     "地區年會主委",
    "admin_all":        "地區總監 / 秘書處（最高權限）",
}
_ADMIN_ROLES = set(_ROLE_NAMES) - {"member"}


def _is_golf_event(ev: dict) -> bool:
    return "高球" in ev["type"] or "高爾夫" in ev["title"] or ev["type"] == "地區運動"


def _is_rye_event(ev: dict) -> bool:
    return ev["id"] == 107 or "RYE" in ev["title"]


def _is_annual_event(ev: dict) -> bool:
    return ev["type"] == "地區年會"


def _admin_has_permission(role: str, scope: str, ev: dict | None) -> bool:
    """Mirror simulator renderAdminMenu(): a chair only sees the backstage of the
    activity they actually run, in the matching scope. admin_all sees everything."""
    if role == "admin_all":
        return True
    if scope == "club":
        if role == "chair_club_admin":
            return True
        if role == "chair_club_golf" and ev is not None and _is_golf_event(ev):
            return True
        return False
    # district scope
    if ev is None:
        return False
    if _is_golf_event(ev) and role == "chair_golf":
        return True
    if _is_rye_event(ev) and role == "chair_rye":
        return True
    if _is_annual_event(ev) and role == "chair_annual":
        return True
    return False


def _club_events(club_name: str) -> list[dict]:
    """Representative in-club schedule (sample data, parallel to _EVENT_SCHEDULE)."""
    club = club_name or "本社"
    today = date.today()
    from datetime import timedelta
    def d(days):
        return (today + timedelta(days=days)).isoformat()
    wk = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    def wd(days):
        return wk[(today + timedelta(days=days)).weekday()]
    return [
        # 社內例會連到社刊，不需 pdf_url；其他社內活動走 Google Drive，pdf_url 留空則顯示「PDF準備中」。
        {"id": 9001, "date": d(9),  "weekday": wd(9),  "title": f"{club} 第 1234 次例會 · 專題演講", "location": "本社例會餐廳", "chair": "本社社長",     "time": "12:00-14:00", "type": "社內例會", "fee": "NT$300", "scope": "club"},
        {"id": 9002, "date": d(24), "weekday": wd(24), "title": f"{club} 秋季國內旅遊",              "location": "宜蘭礁溪",     "chair": "本社旅遊主委", "time": "整天",        "type": "社內旅遊", "fee": "NT$2,800", "scope": "club", "pdf_url": ""},
        {"id": 9003, "date": d(40), "weekday": wd(40), "title": f"{club} 高球月例賽",                "location": "美麗華高爾夫", "chair": "本社高球主委", "time": "06:00-14:00", "type": "社內高球", "fee": "NT$1,800", "scope": "club", "pdf_url": ""},
    ]


def _events_for_scope(scope: str, club_name: str = "") -> list[dict]:
    if scope == "club":
        return _club_events(club_name)
    return _EVENT_SCHEDULE


def _current_event(user_id: str) -> dict | None:
    """Closest upcoming event within the user's active scope (else most recent past)."""
    scope = db.get_user_scope(user_id)
    evs = _events_for_scope(scope, db.get_user_club(user_id))
    if not evs:
        return None
    today = date.today().isoformat()
    upcoming = sorted([e for e in evs if e["date"] >= today], key=lambda e: e["date"])
    if upcoming:
        return upcoming[0]
    return sorted(evs, key=lambda e: e["date"], reverse=True)[0]


def _lookup_event(user_id: str, ev_id: int) -> dict | None:
    """Find an event by id across both district schedule and the user's club schedule."""
    ev = next((e for e in _EVENT_SCHEDULE if e["id"] == ev_id), None)
    if ev:
        return ev
    club_evs = _club_events(db.get_user_club(user_id))
    return next((e for e in club_evs if e["id"] == ev_id), None)


# ── Flex Message builders ─────────────────────────────────────────────────────

def _event_sorted(events: list[dict] | None = None) -> list[dict]:
    src = events if events is not None else _EVENT_SCHEDULE
    today = date.today().isoformat()
    upcoming = sorted([e for e in src if e["date"] >= today], key=lambda e: e["date"])
    past     = sorted([e for e in src if e["date"] <  today], key=lambda e: e["date"], reverse=True)
    return upcoming + past


def _build_event_list_carousel(events: list[dict] | None = None) -> dict:
    today = date.today().isoformat()
    bubbles = []
    for ev in _event_sorted(events)[:10]:
        is_upcoming = ev["date"] >= today
        header_bg   = "#1e3a5f" if is_upcoming else "#374151"
        badge_color = "#10b981" if is_upcoming else "#9ca3af"
        badge_text  = "即將到來" if is_upcoming else "已結束"
        bubble = {
            "type": "bubble",
            "size": "kilo",
            "header": {
                "type": "box", "layout": "vertical",
                "backgroundColor": header_bg, "paddingAll": "14px",
                "contents": [
                    {
                        "type": "box", "layout": "horizontal", "contents": [
                            {
                                "type": "box", "layout": "vertical",
                                "backgroundColor": badge_color, "cornerRadius": "4px",
                                "paddingAll": "2px", "paddingStart": "6px", "paddingEnd": "6px",
                                "contents": [{"type": "text", "text": badge_text, "size": "xxs", "weight": "bold", "color": "#ffffff"}],
                            },
                            {"type": "filler"},
                            {"type": "text", "text": ev["type"], "size": "xxs", "color": "#ffd700", "align": "end"},
                        ],
                    },
                    {"type": "text", "text": f"{ev['date']} {ev['weekday']}", "color": "#93c5fd", "size": "xxs", "margin": "sm"},
                    {"type": "text", "text": ev["title"], "color": "#ffffff", "size": "sm", "weight": "bold", "wrap": True, "margin": "sm"},
                ],
            },
            "body": {
                "type": "box", "layout": "vertical", "spacing": "xs", "paddingAll": "12px",
                "contents": [
                    {"type": "text", "text": f"📍 {ev['location']}", "size": "xs", "color": "#4b5563", "wrap": True},
                    {"type": "text", "text": f"🕐 {ev['time']}", "size": "xs", "color": "#4b5563"},
                    {"type": "text", "text": f"💰 {ev['fee']}", "size": "xs", "color": "#4b5563"},
                ],
            },
            "footer": {
                "type": "box", "layout": "vertical",
                "contents": [{
                    "type": "button",
                    "action": {"type": "postback", "label": "查看詳情", "data": f"action=view_event&id={ev['id']}"},
                    "style": "primary", "color": "#1e3a5f", "height": "sm",
                }],
            },
        }
        bubbles.append(bubble)
    return {"type": "carousel", "contents": bubbles}


def _build_event_detail_bubble(ev: dict, is_registered: bool) -> dict:
    today = date.today().isoformat()
    is_upcoming = ev["date"] >= today
    if is_registered:
        footer_contents = [{
            "type": "button",
            "action": {"type": "postback", "label": "✅ 已報名", "data": "action=noop"},
            "style": "secondary", "height": "sm", "color": "#10b981",
        }]
    elif is_upcoming:
        footer_contents = [{
            "type": "button",
            "action": {"type": "postback", "label": "立即報名", "data": f"action=register&id={ev['id']}"},
            "style": "primary", "color": "#1e3a5f", "height": "sm",
        }]
    else:
        footer_contents = [{
            "type": "button",
            "action": {"type": "postback", "label": "活動已結束", "data": "action=noop"},
            "style": "secondary", "height": "sm",
        }]
    if _is_golf_event(ev):
        footer_contents.append({
            "type": "button",
            "action": {"type": "uri", "label": "⛳ 電子計分卡", "uri": f"{LIFF_URL}?tab=home&action=golf_scorecard&event={ev['id']}"},
            "style": "secondary", "height": "sm", "margin": "sm",
        })
        footer_contents.append({
            "type": "button",
            "action": {"type": "uri", "label": "🏁 賽事排行榜", "uri": f"{LIFF_URL}?tab=home&action=leaderboard&event={ev['id']}"},
            "style": "secondary", "height": "sm",
        })
    return {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": "#1e3a5f", "paddingAll": "16px",
            "contents": [
                {"type": "text", "text": ev["type"], "size": "xxs", "color": "#ffd700", "weight": "bold"},
                {"type": "text", "text": ev["title"], "size": "md", "weight": "bold", "color": "#ffffff", "wrap": True, "margin": "sm"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md", "paddingAll": "16px",
            "contents": [
                {
                    "type": "box", "layout": "vertical", "spacing": "sm",
                    "contents": [
                        {"type": "text", "text": "📅 日期時間", "size": "xs", "color": "#9ca3af", "weight": "bold"},
                        {"type": "text", "text": f"{ev['date']}（{ev['weekday']}）{ev['time']}", "size": "sm", "color": "#1f2937", "wrap": True},
                    ],
                },
                {
                    "type": "box", "layout": "vertical", "spacing": "sm",
                    "contents": [
                        {"type": "text", "text": "📍 地點", "size": "xs", "color": "#9ca3af", "weight": "bold"},
                        {"type": "text", "text": ev["location"], "size": "sm", "color": "#1f2937", "wrap": True},
                    ],
                },
                {
                    "type": "box", "layout": "vertical", "spacing": "sm",
                    "contents": [
                        {"type": "text", "text": "👤 主委", "size": "xs", "color": "#9ca3af", "weight": "bold"},
                        {"type": "text", "text": ev["chair"], "size": "sm", "color": "#1f2937", "wrap": True},
                    ],
                },
                {
                    "type": "box", "layout": "vertical", "spacing": "sm",
                    "contents": [
                        {"type": "text", "text": "💰 費用", "size": "xs", "color": "#9ca3af", "weight": "bold"},
                        {"type": "text", "text": ev["fee"], "size": "sm", "color": "#1f2937"},
                    ],
                },
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical", "contents": footer_contents,
        },
    }


def _build_registration_success(ev: dict) -> dict:
    return {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": "#10b981", "paddingAll": "16px",
            "contents": [
                {"type": "text", "text": "✅ 報名成功", "size": "lg", "weight": "bold", "color": "#ffffff"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md", "paddingAll": "16px",
            "contents": [
                {"type": "text", "text": ev["title"], "size": "md", "weight": "bold", "color": "#1f2937", "wrap": True},
                {"type": "text", "text": f"📅 {ev['date']}（{ev['weekday']}）", "size": "sm", "color": "#4b5563"},
                {"type": "text", "text": f"📍 {ev['location']}", "size": "sm", "color": "#4b5563", "wrap": True},
                {"type": "text", "text": f"💰 費用：{ev['fee']}", "size": "sm", "color": "#4b5563"},
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": "請完成繳費後上傳匯款截圖，秘書處約 1 個工作天確認。", "size": "xs", "color": "#6b7280", "wrap": True, "margin": "md"},
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical",
            "contents": [{
                "type": "button",
                "action": {"type": "uri", "label": "📤 上傳匯款截圖", "uri": f"{LIFF_URL}?tab=profile&action=payment"},
                "style": "primary", "color": "#1e3a5f", "height": "sm",
            }],
        },
    }


def _build_profile_card(user_info: dict | None, reg_count: int) -> dict:
    if user_info:
        name_text = f"{user_info['full_name']}（{user_info.get('nickname', '')}）"
        club_text  = user_info.get("club_name", "")
        body_contents = [
            {"type": "text", "text": name_text, "size": "lg", "weight": "bold", "color": "#1f2937", "wrap": True},
            {"type": "text", "text": club_text, "size": "sm", "color": "#6b7280", "margin": "sm"},
            {"type": "separator", "margin": "lg"},
            {
                "type": "box", "layout": "horizontal", "margin": "lg",
                "contents": [
                    {
                        "type": "box", "layout": "vertical", "flex": 1, "alignItems": "center",
                        "contents": [
                            {"type": "text", "text": str(reg_count), "size": "xxl", "weight": "bold", "color": "#1e3a5f"},
                            {"type": "text", "text": "已報名活動", "size": "xs", "color": "#9ca3af"},
                        ],
                    },
                ],
            },
        ]
        footer_contents = [
            {
                "type": "button",
                "action": {"type": "postback", "label": "📋 報名紀錄", "data": "action=registrations"},
                "style": "primary", "color": "#1e3a5f", "height": "sm",
            },
            {
                "type": "button",
                "action": {"type": "postback", "label": "💰 繳費狀態", "data": "action=payments"},
                "style": "secondary", "height": "sm", "margin": "sm",
            },
        ]
    else:
        body_contents = [
            {"type": "text", "text": "尚未綁定個人資料", "size": "md", "weight": "bold", "color": "#1f2937"},
            {"type": "text", "text": "填寫後即可查詢報名紀錄、繳費狀態等功能。", "size": "sm", "color": "#6b7280", "wrap": True, "margin": "sm"},
        ]
        form_url = f"{APP_BASE_URL}/form/sign?line_user_id=__USER_ID__"
        footer_contents = [{
            "type": "button",
            "action": {"type": "uri", "label": "填寫個人資料", "uri": form_url},
            "style": "primary", "color": "#1e3a5f", "height": "sm",
        }]

    return {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": "#1e3a5f", "paddingAll": "16px",
            "contents": [
                {"type": "text", "text": "👤 個人中心", "size": "sm", "color": "#93c5fd"},
            ],
        },
        "body": {"type": "box", "layout": "vertical", "paddingAll": "16px", "contents": body_contents},
        "footer": {"type": "box", "layout": "vertical", "paddingAll": "12px", "contents": footer_contents},
    }


def _build_registrations_carousel(registrations: list[dict]) -> dict:
    ev_map = {e["id"]: e for e in _EVENT_SCHEDULE}
    bubbles = []
    for reg in registrations:
        ev = ev_map.get(reg["event_id"])
        if not ev:
            continue
        paid = reg.get("payment_status") == "confirmed"
        uploaded = reg.get("payment_status") == "uploaded"
        unpaid = reg.get("payment_status") == "unpaid"
        if paid:
            status_text, status_color = "✅ 已繳費確認", "#10b981"
        elif uploaded:
            status_text, status_color = "⏳ 待秘書處確認", "#f59e0b"
        else:
            status_text, status_color = "❗ 待繳費", "#ef4444"

        footer_btn = {
            "type": "button",
            "action": {"type": "uri", "label": "📤 上傳匯款截圖", "uri": f"{LIFF_URL}?tab=profile&action=payment"},
            "style": "primary", "color": "#1e3a5f", "height": "sm",
        } if unpaid else {
            "type": "button",
            "action": {"type": "postback", "label": status_text, "data": "action=noop"},
            "style": "secondary", "height": "sm",
        }

        bubbles.append({
            "type": "bubble", "size": "kilo",
            "body": {
                "type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "14px",
                "contents": [
                    {"type": "text", "text": ev["title"], "size": "sm", "weight": "bold", "color": "#1f2937", "wrap": True},
                    {"type": "text", "text": f"📅 {ev['date']}（{ev['weekday']}）", "size": "xs", "color": "#6b7280"},
                    {"type": "text", "text": f"💰 {ev['fee']}", "size": "xs", "color": "#6b7280"},
                    {"type": "text", "text": status_text, "size": "xs", "color": status_color, "weight": "bold", "margin": "md"},
                ],
            },
            "footer": {"type": "box", "layout": "vertical", "contents": [footer_btn]},
        })

    if not bubbles:
        bubbles = [{"type": "bubble", "body": {
            "type": "box", "layout": "vertical", "paddingAll": "16px",
            "contents": [{"type": "text", "text": "目前沒有報名紀錄", "color": "#9ca3af"}],
        }}]
    return {"type": "carousel", "contents": bubbles}


_ROLE_BADGE = {
    "chair_club_admin": "社長 / 秘書",
    "chair_club_golf":  "本社高球主委",
    "chair_golf":       "地區高球主委",
    "chair_rye":        "RYE 主委",
    "chair_annual":     "年會主委",
    "admin_all":        "最高權限",
}


def _admin_buttons(scope: str, ev: dict | None) -> list[tuple]:
    """(label, kind, target) tuples per role/event, mirroring renderAdminMenu()."""
    ev_id  = ev["id"] if ev else ""
    stats   = ("📈 報名與繳費看板", "uri", f"{LIFF_URL}?tab=admin&action=stats&event={ev_id}")
    checkin = ("📊 今日報到狀況",   "postback", "action=today_checkin")
    scanner = ("📷 現場掃碼報到",   "uri", f"{LIFF_URL}?tab=admin&action=scanner&event={ev_id}")
    search  = ("🔍 查詢會員",       "postback", "action=search_member")
    announce = ("📢 傳送公告",      "postback", "action=send_announcement")
    support = ("🎧 後台支援",       "postback", "action=admin_stub&f=support")
    vip     = ("🎤 貴賓介紹",       "uri", f"{LIFF_URL}?tab=admin&action=vip&event={ev_id}")
    execreg = ("👥 執秘批次報名",   "uri", f"{LIFF_URL}?tab=admin&action=exec_register&event={ev_id}")

    if scope == "club":
        return [
            execreg,
            ("📊 社友出席率", "uri", f"{LIFF_URL}?tab=admin&scope=club&action=attendance"),
            ("💵 社務對帳",   "postback", "action=admin_stub&f=club_finance"),
            ("👥 理監事專區", "postback", "action=admin_stub&f=board"),
            ("🧾 社友社費",   "uri", f"{LIFF_URL}?tab=admin&scope=club&action=dues"),
            checkin, scanner,
        ]
    if ev and _is_golf_event(ev):
        return [stats,
                ("🔀 即時調組",     "postback", "action=admin_stub&f=golf_swap"),
                ("🏁 賽事成績",     "uri", f"{LIFF_URL}?tab=admin&action=leaderboard&event={ev_id}"),
                ("🎲 新貝利亞抽洞", "postback", "action=admin_stub&f=draw_holes"),
                checkin, scanner]
    if ev and _is_rye_event(ev):
        return [stats,
                ("📋 面試安排",   "postback", "action=admin_stub&f=rye_interview"),
                ("✍️ 同意書審核", "postback", "action=admin_stub&f=rye_consent"),
                vip, checkin, support]
    if ev and _is_annual_event(ev):
        return [stats,
                ("🪑 桌次安排", "postback", "action=admin_stub&f=seating"),
                ("🎟️ 摸彩系統", "postback", "action=admin_stub&f=raffle"),
                vip, checkin, support]
    # default district management
    return [checkin, stats, execreg, scanner, search, announce]


def _btn(spec: tuple, primary: bool = False) -> dict:
    label, kind, target = spec
    action = ({"type": "uri", "label": label, "uri": target} if kind == "uri"
              else {"type": "postback", "label": label, "data": target})
    b = {"type": "button", "action": action, "height": "sm",
         "style": "primary" if primary else "secondary"}
    if primary:
        b["color"] = "#1e1b4b"
    return b


def _build_admin_menu(role: str, scope: str, ev: dict | None) -> dict:
    badge = _ROLE_BADGE.get(role, "管理員")
    specs = _admin_buttons(scope, ev)
    body: list[dict] = [_btn(specs[0], primary=True)]
    for i, spec in enumerate(specs[1:], start=1):
        if i == len(specs) - 2:
            body.append({"type": "separator", "margin": "md"})
        body.append(_btn(spec))
    header_bg = "#064e3b" if scope == "club" else "#1e1b4b"
    subtitle = ev["title"] if ev else ("本社管理後台" if scope == "club" else "地區秘書處")
    return {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": header_bg, "paddingAll": "16px",
            "contents": [
                {
                    "type": "box", "layout": "baseline", "contents": [
                        {"type": "text", "text": f"👑 {badge}", "size": "xs", "color": "#fbbf24", "weight": "bold", "flex": 0},
                    ],
                },
                {"type": "text", "text": "⚙️ 管理後台", "size": "lg", "weight": "bold", "color": "#ffffff", "margin": "sm"},
                {"type": "text", "text": subtitle, "size": "xs", "color": "#a5b4fc", "wrap": True, "margin": "xs"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "14px",
            "contents": body,
        },
    }


def _build_admin_unauthorized(role: str, ev: dict | None) -> dict:
    is_member = role == "member"
    title = "🔒 權限不符" if is_member else "🔒 跨活動權限限制"
    desc = ("本專區僅限管理團隊進入，您目前無權調閱此資料。" if is_member
            else "本專區僅限「主辦該活動之管理團隊」進入，您無權調閱此活動之數據。")
    target = ev["title"] if ev else "未知活動"
    return {
        "type": "bubble",
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md", "paddingAll": "20px",
            "backgroundColor": "#111827",
            "contents": [
                {"type": "text", "text": title, "size": "lg", "weight": "bold", "color": "#f87171"},
                {"type": "text", "text": desc, "size": "xs", "color": "#9ca3af", "wrap": True},
                {"type": "separator", "margin": "md", "color": "#374151"},
                {"type": "text", "text": f"嘗試存取：{target}", "size": "xs", "color": "#fbbf24", "wrap": True, "margin": "md"},
                {"type": "text", "text": "如需開通權限，請聯絡秘書處。", "size": "xxs", "color": "#6b7280", "wrap": True, "margin": "md"},
            ],
        },
    }


def _build_checkin_stats(ev: dict, checked_in: int, total: int) -> dict:
    pct = int(checked_in / total * 100) if total else 0
    bar_filled = "█" * (pct // 10)
    bar_empty  = "░" * (10 - pct // 10)
    return {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": "#1e1b4b", "paddingAll": "14px",
            "contents": [
                {"type": "text", "text": "📊 今日報到狀況", "size": "sm", "color": "#a5b4fc"},
                {"type": "text", "text": ev["title"], "size": "md", "weight": "bold", "color": "#ffffff", "wrap": True, "margin": "sm"},
                {"type": "text", "text": f"📅 {ev['date']}（{ev['weekday']}）{ev['time']}", "size": "xs", "color": "#c7d2fe", "margin": "xs"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md", "paddingAll": "16px",
            "contents": [
                {
                    "type": "box", "layout": "horizontal", "contents": [
                        {
                            "type": "box", "layout": "vertical", "flex": 1, "alignItems": "center",
                            "contents": [
                                {"type": "text", "text": str(checked_in), "size": "3xl", "weight": "bold", "color": "#10b981"},
                                {"type": "text", "text": "已報到", "size": "xs", "color": "#9ca3af"},
                            ],
                        },
                        {"type": "text", "text": "/", "size": "xl", "color": "#d1d5db", "gravity": "center"},
                        {
                            "type": "box", "layout": "vertical", "flex": 1, "alignItems": "center",
                            "contents": [
                                {"type": "text", "text": str(total), "size": "3xl", "weight": "bold", "color": "#1e3a5f"},
                                {"type": "text", "text": "已報名", "size": "xs", "color": "#9ca3af"},
                            ],
                        },
                    ],
                },
                {"type": "text", "text": f"{bar_filled}{bar_empty}  {pct}%", "size": "sm", "color": "#10b981", "align": "center", "margin": "md"},
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical",
            "contents": [{
                "type": "button",
                "action": {"type": "uri", "label": "📷 現場掃碼報到", "uri": f"{LIFF_URL}?tab=admin&action=scanner"},
                "style": "primary", "color": "#1e1b4b", "height": "sm",
            }],
        },
    }


def _build_member_result(members: list[dict]) -> dict:
    if len(members) == 1:
        m = members[0]
        return {
            "type": "bubble",
            "body": {
                "type": "box", "layout": "vertical", "spacing": "md", "paddingAll": "16px",
                "contents": [
                    {"type": "text", "text": "🔍 查詢結果", "size": "xs", "color": "#9ca3af", "weight": "bold"},
                    {"type": "text", "text": m["full_name"], "size": "xl", "weight": "bold", "color": "#1f2937", "margin": "sm"},
                    {"type": "text", "text": m.get("nickname", ""), "size": "sm", "color": "#6b7280"},
                    {"type": "separator", "margin": "lg"},
                    {"type": "text", "text": f"🏢 {m.get('club_name', '—')}", "size": "sm", "color": "#374151", "margin": "lg"},
                ],
            },
        }
    bubbles = []
    for m in members:
        bubbles.append({
            "type": "bubble", "size": "kilo",
            "body": {
                "type": "box", "layout": "vertical", "spacing": "xs", "paddingAll": "14px",
                "contents": [
                    {"type": "text", "text": m["full_name"], "size": "md", "weight": "bold", "color": "#1f2937"},
                    {"type": "text", "text": m.get("nickname", ""), "size": "xs", "color": "#6b7280"},
                    {"type": "text", "text": m.get("club_name", ""), "size": "xs", "color": "#6b7280", "margin": "sm"},
                ],
            },
        })
    return {"type": "carousel", "contents": bubbles}


def _build_award_result(rows: list[dict]) -> dict:
    total = rows[0].get("total_count", len(rows)) if rows else 0
    bubbles = []
    for r in rows:
        name = r.get("姓名") or "—"
        nick = r.get("Nickname") or ""
        club = r.get("社名") or ""
        district = r.get("分區") or ""
        award = r.get("獎項") or "—"
        slot = r.get("頒獎時段") or ""
        note = r.get("備註") or ""
        body_contents = [
            {"type": "text", "text": "🏆 得獎紀錄", "size": "xxs", "color": "#b45309", "weight": "bold"},
            {"type": "text", "text": award, "size": "md", "weight": "bold", "color": "#1f2937", "wrap": True, "margin": "sm"},
            {"type": "separator", "margin": "md"},
            {
                "type": "box", "layout": "vertical", "spacing": "xs", "margin": "md",
                "contents": [
                    {"type": "text", "text": f"👤 {name}" + (f"（{nick}）" if nick else ""), "size": "sm", "color": "#374151", "wrap": True},
                    {"type": "text", "text": f"🏢 {club}" + (f" · {district}" if district else ""), "size": "xs", "color": "#6b7280", "wrap": True},
                ],
            },
        ]
        if slot:
            body_contents.append({"type": "text", "text": f"🕐 頒獎時段：{slot}", "size": "xs", "color": "#6b7280", "wrap": True, "margin": "sm"})
        if note:
            body_contents.append({"type": "text", "text": f"📝 {note}", "size": "xs", "color": "#9ca3af", "wrap": True, "margin": "sm"})
        bubbles.append({
            "type": "bubble", "size": "kilo",
            "header": {
                "type": "box", "layout": "vertical",
                "backgroundColor": "#f59e0b", "paddingAll": "12px",
                "contents": [{"type": "text", "text": "國際扶輪 3523 地區", "size": "xxs", "color": "#ffffff", "weight": "bold"}],
            },
            "body": {"type": "box", "layout": "vertical", "paddingAll": "14px", "contents": body_contents},
        })

    if len(bubbles) > 1 and total > len(bubbles):
        bubbles.append({
            "type": "bubble", "size": "kilo",
            "body": {
                "type": "box", "layout": "vertical", "paddingAll": "16px", "justifyContent": "center",
                "contents": [
                    {"type": "text", "text": f"共 {total} 筆", "size": "sm", "weight": "bold", "color": "#374151", "align": "center"},
                    {"type": "text", "text": f"僅顯示前 {len(bubbles) - 1} 筆\n請縮小關鍵字範圍", "size": "xs", "color": "#9ca3af", "align": "center", "wrap": True, "margin": "sm"},
                ],
            },
        })
    return {"type": "carousel", "contents": bubbles}


# ── Profile handler ───────────────────────────────────────────────────────────

def _handle_profile(reply_token: str, user_id: str) -> None:
    user_infos = db.get_personal_info(user_id)
    user_info  = user_infos[0] if user_infos else None
    reg_count  = len(db.get_registrations(user_id))
    card = _build_profile_card(user_info, reg_count)
    if user_info:
        card["header"]["contents"][0]["text"] = f"👤 {user_info['full_name']}"
    qr_items = [
        {"type": "action", "action": {"type": "postback", "label": "📋 報名紀錄", "data": "action=registrations"}},
        {"type": "action", "action": {"type": "postback", "label": "💰 繳費狀態", "data": "action=payments"}},
        {"type": "action", "action": {"type": "uri",      "label": "🔑 我的QR碼", "uri": f"{LIFF_URL}?tab=profile&action=qrcode"}},
    ]
    line_api.reply_flex(reply_token, "👤 個人中心", card, quick_replies=qr_items)


# ── Postback router ───────────────────────────────────────────────────────────

def _parse_data(data: str) -> dict[str, str]:
    return dict(p.split("=", 1) for p in data.split("&") if "=" in p)


def _handle_postback(reply_token: str, user_id: str, data: str) -> None:
    logger.info("Postback: user=%s data=%s", user_id, data)

    p = _parse_data(data)
    action = p.get("action", "")

    # ── Home flows ─────────────────────────────────────────────────────────────
    if action == "event_list":
        scope = db.get_user_scope(user_id)
        events = _events_for_scope(scope, db.get_user_club(user_id))
        alt = "🏠 本社近期活動" if scope == "club" else "📅 地區近期活動"
        line_api.reply_flex(reply_token, alt, _build_event_list_carousel(events))

    elif action == "view_event":
        ev_id = int(p.get("id", 0))
        ev = _lookup_event(user_id, ev_id)
        if ev:
            is_reg = db.get_registration(user_id, ev_id) is not None
            line_api.reply_flex(reply_token, ev["title"], _build_event_detail_bubble(ev, is_reg))
        else:
            line_api.reply_text(reply_token, "找不到活動資訊。")

    elif action == "register":
        ev_id = int(p.get("id", 0))
        ev = _lookup_event(user_id, ev_id)
        if ev:
            items = [
                {"type": "action", "action": {"type": "postback", "label": "✅ 確認報名", "data": f"action=confirm_register&id={ev_id}"}},
                {"type": "action", "action": {"type": "postback", "label": "❌ 取消",     "data": "action=cancel"}},
            ]
            line_api.reply_text_with_quick_reply(
                reply_token,
                f"確認報名以下活動？\n\n📅 {ev['title']}\n📍 {ev['location']}\n🕐 {ev['date']}（{ev['weekday']}）{ev['time']}\n💰 {ev['fee']}",
                items,
            )

    elif action == "confirm_register":
        ev_id = int(p.get("id", 0))
        ev = _lookup_event(user_id, ev_id)
        if ev:
            is_new = db.register_event(user_id, ev_id)
            if is_new:
                line_api.reply_flex(reply_token, "✅ 報名成功", _build_registration_success(ev))
            else:
                line_api.reply_text(reply_token, f"您已報名過「{ev['title']}」，無需重複報名。\n如有疑問請聯絡秘書處。")

    # ── Profile flows ──────────────────────────────────────────────────────────
    elif action == "my_profile":
        _handle_profile(reply_token, user_id)

    elif action == "registrations":
        regs = db.get_registrations(user_id)
        if not regs:
            line_api.reply_text(reply_token, "您目前沒有任何報名紀錄。\n請至首頁查看近期活動。")
        else:
            line_api.reply_flex(reply_token, "📋 報名紀錄", _build_registrations_carousel(regs))

    elif action == "payments":
        regs = db.get_registrations(user_id)
        unpaid = [r for r in regs if r.get("payment_status") == "unpaid"]
        if not unpaid:
            line_api.reply_text(reply_token, "✅ 目前沒有待繳費項目。")
        else:
            ev_map = {e["id"]: e for e in _EVENT_SCHEDULE}
            lines = [f"💰 待繳費：{len(unpaid)} 筆\n"]
            for r in unpaid:
                ev = ev_map.get(r["event_id"])
                if ev:
                    lines.append(f"• {ev['title']}　{ev['fee']}")
            items = [{"type": "action", "action": {"type": "uri", "label": "📤 上傳匯款截圖", "uri": f"{LIFF_URL}?tab=profile&action=payment"}}]
            line_api.reply_text_with_quick_reply(reply_token, "\n".join(lines), items)

    # ── Admin flows ────────────────────────────────────────────────────────────
    elif action == "admin_menu":
        role  = db.get_user_role(user_id)
        scope = db.get_user_scope(user_id)
        ev    = _current_event(user_id)
        if not _admin_has_permission(role, scope, ev):
            line_api.reply_flex(reply_token, "🔒 權限不符", _build_admin_unauthorized(role, ev))
            return
        line_api.reply_flex(reply_token, "⚙️ 管理後台", _build_admin_menu(role, scope, ev))

    elif action == "today_checkin":
        role  = db.get_user_role(user_id)
        scope = db.get_user_scope(user_id)
        ev    = _current_event(user_id)
        if not _admin_has_permission(role, scope, ev):
            line_api.reply_flex(reply_token, "🔒 權限不符", _build_admin_unauthorized(role, ev))
            return
        if ev is None:
            line_api.reply_text(reply_token, "目前沒有可報到的活動。")
            return
        total = db.get_event_registration_count(ev["id"])
        checked_in = db.get_event_checkin_count(ev["id"])
        line_api.reply_flex(reply_token, "📊 今日報到", _build_checkin_stats(ev, checked_in, total))

    elif action == "admin_stub":
        role  = db.get_user_role(user_id)
        scope = db.get_user_scope(user_id)
        ev    = _current_event(user_id)
        if not _admin_has_permission(role, scope, ev):
            line_api.reply_flex(reply_token, "🔒 權限不符", _build_admin_unauthorized(role, ev))
            return
        feature = p.get("f", "")
        names = {
            "support": "後台支援", "club_finance": "社務對帳", "board": "理監事專區",
            "golf_swap": "即時調組", "draw_holes": "新貝利亞抽洞",
            "rye_interview": "面試安排", "rye_consent": "同意書審核",
            "seating": "桌次安排", "raffle": "摸彩系統",
        }
        line_api.reply_text(reply_token, f"「{names.get(feature, feature)}」功能開發中，敬請期待 🚧")

    elif action == "search_member":
        if not db.is_admin(user_id):
            line_api.reply_text(reply_token, "⚠️ 此功能僅限地區管理員使用。")
            return
        db.set_user_state(user_id, "awaiting_search")
        items = [{"type": "action", "action": {"type": "postback", "label": "✕ 取消", "data": "action=cancel_state"}}]
        line_api.reply_text_with_quick_reply(reply_token, "🔍 請輸入會員姓名或 Nickname：", items)

    elif action == "send_announcement":
        if not db.is_admin(user_id):
            line_api.reply_text(reply_token, "⚠️ 此功能僅限地區管理員使用。")
            return
        db.set_user_state(user_id, "awaiting_announcement")
        items = [{"type": "action", "action": {"type": "postback", "label": "✕ 取消", "data": "action=cancel_state"}}]
        line_api.reply_text_with_quick_reply(reply_token, "📢 請輸入公告內容（輸入後可預覽再確認發送）：", items)

    elif action == "confirm_announcement":
        if not db.is_admin(user_id):
            return
        state = db.get_user_state(user_id)
        text = (state or {}).get("context", {}).get("text", "")
        if not text:
            line_api.reply_text(reply_token, "⚠️ 找不到公告內容，請重新操作。")
            return
        db.clear_user_state(user_id)
        user_ids = db.get_all_user_ids()
        if user_ids:
            line_api.multicast(user_ids, f"📢 地區公告\n\n{text}")
            line_api.reply_text(reply_token, f"✅ 公告已發送給 {len(user_ids)} 位會員。")
        else:
            line_api.reply_text(reply_token, "⚠️ 目前沒有已綁定的會員，無法發送公告。")

    # ── Award lookup ───────────────────────────────────────────────────────────
    elif action == "award_search":
        db.set_user_state(user_id, "awaiting_award_search")
        items = [{"type": "action", "action": {"type": "postback", "label": "✕ 取消", "data": "action=cancel_state"}}]
        line_api.reply_text_with_quick_reply(
            reply_token, "🏆 請輸入要查詢的姓名、Nickname 或社名：", items)

    # ── Shared helpers ─────────────────────────────────────────────────────────
    elif action == "cancel":
        line_api.reply_text(reply_token, "已取消。")

    elif action == "cancel_state":
        db.clear_user_state(user_id)
        line_api.reply_text(reply_token, "已取消。")

    elif action == "noop":
        pass

    # ── Calendar (backward-compat) ─────────────────────────────────────────────
    elif action == "calendar":
        line_api.reply_flex(reply_token, "📅 3523 地區年度行事曆", _build_event_list_carousel())

    elif data.startswith("action=event_detail&id="):
        try:
            ev_id = int(data.split("id=")[1])
        except (ValueError, IndexError):
            ev_id = 0
        ev = next((e for e in _EVENT_SCHEDULE if e["id"] == ev_id), None)
        if ev:
            is_reg = db.get_registration(user_id, ev_id) is not None
            line_api.reply_flex(reply_token, ev["title"], _build_event_detail_bubble(ev, is_reg))


# ── Text message state machine ────────────────────────────────────────────────

def _handle_text(reply_token: str, user_id: str, text: str) -> None:
    stripped = text.strip()
    if stripped in ("得獎查詢", "得獎", "查獎", "獎項", "查詢得獎"):
        _handle_postback(reply_token, user_id, "action=award_search")
        return

    state = db.get_user_state(user_id)
    if not state:
        line_api.reply_text(reply_token, "請使用下方選單按鈕操作 🙏")
        return

    if state["state"] == "awaiting_search":
        db.clear_user_state(user_id)
        members = db.search_member(text)
        if not members:
            line_api.reply_text(reply_token, f"找不到「{text}」相關會員。\n請確認姓名後重試。")
        else:
            line_api.reply_flex(reply_token, "🔍 查詢結果", _build_member_result(members))

    elif state["state"] == "awaiting_award_search":
        db.clear_user_state(user_id)
        kw = stripped
        rows = db.search_awards(kw) if kw else []
        if not rows:
            items = [{"type": "action", "action": {"type": "postback", "label": "🏆 再查一次", "data": "action=award_search"}}]
            line_api.reply_text_with_quick_reply(
                reply_token, f"找不到「{kw}」的得獎紀錄。\n可改用社名或 Nickname 再查一次。", items)
        else:
            line_api.reply_flex(reply_token, f"🏆 「{kw}」得獎查詢", _build_award_result(rows))

    elif state["state"] == "awaiting_announcement":
        db.set_user_state(user_id, "confirm_announcement", {"text": text})
        items = [
            {"type": "action", "action": {"type": "postback", "label": "📢 確認發送",  "data": "action=confirm_announcement"}},
            {"type": "action", "action": {"type": "postback", "label": "✏️ 重新輸入",  "data": "action=send_announcement"}},
            {"type": "action", "action": {"type": "postback", "label": "✕ 取消",       "data": "action=cancel_state"}},
        ]
        line_api.reply_text_with_quick_reply(
            reply_token,
            f"📋 公告預覽\n\n{text}\n\n確認後將廣播給所有已綁定會員。",
            items,
        )

    else:
        db.clear_user_state(user_id)
        line_api.reply_text(reply_token, "請使用下方選單按鈕操作 🙏")


# ── Event dispatcher ──────────────────────────────────────────────────────────

def _handle_event(event: dict[str, Any]) -> None:
    event_type = event.get("type")
    source     = event.get("source", {})
    user_id    = source.get("userId", "")
    reply_token = event.get("replyToken", "")

    if event_type == "postback":
        data = event.get("postback", {}).get("data", "")
        _handle_postback(reply_token, user_id, data)
        return

    if event_type != "message":
        return

    message = event.get("message", {})
    if message.get("type") != "text":
        line_api.reply_text(reply_token, "抱歉，目前僅支援文字及按鈕操作。")
        return

    _handle_text(reply_token, user_id, message.get("text", "").strip())


# ── Webhook endpoint ──────────────────────────────────────────────────────────

@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()
    signature = request.headers.get("X-Line-Signature", "")
    if not _verify_line_signature(body, signature):
        return {"status": "invalid signature"}
    payload = await request.json()
    for event in payload.get("events", []):
        background_tasks.add_task(_handle_event, event)
    return {"status": "ok"}


# ── LIFF check-in API ─────────────────────────────────────────────────────────

def _member_name(uid: str) -> str:
    rows = db.get_personal_info(uid)
    if rows:
        r = rows[0]
        nick = r.get("nickname", "")
        return f"{r.get('full_name', '')}（{nick}）" if nick else r.get("full_name", "") or "社友"
    return "社友"


@app.post("/checkin")
async def checkin(request: Request):
    """Admin scans an attendee's report QR (its value is the attendee's LINE userId).
    Header X-Line-UserId = the scanning admin. Body: {qr, event_id?}."""
    admin_uid = request.headers.get("X-Line-UserId", "")
    body = await request.json()
    attendee_uid = str(body.get("qr", "")).strip()
    event_id = body.get("event_id")

    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "message": "無報到掃描權限"}
    if not attendee_uid:
        return {"status": "invalid", "message": "QR 內容為空"}

    ev = _lookup_event(admin_uid, int(event_id)) if event_id else _current_event(admin_uid)
    if ev is None:
        return {"status": "no_event", "message": "找不到對應活動"}

    result = db.check_in(attendee_uid, ev["id"])
    name = _member_name(attendee_uid)
    resp = {
        "status": result,
        "name": name,
        "event_id": ev["id"],
        "event_title": ev["title"],
        "checked_in": db.get_event_checkin_count(ev["id"]),
        "total": db.get_event_registration_count(ev["id"]),
    }
    # Notify the attendee in their own chat when check-in succeeds.
    if result == "ok":
        try:
            line_api.push_text(attendee_uid, f"✅ 已完成【{ev['title']}】報到，歡迎蒞臨！")
        except Exception:
            logger.exception("check-in push failed for %s", attendee_uid)
    return resp


@app.get("/my_qr")
async def my_qr(request: Request):
    """Return the payload the LIFF should encode into the member's report QR."""
    uid = request.headers.get("X-Line-UserId", "")
    return {"payload": uid, "name": _member_name(uid) if uid else ""}


@app.post("/golf/scores")
async def golf_scores_submit(request: Request):
    """A player submits their own 18-hole scores for a golf event."""
    uid = request.headers.get("X-Line-UserId", "")
    if not uid:
        return {"status": "no_user", "message": "尚未登入"}
    body = await request.json()
    raw = body.get("scores", [])
    try:
        scores = [int(s) for s in raw]
    except (TypeError, ValueError):
        return {"status": "invalid", "message": "成績格式錯誤"}
    if len(scores) != 18 or any(s < 1 for s in scores):
        return {"status": "invalid", "message": "請填寫 18 洞成績（每洞至少 1 桿）"}

    event_id = body.get("event_id")
    ev = _lookup_event(uid, int(event_id)) if event_id else _current_event(uid)
    if ev is None:
        return {"status": "no_event", "message": "找不到對應賽事"}

    name = _member_name(uid)
    db.upsert_golf_score(ev["id"], uid, name, scores)
    result = _new_peoria(scores)
    return {"status": "ok", "event_title": ev["title"], **result}


@app.get("/golf/my_score")
async def golf_my_score(request: Request, event: int | None = None):
    """The player's own saved scores for a golf event (for prefilling the scorecard)."""
    uid = request.headers.get("X-Line-UserId", "")
    ev = _lookup_event(uid, event) if event else _current_event(uid)
    if ev is None or not uid:
        return {"status": "ok", "scores": None, "pars": GOLF_PARS}
    row = db.get_golf_score(ev["id"], uid)
    scores = row["scores"] if row and isinstance(row.get("scores"), list) else None
    return {"status": "ok", "event_id": ev["id"], "scores": scores, "pars": GOLF_PARS}


@app.get("/golf/leaderboard")
async def golf_leaderboard(request: Request, event: int | None = None):
    """New-Peoria net leaderboard for a golf event (open to participants)."""
    uid = request.headers.get("X-Line-UserId", "")
    ev = _lookup_event(uid, event) if event else _current_event(uid)
    if ev is None:
        return {"status": "no_event", "players": []}
    rows = db.get_golf_scores(ev["id"])
    players = []
    for r in rows:
        scores = r["scores"]
        if not isinstance(scores, list) or len(scores) != 18:
            continue
        calc = _new_peoria(scores)
        players.append({
            "name": r.get("full_name") or r.get("player_name") or "選手",
            "club": r.get("club_name") or "",
            "out": calc["out"], "in": calc["in"],
            "gross": calc["gross"], "handicap": calc["handicap"],
            "net": calc["net"], "diff": calc["gross"] - calc["par"],
        })
    players.sort(key=lambda p: (p["net"], p["gross"]))
    for i, p in enumerate(players, start=1):
        p["rank"] = i
    return {"status": "ok", "event_title": ev["title"], "players": players}


# ── Club dues (社友社費) ───────────────────────────────────────────────────────
DUES_BASE = 2100      # 長年月費
DUES_DISTRICT = 125   # 地區分攤金


def _dues_total(meal: int, iou: int, customs: list) -> int:
    return DUES_BASE + DUES_DISTRICT + (meal or 0) + (iou or 0) + sum(int(c.get("amount", 0) or 0) for c in customs)


def _dues_payload(row: dict | None) -> dict:
    meal = row["meal"] if row else 0
    iou = row["iou"] if row else 0
    customs = row["customs"] if row and isinstance(row.get("customs"), list) else []
    return {
        "meal": meal, "iou": iou, "customs": customs,
        "is_paid": bool(row["is_paid"]) if row else False,
        "base": DUES_BASE, "district": DUES_DISTRICT,
        "total": _dues_total(meal, iou, customs),
        "has_bill": bool(row and (meal or iou or customs)),
    }


@app.get("/dues/member")
async def dues_member(request: Request, club: str = "", month: str = "", uid: str = ""):
    """Secretary loads one member's dues for a month (admin only)."""
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden"}
    if not club:
        club = db.get_user_club(admin_uid)
    row = db.get_dues(club, month, uid) if (month and uid) else None
    return {"status": "ok", **_dues_payload(row)}


@app.post("/dues/save")
async def dues_save(request: Request):
    """Secretary saves a member's fee items (produces the bill)."""
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "message": "無社費記帳權限"}
    body = await request.json()
    club = str(body.get("club", "")).strip() or db.get_user_club(admin_uid)
    month = str(body.get("month", "")).strip()
    uid = str(body.get("uid", "")).strip()
    if not (club and month and uid):
        return {"status": "invalid", "message": "缺少社別 / 月份 / 社友"}
    meal = int(body.get("meal", 0) or 0)
    iou = int(body.get("iou", 0) or 0)
    customs = [{"name": str(c.get("name", "")), "amount": int(c.get("amount", 0) or 0)}
               for c in body.get("customs", []) if str(c.get("name", "")).strip()]
    db.upsert_dues(club, month, uid, meal, iou, customs)
    total = _dues_total(meal, iou, customs)
    try:
        line_api.push_text(uid, f"💰 {month} 社費帳單已產出，本月應繳 NT${total:,}。可於「個人中心 → 我的社費」查看並回報繳款。")
    except Exception:
        logger.exception("dues bill push failed for %s", uid)
    return {"status": "ok", "total": total}


@app.get("/dues/me")
async def dues_me(request: Request, month: str = ""):
    """A member views their own dues for a month."""
    uid = request.headers.get("X-Line-UserId", "")
    if not uid:
        return {"status": "no_user"}
    month = month or date.today().strftime("%Y-%m")
    club = db.get_user_club(uid)
    row = db.get_dues(club, month, uid)
    return {"status": "ok", "month": month, **_dues_payload(row)}


@app.post("/dues/pay")
async def dues_pay(request: Request):
    """A member reports payment of their own dues."""
    uid = request.headers.get("X-Line-UserId", "")
    if not uid:
        return {"status": "no_user"}
    body = await request.json()
    month = str(body.get("month", "")).strip() or date.today().strftime("%Y-%m")
    digits = str(body.get("bank_digits", "")).strip()
    if digits and (len(digits) != 5 or not digits.isdigit()):
        return {"status": "invalid", "message": "請輸入正確的匯款帳號末 5 碼"}
    club = db.get_user_club(uid)
    db.pay_dues(club, month, uid, digits)
    return {"status": "ok", "month": month}


@app.get("/events")
async def events(request: Request, scope: str = ""):
    """Single source of truth for the LIFF's event list (district or club scope)."""
    uid = request.headers.get("X-Line-UserId", "")
    if scope not in ("district", "club"):
        scope = db.get_user_scope(uid) if uid else "district"
    club = db.get_user_club(uid) if uid else ""
    evs = _events_for_scope(scope, club)
    return {"status": "ok", "scope": scope, "events": evs}


@app.get("/me")
async def me(request: Request):
    """The caller's role / scope / club — LIFF uses this to gate the admin tab."""
    uid = request.headers.get("X-Line-UserId", "")
    if not uid:
        return {"status": "no_user", "is_admin": False, "role": "member"}
    return {
        "status": "ok",
        "role": db.get_user_role(uid),
        "scope": db.get_user_scope(uid),
        "club": db.get_user_club(uid),
        "is_admin": db.is_admin(uid),
        "name": _member_name(uid),
    }


@app.get("/bulletin/can_edit")
async def bulletin_can_edit(request: Request):
    """Whether the caller may edit the weekly bulletin — DB-driven 社刊主委 whitelist."""
    uid = request.headers.get("X-Line-UserId", "")
    return {"status": "ok", "can_edit": db.is_bulletin_editor(uid)}


@app.post("/bulletin/content")
async def publish_bulletin_content(request: Request):
    """社刊主委按『產生 PDF』時發布成品內容（四頁 HTML + 品牌色，JSON）。
    社員之後 GET /bulletin/content 取得此版，線上閱覽並自行列印成向量 PDF。"""
    uid = request.headers.get("X-Line-UserId", "")
    if not db.is_bulletin_editor(uid):
        raise HTTPException(status_code=403, detail="Not a bulletin editor")
    raw = (await request.body()).decode("utf-8")
    if not raw.strip():
        raise HTTPException(status_code=400, detail="Empty content body")
    try:
        json.loads(raw)  # 僅驗證為合法 JSON，實際原文照存（內含 base64 圖片）
    except ValueError:
        raise HTTPException(status_code=400, detail="Body is not valid JSON")
    db.save_bulletin_content(raw)
    return {"status": "ok"}


@app.get("/bulletin/content")
async def get_bulletin_content():
    """社員唯讀版載入主委發布的最新社刊內容；尚未發布時回 404，前端退回預設範本。"""
    raw = db.get_bulletin_content()
    if raw is None:
        raise HTTPException(status_code=404, detail="No bulletin published yet")
    return Response(content=raw, media_type="application/json")


@app.post("/payment/report")
async def payment_report(request: Request):
    """Self-register and/or report transfer digits. Stores into registrations.bank_digits."""
    uid = request.headers.get("X-Line-UserId", "")
    if not uid:
        return {"status": "no_user", "message": "尚未登入"}
    body = await request.json()
    digits = str(body.get("bank_digits", "")).strip()
    if digits and (len(digits) != 5 or not digits.isdigit()):
        return {"status": "invalid", "message": "請輸入正確的匯款帳號末 5 碼"}

    event_id = body.get("event_id")
    ev = (_lookup_event(uid, int(event_id)) if event_id else None) or _current_event(uid)
    if ev is None:
        return {"status": "no_event", "message": "找不到對應活動"}

    res = db.report_payment(uid, ev["id"], digits)
    return {
        "status": "ok",
        "event_id": ev["id"],
        "event_title": ev["title"],
        "was_registered": res["was_registered"],
        "bank_digits": digits,
    }


@app.get("/attendance/me")
async def attendance_me(request: Request):
    """A member's own attendance record (which events they checked in to)."""
    uid = request.headers.get("X-Line-UserId", "")
    rows = db.get_member_attendance(uid)
    events = []
    for r in rows:
        ev = _lookup_event(uid, r["event_id"])
        events.append({
            "event_id": r["event_id"],
            "title": ev["title"] if ev else f"活動 #{r['event_id']}",
            "date": ev["date"] if ev else "",
            "checked_in": bool(r["checked_in"]),
        })
    attended = sum(1 for e in events if e["checked_in"])
    return {
        "status": "ok",
        "name": _member_name(uid) if uid else "",
        "registered": len(events),
        "attended": attended,
        "rate": round(attended / len(events) * 100) if events else 0,
        "events": events,
    }


@app.get("/admin/registrants")
async def admin_registrants(request: Request, event: int | None = None, club: str = ""):
    """Registration name list for an event (admin only); optionally filtered to a club."""
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "registrants": []}
    ev = (_lookup_event(admin_uid, int(event)) if event else None) or _current_event(admin_uid)
    if ev is None:
        return {"status": "no_event", "registrants": []}
    rows = db.get_event_registrants(ev["id"], club)
    registrants = [
        {
            "name": r["full_name"] + (f"（{r['nickname']}）" if r.get("nickname") else ""),
            "club": r.get("club_name", ""),
            "checked_in": bool(r["checked_in"]),
            "paid": r["payment_status"] == "confirmed",
            "uploaded": r["payment_status"] == "uploaded",
            "by_secretary": bool(r.get("registered_by")),
        }
        for r in rows
    ]
    return {"status": "ok", "event_title": ev["title"], "count": len(registrants), "registrants": registrants}


@app.get("/admin/club_attendance")
async def admin_club_attendance(request: Request, club: str = ""):
    """Per-member attendance leaderboard for a club (admin only)."""
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "members": []}
    if not club:
        club = db.get_user_club(admin_uid)
    rows = db.get_club_attendance(club)
    members = [
        {
            "name": r["full_name"] + (f"（{r['nickname']}）" if r.get("nickname") else ""),
            "registered": r["registered"],
            "attended": r["attended"],
            "rate": round(r["attended"] / r["registered"] * 100) if r["registered"] else 0,
        }
        for r in rows
    ]
    total_att = sum(m["attended"] for m in members)
    total_reg = sum(m["registered"] for m in members)
    return {
        "status": "ok",
        "club": club,
        "member_count": len(members),
        "avg_rate": round(total_att / total_reg * 100) if total_reg else 0,
        "members": members,
    }


@app.get("/admin/clubs")
async def admin_clubs(request: Request):
    """Club dropdown + members for the exec-secretary bulk-register form (admin only)."""
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "clubs": []}
    return {"status": "ok", "clubs": db.list_clubs()}


@app.get("/admin/club_members")
async def admin_club_members(request: Request, club: str = ""):
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "members": []}
    return {"status": "ok", "members": db.get_club_members(club)}


@app.post("/admin/bulk_register")
async def admin_bulk_register(request: Request):
    """Exec secretary registers many club members (and guests) for one event at once."""
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "message": "無批次報名權限"}
    body = await request.json()
    uids = [str(u) for u in body.get("uids", []) if u]
    guests = [str(g) for g in body.get("guests", []) if g]
    bank_digits = str(body.get("bank_digits", "")).strip()
    event_id = body.get("event_id")

    ev = _lookup_event(admin_uid, int(event_id)) if event_id else _current_event(admin_uid)
    if ev is None:
        return {"status": "no_event", "message": "找不到對應活動"}
    if not uids and not guests:
        return {"status": "empty", "message": "未選擇任何社友或來賓"}

    result = db.bulk_register(uids, ev["id"], bank_digits, admin_uid)
    guest_count = db.add_event_guests(ev["id"], guests, admin_uid, bank_digits)

    # Notify each newly-registered member in their own chat.
    for uid in uids:
        try:
            line_api.push_text(uid, f"📋 執秘已代您報名【{ev['title']}】，如有疑問請洽社務行政。")
        except Exception:
            logger.exception("bulk-register push failed for %s", uid)

    return {
        "status": "ok",
        "event_id": ev["id"],
        "event_title": ev["title"],
        "members_new": result["new"],
        "members_dup": result["dup"],
        "guests": guest_count,
    }


@app.get("/admin/stats")
async def admin_stats(request: Request, event: int | None = None):
    """Live registration / payment / check-in dashboard for one event (admin only)."""
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "message": "無統計檢視權限"}
    ev = _lookup_event(admin_uid, event) if event else _current_event(admin_uid)
    if ev is None:
        return {"status": "no_event", "message": "找不到對應活動"}
    stats = db.get_event_stats(ev["id"])
    return {
        "status": "ok",
        "event_id": ev["id"],
        "event_title": ev["title"],
        "event_date": ev["date"],
        **stats,
    }


# ── Form endpoints ────────────────────────────────────────────────────────────

@app.get("/form/sign", response_class=HTMLResponse)
async def form_get(request: Request, line_user_id: str = ""):
    return templates.TemplateResponse(
        "form.html",
        {
            "request": request,
            "line_user_id": line_user_id,
            "success": False,
            "error": None,
            "values": {"club": "", "full_name": "", "nickname": "", "diet_type": ""},
        },
    )


@app.post("/form/sign", response_class=HTMLResponse)
async def form_post(
    request: Request,
    line_user_id: str = "",
    club: str = Form(""),
    full_name: str = Form(""),
    nickname: str = Form(""),
    diet_type: str = Form(""),
):
    values = {"club": club, "full_name": full_name, "nickname": nickname, "diet_type": diet_type}

    if not all([club, full_name, nickname, diet_type]):
        return templates.TemplateResponse(
            "form.html",
            {"request": request, "line_user_id": line_user_id,
             "success": False, "error": "請填寫所有欄位", "values": values},
        )

    try:
        db.upsert_personal_info(line_user_id, club, full_name, nickname, diet_type)
    except Exception:
        logger.exception("DB upsert failed for user %s", line_user_id)
        return templates.TemplateResponse(
            "form.html",
            {"request": request, "line_user_id": line_user_id,
             "success": False, "error": "儲存失敗，請稍後再試", "values": values},
        )

    if line_user_id:
        line_api.push_text(line_user_id, "✅ 個人資料已儲存！")

    return templates.TemplateResponse(
        "form.html",
        {"request": request, "line_user_id": line_user_id,
         "success": True, "error": None, "values": values},
    )
