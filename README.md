# Rotary 3523 LINE App

A LINE **LIFF** web app plus a **FastAPI** backend for International Rotary
District 3523 — event calendar & registration, per-club weekly bulletins
(社刊), meeting agendas (議程), and a LINE chatbot (award queries, RAG Q&A,
member profiles).

This is a **monorepo**: the static frontend and the Python backend live side by
side but deploy independently.

```
rotary-3523-liff/
├── frontend/                 # Static LIFF pages — served by GitHub Pages
│   ├── index.html            # Main LIFF app: events, registration, profile, admin (社長/主委)
│   ├── bulletin.html         # Per-club weekly bulletin (社刊) editor + read-only viewer
│   └── calendar.html         # Calendar + agenda (議程) editor, agenda PDF export
├── backend/                  # FastAPI service (LINE webhook + REST API)
│   ├── app/
│   │   ├── main.py           # Routes: webhook, /events, /admin/events, /bulletin/*, /me …
│   │   ├── db.py             # psycopg2 pool + all SQL (Supabase Postgres)
│   │   ├── event_pdfs.py     # Per-event PDFs resolved from a Google Drive folder
│   │   ├── agent.py          # OpenAI tool-calling loop (award search / RAG)
│   │   ├── tools.py          # Agent tools
│   │   ├── line_api.py       # LINE Messaging API wrapper
│   │   └── config.py         # Env loading
│   ├── ingest.py             # Google Drive → Supabase RAG ingestion (standalone)
│   ├── reauth_drive.py       # Re-mint Google OAuth token when it expires
│   └── run.py                # Entry point: uvicorn app.main:app
└── .github/workflows/pages.yml  # Publishes frontend/ to GitHub Pages
```

---

## Frontend (`frontend/`)

Static HTML opened as a LINE LIFF app; no build step.

| Page | What it does |
|------|--------------|
| `index.html` | Main app — event calendar & registration, payment reporting, member profile, and an admin area (labelled **社長** in club scope, **主委** in district scope) with event management, attendance, dues, stats. |
| `bulletin.html` | Weekly bulletin (社刊), **stored per club**. 主委 edits and **發布社刊** (one-click publish, no dialog); anyone can **下載 PDF** (real vector PDF via the browser's print engine). |
| `calendar.html` | Annual event table (district/club scope) + per-event **agenda (議程)** editor with auto-computed times, quick-fill templates, PDF-link attachments, LINE preview, and agenda PDF export. |

### Deploy (GitHub Pages via Actions)

`.github/workflows/pages.yml` publishes `frontend/` as the **site root**, so the
public URLs stay `https://<user>.github.io/rotary-3523-liff/index.html` (and
`bulletin.html`, `calendar.html`) — the LIFF entry point and the backend's
`BULLETIN_BASE_URL` / `CALENDAR_BASE_URL` don't change.

**One-time setup:** repo **Settings → Pages → Source = "GitHub Actions"**.
After that, every push to `main` that touches `frontend/` redeploys automatically.

---

## Backend (`backend/`)

FastAPI service backing the LIFF app and the LINE bot.

**Highlights**
- **LINE bot** — award queries, RAG document Q&A, member profiles, date/weather; keyword shortcuts `社刊` and `行事曆` reply with editor links.
- **Events / 行事曆** — editable in Supabase (`events` table, scope + club + agenda JSON). CRUD via `/admin/events` (admin-gated); read via `/events`.
- **社刊** — published per club as content JSON (`/bulletin/content`); members load and print their own vector PDF.
- **Event PDFs** — non-例會 events link a PDF that 執秘 drops in a Google Drive folder; the backend streams it via `/events/{id}/pdf`.
- **RAG ingestion** — `ingest.py` watches a Google Drive folder and re-embeds changed files into Supabase (pgvector).

### Requirements
- Python 3.11+ (3.10 works but Google libs warn)
- Supabase project (pgvector enabled)
- LINE Messaging API channel · OpenAI API key · OpenWeatherMap API key
- Google Cloud project with Drive API (a **service account** is recommended over OAuth for the server)

### Setup

```bash
cd backend
pip install -r requirements.txt
```

Create `backend/.env` (git-ignored):

```env
LINE_CHANNEL_ACCESS_TOKEN=...
LINE_CHANNEL_SECRET=...
OPENAI_API_KEY=...
DATABASE_URL=postgresql://postgres.PROJECT_REF:PASSWORD@aws-0-REGION.pooler.supabase.com:5432/postgres
OPENWEATHERMAP_API_KEY=...
APP_BASE_URL=https://your-public-backend            # ngrok / prod URL, used for event PDF links
GOOGLE_DRIVE_FOLDER_ID=...                           # RAG ingestion folder
EVENT_PDF_FOLDER_ID=...                              # separate folder for event PDFs (must differ)
# Optional overrides (default to the GitHub Pages URLs):
# BULLETIN_BASE_URL=https://<user>.github.io/rotary-3523-liff/bulletin.html
# CALENDAR_BASE_URL=https://<user>.github.io/rotary-3523-liff/calendar.html
```

Google Drive auth — either:
- **Service account** (recommended): put the key at `backend/secrets/service_account.json` and share both Drive folders with its email; or
- **OAuth**: put `secrets/credentials.json`, then run `python reauth_drive.py` once to create `secrets/token.json` (expires if the OAuth app is in "Testing" mode).

Tables are created / migrated automatically on startup (`ensure_*` in `db.py`);
the RAG tables (`documents`, `document_rows`, `document_metadata`,
`personal_information`) still need the SQL from the Supabase editor — see the
git history for the DDL.

### Run

```bash
cd backend
python run.py            # uvicorn app.main:app on 0.0.0.0:80 (reload)
```

Expose it (ngrok / reverse proxy) and set the LINE webhook to
`https://your-public-backend/webhook`. Keep `APP_BASE_URL` in `.env` equal to
that public URL.

RAG ingestion (optional, separate process):

```bash
python ingest.py --full-sync   # one-time import
python ingest.py               # poll Drive every 60s
```

---

## Notes
- `backend/.env` and `backend/secrets/` hold credentials and are git-ignored — never commit them.
- Frontend and backend deploy separately: **frontend** = push → GitHub Pages Action; **backend** = restart the service (DB migrations run on startup).
- Event `id` is referenced across registrations, check-in, stats, golf scores, and event-PDF filenames — keep it stable.
