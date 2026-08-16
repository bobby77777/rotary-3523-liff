import hashlib
import hmac
import base64
import json
import logging
import random
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from . import agenda_pdf, db, event_pdfs, line_api, notices
from urllib.parse import quote
from .config import APP_BASE_URL, CALENDAR_BASE_URL, GOLF_BASE_URL, LINE_CHANNEL_SECRET, LIFF_URL

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_TPE = timezone(timedelta(hours=8))   # 現場時間一律用台北時間，不跟著伺服器時區跑

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
    db.ensure_event_surveys_table()
    db.ensure_event_vips_table()
    db.ensure_golf_groups_table()
    db.ensure_event_seating_table()
    db.ensure_raffle_tables()
    db.ensure_rye_applicants_table()
    db.ensure_board_tables()
    db.ensure_bulletin_editors_table()
    db.ensure_bulletin_content_table()
    db.ensure_club_finance_table()
    db.ensure_member_business_table()
    db.ensure_events_table()
    db.ensure_event_pdf_table()
    # First run: migrate the previously-hardcoded schedule into the editable table.
    if db.events_count() == 0:
        db.seed_events(list(_EVENT_SCHEDULE) + _club_events("本社"))
    db.ensure_personal_information_columns()
    # 背景固定抓新公文進行事曆：開機先跑一次，之後每 6 小時再查一次（只處理沒同步
    # 過的，穩態幾乎不做事）。這後端沒有排程器，這條 daemon 執行緒就是它的 cron；
    # 用執行緒是因為 sync 走的是 requests/Drive/OpenAI 的同步 I/O，不該卡住啟動。
    threading.Thread(target=notices.run_periodic, daemon=True).start()
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
# Default hidden holes when no draw has happened: 12 hidden (6 visible), i.e. Double
# Peoria. After a draw, an event stores its own hidden-hole set (e.g. 6 holes).
_GOLF_VISIBLE = {2, 5, 8, 11, 14, 17}  # 0-indexed (holes 3,6,9,12,15,18)
_DEFAULT_HIDDEN = set(range(18)) - _GOLF_VISIBLE


# 球場方案：收費依球場、依場次而異（總監盃跟社內球敘不會同價），所以存在活動的
# golf_plans 欄位，由執秘在行事曆管理裡編輯——程式裡沒有任何價格。
# 'plays' = False 代表不下場（只出席晚宴），那種人不必登錄差點。
_PLAN_CODES = "ABCDEFGHIJ"


def _normalize_golf_plans(raw) -> tuple[list[dict], str]:
    """執秘存檔時把方案列表整理成正規形式 -> (plans, error).
    代碼由順序決定（A、B、C…），執秘只要填名稱與金額。"""
    if raw in (None, ""):
        return [], ""
    if not isinstance(raw, list):
        return [], "球場方案格式錯誤"
    if len(raw) > len(_PLAN_CODES):
        return [], f"球場方案最多 {len(_PLAN_CODES)} 種"
    plans = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            return [], "球場方案格式錯誤"
        label = str(item.get("label", "")).strip()
        if not label:
            return [], f"第 {i + 1} 個方案沒有填名稱"
        try:
            fee = int(float(item.get("fee", 0)))
        except (TypeError, ValueError):
            return [], f"「{label}」的金額不是數字"
        if fee < 0:
            return [], f"「{label}」的金額不能是負數"
        plans.append({
            "code":  _PLAN_CODES[i],
            "label": label,
            "fee":   fee,
            "note":  str(item.get("note", "")).strip(),
            # 沒指定就當作要下場；只有明確關掉的才是純晚宴之類的方案。
            "plays": bool(item.get("plays", True)),
        })
    return plans, ""


def _event_golf_plans(ev: dict | None) -> list[dict]:
    plans = (ev or {}).get("golf_plans")
    return plans if isinstance(plans, list) else []


def _find_plan(ev: dict | None, code: str | None) -> dict | None:
    if not code:
        return None
    return next((p for p in _event_golf_plans(ev) if p.get("code") == code), None)


def _plan_plays_golf(ev: dict | None, code: str | None) -> bool:
    """Whether this plan actually goes out on the course (so a handicap matters).
    An event with no plans configured doesn't distinguish, so everyone plays."""
    if not _event_golf_plans(ev):
        return True
    plan = _find_plan(ev, code)
    return bool(plan and plan.get("plays", True))


def _parse_course_plan(ev: dict | None, raw) -> tuple[str | None, str]:
    """球場方案代碼 -> (code, error), validated against this event's own plans.
    Blank is allowed here; the caller decides whether it may go without one."""
    if raw is None or not str(raw).strip():
        return None, ""
    code = str(raw).strip().upper()
    if _find_plan(ev, code) is None:
        return None, "球場方案選擇不正確，請重新選擇"
    return code, ""


def _plan_summary(ev: dict | None, code: str | None) -> str:
    """'A. 非球場會員 4,900 元' — for pushes and receipts."""
    plan = _find_plan(ev, code)
    if not plan:
        return ""
    note = f"（{plan['note']}）" if plan.get("note") else ""
    return f"{plan['code']}. {plan['label']} {plan['fee']:,} 元{note}"


def _parse_handicap(raw) -> tuple[float | None, str]:
    """Registration handicap -> (value, error). Blank is allowed here; the caller
    decides whether a golf event may go without one."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, ""
    try:
        value = round(float(raw), 1)
    except (TypeError, ValueError):
        return None, "差點請填數字"
    if not 0 <= value <= 54:
        return None, "差點請填 0 ~ 54 之間的數字"
    return value, ""


def _fmt_handicap(value: float) -> str:
    """18.0 -> '18'，18.5 維持 '18.5'，讓推播看起來像球場寫法。"""
    return str(int(value)) if float(value).is_integer() else str(value)


def _balance_by_handicap(players: list[dict], per_group: int = 4) -> list[dict]:
    """蛇形分配：依差點排序後 1-2-3-4 / 4-3-2-1 輪流發牌，各組平均差點會很接近。

    回傳的是攤平後的順序，且組界對齊 replace_golf_groups 的 i // per_group，
    所以各組人數與「依報名順序」那種分法完全一樣，只有組成不同。
    沒登錄差點的人排在最後發，不會攪亂前面各組的平衡。"""
    total = len(players)
    if total <= per_group:
        return players
    ranked = sorted((p for p in players if p.get("handicap") is not None),
                    key=lambda p: p["handicap"])
    ranked += [p for p in players if p.get("handicap") is None]

    caps = [per_group] * (total // per_group)
    if total % per_group:
        caps.append(total % per_group)
    buckets: list[list[dict]] = [[] for _ in caps]

    i, rnd = 0, 0
    while i < len(ranked):
        seq = range(len(caps)) if rnd % 2 == 0 else range(len(caps) - 1, -1, -1)
        moved = False
        for gi in seq:
            if i < len(ranked) and len(buckets[gi]) < caps[gi]:
                buckets[gi].append(ranked[i])
                i += 1
                moved = True
        if not moved:      # 容量已滿，不會發生，但別讓它變成無窮迴圈
            break
        rnd += 1
    return [p for b in buckets for p in b]


def _event_hidden_holes(ev: dict | None) -> set[int]:
    """The hidden holes to score by: the event's drawn set if any, else the default."""
    holes = (ev or {}).get("golf_holes")
    if isinstance(holes, list) and holes:
        s = {int(h) for h in holes if isinstance(h, (int, float)) and 0 <= int(h) < 18}
        if s:
            return s
    return _DEFAULT_HIDDEN


def _new_peoria(scores: list[int], hidden: set[int] | None = None) -> dict:
    hidden = hidden or _DEFAULT_HIDDEN
    gross = sum(scores)
    hidden_sum = sum(s for i, s in enumerate(scores) if i in hidden)
    par_total = sum(GOLF_PARS)
    # handicap = (sum of hidden holes, scaled to 18 holes) − par. 12 hidden → ×1.5, 6 → ×3.
    handicap = max(0.0, round(hidden_sum * (18 / len(hidden)) - par_total, 1))
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


def _is_golf_event(ev: dict | None) -> bool:
    # 報名紀錄查得到的活動不一定還在（_lookup_event 會回 None），所以這裡容忍缺欄位。
    if not ev:
        return False
    text = f"{ev.get('type', '')} {ev.get('title', '')}"
    return "高球" in text or "高爾夫" in text or ev.get("type") == "地區運動"


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
    # Events now live in an editable DB table (seeded from the lists above); the
    # 執秘 maintains them from the admin panel. All lookups go through here / db.
    return db.list_events(scope, club_name)


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
    """Find an event by id (ids are unique across district + club schedules)."""
    return db.get_event(ev_id)


# ── Flex Message builders ─────────────────────────────────────────────────────

def _event_sorted(events: list[dict] | None = None) -> list[dict]:
    src = events if events is not None else db.list_events()
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


