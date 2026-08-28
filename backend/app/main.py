import hashlib
import hmac
import base64
import json
import logging
import random
import re
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
from .config import (APP_BASE_URL, CALENDAR_BASE_URL, FINANCE_BASE_URL, GOLF_BASE_URL,
                     LINE_CHANNEL_SECRET, LIFF_URL)

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
    db.ensure_club_dues_settings_table()
    db.ensure_club_dues_tier_tables()
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
    db.ensure_club_opening_balance_table()
    db.ensure_member_business_table()
    # 地區要在活動之前建好：活動與社都掛在地區底下，順序反了就會有一批資料指向
    # 一個還不存在的地區。
    db.ensure_districts_table()
    for d in _SEED_DISTRICTS:
        db.seed_district(**d)
    db.ensure_clubs_table()
    db.ensure_events_table()
    db.ensure_event_pdf_table()
    # First run: migrate the previously-hardcoded schedule into the editable table.
    if db.events_count() == 0:
        db.seed_events(list(_EVENT_SCHEDULE) + _club_events("本社"))
    db.ensure_personal_information_columns()
    # 名冊裡有、但還沒登記地區的社，補成預設地區。既有的社都是 3523 年代建的；
    # 漏掉任何一個，那個社的社友會落到「沒有地區」而什麼活動都看不到。
    added = db.backfill_clubs()
    if added:
        logger.info("clubs backfilled into %s: %d", db.DEFAULT_DISTRICT, added)
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


# ── 地區 ──────────────────────────────────────────────────────────────────────
# 開機時確保這些地區存在（已存在就不動，管理員後來改的設定不會被蓋掉）。
# notices_api 是那個地區公文網站的 WordPress REST 根目錄，留空 = 不自動同步公文，
# 該地區的活動就純靠人工在行事曆建立。3481 的來源網站尚未確認，先留空。
_SEED_DISTRICTS = [
    {"code": "3523", "name": "國際扶輪 3523 地區", "short_name": "3523 地區",
     "website": "https://www.rotary3523.org.tw",
     "notices_api": "https://ri3523.org/wp-json/wp/v2",
     "contact_email": "office@rotary3523.org.tw"},
    {"code": "3481", "name": "國際扶輪 3481 地區", "short_name": "3481 地區",
     "website": "", "notices_api": "", "contact_email": ""},
]


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


def _district_of(user_id: str) -> dict:
    """使用者所屬地區的設定（名稱、網站、公文來源）。

    地區被刪掉或資料還沒建時退回一組以代碼組出來的預設值，不要讓標題變成空白或
    整支端點爆掉 —— 地區資料出問題時，社友該看到的是活動，不是錯誤畫面。"""
    code = db.get_user_district(user_id) if user_id else db.DEFAULT_DISTRICT
    return db.get_district(code) or {
        "code": code, "name": f"國際扶輪 {code} 地區", "short_name": f"{code} 地區",
        "website": "", "notices_api": "", "contact_email": "",
    }


def _events_for_scope(scope: str, club_name: str = "",
                      district: str | None = "") -> list[dict]:
    """Events now live in an editable DB table (seeded from the lists above); the
    執秘 maintains them from the admin panel. All lookups go through here / db.

    district 一律要帶：這是 3481 的社友看不到 3523 活動的唯一機制。三種值意義不同
    —— 代碼是那一區、None 是不分地區（只有跨地區管理員拿得到）、空字串是「沒指定」
    而落回預設地區。少了 None 這一種，跨地區管理員的「全部」會被當成沒指定，
    看到的仍然只有預設地區。"""
    if district is None:
        return db.list_events(scope, club_name, "")
    return db.list_events(scope, club_name, district or db.DEFAULT_DISTRICT)


def _current_event(user_id: str) -> dict | None:
    """Closest upcoming event within the user's active scope (else most recent past)."""
    scope = db.get_user_scope(user_id)
    evs = _events_for_scope(scope, db.get_user_club(user_id), _visible_district(user_id))
    if not evs:
        return None
    today = date.today().isoformat()
    upcoming = sorted([e for e in evs if e["date"] >= today], key=lambda e: e["date"])
    if upcoming:
        return upcoming[0]
    return sorted(evs, key=lambda e: e["date"], reverse=True)[0]


def _lookup_event(user_id: str, ev_id: int) -> dict | None:
    """Find an event by id (ids are unique across district + club schedules).

    別的地區的活動一律當作不存在。報名、報到、繳費回報、後台作業全都經過這裡
    查活動，所以地區的隔離只要守住這一關 —— 每個呼叫點各自檢查一次，遲早會有
    一個漏掉，而那一個就是別區的人報進本區名單的入口。

    跨地區管理員是唯一的例外，見 db.is_super_admin。"""
    ev = db.get_event(ev_id)
    if ev is None:
        return None
    if user_id and db.is_super_admin(user_id):
        return ev
    district = db.get_user_district(user_id) if user_id else db.DEFAULT_DISTRICT
    return ev if (ev.get("district") or db.DEFAULT_DISTRICT) == district else None


def _visible_district(user_id: str) -> str | None:
    """查活動時要套的地區條件；跨地區管理員回 None = 不過濾（見 _events_for_scope）。"""
    return None if db.is_super_admin(user_id) else db.get_user_district(user_id)


def _target_club(admin_uid: str, requested: str = "") -> str:
    """社務類畫面（財務看板、名冊、社費）要操作哪一個社。

    一般管理員永遠只有自己的社，指定別的社會被忽略而不是報錯 —— 那是呼叫端帶
    參數的問題，不是使用者做錯什麼。跨地區管理員可以指定任何一個社，這正是
    「看得到全部社」的意思。"""
    requested = (requested or "").strip()
    if requested and db.is_super_admin(admin_uid):
        return requested
    return db.get_user_club(admin_uid)


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


def _build_award_result(rows: list[dict], district_name: str = "") -> dict:
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
                "contents": [{"type": "text", "text": district_name or "國際扶輪", "size": "xxs", "color": "#ffffff", "weight": "bold"}],
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
        events = _events_for_scope(scope, db.get_user_club(user_id),
                                   _visible_district(user_id))
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
            # 信箱跟著問的人所屬地區走；該地區還沒填就不要編一個出來
            email = _district_of(user_id).get("contact_email") or ""
            line_api.reply_text(reply_token,
                                "🎧 秘書處聯絡方式\n\n"
                                + (f"信箱：{email}\n" if email
                                   else "（貴地區尚未設定秘書處信箱）\n")
                                + "系統問題請附上活動名稱與畫面截圖，我們會盡快回覆。")
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
        d = _district_of(user_id)
        line_api.reply_flex(reply_token, f"📅 {d['short_name']}年度行事曆",
                            _build_event_list_carousel(
                                db.list_events("district", district=d["code"])))

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


def _reply_finance_link(reply_token: str, user_id: str) -> None:
    """Reply with a link to the treasury board (finance.html) — 執秘／財務 only. A
    member asking about money wants their own bill, so they get 我的社費 instead."""
    if not db.is_admin(user_id):
        items = [{"type": "action", "action": {
            "type": "uri", "label": "🧾 我的社費", "uri": f"{LIFF_URL}?tab=profile&action=my_dues"}}]
        line_api.reply_text_with_quick_reply(
            reply_token, "🧾 社費查詢\n點下面可以看自己這個月的帳單並回報繳款。", items)
        return
    text = ("💰 社費財務看板\n這個月該收多少、收了多少、還有誰沒繳，扣掉社務支出剩多少，"
            "都在同一頁。表格較寬，建議用電腦瀏覽器打開（會請您用 LINE 登入）。\n\n"
            + FINANCE_BASE_URL)
    items = [{"type": "action", "action": {
        "type": "uri", "label": "💰 開啟財務看板", "uri": FINANCE_BASE_URL}}]
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
    if stripped in ("財務", "財務看板", "社費", "社費收繳", "收繳", "對帳"):
        _reply_finance_link(reply_token, user_id)
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
            line_api.reply_flex(reply_token, f"🏆 「{kw}」得獎查詢",
                                _build_award_result(rows, _district_of(user_id)["name"]))

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
# 沒設定過費率的社沿用這兩個數字；設定後以 club_dues_settings 的生效段落為準。
DUES_BASE = 2100      # 常年月費
DUES_DISTRICT = 125   # 地區分攤金


_MAX_TIER_ITEMS = 5


def _clean_fixed_items(raw) -> list[dict]:
    """會籍類別的固定加項 [{name, amount}]，寫入與讀出都經過這裡。

    沒有名稱的整列丟掉，規則跟 _clean_dues_items 一樣、理由也一樣：社友的帳單上
    會出現這個名稱，沒有名稱的固定收費是打到一半放棄的東西，不是費用。
    金額只收非負整數 —— 負的固定項是折扣，會在沒有任何紀錄的情況下把應收砍掉；
    要折扣就給那個類別一個比較低的常年月費。"""
    items = []
    for c in (raw if isinstance(raw, list) else []):
        if not isinstance(c, dict):
            continue
        name = str(c.get("name", "")).strip()[:20]
        if not name:
            continue
        try:
            amount = int(c.get("amount", 0) or 0)
        except (TypeError, ValueError):
            continue
        items.append({"name": name, "amount": max(0, amount)})
        if len(items) >= _MAX_TIER_ITEMS:
            break
    return items


def _items_total(items) -> int:
    return sum(int(i.get("amount", 0) or 0) for i in (items or []))


def _bad_amount(value) -> bool:
    """負數或不是數字。_clean_fixed_items 會默默把這種列修掉或丟掉，但使用者
    自己打進來的那一筆要當面說，不能無聲無息變成 0。"""
    try:
        return int(value or 0) < 0
    except (TypeError, ValueError):
        return True


def _tier_book(club: str) -> dict:
    """整社的會籍類別費率與指派段落，兩個查詢一次撈完。

    看板會把過去每一個月都重算一遍（見 _carry_forward），每個月各查一次的話，看
    八個月就是十六趟往返。提到迴圈外面撈一次，整趟只多兩個查詢。"""
    if not club:
        return {"rates": [], "members": []}
    return {"rates": db.list_tier_rates(club), "members": db.list_member_tiers(club)}


def _rates_for_month(club: str, month: str, book: dict | None = None) -> dict:
    """某個月的費率全貌：全社預設、各類別的常年月費、誰屬於哪一類。

    「哪一段生效」跟費率設定同一條規則 —— 生效月份 <= 該月之中最新的那一段。兩份
    清單都已經按月份由新到舊排好，所以第一個對上的就是答案。"""
    s = db.get_dues_settings(club, month) if (club and month) else None
    rb = {
        "district": int(s["district"]) if s else DUES_DISTRICT,
        "default_base": int(s["base"]) if s else DUES_BASE,
        "tier_base": {}, "tier_label": {}, "tier_from": {}, "tier_items": {},
        "tier_of": {},
    }
    if book is None:
        book = _tier_book(club)
    for r in book["rates"]:
        tier = r["tier"]
        if r["effective_month"] <= month and tier not in rb["tier_base"]:
            rb["tier_base"][tier] = int(r["base"])
            rb["tier_label"][tier] = r.get("label") or ""
            rb["tier_from"][tier] = r["effective_month"]
            # 資料庫裡的 JSON 先洗過再放行：壞掉的一列不該一路流到 sum() 或社友的帳單上
            rb["tier_items"][tier] = _clean_fixed_items(r.get("items"))
    for m in book["members"]:
        uid = m["line_user_id"]
        if m["effective_month"] <= month and uid not in rb["tier_of"]:
            rb["tier_of"][uid] = m["tier"] or ""
    return rb


