"""Sync 【公文】 (official-notice) posts from the district website into the calendar.

The district publishes every notice as a WordPress post under the 最新消息
(/category/news/) section — 公文 is a child category of it; the ones whose title
starts with 【公文】 are the
official letters (聯席會、研習會、就職典禮、年會…). This module pulls those posts
through the WordPress REST API, reads the real event date/location/fee out of the
PDF each post links to on Google Drive, and files them into the district calendar
as type="公文" events.

The PDFs live in world-readable Drive folders, so they are fetched anonymously over
plain HTTP; the Drive API is only a fallback, because its OAuth token expires and a
dead token used to leave every notice detail-less.

Everything degrades gracefully: the WP fetch is the only hard dependency. If the PDF
or the LLM is unavailable, the notice is still added to the calendar using the post's
publish date as a stand-in, so nothing silently disappears — and refresh_notice_details()
fills the real date/location/fee in later, once the PDF can be read.

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
# 公文貼在 最新消息（/category/news/）底下的「公文」子分類。地區活動
# （/category/events/）那邊只剩兩篇舊文，抓那裡等於抓不到新公文。
_NOTICE_CATEGORY_SLUG = "news"       # 最新消息 archive shown at /category/news/
_UA = {"User-Agent": "rotary-3523-liff notices sync"}
# Drive serves the folder page (and its embedded file list) only to browser-ish
# clients, so the anonymous PDF route needs a browser UA.
_DRIVE_UA = {"User-Agent": "Mozilla/5.0 (compatible; rotary-3523-liff notices sync)"}
_HTTP_TIMEOUT = 20


# ── WordPress REST ────────────────────────────────────────────────────────────
def _get(path: str, params: dict, api_base: str = "") -> list | dict | None:
    try:
        r = requests.get(f"{api_base or WP_BASE}/{path}", params=params,
                         headers=_UA, timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning("notices: WP GET %s failed: %s", path, e)
        return None


def _notice_category_ids(api_base: str = "") -> list[int]:
    """The 最新消息 archive aggregates its child categories (公文、其他消息), so we
    collect the parent plus every child — that mirrors what /category/news/ shows."""
    cats = _get("categories", {"slug": _NOTICE_CATEGORY_SLUG, "_fields": "id"}, api_base)
    if not isinstance(cats, list) or not cats:
        return []
    parent = cats[0]["id"]
    ids = [parent]
    children = _get("categories", {"parent": parent, "per_page": 100, "_fields": "id"}, api_base)
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


def fetch_notice_posts(api_base: str = "") -> list[dict]:
    """Every 【公文】 post in the 最新消息 archive, newest first."""
    cat_ids = _notice_category_ids(api_base)
    if not cat_ids:
        return []
    posts: list[dict] = []
    page = 1
    while True:
        batch = _get("posts", {
            "categories": ",".join(map(str, cat_ids)),
            "per_page": 100, "page": page,
            "_fields": "id,date,link,title,content",
        }, api_base)
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


def _drive_ivd_json(page_html: str) -> list | None:
    """The file list a public Drive folder page embeds as window['_DRIVE_ivd'].

    It is a JS string literal whose quotes/brackets are \\xNN-escaped; decode those
    and the rest is plain JSON: [[[file_id, [parent_id], name, mime_type, …], …], …].
    """
    m = re.search(r"_DRIVE_ivd'\]\s*=\s*'(.*?)'\s*;", page_html, re.S)
    if not m:
        return None
    body = re.sub(r"\\x([0-9a-fA-F]{2})",
                  lambda h: chr(int(h.group(1), 16)), m.group(1))
    try:
        return json.loads(body)
    except Exception as e:
        logger.warning("notices: could not parse Drive folder listing: %s", e)
        return None


def _pdf_rank(name: str) -> int:
    """Sort key that puts the 公文 letter itself ahead of its attachments.

    A notice folder holds the letter (named with its 文號, e.g.
    "CJ-DTLS-251231-01 函邀 …") plus 附件 (報名表、名單…). The letter is the only
    file with the event's date/place in it — reading 附件一 instead yields nonsense."""
    if re.match(r"\s*[A-Za-z]{2,6}-[A-Za-z]{2,6}-\d{6}", name or ""):
        return 0
    if re.search(r"附件|報名表|名單", name or ""):
        return 2
    return 1