def _build_survey_bubble(ev: dict, reminder: bool = False) -> dict:
    """參加意願調查表 pushed by the 執秘 from 報名專區. Same bubble for everyone,
    so it can go out as one multicast; the answer comes back as a postback."""
    head_text  = "🔔 出席意願提醒" if reminder else "📋 活動出席調查"
    head_color = "#f59e0b" if reminder else "#1e3a5f"
    note = ("尚未收到您的回覆，麻煩撥空點選 🙏" if reminder
            else "請點選下方按鈕回覆，秘書處將依此統計人數。")
    return {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": head_color, "paddingAll": "16px",
            "contents": [
                {"type": "text", "text": head_text, "size": "lg", "weight": "bold", "color": "#ffffff"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md", "paddingAll": "16px",
            "contents": [
                {"type": "text", "text": ev["title"], "size": "md", "weight": "bold", "color": "#1f2937", "wrap": True},
                {"type": "text", "text": f"📅 {ev['date']}（{ev['weekday']}）{ev['time']}", "size": "sm", "color": "#4b5563", "wrap": True},
                {"type": "text", "text": f"📍 {ev['location']}", "size": "sm", "color": "#4b5563", "wrap": True},
                {"type": "text", "text": f"💰 費用：{ev['fee']}", "size": "sm", "color": "#4b5563"},
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": note, "size": "xs", "color": "#6b7280", "wrap": True, "margin": "md"},
            ],
        },
        "footer": {
            "type": "box", "layout": "horizontal", "spacing": "sm",
            "contents": [
                {
                    "type": "button", "style": "primary", "color": "#10b981", "height": "sm",
                    "action": {"type": "postback", "label": "✅ 參加",
                               "data": f"action=survey_reply&id={ev['id']}&r=yes",
                               "displayText": f"我要參加「{ev['title']}」"},
                },
                {
                    "type": "button", "style": "primary", "color": "#9ca3af", "height": "sm",
                    "action": {"type": "postback", "label": "❌ 請假",
                               "data": f"action=survey_reply&id={ev['id']}&r=no",
                               "displayText": f"「{ev['title']}」我要請假"},
                },
            ],
        },
    }


def _build_motion_bubble(motion: dict, club: str) -> dict:
    """理監事議案表決票，三個選項都是 postback，票直接回寫資料庫。"""
    body = [
        {"type": "text", "text": motion["title"], "size": "md", "weight": "bold",
         "color": "#1f2937", "wrap": True},
        {"type": "text", "text": club, "size": "xs", "color": "#6b7280"},
    ]
    if motion.get("detail"):
        body += [{"type": "separator", "margin": "md"},
                 {"type": "text", "text": motion["detail"], "size": "sm", "color": "#4b5563",
                  "wrap": True, "margin": "md"}]
    vote = lambda label, code, color: {
        "type": "button", "style": "primary", "color": color, "height": "sm",
        "action": {"type": "postback", "label": label,
                   "data": f"action=board_vote&id={motion['id']}&v={code}",
                   "displayText": f"{label}：{motion['title']}"},
    }
    return {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#1e3a5f", "paddingAll": "16px",
            "contents": [{"type": "text", "text": "🗳️ 理監事議案表決", "size": "lg",
                          "weight": "bold", "color": "#ffffff"}],
        },
        "body": {"type": "box", "layout": "vertical", "spacing": "sm",
                 "paddingAll": "16px", "contents": body},
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {"type": "box", "layout": "horizontal", "spacing": "sm",
                 "contents": [vote("✅ 同意", "yes", "#10b981"), vote("❌ 反對", "no", "#ef4444")]},
                vote("➖ 棄權", "abstain", "#9ca3af"),
            ],
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
    ev_map = {e["id"]: e for e in db.list_events()}
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
            ("💵 社務對帳",   "uri", f"{LIFF_URL}?tab=admin&scope=club&action=club_finance"),
            ("👥 理監事專區", "uri", f"{LIFF_URL}?tab=admin&scope=club&action=board"),
            ("🧾 社友社費",   "uri", f"{LIFF_URL}?tab=admin&scope=club&action=dues"),
            checkin, scanner,
        ]
    if ev and _is_golf_event(ev):
        return [stats,
                ("🔀 即時調組",     "uri", f"{LIFF_URL}?tab=admin&action=golf_swap&event={ev_id}"),
                ("🏁 賽事成績",     "uri", f"{LIFF_URL}?tab=admin&action=leaderboard&event={ev_id}"),
                ("🎲 新貝利亞抽洞", "uri", f"{LIFF_URL}?tab=admin&action=draw_holes&event={ev_id}"),
                checkin, scanner]
    if ev and _is_rye_event(ev):
        return [stats,
                ("📋 面試安排",   "uri", f"{LIFF_URL}?tab=admin&action=rye_interview&event={ev_id}"),
                ("✍️ 同意書審核", "uri", f"{LIFF_URL}?tab=admin&action=rye_consent&event={ev_id}"),
                vip, checkin, support]
    if ev and _is_annual_event(ev):
        return [stats,
                ("🪑 桌次安排", "uri", f"{LIFF_URL}?tab=admin&action=seating&event={ev_id}"),
                ("🎟️ 摸彩系統", "uri", f"{LIFF_URL}?tab=admin&action=raffle&event={ev_id}"),
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
        if ev and _is_golf_event(ev):
            # 高球報名一定要登錄差點，聊天泡泡收不到數字，一律請他們用 App 報名。
            line_api.reply_text(
                reply_token,
                f"⛳️ {ev['title']} 報名需登錄您的差點（HDCP），\n"
                f"請由 App 完成報名：\n{LIFF_URL}?tab=home&event={ev_id}",
            )
        elif ev:
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

    elif action == "survey_reply":
        ev_id = int(p.get("id", 0))
        ev = _lookup_event(user_id, ev_id)
        if ev is None:
            line_api.reply_text(reply_token, "找不到活動資訊，請聯絡秘書處。")
            return
        attending = p.get("r") == "yes"
        db.set_survey_status(ev_id, user_id, "attending" if attending else "leave")
        if attending:
            # 回覆「參加」即視同報名，秘書處看到的名單才會一致。
            db.register_event(user_id, ev_id)
            line_api.reply_flex(reply_token, "✅ 報名成功", _build_registration_success(ev))
            # 這條路徑填不了差點，所以高球賽事要另外提醒他回 App 補登，
            # 否則報名差點榜上不會有他。
            if _is_golf_event(ev) and (db.get_registration(user_id, ev_id) or {}).get("handicap") is None:
                _push_receipt(user_id, f"⛳️ 別忘了登錄您的差點（HDCP），才會列入 {ev['title']} 的淨桿排名：\n"
                                       f"{LIFF_URL}?tab=home&event={ev_id}")
        else:
            line_api.reply_text(
                reply_token,
                f"已為您登記請假：\n\n📅 {ev['title']}\n\n秘書處會在報名專區看到這筆回覆。"
                "若之後改變主意，可從首頁重新報名。",
            )

    elif action == "board_vote":
        motion = db.get_board_motion(int(p.get("id", 0)))
        if motion is None:
            line_api.reply_text(reply_token, "找不到這個議案，請聯絡秘書處。")
            return
        if motion["status"] != "open":
            line_api.reply_text(reply_token, f"「{motion['title']}」已結案，無法再投票。")
            return
        if user_id not in [m["line_user_id"] for m in db.list_board_members(motion["club_name"])]:
            line_api.reply_text(reply_token, "此議案僅限本社理監事表決。")
            return
        choice = p.get("v", "")
        if choice not in ("yes", "no", "abstain"):
            return
        db.cast_board_vote(motion["id"], user_id, choice)
        label = {"yes": "✅ 同意", "no": "❌ 反對", "abstain": "➖ 棄權"}[choice]
        line_api.reply_text(reply_token, f"已記錄您的表決：{label}\n\n{motion['title']}\n"
                                         "（結案前可再點一次改票）")

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
            ev_map = {e["id"]: e for e in db.list_events()}
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
        # 其餘後台功能都已改成直接開 LIFF（見 _admin_buttons）；這裡只剩後台支援。
        if p.get("f") == "support":
            line_api.reply_text(reply_token,
                                "🎧 秘書處聯絡方式\n\n信箱：office@rotary3523.org.tw\n"
                                "系統問題請附上活動名稱與畫面截圖，我們會盡快回覆。")
        else:
            line_api.reply_text(reply_token, "請改用後台選單中的按鈕操作 🙏")

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
        line_api.reply_flex(reply_token, "📅 3523 地區年度行事曆",
                            _build_event_list_carousel(db.list_events("district")))

    elif data.startswith("action=event_detail&id="):
        try:
            ev_id = int(data.split("id=")[1])
        except (ValueError, IndexError):
            ev_id = 0
        ev = db.get_event(ev_id)
        if ev:
            is_reg = db.get_registration(user_id, ev_id) is not None
            line_api.reply_flex(reply_token, ev["title"], _build_event_detail_bubble(ev, is_reg))


# ── Text message state machine ────────────────────────────────────────────────

def _reply_calendar_link(reply_token: str, user_id: str) -> None:
    """Reply with a link to the calendar + agenda editor (calendar.html). 管理員 gets
    the editor link (?uid=) — best opened in a computer browser — others read-only."""
    if db.is_admin(user_id):
        url = CALENDAR_BASE_URL  # 開啟後自行 LINE 登入取得身分，網址不帶 uid
        text = ("🗓️ 行事曆與議程編輯器\n建議用電腦瀏覽器打開下面連結（會請您用 LINE 登入），"
                "可新增/修改活動、編排議程並匯出議程 PDF。\n\n" + url)
        label = "✏️ 編輯行事曆"
    else:
        url = CALENDAR_BASE_URL
        text = "🗓️ 年度行事曆\n點開即可檢視近期活動與議程。"
        label = "🗓️ 檢視行事曆"
    items = [{"type": "action", "action": {"type": "uri", "label": label, "uri": url}}]
    line_api.reply_text_with_quick_reply(reply_token, text, items)


def _reply_golf_link(reply_token: str, user_id: str) -> None:
    """Reply with a link to the grouping board (golf.html). 主委 can regroup and swap
    there; everyone else opens the same page read-only to find their own 組別."""
    ev = _current_event(user_id)
    # 帶上目前那場高球賽事，社友點開就直接看到該場，不必自己在下拉選單裡找。
    url = f"{GOLF_BASE_URL}?event={ev['id']}" if ev and _is_golf_event(ev) else GOLF_BASE_URL
    if db.is_admin(user_id):
        text = ("⛳ 高球分組表\n點開可依報名名單重新分組、點兩位球友即時對調"
                "（系統會通知本人），並匯出分組表 PDF。\n\n" + url)
        label = "✏️ 編排分組"
    else:
        text = "⛳ 高球分組表\n點開即可查看分組與同組球友。\n\n" + url
        label = "⛳ 查看分組"
    items = [{"type": "action", "action": {"type": "uri", "label": label, "uri": url}}]
    line_api.reply_text_with_quick_reply(reply_token, text, items)


def _handle_text(reply_token: str, user_id: str, text: str) -> None:
    stripped = text.strip()
    if stripped in ("得獎查詢", "得獎", "查獎", "獎項", "查詢得獎"):
        _handle_postback(reply_token, user_id, "action=award_search")
        return
    if stripped in ("行事曆", "行事曆管理", "編輯行事曆", "議程", "活動議程"):
        _reply_calendar_link(reply_token, user_id)
        return
    if stripped in ("高爾夫", "高球", "分組", "分組表", "高球分組", "高爾夫分組"):
        _reply_golf_link(reply_token, user_id)
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

def _push_receipt(uid: str, text: str) -> None:
    """Confirm a LIFF action in the user's own chat, as the bot.

    The LIFF used to do this with liff.sendMessages, which posts as the *member*
    — their own words in their own chat, and the bot then answered the
    unrecognised text with 「請使用下方選單按鈕操作」. Never fatal: the action
    already succeeded by the time we get here."""
    if not uid:
        return
    try:
        line_api.push_text(uid, text)
    except Exception:
        logger.exception("receipt push failed for %s", uid)


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
    # Notify the attendee in their own chat when check-in succeeds, and leave the
    # scanning admin a running tally in theirs.
    if result == "ok":
        try:
            line_api.push_text(attendee_uid, f"✅ 已完成【{ev['title']}】報到，歡迎蒞臨！")
        except Exception:
            logger.exception("check-in push failed for %s", attendee_uid)
        _push_receipt(admin_uid, f"✅ {name} 已完成【{ev['title']}】報到"
                                 f"（{resp['checked_in']}/{resp['total']}）")
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
    # 存下此刻的報名差點：之後他改報名差點，這場已結算的淨桿不該跟著變。
    reg = db.get_registration(uid, ev["id"]) or {}
    db.upsert_golf_score(ev["id"], uid, name, scores, reg.get("handicap"))
    result = _new_peoria(scores, _event_hidden_holes(ev))
    _push_receipt(uid, f"⛳️ 成績已送出【{ev['title']}】\n"
                       f"總桿 {result['gross']}、差點 {result['handicap']}、淨桿 {result['net']}（新貝利亞）")
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
    return {"status": "ok", "event_id": ev["id"], "event_title": ev["title"],
            "scores": scores, "pars": GOLF_PARS}


@app.get("/golf/leaderboard")
async def golf_leaderboard(request: Request, event: int | None = None):
    """Two net leaderboards for a golf event (open to participants): 新貝利亞 (rank)
    and 報名時登錄的差點 (reg_rank). A player with no registered handicap has no
    reg_rank and simply doesn't appear on the second board."""
    uid = request.headers.get("X-Line-UserId", "")
    ev = _lookup_event(uid, event) if event else _current_event(uid)
    if ev is None:
        return {"status": "no_event", "message": "找不到對應賽事", "players": []}
    rows = db.get_golf_scores(ev["id"])
    hidden = _event_hidden_holes(ev)
    players = []
    for r in rows:
        scores = r["scores"]
        if not isinstance(scores, list) or len(scores) != 18:
            continue
        calc = _new_peoria(scores, hidden)
        reg_hcp = r.get("reg_handicap")
        reg_hcp = round(float(reg_hcp), 1) if reg_hcp is not None else None
        players.append({
            "name": r.get("full_name") or r.get("player_name") or "選手",
            "club": r.get("club_name") or "",
            "out": calc["out"], "in": calc["in"],
            "gross": calc["gross"], "handicap": calc["handicap"],
            "net": calc["net"], "diff": calc["gross"] - calc["par"],
            "reg_handicap": reg_hcp,
            "reg_net": round(calc["gross"] - reg_hcp, 1) if reg_hcp is not None else None,
            "reg_rank": None,
        })
    for i, p in enumerate(sorted([p for p in players if p["reg_net"] is not None],
                                key=lambda p: (p["reg_net"], p["gross"])), start=1):
        p["reg_rank"] = i
    players.sort(key=lambda p: (p["net"], p["gross"]))
    for i, p in enumerate(players, start=1):
        p["rank"] = i
    return {
        "status": "ok",
        "event_title": ev["title"],
        "players": players,
        "reg_count": sum(1 for p in players if p["reg_rank"]),
    }


@app.post("/golf/draw_holes")
async def golf_draw_holes(request: Request):
    """高球主委抽出新貝利亞隱藏洞：前 9、後 9 各隨機 3 洞，存到該賽事並用於淨桿重算。
    已抽過就回傳原本的（避免出分後被改），除非帶 redraw=true。"""
    uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(uid):
        raise HTTPException(status_code=403, detail="Not an admin")
    body = await request.json()
    event_id = body.get("event_id")
    ev = _lookup_event(uid, int(event_id)) if event_id else _current_event(uid)
    if ev is None:
        return {"status": "no_event", "message": "找不到對應賽事"}

    existing = ev.get("golf_holes")
    if isinstance(existing, list) and existing and not body.get("redraw"):
        return {"status": "ok", "already": True, "event_title": ev["title"],
                "holes": sorted(int(h) for h in existing)}

    holes = sorted(random.sample(range(0, 9), 3) + random.sample(range(9, 18), 3))
    db.save_golf_holes(ev["id"], holes)
    _push_receipt(uid, f"🎲 新貝利亞抽洞完成【{ev['title']}】\n"
                       f"隱藏洞：H{'、H'.join(str(h + 1) for h in holes)}")
    return {"status": "ok", "already": False, "event_title": ev["title"], "holes": holes}


# ── Club dues (社友社費) ───────────────────────────────────────────────────────
DUES_BASE = 2100      # 常年月費
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
    _push_receipt(uid, f"💰 已回報 {month} 社費繳款" + (f"，末 5 碼：{digits}" if digits else "")
                  + "\n執秘對帳後狀態會更新為「已收繳費」。")
    return {"status": "ok", "month": month}


@app.get("/events")
async def events(request: Request, scope: str = ""):
    """Single source of truth for the LIFF's event list (district or club scope)."""
    uid = request.headers.get("X-Line-UserId", "")
    if scope not in ("district", "club"):
        scope = db.get_user_scope(uid) if uid else "district"
    club = db.get_user_club(uid) if uid else ""
    evs = _events_for_scope(scope, club)
    # 活動 PDF 三個來源：(1) 已存檔的議程（後端即時產生向量 PDF）；(2) 舊版存進 DB
    # 的議程 PDF；(3) 執秘上傳到 Drive 資料夾的檔案。任一存在就把 pdf_url 指到後端
    # 端點（GET /events/{id}/pdf）。
    pmap = await run_in_threadpool(event_pdfs.event_pdf_map)
    stored = db.event_pdf_ids()
    can_render = agenda_pdf.font_path() is not None

    def _with_pdfs(e: dict) -> dict:
        has_agenda_pdf = ((can_render and e.get("agenda"))
                          or e.get("id") in stored or e.get("id") in pmap)
        agenda_url = f"{APP_BASE_URL}/events/{e['id']}/pdf" if has_agenda_pdf else ""
        # 兩份文件是不同的東西，活動卡要能各開各的：公文本文是地區網站那份 PDF
        # （notices 同步時存進 pdf_url），流程表是議程產生的。以前只有一個 pdf_url，
        # 有議程就把公文連結蓋掉，公文本文就再也點不到了。
        return {**e,
                "notice_pdf_url": e.get("pdf_url") or "",
                "agenda_pdf_url": agenda_url,
                "pdf_url": agenda_url or e.get("pdf_url") or ""}   # 舊前端只認得這個

    evs = [_with_pdfs(e) for e in evs]
    # is_golf 由後端算：報名表、行事曆管理、議程編輯三處都要判斷，規則各寫一份遲早會歪。
    evs = [{**e, "is_golf": _is_golf_event(e)} for e in evs]
    return {"status": "ok", "scope": scope, "events": evs}


@app.post("/admin/events/{event_id}/pdf")
async def admin_save_event_pdf(event_id: int, request: Request):
    """儲存某活動的 PDF。Body 為 PDF 位元組。議程 PDF 現在由後端即時產生（見
    GET /events/{id}/pdf），這裡保留給舊版前端與手動上傳的備援檔。"""
    uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(uid):
        raise HTTPException(status_code=403, detail="Not an admin")
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="Empty PDF body")
    db.save_event_pdf(event_id, data)
    return {"status": "ok"}