def _member_rates(rb: dict, uid: str = "") -> tuple[int, int, list]:
    """這位社友該月的 (常年月費, 地區分攤金, 類別固定加項)。

    回退鏈：他的類別 → 那個類別的費率 → 類別在這個月還沒有費率段落就用全社預設
    （不是 0 —— 還沒設定不等於不用繳）。分攤金永遠是全社那一個，不看類別。

    加項的回退跟 base 刻意不對稱：沒對上任何段落就是空的，不會落回全社預設 ——
    全社預設根本沒有加項這回事，全社都要收的東西是加在常年月費裡的。"""
    tier = rb["tier_of"].get(uid, "") if uid else ""
    base = rb["tier_base"].get(tier, rb["default_base"]) if tier else rb["default_base"]
    extras = rb["tier_items"].get(tier, []) if tier else []
    return base, rb["district"], extras


def _dues_rates(club: str, month: str, uid: str = "",
                rb: dict | None = None) -> tuple[int, int, list]:
    """(常年月費, 地區分攤金, 類別固定加項) in force for that member that month.

    uid 留空 = 全社預設那一組，給沒有指定社友的呼叫端（費率設定畫面）用。"""
    if rb is None:
        rb = _rates_for_month(club, month)
    return _member_rates(rb, uid)


def _dues_total(meal: int, iou: int, customs: list, base: int, district: int,
                extras=()) -> int:
    return (base + district + _items_total(extras) + (meal or 0) + (iou or 0)
            + sum(int(c.get("amount", 0) or 0) for c in customs))


def _dues_payload(row: dict | None, base: int = DUES_BASE, district: int = DUES_DISTRICT,
                  extras=()) -> dict:
    meal = row["meal"] if row else 0
    iou = row["iou"] if row else 0
    customs = row["customs"] if row and isinstance(row.get("customs"), list) else []
    return {
        "meal": meal, "iou": iou, "customs": customs,
        "is_paid": bool(row["is_paid"]) if row else False,
        "confirmed": bool(row.get("confirmed")) if row else False,
        # base 只是常年月費，加項不併進去：併了之後看板的類別提示、名冊、月費設定
        # 與社友帳單上的「常年月費」都會變成一個誰也沒設定過的數字，而且那一行
        # 具名的加項就再也印不出來。
        "base": base, "district": district,
        "extras": list(extras or []), "extras_total": _items_total(extras),
        "total": _dues_total(meal, iou, customs, base, district, extras),
        # 固定費用（含類別加項）不算「有帳單」：這個旗標的意思是執秘登過變動費用
        # 沒有，批次記帳的名單就是照它挑人的。
        "has_bill": bool(row and (meal or iou or customs)),
    }


@app.get("/dues/member")
async def dues_member(request: Request, club: str = "", month: str = "", uid: str = ""):
    """Secretary loads one member's dues for a month (admin only)."""
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden"}
    # club 是呼叫端給的，之前照單全收 —— 任何一位管理員都能讀別的社的帳單。
    # 現在只有跨地區管理員指定得動，其餘人一律自己的社。
    club = _target_club(admin_uid, club)
    rb = _rates_for_month(club, month)
    # tier 給執秘看：他在後台切換社友時，要看得出這個人的常年月費為什麼跟上一位不同
    return {"status": "ok", "tier": rb["tier_of"].get(uid, ""),
            **_member_month(club, uid, month, rb)}


def _clean_dues_items(body: dict) -> tuple[int, int, list]:
    """例會餐費 / IOU / 臨時項目 out of a save request. Unnamed custom rows are
    dropped — the member sees the name on their bill, so a nameless charge is
    something 執秘 started typing and abandoned, not a fee.

    帶著 event_id 的項目是從地區活動報名帶進來的報名費。那個 id 是「這場已經記過
    帳」的唯一憑據，所以原封不動留著 —— 洗掉的話，同一場下個月又會被列出來等著
    再記一次。"""
    customs = []
    for c in body.get("customs", []):
        name = str(c.get("name", ""))
        if not name.strip():
            continue
        item = {"name": name, "amount": int(c.get("amount", 0) or 0)}
        try:
            event_id = int(c.get("event_id") or 0)
        except (TypeError, ValueError):
            event_id = 0
        if event_id > 0:
            item["event_id"] = event_id
        customs.append(item)
    return int(body.get("meal", 0) or 0), int(body.get("iou", 0) or 0), customs


def _push_dues_bill(uid: str, month: str, total: int) -> None:
    try:
        line_api.push_text(uid, f"💰 {month} 社費帳單已產出，本月應繳 NT${total:,}。可於「個人中心 → 我的社費」查看並回報繳款。")
    except Exception:
        logger.exception("dues bill push failed for %s", uid)


@app.post("/dues/save")
async def dues_save(request: Request):
    """Secretary saves a member's fee items (produces the bill).

    notify=false suppresses the LINE bill notice — 財務看板 edits an existing bill
    in place (fixing a mistyped 餐費), and re-pushing "帳單已產出" for a correction
    trains members to ignore the notice that matters."""
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "message": "無社費記帳權限"}
    body = await request.json()
    club = _target_club(admin_uid, str(body.get("club", "")).strip())
    month = str(body.get("month", "")).strip()
    uid = str(body.get("uid", "")).strip()
    if not (club and month and uid):
        return {"status": "invalid", "message": "缺少社別 / 月份 / 社友"}
    meal, iou, customs = _clean_dues_items(body)
    db.upsert_dues(club, month, uid, meal, iou, customs)
    total = _dues_total(meal, iou, customs, *_dues_rates(club, month, uid))
    notify = bool(body.get("notify", True))     # 預設通知，維持 LIFF 既有行為
    if notify:
        _push_dues_bill(uid, month, total)
    return {"status": "ok", "total": total, "notified": notify}


@app.post("/dues/bulk_save")
async def dues_bulk_save(request: Request):
    """Bill many members the same items at once — 執秘 opening the month for the
    whole club. Same write as /dues/save repeated, so a 30-member club is one
    request instead of 30. Members who already have a bill are skipped unless
    overwrite=true, so a second pass doesn't wipe the ones already itemised."""
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "message": "無社費記帳權限"}
    body = await request.json()
    club = _target_club(admin_uid, str(body.get("club", "")).strip())
    month = str(body.get("month", "")).strip()
    uids = [str(u).strip() for u in body.get("uids", []) if str(u).strip()]
    if not (club and _valid_month(month) and uids):
        return {"status": "invalid", "message": "缺少社別 / 月份 / 社友"}
    meal, iou, customs = _clean_dues_items(body)
    # 報名費是逐人逐場的，批次記帳裡不該出現 event_id：留著的話這一場會被標記成
    # 全社每個人都繳過了，真正報名的人反而再也不會被列出來。
    customs = [{k: v for k, v in c.items() if k != "event_id"} for c in customs]
    # 記上去的變動費用是同一份，但總額不是：固定月費隨各人的會籍類別而異，一個
    # 共用的數字推播出去，B 類的社友收到的會是 A 類的金額。
    items_total = (meal or 0) + (iou or 0) + sum(int(c.get("amount", 0) or 0) for c in customs)
    rb = _rates_for_month(club, month)
    notify = bool(body.get("notify", True))
    overwrite = bool(body.get("overwrite", False))
    existing = {r["line_user_id"] for r in db.list_dues(club, month)
                if r["meal"] or r["iou"] or r["customs"]}
    saved, skipped = [], 0
    for uid in uids:
        if uid in existing and not overwrite:
            skipped += 1
            continue
        db.upsert_dues(club, month, uid, meal, iou, customs)
        saved.append((uid, _dues_total(meal, iou, customs, *_member_rates(rb, uid))))
    if notify:
        for uid, total in saved:
            _push_dues_bill(uid, month, total)
    distinct = {t for _, t in saved}
    varies = len(distinct) > 1
    return {"status": "ok", "saved": len(saved), "skipped": skipped,
            # total 只有在每個人都一樣時才給得出來；不一樣時前端要改口說「每位變動
            # 費用 X」，而不是報一個沒有人真的收到的數字。
            "total": next(iter(distinct)) if len(distinct) == 1 else 0,
            "items_total": items_total, "base_varies": varies,
            "notified": notify}


@app.post("/dues/bill_event_fees")
async def dues_bill_event_fees(request: Request):
    """把這個月還沒記帳的地區活動報名費，一次帶進各社友的帳單（向社友請款）。

    社的帳戶早就把這筆錢墊給地區了，請款只是把它攤回報名的人身上。逐一開帳單也做
    得到，只是一場年會二十個人就要開二十次，沒有人會做完。

    仍然是「執秘按了才算」—— 自動在報名當下記帳的那一版被 revert 過兩次。已經對過
    帳的帳單一律跳過：那個月的錢已經結清，事後偷偷加一筆進去，社友收到的數字會跟他
    當初繳的對不起來。金額讀不出來的（每隊、多種方案）也跳過，回報給執秘自己填。"""
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "message": "無社費記帳權限"}
    body = await request.json()
    club = _target_club(admin_uid, str(body.get("club", "")).strip())
    month = str(body.get("month", "")).strip()
    if not (club and _valid_month(month)):
        return {"status": "invalid", "message": "缺少社別 / 月份"}
    fees = _event_fees_by_uid(club, month)
    names = {m["line_user_id"]: (m.get("full_name") or "社友")
             for m in db.get_club_members(club)}
    rows = {r["line_user_id"]: r for r in db.list_dues(club, month)}
    rb = _rates_for_month(club, month)   # 推播的總額含固定月費，而那個數字因人而異
    notify = bool(body.get("notify", True))
    billed, saved, skipped_confirmed, skipped_no_amount = 0, [], [], []
    for uid, items in fees.items():
        usable = [f for f in items if f["amount"] > 0]
        skipped_no_amount += [{"name": names.get(uid, "社友"), "title": f["title"]}
                              for f in items if not f["amount"]]
        if not usable:
            continue
        row = rows.get(uid)
        if row and row.get("confirmed"):
            skipped_confirmed.append({"name": names.get(uid, "社友"), "count": len(usable)})
            continue
        customs = list((row or {}).get("customs") or [])
        customs += [{"name": f["name"], "amount": f["amount"], "event_id": f["event_id"]}
                    for f in usable]
        meal, iou, customs = _clean_dues_items({
            "meal": (row or {}).get("meal") or 0, "iou": (row or {}).get("iou") or 0,
            "customs": customs})
        db.upsert_dues(club, month, uid, meal, iou, customs)
        billed += len(usable)
        saved.append((uid, _dues_total(meal, iou, customs, *_member_rates(rb, uid))))
    if notify:
        for uid, total in saved:
            _push_dues_bill(uid, month, total)
    return {"status": "ok", "billed": billed, "members": len(saved),
            "skipped_confirmed": skipped_confirmed, "skipped_no_amount": skipped_no_amount,
            "notified": notify and bool(saved)}


@app.get("/dues/me")
async def dues_me(request: Request, month: str = ""):
    """A member views their own dues for a month."""
    uid = request.headers.get("X-Line-UserId", "")
    if not uid:
        return {"status": "no_user"}
    month = month or date.today().strftime("%Y-%m")
    club = db.get_user_club(uid)
    # 社友只看得到金額，看不到自己的會籍類別 —— 減免、榮譽這種事是社內的安排，
    # 不掛在本人的帳單上。溢繳餘額則要看得到：不然他會照著總計再匯一次全額。
    return {"status": "ok", "month": month, **_member_month(club, uid, month)}


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


