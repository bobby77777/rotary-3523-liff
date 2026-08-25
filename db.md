# Database

Supabase **Postgres**. Every statement in the app goes through
`backend/app/db.py` — there is no ORM, no migration tool, and no SQL anywhere
else in the backend.

**Schema changes are code.** Each area has an `ensure_*_table()` function built
out of `CREATE TABLE IF NOT EXISTS` plus `ALTER TABLE … ADD COLUMN IF NOT
EXISTS`; they all run from the FastAPI `lifespan` hook in `main.py` on every
boot, so deploying the code migrates the database. Adding a column means adding
one `ALTER` line next to the others — never editing the `CREATE`, which no
existing database will ever re-run.

Two helpers, and the difference matters: **`query()` does not commit**, it is
for `SELECT` only. `execute()` commits. A `DELETE … RETURNING` sent through
`query()` reports success and changes nothing — that bug has been shipped here
before.

**There are no foreign keys** (except `document_rows.dataset_id`). Rows are
related by convention, so deleting a parent leaves orphans behind on purpose:
a resigned member's dues rows still have to reconcile last year's books.
`delete_event()` is the one place that cleans up after itself (`event_pdf`).

---

## Conventions

| Thing | How it is stored | Why it matters |
|---|---|---|
| Identity | `line_user_id` (LINE uid) everywhere; `personal_information.id` is never used as a key | A member with no LINE account yet gets a synthetic `manual_<uuid>` id from 名冊管理 |
| Club | `club_name` **text**, not an id | Renaming a club would orphan its rows |
| Month | `'YYYY-MM'` text | Sorts chronologically as a plain string; every finance query relies on that |
| Money | `integer`, NTD, no decimals | `club_opening_balance.amount` is `bigint` and may be negative |
| Timestamps | `timestamptz`, default `now()` | Registration months are converted `AT TIME ZONE 'Asia/Taipei'` before truncating — the server clock is not Taiwan's |
| JSON | `jsonb` in `club_dues.customs`, `document_rows.row_data`, `golf_scores.scores`, `user_state.context`; **plain TEXT** in `events.agenda` / `golf_plans` / `golf_holes` and `club_finance.data` / `bulletin_content.data` | The TEXT ones are `json.dumps`'d in Python and **cannot be queried or aggregated in SQL** |

---

## 身分與權限

### `personal_information` — 社友基本資料
`line_user_id` (unique) · `club_name` · `full_name` · `nickname` · `diet_type` ·
`spouse_name` · `left_at`

The root of "who is this and which club are they in". Everything else joins to
it by `line_user_id`.

- **`left_at` is the resignation marker.** `get_club_members()` and every roster,
  billing and registration list filter `left_at IS NULL`; 名冊管理 sets it
  instead of deleting, so old bills and 報名 records keep their names.
- Written by: the profile gate in the LIFF (`/me/profile`), 執秘 in 財務看板 →
  社友名冊 (`/finance/roster*`).

### `user_roles` — 管理權限與檢視範圍
`line_user_id` (PK) · `role` · `club_name` · `scope` · `district` · `all_districts`

| Column | Meaning |
|---|---|
| `role` | `member` = no admin. Anything else (`admin_all`, …) passes `is_admin()` |
| `club_name` | The admin's home club — what 財務看板 / 名冊 default to |
| `scope` | Default viewpoint: `club` or `district` |
| `district` | Which district they administer. Blank = derive it from their club |
| `all_districts` | **Cross-district super admin.** With `role='admin_all'` this is `is_super_admin()`: every district, every club |

`is_super_admin()` requires *both* `admin_all` and `all_districts` — the flag
widens a scope, it never grants one.

### `admin_users` — legacy
Seed list kept only as a fallback inside `is_admin()`. Do not add people here;
use `user_roles`.

### `bulletin_editors` — 誰能編社刊
Separate from the admin role on purpose (主委 edits the bulletin, 執秘 does not).