@app.get("/events/{event_id}/pdf")
async def event_pdf(event_id: int):
    """某活動的 PDF。順序：(1) 由已存檔的議程即時產生「向量」PDF（文字可選取、
    可搜尋）；(2) 舊版由瀏覽器上傳存進 DB 的 PDF；(3) 代理串流 執秘上傳到
    Drive 的檔案。前端活動卡的 PDF 鈕直接開這個網址。"""
    ev = db.get_event(event_id)
    data = None
    if ev and ev.get("agenda"):
        data = await run_in_threadpool(agenda_pdf.build_agenda_pdf, ev)
    if data is None:
        data = db.get_event_pdf(event_id)
    if data is None:
        file_id = event_pdfs.get_pdf_file_id(event_id)
        if not file_id:
            raise HTTPException(status_code=404, detail="No PDF for this event")
        data = await run_in_threadpool(event_pdfs.download_pdf, file_id)
        if data is None:
            raise HTTPException(status_code=502, detail="Failed to fetch PDF from Drive")
    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="event-{event_id}.pdf"',
            "Cache-Control": "no-cache",
        },
    )


def _clean_event_payload(data: dict) -> dict:
    """Keep only editable event fields; drop an invalid scope so a default/existing
    value stands. Field-level validation stays light — this is an admin-only panel."""
    out = {k: data[k] for k in db._EVENT_FIELDS + ("agenda", "golf_plans") if k in data}
    if out.get("scope") not in ("club", "district"):
        out.pop("scope", None)
    # 方案關係到社友要匯多少錢，是這張表單裡唯一不能「輕度驗證」的欄位：
    # 金額寫錯會直接變成收錯錢，所以壞資料寧可擋下也不存。
    if "golf_plans" in out:
        plans, err = _normalize_golf_plans(out["golf_plans"])
        if err:
            raise HTTPException(status_code=400, detail=err)
        out["golf_plans"] = plans
    return out


@app.get("/events/can_edit")
async def events_can_edit(request: Request):
    """Whether the caller may edit the calendar — gated by the admin role (執秘 等)."""
    uid = request.headers.get("X-Line-UserId", "")
    return {"status": "ok", "can_edit": db.is_admin(uid)}


@app.post("/admin/events")
async def admin_create_event(request: Request):
    """執秘 從管理面板新增活動。"""
    uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(uid):
        raise HTTPException(status_code=403, detail="Not an admin")
    data = _clean_event_payload(await request.json())
    if not data.get("title"):
        raise HTTPException(status_code=400, detail="活動標題必填")
    return {"status": "ok", "event": db.create_event(data)}


@app.put("/admin/events/{event_id}")
async def admin_update_event(event_id: int, request: Request):
    """執秘 從管理面板修改活動。"""
    uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(uid):
        raise HTTPException(status_code=403, detail="Not an admin")
    ev = db.update_event(event_id, _clean_event_payload(await request.json()))
    if ev is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return {"status": "ok", "event": ev}