# ── 財務管理看板 (finance.html) ────────────────────────────────────────────────
# 錢的兩邊本來各自關在一個 modal 裡：社費收入在 club_dues（逐位社友一張帳單），
# 社務支出在 club_finance（每月一張表）。兩邊都看得到單筆，卻沒有人看得到
# 「這個月該收多少、收了多少、還差誰、扣掉支出剩多少」。以下端點把兩張表合成
# 財務長要的那一張，finance.html 只負責畫。

def _this_month() -> str:
    return date.today().strftime("%Y-%m")


def _valid_month(m: str) -> bool:
    """A "YYYY-MM" string naming a real month — everything below keys on it."""
    parts = (m or "").split("-")
    return (len(parts) == 2 and len(parts[0]) == 4 and len(parts[1]) == 2
            and parts[0].isdigit() and parts[1].isdigit() and 1 <= int(parts[1]) <= 12)


def _month_list(n: int, end: str = "") -> list[str]:
    """The n months ending at `end` (default this month), oldest first."""
    last = end if _valid_month(end) else _this_month()
    y, m = int(last[:4]), int(last[5:7])
    out = []
    for back in range(n - 1, -1, -1):
        total = y * 12 + (m - 1) - back
        out.append(f"{total // 12:04d}-{total % 12 + 1:02d}")
    return out


# ── 地區活動報名費 → 社費帳單 ─────────────────────────────────────────────────
# 報名跟收錢本來是兩個系統：報名在 registrations，欠社裡的錢在 club_dues，中間
# 靠執秘自己記得「這個月誰報了年會」。這裡把報名撈進帳單編輯視窗，金額自動抓，
# 但要不要記、記多少仍然是執秘按了才算 —— 費用文字是寫給人看的，程式看不懂的
# 情況（每隊、多種方案、計畫金額）遠比想像的多，不能自作主張扣人家錢。

# 這幾個字一出現，那個數字就不是「每人報名費」：每隊 NT$1,500 是一整隊的錢，
# 記到一個人頭上會多收四倍。
_FEE_NOT_PER_PERSON = re.compile(r"每隊|每社|每組|每桌|團體|保證金|贊助|捐款")

# 明講不用錢的活動：連列都不要列出來，帳單編輯視窗只該擺真的要收的錢。
_FEE_FREE = re.compile(r"免費|免收|不收費|無須繳費|無需繳費")


