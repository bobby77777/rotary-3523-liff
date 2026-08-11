"""Sync 【公文】 (official-notice) posts from the district website into the calendar.

The district publishes every notice as a WordPress post under the 地區活動
(/category/events/) section; the ones whose title starts with 【公文】 are the
official letters (聯席會、研習會、就職典禮、年會…). This module pulls those posts
through the WordPress REST API, reads the real event date/location/fee out of the
PDF each post links to on Google Drive, and files them into the district calendar
as type="公文" events.

Everything degrades gracefully: the WP fetch is the only hard dependency. If Drive
credentials or the LLM are unavailable, the notice is still added to the calendar
using the post's publish date as a stand-in, so nothing silently disappears — the
date just gets refined once the PDF can be read.

Idempotency: each event stores the post URL in events.source_url, and a re-sync
skips URLs already present, so it is safe to run on every startup and on demand.
"""
import io
import re
import json
import html
import time
import logging

import requests

from . import db, event_pdfs
from .config import OPENAI_API_KEY

logger = logging.getLogger(__name__)

WP_BASE = "https://ri3523.org/wp-json/wp/v2"
_EVENTS_CATEGORY_SLUG = "events"     # 地區活動 archive shown at /category/events/
_UA = {"User-Agent": "rotary-3523-liff notices sync"}
_HTTP_TIMEOUT = 20


# ── WordPress REST ────────────────────────────────────────────────────────────
def _get(path: str, params: dict) -> list | dict | None:
    try:
        r = requests.get(f"{WP_BASE}/{path}", params=params,
                         headers=_UA, timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning("notices: WP GET %s failed: %s", path, e)
        return None


def _events_category_ids() -> list[int]:
    """The 地區活動 archive aggregates its child categories (研習會、聯席會…), so we
    collect the parent plus every child — that mirrors what /category/events/ shows."""
    cats = _get("categories", {"slug": _EVENTS_CATEGORY_SLUG, "_fields": "id"})
    if not isinstance(cats, list) or not cats:
        return []
    parent = cats[0]["id"]
    ids = [parent]
    children = _get("categories", {"parent": parent, "per_page": 100, "_fields": "id"})
    if isinstance(children, list):
        ids += [c["id"] for c in children]
    return ids


def _clean_text(raw: str) -> str:
    """Strip HTML tags/entities from a rendered WP field down to plain text."""
    return html.unescape(re.sub(r"<[^>]+>", " ", raw or "")).strip()


def _drive_url(content_html: str) -> str:
    """The first Google Drive link in the post body — that's the notice's PDF/folder."""
    for href in re.findall(r'href="([^"]+)"', content_html or ""):
        if "drive.google.com" in href or "docs.google.com" in href:
            return href
    return ""


def fetch_notice_posts() -> list[dict]:
    """Every 【公文】 post in the events archive, newest first."""
    cat_ids = _events_category_ids()
    if not cat_ids:
        return []
    posts: list[dict] = []
    page = 1
    while True:
        batch = _get("posts", {
            "categories": ",".join(map(str, cat_ids)),
            "per_page": 100, "page": page,
            "_fields": "id,date,link,title,content",
        })
        if not isinstance(batch, list) or not batch:
            break
        for p in batch:
            title = _clean_text(p.get("title", {}).get("rendered", ""))
            if "公文" not in title:
                continue
            content = p.get("content", {}).get("rendered", "")
            posts.append({
                "wp_id": p.get("id"),
                "title": title,
                "source_url": p.get("link", ""),
                "publish_date": (p.get("date") or "")[:10],   # YYYY-MM-DD
                "drive_url": _drive_url(content),
            })
        if len(batch) < 100:
            break
        page += 1
    return posts


# ── Google Drive PDF → text ───────────────────────────────────────────────────
def _drive_folder_id(url: str) -> str | None:
    m = re.search(r"/folders/([\w-]+)", url) or re.search(r"[?&]id=([\w-]+)", url)
    return m.group(1) if m else None


def _drive_file_id(url: str) -> str | None:
    m = re.search(r"/file/d/([\w-]+)", url)
    return m.group(1) if m else None


def _notice_pdf_bytes(drive_url: str) -> bytes | None:
    """Resolve the notice's Drive link to PDF bytes — the link is usually a shared
    folder (list it, take the first PDF) but may point straight at a file."""
    svc = event_pdfs._drive_service()
    if svc is None or not drive_url:
        return None
    file_id = _drive_file_id(drive_url)
    if not file_id:
        folder_id = _drive_folder_id(drive_url)
        if not folder_id:
            return None
        try:
            resp = svc.files().list(
                q=(f"'{folder_id}' in parents and trashed=false "
                   f"and mimeType='application/pdf'"),
                fields="files(id, name)", pageSize=10, orderBy="name",
            ).execute()
            files = resp.get("files", [])
        except Exception as e:
            logger.warning("notices: listing Drive folder %s failed: %s", folder_id, e)
            return None
        if not files:
            return None
        file_id = files[0]["id"]
    return event_pdfs.download_pdf(file_id)


def _pdf_text(data: bytes, max_pages: int = 4) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages[:max_pages])
    except Exception as e:
        logger.warning("notices: PDF text extraction failed: %s", e)
        return ""


