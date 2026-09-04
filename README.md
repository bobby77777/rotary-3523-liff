# Rotary 3523 LINE App

A LINE **LIFF** web app plus a **FastAPI** backend for International Rotary
District 3523 — event calendar & registration, per-club weekly bulletins
(社刊), meeting agendas (議程), a chair/president back-office (attendance,
dues, seating, raffle, board motions …), and a LINE chatbot (award queries,
RAG Q&A, member profiles).

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
│   │   ├── agenda_pdf.py     # 議程 → vector PDF (fpdf2 + embedded CJK font)
│   │   ├── agent.py          # OpenAI tool-calling loop (award search / RAG)
│   │   ├── tools.py          # Agent tools
│   │   ├── line_api.py       # LINE Messaging API wrapper
│   │   └── config.py         # Env loading
│   ├── ingest.py             # Google Drive → Supabase RAG ingestion (standalone)
│   ├── reauth_drive.py       # Re-mint Google OAuth token when it expires
│   └── run.py                # Entry point: uvicorn app.main:app
├── db.md                     # Every table: what it holds, who writes it, the traps
├── requirements.txt          # Python deps for the backend
└── .github/workflows/pages.yml  # Publishes frontend/ to GitHub Pages
```

---

## Frontend (`frontend/`)

Static HTML opened as a LINE LIFF app; no build step.

| Page | What it does |
|------|--------------|
| `index.html` | Main app. Four full pages behind a bottom tab bar — **首頁** (next event + 近期活動), **個人** (check-in, 報名紀錄, 繳費, 得獎), **刊物**, and an admin page labelled **社長** in club scope / **主委** in district scope (hidden for plain members). Rows inside 現場作業 follow the active event's type; see [Admin tools](#admin-tools-後台). Short confirmations appear as toasts, receipts as dialogs. |
| `bulletin.html` | Weekly bulletin (社刊), **stored per club**. 主委 edits and **發布社刊** (one-click publish, no dialog); anyone can **下載 PDF** (real vector PDF via the browser's print engine). |
| `calendar.html` | Annual event table (district/club scope) + per-event **agenda (議程)** editor with auto-computed times, quick-fill templates, PDF-link attachments, and LINE preview. **下載 PDF** prints the agenda being edited (vector, via the browser); the copy members see is rendered by the backend from the saved agenda. |

### Deploy (GitHub Pages via Actions)

`.github/workflows/pages.yml` publishes `frontend/` as the **site root**, so the
public URLs stay `https://<user>.github.io/rotary-3523-liff/index.html` (and
`bulletin.html`, `calendar.html`) — the LIFF entry point and the backend's
`BULLETIN_BASE_URL` / `CALENDAR_BASE_URL` don't change.

**One-time setup:** repo **Settings → Pages → Source = "GitHub Actions"**.
After that, every push to `main` that touches `frontend/` redeploys automatically.

#### When a deploy doesn't land

A green workflow only means the artifact was uploaded and a deployment was
**requested** — publishing happens on GitHub's side afterwards. The two have
come apart, so verify against the served page, not the badge:

```bash
curl -sI https://<user>.github.io/rotary-3523-liff/ | grep -i last-modified
```

Failure modes seen in one afternoon (2026-08-06), all GitHub-side, none fixable
in this repo:

