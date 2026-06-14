import hashlib
import hmac
import base64
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from . import db, line_api
from .agent import run_agent
from .config import APP_BASE_URL, LINE_CHANNEL_SECRET

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.ensure_message_store()
    for data_key, alias in _TAB_ALIASES.items():
        menu_id = line_api.get_menu_id_by_alias(alias)
        if menu_id:
            _TAB_MENU_IDS[data_key] = menu_id
            logger.info("Cached tab menu: %s → %s", alias, menu_id)
        else:
            logger.warning("Could not resolve alias at startup: %s", alias)
    yield


app = FastAPI(lifespan=lifespan)


def _verify_line_signature(body: bytes, signature: str) -> bool:
    digest = hmac.new(LINE_CHANNEL_SECRET.encode(), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature)


_EVENT_SCHEDULE = [
    {"id": 101, "date": "2024-11-22", "weekday": "星期五", "title": "RF GMS 扶輪獎助金管理研討會",       "location": "政大公企中心 2F",         "chair": "蔡輝彬 P.P. Stanley", "time": "13:30-17:30", "type": "地區會議"},
    {"id": 102, "date": "2025-02-25", "weekday": "星期二", "title": "DTTS 地區團隊訓練研討會",           "location": "漢來飯店",                 "chair": "王維宏 P.P. JoeWang", "time": "13:00-17:00", "type": "訓練研討"},
    {"id": 103, "date": "2025-03-22", "weekday": "星期六", "title": "PETS 社長當選人訓練研討會",         "location": "美福飯店",                 "chair": "許顥譪 P.P. Anthony", "time": "10:00-16:30", "type": "訓練研討"},
    {"id": 104, "date": "2025-05-24", "weekday": "星期六", "title": "DTA 地區訓練講習會 (合併 CTTS)",   "location": "大直典華",                 "chair": "蔡圻 P.P. Chigo",    "time": "10:00-16:30", "type": "訓練研討"},
    {"id": 105, "date": "2025-07-01", "weekday": "星期二", "title": "總監暨社長聯合就職典禮",           "location": "漢來飯店",                 "chair": "蔡圻 P.P. Chigo",    "time": "11:00-14:00", "type": "年度慶典"},
    {"id": 106, "date": "2025-07-14", "weekday": "星期一", "title": "總監盃高爾夫球比賽",               "location": "老淡水高爾夫球場",         "chair": "林星煌 P.P. Star",    "time": "整天",        "type": "地區運動"},
    {"id": 107, "date": "2026-06-15", "weekday": "星期一", "title": "地區青少年交換(RYE)講習會",        "location": "台北福華大飯店",           "chair": "陳俊宇 P.P. RYE",    "time": "10:00-15:00", "type": "講習培訓"},
    {"id": 108, "date": "2026-10-24", "weekday": "星期六", "title": "第九屆地區年會暨職業服務論壇",     "location": "台北萬豪酒店 5樓萬豪廳",  "chair": "張秘書長",            "time": "09:00-17:30", "type": "地區年會"},
]


def _build_calendar_carousel() -> dict:
    from datetime import date
    today = date.today().isoformat()

    bubbles = []
    for ev in _EVENT_SCHEDULE:
        is_upcoming = ev["date"] >= today
        badge_color = "#10b981" if is_upcoming else "#9ca3af"
        badge_label = "即將到來" if is_upcoming else "已結束"
        header_bg   = "#1e3a5f" if is_upcoming else "#374151"

        bubble = {
            "type": "bubble",
            "size": "kilo",
            "header": {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": header_bg,
                "paddingAll": "12px",
                "contents": [
                    {
                        "type": "box",
                        "layout": "horizontal",
                        "contents": [
                            {
                                "type": "box",
                                "layout": "vertical",
                                "backgroundColor": badge_color,
                                "cornerRadius": "4px",
                                "paddingAll": "2px",
                                "paddingStart": "6px",
                                "paddingEnd": "6px",
                                "width": "60px",
                                "contents": [
                                    {
                                        "type": "text",
                                        "text": badge_label,
                                        "size": "xxs",
                                        "weight": "bold",
                                        "color": "#ffffff",
                                    }
                                ],
                            },
                            {"type": "filler"},
                            {
                                "type": "text",
                                "text": ev["type"],
                                "size": "xxs",
                                "color": "#ffd700",
                                "align": "end",
                            },
                        ],
                    },
                    {
                        "type": "text",
                        "text": f"{ev['date']} {ev['weekday']}",
                        "color": "#93c5fd",
                        "size": "xxs",
                        "margin": "sm",
                    },
                ],
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "paddingAll": "12px",
                "contents": [
                    {
                        "type": "text",
                        "text": ev["title"],
                        "weight": "bold",
                        "size": "sm",
                        "wrap": True,
                        "color": "#1f2937",
                    },
                    {
                        "type": "text",
                        "text": f"\U0001f4cd {ev['location']}",
                        "size": "xxs",
                        "color": "#6b7280",
                        "wrap": True,
                    },
                    {
                        "type": "text",
                        "text": f"\U0001f464 主委：{ev['chair']}",
                        "size": "xxs",
                        "color": "#6b7280",
                        "wrap": True,
                    },
                    {
                        "type": "text",
                        "text": f"\U0001f550 {ev['time']}",
                        "size": "xxs",
                        "color": "#6b7280",
                    },
                ],
            },
            "footer": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {
                        "type": "button",
                        "action": {
                            "type": "postback",
                            "label": "查看活動詳情",
                            "data": f"action=event_detail&id={ev['id']}",
                        },
                        "style": "primary",
                        "color": "#1e3a5f",
                        "height": "sm",
                    }
                ],
            },
        }
        bubbles.append(bubble)

    return {"type": "carousel", "contents": bubbles}