@app.delete("/admin/events/{event_id}")
async def admin_delete_event(event_id: int, request: Request):
    """執秘 從管理面板刪除活動。"""
    uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(uid):
        raise HTTPException(status_code=403, detail="Not an admin")
    db.delete_event(event_id)
    return {"status": "ok"}


@app.post("/admin/events/sync-notices")
async def admin_sync_notices(request: Request):
    """從地區網站抓 【公文】 貼文，補進地區行事曆（已同步過的略過），並回頭補讀
    當初沒讀到公文 PDF、因此還缺日期/地點/費用的那幾筆。"""
    uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(uid):
        raise HTTPException(status_code=403, detail="Not an admin")
    report = await run_in_threadpool(notices.sync_notices, True)
    return {"status": "ok", "report": report}


# 個人基本資料：社別、中文名、Nickname、葷素。少了這些，報名名單、桌次、頒獎查詢
# 都對不到人，所以第一次進 LIFF 就要填。
_DIET_TYPES = ("葷食", "素食", "海鮮素")
_PROFILE_KEYS = ("club", "full_name", "nickname", "diet_type")


def _member_profile(uid: str) -> dict:
    rows = db.get_personal_info(uid) if uid else []
    r = rows[0] if rows else {}
    return {
        "club": r.get("club_name") or "",
        "full_name": r.get("full_name") or "",
        "nickname": r.get("nickname") or "",
        "diet_type": r.get("diet_type") or "",
    }


def _profile_incomplete(profile: dict) -> bool:
    return not all(str(profile.get(k) or "").strip() for k in _PROFILE_KEYS)


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
        # 還沒填完基本資料的話，LIFF 一開就先請本人補（見 openProfileGate）。
        "needs_profile": _profile_incomplete(_member_profile(uid)),
    }


@app.get("/me/profile")
async def me_profile(request: Request):
    """本人的基本資料，連社別選單一起給——新社友要自己填，沒有 admin 權限可以撈社名。"""
    uid = request.headers.get("X-Line-UserId", "")
    if not uid:
        return {"status": "no_user", "message": "請從 LINE 開啟"}
    profile = _member_profile(uid)
    return {
        "status": "ok",
        "profile": profile,
        "needs_profile": _profile_incomplete(profile),
        "clubs": db.list_clubs(),
        "diet_types": list(_DIET_TYPES),
    }


@app.post("/me/profile")
async def me_profile_save(request: Request):
    """本人存自己的基本資料。身分一律看 header，不吃 body 傳來的 uid。"""
    uid = request.headers.get("X-Line-UserId", "")
    if not uid:
        return {"status": "no_user", "message": "請從 LINE 開啟"}
    body = await request.json()
    values = {k: str(body.get(k) or "").strip() for k in _PROFILE_KEYS}
    if not all(values.values()):
        return {"status": "invalid", "message": "四個欄位都要填"}
    if any(len(v) > 40 for v in values.values()):
        return {"status": "invalid", "message": "每個欄位最多 40 個字"}
    if values["diet_type"] not in _DIET_TYPES:
        return {"status": "invalid", "message": "葷素請從選單選擇"}
    db.upsert_personal_info(uid, values["club"], values["full_name"],
                            values["nickname"], values["diet_type"])
    return {"status": "ok", "profile": values}


@app.get("/awards/me")
async def awards_me(request: Request):
    """This member's own awards (matched by their 姓名 / Nickname)."""
    uid = request.headers.get("X-Line-UserId", "")
    if not uid:
        return {"status": "no_user", "awards": [], "count": 0}
    awards = db.get_member_awards(uid)
    return {"status": "ok", "name": _member_name(uid), "awards": awards, "count": len(awards)}


@app.get("/awards/club")
async def awards_club(request: Request, club: str = ""):
    """All awards for a club (defaults to the caller's own club)."""
    uid = request.headers.get("X-Line-UserId", "")
    if not club:
        club = db.get_user_club(uid) if uid else ""
    awards = db.get_club_awards(club)
    return {"status": "ok", "club": club, "awards": awards, "count": len(awards)}


@app.get("/club/finance")
async def club_finance_get(request: Request, month: str = ""):
    """Load a club's monthly finance sheet (admin). Defaults to the caller's club
    and the current month; returns empty defaults when nothing is saved yet."""
    uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(uid):
        raise HTTPException(status_code=403, detail="Not an admin")
    club = db.get_user_club(uid)
    if not club:
        return {"status": "no_club", "message": "找不到您的社別"}
    month = month or date.today().strftime("%Y-%m")
    data = db.get_club_finance(club, month) or {"rent": 20000, "salary": 35000,
                                                "fixed": [], "advances": []}
    return {"status": "ok", "club": club, "month": month, "data": data}


@app.post("/club/finance")
async def club_finance_save(request: Request):
    """Save a club's monthly finance sheet (admin)."""
    uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(uid):
        raise HTTPException(status_code=403, detail="Not an admin")
    club = db.get_user_club(uid)
    if not club:
        return {"status": "no_club", "message": "找不到您的社別"}
    body = await request.json()
    month = body.get("month") or date.today().strftime("%Y-%m")
    data = {
        "rent": int(body.get("rent") or 0),
        "salary": int(body.get("salary") or 0),
        "fixed": [{"name": str(f.get("name", "")), "amount": int(f.get("amount") or 0)}
                  for f in body.get("fixed", []) if f.get("name")],
        "advances": [{"member": str(a.get("member", "")), "detail": str(a.get("detail", "")),
                      "amount": int(a.get("amount") or 0)}
                     for a in body.get("advances", []) if a.get("amount")],
    }
    db.save_club_finance(club, month, data)
    total = data["rent"] + data["salary"] + sum(f["amount"] for f in data["fixed"]) \
        + sum(a["amount"] for a in data["advances"])
    return {"status": "ok", "month": month, "total": total, "advance_count": len(data["advances"])}


@app.get("/me/business")
async def my_business_get(request: Request):
    """The caller's own business card (職業名片)."""
    uid = request.headers.get("X-Line-UserId", "")
    if not uid:
        return {"status": "no_user", "business": None}
    b = db.get_member_business(uid) or {"industry": "", "company": "", "intro": "", "offer": ""}
    return {"status": "ok", "business": b}


@app.post("/me/business")
async def my_business_save(request: Request):
    """Save the caller's own business card."""
    uid = request.headers.get("X-Line-UserId", "")
    if not uid:
        return {"status": "no_user", "message": "尚未登入"}
    body = await request.json()
    db.save_member_business(
        uid,
        str(body.get("industry", "")).strip(),
        str(body.get("company", "")).strip(),
        str(body.get("intro", "")).strip(),
        str(body.get("offer", "")).strip(),
    )
    return {"status": "ok"}


@app.get("/matchmaking")
async def matchmaking(request: Request, q: str = ""):
    """Industry matchmaking: find fellow Rotarians whose business card matches a need."""
    uid = request.headers.get("X-Line-UserId", "")
    if not q.strip():
        return {"status": "empty", "matches": [], "count": 0}
    rows = db.search_business(q, exclude_uid=uid, limit=8)
    matches = [{
        "name": r.get("full_name") or r.get("nickname") or "社友",
        "club": r.get("club_name") or "",
        "industry": r.get("industry") or "",
        "company": r.get("company") or "",
        "intro": r.get("intro") or "",
        "offer": r.get("offer") or "",
    } for r in rows]
    return {"status": "ok", "query": q, "matches": matches, "count": len(matches)}


@app.get("/bulletin/can_edit")
async def bulletin_can_edit(request: Request):
    """Whether the caller may edit the weekly bulletin — DB-driven 社刊主委 whitelist."""
    uid = request.headers.get("X-Line-UserId", "")
    return {"status": "ok", "can_edit": db.is_bulletin_editor(uid)}


@app.post("/bulletin/content")
async def publish_bulletin_content(request: Request, event: int | None = None):
    """發布某場例會的社刊（四頁 HTML + 品牌色，JSON）。每個例會活動一份，以 ?event= 指定。"""
    uid = request.headers.get("X-Line-UserId", "")
    if not (db.is_bulletin_editor(uid) or db.is_admin(uid)):
        raise HTTPException(status_code=403, detail="Not allowed to edit bulletin")
    if not event:
        raise HTTPException(status_code=400, detail="缺少活動 id（?event=），無法發布社刊")
    club = db.get_user_club(uid)
    raw = (await request.body()).decode("utf-8")
    if not raw.strip():
        raise HTTPException(status_code=400, detail="Empty content body")
    try:
        json.loads(raw)  # 僅驗證為合法 JSON，實際原文照存（內含 base64 圖片）
    except ValueError:
        raise HTTPException(status_code=400, detail="Body is not valid JSON")
    db.save_bulletin_content(event, club, raw)
    return {"status": "ok", "event": event, "club": club}