**Trap:** while the table is *empty*, any `admin_all` may edit (otherwise a new
club could never start). The moment one row exists, that fallback is off — an
admin who is not on the list loses bulletin access without any visible change.

### `user_state` — LINE 對話狀態
`line_user_id` (PK) · `state` · `context` (jsonb). Which step of a chat flow the
member is in. Empty in production today.

---

## 地區與社

### `districts`
`code` (PK, e.g. `3523`) · `name` · `short_name` · `website` · `notices_api` ·
`contact_email`

`notices_api` is the district site's WordPress REST endpoint that `notices.py`
polls for 【公文】; a district without one is skipped by the sync instead of
failing it.

### `clubs`
`club_name` (PK) · `district` · `full_name`

`full_name` (「台北松山扶輪社」) is what the bulletin masthead prints; the short
`club_name` is what every other table stores.

---

## 行事曆與活動

### `events` — 所有活動
The busiest table in the system: district events, club events, and every 公文
synced from the district website.

| Column | Notes |
|---|---|
| `scope` | `district` / `club`; `club_name` blank = shared by all clubs |
| `district` | Which district owns it. Defaults to `3523` because the table predates multi-district; isolation depends on it being right |
| `date`, `time`, `start_time`, `location`, `mc`, `chair`, `type`, `fee` | `fee` is free text written for humans (`每人 NT$800`, `每隊 3,000`) — see 報名費 below |
| `agenda` | TEXT JSON: the 議程 rows (`{content, speaker, duration, pdf}`) |
| `golf_plans`, `golf_holes` | TEXT JSON: 球場方案與收費, 抽洞 |
| `geo` | `"緯度,經度"`; set = check-in is gated on being within 100 m, blank = no location check |
| `source_url` | The 公文 post URL. Also the dedupe key for the sync |
| `pdf_url` | The Drive **folder** the district links (letter + 附件) |
| `notice_file_url` | The 公文 **letter itself**, resolved out of that folder — what the 公文 button opens |
| `fee_amount` | **Dead.** Left over from an auto-charge feature that was reverted twice; nothing reads it |

### `registrations` — 報名
`line_user_id` + `event_id` (unique together) · `payment_status` · `checked_in` /
`checked_in_at` · `bank_digits` · `registered_by` · `handicap` · `course_plan`

No club column and no month column — the club comes from
`personal_information`, and the month is derived from `created_at` in Taipei
time. `registered_by` is set when 執秘 registers someone else.

### `event_guests` — 來賓報名
Non-members (眷屬, 友社) that a member or 執秘 brings. Same fields as a
registration minus the identity.

### `event_pdf` — 舊版活動 PDF
`event_id` (PK) · `data` (bytea). Legacy blobs. New PDFs are either rendered on
demand from `agenda` or streamed from Drive; this is only a fallback in
`/events/{id}/pdf`.

### 現場作業 tables (all empty today)
| Table | Use |
|---|---|
| `event_vips` | 貴賓唱名: name, title, 到場, 已唱名 |
| `event_seating` | 年會桌次 (`event_id, table_no, seat_no` unique) |
| `event_prizes` / `event_raffle_winners` | 摸彩獎項與中獎名單 |
| `event_surveys` | 參加意願調查 + 催覆 (`event_id, line_user_id` unique) |
| `rye_applicants` | 青少年交換面試時段與同意書審核 |

### `golf_groups` / `golf_scores`
Groups are `(event_id, group_no, slot)` unique — one player per slot, so
dragging two players into the same slot cannot happen. Scores are one row per
player with a `jsonb` array of hole scores.

---

## 社費與財務

### `club_dues` — 每人每月的帳單
`(club_name, month, line_user_id)` unique.

