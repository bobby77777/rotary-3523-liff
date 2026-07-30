"""
Google Drive → Supabase ingestion watcher.

Polls the configured Drive folder every 60 seconds.
Supports PDF (vector store), CSV, and XLSX (document_rows + vector store).

Usage:
    python ingest.py               # watch for changes only
    python ingest.py --full-sync   # ingest all existing files first, then watch
"""

import io
import json
import logging
import sys
import time
from pathlib import Path

import pandas as pd
import pypdf
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from openai import OpenAI

from app import db
from app.config import GOOGLE_DRIVE_FOLDER_ID, OPENAI_API_KEY

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
_SECRETS = Path(__file__).parent / "secrets"
CREDENTIALS_FILE = _SECRETS / "credentials.json"
TOKEN_FILE = _SECRETS / "token.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

POLL_INTERVAL = 60
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 200
STATE_FILE = _SECRETS / "drive_state.json"

SUPPORTED_TYPES = {
    "application/pdf",
    "text/csv",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

_openai = OpenAI(api_key=OPENAI_API_KEY)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _drive_service():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _embed(texts: list[str]) -> list[list[float]]:
    resp = _openai.embeddings.create(model="text-embedding-ada-002", input=texts)
    return [e.embedding for e in resp.data]


def _chunk(text: str) -> list[str]:
    if len(text) <= CHUNK_SIZE:
        return [text]
    chunks, start = [], 0
    while start < len(text):
        end = min(start + CHUNK_SIZE, len(text))
        chunks.append(text[start:end])
        if end == len(text):
            break
        start = end - CHUNK_OVERLAP
    return chunks


def _download(service, file_id: str) -> io.BytesIO:
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, service.files().get_media(fileId=file_id))
    done = False
    while not done:
        _, done = dl.next_chunk()
    buf.seek(0)
    return buf


# ── File processors ────────────────────────────────────────────────────────────

def _process_pdf(file_id: str, file_title: str, buf: io.BytesIO) -> None:
    reader = pypdf.PdfReader(buf)
    text = "\n".join(
        page.extract_text() for page in reader.pages if page.extract_text()
    )
    if not text.strip():
        logger.warning("No text extracted from %s", file_title)
        return
    chunks = _chunk(text)
    embeddings = _embed(chunks)
    for chunk, emb in zip(chunks, embeddings):
        db.insert_document(chunk, {"file_id": file_id, "file_title": file_title}, emb)
    logger.info("Inserted %d chunks for PDF: %s", len(chunks), file_title)


def _process_tabular(file_id: str, df: pd.DataFrame) -> None:
    # Bulk insert raw rows for SQL search
    db.insert_document_rows_bulk(file_id, [row.to_dict() for _, row in df.iterrows()])

    # Embed chunked text for RAG
    schema = json.dumps(list(df.columns))
    concatenated = df.to_string(index=False)
    chunks = _chunk(concatenated)
    embeddings = _embed(chunks)
    for chunk, emb in zip(chunks, embeddings):
        db.insert_document(chunk, {"file_id": file_id}, emb)
    db.update_document_metadata_schema(file_id, schema)
    logger.info("Inserted %d rows + %d chunks for tabular file", len(df), len(chunks))


def process_file(service, file: dict) -> None:
    file_id = file["id"]
    file_type = file["mimeType"]
    file_title = file["name"]
    file_url = file.get("webViewLink", "")

    if file_type not in SUPPORTED_TYPES:
        logger.info("Skipping unsupported type %s: %s", file_type, file_title)
        return

    logger.info("Processing: %s (%s)", file_title, file_type)

    db.delete_documents_by_file_id(file_id)
    db.delete_document_rows_by_dataset_id(file_id)
    db.upsert_document_metadata(file_id, file_title, file_url)

    buf = _download(service, file_id)

    if file_type == "application/pdf":
        _process_pdf(file_id, file_title, buf)
    elif file_type == "text/csv":
        _process_tabular(file_id, pd.read_csv(buf))
    else:
        _process_tabular(file_id, pd.read_excel(buf, engine="openpyxl"))


# ── Drive change polling ───────────────────────────────────────────────────────

def _load_page_token(service) -> str:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())["page_token"]
    token = service.changes().getStartPageToken().execute()["startPageToken"]
    _save_page_token(token)
    return token


def _save_page_token(token: str) -> None:
    STATE_FILE.write_text(json.dumps({"page_token": token}))


def _poll(service, folder_id: str, page_token: str) -> tuple[list[dict], str]:
    changed = []
    while True:
        resp = service.changes().list(
            pageToken=page_token,
            spaces="drive",
            fields="nextPageToken,newStartPageToken,"
                   "changes(fileId,file(id,name,mimeType,webViewLink,parents,trashed))",
        ).execute()

        for change in resp.get("changes", []):
            f = change.get("file") or {}
            if f and not f.get("trashed") and folder_id in (f.get("parents") or []):
                changed.append(f)

        if "newStartPageToken" in resp:
            return changed, resp["newStartPageToken"]
        page_token = resp["nextPageToken"]


# ── Full sync ──────────────────────────────────────────────────────────────────

def full_sync(service) -> None:
    logger.info("Running full sync of folder %s ...", GOOGLE_DRIVE_FOLDER_ID)
    page_token = None
    while True:
        params = dict(
            q=f"'{GOOGLE_DRIVE_FOLDER_ID}' in parents and trashed=false",
            fields="nextPageToken,files(id,name,mimeType,webViewLink)",
            pageSize=100,
        )
        if page_token:
            params["pageToken"] = page_token
        resp = service.files().list(**params).execute()
        for f in resp.get("files", []):
            try:
                process_file(service, f)
            except Exception:
                logger.exception("Failed: %s", f.get("name"))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    logger.info("Full sync complete.")


# ── Entry point ────────────────────────────────────────────────────────────────

def run(sync_first: bool = False) -> None:
    service = _drive_service()

    if sync_first:
        full_sync(service)

    page_token = _load_page_token(service)
    logger.info("Watching for changes every %ds ...", POLL_INTERVAL)

    while True:
        try:
            changed, page_token = _poll(service, GOOGLE_DRIVE_FOLDER_ID, page_token)
            _save_page_token(page_token)
            for f in changed:
                try:
                    process_file(service, f)
                except Exception:
                    logger.exception("Failed: %s", f.get("name"))
        except Exception:
            logger.exception("Polling error")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run(sync_first="--full-sync" in sys.argv)