@app.get("/bulletin/content")
async def get_bulletin_content(request: Request, event: int | None = None, club: str = ""):
    """讀社刊：帶 ?event= 取該場例會的社刊；否則帶 ?club=（或用呼叫者的社）取該社最新一份。
    尚未發布時回 404，前端退回預設範本。"""
    if event:
        raw = db.get_bulletin_content(event)
    else:
        if not club:
            uid = request.headers.get("X-Line-UserId", "")
            club = db.get_user_club(uid) if uid else ""
        raw = db.get_club_latest_bulletin(club) if club else None
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

    handicap, hcp_err = _parse_handicap(body.get("handicap"))
    if hcp_err:
        return {"status": "invalid", "message": hcp_err}
    plan, plan_err = _parse_course_plan(ev, body.get("course_plan"))
    if plan_err:
        return {"status": "invalid", "message": plan_err}

    # 高球賽事報名一定要帶球場方案和差點。已經報名的人不再擋——他們可能只是來
    # 回報匯款，缺的欄位首頁會有補填入口。
    if _is_golf_event(ev) and db.get_registration(uid, ev["id"]) is None:
        # 沒設定方案的場次就不問方案——例如社內球敘只收一種費用。
        if plan is None and _event_golf_plans(ev):
            return {"status": "invalid", "message": "高爾夫球賽報名請選擇球場方案"}
        # 只參加晚宴的人不下場，要他填差點沒有意義。
        if handicap is None and _plan_plays_golf(ev, plan):
            return {"status": "invalid", "message": "高爾夫球賽報名請填寫您的差點"}

    res = db.report_payment(uid, ev["id"], digits, handicap, plan)
    if res["was_registered"]:
        note = f"💰 已回報【{ev['title']}】匯款末 5 碼：{digits}\n秘書處對帳後會通知您。"
    else:
        note = f"✅ 報名成功：{ev['title']}\n{ev['date']}（{ev['weekday']}）{ev['time']}　{ev['location']}"
        note += (f"\n匯款末 5 碼：{digits}（待對帳）" if digits
                 else "\n完成匯款後請至「個人中心 → 回報匯款」補填末 5 碼。")
    if plan is not None:
        note += f"\n⛳️ 球場方案：{_plan_summary(ev, plan)}"
    if handicap is not None:
        note += f"\n⛳️ 登錄差點：{_fmt_handicap(handicap)}"
    _push_receipt(uid, note)
    return {
        "status": "ok",
        "event_id": ev["id"],
        "event_title": ev["title"],
        "was_registered": res["was_registered"],
        "bank_digits": digits,
        "handicap": handicap,
        "course_plan": plan,
        "course_plan_label": _plan_summary(ev, plan),
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
            "location": ev["location"] if ev else "",
            "time": ev["time"] if ev else "",
            "fee": ev["fee"] if ev else "",
            # 前端的「我的報名紀錄」依目前視角過濾，所以要知道這筆屬於地區還是社內
            "scope": ev["scope"] if ev else "",
            "checked_in": bool(r["checked_in"]),
            "payment_status": r["payment_status"],
            # 高球賽事用：從 LINE 泡泡報名的人沒填方案／差點，首頁要請他補填。
            # 「還缺什麼」由後端判斷，前端才不必自己複製一份「哪種方案要下場」的規則。
            "is_golf": _is_golf_event(ev),
            "handicap": r.get("handicap"),
            "course_plan": r.get("course_plan"),
            "golf_incomplete": bool(
                _is_golf_event(ev)
                and ((r.get("course_plan") is None and _event_golf_plans(ev))
                     or (r.get("handicap") is None and _plan_plays_golf(ev, r.get("course_plan"))))
            ),
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
            "handicap": r.get("handicap"),
            "course_plan": r.get("course_plan"),
            "course_plan_label": _plan_summary(ev, r.get("course_plan")),
            "course_plan_fee": (_find_plan(ev, r.get("course_plan")) or {}).get("fee"),
            # 只參加晚宴的人不下場，名單就不該把他標成「未填差點」。
            "handicap_required": _plan_plays_golf(ev, r.get("course_plan")),
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


# ── 待繳費明細 / 一鍵催繳 ─────────────────────────────────────────────────────

@app.get("/admin/unpaid")
async def admin_unpaid(request: Request, event: int | None = None):
    """Outstanding-payment drill-down for one event, grouped by club (admin only)."""
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "message": "無檢視權限", "clubs": []}
    ev = _lookup_event(admin_uid, event) if event else _current_event(admin_uid)
    if ev is None:
        return {"status": "no_event", "message": "找不到對應活動", "clubs": []}
    rows = db.get_event_unpaid(ev["id"])
    clubs: dict[str, dict] = {}
    for r in rows:
        c = clubs.setdefault(r["club_name"], {"club": r["club_name"], "unpaid": [], "reported": []})
        name = (r["full_name"] + (f"（{r['nickname']}）" if r["nickname"] else "")) or "（未填個人資料）"
        # 'uploaded' 已回報末 5 碼、只等執秘對帳 —— 列出來但不催。
        c["reported" if r["payment_status"] == "uploaded" else "unpaid"].append(name)
    out = sorted(clubs.values(), key=lambda c: (-len(c["unpaid"]), c["club"]))
    return {
        "status": "ok",
        "event_id": ev["id"],
        "event_title": ev["title"],
        "fee": ev["fee"],
        "total_unpaid": sum(len(c["unpaid"]) for c in out),
        "total_reported": sum(len(c["reported"]) for c in out),
        "clubs": out,
    }


@app.post("/admin/unpaid/remind")
async def admin_unpaid_remind(request: Request):
    """Push a payment reminder to one club's still-unpaid registrants (admin only)."""
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "message": "無催繳權限"}
    body = await request.json()
    club = str(body.get("club", "")).strip()
    ev_id = body.get("event_id")
    ev = _lookup_event(admin_uid, int(ev_id)) if ev_id else _current_event(admin_uid)
    if ev is None:
        return {"status": "no_event", "message": "找不到對應活動"}
    targets = [r for r in db.get_event_unpaid(ev["id"], club) if r["payment_status"] != "uploaded"]
    if not targets:
        return {"status": "none_unpaid", "reminded": 0}
    text = (f"🔔 繳費提醒\n\n【{ev['title']}】\n{ev['date']}（{ev['weekday']}）　{ev['location']}\n"
            f"費用：{ev['fee']}\n\n完成匯款後請至 LIFF「個人中心 → 回報匯款」填寫帳號末 5 碼，"
            "秘書處才能完成對帳。")
    sent = 0
    for r in targets:
        try:
            line_api.push_text(r["line_user_id"], text)
            sent += 1
        except Exception:
            logger.exception("payment reminder failed for %s", r["line_user_id"])
    return {"status": "ok", "club": club, "reminded": sent, "targets": len(targets)}


# ── 貴賓唱名 ─────────────────────────────────────────────────────────────────

def _vip_cutoff(ev: dict) -> str:
    """VIPs arriving after the event starts go to the 補介紹 list."""
    start = (ev.get("start_time") or "").strip()
    if not start:
        start = (ev.get("time") or "").split("-")[0].strip()
    return start if len(start) == 5 and start[2] == ":" else "10:30"


@app.get("/admin/vips")
async def admin_vips(request: Request, event: int | None = None):
    """貴賓唱名名單（主委專用）。"""
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "message": "無貴賓名單權限", "vips": []}
    ev = _lookup_event(admin_uid, event) if event else _current_event(admin_uid)
    if ev is None:
        return {"status": "no_event", "message": "找不到對應活動", "vips": []}
    return {"status": "ok", "event_id": ev["id"], "event_title": ev["title"],
            "cutoff": _vip_cutoff(ev), "vips": db.list_event_vips(ev["id"])}


@app.post("/admin/vips")
async def admin_vip_add(request: Request):
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "message": "無貴賓名單權限"}
    body = await request.json()
    name = str(body.get("name", "")).strip()
    if not name:
        return {"status": "invalid", "message": "請填寫貴賓姓名"}
    ev_id = body.get("event_id")
    ev = _lookup_event(admin_uid, int(ev_id)) if ev_id else _current_event(admin_uid)
    if ev is None:
        return {"status": "no_event", "message": "找不到對應活動"}
    return {"status": "ok", "vip": db.add_event_vip(ev["id"], name, str(body.get("title", "")).strip())}


@app.post("/admin/vips/{vip_id}")
async def admin_vip_update(vip_id: int, request: Request):
    """報到 / 唱名 / 改名銜。報到時由伺服器蓋抵達時間，現場才不會靠手機時間各說各話。"""
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "message": "無貴賓名單權限"}
    body = await request.json()
    fields = {k: body[k] for k in ("name", "title", "sort_order", "is_called") if k in body}
    if "arrived" in body:
        fields["arrived"] = bool(body["arrived"])
        fields["arrive_time"] = datetime.now(_TPE).strftime("%H:%M") if fields["arrived"] else ""
    vip = db.update_event_vip(vip_id, fields)
    if vip is None:
        return {"status": "not_found", "message": "找不到這位貴賓"}
    return {"status": "ok", "vip": vip}


@app.delete("/admin/vips/{vip_id}")
async def admin_vip_delete(vip_id: int, request: Request):
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "message": "無貴賓名單權限"}
    db.delete_event_vip(vip_id)
    return {"status": "ok"}


# ── 高球即時調組 ─────────────────────────────────────────────────────────────

@app.get("/golf/groups")
async def golf_groups(request: Request, event: int | None = None):
    """分組表。報名者本人也能看自己的組別，所以這支不擋一般社友。"""
    uid = request.headers.get("X-Line-UserId", "")
    ev = _lookup_event(uid, event) if event else _current_event(uid)
    if ev is None:
        return {"status": "no_event", "message": "找不到對應賽事", "groups": []}
    rows = db.list_golf_groups(ev["id"])
    groups: dict[int, list] = {}
    for r in rows:
        groups.setdefault(r["group_no"], []).append({
            "id": r["id"], "slot": r["slot"], "name": r["player_name"], "uid": r["line_user_id"],
            "club": r.get("club_name") or "",
            "handicap": r.get("handicap"),
            "course_plan": r.get("course_plan"),
            "course_plan_label": _plan_summary(ev, r.get("course_plan")),
            "course_plan_fee": (_find_plan(ev, r.get("course_plan")) or {}).get("fee"),
        })
    return {
        "status": "ok", "event_id": ev["id"], "event_title": ev["title"],
        "event_date": ev.get("date", ""), "event_location": ev.get("location", ""),
        "groups": [{"group_no": g, "players": p} for g, p in sorted(groups.items())],
        "players": [{"id": r["id"], "name": r["player_name"], "group_no": r["group_no"]} for r in rows],
        # 編輯球友時要能改方案，選項就是這場賽事自己設的那幾種。
        "plans": _event_golf_plans(ev),
    }