| Column | Notes |
|---|---|
| `meal`, `iou` | 餐費 / IOU for that month |
| `customs` | jsonb `[{name, amount, event_id?}]` — 臨時費用. **`event_id` marks "this registration fee has already been billed"**; `_charged_events()` scans it so the same event is never offered twice, and `/dues/bulk_save` deliberately strips it (batch-billing everyone would mark a whole club as charged for an event a handful attended) |
| `is_paid` | The member reported a transfer |
| `confirmed` / `confirmed_at` | 執秘 matched it against the bank statement. **Only `confirmed` counts as income**; `confirmed_at` is written but never read |
| `bank_digits` | Last 5 digits of the transfer |

The month on the row is the month the money belongs to. A January bill
confirmed in April books as **January** income — every finance aggregate filters
`WHERE month = %s`.

Fixed monthly dues (常年月費 + 地區分攤金) are **not** stored here: a member with
no row still owes them, which is why 應收 counts people, not rows.

### `club_dues_settings` — 月費費率
`(club_name, effective_month)` PK · `base` · `district`. Rate segments; a bill
uses the latest segment with `effective_month <= month`, so changing the rate
never rewrites old bills.

### `club_finance` — 每月社務對帳表
`(club_name, month)` PK · `data` TEXT JSON:
`{rent, salary, fixed[{name, amount}], advances[{member, detail, amount}]}`

`advances` = 社友替社墊的錢 (club owes the member). Do not confuse it with the
地區活動代墊 line on 財務看板, which is the opposite direction (club paid for the
member) and is **computed from registrations, not stored**.

### `club_opening_balance` — 期初結餘
`club_name` **PK — one row per club**, not a history. The month the club started
keeping books here and how much was on hand. Carry-forward starts there and
re-adds every month since, on every board load; nothing is cached, so correcting
an old month propagates forward by itself.

### `club_bank_account` — 社的匯款帳號
Shown to members when they ask where to transfer. Empty today.

---

## 社刊

### `bulletin_content`
`event_id` (unique index) · `club_name` · `data` TEXT JSON — one published
bulletin per 例會. The editor writes the whole page layout in `data`; the
masthead (社名/期數/日期) is not stored, it is derived from the event.

### `bulletin_pdf`
Single-row table (`id = 1`) holding the last published PDF blob.

---

## 理監事

`board_members` (`club_name, line_user_id` PK) · `board_motions` (議案, `status`
open/closed) · `board_votes` (`motion_id, line_user_id` PK, one vote each).
Built, not yet in use.

---

## AI 與檢索

| Table | Use |
|---|---|
| `documents` | pgvector store: `content` + `metadata` + `embedding`. RAG answers in the LINE bot |
| `document_rows` | Rows of imported CSV/XLSX (得獎名單, 社友資料) as `jsonb`, queried by the agent's tools |
| `document_metadata` | The imported file's title / URL / column schema (`document_rows.dataset_id` → here — the only real FK) |
| `message_store` | LINE conversation history, keyed by `session_id` |
| `member_business` | 職業名片 (產業, 公司, 介紹, 可提供的協助) for AI 產業媒合 |

Filled by `backend/ingest.py`, which watches a Google Drive folder and re-embeds
changed files.

---

## Reading the money flow

Four tables answer "where is the club's money":

```
registrations ──(報名費, 解析自 events.fee)──► 支出：地區活動代墊   （社先墊給地區）
       │
       └─► club_dues.customs[{event_id}] ──► 社友的帳單 ──confirmed──► 收入
                                                                  │
club_finance (租金/薪資/固定/社友代墊款) ──────────────────► 支出 ─┘
                                                                  │
club_opening_balance ──► 期初 ──► 逐月結轉 ──► 上期結餘 ──────────┘ = 期末結餘
```

Nothing is charged automatically: a registration only becomes money when 執秘
presses 帶入 (or 全部帶入本月帳單) and saves. An earlier version that billed at
registration time was reverted twice — the 費用 text is written for humans, and
text that a regex misreads should never move money on its own.
