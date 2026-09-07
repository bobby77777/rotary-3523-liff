"""Resolve per-event PDFs from a dedicated Google Drive folder.

執秘 workflow: drop a PDF into the EVENT_PDF_FOLDER_ID folder, named with the
event id as a leading number (e.g. "102.pdf" or "102 - DTTS研討會.pdf"). The
backend lists that folder (cached ~60s), maps each file to an event id, and
streams the bytes on demand via GET /events/{id}/pdf — files stay private, and
no code change is needed to publish a new event PDF.

Credentials come from a service account or the OAuth token ingest.py already
created — as files under secrets/ locally, or as env vars holding the whole JSON
(GOOGLE_SERVICE_ACCOUNT_JSON / GOOGLE_OAUTH_TOKEN_JSON) on a serverless host with
no writable disk. Unlike ingest.py this never launches an interactive OAuth flow,
because it runs inside web requests: if no credential works it degrades to
"no PDFs".
"""
import io
import json
import re
import time
import logging
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from .config import (EVENT_PDF_FOLDER_ID, GOOGLE_OAUTH_TOKEN_JSON,
                     GOOGLE_SERVICE_ACCOUNT_JSON)

logger = logging.getLogger(__name__)

_SECRETS = Path(__file__).parent.parent / "secrets"
_TOKEN_FILE = _SECRETS / "token.json"
_SA_FILE = _SECRETS / "service_account.json"
_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

_CACHE_TTL = 60  # seconds
_cache = {"at": 0.0, "map": {}}  # {event_id: file_id}


def _service_account_info() -> dict | None:
    """Service-account JSON from the env var, else from secrets/service_account.json."""
    if GOOGLE_SERVICE_ACCOUNT_JSON:
        try:
            return json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        except ValueError as e:
            logger.warning("event_pdfs: GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON: %s", e)
    if _SA_FILE.exists():
        try:
            return json.loads(_SA_FILE.read_text())
        except (OSError, ValueError) as e:
            logger.warning("event_pdfs: cannot read %s: %s", _SA_FILE, e)
    return None


def _oauth_token_info() -> dict | None:
    """OAuth user-token JSON from the env var, else from secrets/token.json."""
    if GOOGLE_OAUTH_TOKEN_JSON:
        try:
            return json.loads(GOOGLE_OAUTH_TOKEN_JSON)
        except ValueError as e:
            logger.warning("event_pdfs: GOOGLE_OAUTH_TOKEN_JSON is not valid JSON: %s", e)
    if _TOKEN_FILE.exists():
        try:
            return json.loads(_TOKEN_FILE.read_text())
        except (OSError, ValueError) as e:
            logger.warning("event_pdfs: cannot read %s: %s", _TOKEN_FILE, e)
    return None


def _drive_service():
    """Read-only Drive client. Prefers a service account, which never expires and needs
    no browser; falls back to the OAuth user token. Either credential can come from an
    env var holding the whole JSON (the only option on a read-only serverless host) or
    from the file under secrets/ — env var wins. Never launches an interactive OAuth
    flow (this runs inside web requests). Returns None if nothing works.
    """
    # 1) Service account — the robust backend path (share the folder with its email).
    sa_info = _service_account_info()
    if sa_info:
        try:
            creds = ServiceAccountCredentials.from_service_account_info(
                sa_info, scopes=_SCOPES)
            return build("drive", "v3", credentials=creds, cache_discovery=False)
        except Exception as e:
            logger.warning("event_pdfs: service account auth failed: %s", e)
    # 2) Fallback — existing OAuth user token (may expire if the app is in Testing mode).
    token_info = _oauth_token_info()
    if not token_info:
        return None
    try:
        creds = Credentials.from_authorized_user_info(token_info, _SCOPES)
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                # 把更新後的 token 寫回去，下次冷啟動才不用再 refresh。serverless 的
                # 檔案系統是唯讀的，寫不進去也無所謂 —— 這趟的 creds 已經可用了。
                try:
                    _TOKEN_FILE.write_text(creds.to_json())
                except OSError:
                    logger.debug("event_pdfs: token cache is read-only; not persisting")
            else:
                return None
        return build("drive", "v3", credentials=creds, cache_discovery=False)
    except Exception as e:
        logger.warning("event_pdfs: drive service unavailable: %s", e)
        return None


def _leading_event_id(name: str) -> int | None:
    """Event id = the leading run of digits in the filename ("102 - foo.pdf" -> 102)."""
    m = re.match(r"\s*(\d+)", name or "")
    return int(m.group(1)) if m else None


def _refresh_map() -> dict:
    if not EVENT_PDF_FOLDER_ID:
        return {}
    svc = _drive_service()
    if svc is None:
        return {}
    mapping: dict[int, str] = {}
    try:
        page_token = None
        while True:
            resp = svc.files().list(
                q=(f"'{EVENT_PDF_FOLDER_ID}' in parents and trashed=false "
                   f"and mimeType='application/pdf'"),
                fields="nextPageToken, files(id, name, modifiedTime)",
                orderBy="modifiedTime desc",
                pageSize=100,
                pageToken=page_token,
            ).execute()
            for f in resp.get("files", []):
                eid = _leading_event_id(f["name"])
                # Newest file wins (ordered desc), so re-uploading replaces cleanly.
                if eid is not None and eid not in mapping:
                    mapping[eid] = f["id"]
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    except Exception as e:
        logger.warning("event_pdfs: listing folder failed: %s", e)
        return _cache["map"]  # keep last-known good on a transient failure
    return mapping


def event_pdf_map(force: bool = False) -> dict:
    """{event_id: drive_file_id} for events that currently have an uploaded PDF."""
    now = time.time()
    if force or now - _cache["at"] > _CACHE_TTL:
        _cache["map"] = _refresh_map()
        _cache["at"] = now
    return _cache["map"]


def get_pdf_file_id(event_id: int) -> str | None:
    return event_pdf_map().get(event_id)


def download_pdf(file_id: str) -> bytes | None:
    svc = _drive_service()
    if svc is None:
        return None
    try:
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, svc.files().get_media(fileId=file_id))
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buf.getvalue()
    except Exception as e:
        logger.warning("event_pdfs: download %s failed: %s", file_id, e)
        return None