_TAB_ALIASES = {
    "tab=home":    "richmenu-3523-home",
    "tab=profile": "richmenu-3523-profile",
    "tab=admin":   "richmenu-3523-admin",
}

# alias → real menu_id, resolved once at startup
_TAB_MENU_IDS: dict[str, str] = {}


def _handle_postback(reply_token: str, user_id: str, data: str) -> None:
    logger.info("Postback: user=%s data=%s", user_id, data)
    if data in _TAB_ALIASES:
        menu_id = _TAB_MENU_IDS.get(data)
        logger.info("Tab switch: data=%s menu_id=%s", data, menu_id)
        if menu_id:
            line_api.link_rich_menu(user_id, menu_id)
        else:
            logger.warning("No cached menu_id for tab: %s", data)
        return

    if data == "action=rotary_knowledge":
        form_url = f"{APP_BASE_URL}/form/sign?line_user_id={user_id}"
        line_api.reply_registration_button(reply_token, form_url)
    elif data == "action=banner":
        line_api.reply_text(reply_token,
            "📅 歡迎使用 3523 地區活動報名系統！\n請點選【年度行事曆】查詢近期活動，再點選大螢幕即可報名。")
    elif data == "action=calendar":
        carousel = _build_calendar_carousel()
        line_api.reply_flex(reply_token, "📅 3523 地區年度行事曆", carousel)
    elif data.startswith("action=event_detail&id="):
        try:
            ev_id = int(data.split("id=")[1])
            ev = next((e for e in _EVENT_SCHEDULE if e["id"] == ev_id), None)
        except (ValueError, IndexError):
            ev = None
        if ev:
            line_api.reply_text(
                reply_token,
                f"📅 {ev['title']}\n\n"
                f"日期：{ev['date']}（{ev['weekday']}）\n"
                f"時間：{ev['time']}\n"
                f"地點：{ev['location']}\n"
                f"主委：{ev['chair']}\n"
                f"類型：{ev['type']}\n\n"
                "如需報名請洽地區秘書處 office@rotary3523.org.tw"
            )
    elif data == "action=qrcode":
        line_api.reply_text(reply_token,
            "🎫 您的活動報到 QR Code 將於繳費確認後開通。\n如已完成繳費，請傳送「報到QR」由系統為您產生。")
    elif data == "action=history":
        line_api.reply_text(reply_token,
            "📋 請傳送「查詢報名」，系統將為您顯示目前的報名與繳費紀錄。")
    elif data == "action=upload":
        line_api.reply_text(reply_token,
            "📸 請直接傳送匯款收據截圖至此對話，地區秘書處核對後將為您更新繳費狀態（約 1 個工作天）。")
    elif data == "action=statistics":
        line_api.reply_text(reply_token,
            "📊 【主委專用】即時統計報表功能需經地區秘書處授權後開放。\n如您為活動主委，請聯絡 office@rotary3523.org.tw 申請權限。")
    elif data == "action=scanner":
        line_api.reply_text(reply_token,
            "📷 【主委專用】現場報到掃描功能需主委權限。\n請聯絡地區秘書處開通後方可使用。")
    elif data == "action=support":
        line_api.reply_text(reply_token,
            "☎️ 3523 地區秘書處聯絡資訊\n電話：(02) 2715-XXXX\nEmail：office@rotary3523.org.tw\n\n如有緊急狀況，請直接致電秘書支援專線。")


def _handle_event(event: dict[str, Any]) -> None:
    event_type = event.get("type")
    source = event.get("source", {})
    user_id = source.get("userId", "")
    reply_token = event.get("replyToken", "")

    if event_type == "postback":
        data = event.get("postback", {}).get("data", "")
        _handle_postback(reply_token, user_id, data)
        return

    if event_type != "message":
        return

    message = event.get("message", {})
    if message.get("type") != "text":
        line_api.reply_text(reply_token, "抱歉，我只能處理文字訊息。")
        return

    line_api.reply_text(reply_token, "請使用下方選單按鈕操作 🙏")


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
            {
                "request": request,
                "line_user_id": line_user_id,
                "success": False,
                "error": "請填寫所有欄位",
                "values": values,
            },
        )

    try:
        db.upsert_personal_info(line_user_id, club, full_name, nickname, diet_type)
    except Exception:
        logger.exception("DB upsert failed for user %s", line_user_id)
        return templates.TemplateResponse(
            "form.html",
            {
                "request": request,
                "line_user_id": line_user_id,
                "success": False,
                "error": "儲存失敗，請稍後再試",
                "values": values,
            },
        )

    if line_user_id:
        line_api.push_text(line_user_id, "✅ 您已完成填寫！")

    return templates.TemplateResponse(
        "form.html",
        {
            "request": request,
            "line_user_id": line_user_id,
            "success": True,
            "error": None,
            "values": values,
        },
    )