# ── LLM: pull the real event date out of the notice ───────────────────────────
_EXTRACT_PROMPT = """你是扶輪地區秘書處的助理。以下是一份地區公文的標題與內文，請判斷這份公文是否
在「通知一場有明確日期的活動」（例如研習會、聯席會、就職典禮、比賽、年會、餐敘），
並抽取活動資訊。只根據內文，不得臆測。

規則：
- 純粹公告、通知結果、催繳費用、提供名單這類「沒有要出席的活動」→ is_event=false。
- 日期一律用西元 YYYY-MM-DD。看到民國年請換算（民國115年=西元2026年）。
- 找不到的欄位留空字串。時間用 24 小時制範圍（例：13:30-17:00），整天可寫「整天」。
- 費用照原文（例：NT$500、免費），沒寫就留空。

只輸出 JSON，格式：
{"is_event": true/false, "date": "", "location": "", "time": "", "fee": ""}"""

_llm = None


def _llm_client():
    global _llm
    if not OPENAI_API_KEY:
        return None
    if _llm is None:
        from openai import OpenAI
        _llm = OpenAI(api_key=OPENAI_API_KEY)
    return _llm


def _extract_event_details(title: str, pdf_text: str, publish_date: str) -> dict:
    """Best-effort {is_event, date, location, time, fee}; {} if it can't be read."""
    client = _llm_client()
    if client is None or not pdf_text.strip():
        return {}
    user = (f"公文標題：{title}\n公文張貼日：{publish_date}\n"
            f"公文內文：\n{pdf_text[:6000]}")
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": _EXTRACT_PROMPT},
                      {"role": "user", "content": user}],
            response_format={"type": "json_object"},
            temperature=0,
        )
        data = json.loads(resp.choices[0].message.content or "{}")
    except Exception as e:
        logger.warning("notices: LLM extraction failed for %r: %s", title, e)
        return {}
    date = str(data.get("date") or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):   # trust only a clean ISO date
        date = ""
    return {
        "is_event": bool(data.get("is_event")),
        "date": date,
        "location": str(data.get("location") or "").strip(),
        "time": str(data.get("time") or "").strip(),
        "fee": str(data.get("fee") or "").strip(),
    }


# ── Orchestration ─────────────────────────────────────────────────────────────
def _build_event(post: dict) -> dict:
    """Map a notice post (+ whatever we could read from its PDF) to an event row."""
    details = {}
    if post.get("drive_url"):
        pdf = _notice_pdf_bytes(post["drive_url"])
        if pdf:
            details = _extract_event_details(
                post["title"], _pdf_text(pdf), post["publish_date"])
    # Real event date when we could read it; otherwise fall back to the publish date
    # so the notice still lands on the calendar rather than vanishing.
    return {
        "scope": "district",
        "type": "公文",
        "title": post["title"],
        "date": details.get("date") or post["publish_date"] or None,
        "location": details.get("location", ""),
        "time": details.get("time", ""),
        "fee": details.get("fee", ""),
        "pdf_url": post.get("drive_url", ""),
        "source_url": post["source_url"],
    }


def sync_notices() -> dict:
    """Pull 【公文】 posts and add any not already synced. Returns a summary report."""
    posts = fetch_notice_posts()
    if not posts:
        return {"found": 0, "added": 0, "skipped": 0, "errors": 0}
    known = db.event_source_urls()
    added = skipped = errors = 0
    for post in posts:
        if not post.get("source_url") or post["source_url"] in known:
            skipped += 1
            continue
        try:
            db.create_event(_build_event(post))
            added += 1
        except Exception as e:
            errors += 1
            logger.warning("notices: failed to add %r: %s", post.get("title"), e)
    report = {"found": len(posts), "added": added,
              "skipped": skipped, "errors": errors}
    logger.info("notices: sync done %s", report)
    return report


def sync_notices_safe() -> dict:
    """sync_notices() that never raises — for fire-and-forget background use."""
    try:
        return sync_notices()
    except Exception as e:
        logger.warning("notices: sync aborted: %s", e)
        return {"found": 0, "added": 0, "skipped": 0, "errors": 0}


SYNC_INTERVAL_SECONDS = 6 * 60 * 60   # re-check the site for new 公文 every 6 hours


def run_periodic(interval: float = SYNC_INTERVAL_SECONDS) -> None:
    """Sync once now, then every `interval` seconds. Runs forever — meant to be the
    target of a daemon thread started at app startup. Since there is no scheduler in
    this backend, this is the whole cron: it lives as long as the process does, and
    each pass only touches 公文 not already in the calendar."""
    while True:
        sync_notices_safe()
        time.sleep(interval)