def _guess_fee_amount(text: str) -> int:
    """從活動的費用文字猜每人報名費；猜不出來回 0，由執秘自己填。

    刻意保守。文字裡有兩個以上不同金額的是多種方案，只有人知道該收哪一個；
    六位數以上不是報名費，是計畫金額或捐款目標（惜食行動計畫 NT$100,000）。"""
    t = str(text or "")
    if not t.strip() or _FEE_NOT_PER_PERSON.search(t):
        return 0
    amounts = {int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", t)}
    amounts = {n for n in amounts if 0 < n < 100000}
    return amounts.pop() if len(amounts) == 1 else 0


def _registration_fee(reg: dict) -> tuple[int, str]:
    """(金額, 來源) for one registration. 高球以社友自己選的球場方案為準 ——
    下場含餐與僅餐會不同價，一個「每人多少錢」表達不出來。"""
    if reg.get("course_plan"):
        try:
            plans = json.loads(reg.get("golf_plans") or "[]")
        except (ValueError, TypeError):
            plans = []
        plan = next((p for p in plans if isinstance(p, dict)
                     and p.get("code") == reg["course_plan"]), None)
        if plan:
            return int(plan.get("fee") or 0), f"球場方案：{plan.get('label', '')}"
    return _guess_fee_amount(reg.get("fee")), str(reg.get("fee") or "")


def _charged_events(club: str) -> set[tuple[str, int]]:
    """(社友, 活動) 已經記過報名費的組合，橫跨所有月份。"""
    done = set()
    for row in db.list_dues_event_items(club):
        for c in (row.get("customs") or []):
            if isinstance(c, dict) and c.get("event_id"):
                try:
                    done.add((row["line_user_id"], int(c["event_id"])))
                except (TypeError, ValueError):
                    continue
    return done


def _event_fees_by_uid(club: str, month: str) -> dict[str, list[dict]]:
    """每位社友這個月還沒記帳的地區活動報名費，供帳單編輯視窗列出來。"""
    charged = _charged_events(club)
    out: dict[str, list[dict]] = {}
    for reg in db.list_club_event_registrations(club, month):
        uid, event_id = reg["line_user_id"], int(reg["event_id"])
        if (uid, event_id) in charged:
            continue
        amount, note = _registration_fee(reg)
        fee_text = str(reg.get("fee") or "")
        # 沒有費用、或公文寫明免費的，不是「執秘忘了填金額」，是真的不用收
        if not amount and (not fee_text.strip() or _FEE_FREE.search(fee_text)):
            continue
        title = str(reg.get("title") or "").strip() or f"活動 {event_id}"
        out.setdefault(uid, []).append({
            "event_id": event_id,
            "title": title,
            "name": f"{title} 報名費",
            "date": reg["date"].isoformat() if reg.get("date") else "",
            "amount": amount,
            "note": note,
            # 這個月報的名，還是這個月舉行的活動 —— 執秘要看得出這筆為什麼在這裡
            "registered_this_month": reg.get("reg_month") == month,
        })
    return out


def _advance_ledger(club: str) -> dict:
    """社為社友墊出去的地區報名費，整個社的歷史一次算完。

    地區活動的報名費是社的帳戶先匯給地區，隔月才在社費帳單上向社友收。錢確實出去
    了，所以那個月就是支出；收回來是社友的帳單對帳的那個月（整張帳單計進「已繳」，
    報名費就在裡面）。中間那段沒收回來的差額要看得見，否則期末結餘會比銀行裡的多。

    不另存一份代墊記錄：報名清單與帳單本來就是同一筆錢的兩端，存第三份只會多一份
    對不起來的答案。金額以「帳單上記的」優先 —— 每隊、多種方案這類程式讀不懂的
    價格，執秘在帳單裡填的數字就是唯一權威，支出面要跟著他走。"""
    billed: dict[tuple[str, int], dict] = {}
    for row in db.list_dues_event_items(club):
        for c in (row.get("customs") or []):
            if not (isinstance(c, dict) and c.get("event_id")):
                continue
            try:
                key = (row["line_user_id"], int(c["event_id"]))
            except (TypeError, ValueError):
                continue
            billed[key] = {"month": row["month"], "amount": int(c.get("amount") or 0),
                           "collected": bool(row.get("confirmed"))}

    # 名字一次查完：逐筆去查 personal_information 等於報名筆數乘一次往返
    names = {m["line_user_id"]: (m.get("full_name") or "社友")
             for m in db.get_club_members(club)}

    items, unknown = [], []
    for reg in db.club_district_registrations(club):
        uid, event_id = reg["line_user_id"], int(reg["event_id"])
        bill = billed.get((uid, event_id))
        amount = bill["amount"] if bill else _registration_fee(reg)[0]
        title = str(reg.get("title") or "").strip() or f"活動 {event_id}"
        fee_text = str(reg.get("fee") or "")
        if not amount:
            # 真的免費的不是代墊；讀不懂的價格是「還不知道墊了多少」，要講出來
            if fee_text.strip() and not _FEE_FREE.search(fee_text):
                unknown.append({"uid": uid, "name": names.get(uid, "社友"), "event_id": event_id,
                                "title": title, "fee": fee_text,
                                "advance_month": reg.get("reg_month") or ""})
            continue
        items.append({
            "uid": uid, "name": names.get(uid, "社友"), "event_id": event_id, "title": title,
            "amount": amount, "date": reg["date"].isoformat() if reg.get("date") else "",
            # 報名當下社就把錢匯給地區了，代墊算在報名的那個月
            "advance_month": reg.get("reg_month") or "",
            "bill_month": bill["month"] if bill else "",
            "collected": bool(bill and bill["collected"]),
        })

    by_month: dict[str, int] = {}
    collected_by_month: dict[str, int] = {}
    for it in items:
        if it["advance_month"]:
            by_month[it["advance_month"]] = by_month.get(it["advance_month"], 0) + it["amount"]
        if it["collected"] and it["bill_month"]:
            collected_by_month[it["bill_month"]] = \
                collected_by_month.get(it["bill_month"], 0) + it["amount"]
    return {"items": items, "unknown": unknown,
            "by_month": by_month, "collected_by_month": collected_by_month}


def _advance_outstanding(ledger: dict, month: str) -> dict:
    """截至某個月，墊出去還沒收回來的錢。

    用月份比大小而不是時間戳，跟結轉同一套算法：翻回上個月看到的就是上個月當時的
    狀態，而不是今天的狀態。"""
    out = [it for it in ledger["items"]
           if it["advance_month"] and it["advance_month"] <= month
           and not (it["collected"] and it["bill_month"] and it["bill_month"] <= month)]
    billed = [it for it in out if it["bill_month"]]
    # 這個月墊出去的每一筆（含已經收回來的）：支出面那個數字是它們加起來的，
    # 點開來要對得出是哪幾個人、哪幾場，不然「本月代墊 9,200」沒有人能查證。
    this_month = [it for it in ledger["items"] if it["advance_month"] == month]
    return {
        "total": sum(it["amount"] for it in out),
        "billed": sum(it["amount"] for it in billed),
        "unbilled": sum(it["amount"] for it in out if not it["bill_month"]),
        "count": len(out),
        # 未收回的裡面，這個月才墊的已經列在 month_items，這裡只留更早以前的
        "items": sorted((it for it in out if it["advance_month"] != month),
                        key=lambda it: (it["advance_month"], it["name"])),
        "month_items": sorted(this_month, key=lambda it: (it["name"], it["title"])),
        "month_total": sum(it["amount"] for it in this_month),
        "unknown": [u for u in ledger["unknown"]
                    if u["advance_month"] and u["advance_month"] <= month],
    }


def _dues_rows(club: str, month: str, members: list[dict],
               book: dict | None = None) -> list[dict]:
    """One row per member: what they were billed and how far the money got.

    Members with no bill are listed too — "執秘 hasn't billed 王大明 yet" is a hole
    in the month's takings just as much as "王大明 hasn't paid", and only this view
    can show it."""
    by_uid = {r["line_user_id"]: r for r in db.list_dues(club, month)}
    # 這個月的費率查一次，逐人取值是純字典查找 —— 常年月費會因會籍類別而異，但
    # 那份對照表整社共用一份。
    rb = _rates_for_month(club, month, book)
    fees = _event_fees_by_uid(club, month)
    rows = []
    for m in members:
        uid = m["line_user_id"]
        rows.append(_dues_row(uid, m.get("full_name") or "", m.get("nickname") or "",
                              by_uid.pop(uid, None), rates=_member_rates(rb, uid),
                              tier=rb["tier_of"].get(uid, ""),
                              event_fees=fees.get(uid, [])))
    # 有帳單、但 personal_information 裡查不到的人（已退社、資料未建）不能就這樣消失，
    # 否則看板的應收總額會對不上帳。
    for uid, row in by_uid.items():
        rows.append(_dues_row(uid, _member_name(uid), "", row, orphan=True,
                              rates=_member_rates(rb, uid),
                              tier=rb["tier_of"].get(uid, ""),
                              event_fees=fees.get(uid, [])))
    return rows


def _dues_row(uid: str, name: str, nickname: str, row: dict | None,
              orphan: bool = False, rates: tuple = (DUES_BASE, DUES_DISTRICT, ()),
              tier: str = "", event_fees: list[dict] | None = None) -> dict:
    """狀態只看錢收到哪一步：未繳 → 待對帳 → 已繳。

    沒有費用明細的社友一樣是「未繳」，金額算固定月費 —— 常年月費與地區分攤金
    每個月都要收，執秘還沒記到餐費／IOU 不代表這個人這個月不用繳。has_bill 留
    著只是給「批次記帳」找出誰還沒登過變動費用，不再是一種狀態。"""
    d = _dues_payload(row, *rates)
    status = "confirmed" if d["confirmed"] else ("reported" if d["is_paid"] else "unpaid")
    return {
        "uid": uid, "name": name or "（未建資料）", "nickname": nickname,
        "has_bill": d["has_bill"], "total": d["total"],
        # 常年月費逐人給：同一個社裡不同會籍類別的人金額不一樣，帳單畫面不能再
        # 拿看板層級的那一個數字去算總計（分攤金全社一律，仍然放在看板層級）。
        "base": d["base"], "tier": tier,
        "extras": d["extras"], "extras_total": d["extras_total"],
        "meal": d["meal"], "iou": d["iou"], "customs": d["customs"],
        "is_paid": d["is_paid"], "confirmed": d["confirmed"],
        "bank_digits": (row or {}).get("bank_digits") or "",
        "paid_amount": (row or {}).get("paid_amount"),
        "status": status, "orphan": orphan,
        # 這個月報名的地區活動裡，還沒記進任何一張帳單的那些
        "event_fees": event_fees or [],
        # 溢繳結算的欄位先用「沒有溢繳」預設好：沒經過 _settle_month 的呼叫端
        # （單月查詢、舊的路徑）才會拿到跟以前一模一樣的數字。
        **_settle_row({"total": d["total"], "confirmed": d["confirmed"],
                       "paid_amount": (row or {}).get("paid_amount")}, 0),
    }


def _settle_row(row: dict, credit_in: int) -> dict:
    """一位社友、一個月的結算：先用溢繳抵，再看實際收到多少。

        抵扣 = min(帶進來的溢繳, 帳單金額)
        還要付的現金 = 帳單金額 － 抵扣
        對過帳的話，實收 = paid_amount（NULL 就是「剛好繳完該付的」）
        實收比該付的多 → 多的留到下個月；少 → 短的留在這個月當欠款

    短收刻意不變成「負的溢繳」：負餘額會把債務悄悄搬離它發生的月份，而執秘追繳
    的第一句話就是「哪一個月？」。被溢繳抵光的月份 settled 為真但沒有 confirmed
    —— 沒有現金要確認，不該掛在未繳名單上等人去按一顆沒有意義的按鈕。"""
    billed = int(row.get("total") or 0)
    applied = min(max(credit_in, 0), billed)
    net_due = billed - applied
    left = max(credit_in, 0) - applied
    if row.get("confirmed"):
        paid = row.get("paid_amount")
        paid = net_due if paid is None else int(paid)
        return {"credit_available": credit_in, "credit_applied": applied,
                "due_now": net_due, "credit_left": left + max(0, paid - net_due),
                "credit_earned": max(0, paid - net_due),
                "cash": paid, "short": max(0, net_due - paid), "settled": True}
    # 沒對帳的月份只有「真的被溢繳抵掉」才算結清。金額本來就是 0 的帳單不算 ——
    # 那不是收到了錢，是根本沒有帳，人數統計要跟以前一樣把它留在未繳那一欄。
    return {"credit_available": credit_in, "credit_applied": applied,
            "due_now": net_due, "credit_left": left, "credit_earned": 0,
            "cash": 0, "short": net_due, "settled": net_due == 0 and applied > 0}


def _retire_arrears(credit: dict, arrears: dict) -> dict:
    """手上的溢繳先把還掛著的舊月份補掉，剩下的才是「可留抵」。

    社友一次拿一大筆過來的時候，那筆錢裡通常有一部分是補上個月沒繳的。留抵的數字
    不扣掉欠款的話，畫面會同時說「他欠 2,225」和「他有 20,000 可以留抵」，而那
    20,000 裡就有那 2,225。

    補掉的月份不從清單上消失，改成標 covered ——「他七月的帳是用八月多繳的錢補的」
    這件事執秘要看得見，不然他會跑去七月按一次已收，那筆錢就被算進兩個月。"""
    for uid, left in list(credit.items()):
        for a in arrears.get(uid, []):          # 已經照月份由舊到新排好
            if left <= 0:
                break
            used = min(left, a["amount"])
            if not used:
                continue
            a["amount"] -= used
            a["covered"] = a.get("covered", 0) + used
            left -= used
        credit[uid] = left
    return credit


def _member_credit(club: str, uid: str, month: str) -> int:
    """這位社友走到 month 之前，累積剩下多少溢繳。

    折疊逐人獨立（A 的餘額不會讀到 B 的任何一列），所以社友自己開帳單、或執秘在
    後台查單一社友時，不必把整個社每個月重算一遍。走的月份清單與抵扣規則跟看板
    的結轉完全相同 —— 兩邊算出不一樣的餘額比算不出來更糟。"""
    months, _opening, _applies = _carry_months(club, month)
    if not (club and uid and months):
        return 0
    by_month = {r["month"]: r for r in db.list_member_dues(club, uid)}
    book = _tier_book(club)
    credit, owed = 0, 0
    for m in months:
        row = by_month.get(m)
        d = _dues_payload(row, *_dues_rates(club, m, uid, _rates_for_month(club, m, book)))
        s = _settle_row({"total": d["total"], "confirmed": d["confirmed"],
                         "paid_amount": (row or {}).get("paid_amount")}, credit)
        credit, owed = s["credit_left"], owed + s["short"]
        # 跟看板同一條規矩（見 _retire_arrears）：先補還欠著的月份，剩下的才留抵。
        # 兩邊算出不一樣的餘額，比算不出來更糟。
        used = min(credit, owed)
        credit, owed = credit - used, owed - used
    return credit


def _member_month(club: str, uid: str, month: str, rb: dict | None = None) -> dict:
    """一位社友某個月的帳單：金額、繳款狀態，加上溢繳抵扣後真正要付多少。"""
    row = db.get_dues(club, month, uid) if (club and month and uid) else None
    rb = rb if rb is not None else _rates_for_month(club, month)
    d = _dues_payload(row, *_member_rates(rb, uid))
    s = _settle_row({"total": d["total"], "confirmed": d["confirmed"],
                     "paid_amount": (row or {}).get("paid_amount")},
                    _member_credit(club, uid, month))
    return {**d, **s, "paid_amount": (row or {}).get("paid_amount"),
            "bank_digits": (row or {}).get("bank_digits") or ""}


def _settle_month(rows: list[dict], credit_in: dict) -> dict:
    """把一個月的每一列結算掉，回傳下個月要帶過去的 uid -> 溢繳餘額。

    狀態在這裡才細化：_dues_row 手上沒有溢繳的上下文，它只知道對帳與否。"""
    credit_out: dict[str, int] = {}
    for r in rows:
        s = _settle_row(r, int(credit_in.get(r["uid"], 0)))
        r.update(s)
        if not r["confirmed"] and s["settled"] and s["credit_applied"]:
            r["status"] = "credited"
        if s["credit_left"]:
            credit_out[r["uid"]] = s["credit_left"]
    return credit_out


def _dues_totals(rows: list[dict]) -> dict:
    """應收 = 全社每個人的應繳，含只有固定月費的人。

    這裡有兩個長得很像、但絕對不能混用的數字：

      settled = 帳單結清了多少（收繳率、應收看板看這個）
      cash    = 實際進了多少錢（社的結餘、趨勢圖看這個）

    沒有人溢繳時兩者相等，那正是以前只用一個 confirmed 也不會出錯的原因。有人
    一次匯一整年之後就不等了：那個月現金多收、但只結清了一個月的帳單，而結轉要
    的是現金，收繳率要的是帳單。

        cash = Σ已對帳的帳單金額 － Σ抵扣 ＋ Σ新產生的溢繳 － Σ短收
    """
    expected = sum(r["total"] for r in rows)
    settled = sum(r["total"] for r in rows if r["settled"])
    cash = sum(r["cash"] for r in rows)
    reported = sum(r["total"] for r in rows if r["is_paid"] and not r["settled"])
    return {
        "members": len(rows),
        # 還沒登過餐費／IOU 的人數：不是狀態，是「批次記帳」的對象
        "no_bill": sum(1 for r in rows if not r["has_bill"]),
        "expected": expected, "settled": settled, "cash": cash, "reported": reported,
        # confirmed 留著是資料庫事實（有幾筆被按過對帳），沒有人再拿它算錢
        "confirmed": sum(r["total"] for r in rows if r["confirmed"]),
        "outstanding": expected - settled - reported,
        "credit_applied": sum(r["credit_applied"] for r in rows),
        "credit_earned": sum(r["credit_earned"] for r in rows),
        "credit_outstanding": sum(r["credit_left"] for r in rows),
        "shortfall": sum(r["short"] for r in rows if r["confirmed"]),
        "confirmed_count": sum(1 for r in rows if r["settled"]),
        "credited_count": sum(1 for r in rows if r["status"] == "credited"),
        "reported_count": sum(1 for r in rows if r["is_paid"] and not r["settled"]),
        "unpaid_count": sum(1 for r in rows if not r["is_paid"] and not r["settled"]),
        "rate": round(settled / expected * 100) if expected else 0,
    }


def _expense_summary(data: dict | None, event_advance: int = 0) -> dict:
    """社務對帳表 → 支出面總計。欄位與 /club/finance 存進去的那份一致。

    地區報名費的代墊（event_advance）不在對帳表裡，是從報名清單算出來的，但錢確實
    是那個月從社的帳戶出去的，所以一起計進支出總額。三個呼叫端（看板、結轉、趨勢）
    都要傳同一個數字，否則期初結餘就不再等於每個月淨額的累加。"""
    d = data or {}
    fixed = [f for f in d.get("fixed", []) if isinstance(f, dict)]
    advances = [a for a in d.get("advances", []) if isinstance(a, dict)]
    rent, salary = int(d.get("rent") or 0), int(d.get("salary") or 0)
    return {
        "rent": rent, "salary": salary, "fixed": fixed, "advances": advances,
        "fixed_total": sum(int(f.get("amount") or 0) for f in fixed),
        "advance_total": sum(int(a.get("amount") or 0) for a in advances),
        "event_advance": int(event_advance or 0),
        "total": (rent + salary + int(event_advance or 0)
                  + sum(int(f.get("amount") or 0) for f in fixed)
                  + sum(int(a.get("amount") or 0) for a in advances)),
    }


def _carry_months(club: str, month: str, ledger: dict | None = None
                  ) -> tuple[list[str], dict | None, bool]:
    """要結轉的月份（本月之前、期初之後、且確實有記過帳的），以及期初設定。

    代墊的月份也要算進來：一個月可能沒有任何帳單、也沒填支出表，卻墊了一筆地區
    報名費出去，跳過它就少算一筆支出。"""
    opening = db.get_opening_balance(club)
    start = opening["month"] if opening else ""
    applies = bool(opening) and month >= start
    active = set(db.finance_months(club)) | set((ledger or {}).get("by_month", {}))
    months = sorted(m for m in active if m < month and (not start or m >= start))
    return months, opening, applies


def _months_between(start: str, end_exclusive: str) -> list[str]:
    """start 到 end_exclusive（不含）之間每一個月，含中間沒有任何紀錄的月份。

    溢繳要逐月抵下去，而「執秘還沒開過帳的月份」在 club_dues 裡是一列都沒有的。
    只走有紀錄的月份，那些空月份就不會把餘額吃掉，同一筆溢繳會在之後每一個月都
    被當成還能用 —— 一筆錢抵兩次。固定月費本來就不存在帳單列裡（見 db.md），
    月份是不是「有帳」跟他要不要繳這個月無關。"""
    if not (start and end_exclusive):
        return []
    y, m = int(start[:4]), int(start[5:7])
    out: list[str] = []
    while f"{y:04d}-{m:02d}" < end_exclusive:
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        if len(out) > 240:          # 二十年的保險絲，資料壞掉時不要無限跑
            break
    return out


def _carry_forward(club: str, month: str, members: list[dict], ledger: dict,
                   book: dict | None = None) -> dict:
    """這個月開始時手上有多少 = 期初結餘 + 期初之後、本月之前每個月的淨額，
    外加每位社友從那些月份欠到現在的錢。

    逐月累加而不是存一個數字：任何一個舊月份被更正（補記一筆餐費、改對帳狀態），
    往後每一個月的期初都要跟著變，存起來的快照只會慢慢跟事實脫節。

    社的結餘、社友的欠款與溢繳餘額走同一趟迴圈：三者都是「把之前每個月再看一遍」，
    各跑一次等於把最貴的部分做三遍，也給了三份可能對不起來的答案。"""
    months, opening, applies = _carry_months(club, month, ledger)
    carry = int(opening["amount"]) if applies else 0
    # uid -> [{month, amount, is_paid}]：欠的是哪幾個月要講得出來，社友被要求補繳
    # 的第一句話一定是「哪一個月？」
    arrears: dict[str, list[dict]] = {}
    credit: dict[str, int] = {}
    advance_by_month = ledger.get("by_month", {})
    if book is None:
        book = _tier_book(club)     # 每個月都要用同一份類別對照表，別在迴圈裡各查一次
    # 走的是連續的月份，中間沒開過帳的月份也要走過去把溢繳抵掉（見 _months_between）。
    # 但欠款只列有紀錄的那些月份 —— 「上期未繳只列執秘開過帳的月份」是本來就有的
    # 規矩，這次不動它。
    billed = set(months)
    for m in (_months_between(months[0], month) if months else []):
        rows = _dues_rows(club, m, members, book)
        # 上個月剩下的溢繳帶進這個月抵扣，抵完剩下的再帶去下一個月
        credit = _settle_month(rows, credit)
        _retire_arrears(credit, arrears)
        if m not in billed:
            continue
        expense = _expense_summary(db.get_club_finance(club, m), advance_by_month.get(m, 0))
        # 結轉看的是真的進來的錢，不是結清了多少帳單：社友一次匯一整年時，那個月
        # 的現金比帳單多，而銀行裡的數字是現金。
        carry += _dues_totals(rows)["cash"] - expense["total"]
        for r in rows:
            # 收到的錢不夠付的那部分掛在他頭上（完全沒對帳＝整筆都掛著）。社友自己
            # 回報但還沒對到的一樣掛著，只是標記出來，執秘看得出那筆是「在路上」
            # 還是真的沒繳。
            if r["short"]:
                arrears.setdefault(r["uid"], []).append(
                    {"month": m, "amount": r["short"], "is_paid": r["is_paid"],
                     "partial": bool(r["confirmed"])})
    return {"carry": carry,
            "arrears": arrears,
            "credit": credit,
            "opening_month": opening["month"] if opening else "",
            "opening_amount": int(opening["amount"]) if opening else 0,
            "has_opening": opening is not None,
            "opening_applies": applies,
            "counted_months": len(months)}


@app.post("/finance/opening")
async def finance_opening(request: Request):
    """設定期初結餘：某個月開始時社的帳上有多少錢。

    系統上線之前的收支沒有任何紀錄可以推算，只能由財務長填一次。填了之後每個月
    的期初都自動結轉，不必再輸入第二次。amount 可以是負數（社有欠款）。"""
    uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(uid):
        return {"status": "forbidden", "message": "無財務管理權限"}
    body = await request.json()
    club = _target_club(uid, str(body.get("club", "")).strip())
    if not club:
        return {"status": "no_club", "message": "找不到您的社別"}
    if body.get("clear"):
        db.delete_opening_balance(club)
        return {"status": "ok", "cleared": True}
    month = str(body.get("month", "")).strip()
    if not _valid_month(month):
        return {"status": "invalid", "message": "生效年月格式需為 YYYY-MM"}
    try:
        amount = int(body.get("amount", 0) or 0)
    except (TypeError, ValueError):
        return {"status": "invalid", "message": "金額需為數字"}
    db.save_opening_balance(club, month, amount)
    return {"status": "ok", "month": month, "amount": amount}


@app.get("/finance/board")
async def finance_board(request: Request, month: str = "", club: str = ""):
    """One month of club money: per-member dues collection + the expense sheet."""
    uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(uid):
        raise HTTPException(status_code=403, detail="無財務管理權限")
    club = _target_club(uid, club)
    if not club:
        return {"status": "no_club", "message": "找不到您的社別"}
    month = month if _valid_month(month) else _this_month()
    members = db.get_club_members(club)      # 結轉每個月都要用同一份名單，撈一次就好
    book = _tier_book(club)                  # 會籍類別的費率與指派，同樣整趟共用一份
    rows = _dues_rows(club, month, members, book)
    # 代墊帳整個社算一次，看板與結轉共用：一個月一個月去算等於把最貴的查詢乘上月份數
    ledger = _advance_ledger(club)
    carry = _carry_forward(club, month, members, ledger, book)
    # 每位社友把自己的上期未繳帶在身上：帳單編輯與明細表都要看得到「他還欠哪幾
    # 個月」，逐一去翻舊月份是沒有人會做的事。
    arrears = carry.pop("arrears", {})
    # 本月要用的溢繳是「本月之前累積剩下的」，所以先結轉再結算這個月；本月自己多繳
    # 出來的那部分，同樣要先把還掛著的舊月份補掉才算留抵。
    left = _retire_arrears(_settle_month(rows, carry.pop("credit", {})), arrears)
    for r in rows:
        r["credit_left"] = left.get(r["uid"], 0)
    rows.sort(key=lambda r: (["unpaid", "reported", "credited", "confirmed"]
                             .index(r["status"]), r["name"]))
    totals = _dues_totals(rows)
    expense = _expense_summary(db.get_club_finance(club, month),
                               ledger["by_month"].get(month, 0))
    net = totals["cash"] - expense["total"]
    for r in rows:
        owed = arrears.get(r["uid"], [])
        r["arrears"] = owed
        # 已經被溢繳補掉的部分不再算進未繳（那一筆留在清單上標 covered，讓執秘看得
        # 出來是補過的，別再跑去那個月按一次已收）
        r["arrears_total"] = sum(x["amount"] for x in owed)
        r["arrears_covered"] = sum(x.get("covered", 0) for x in owed)
        # 本月實付 + 上期未繳。刻意不寫進 total：那些月份的帳單本來就還在，
        # 加進來的話應收會把同一筆錢算兩次。用 due_now 而不是 total —— 不該去追
        # 一筆他自己的溢繳已經蓋掉的錢。
        r["due_with_arrears"] = r["due_now"] + r["arrears_total"]
    totals["arrears"] = sum(r["arrears_total"] for r in rows)
    totals["arrears_members"] = sum(1 for r in rows if r["arrears_total"])
    return {"status": "ok", "club": club, "month": month, "members": rows,
            # 跨地區管理員可以切社，畫面要知道現在看的是誰、還能切到哪些社
            "all_districts": db.is_super_admin(uid),
            "clubs": [c["club_name"] for c in db.all_clubs()] if db.is_super_admin(uid) else [],
            "totals": totals, "expense": expense,
            # 墊出去還沒收回來的：期末結餘裡有多少其實是「在社友身上」的錢
            "advance_outstanding": _advance_outstanding(ledger, month),
            "net": net,
            # 上期結餘與期末結餘：社的錢是滾過來的，只看單月會以為社裡只有這個月
            # 收到的那些錢。closing 就是下個月的期初。
            **carry,
            "closing": carry["carry"] + net,
            # 會籍類別：有在用的社才會是非空的，畫面照這個決定要不要顯示類別欄
            "tiers": _tiers_payload(club, month),
            # 帳單編輯要顯示固定的兩項，金額由後端給，前端不自己抄一份常數。
            # base 是「全社預設」那一個；逐位社友的常年月費在他自己那一列上。
            **_rates_payload(club, month)}


def _rates_payload(club: str, month: str) -> dict:
    """該月適用的費率，外加它是社自訂的還是系統預設 —— 看板要講得出「這個數字
    是哪裡來的」，否則沒人敢改。"""
    s = db.get_dues_settings(club, month)
    # 這裡描述的是「全社預設」，而全社預設沒有加項那回事（見 _member_rates）
    base, district, _ = _dues_rates(club, month)
    return {"base": base, "district": district,
            "rates_from": s["effective_month"] if s else "",
            "rates_default": s is None}


@app.get("/dues/settings")
async def dues_settings_get(request: Request, month: str = "", club: str = ""):
    """月費費率設定：這個月適用哪一段，以及全部的生效歷程。"""
    uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(uid):
        raise HTTPException(status_code=403, detail="無社費設定權限")
    club = _target_club(uid, club)
    if not club:
        return {"status": "no_club", "message": "找不到您的社別"}
    month = month if _valid_month(month) else _this_month()
    return {"status": "ok", "club": club, "month": month,
            "default_base": DUES_BASE, "default_district": DUES_DISTRICT,
            "history": db.list_dues_settings(club),
            "tiers": _tiers_payload(club, month),
            **_rates_payload(club, month)}


def _tiers_payload(club: str, month: str) -> list[dict]:
    """各會籍類別在這個月的常年月費、人數，以及它自己的生效歷程。

    沒有任何類別的社回傳空陣列，畫面就跟以前一模一樣 —— 這個功能不該讓沒在用的
    社多看到一塊東西。"""
    book = _tier_book(club)
    rb = _rates_for_month(club, month, book)
    counts: dict[str, int] = {}
    for uid, tier in rb["tier_of"].items():
        if tier:
            counts[tier] = counts.get(tier, 0) + 1
    history: dict[str, list] = {}
    for r in book["rates"]:
        items = _clean_fixed_items(r.get("items"))
        history.setdefault(r["tier"], []).append(
            {"effective_month": r["effective_month"], "base": int(r["base"]),
             "label": r.get("label") or "",
             # 每一段自己的加項：刪除鈕的提示會引用那一段的金額，拿現在生效的那組
             # 去寫的話，刪舊段落時畫面講的是另一段的數字。
             "items": items, "extras_total": _items_total(items)})
    return [{"tier": t,
             # 這個月還沒有生效段落的類別：常年月費落回全社預設，畫面要講得出來，
             # 不然執秘看到的是一個沒有來源的數字。加項不落回（見 _member_rates）。
             "base": rb["tier_base"].get(t, rb["default_base"]),
             "label": rb["tier_label"].get(t, ""),
             "rate_from": rb["tier_from"].get(t, ""),
             "items": rb["tier_items"].get(t, []),
             "extras_total": _items_total(rb["tier_items"].get(t, [])),
             "members": counts.get(t, 0),
             "history": history.get(t, [])}
            for t in sorted(history.keys() | counts.keys())]


@app.post("/dues/settings")
async def dues_settings_save(request: Request):
    """Set the rates that apply from `effective_month` onwards.

    Only forward-looking on purpose: bills already issued and paid were issued at
    the old rate, and silently restating them would break every reconciliation
    done to date. Correcting an earlier段 means saving that段's own month."""
    uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(uid):
        return {"status": "forbidden", "message": "無社費設定權限"}
    body = await request.json()
    club = _target_club(uid, str(body.get("club", "")).strip())
    if not club:
        return {"status": "no_club", "message": "找不到您的社別"}
    month = str(body.get("effective_month", "")).strip()
    if not _valid_month(month):
        return {"status": "invalid", "message": "生效月份格式需為 YYYY-MM"}
    try:
        base = int(body.get("base", 0) or 0)
        district = int(body.get("district", 0) or 0)
    except (TypeError, ValueError):
        return {"status": "invalid", "message": "金額需為數字"}
    if base < 0 or district < 0:
        return {"status": "invalid", "message": "金額不可為負數"}
    db.save_dues_settings(club, month, base, district)
    return {"status": "ok", "effective_month": month, "base": base, "district": district,
            "total": base + district}


@app.post("/dues/settings/delete")
async def dues_settings_delete(request: Request):
    """Drop one段. The months it covered fall back to the previous段, or to the
    system defaults when it was the only one."""
    uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(uid):
        return {"status": "forbidden", "message": "無社費設定權限"}
    body = await request.json()
    club = _target_club(uid, str(body.get("club", "")).strip())
    if not club:
        return {"status": "no_club", "message": "找不到您的社別"}
    month = str(body.get("effective_month", "")).strip()
    if not _valid_month(month):
        return {"status": "invalid", "message": "生效月份格式需為 YYYY-MM"}
    db.delete_dues_settings(club, month)
    return {"status": "ok", "effective_month": month}


# ── 會籍類別 ──────────────────────────────────────────────────────────────────
# 類別是 A、B、C 這樣的代碼，社友被指到某一個類別，費率掛在類別上。單一字母是
# 刻意的：這個社目前只有三種，取名字反而要多一套建檔、改名、停用的流程。
_TIER_CODES = "ABCDEFGH"


def _clean_tier(value) -> str | None:
    """類別代碼：單一大寫字母，或空字串（＝全社預設）。認不出來的回 None。"""
    t = str(value or "").strip().upper()
    if t == "":
        return ""
    return t if len(t) == 1 and t in _TIER_CODES else None


@app.post("/dues/tier_rate")
async def dues_tier_rate_save(request: Request):
    """設定某個會籍類別從哪個月起收多少常年月費，以及每個月跟著收的固定加項。

    跟全社費率同一個規矩：只往後生效，要更正舊的就存那一段自己的月份。回傳
    affected_confirmed —— 這一段往後有幾張帳單已經對過帳了，畫面要先講清楚。

    items 是整份取代：畫面上留著的那幾筆就是存完之後的全部。

    這裡沒有地區分攤金：分攤金是地區按人頭收的，全社一律，只在 /dues/settings 改。"""
    uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(uid):
        return {"status": "forbidden", "message": "無社費設定權限"}
    body = await request.json()
    club = _target_club(uid, str(body.get("club", "")).strip())
    if not club:
        return {"status": "no_club", "message": "找不到您的社別"}
    tier = _clean_tier(body.get("tier"))
    if not tier:
        return {"status": "invalid", "message": "會籍類別需為單一代碼（A、B、C…）"}
    month = str(body.get("effective_month", "")).strip()
    if not _valid_month(month):
        return {"status": "invalid", "message": "生效月份格式需為 YYYY-MM"}
    try:
        base = int(body.get("base", 0) or 0)
    except (TypeError, ValueError):
        return {"status": "invalid", "message": "金額需為數字"}
    if base < 0:
        return {"status": "invalid", "message": "金額不可為負數"}
    raw_items = body.get("items")
    if isinstance(raw_items, list) and len(raw_items) > _MAX_TIER_ITEMS:
        return {"status": "invalid", "message": f"每個類別最多 {_MAX_TIER_ITEMS} 個固定加項"}
    if any(isinstance(c, dict) and str(c.get("name", "")).strip()
           and _bad_amount(c.get("amount")) for c in (raw_items or [])):
        return {"status": "invalid", "message": "加項金額需為 0 以上的數字"}
    items = _clean_fixed_items(raw_items)
    label = str(body.get("label", "")).strip()[:20]
    db.save_tier_rate(club, tier, month, base, label, items)
    book = _tier_book(club)
    rb = _rates_for_month(club, month, book)
    # 會被這一段影響的人：生效月當下就在這一類的，加上生效月之後才被指進來的。
    # 只看當下的話，「十一月才改成 B 類」的那位不會被算到，但他從十一月起收的
    # 正是這一段的錢。
    members = {u for u, t in rb["tier_of"].items() if t == tier}
    members |= {m["line_user_id"] for m in book["members"]
                if m["tier"] == tier and m["effective_month"] >= month}
    return {"status": "ok", "tier": tier, "effective_month": month, "base": base,
            "label": label, "items": items, "extras_total": _items_total(items),
            "members": len(members),
            "affected_confirmed": db.count_tier_confirmed_bills(club, sorted(members), month)}


@app.post("/dues/tier_rate/delete")
async def dues_tier_rate_delete(request: Request):
    """刪掉某個類別的一段費率。它涵蓋的月份會落回前一段，沒有前一段就落回全社
    預設 —— 也就是說掛在這個類別上的人金額會變，前端刪之前要先問。"""
    uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(uid):
        return {"status": "forbidden", "message": "無社費設定權限"}
    body = await request.json()
    club = _target_club(uid, str(body.get("club", "")).strip())
    if not club:
        return {"status": "no_club", "message": "找不到您的社別"}
    tier = _clean_tier(body.get("tier"))
    month = str(body.get("effective_month", "")).strip()
    if not tier or not _valid_month(month):
        return {"status": "invalid", "message": "缺少類別 / 生效月份"}
    db.delete_tier_rate(club, tier, month)
    return {"status": "ok", "tier": tier, "effective_month": month}


@app.post("/finance/roster/tier")
async def finance_roster_tier(request: Request):
    """把一位社友指到某個會籍類別，從某個月起算。

    指派本身也是分月段的：七月才改成 B 類的人，一到六月的帳仍然要用他當時的類別
    算。看板的欠款與期初結轉都是把舊月份重算一遍的，少了這一層，改一次類別會把
    他過去每個月的欠款金額一起改掉。

    tier 留空 = 從那個月起回到全社預設；clear=true 則是把那一段整個拿掉（指派記
    錯月份時用的）。"""
    uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(uid):
        return {"status": "forbidden", "message": "無社費設定權限"}
    body = await request.json()
    club = _target_club(uid, str(body.get("club", "")).strip())
    if not club:
        return {"status": "no_club", "message": "找不到您的社別"}
    member = str(body.get("uid", "")).strip()
    month = str(body.get("effective_month", "")).strip()
    if not member or not _valid_month(month):
        return {"status": "invalid", "message": "缺少社友 / 生效月份"}
    if bool(body.get("clear", False)):
        db.delete_member_tier(club, member, month)
        return {"status": "ok", "uid": member, "effective_month": month, "cleared": True}
    tier = _clean_tier(body.get("tier"))
    if tier is None:
        return {"status": "invalid", "message": "會籍類別需為單一代碼（A、B、C…）"}
    db.save_member_tier(club, member, month, tier)
    base, district, extras = _dues_rates(club, month, member)
    return {"status": "ok", "uid": member, "tier": tier, "effective_month": month,
            "base": base, "district": district,
            "extras": extras, "extras_total": _items_total(extras)}


@app.post("/finance/confirm")
async def finance_confirm(request: Request):
    """執秘 ticks a member's dues off against the bank statement (or unticks it).

    paid_amount 是選填的：沒帶就是「收到的正是他該付的」，也就是那顆一鍵「標記
    已收」原本的意思。金額不一樣時才帶，多的自動變成他的溢繳。

    取消對帳不會清掉已經登記的實收金額 —— 按錯一顆按鈕不該把記過的數字也弄丟。"""
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "message": "無財務管理權限"}
    body = await request.json()
    month = str(body.get("month", "")).strip()
    uid = str(body.get("uid", "")).strip()
    if not (_valid_month(month) and uid):
        return {"status": "invalid", "message": "缺少月份或社友"}
    confirmed = bool(body.get("confirmed", True))
    has_amount = confirmed and body.get("paid_amount") is not None
    if has_amount:
        if _bad_amount(body.get("paid_amount")):
            return {"status": "invalid", "message": "實收金額需為 0 以上的數字"}
        if int(body.get("paid_amount")) > _MAX_PAID_AMOUNT:
            return {"status": "invalid", "message": "實收金額看起來不合理，請確認"}
    club = _target_club(admin_uid, str(body.get("club", "")).strip())
    # 只有固定月費、還沒登過餐費／IOU 的社友也會繳錢，看板上他就是「未繳」。
    # 這種人還沒有 club_dues 那一列，先補一列空的明細，才有東西可以標記已收。
    if not db.get_dues(club, month, uid):
        db.upsert_dues(club, month, uid, 0, 0, [])
    if has_amount:
        db.set_dues_paid_amount(club, month, uid, int(body.get("paid_amount")))
    db.confirm_dues(club, month, uid, confirmed)
    if confirmed:
        try:
            line_api.push_text(uid, f"✅ 您的 {month} 社費已對帳完成，感謝繳納！")
        except Exception:
            logger.exception("dues confirm push failed for %s", uid)
    return {"status": "ok", "month": month, "uid": uid, "confirmed": confirmed}


