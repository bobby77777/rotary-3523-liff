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
    yield


app = FastAPI(lifespan=lifespan)


def _verify_line_signature(body: bytes, signature: str) -> bool:
    digest = hmac.new(LINE_CHANNEL_SECRET.encode(), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature)


def _handle_event(event: dict[str, Any]) -> None:
    event_type = event.get("type")
    source = event.get("source", {})
    user_id = source.get("userId", "")
    reply_token = event.get("replyToken", "")

    if event_type == "postback":
        data = event.get("postback", {}).get("data", "")
        if data == "action=rotary_knowledge":
            form_url = f"{APP_BASE_URL}/form/sign?line_user_id={user_id}"
            line_api.reply_registration_button(reply_token, form_url)
        return

    if event_type != "message":
        return

    message = event.get("message", {})
    if message.get("type") != "text":
        line_api.reply_text(reply_token, "抱歉，我只能處理文字訊息。")
        return

    text = message.get("text", "").strip()
    if not text:
        return

    try:
        line_api.send_loading(user_id)
        reply = run_agent(user_id, text)
        line_api.reply_text(reply_token, reply)
    except Exception:
        logger.exception("Agent error for user %s", user_id)
        line_api.push_text(user_id, "抱歉，系統出現錯誤")


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