| Symptom in the log | What it means | What to do |
|---|---|---|
| `Current status: deployment_queued` repeating until `Timeout reached, aborting!` | The deployment was accepted but no Pages runner picked it up. `deploy-pages` clamps its wait to 10 min (`MAX_TIMEOUT = 600000`; the `timeout` input is `Math.min`'d against it, so raising it does nothing). | Wait, then re-dispatch. If it stays broken, use the branch fallback below. |
| Job `cancelled` with `steps: 0`, or a step failing at `Set up job` | No Actions runner was allocated at all. | `gh run rerun <id>` once runners recover. |
| `Multiple artifacts named "github-pages" … Artifact count is 2` | A **re-run** uploaded a second artifact beside the first instead of replacing it, and `deploy-pages` won't guess. The run is now poisoned. | Never "Re-run jobs" for this workflow — start a fresh run (`gh workflow run pages.yml`). |
| `Deployment cancelled` within seconds, on a commit that failed before | Pages keys a deployment by **commit sha**, and a timed-out deploy cancels its own record. That sha can never deploy again. | Push a new commit; an empty one (`git commit --allow-empty`) is enough. |

**Fallback: publish through the `gh-pages` branch.** The Actions deployment
pipeline and the legacy branch-build pipeline are separate, and the legacy one
kept working while the other was stuck. The `gh-pages` branch is left in place
for exactly this. To switch over, restore the branch-push version of
`pages.yml` (see `git log -- .github/workflows/pages.yml`) and point Pages at
the branch:

```bash
gh api -X PUT repos/:owner/:repo/pages --input - <<< \
  '{"build_type":"legacy","source":{"branch":"gh-pages","path":"/"}}'
gh api -X POST repos/:owner/:repo/pages/builds   # force a build; auto-triggers can stall
gh api repos/:owner/:repo/pages/builds/latest --jq '{status, commit}'
```

Switching the source does **not** tear down the currently served site, so this
is safe to do while members are using the app. Going back is
`'{"build_type":"workflow"}'` plus reverting `pages.yml`. Either way the public
URLs are identical, so the LIFF endpoint never changes.

---

## Backend (`backend/`)

FastAPI service backing the LIFF app and the LINE bot.

**Highlights**
- **LINE bot** — award queries, RAG document Q&A, member profiles, date/weather; the `行事曆` keyword replies with the calendar/agenda editor link. Everything else typed as text is answered with 「請使用下方選單按鈕操作」 — entry points live in the rich menu and the LIFF, not in keywords.
- **Events / 行事曆** — editable in Supabase (`events` table, scope + club + agenda JSON). CRUD via `/admin/events` (admin-gated); read via `/events`.
- **社刊** — published per club as content JSON (`/bulletin/content`); members load and print their own vector PDF.
- **議程 PDF** — `/events/{id}/pdf` renders the saved agenda into a real **vector** PDF on the fly (`agenda_pdf.py`, fpdf2 + an embedded CJK font): selectable, searchable text and working attachment links, instead of the old browser screenshot. Needs one font covering Traditional Chinese — auto-detected from `backend/assets/fonts/*.ttf` or the usual system paths, or set `AGENDA_FONT_PATH`. Without one the endpoint falls back to whatever was stored/uploaded before.
- **Event PDFs** — non-例會 events link a PDF that 執秘 drops in a Google Drive folder; the same `/events/{id}/pdf` streams it when the event has no agenda.
- **Admin back-office** — see the table below; every tool is backed by real tables and LINE pushes.
- **Check-in** — one endpoint (`/checkin`), two scan directions, told apart by the QR's content:
  - a member scans the **event's** check-in QR posted at the door (`RC3523-CHECKIN:<token>`) and is checked in themselves — the token identifies the event, so nobody has to pick one;
  - an admin scans a **member's** report QR (its value is that member's LINE userId) and checks that member in.

  The event QR is generated per event in the agenda editor (`/admin/events/{id}/checkin_qr`, admin-only) and stored in `event_checkin_qr` — token *and* the printable PNG, so the sheet on the wall and every later download are the same image. Re-generating rotates the token and voids anything already printed. Because that sheet can be photographed, self check-in is accepted **only on the event's own date**; and events carrying a venue coordinate (`events.geo`, set in 行事曆管理 as `緯度,經度`) additionally gate check-in on being within 100 m. Both directions notify the member, and admin scans leave the scanner a running tally.
- **RAG ingestion** — `ingest.py` watches a Google Drive folder and re-embeds changed files into Supabase (pgvector).

### Admin tools (後台)

Each is admin-gated (`db.is_admin`), scoped to one event or one club, and
reachable both from the LIFF admin tab and from a deep link in the LINE admin
menu (`?tab=admin&action=…`).