def _public_folder_pdf_ids(folder_id: str) -> list[str]:
    """PDF file ids inside a *publicly shared* Drive folder, letter first.

    公文 folders are shared with anyone-with-the-link, so the folder page itself
    lists them — no credentials needed. That matters: the Drive API path below
    depends on an OAuth token that silently expires, and when it does every
    notice loses its date/location/fee."""
    if not folder_id:
        return []
    try:
        r = requests.get(f"https://drive.google.com/drive/folders/{folder_id}",
                         headers=_DRIVE_UA, timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        logger.warning("notices: fetching Drive folder %s failed: %s", folder_id, e)
        return []
    listing = _drive_ivd_json(r.text)
    if not listing or not isinstance(listing[0], list):
        return []
    pdfs = [f for f in listing[0]
            if isinstance(f, list) and len(f) > 3 and f[3] == "application/pdf"]
    pdfs.sort(key=lambda f: (_pdf_rank(f[2] or ""), f[2] or ""))
    return [f[0] for f in pdfs]


def _public_pdf_bytes(file_id: str) -> bytes | None:
    """Download a world-readable Drive file without credentials. Returns None for
    anything that isn't a PDF — a permission wall answers with an HTML page."""
    try:
        r = requests.get("https://drive.google.com/uc",
                         params={"export": "download", "id": file_id},
                         headers=_DRIVE_UA, timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        logger.warning("notices: public download of %s failed: %s", file_id, e)
        return None
    return r.content if r.content[:4] == b"%PDF" else None


def _notice_pdf_bytes(drive_url: str) -> bytes | None:
    """Resolve the notice's Drive link to PDF bytes — the link is usually a shared
    folder (list it, take the first PDF) but may point straight at a file.

    Tries the public HTTP route first and only then the Drive API, so a missing or
    expired OAuth token costs nothing as long as the notice is publicly shared."""
    if not drive_url:
        return None
    file_id = _drive_file_id(drive_url)
    folder_id = None if file_id else _drive_folder_id(drive_url)

    for fid in ([file_id] if file_id else _public_folder_pdf_ids(folder_id or "")):
        pdf = _public_pdf_bytes(fid)
        if pdf:
            return pdf

    svc = event_pdfs._drive_service()
    if svc is None:
        return None
    if not file_id:
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
        files.sort(key=lambda f: (_pdf_rank(f.get("name", "")), f.get("name", "")))
        file_id = files[0]["id"]
    return event_pdfs.download_pdf(file_id)


def notice_file_url(drive_url: str) -> str:
    """公文本文那一份 PDF 的直接連結（Drive 檢視頁），解不出來就回空字串。

    地區網站給的是一個資料夾：公文本身、報名表、附件都在裡面，社友點「公文」看到的
    是一份檔案清單，還要自己認哪一份才是公文。這裡沿用 _pdf_rank 的規則（有文號的
    那份排最前）把它挑出來。資料夾連結仍然留在 pdf_url，附件不會因此不見。"""
    if not drive_url:
        return ""
    file_id = _drive_file_id(drive_url)
    if not file_id:
        ids = _public_folder_pdf_ids(_drive_folder_id(drive_url) or "")
        file_id = ids[0] if ids else ""
    return f"https://drive.google.com/file/d/{file_id}/view" if file_id else ""


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
- is_event 先看公文有沒有要求收件人「出席／報名／派員參加」某一場活動。純粹公告、
  通知決議結果、催繳費用、通過預算、提供名單 → is_event=false，即使內文順帶提到
  某個未來日期（例如決議通過的明年年會日期），也不要當成活動。
- 活動日期寫在「說明」段落裡（多半是「時間：2025 年 08 月 09 日(星期六) 13:00-17:30」）。
  公文開頭信頭的「日期：」是發文日期，文號裡的數字也是發文日期，兩者都不是活動日期，
  絕對不要拿來當 date。內文找不到活動日期就 is_event=false。
- 日期一律用西元 YYYY-MM-DD。看到民國年請換算（民國115年=西元2026年）。
- time 只填時刻，不要填日期。
- 找不到的欄位留空字串。時間用 24 小時制範圍（例：13:30-17:00），整天可寫「整天」。
- 費用只寫金額，越短越好（例：每社 NT$3,000、每位 NT$500、免費），不要整段抄說明。
- 地點寫場地名稱，可加括號地址，不要抄整段。

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
def _read_notice_details(title: str, drive_url: str, fallback_date: str) -> dict:
    """{date, location, time, fee} read out of the notice's PDF — {} if the PDF is
    unreachable/unreadable, or if the 公文 announces no event to attend (a fee
    reminder, a name list, a result notice), in which case there is no date to
    put on the calendar and the publish date must stand."""
    if not drive_url:
        return {}
    pdf = _notice_pdf_bytes(drive_url)
    if not pdf:
        return {}
    details = _extract_event_details(title, _pdf_text(pdf), fallback_date)
    if not details or not details.get("is_event"):
        return {}
    return {k: details[k] for k in ("date", "location", "time", "fee")}


def _build_event(post: dict, district: str = "") -> dict:
    """Map a notice post (+ whatever we could read from its PDF) to an event row."""
    details = _read_notice_details(
        post["title"], post.get("drive_url", ""), post["publish_date"])
    # Real event date when we could read it; otherwise fall back to the publish date
    # so the notice still lands on the calendar rather than vanishing.
    return {
        "scope": "district",
        "district": district or db.DEFAULT_DISTRICT,
        "type": "公文",
        "title": post["title"],
        "date": details.get("date") or post["publish_date"] or None,
        "location": details.get("location", ""),
        "time": details.get("time", ""),
        "fee": details.get("fee", ""),
        "pdf_url": post.get("drive_url", ""),
        # 資料夾留著（附件在裡面），另外記住公文本文那一份，社友才能一點就看到公文
        "notice_file_url": notice_file_url(post.get("drive_url", "")),
        "source_url": post["source_url"],
    }


def refresh_notice_details() -> dict:
    """Re-read the PDF of every synced 公文 that still has no 地點/時間/費用 and fill
    them in. Notices synced while Drive was unreachable are stuck on their publish
    date with empty details; this is what unsticks them without re-adding rows."""
    rows = db.notice_events_missing_details()
    updated = errors = 0
    for row in rows:
        try:
            details = _read_notice_details(
                row["title"], row.get("pdf_url") or "", str(row.get("date") or ""))
        except Exception as e:
            errors += 1
            logger.warning("notices: refresh failed for %r: %s", row["title"], e)
            continue
        if not any(details.values()):
            continue
        patch = {k: v for k, v in details.items() if v}
        db.update_event(row["id"], patch)
        updated += 1
    report = {"checked": len(rows), "updated": updated, "errors": errors}
    logger.info("notices: refresh done %s", report)
    return report


def refresh_notice_file_links() -> dict:
    """把還停在資料夾連結的舊公文，補上「公文本文那一份 PDF」的直接連結。

    這些公文是在只存資料夾連結的年代同步進來的。每一筆要抓一次 Drive 的資料夾頁，
    所以只處理還沒有連結的，重跑不會重做。"""
    rows = db.notice_events_without_file_link()
    updated = errors = 0
    for row in rows:
        try:
            url = notice_file_url(row.get("pdf_url") or "")
        except Exception as e:
            errors += 1
            logger.warning("notices: file link failed for %r: %s", row["title"], e)
            continue
        if not url:
            continue
        db.update_event(row["id"], {"notice_file_url": url})
        updated += 1
    report = {"checked": len(rows), "updated": updated, "errors": errors}
    logger.info("notices: file links done %s", report)
    return report


def sync_district_notices(district: str, api_base: str) -> dict:
    """One district's 公文 → its own calendar. 抓來的活動都蓋上這個地區的章。"""
    posts = fetch_notice_posts(api_base)
    if not posts:
        return {"found": 0, "added": 0, "skipped": 0, "errors": 0}
    known = db.event_source_urls()
    added = skipped = errors = 0
    for post in posts:
        if not post.get("source_url") or post["source_url"] in known:
            skipped += 1
            continue
        try:
            db.create_event(_build_event(post, district))
            added += 1
        except Exception as e:
            errors += 1
            logger.warning("notices: failed to add %r: %s", post.get("title"), e)
    return {"found": len(posts), "added": added, "skipped": skipped, "errors": errors}


def sync_notices(refresh: bool = False) -> dict:
    """Pull 【公文】 posts for every district that has a source configured, and add
    any not already synced. With refresh=True, also re-read the PDFs of previously
    synced notices whose details came out empty. Returns a summary report.

    沒設定 notices_api 的地區直接跳過 —— 不是每個地區的網站都是 WordPress，也不是
    每個地區都要自動同步；沒有來源就靠人工在行事曆建立活動，不該因此讓整批同步失敗。
    去重看的是貼文網址（全域唯一），所以兩個地區的公文不會互相蓋掉。"""
    districts = [d for d in db.list_districts() if (d.get("notices_api") or "").strip()]
    totals = {"found": 0, "added": 0, "skipped": 0, "errors": 0}
    per_district = {}
    for d in districts:
        rep = sync_district_notices(d["code"], d["notices_api"])
        per_district[d["code"]] = rep
        for k in totals:
            totals[k] += rep[k]
    report = {**totals, "districts": per_district,
              "districts_without_source": [d["code"] for d in db.list_districts()
                                           if not (d.get("notices_api") or "").strip()]}
    if refresh:
        report["refreshed"] = refresh_notice_details()
        # 舊公文只存了資料夾連結，順便補上「公文本文那一份」的直接連結
        report["file_links"] = refresh_notice_file_links()
    logger.info("notices: sync done %s", report)
    return report


def sync_notices_safe(refresh: bool = False) -> dict:
    """sync_notices() that never raises — for fire-and-forget background use."""
    try:
        return sync_notices(refresh=refresh)
    except Exception as e:
        logger.warning("notices: sync aborted: %s", e)
        return {"found": 0, "added": 0, "skipped": 0, "errors": 0}


SYNC_INTERVAL_SECONDS = 6 * 60 * 60   # re-check the site for new 公文 every 6 hours


def run_periodic(interval: float = SYNC_INTERVAL_SECONDS) -> None:
    """Sync once now, then every `interval` seconds. Runs forever — meant to be the
    target of a daemon thread started at app startup. Since there is no scheduler in
    this backend, this is the whole cron: it lives as long as the process does, and
    each pass only touches 公文 not already in the calendar. The first pass also
    retries the PDFs of notices that were synced without details — once per boot,
    not every pass, so a 公文 that genuinely has no event isn't re-read forever."""
    first = True
    while True:
        sync_notices_safe(refresh=first)
        first = False
        time.sleep(interval)