@app.post("/finance/bank_digits")
async def finance_bank_digits(request: Request):
    """執秘 代填／更正某位社友該月的匯款末 5 碼。

    社友自己回報是常態，但總有人是當面給現金、用紙條寫帳號、或回報時打錯一碼。
    以前這一欄只有社友本人寫得進去，執秘看著錯的號碼對不了帳也改不動。

    只寫號碼，不動繳款狀態：填了不代表錢進來了，「已繳」仍然要執秘看著對帳單按
    「標記已收」。空字串是清掉（打錯要能改回空白），這也是它不走 /dues/pay 的原因
    —— 那支是社友回報用的，空字串會沿用舊值，而且會把人標成已回報。"""
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "message": "無財務管理權限"}
    body = await request.json()
    month = str(body.get("month", "")).strip()
    uid = str(body.get("uid", "")).strip()
    digits = str(body.get("bank_digits", "")).strip()
    if not (_valid_month(month) and uid):
        return {"status": "invalid", "message": "缺少月份或社友"}
    if digits and (len(digits) != 5 or not digits.isdigit()):
        return {"status": "invalid", "message": "末 5 碼需為 5 位數字"}
    club = _target_club(admin_uid, str(body.get("club", "")).strip())
    if not club:
        return {"status": "no_club", "message": "找不到您的社別"}
    db.set_dues_bank_digits(club, month, uid, digits)
    return {"status": "ok", "month": month, "uid": uid, "bank_digits": digits}