| 功能 | What it does | Endpoints |
|------|--------------|-----------|
| 報名專區 | Push a 參加意願調查 Flex card to selected members; 參加 also registers them. Chase non-responders, escalate the pending list to the 社長. | `/admin/survey*` |
| 社友出席率 · 統計看板 | Per-member attendance, per-club registration/payment/check-in KPIs. | `/admin/club_attendance`, `/admin/stats` |
| 待繳費明細 | Who still owes for an event, by club; 一鍵催繳 pushes them. People who already reported transfer digits are listed as 待對帳 and never chased. | `/admin/unpaid*` |
| 社友社費 | 執秘 bills a member for the month; the member reports payment from 個人中心. | `/dues/*` |
| 社務對帳 | Monthly club finance sheet (rent, salary, fixed items, member advances). | `/club/finance` |
| 貴賓唱名 | Per-event VIP list; mark arrival (server-stamped Taipei time) and 唱名. Arrivals after the event's start time fall into 補介紹. | `/admin/vips*` |
| 高球 | Score submission with New Peoria netting, hidden-hole draw, 4-per-組 grouping and 即時調組 (both players notified). | `/golf/*` |
| 桌次安排 | Seats registrants (guests included) N per table with clubmates together; swap seats on the spot; 公布桌次 pushes each attendee their table and seat. | `/admin/seating*` |
| 摸彩系統 | Prizes with quotas, drawn only from **checked-in** attendees, never the same person twice per event. Winners get a LINE push. | `/admin/raffle*` |
| RYE | One applicant list: auto-assign interview slots from the event start, notify students, and record/approve the parental consent form (verdict is pushed to the student). | `/admin/rye*` |
| 理監事專區 | Pick the board from the club roster, post a motion, and it goes out as a Flex card whose 同意/反對/棄權 buttons write back by postback. Votes are changeable until the motion is closed. | `/admin/board*` |

### Requirements
- Python 3.11+ (3.10 works but Google libs warn)
- Supabase project (pgvector enabled)
- LINE Messaging API channel · OpenAI API key · OpenWeatherMap API key
- Google Cloud project with Drive API (a **service account** is recommended over OAuth for the server)

### Setup

```bash
pip install -r requirements.txt   # at the repo root
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
# Optional — font for 議程 PDFs; auto-detected when omitted (see 議程 PDF above):
# AGENDA_FONT_PATH=/System/Library/Fonts/Supplemental/Arial Unicode.ttf
# Optional overrides (default to the GitHub Pages URLs):
# BULLETIN_BASE_URL=https://<user>.github.io/rotary-3523-liff/bulletin.html
# CALENDAR_BASE_URL=https://<user>.github.io/rotary-3523-liff/calendar.html
```

Google Drive auth — either:
- **Service account** (recommended): put the key at `backend/secrets/service_account.json` and share both Drive folders with its email; or
- **OAuth**: put `secrets/credentials.json`, then run `python reauth_drive.py` once to create `secrets/token.json` (expires if the OAuth app is in "Testing" mode).

Tables are created / migrated automatically on startup (`ensure_*` in `db.py`,
called from the app's `lifespan`) — adding a feature means adding an `ensure_*`
there, not a manual migration. **[`db.md`](db.md) documents every table**: what
it holds, who writes it, and the conventions (month keys, the `event_id` marker
inside `club_dues.customs`, `confirmed` vs `is_paid`, …). The RAG tables (`documents`, `document_rows`,
`document_metadata`, `personal_information`) still need the SQL from the
Supabase editor — see the git history for the DDL.

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
- Event `id` is referenced across registrations, check-in, stats, golf scores, seating, raffle and event-PDF filenames — keep it stable.

### Conventions worth keeping
- **No placeholder data in the UI.** When a call fails, show the error — never
  fall back to sample clubs, members or events. Chairs report these numbers
  externally, and a fake roster once let the exec secretary register
  non-existent `line_user_id`s.
- **Receipts are bot pushes, not `liff.sendMessages`.** The LIFF must never
  speak as the member: that puts words in their chat and the bot answers its own
  unrecognised text. Confirmations go out from the backend via `_push_receipt`
  to whoever performed the action, and are best-effort — a LINE outage must not
  fail an action that already succeeded.
- **Anything shown as verified must be verified.** No simulated GPS, no
  hardcoded venue coordinates: if the check can't run, it fails or is skipped
  explicitly.
- **Results belong on a page, not in a popup stream.** `index.html` is four
  full pages plus modals; anything a member has to read or scroll (event list,
  registrant list, award lookup, matchmaking hits) renders into a page section.
  Only two transient channels exist: `showToast` for "done" messages (text-only,
  auto-dismissing) and `showAlert` for receipts carrying numbers or names.
- **Deep links are a public contract.** `backend/app/main.py` bakes
  `?tab=…&action=…` into Flex messages already delivered to members, so the four
  page ids and every `action` in `handleDeepLink` must keep working. The
  scratchpad Playwright suite walks all 18 of them.
