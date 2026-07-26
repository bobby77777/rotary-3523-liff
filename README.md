# Rotary LINE Bot

A LINE chatbot assistant for Rotary club events. Supports award record queries, member profile management, and automatic Google Drive document sync.

---

## Features

- **Award queries** — Search by club, name, nickname, award, district, time slot, or notes in any combination
- **Statistics & rankings** — Which club has the most awards, list all award types, count by district
- **Knowledge Q&A** — RAG-based answers from uploaded PDF / documents
- **Member profiles** — Members fill in and query their own profile via an in-LINE form
- **Date / weather** — Real-time Taipei time and city weather
- **Auto sync** — Watches a Google Drive folder and re-ingests files on any change

---

## Architecture

```
LINE User
   │  webhook
   ▼
app/main.py (FastAPI)
   ├── /webhook        → verify signature → process events in background
   └── /form/sign      → member profile form
         │
         ▼
     app/agent.py (GPT-4o-mini + tool calling loop)
         │
         ├── app/tools.py
         │    ├── get_document_rows   → SQL award list search
         │    ├── get_award_stats     → GROUP BY aggregation
         │    ├── rag_search          → pgvector semantic search
         │    ├── get_personal_information
         │    ├── get_datetime / get_weather
         │    └── list_documents / get_file_content
         │
         └── app/db.py (psycopg2 connection pool → Supabase PostgreSQL)

ingest.py (standalone process)
   └── Google Drive → PDF / CSV / XLSX → chunked embeddings → documents + document_rows
```

---

## Database Tables

| Table | Purpose |
|-------|---------|
| `document_rows` | Raw CSV/XLSX rows (JSONB) for SQL search |
| `documents` | Chunked text + pgvector embeddings for RAG |
| `document_metadata` | Document list (id, title, url, schema) |
| `personal_information` | Member profiles (line_user_id, club, name, nickname, diet, 寶尊眷) |
| `message_store` | Last 20 messages per user for conversation history |

---

## Requirements

- Python 3.11+
- Supabase project with pgvector extension enabled
- LINE Messaging API channel
- OpenAI API key
- OpenWeatherMap API key
- Google Cloud project with Drive API enabled (OAuth 2.0 desktop client credentials)

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Create `.env`

```env
LINE_CHANNEL_ACCESS_TOKEN=your_line_channel_access_token
LINE_CHANNEL_SECRET=your_line_channel_secret
OPENAI_API_KEY=your_openai_api_key
DATABASE_URL=your_supabase_session_pooler_url
OPENWEATHERMAP_API_KEY=your_openweathermap_api_key
APP_BASE_URL=https://your-domain.com
GOOGLE_DRIVE_FOLDER_ID=your_google_drive_folder_id
```

> Use the Supabase **Session Pooler** URL (port 5432) for `DATABASE_URL`:
> `postgresql://postgres.PROJECT_REF:PASSWORD@aws-0-REGION.pooler.supabase.com:5432/postgres`

### 3. Create Supabase tables

Run the following in the Supabase SQL Editor:

```sql
-- Enable pgvector
create extension if not exists vector;

-- Vector document store
create table documents (
    id bigserial primary key,
    content text,
    metadata jsonb,
    embedding vector(1536)
);

-- Raw row data for SQL search
create table document_rows (
    id bigserial primary key,
    dataset_id text,
    row_data jsonb
);

-- Document index
create table document_metadata (
    id text primary key,
    title text,
    url text,
    schema text
);

-- Member profiles
create table personal_information (
    line_user_id text primary key,
    club_name text,
    full_name text,
    nickname text,
    diet_type text,
    spouse_name text not null default ''
);
```

> `message_store` is created automatically on first startup.

### 4. Set up Google Drive OAuth

1. Go to [Google Cloud Console](https://console.cloud.google.com) and create an OAuth 2.0 client (type: Desktop app)
2. Download the credentials and save as `secrets/credentials.json`
3. Run `ingest.py` once — it will open a browser for authorization and save `secrets/token.json`

---

## Running

### Start the webhook server

```bash
python run.py
```

The server listens on `0.0.0.0:8000`. Expose it via ngrok or a reverse proxy, then set the LINE Webhook URL to:

```
https://your-domain.com/webhook
```

### Initial Google Drive import

```bash
python ingest.py --full-sync
```

### Watch for Drive changes

```bash
python ingest.py
```

Polls every 60 seconds and automatically re-ingests any new or modified files.

---

## Supported Query Types

| Example question | Tool used |
|-----------------|-----------|
| Who won the Arch C. Klumph award? | `get_document_rows` (award) |
| What awards did 松青社 win? | `get_document_rows` (club_name) |
| Who won in the first time slot? | `get_document_rows` (time_slot) |
| Award list for district 3490 | `get_document_rows` (district) |
| Which club has the most awards? | `get_award_stats` (group_by=社名) |
| What award types are there? | `get_award_stats` (group_by=獎項) |
| Who am I? | `get_personal_information` |
| What's today's date? | `get_datetime` |
| Weather in Taipei | `get_weather` |

---

## Project Structure

```
rotary/
├── app/
│   ├── __init__.py
│   ├── main.py          # FastAPI routes (webhook, form)
│   ├── agent.py         # GPT-4o-mini tool calling loop
│   ├── tools.py         # LangChain @tool definitions
│   ├── db.py            # psycopg2 connection pool and all SQL operations
│   ├── line_api.py      # LINE Bot API wrapper
│   ├── config.py        # Environment variable loading
│   └── templates/
│       └── form.html    # Member profile form
├── secrets/
│   ├── credentials.json # Google OAuth credentials (never commit)
│   ├── token.json       # Google OAuth token (never commit)
│   └── drive_state.json # Google Drive poll page token
├── ingest.py            # Standalone Google Drive sync script
├── run.py               # Server entry point: uvicorn app.main:app
├── .env                 # Environment variables (never commit)
├── requirements.txt
└── .gitignore
```

---

## Notes

- `secrets/` and `.env` contain sensitive credentials — both are excluded by `.gitignore`
- LINE messages are capped at 5000 characters; responses over 4500 chars are automatically truncated with a prompt to narrow the search
- `get_document_rows` returns up to 50 rows; the `total` field shows the full match count
- Conversation history is limited to the last 20 messages per user to stay within token limits