@app.post("/golf/groups/auto")
async def golf_groups_auto(request: Request):
    """依報名名單重新分組（4 人一組，含執秘代報的來賓）。會蓋掉現有分組。"""
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "message": "無分組權限"}
    body = await request.json()
    ev_id = body.get("event_id")
    ev = _lookup_event(admin_uid, int(ev_id)) if ev_id else _current_event(admin_uid)
    if ev is None:
        return {"status": "no_event", "message": "找不到對應賽事"}
    players = [
        {"uid": r["line_user_id"],
         "name": (r["full_name"] + (f"（{r['nickname']}）" if r.get("nickname") else "")) or "社友",
         "handicap": r.get("handicap")}
        for r in db.get_event_registrants(ev["id"])
    ]
    players += [{"uid": "", "name": f"{g['name']}（來賓）", "handicap": g.get("handicap"),
                 "guest_id": g["id"]}
                for g in db.get_event_guests(ev["id"])]
    if not players:
        return {"status": "empty", "message": "此賽事還沒有人報名，無法分組"}
    # mode='balanced' 依差點蛇形分配，各組實力相當；預設仍是依報名順序。
    mode = str(body.get("mode", "order")).lower()
    if mode == "balanced":
        players = _balance_by_handicap(players)
    count = db.replace_golf_groups(ev["id"], players)
    return {"status": "ok", "event_title": ev["title"], "players": count,
            "groups": (count + 3) // 4, "mode": mode}


@app.post("/golf/groups/swap")
async def golf_groups_swap(request: Request):
    """即時調組：兩位球友互換組別與順位，並通知本人。"""
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "message": "無調組權限"}
    body = await request.json()
    ev_id = body.get("event_id")
    ev = _lookup_event(admin_uid, int(ev_id)) if ev_id else _current_event(admin_uid)
    if ev is None:
        return {"status": "no_event", "message": "找不到對應賽事"}
    try:
        id_a, id_b = int(body.get("a")), int(body.get("b"))
    except (TypeError, ValueError):
        return {"status": "invalid", "message": "請選擇兩位要對調的球友"}
    if id_a == id_b:
        return {"status": "invalid", "message": "請選擇兩位不同的球友"}
    swapped = db.swap_golf_players(ev["id"], id_a, id_b)
    if swapped is None:
        return {"status": "not_found", "message": "找不到這兩位球友的分組資料"}
    a, b = swapped
    for p in (a, b):
        if p["line_user_id"]:
            _push_receipt(p["line_user_id"],
                          f"🔀 分組調整通知【{ev['title']}】\n"
                          f"{p['player_name']} 已改分至第 {p['group_no']} 組（第 {p['slot']} 位）。")
    return {"status": "ok",
            "a": {"name": a["player_name"], "group_no": a["group_no"]},
            "b": {"name": b["player_name"], "group_no": b["group_no"]}}


def _golf_row_target(admin_uid: str, body: dict) -> tuple[dict | None, dict | None, dict | None]:
    """編輯與移除都要先確認：是主委、賽事在、而且那一列真的屬於這場賽事。
    回傳 (賽事, 分組列, 錯誤)，錯誤不是 None 時就直接把它回給前端。"""
    if not db.is_admin(admin_uid):
        return None, None, {"status": "forbidden", "message": "無分組權限"}
    ev_id = body.get("event_id")
    ev = _lookup_event(admin_uid, int(ev_id)) if ev_id else _current_event(admin_uid)
    if ev is None:
        return None, None, {"status": "no_event", "message": "找不到對應賽事"}
    try:
        row_id = int(body.get("id"))
    except (TypeError, ValueError):
        return None, None, {"status": "invalid", "message": "請選擇一位球友"}
    row = db.get_golf_player(ev["id"], row_id)
    if row is None:
        return None, None, {"status": "not_found", "message": "找不到這位球友的分組資料"}
    return ev, row, None


@app.post("/golf/groups/update")
async def golf_groups_update(request: Request):
    """訂正分組表上某一位的姓名與差點。"""
    admin_uid = request.headers.get("X-Line-UserId", "")
    body = await request.json()
    ev, row, err = _golf_row_target(admin_uid, body)
    if err is not None:
        return err
    name = str(body.get("name") or "").strip() or row["player_name"]
    handicap, hcp_err = _parse_handicap(body.get("handicap"))
    if hcp_err:
        return {"status": "invalid", "message": hcp_err}
    # 沒送 course_plan 就維持原方案——舊版頁面（瀏覽器快取）不會送這個欄位，
    # 不能因此把人家選好的方案清掉。送空字串才是「清掉方案」。
    plan = row.get("course_plan")
    if "course_plan" in body:
        plan, plan_err = _parse_course_plan(ev, body.get("course_plan"))
        if plan_err:
            return {"status": "invalid", "message": plan_err}
    db.update_golf_player(ev["id"], row["id"], name, handicap)
    # 差點與方案也要寫回來源。重新分組是整批刪除重建，只改分組表的話一按就被舊值蓋回去。
    if row["line_user_id"]:
        db.set_registration_golf_info(ev["id"], row["line_user_id"], handicap, plan)
    elif row.get("guest_id"):
        db.set_guest_golf_info(row["guest_id"], handicap, plan)
    return {"status": "ok", "name": name, "handicap": handicap, "course_plan": plan}


@app.post("/golf/groups/move")
async def golf_groups_move(request: Request):
    """把一位球友搬到另一組的空位。對調要兩個人，空位沒有人可以換。"""
    admin_uid = request.headers.get("X-Line-UserId", "")
    body = await request.json()
    ev, row, err = _golf_row_target(admin_uid, body)
    if err is not None:
        return err
    try:
        group_no = int(body.get("group_no"))
    except (TypeError, ValueError):
        return {"status": "invalid", "message": "請選擇要搬到哪一組"}
    status, moved = db.move_golf_player(ev["id"], row["id"], group_no)
    if status == "no_group":
        return {"status": "no_group", "message": f"找不到第 {group_no} 組"}
    if status == "full":
        return {"status": "full", "message": f"第 {group_no} 組已經滿 4 位，請改用對調"}
    if moved is None:
        return {"status": "not_found", "message": "找不到這位球友的分組資料"}
    if moved["line_user_id"]:
        _push_receipt(moved["line_user_id"],
                      f"🔀 分組調整通知【{ev['title']}】\n"
                      f"{moved['player_name']} 已改分至第 {moved['group_no']} 組（第 {moved['slot']} 位）。")
    return {"status": "ok", "name": moved["player_name"],
            "group_no": moved["group_no"], "slot": moved["slot"]}


@app.post("/golf/groups/delete")
async def golf_groups_delete(request: Request):
    """把某一位從分組表移除（報名資料不動），同組後面的順位往前補，並通知本人。"""
    admin_uid = request.headers.get("X-Line-UserId", "")
    body = await request.json()
    ev, row, err = _golf_row_target(admin_uid, body)
    if err is not None:
        return err
    db.delete_golf_player(ev["id"], row["id"])
    if row["line_user_id"]:
        _push_receipt(row["line_user_id"],
                      f"🔀 分組調整通知【{ev['title']}】\n"
                      f"{row['player_name']} 已從分組表移除，如有疑問請洽主委。")
    return {"status": "ok", "name": row["player_name"]}


# ── 年會桌次安排 ─────────────────────────────────────────────────────────────

def _attendee_pool(event_id: int) -> list[dict]:
    """報名者（含執秘代報的來賓），照社別排在一起。"""
    people = [
        {"uid": r["line_user_id"],
         "name": (r["full_name"] + (f"（{r['nickname']}）" if r.get("nickname") else "")) or "社友",
         "club": r.get("club_name", "")}
        for r in db.get_event_registrants(event_id)
    ]
    people.sort(key=lambda p: (p["club"], p["name"]))
    people += [{"uid": "", "name": f"{g['name']}（來賓）", "club": ""}
               for g in db.get_event_guests(event_id)]
    return people


@app.get("/admin/seating")
async def admin_seating(request: Request, event: int | None = None):
    """桌次表（年會主委）。"""
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "message": "無桌次安排權限", "tables": []}
    ev = _lookup_event(admin_uid, event) if event else _current_event(admin_uid)
    if ev is None:
        return {"status": "no_event", "message": "找不到對應活動", "tables": []}
    rows = db.list_event_seating(ev["id"])
    tables: dict[int, list] = {}
    for r in rows:
        tables.setdefault(r["table_no"], []).append(
            {"id": r["id"], "seat_no": r["seat_no"], "name": r["name"],
             "uid": r["line_user_id"], "club": r["club_name"]})
    return {
        "status": "ok", "event_id": ev["id"], "event_title": ev["title"],
        "tables": [{"table_no": t, "seats": s} for t, s in sorted(tables.items())],
        "people": [{"id": r["id"], "name": r["name"], "table_no": r["table_no"]} for r in rows],
    }


@app.post("/admin/seating/auto")
async def admin_seating_auto(request: Request):
    """依報名名單排桌（預設 10 人一桌，同社盡量坐一起）。會蓋掉現有桌次。"""
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "message": "無桌次安排權限"}
    body = await request.json()
    ev_id = body.get("event_id")
    ev = _lookup_event(admin_uid, int(ev_id)) if ev_id else _current_event(admin_uid)
    if ev is None:
        return {"status": "no_event", "message": "找不到對應活動"}
    per_table = max(2, min(int(body.get("per_table") or 10), 20))
    people = _attendee_pool(ev["id"])
    if not people:
        return {"status": "empty", "message": "此活動還沒有人報名，無法排桌"}
    count = db.replace_event_seating(ev["id"], people, per_table)
    return {"status": "ok", "seated": count,
            "tables": (count + per_table - 1) // per_table, "per_table": per_table}


@app.post("/admin/seating/swap")
async def admin_seating_swap(request: Request):
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "message": "無桌次安排權限"}
    body = await request.json()
    ev_id = body.get("event_id")
    ev = _lookup_event(admin_uid, int(ev_id)) if ev_id else _current_event(admin_uid)
    if ev is None:
        return {"status": "no_event", "message": "找不到對應活動"}
    try:
        id_a, id_b = int(body.get("a")), int(body.get("b"))
    except (TypeError, ValueError):
        return {"status": "invalid", "message": "請選擇兩位要對調的與會者"}
    if id_a == id_b:
        return {"status": "invalid", "message": "請選擇兩位不同的與會者"}
    swapped = db.swap_event_seats(ev["id"], id_a, id_b)
    if swapped is None:
        return {"status": "not_found", "message": "找不到這兩位的座位資料"}
    a, b = swapped
    return {"status": "ok",
            "a": {"name": a["name"], "table_no": a["table_no"]},
            "b": {"name": b["name"], "table_no": b["table_no"]}}


@app.post("/admin/seating/publish")
async def admin_seating_publish(request: Request):
    """公布桌次：逐一通知有 LINE 身分的與會者自己坐第幾桌。"""
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "message": "無桌次安排權限"}
    body = await request.json()
    ev_id = body.get("event_id")
    ev = _lookup_event(admin_uid, int(ev_id)) if ev_id else _current_event(admin_uid)
    if ev is None:
        return {"status": "no_event", "message": "找不到對應活動"}
    seats = [s for s in db.list_event_seating(ev["id"]) if s["line_user_id"]]
    if not seats:
        return {"status": "empty", "message": "尚未排桌，或與會者都沒有 LINE 身分"}
    sent = 0
    for s in seats:
        try:
            line_api.push_text(
                s["line_user_id"],
                f"🪑 桌次通知【{ev['title']}】\n{ev['date']}（{ev['weekday']}）　{ev['location']}\n\n"
                f"您的座位：第 {s['table_no']} 桌　第 {s['seat_no']} 位")
            sent += 1
        except Exception:
            logger.exception("seating push failed for %s", s["line_user_id"])
    return {"status": "ok", "notified": sent}