# 實收金額的上限：最大的一張帳單也就幾萬塊，手滑多按一個 0 不該把社的結餘搬走。
_MAX_PAID_AMOUNT = 1_000_000


@app.post("/finance/paid_amount")
async def finance_paid_amount(request: Request):
    """登記某位社友某個月實際收到多少錢。

    帳單上每一個數字都是「他該繳多少」，只有這一個是「錢」。社友一次匯一整年、
    或匯款湊個整數的時候，多的部分會自動變成他的溢繳留到之後幾個月抵扣。

    跟末 5 碼同一個規矩：不碰 confirmed／is_paid，也不推播 —— 登記一個數字不是
    對帳，改一個打錯的數字更不該再通知社友一次。clear=true 是清回「就是應繳金額」。"""
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "message": "無財務管理權限"}
    body = await request.json()
    month = str(body.get("month", "")).strip()
    uid = str(body.get("uid", "")).strip()
    if not (_valid_month(month) and uid):
        return {"status": "invalid", "message": "缺少月份或社友"}
    club = _target_club(admin_uid, str(body.get("club", "")).strip())
    if not club:
        return {"status": "no_club", "message": "找不到您的社別"}
    amount = None
    if not bool(body.get("clear", False)):
        if _bad_amount(body.get("amount")):
            return {"status": "invalid", "message": "實收金額需為 0 以上的數字"}
        amount = int(body.get("amount") or 0)
        if amount > _MAX_PAID_AMOUNT:
            return {"status": "invalid", "message": "實收金額看起來不合理，請確認"}
    # 只有固定月費、還沒有帳單列的人也要記得到實收，跟 /finance/confirm 同一個理由
    if not db.get_dues(club, month, uid):
        db.upsert_dues(club, month, uid, 0, 0, [])
    db.set_dues_paid_amount(club, month, uid, amount)
    return {"status": "ok", "month": month, "uid": uid, "paid_amount": amount}


