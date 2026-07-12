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


def get_menu_id_by_alias(alias_id: str) -> str:
    resp = requests.get(f"{_BASE}/richmenu/alias/{alias_id}", headers=_HEADERS, timeout=10)
    return resp.json().get("richMenuId", "")


def link_rich_menu(user_id: str, rich_menu_id: str) -> None:
    import logging
    resp = requests.post(
        f"{_BASE}/user/{user_id}/richmenu/{rich_menu_id}",
        headers=_HEADERS,
        timeout=10,
    )
    logging.getLogger(__name__).info(
        "link_rich_menu: status=%d body=%s", resp.status_code, resp.text[:200]
    )


def reply_flex(
    reply_token: str,
    alt_text: str,
    contents: dict,
    quick_replies: list | None = None,
) -> None:
    import logging
    msg: dict = {"type": "flex", "altText": alt_text, "contents": contents}
    if quick_replies:
        msg["quickReply"] = {"items": quick_replies}
    resp = requests.post(
        f"{_BASE}/message/reply",
        headers=_HEADERS,
        json={"replyToken": reply_token, "messages": [msg]},
        timeout=10,
    )
    if not resp.ok:
        logging.getLogger(__name__).error(
            "reply_flex failed: status=%d body=%s", resp.status_code, resp.text[:500]
        )


def reply_text_with_quick_reply(reply_token: str, text: str, items: list) -> None:
    requests.post(
        f"{_BASE}/message/reply",
        headers=_HEADERS,
        json={
            "replyToken": reply_token,
            "messages": [
                {
                    "type": "text",
                    "text": text,
                    "quickReply": {"items": items},
                }
            ],
        },
        timeout=10,
    )


def push_flex(user_id: str, alt_text: str, contents: dict) -> None:
    requests.post(
        f"{_BASE}/message/push",
        headers=_HEADERS,
        json={
            "to": user_id,
            "messages": [{"type": "flex", "altText": alt_text, "contents": contents}],
        },
        timeout=10,
    )


def multicast(user_ids: list[str], text: str) -> None:
    if not user_ids:
        return
    for i in range(0, len(user_ids), 500):
        batch = user_ids[i : i + 500]
        requests.post(
            f"{_BASE}/message/multicast",
            headers=_HEADERS,
            json={
                "to": batch,
                "messages": [{"type": "text", "text": text}],
            },
            timeout=30,
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