# ── 年會摸彩 ─────────────────────────────────────────────────────────────────

@app.get("/admin/raffle")
async def admin_raffle(request: Request, event: int | None = None):
    """獎項、已中獎名單與目前可抽人數（年會主委）。"""
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "message": "無摸彩權限", "prizes": []}
    ev = _lookup_event(admin_uid, event) if event else _current_event(admin_uid)
    if ev is None:
        return {"status": "no_event", "message": "找不到對應活動", "prizes": []}
    winners = db.list_winners(ev["id"])
    by_prize: dict[int, list] = {}
    for w in winners:
        by_prize.setdefault(w["prize_id"], []).append({"name": w["name"], "club": w["club_name"]})
    return {
        "status": "ok", "event_id": ev["id"], "event_title": ev["title"],
        "candidates": len(db.raffle_candidates(ev["id"])),
        "prizes": [{**p, "winners": by_prize.get(p["id"], [])} for p in db.list_prizes(ev["id"])],
    }


@app.post("/admin/raffle/prizes")
async def admin_raffle_add_prize(request: Request):
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "message": "無摸彩權限"}
    body = await request.json()
    name = str(body.get("name", "")).strip()
    if not name:
        return {"status": "invalid", "message": "請填寫獎項名稱"}
    try:
        qty = max(1, min(int(body.get("qty") or 1), 100))
    except (TypeError, ValueError):
        return {"status": "invalid", "message": "名額請填數字"}
    ev_id = body.get("event_id")
    ev = _lookup_event(admin_uid, int(ev_id)) if ev_id else _current_event(admin_uid)
    if ev is None:
        return {"status": "no_event", "message": "找不到對應活動"}
    return {"status": "ok", "prize": db.add_prize(ev["id"], name, qty)}


@app.delete("/admin/raffle/prizes/{prize_id}")
async def admin_raffle_delete_prize(prize_id: int, request: Request):
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "message": "無摸彩權限"}
    db.delete_prize(prize_id)
    return {"status": "ok"}


@app.post("/admin/raffle/draw")
async def admin_raffle_draw(request: Request):
    """從「已報到」且尚未中獎的人裡抽出得獎者，並推播通知本人。"""
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "message": "無摸彩權限"}
    body = await request.json()
    prize = db.get_prize(int(body.get("prize_id") or 0))
    if prize is None:
        return {"status": "not_found", "message": "找不到這個獎項"}
    ev = db.get_event(prize["event_id"])
    if ev is None:
        return {"status": "no_event", "message": "找不到對應活動"}
    if body.get("redraw"):
        db.clear_prize_winners(prize["id"])
    elif [w for w in db.list_winners(ev["id"]) if w["prize_id"] == prize["id"]]:
        return {"status": "already", "message": "此獎項已抽出，如要重抽請先清除"}

    pool = db.raffle_candidates(ev["id"])
    if not pool:
        return {"status": "empty", "message": "目前沒有已報到且尚未中獎的與會者"}
    picked = random.sample(pool, min(prize["qty"], len(pool)))
    winners = [{"uid": p["line_user_id"],
                "name": (p["full_name"] + (f"（{p['nickname']}）" if p["nickname"] else "")) or "與會者",
                "club": p["club_name"]}
               for p in picked]
    db.add_winners(ev["id"], prize["id"], winners)
    for w in winners:
        if w["uid"]:
            _push_receipt(w["uid"], f"🎉 恭喜中獎！\n\n【{ev['title']}】{prize['name']}\n"
                                    f"請至現場服務台領獎。")
    return {"status": "ok", "prize": prize["name"], "drawn": len(winners),
            "short": len(winners) < prize["qty"],
            "winners": [{"name": w["name"], "club": w["club"]} for w in winners]}


# ── RYE：面試安排 + 同意書審核 ───────────────────────────────────────────────

@app.get("/admin/rye")
async def admin_rye(request: Request, event: int | None = None):
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "message": "無 RYE 管理權限", "applicants": []}
    ev = _lookup_event(admin_uid, event) if event else _current_event(admin_uid)
    if ev is None:
        return {"status": "no_event", "message": "找不到對應活動", "applicants": []}
    return {"status": "ok", "event_id": ev["id"], "event_title": ev["title"],
            "start_time": ev.get("start_time") or (ev.get("time") or "").split("-")[0].strip(),
            "applicants": db.list_rye_applicants(ev["id"])}


@app.post("/admin/rye/applicants")
async def admin_rye_add(request: Request):
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "message": "無 RYE 管理權限"}
    body = await request.json()
    name = str(body.get("name", "")).strip()
    if not name:
        return {"status": "invalid", "message": "請填寫學生姓名"}
    ev_id = body.get("event_id")
    ev = _lookup_event(admin_uid, int(ev_id)) if ev_id else _current_event(admin_uid)
    if ev is None:
        return {"status": "no_event", "message": "找不到對應活動"}
    return {"status": "ok", "applicant": db.add_rye_applicant(
        ev["id"], name, str(body.get("club_name", "")).strip(),
        str(body.get("line_user_id", "")).strip())}


@app.post("/admin/rye/applicants/{applicant_id}")
async def admin_rye_update(applicant_id: int, request: Request):
    """改時段 / 面試官 / 同意書連結與審核結果。審核有結果就通知學生。"""
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "message": "無 RYE 管理權限"}
    body = await request.json()
    fields = {k: str(body[k]).strip() for k in
              ("name", "club_name", "line_user_id", "slot_time", "interviewer",
               "consent_url", "consent_note") if k in body}
    if "consent_status" in body:
        status = str(body["consent_status"]).strip()
        if status not in ("none", "pending", "approved", "rejected"):
            return {"status": "invalid", "message": "同意書狀態不正確"}
        fields["consent_status"] = status
    applicant = db.update_rye_applicant(applicant_id, fields)
    if applicant is None:
        return {"status": "not_found", "message": "找不到這位學生"}
    if fields.get("consent_status") in ("approved", "rejected") and applicant["line_user_id"]:
        verdict = "✅ 已通過" if fields["consent_status"] == "approved" else "❌ 需補件"
        note = f"\n主委備註：{applicant['consent_note']}" if applicant["consent_note"] else ""
        _push_receipt(applicant["line_user_id"],
                      f"📄 家長同意書審核結果：{verdict}\n\n{applicant['name']}{note}")
    return {"status": "ok", "applicant": applicant}


@app.delete("/admin/rye/applicants/{applicant_id}")
async def admin_rye_delete(applicant_id: int, request: Request):
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "message": "無 RYE 管理權限"}
    db.delete_rye_applicant(applicant_id)
    return {"status": "ok"}


@app.post("/admin/rye/schedule")
async def admin_rye_schedule(request: Request):
    """自動排面試時段：從活動開始時間起，每人間隔 N 分鐘。"""
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "message": "無 RYE 管理權限"}
    body = await request.json()
    ev_id = body.get("event_id")
    ev = _lookup_event(admin_uid, int(ev_id)) if ev_id else _current_event(admin_uid)
    if ev is None:
        return {"status": "no_event", "message": "找不到對應活動"}
    applicants = db.list_rye_applicants(ev["id"])
    if not applicants:
        return {"status": "empty", "message": "尚未建立學生名單"}
    try:
        every = max(5, min(int(body.get("minutes") or 20), 120))
    except (TypeError, ValueError):
        every = 20
    start = str(body.get("start") or ev.get("start_time") or
                (ev.get("time") or "").split("-")[0].strip() or "10:00")
    try:
        h, m = start.split(":")[:2]
        cur = int(h) * 60 + int(m)
    except ValueError:
        cur = 600
    slots = []
    for a in applicants:
        slots.append((a["id"], f"{cur // 60 % 24:02d}:{cur % 60:02d}"))
        cur += every
    db.set_rye_slots(ev["id"], slots)
    return {"status": "ok", "scheduled": len(slots), "start": slots[0][1], "minutes": every}


@app.post("/admin/rye/notify")
async def admin_rye_notify(request: Request):
    """把面試時段通知有 LINE 身分的學生。"""
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "message": "無 RYE 管理權限"}
    body = await request.json()
    ev_id = body.get("event_id")
    ev = _lookup_event(admin_uid, int(ev_id)) if ev_id else _current_event(admin_uid)
    if ev is None:
        return {"status": "no_event", "message": "找不到對應活動"}
    targets = [a for a in db.list_rye_applicants(ev["id"]) if a["line_user_id"] and a["slot_time"]]
    if not targets:
        return {"status": "empty", "message": "沒有可通知的對象（需有 LINE 身分且已排定時段）"}
    sent = 0
    for a in targets:
        who = f"\n面試官：{a['interviewer']}" if a["interviewer"] else ""
        try:
            line_api.push_text(
                a["line_user_id"],
                f"📋 面試時段通知【{ev['title']}】\n{ev['date']}（{ev['weekday']}）　{ev['location']}\n\n"
                f"{a['name']} 同學，您的面試時間為 {a['slot_time']}{who}\n請提前 10 分鐘報到。")
            sent += 1
        except Exception:
            logger.exception("rye notify failed for %s", a["line_user_id"])
    return {"status": "ok", "notified": sent}


# ── 理監事專區：名單 + 議案表決 ───────────────────────────────────────────────

def _motion_tally(motion_id: int, club: str) -> dict:
    votes = db.list_board_votes(motion_id)
    members = db.list_board_members(club)
    voted = {v["line_user_id"]: v for v in votes}
    name = lambda r: (r["full_name"] + (f"（{r['nickname']}）" if r["nickname"] else "")) or "理監事"
    return {
        "yes":     [name(v) for v in votes if v["vote"] == "yes"],
        "no":      [name(v) for v in votes if v["vote"] == "no"],
        "abstain": [name(v) for v in votes if v["vote"] == "abstain"],
        "pending": [name(m) for m in members if m["line_user_id"] not in voted],
    }