# ── 社友名冊（財務看板的「社友名冊」） ─────────────────────────────────────────
# 名冊以前只能由社友自己填第一次進 LIFF 的那張表長出來，沒有任何地方刪得掉。
# 結果是退社的人年年出現在應收名單裡，而還沒加官方帳號的新社友則根本不存在，
# 執秘想幫他記帳也記不了。這三支讓執秘自己維護自己社的名冊。

def _is_line_bound(uid: str) -> bool:
    """LINE 的 user id 是 U + 32 位十六進位；其餘是手動建立或匯入的佔位 id。"""
    return len(uid) == 33 and uid.startswith("U")


@app.get("/finance/roster")
async def finance_roster(request: Request, club: str = "", month: str = ""):
    """執秘自己社的名冊，附上刪除前該知道的事：有沒有綁 LINE、有幾個月的帳單。

    會籍類別也在這裡指派，所以要帶 month —— 「這個人現在是哪一類」問的其實是
    「他在這個月是哪一類」，指派是分月段的。"""
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        raise HTTPException(status_code=403, detail="無社友名冊管理權限")
    club = _target_club(admin_uid, club)
    if not club:
        return {"status": "no_club", "message": "找不到您的社別"}
    month = month if _valid_month(month) else _this_month()
    dues_months = db.dues_month_counts(club)
    book = _tier_book(club)
    rb = _rates_for_month(club, month, book)
    # 這一位的指派是從哪個月起生效的：畫面要寫「B（2026-09 起）」，否則執秘看不出
    # 眼前這個類別是本來就這樣，還是他上禮拜才改的。
    tier_from: dict[str, str] = {}
    for m in book["members"]:
        if m["effective_month"] <= month and m["line_user_id"] not in tier_from:
            tier_from[m["line_user_id"]] = m["effective_month"]
    members = []
    for m in db.club_member_rows(club):
        uid = m["line_user_id"]
        members.append({
            "uid": uid,
            "name": m.get("full_name") or "",
            "nickname": m.get("nickname") or "",
            "diet_type": m.get("diet_type") or "",
            "line_bound": _is_line_bound(uid),
            "dues_months": dues_months.get(uid, 0),
            "tier": rb["tier_of"].get(uid, ""),
            "tier_from": tier_from.get(uid, ""),
            "base": _member_rates(rb, uid)[0],
            "extras_total": _items_total(_member_rates(rb, uid)[2]),
        })
    return {"status": "ok", "club": club, "month": month, "members": members,
            "tiers": _tiers_payload(club, month),
            "diet_types": list(_DIET_TYPES)}


@app.post("/finance/roster/add")
async def finance_roster_add(request: Request):
    """新增一位社友到執秘自己的社。"""
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "message": "無社友名冊管理權限"}
    body = await request.json()
    club = _target_club(admin_uid, str(body.get("club", "")).strip())
    if not club:
        return {"status": "no_club", "message": "找不到您的社別"}
    name = str(body.get("full_name", "")).strip()
    nickname = str(body.get("nickname", "")).strip()
    diet = str(body.get("diet_type", "")).strip() or _DIET_TYPES[0]
    if not name:
        return {"status": "invalid", "message": "請填社友姓名"}
    if len(name) > 40 or len(nickname) > 40:
        return {"status": "invalid", "message": "姓名／Nickname 請勿超過 40 字"}
    if diet not in _DIET_TYPES:
        return {"status": "invalid", "message": "飲食習慣選擇不正確"}
    # 同名的人在一個社裡不是不可能，但多半是重複新增。擋下來，真的有同名就
    # 用 Nickname 區分。
    if any((m.get("full_name") or "").strip() == name for m in db.club_member_rows(club)):
        return {"status": "duplicate", "message": f"名冊裡已經有「{name}」了"}
    # 同名的退社社友：接回原本那一列，而不是給他一個新身分。退社前欠的那幾個月
    # 本來就掛在舊的 id 上，開新的一列等於把那筆錢留在一個沒有人的名字底下。
    left = db.find_left_member(club, name)
    if left:
        db.restore_club_member(club, left["line_user_id"], nickname, diet)
        _assign_new_member_tier(club, left["line_user_id"], body)
        return {"status": "ok", "uid": left["line_user_id"], "name": name, "restored": True}
    uid = db.create_club_member(club, name, nickname, diet)
    _assign_new_member_tier(club, uid, body)
    return {"status": "ok", "uid": uid, "name": name, "restored": False}


def _assign_new_member_tier(club: str, uid: str, body: dict) -> None:
    """入社就順手歸類。沒指定類別就不寫 —— 不寫等於走全社預設，跟以前一樣。"""
    tier = _clean_tier(body.get("tier"))
    if not tier:
        return
    month = str(body.get("effective_month", "")).strip()
    db.save_member_tier(club, uid, month if _valid_month(month) else _this_month(), tier)


@app.post("/finance/roster/delete")
async def finance_roster_delete(request: Request):
    """把一位社友移出名冊（標記退社）；帳單與報名紀錄留著，欠的錢還是要收。"""
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "message": "無社友名冊管理權限"}
    body = await request.json()
    club = _target_club(admin_uid, str(body.get("club", "")).strip())
    if not club:
        return {"status": "no_club", "message": "找不到您的社別"}
    uid = str(body.get("uid", "")).strip()
    if not uid:
        return {"status": "invalid", "message": "缺少社友"}
    if uid == admin_uid:
        return {"status": "invalid", "message": "不能把自己移出名冊"}
    if not db.leave_club_member(club, uid):
        return {"status": "not_found", "message": "名冊裡找不到這位社友"}
    return {"status": "ok", "uid": uid}


@app.get("/finance/trend")
async def finance_trend(request: Request, months: int = 6, end: str = "", club: str = ""):
    """收入／支出／結餘 for the last N months — the run of numbers a treasurer reads
    to spot the month collection slipped, which a single month can never show."""
    uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(uid):
        raise HTTPException(status_code=403, detail="無財務管理權限")
    club = _target_club(uid, club)
    if not club:
        return {"status": "no_club", "message": "找不到您的社別"}
    members = db.get_club_members(club)     # 社友名單每個月都一樣，撈一次就好
    book = _tier_book(club)                 # 會籍類別也是，別在月份迴圈裡各查一次
    advance_by_month = _advance_ledger(club)["by_month"]
    window = _month_list(max(1, min(int(months or 6), 24)), end)
    # 溢繳是逐月折疊出來的：某個月能抵多少，要看它之前發生過什麼。所以從有紀錄的
    # 第一個月開始跑，只吐出要顯示的那幾個月 —— 直接從視窗第一個月起算的話，趨勢
    # 圖的「收」會跟看板的本月結餘對不起來。
    active = sorted(set(db.finance_months(club)) | set(advance_by_month))
    earlier = (_months_between(active[0], window[0])
               if active and window and active[0] < window[0] else [])
    shown = set(window)
    credit: dict[str, int] = {}
    out = []
    for m in earlier + window:
        rows = _dues_rows(club, m, members, book)
        credit = _settle_month(rows, credit)
        if m not in shown:
            continue                    # 只是為了把溢繳帶過來，不畫這一個月
        totals = _dues_totals(rows)
        expense = _expense_summary(db.get_club_finance(club, m), advance_by_month.get(m, 0))
        out.append({"month": m, "expected": totals["expected"],
                    # received 一直叫這個名字，現在它才真的是「收到的錢」
                    "received": totals["cash"], "expense": expense["total"],
                    "net": totals["cash"] - expense["total"]})
    return {"status": "ok", "club": club, "months": out}


@app.get("/events")
async def events(request: Request, scope: str = "", district: str = "", club: str = ""):
    """Single source of truth for the LIFF's event list (district or club scope).

    district／club 只有跨地區管理員指定得動 —— 他要能一區一區、一社一社地看。
    其餘人傳什麼都以自己的地區與社為準，跟 _target_club 同一個規矩。"""
    uid = request.headers.get("X-Line-UserId", "")
    if scope not in ("district", "club"):
        scope = db.get_user_scope(uid) if uid else "district"
    everywhere = bool(uid) and db.is_super_admin(uid)
    target_club = db.get_user_club(uid) if uid else ""
    target_district = _visible_district(uid) if uid else ""
    if everywhere:
        if scope == "club" and club:
            # 看別的社時，地區跟著那個社走，否則會拿自己的地區去濾別區的社
            target_club = club
            target_district = db.get_club_district(club) or None
        elif scope == "district":
            # 指定了就看那一區；沒指定維持 None＝全部地區一起看
            target_district = district.strip() or None
    evs = _events_for_scope(scope, target_club, target_district)
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
        # （notices 同步時存進 pdf_url），議程 PDF 是議程編輯器產生的。以前只有一個 pdf_url，
        # 有議程就把公文連結蓋掉，公文本文就再也點不到了。
        # 公文優先給「本文那一份 PDF」的直接連結：pdf_url 是地區網站給的 Drive 資料夾，
        # 點開來是一堆檔案（公文、報名表、附件），社友還要自己認哪一份才是公文。
        # 解析不出來（新同步的、非公開資料夾）才退回資料夾。
        notice_url = e.get("notice_file_url") or e.get("pdf_url") or ""
        return {**e,
                "notice_pdf_url": notice_url,
                "agenda_pdf_url": agenda_url,
                "pdf_url": agenda_url or notice_url}   # 舊前端只認得這個

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
    # 地區不由前端決定：活動一律建在建立者自己的地區底下，否則一個打錯的欄位
    # 就會讓活動出現在別的地區的行事曆上。跨地區管理員是例外 —— 他本來就管所有
    # 地區，而且沒有這個例外的話，他自己那一區以外的地區永遠不會有活動。
    data["district"] = _event_district(uid, data.get("district"))
    return {"status": "ok", "event": db.create_event(data)}


def _event_district(uid: str, requested, current: str = "") -> str:
    """活動要掛在哪一個地區。

    一般管理員只有自己那一區，前端傳什麼都不算數；跨地區管理員可以指定，但仍然
    要是系統裡真的存在的地區 —— 打錯一個代碼，那筆活動會掉進沒有人看得到的地方。"""
    own = current or db.get_user_district(uid)
    code = str(requested or "").strip()
    if not code or not db.is_super_admin(uid):
        return own
    return code if db.get_district(code) else own


