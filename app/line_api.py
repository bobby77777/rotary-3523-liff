import requests

from .config import LINE_CHANNEL_ACCESS_TOKEN

_BASE = "https://api.line.me/v2/bot"
_HEADERS = {
    "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    "Content-Type": "application/json",
}


def reply_text(reply_token: str, text: str) -> None:
    requests.post(
        f"{_BASE}/message/reply",
        headers=_HEADERS,
        json={
            "replyToken": reply_token,
            "messages": [{"type": "text", "text": text}],
        },
        timeout=10,
    )


def push_text(user_id: str, text: str) -> None:
    requests.post(
        f"{_BASE}/message/push",
        headers=_HEADERS,
        json={
            "to": user_id,
            "messages": [{"type": "text", "text": text}],
        },
        timeout=10,
    )


def send_loading(user_id: str, seconds: int = 30) -> None:
    requests.post(
        f"{_BASE}/chat/loading/start",
        headers=_HEADERS,
        json={"chatId": user_id, "loadingSeconds": seconds},
        timeout=10,
    )


def reply_registration_button(reply_token: str, form_url: str) -> None:
    requests.post(
        f"{_BASE}/message/reply",
        headers=_HEADERS,
        json={
            "replyToken": reply_token,
            "messages": [
                {
                    "type": "template",
                    "altText": "個人資料",
                    "template": {
                        "type": "buttons",
                        "title": "個人資料填寫",
                        "text": "請點擊下方按鈕填寫個人資料",
                        "actions": [
                            {"type": "uri", "label": "填寫", "uri": form_url}
                        ],
                    },
                }
            ],
        },
        timeout=10,
    )