@app.get("/admin/board")
async def admin_board(request: Request, club: str = ""):
    """理監事名單與議案表決狀況（社長 / 秘書）。"""
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "message": "無理監事專區權限", "motions": []}
    club = club or db.get_user_club(admin_uid)
    motions = []
    for m in db.list_board_motions(club):
        motions.append({"id": m["id"], "title": m["title"], "detail": m["detail"],
                        "status": m["status"], **_motion_tally(m["id"], club)})
    return {"status": "ok", "club": club,
            "members": [{"uid": m["line_user_id"],
                         "name": (m["full_name"] + (f"（{m['nickname']}）" if m["nickname"] else ""))
                                 or "（未填個人資料）"}
                        for m in db.list_board_members(club)],
            "motions": motions}


@app.post("/admin/board/members")
async def admin_board_members(request: Request):
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "message": "無理監事專區權限"}
    body = await request.json()
    club = str(body.get("club", "")).strip() or db.get_user_club(admin_uid)
    uids = [str(u) for u in body.get("uids", []) if u]
    return {"status": "ok", "club": club, "members": db.set_board_members(club, uids)}


@app.post("/admin/board/motions")
async def admin_board_add_motion(request: Request):
    """建立議案並推播給理監事，讓他們直接在 LINE 上表決。"""
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "message": "無理監事專區權限"}
    body = await request.json()
    title = str(body.get("title", "")).strip()
    if not title:
        return {"status": "invalid", "message": "請填寫議案名稱"}
    club = str(body.get("club", "")).strip() or db.get_user_club(admin_uid)
    members = db.list_board_members(club)
    if not members:
        return {"status": "no_members", "message": "請先設定理監事名單"}
    motion = db.add_board_motion(club, title, str(body.get("detail", "")).strip(), admin_uid)
    try:
        line_api.multicast_flex([m["line_user_id"] for m in members],
                                f"🗳️ 議案表決：{title}",
                                _build_motion_bubble(motion, club))
    except Exception:
        logger.exception("motion multicast failed for %s", motion["id"])
        return {"status": "push_failed", "message": "議案已建立，但 LINE 推播失敗。"}
    return {"status": "ok", "motion_id": motion["id"], "notified": len(members)}


@app.post("/admin/board/motions/{motion_id}/close")
async def admin_board_close_motion(motion_id: int, request: Request):
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "message": "無理監事專區權限"}
    body = await request.json()
    status = str(body.get("result", "")).strip()
    if status not in ("passed", "rejected"):
        return {"status": "invalid", "message": "結果只能是通過或否決"}
    motion = db.get_board_motion(motion_id)
    if motion is None:
        return {"status": "not_found", "message": "找不到這個議案"}
    db.close_board_motion(motion_id, status)
    verdict = "✅ 通過" if status == "passed" else "❌ 否決"
    tally = _motion_tally(motion_id, motion["club_name"])
    for m in db.list_board_members(motion["club_name"]):
        _push_receipt(m["line_user_id"],
                      f"🗳️ 議案結果：{verdict}\n\n{motion['title']}\n"
                      f"同意 {len(tally['yes'])}　反對 {len(tally['no'])}　棄權 {len(tally['abstain'])}")
    return {"status": "ok", "result": status}


# ── 報名專區：活動意願調查 ────────────────────────────────────────────────────

def _survey_name(r: dict) -> str:
    full = r.get("full_name") or ""
    if not full:
        return "（未填個人資料）"
    return full + (f"（{r['nickname']}）" if r.get("nickname") else "")


def _survey_payload(event_id: int, club: str) -> dict:
    rows = db.get_survey(event_id, club)
    targets = [
        {
            "uid": r["line_user_id"],
            "name": _survey_name(r),
            "status": r["status"],
            "reminded": bool(r["reminded"]),
        }
        for r in rows
    ]
    return {
        "status": "ok",
        "event_id": event_id,
        "sent_count": len(targets),
        "attending": sum(1 for t in targets if t["status"] == "attending"),
        "leave": sum(1 for t in targets if t["status"] == "leave"),
        "pending": sum(1 for t in targets if t["status"] == "pending"),
        "targets": targets,
    }


@app.get("/admin/survey")
async def admin_survey_get(request: Request, event: int, club: str = ""):
    """Current 意願調查 state for an event (admin only)."""
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "targets": []}
    club = club or db.get_user_club(admin_uid)
    return _survey_payload(int(event), club)


@app.post("/admin/survey/send")
async def admin_survey_send(request: Request):
    """Push the 參加意願調查表 to the selected members (admin only)."""
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "message": "無發送權限"}
    body = await request.json()
    ev_id = int(body.get("event_id") or 0)
    uids = [str(u) for u in body.get("uids", []) if u]
    if not (ev_id and uids):
        return {"status": "invalid", "message": "缺少活動或發送對象"}
    ev = db.get_event(ev_id)
    if ev is None:
        return {"status": "no_event", "message": "找不到該活動"}
    added = db.add_survey_targets(ev_id, uids)
    try:
        line_api.multicast_flex(uids, f"📋 {ev['title']} 出席調查", _build_survey_bubble(ev))
    except Exception:
        logger.exception("survey multicast failed for event %s", ev_id)
        return {"status": "push_failed", "message": "名單已建立，但 LINE 推播失敗，請稍後重送。"}
    club = str(body.get("club", "")).strip() or db.get_user_club(admin_uid)
    return {"sent": len(uids), "added": added, **_survey_payload(ev_id, club)}


@app.post("/admin/survey/remind")
async def admin_survey_remind(request: Request):
    """Second push to everyone who has not answered yet (admin only)."""
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "message": "無跟催權限"}
    body = await request.json()
    ev_id = int(body.get("event_id") or 0)
    ev = db.get_event(ev_id) if ev_id else None
    if ev is None:
        return {"status": "no_event", "message": "找不到該活動"}
    club = str(body.get("club", "")).strip() or db.get_user_club(admin_uid)
    pending = db.get_survey_pending(ev_id, club)
    uids = [r["line_user_id"] for r in pending]
    if not uids:
        return {**_survey_payload(ev_id, club), "status": "none_pending"}
    try:
        line_api.multicast_flex(uids, f"🔔 {ev['title']} 出席調查提醒",
                                _build_survey_bubble(ev, reminder=True))
    except Exception:
        logger.exception("survey reminder failed for event %s", ev_id)
        return {"status": "push_failed", "message": "跟催推播失敗，請稍後再試。"}
    db.mark_survey_reminded(ev_id, uids)
    return {"reminded": len(uids), **_survey_payload(ev_id, club)}


@app.post("/admin/survey/report")
async def admin_survey_report(request: Request):
    """Send the not-yet-answered list to the club's 社長 / 秘書 (admin only)."""
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "message": "無呈報權限"}
    body = await request.json()
    ev_id = int(body.get("event_id") or 0)
    ev = db.get_event(ev_id) if ev_id else None
    if ev is None:
        return {"status": "no_event", "message": "找不到該活動"}
    club = str(body.get("club", "")).strip() or db.get_user_club(admin_uid)
    pending = db.get_survey_pending(ev_id, club)
    names = [_survey_name(r) for r in pending]
    if not names:
        return {"status": "none_pending", "names": []}
    presidents = db.get_club_admins(club)
    # No 社長 on file → send it back to the 執秘 who asked, so nothing is lost.
    recipients = presidents or [admin_uid]
    text = (f"📄 【未回覆名單呈報】\n\n{ev['title']}\n{ev['date']}（{ev['weekday']}）\n\n"
            f"尚未回覆出席意願：{len(names)} 位\n" + "\n".join(f"• {n}" for n in names))
    sent = 0
    for uid in set(recipients):
        try:
            line_api.push_text(uid, text)
            sent += 1
        except Exception:
            logger.exception("survey report push failed for %s", uid)
    return {"status": "ok", "names": names, "recipients": sent, "to_self": not presidents}


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
    bank_digits = str(body.get("bank_digits", "")).strip()
    event_id = body.get("event_id")

    ev = _lookup_event(admin_uid, int(event_id)) if event_id else _current_event(admin_uid)
    if ev is None:
        return {"status": "no_event", "message": "找不到對應活動"}

    # 來賓的差點與方案只能在建立當下問——他沒有報名紀錄可以事後補填。
    # 方案要對照本場賽事的設定，所以這段要在查到 ev 之後。
    guests = []
    for g in body.get("guests", []) or []:
        item = g if isinstance(g, dict) else {"name": g}
        nm = str(item.get("name", "")).strip()
        if not nm:
            continue
        hcp, err = _parse_handicap(item.get("handicap"))
        if err:
            return {"status": "invalid", "message": f"來賓「{nm}」的{err}"}
        plan, err = _parse_course_plan(ev, item.get("course_plan"))
        if err:
            return {"status": "invalid", "message": f"來賓「{nm}」的{err}"}
        guests.append({"name": nm, "handicap": hcp, "course_plan": plan})

    if not uids and not guests:
        return {"status": "empty", "message": "未選擇任何社友或來賓"}

    # 執秘不見得知道每個人的差點，所以這裡是選填：留白的人不會進報名差點榜，
    # 之後他自己在首頁補填即可。
    handicaps: dict[str, float] = {}
    for uid_key, raw in (body.get("handicaps") or {}).items():
        value, err = _parse_handicap(raw)
        if err:
            return {"status": "invalid", "message": f"差點格式錯誤：{err}"}
        if value is not None:
            handicaps[str(uid_key)] = value

    course_plans: dict[str, str] = {}
    for uid_key, raw in (body.get("course_plans") or {}).items():
        code, err = _parse_course_plan(ev, raw)
        if err:
            return {"status": "invalid", "message": err}
        if code is not None:
            course_plans[str(uid_key)] = code

    result = db.bulk_register(uids, ev["id"], bank_digits, admin_uid, handicaps, course_plans)
    guest_count = db.add_event_guests(ev["id"], guests, admin_uid, bank_digits)

    # Notify each newly-registered member in their own chat.
    for uid in uids:
        try:
            line_api.push_text(uid, f"📋 執秘已代您報名【{ev['title']}】，如有疑問請洽社務行政。")
        except Exception:
            logger.exception("bulk-register push failed for %s", uid)

    _push_receipt(admin_uid, f"✅ 已完成 {result['new'] + guest_count} 筆【{ev['title']}】報名"
                             + (f"（另有 {result['dup']} 人先前已報名）" if result["dup"] else ""))

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