def _same_district_event(uid: str, event_id: int) -> dict | None:
    """要改的活動必須跟操作者同地區，否則當成不存在。

    回 404 而不是 403 是刻意的：別的地區有沒有這個活動，本來就不干他的事。"""
    ev = db.get_event(event_id)
    if ev is None:
        return None
    if db.is_super_admin(uid):
        return ev
    if (ev.get("district") or db.DEFAULT_DISTRICT) != db.get_user_district(uid):
        return None
    return ev


@app.put("/admin/events/{event_id}")
async def admin_update_event(event_id: int, request: Request):
    """執秘 從管理面板修改活動。"""
    uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(uid):
        raise HTTPException(status_code=403, detail="Not an admin")
    current = _same_district_event(uid, event_id)
    if current is None:
        raise HTTPException(status_code=404, detail="Event not found")
    data = _clean_event_payload(await request.json())
    # 一般管理員改不動地區（活動不能被搬到別的地區）；跨地區管理員可以，
    # 因為建錯地區的活動總得有人搬得回去。
    if "district" in data:
        data["district"] = _event_district(uid, data["district"], current=current["district"])
    ev = db.update_event(event_id, data)
    if ev is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return {"status": "ok", "event": ev}


@app.delete("/admin/events/{event_id}")
async def admin_delete_event(event_id: int, request: Request):
    """執秘 從管理面板刪除活動。"""
    uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(uid):
        raise HTTPException(status_code=403, detail="Not an admin")
    if _same_district_event(uid, event_id) is None:
        raise HTTPException(status_code=404, detail="Event not found")
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
        d = db.get_district(db.DEFAULT_DISTRICT) or {}
        return {"status": "no_user", "is_admin": False, "role": "member", "district": d}
    return {
        "status": "ok",
        "role": db.get_user_role(uid),
        "scope": db.get_user_scope(uid),
        "club": db.get_user_club(uid),
        "is_admin": db.is_admin(uid),
        "name": _member_name(uid),
        # 還沒填完基本資料的話，LIFF 一開就先請本人補（見 openProfileGate）。
        "needs_profile": _profile_incomplete(_member_profile(uid)),
        # 前端的標題、地區網站連結都跟著這一包走，不再各自寫死 3523
        "district": _district_of(uid),
        # 跨地區管理員的畫面要多一個社／地區的切換器（見 finance.html 的社別選單）
        "all_districts": db.is_super_admin(uid),
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
async def club_finance_get(request: Request, month: str = "", club: str = ""):
    """Load a club's monthly finance sheet (admin). Defaults to the caller's club
    and the current month; returns empty defaults when nothing is saved yet.

    club 跟財務看板同一個規矩（_target_club）：只有跨地區管理員指定得動別的社。"""
    uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(uid):
        raise HTTPException(status_code=403, detail="Not an admin")
    club = _target_club(uid, club)
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
    body = await request.json()
    # 跨地區管理員切到別社時，支出要寫進他正在看的那個社，不是他自己的
    club = _target_club(uid, str(body.get("club", "")).strip())
    if not club:
        return {"status": "no_club", "message": "找不到您的社別"}
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


# ── 社刊抬頭 ──────────────────────────────────────────────────────────────────
# 社名、期數、日期以前是寫死在版型裡的示範值（台北信義扶輪社／第 2489 期／
# 2026-07-04），每一份社刊都要主委自己改，改漏了就掛著別人的社名發出去。

# 例會名稱的寫法社社不同，實際資料裡就有「第1350次例會」「本社 第 1234 次例會」
# 「第1236例會」「1237例會」四種。認的是「緊貼著例會的那個數字」，而不是標題裡
# 任何一個數字 ——「函請各社踴躍報名2026年地區年會聯合例會」的 2026 離「例會」隔著
# 一整段字，不該被當成期數。抓不到就留白讓主委自己填。
_ISSUE_PATTERNS = (
    r"第\s*(\d{1,5})\s*[次期]",          # 第1350次 / 第 1234 次 / 第 88 期
    r"(\d{1,5})\s*[次期]?\s*例會",       # 第1236例會 / 1237例會
)


def _issue_no(title: str) -> str:
    """從例會名稱抓期數，抓不到回空字串。"""
    text = str(title or "")
    for pattern in _ISSUE_PATTERNS:
        m = re.search(pattern, text)
        if m:
            return m.group(1)
    return ""


@app.get("/bulletin/header")
async def bulletin_header(request: Request, event: int | None = None):
    """社刊抬頭要帶入的值：社名、期數、例會日期。

    社名取完整名稱（沒設定就用簡稱）。沒帶 event 就只回社名，其餘留空。

    地點／司儀／開始時間不是抬頭要印的，是社刊編輯器裡「例會資料」那一格要填的
    ——例會的這幾個欄位以前只能在行事曆改，社刊得另外開一個分頁。多回這三個欄位
    就不必為了三個字再開一支端點；社友在活動卡上本來就看得到，沒有多露出什麼。"""
    uid = request.headers.get("X-Line-UserId", "")
    club = db.get_user_club(uid) if uid else ""
    ev = _lookup_event(uid, int(event)) if event else None
    # 社內活動掛在自己社底下；地區活動沒有社，抬頭仍用開啟者的社
    if ev and ev.get("club_name"):
        club = ev["club_name"]
    return {
        "status": "ok",
        "club": club,
        "club_full_name": db.get_club_full_name(club) if club else "",
        "issue_no": _issue_no(ev["title"]) if ev else "",
        "date": (ev or {}).get("date", ""),
        "title": (ev or {}).get("title", ""),
        "location": (ev or {}).get("location", ""),
        "mc": (ev or {}).get("mc", ""),
        # 能不能改由後端說了算：PUT /admin/events/{id} 認的是 is_admin，而社刊主委
        # 不一定是執秘（bulletin_editors 與 admin 是兩份名單）。前端自己猜的話，
        # 會長出一顆按下去才發現是 403 的按鈕。
        "can_edit_event": bool(ev) and db.is_admin(uid),
    }


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
    # 指名了活動就只能是那一場。以前查不到會靜靜地退回「使用者當前的活動」，
    # 有了第二個地區之後那是實害：點 3481 的活動送出，人卻被報進自己這區的下一場。
    ev = _lookup_event(uid, int(event_id)) if event_id else _current_event(uid)
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
                 else "\n完成匯款後請至「個人中心 → 提供匯款帳號」補填末 5 碼。")
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
    # 同上：指名的活動看不到就是看不到，不要改端出另一場的報名名單。
    ev = _lookup_event(admin_uid, int(event)) if event else _current_event(admin_uid)
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
            f"費用：{ev['fee']}\n\n完成匯款後請至 LIFF「個人中心 → 提供匯款帳號」填寫帳號末 5 碼，"
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


# ── 地區管理 ──────────────────────────────────────────────────────────────────
# 新增地區、設定它的名稱／網站／公文來源、把社指派給地區。這些以前都不存在，
# 因為只有一個地區；現在不給介面的話，每加一個社就要有人進資料庫下 SQL。

@app.get("/admin/districts")
async def admin_districts(request: Request):
    """地區清單與各自的社。只有 admin_all 看得到全部 —— 社幹部沒有理由知道別的
    地區有哪些社。"""
    uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(uid):
        raise HTTPException(status_code=403, detail="無地區管理權限")
    mine = db.get_user_district(uid)
    everything = db.get_user_role(uid) == "admin_all"
    districts = db.list_districts()
    if not everything:
        districts = [d for d in districts if d["code"] == mine]
    by_district = {}
    for c in db.list_clubs_with_district():
        # 社刊抬頭的全名也一起給，地區管理那個畫面就是維護它的地方
        by_district.setdefault(c["district"], []).append(
            {"name": c["club_name"], "full_name": c.get("full_name") or ""})
    return {"status": "ok", "my_district": mine,
            "districts": [{**d, "clubs": by_district.get(d["code"], [])} for d in districts]}


@app.post("/admin/districts")
async def admin_district_save(request: Request):
    """建立或修改一個地區。code 建立後不可改：活動、社、角色全都指向它。"""
    uid = request.headers.get("X-Line-UserId", "")
    if db.get_user_role(uid) != "admin_all":
        return {"status": "forbidden", "message": "只有最高管理員可以維護地區"}
    body = await request.json()
    code = str(body.get("code", "")).strip()
    if not code.isdigit() or not (3 <= len(code) <= 5):
        return {"status": "invalid", "message": "地區代碼需為 3-5 位數字，例如 3481"}
    name = str(body.get("name", "")).strip() or f"國際扶輪 {code} 地區"
    short = str(body.get("short_name", "")).strip() or f"{code} 地區"
    website = str(body.get("website", "")).strip()
    notices_api = str(body.get("notices_api", "")).strip()
    email = str(body.get("contact_email", "")).strip()
    if db.get_district(code):
        db.update_district(code, name, short, website, notices_api, email)
        return {"status": "ok", "code": code, "created": False}
    db.seed_district(code, name, short, website, notices_api, email)
    return {"status": "ok", "code": code, "created": True}


@app.post("/admin/districts/club")
async def admin_district_set_club(request: Request):
    """把一個社指派到某個地區。

    社友只填社名，社屬於哪個地區是社的屬性 —— 改了之後那個社的所有社友、活動、
    帳單一起換地區，所以只開放給最高管理員。"""
    uid = request.headers.get("X-Line-UserId", "")
    if db.get_user_role(uid) != "admin_all":
        return {"status": "forbidden", "message": "只有最高管理員可以調整社的地區"}
    body = await request.json()
    club = str(body.get("club", "")).strip()
    district = str(body.get("district", "")).strip()
    if not club:
        return {"status": "invalid", "message": "缺少社名"}
    if not db.get_district(district):
        return {"status": "invalid", "message": f"地區 {district} 不存在"}
    db.set_club_district(club, district)
    # 完整社名（社刊抬頭用）順便在這裡維護；沒帶就不動它
    if "full_name" in body:
        db.set_club_full_name(club, str(body.get("full_name", "")).strip())
    return {"status": "ok", "club": club, "district": district}


@app.get("/admin/clubs")
async def admin_clubs(request: Request):
    """Club dropdown + members for the exec-secretary bulk-register form (admin only).
    只列自己地區的社 —— 3481 的執秘在批次報名時不該挑得到 3523 的社。"""
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "clubs": []}
    if db.is_super_admin(admin_uid):
        return {"status": "ok", "district": "", "all_districts": True,
                "clubs": [c["club_name"] for c in db.all_clubs()]}
    district = db.get_user_district(admin_uid)
    return {"status": "ok", "district": district, "clubs": db.list_clubs(district)}


@app.get("/admin/club_members")
async def admin_club_members(request: Request, club: str = ""):
    """某個社的名冊。club 是呼叫端給的，所以要驗它跟管理員同地區：這支端點本來
    誰都能查任何一個社，多了第二個地區之後那就是跨地區的個資外洩。"""
    admin_uid = request.headers.get("X-Line-UserId", "")
    if not db.is_admin(admin_uid):
        return {"status": "forbidden", "members": []}
    if (club and not db.is_super_admin(admin_uid)
            and db.get_club_district(club) != db.get_user_district(admin_uid)):
        return {"status": "forbidden", "message": "無法查詢其他地區的社友名冊", "members": []}
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

    # 要報名的人也必須是同地區的。活動已由 _lookup_event 把關，但 uids 是呼叫端
    # 給的，不驗的話一次批次就能把別區的社友塞進本區的名單。
    district = db.get_user_district(admin_uid)
    outsiders = ([] if db.is_super_admin(admin_uid)
                 else [u for u in uids if db.get_user_district(u) != district])
    if outsiders:
        return {"status": "forbidden",
                "message": f"名單中有 {len(outsiders)} 位不屬於本地區的社友，無法代為報名"}

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
