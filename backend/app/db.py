import json
import re
import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

from .config import DATABASE_URL

_pool: ThreadedConnectionPool | None = None


def _get_pool() -> ThreadedConnectionPool:
    global _pool
    if _pool is None:
        _pool = ThreadedConnectionPool(1, 10, DATABASE_URL)
    return _pool


def _get_conn():
    return _get_pool().getconn()


def _release_conn(conn):
    _get_pool().putconn(conn)


def query(sql: str, params=None) -> list[dict]:
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]
    finally:
        _release_conn(conn)


def execute(sql: str, params=None) -> None:
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _release_conn(conn)


def ensure_personal_information_columns() -> None:
    execute("ALTER TABLE personal_information ADD COLUMN IF NOT EXISTS spouse_name TEXT NOT NULL DEFAULT ''")


def upsert_personal_info(
    line_user_id: str,
    club_name: str,
    full_name: str,
    nickname: str,
    diet_type: str,
    spouse_name: str = "",
) -> None:
    """Blank spouse_name leaves any existing 寶尊眷 untouched (the member form never sends it)."""
    execute(
        """
        INSERT INTO personal_information (line_user_id, club_name, full_name, nickname, diet_type, spouse_name)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (line_user_id) DO UPDATE SET
            club_name   = EXCLUDED.club_name,
            full_name   = EXCLUDED.full_name,
            nickname    = EXCLUDED.nickname,
            diet_type   = EXCLUDED.diet_type,
            spouse_name = COALESCE(NULLIF(EXCLUDED.spouse_name, ''), personal_information.spouse_name)
        """,
        (line_user_id, club_name, full_name, nickname, diet_type, spouse_name),
    )


def vector_search(embedding: list[float], limit: int = 5) -> list[dict]:
    embedding_str = "[" + ",".join(map(str, embedding)) + "]"
    return query(
        """
        SELECT content, metadata
        FROM documents
        ORDER BY embedding <=> %s::vector
        LIMIT %s
        """,
        (embedding_str, limit),
    )


def _normalize(text: str) -> str:
    """Normalize punctuation variants and spacing so searches are dot-agnostic."""
    return (
        text.replace("・", "·")   # Japanese middle dot → standard middle dot
            .replace("•", "·")    # bullet → standard middle dot
            .replace("．", ".")   # fullwidth period → period
            .replace(" · ", "·")  # remove spaces around middle dot
    )


_AWARD_WHERE = """
    (%s = '' OR row_data->>'社名'     ILIKE %s)
    AND (%s = '' OR row_data->>'姓名'     ILIKE %s)
    AND (%s = '' OR REPLACE(row_data->>'獎項', ' · ', '·') ILIKE %s)
    AND (%s = '' OR row_data->>'Nickname' ILIKE %s)
    AND (%s = '' OR row_data->>'分區'     ILIKE %s)
    AND (%s = '' OR row_data->>'頒獎時段' ILIKE %s)
    AND (%s = '' OR row_data->>'備註'     ILIKE %s)
"""


def _award_params(club, person, award, nick, district, time_slot, notes):
    return (
        club,      f"%{club}%",
        person,    f"%{person}%",
        award,     f"%{award}%",
        nick,      f"%{nick}%",
        district,  f"%{district}%",
        time_slot, f"%{time_slot}%",
        notes,     f"%{notes}%",
    )


def search_document_rows(
    club: str, person: str, award: str, nick: str,
    district: str = "", time_slot: str = "", notes: str = "",
    limit: int = 50,
) -> list[dict]:
    club      = _normalize(club)
    person    = _normalize(person)
    award     = _normalize(award)
    nick      = _normalize(nick)
    district  = _normalize(district)
    time_slot = _normalize(time_slot)
    notes     = _normalize(notes)
    return query(
        f"""
        SELECT
            row_data->>'分區'     AS "分區",
            row_data->>'社名'     AS "社名",
            row_data->>'姓名'     AS "姓名",
            row_data->>'Nickname' AS "Nickname",
            row_data->>'獎項'     AS "獎項",
            row_data->>'頒獎時段' AS "頒獎時段",
            row_data->>'備註'     AS "備註",
            COUNT(*) OVER()       AS total_count
        FROM document_rows
        WHERE {_AWARD_WHERE}
        ORDER BY row_data->>'社名', row_data->>'Nickname'
        LIMIT %s
        """,
        (*_award_params(club, person, award, nick, district, time_slot, notes), limit),
    )


def search_awards(keyword: str, limit: int = 12) -> list[dict]:
    """Free-text award lookup matching 姓名 / Nickname / 社名 (OR)."""
    kw = _normalize(keyword)
    like = f"%{kw}%"
    return query(
        """
        SELECT row_data->>'分區'     AS "分區",
               row_data->>'社名'     AS "社名",
               row_data->>'姓名'     AS "姓名",
               row_data->>'Nickname' AS "Nickname",
               row_data->>'獎項'     AS "獎項",
               row_data->>'頒獎時段' AS "頒獎時段",
               row_data->>'備註'     AS "備註",
               COUNT(*) OVER()       AS total_count
        FROM document_rows
        WHERE row_data->>'姓名'     ILIKE %s
           OR row_data->>'Nickname' ILIKE %s
           OR row_data->>'社名'     ILIKE %s
        ORDER BY row_data->>'社名', row_data->>'姓名'
        LIMIT %s
        """,
        (like, like, like, limit),
    )


_AWARD_COLS = """row_data->>'分區'     AS "分區",
                 row_data->>'社名'     AS "社名",
                 row_data->>'姓名'     AS "姓名",
                 row_data->>'Nickname' AS "Nickname",
                 row_data->>'獎項'     AS "獎項",
                 row_data->>'頒獎時段' AS "頒獎時段",
                 row_data->>'備註'     AS "備註" """


def get_member_awards(line_user_id: str) -> list[dict]:
    """This member's own awards — matched by their 姓名 / Nickname from their profile."""
    pi = query("SELECT full_name, nickname FROM personal_information WHERE line_user_id = %s",
               (line_user_id,))
    if not pi:
        return []
    name = (pi[0].get("full_name") or "").strip()
    nick = (pi[0].get("nickname") or "").strip()
    if not name and not nick:
        return []
    return query(
        f"""
        SELECT {_AWARD_COLS}
        FROM document_rows
        WHERE (%s <> '' AND row_data->>'姓名' = %s)
           OR (%s <> '' AND row_data->>'Nickname' = %s)
        ORDER BY row_data->>'頒獎時段'
        """,
        (name, name, nick, nick),
    )


def get_club_awards(club_name: str) -> list[dict]:
    """All awards for a club — 社名 matched leniently (e.g. '松山' ⊂ '台北松山扶輪社')."""
    if not club_name:
        return []
    return query(
        f"""
        SELECT {_AWARD_COLS}
        FROM document_rows
        WHERE row_data->>'社名' ILIKE %s
        ORDER BY row_data->>'姓名'
        """,
        (f"%{club_name}%",),
    )


def get_award_stats(group_by: str, club: str = "", award: str = "", limit: int = 20) -> list[dict]:
    """Aggregation: count records grouped by a field. group_by must be one of 社名/Nickname/獎項/分區."""
    valid = {"社名", "Nickname", "獎項", "分區"}
    if group_by not in valid:
        return []
    club  = _normalize(club)
    award = _normalize(award)
    return query(
        f"""
        SELECT row_data->>'{group_by}' AS "{group_by}", COUNT(*) AS 得獎次數
        FROM document_rows
        WHERE
            (%s = '' OR row_data->>'社名' ILIKE %s)
            AND (%s = '' OR REPLACE(row_data->>'獎項', ' · ', '·') ILIKE %s)
        GROUP BY row_data->>'{group_by}'
        ORDER BY 得獎次數 DESC
        LIMIT %s
        """,
        (club, f"%{club}%", award, f"%{award}%", limit),
    )


def get_personal_info(line_user_id: str) -> list[dict]:
    return query(
        "SELECT * FROM personal_information WHERE line_user_id = %s",
        (line_user_id,),
    )


def list_document_metadata() -> list[dict]:
    return query("SELECT * FROM document_metadata")


def get_file_content(file_id: str) -> list[dict]:
    return query(
        """
        SELECT string_agg(content, ' ') AS document_text
        FROM documents
        WHERE metadata->>'file_id' = %s
        GROUP BY metadata->>'file_id'
        """,
        (file_id,),
    )


def ensure_message_store() -> None:
    execute("""
        CREATE TABLE IF NOT EXISTS message_store (
            id SERIAL PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)


def ensure_registrations_table() -> None:
    execute("""
        CREATE TABLE IF NOT EXISTS registrations (
            id SERIAL PRIMARY KEY,
            line_user_id TEXT NOT NULL,
            event_id INTEGER NOT NULL,
            payment_status TEXT NOT NULL DEFAULT 'unpaid',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(line_user_id, event_id)
        )
    """)
    execute("ALTER TABLE registrations ADD COLUMN IF NOT EXISTS checked_in BOOLEAN NOT NULL DEFAULT FALSE")
    execute("ALTER TABLE registrations ADD COLUMN IF NOT EXISTS checked_in_at TIMESTAMPTZ")
    execute("ALTER TABLE registrations ADD COLUMN IF NOT EXISTS bank_digits TEXT")
    execute("ALTER TABLE registrations ADD COLUMN IF NOT EXISTS registered_by TEXT")
    # 高球賽事報名時登錄的個人差點，供「報名差點」淨桿榜使用（與新貝利亞抽洞各算各的）。
    # NULL = 未登錄，該球員就不會出現在報名差點榜上。
    execute("ALTER TABLE registrations ADD COLUMN IF NOT EXISTS handicap REAL")
    # 高球賽事的球場方案代碼（A/B/C/D，見 main.GOLF_PLANS）。收費依方案不同，
    # 執秘對帳看的是這一欄。NULL = 非高球賽事或尚未選擇。
    execute("ALTER TABLE registrations ADD COLUMN IF NOT EXISTS course_plan TEXT")


def ensure_club_dues_table() -> None:
    execute("""
        CREATE TABLE IF NOT EXISTS club_dues (
            id SERIAL PRIMARY KEY,
            club_name    TEXT NOT NULL,
            month        TEXT NOT NULL,
            line_user_id TEXT NOT NULL,
            meal         INTEGER NOT NULL DEFAULT 0,
            iou          INTEGER NOT NULL DEFAULT 0,
            customs      JSONB   NOT NULL DEFAULT '[]',
            is_paid      BOOLEAN NOT NULL DEFAULT FALSE,
            bank_digits  TEXT,
            updated_at   TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(club_name, month, line_user_id)
        )
    """)
    # is_paid 是社友自己按「已匯款」回報的，不代表錢真的進來了。confirmed 才是執秘
    # 拿對帳單核對過的「已收訖」——收繳看板要分得出這兩者，才知道還有誰要追。
    execute("ALTER TABLE club_dues ADD COLUMN IF NOT EXISTS confirmed BOOLEAN NOT NULL DEFAULT FALSE")
    execute("ALTER TABLE club_dues ADD COLUMN IF NOT EXISTS confirmed_at TIMESTAMPTZ")


def get_dues(club_name: str, month: str, line_user_id: str) -> dict | None:
    rows = query(
        "SELECT meal, iou, customs, is_paid, bank_digits, confirmed FROM club_dues "
        "WHERE club_name = %s AND month = %s AND line_user_id = %s",
        (club_name, month, line_user_id),
    )
    return rows[0] if rows else None


def list_dues(club_name: str, month: str) -> list[dict]:
    """Every dues row a club has for one month, in one query — the 收繳看板 shows all
    members at once, and asking per member would be one round trip each."""
    return query(
        "SELECT line_user_id, meal, iou, customs, is_paid, bank_digits, confirmed "
        "FROM club_dues WHERE club_name = %s AND month = %s",
        (club_name, month),
    )


def upsert_dues(club_name: str, month: str, line_user_id: str,
                meal: int, iou: int, customs: list) -> None:
    """Secretary sets the fee items; existing is_paid / bank_digits are preserved."""
    execute(
        """
        INSERT INTO club_dues (club_name, month, line_user_id, meal, iou, customs, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (club_name, month, line_user_id) DO UPDATE SET
            meal = EXCLUDED.meal,
            iou = EXCLUDED.iou,
            customs = EXCLUDED.customs,
            updated_at = NOW()
        """,
        (club_name, month, line_user_id, meal, iou, json.dumps(customs)),
    )


def pay_dues(club_name: str, month: str, line_user_id: str, bank_digits: str) -> None:
    execute(
        """
        INSERT INTO club_dues (club_name, month, line_user_id, is_paid, bank_digits, updated_at)
        VALUES (%s, %s, %s, TRUE, %s, NOW())
        ON CONFLICT (club_name, month, line_user_id) DO UPDATE SET
            is_paid = TRUE,
            bank_digits = COALESCE(NULLIF(EXCLUDED.bank_digits, ''), club_dues.bank_digits),
            updated_at = NOW()
        """,
        (club_name, month, line_user_id, bank_digits or None),
    )


def confirm_dues(club_name: str, month: str, line_user_id: str, confirmed: bool) -> None:
    """執秘 marks a member's dues as reconciled against the bank statement.

    Confirming also sets is_paid: a member who paid cash at the meeting never
    reported anything, and leaving is_paid FALSE would keep nagging them to. The
    reverse doesn't hold — un-confirming a mistaken tick must not erase the
    member's own report, so is_paid is left alone on the way back.

    Only updates an existing bill; there is nothing to reconcile before 執秘 has
    produced one, and inserting here would create a bill with no line items."""
    execute(
        """
        UPDATE club_dues SET
            confirmed = %s,
            confirmed_at = CASE WHEN %s THEN NOW() ELSE NULL END,
            is_paid = CASE WHEN %s THEN TRUE ELSE is_paid END,
            updated_at = NOW()
        WHERE club_name = %s AND month = %s AND line_user_id = %s
        """,
        (confirmed, confirmed, confirmed, club_name, month, line_user_id),
    )


# ── 月費費率（常年月費 / 地區分攤金） ──────────────────────────────────────────
# 這兩筆由社章程與地區訂，全社一致，所以不存在每張帳單裡。但它們會變（換年度、
# 地區調整分攤金），而且必須「從某個月起」才變：直接改一個全域數字的話，過去
# 每一張已開立、社友也繳過的帳單金額都會跟著改，帳就對不回去了。因此存成一段
# 段生效期間，某個月適用的就是「生效月份 <= 該月」之中最新的那一段。

def ensure_club_dues_settings_table() -> None:
    execute("""
        CREATE TABLE IF NOT EXISTS club_dues_settings (
            club_name       TEXT NOT NULL,
            effective_month TEXT NOT NULL,
            base            INTEGER NOT NULL DEFAULT 0,
            district        INTEGER NOT NULL DEFAULT 0,
            updated_at      TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (club_name, effective_month)
        )
    """)


def get_dues_settings(club_name: str, month: str) -> dict | None:
    """The rates in force for `month` — None means no社 setting, use the defaults.

    "YYYY-MM" sorts the same as chronological order, so a plain string compare
    picks the right段 without any date parsing."""
    rows = query(
        "SELECT effective_month, base, district FROM club_dues_settings "
        "WHERE club_name = %s AND effective_month <= %s "
        "ORDER BY effective_month DESC LIMIT 1",
        (club_name, month),
    )
    return rows[0] if rows else None


def list_dues_settings(club_name: str) -> list[dict]:
    return query(
        "SELECT effective_month, base, district, updated_at FROM club_dues_settings "
        "WHERE club_name = %s ORDER BY effective_month DESC",
        (club_name,),
    )


def save_dues_settings(club_name: str, effective_month: str, base: int, district: int) -> None:
    execute(
        """
        INSERT INTO club_dues_settings (club_name, effective_month, base, district, updated_at)
        VALUES (%s, %s, %s, %s, NOW())
        ON CONFLICT (club_name, effective_month) DO UPDATE SET
            base = EXCLUDED.base,
            district = EXCLUDED.district,
            updated_at = NOW()
        """,
        (club_name, effective_month, base, district),
    )


def delete_dues_settings(club_name: str, effective_month: str) -> None:
    execute("DELETE FROM club_dues_settings WHERE club_name = %s AND effective_month = %s",
            (club_name, effective_month))


# ── 社的收款帳戶 ──────────────────────────────────────────────────────────────
# 社友要繳社費／活動費時得知道錢匯去哪。以前這件事只存在 LINE 群組的公告裡，
# 每個人翻紀錄找一次；現在存成資料，繳費畫面直接顯示。每社一組。

def ensure_club_bank_account_table() -> None:
    execute("""
        CREATE TABLE IF NOT EXISTS club_bank_account (
            club_name    TEXT PRIMARY KEY,
            bank_name    TEXT NOT NULL DEFAULT '',
            bank_code    TEXT NOT NULL DEFAULT '',
            branch       TEXT NOT NULL DEFAULT '',
            account_no   TEXT NOT NULL DEFAULT '',
            account_name TEXT NOT NULL DEFAULT '',
            note         TEXT NOT NULL DEFAULT '',
            updated_at   TIMESTAMPTZ DEFAULT NOW()
        )
    """)


def get_club_bank_account(club_name: str) -> dict | None:
    rows = query(
        "SELECT bank_name, bank_code, branch, account_no, account_name, note "
        "FROM club_bank_account WHERE club_name = %s",
        (club_name,),
    )
    return rows[0] if rows else None


def save_club_bank_account(club_name: str, data: dict) -> None:
    execute(
        """
        INSERT INTO club_bank_account
            (club_name, bank_name, bank_code, branch, account_no, account_name, note, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (club_name) DO UPDATE SET
            bank_name = EXCLUDED.bank_name,
            bank_code = EXCLUDED.bank_code,
            branch = EXCLUDED.branch,
            account_no = EXCLUDED.account_no,
            account_name = EXCLUDED.account_name,
            note = EXCLUDED.note,
            updated_at = NOW()
        """,
        (club_name, data.get("bank_name", ""), data.get("bank_code", ""),
         data.get("branch", ""), data.get("account_no", ""),
         data.get("account_name", ""), data.get("note", "")),
    )


def ensure_golf_scores_table() -> None:
    execute("""
        CREATE TABLE IF NOT EXISTS golf_scores (
            id SERIAL PRIMARY KEY,
            event_id INTEGER NOT NULL,
            line_user_id TEXT NOT NULL,
            player_name TEXT NOT NULL DEFAULT '',
            scores JSONB NOT NULL,
            updated_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(event_id, line_user_id)
        )
    """)
    # 送出成績當下的報名差點快照。結算過的淨桿不該因為之後有人改報名差點而變動，
    # 所以存下來，而不是每次查詢再去 join registrations。
    execute("ALTER TABLE golf_scores ADD COLUMN IF NOT EXISTS handicap REAL")


def upsert_golf_score(event_id: int, line_user_id: str, player_name: str, scores: list,
                      handicap: float | None = None) -> None:
    execute(
        """
        INSERT INTO golf_scores (event_id, line_user_id, player_name, scores, handicap, updated_at)
        VALUES (%s, %s, %s, %s, %s, NOW())
        ON CONFLICT (event_id, line_user_id) DO UPDATE SET
            player_name = EXCLUDED.player_name,
            scores = EXCLUDED.scores,
            handicap = COALESCE(EXCLUDED.handicap, golf_scores.handicap),
            updated_at = NOW()
        """,
        (event_id, line_user_id, player_name, json.dumps(scores), handicap),
    )


def get_golf_score(event_id: int, line_user_id: str) -> dict | None:
    rows = query(
        "SELECT player_name, scores FROM golf_scores WHERE event_id = %s AND line_user_id = %s",
        (event_id, line_user_id),
    )
    return rows[0] if rows else None


def get_golf_scores(event_id: int) -> list[dict]:
    return query(
        """
        SELECT g.line_user_id, g.player_name, g.scores, pi.club_name, pi.full_name,
               COALESCE(g.handicap, r.handicap) AS reg_handicap
        FROM golf_scores g
        LEFT JOIN personal_information pi ON pi.line_user_id = g.line_user_id
        LEFT JOIN registrations r
               ON r.line_user_id = g.line_user_id AND r.event_id = g.event_id
        WHERE g.event_id = %s
        """,
        (event_id,),
    )


def ensure_event_guests_table() -> None:
    execute("""
        CREATE TABLE IF NOT EXISTS event_guests (
            id SERIAL PRIMARY KEY,
            event_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            registered_by TEXT NOT NULL DEFAULT '',
            bank_digits TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    # 來賓的高球差點與球場方案。必須存在這裡而不是只存 golf_groups——重新分組會整批
    # 刪除重建，來賓沒有 registrations 可以回頭查，這兩項就會一按歸零。
    execute("ALTER TABLE event_guests ADD COLUMN IF NOT EXISTS handicap REAL")
    execute("ALTER TABLE event_guests ADD COLUMN IF NOT EXISTS course_plan TEXT")


def ensure_admin_users_table() -> None:
    execute("""
        CREATE TABLE IF NOT EXISTS admin_users (
            line_user_id TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)


def ensure_bulletin_editors_table() -> None:
    execute("""
        CREATE TABLE IF NOT EXISTS bulletin_editors (
            line_user_id TEXT PRIMARY KEY,
            name         TEXT NOT NULL DEFAULT '',
            created_at   TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    # Seed the original 社刊主委 so existing edit access is preserved after the move to DB.
    execute("""
        INSERT INTO bulletin_editors (line_user_id, name)
        VALUES ('U40fb26734c5a1da70261e06570830f01', '社刊主委')
        ON CONFLICT (line_user_id) DO NOTHING
    """)


def is_bulletin_editor(line_user_id: str) -> bool:
    if not line_user_id:
        return False
    rows = query("SELECT 1 FROM bulletin_editors WHERE line_user_id = %s", (line_user_id,))
    return len(rows) > 0


def add_bulletin_editor(line_user_id: str, name: str = "") -> None:
    execute(
        """
        INSERT INTO bulletin_editors (line_user_id, name)
        VALUES (%s, %s)
        ON CONFLICT (line_user_id) DO UPDATE SET name = EXCLUDED.name
        """,
        (line_user_id, name),
    )


def remove_bulletin_editor(line_user_id: str) -> None:
    execute("DELETE FROM bulletin_editors WHERE line_user_id = %s", (line_user_id,))


def list_bulletin_editors() -> list[dict]:
    return query("SELECT line_user_id, name FROM bulletin_editors ORDER BY created_at")


# ── Published bulletin content (per 例會 event) ───────────────────────────────────
# Each 例會 activity has its own 社刊 (content = four finished pages' HTML + theme).
# Keyed by event_id; club_name is kept so we can show a club's latest bulletin.
def ensure_bulletin_content_table() -> None:
    execute("""
        CREATE TABLE IF NOT EXISTS bulletin_content (
            event_id   INT,
            club_name  TEXT NOT NULL DEFAULT '',
            data       TEXT NOT NULL,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    # Migrate: earlier the 社刊 was one-per-club (no event_id). That doesn't map to the
    # new per-event model, so drop those legacy rows and re-key on event_id.
    execute("ALTER TABLE bulletin_content ADD COLUMN IF NOT EXISTS event_id INT")
    execute("ALTER TABLE bulletin_content ADD COLUMN IF NOT EXISTS club_name TEXT NOT NULL DEFAULT ''")
    execute("DELETE FROM bulletin_content WHERE event_id IS NULL")
    execute("DROP INDEX IF EXISTS bulletin_content_club_key")
    execute("ALTER TABLE bulletin_content DROP CONSTRAINT IF EXISTS bulletin_content_pkey")
    execute("ALTER TABLE bulletin_content DROP COLUMN IF EXISTS id")
    execute("CREATE UNIQUE INDEX IF NOT EXISTS bulletin_content_event_key "
            "ON bulletin_content (event_id)")


def save_bulletin_content(event_id: int, club_name: str, raw: str) -> None:
    execute(
        """
        INSERT INTO bulletin_content (event_id, club_name, data, updated_at)
        VALUES (%s, %s, %s, NOW())
        ON CONFLICT (event_id) DO UPDATE SET
            data = EXCLUDED.data, club_name = EXCLUDED.club_name, updated_at = NOW()
        """,
        (event_id, club_name, raw),
    )


def get_bulletin_content(event_id: int) -> str | None:
    rows = query("SELECT data FROM bulletin_content WHERE event_id = %s", (event_id,))
    return rows[0]["data"] if rows and rows[0]["data"] is not None else None


def get_club_latest_bulletin(club_name: str) -> str | None:
    """The club's most recently published 例會 社刊 (for the 每週社刊 tile)."""
    rows = query("SELECT data FROM bulletin_content WHERE club_name = %s "
                 "ORDER BY updated_at DESC LIMIT 1", (club_name,))
    return rows[0]["data"] if rows and rows[0]["data"] is not None else None


# ── Club finance sheet (社務對帳) ─────────────────────────────────────────────────
# One monthly finance sheet per club: fixed expenses (rent/salary/custom) + 社友代墊款.
# Stored as a JSON string keyed by (club_name, month 'YYYY-MM').
def ensure_club_finance_table() -> None:
    execute("""
        CREATE TABLE IF NOT EXISTS club_finance (
            club_name  TEXT NOT NULL,
            month      TEXT NOT NULL,
            data       TEXT NOT NULL DEFAULT '{}',
            updated_at TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (club_name, month)
        )
    """)


def get_club_finance(club_name: str, month: str) -> dict | None:
    rows = query("SELECT data FROM club_finance WHERE club_name = %s AND month = %s",
                 (club_name, month))
    if not rows or not rows[0]["data"]:
        return None
    try:
        return json.loads(rows[0]["data"])
    except (ValueError, TypeError):
        return None


def save_club_finance(club_name: str, month: str, data: dict) -> None:
    execute(
        """
        INSERT INTO club_finance (club_name, month, data, updated_at)
        VALUES (%s, %s, %s, NOW())
        ON CONFLICT (club_name, month) DO UPDATE SET data = EXCLUDED.data, updated_at = NOW()
        """,
        (club_name, month, json.dumps(data)),
    )


# ── Member business cards (AI 產業媒合) ───────────────────────────────────────────
# Each member's own professional card (industry / company / intro / 社友優惠). Members
# fill it in; industry matchmaking searches these to connect fellow Rotarians.
def ensure_member_business_table() -> None:
    execute("""
        CREATE TABLE IF NOT EXISTS member_business (
            line_user_id TEXT PRIMARY KEY,
            industry     TEXT NOT NULL DEFAULT '',
            company      TEXT NOT NULL DEFAULT '',
            intro        TEXT NOT NULL DEFAULT '',
            offer        TEXT NOT NULL DEFAULT '',
            updated_at   TIMESTAMPTZ DEFAULT NOW()
        )
    """)


def get_member_business(line_user_id: str) -> dict | None:
    rows = query("SELECT industry, company, intro, offer FROM member_business "
                 "WHERE line_user_id = %s", (line_user_id,))
    return rows[0] if rows else None


def save_member_business(line_user_id: str, industry: str, company: str,
                         intro: str, offer: str) -> None:
    execute(
        """
        INSERT INTO member_business (line_user_id, industry, company, intro, offer, updated_at)
        VALUES (%s, %s, %s, %s, %s, NOW())
        ON CONFLICT (line_user_id) DO UPDATE SET
            industry = EXCLUDED.industry, company = EXCLUDED.company,
            intro = EXCLUDED.intro, offer = EXCLUDED.offer, updated_at = NOW()
        """,
        (line_user_id, industry, company, intro, offer),
    )


def search_business(q: str, exclude_uid: str = "", limit: int = 10) -> list[dict]:
    """Match a free-text need against members' industry / company / intro. Uses Chinese
    bigrams (no word-segmenter needed) so '需要辦公室裝潢' still finds '室內裝潢設計';
    candidates are ranked by how many distinct bigrams they hit."""
    clean = re.sub(r"[\s,，、。;；:：/\\!！?？.\-（）()]+", "", q or "")
    grams = {clean[i:i + 2] for i in range(len(clean) - 1)}
    if not grams:
        grams = {clean} if clean else set()
    grams = list(grams)[:30]
    if not grams:
        return []
    conds, params = [], []
    for g in grams:
        like = f"%{g}%"
        conds.append("(b.industry ILIKE %s OR b.company ILIKE %s OR b.intro ILIKE %s)")
        params += [like, like, like]
    params += [exclude_uid]
    rows = query(
        f"""
        SELECT b.industry, b.company, b.intro, b.offer,
               p.full_name, p.nickname, p.club_name
        FROM member_business b
        JOIN personal_information p ON b.line_user_id = p.line_user_id
        WHERE ({' OR '.join(conds)}) AND b.line_user_id <> %s
        """,
        params,
    )

    def _score(r: dict) -> int:
        text = f"{r.get('industry','')}{r.get('company','')}{r.get('intro','')}"
        return sum(1 for g in grams if g in text)

    rows.sort(key=_score, reverse=True)
    return rows[:limit]


# ── Events (行事曆) ──────────────────────────────────────────────────────────────
# Editable event schedule so the 執秘 can maintain the calendar from the admin panel
# instead of us hardcoding it. scope ∈ {district, club}; club_name '' = shared by all
# clubs. weekday is derived from the date, so the editor only picks a date.
_WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
# Simple scalar text/date fields (agenda is handled separately as a JSON string).
_EVENT_FIELDS = ("scope", "club_name", "date", "title", "location",
                 "chair", "time", "type", "fee", "pdf_url", "start_time", "mc", "geo",
                 "source_url")


def ensure_events_table() -> None:
    execute("""
        CREATE TABLE IF NOT EXISTS events (
            id         SERIAL PRIMARY KEY,
            scope      TEXT NOT NULL DEFAULT 'district',
            club_name  TEXT NOT NULL DEFAULT '',
            date       DATE,
            title      TEXT NOT NULL DEFAULT '',
            location   TEXT NOT NULL DEFAULT '',
            chair      TEXT NOT NULL DEFAULT '',
            time       TEXT NOT NULL DEFAULT '',
            type       TEXT NOT NULL DEFAULT '',
            fee        TEXT NOT NULL DEFAULT '',
            pdf_url    TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    # 議程編輯器（calendar.html）需要的欄位：開始時間、司儀、以及議程流程表(JSON 字串)。
    execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS start_time TEXT NOT NULL DEFAULT ''")
    execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS mc TEXT NOT NULL DEFAULT ''")
    execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS agenda TEXT NOT NULL DEFAULT '[]'")
    # 高球抽洞：主委抽出的隱藏洞（0-based index 的 JSON 陣列），空 = 尚未抽、用預設。
    execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS golf_holes TEXT NOT NULL DEFAULT '[]'")
    # 高球賽事的球場方案與收費（JSON 陣列，見 main._normalize_golf_plans）。價格因球場、
    # 因場次而異，所以跟著活動走，不寫在程式裡。空陣列 = 這場不分方案。
    execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS golf_plans TEXT NOT NULL DEFAULT '[]'")
    # 會場座標 "緯度,經度"，供報到的 LBS 距離驗證用；留空 = 該活動不做定位驗證。
    execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS geo TEXT NOT NULL DEFAULT ''")
    # 公文自動同步（notices.py）：來源貼文網址，當作去重鍵——已同步過的公文不再重覆新增。
    # 空字串 = 人工在行事曆建立的活動，跟自動抓來的公文區分開。
    execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS source_url TEXT NOT NULL DEFAULT ''")


def events_count() -> int:
    rows = query("SELECT COUNT(*) AS n FROM events")
    return rows[0]["n"] if rows else 0


def seed_events(rows: list[dict]) -> None:
    """One-time migration of the previously-hardcoded schedule, preserving ids."""
    for e in rows:
        execute(
            """
            INSERT INTO events (id, scope, club_name, date, title, location,
                                chair, time, type, fee, pdf_url)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (id) DO NOTHING
            """,
            (e.get("id"), e.get("scope", "district"), e.get("club_name", ""),
             e.get("date") or None, e.get("title", ""), e.get("location", ""),
             e.get("chair", ""), e.get("time", ""), e.get("type", ""),
             e.get("fee", ""), e.get("pdf_url", "")),
        )
    # Keep the SERIAL sequence above the highest explicit id we just inserted.
    execute("SELECT setval(pg_get_serial_sequence('events','id'), "
            "GREATEST((SELECT MAX(id) FROM events), 1))")


def _row_to_event(r: dict) -> dict:
    d = r.get("date")
    iso = d.isoformat() if d else ""
    try:
        agenda = json.loads(r.get("agenda") or "[]")
    except (ValueError, TypeError):
        agenda = []
    try:
        golf_holes = json.loads(r.get("golf_holes") or "[]")
    except (ValueError, TypeError):
        golf_holes = []
    try:
        golf_plans = json.loads(r.get("golf_plans") or "[]")
    except (ValueError, TypeError):
        golf_plans = []
    return {
        "id": r["id"], "scope": r["scope"], "club_name": r["club_name"],
        "date": iso, "displayDate": iso,
        "weekday": _WEEKDAYS[d.weekday()] if d else "",
        "title": r["title"], "location": r["location"], "chair": r["chair"],
        "time": r["time"], "type": r["type"], "fee": r["fee"],
        "pdf_url": r["pdf_url"],
        "start_time": r.get("start_time") or "",
        "mc": r.get("mc") or "",
        "geo": r.get("geo") or "",
        "source_url": r.get("source_url") or "",
        "agenda": agenda,
        "golf_holes": golf_holes,
        "golf_plans": golf_plans,
    }


def save_golf_holes(event_id: int, holes: list) -> None:
    """Store the drawn hidden holes (0-based indices) for a golf event."""
    execute("UPDATE events SET golf_holes = %s, updated_at = NOW() WHERE id = %s",
            (json.dumps(list(holes)), event_id))


def list_events(scope: str = "", club_name: str = "") -> list[dict]:
    if scope == "club":
        rows = query("SELECT * FROM events WHERE scope='club' "
                     "AND (club_name='' OR club_name=%s) ORDER BY date", (club_name,))
    elif scope == "district":
        rows = query("SELECT * FROM events WHERE scope='district' ORDER BY date")
    else:
        rows = query("SELECT * FROM events ORDER BY date")
    return [_row_to_event(r) for r in rows]


def get_event(event_id: int) -> dict | None:
    rows = query("SELECT * FROM events WHERE id = %s", (event_id,))
    return _row_to_event(rows[0]) if rows else None


def event_source_urls() -> set[str]:
    """Post URLs of events already synced from an external source (notices.py),
    so a re-sync skips them instead of inserting duplicates."""
    rows = query("SELECT source_url FROM events WHERE source_url <> ''")
    return {r["source_url"] for r in rows}


def notice_events_missing_details() -> list[dict]:
    """Synced 公文 that carry nothing read out of their PDF — no 地點/時間/費用, so
    their date is still the post's publish date. notices.py retries these."""
    rows = query(
        """
        SELECT id, title, date, pdf_url FROM events
        WHERE type = '公文' AND source_url <> ''
          AND location = '' AND time = '' AND fee = ''
        ORDER BY id
        """)
    return [dict(r) for r in rows]


def create_event(data: dict) -> dict:
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO events (scope, club_name, date, title, location,
                                    chair, time, type, fee, pdf_url,
                                    start_time, mc, geo, agenda, golf_plans, source_url)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING *
                """,
                (data.get("scope", "district"), data.get("club_name", ""),
                 data.get("date") or None, data.get("title", ""), data.get("location", ""),
                 data.get("chair", ""), data.get("time", ""), data.get("type", ""),
                 data.get("fee", ""), data.get("pdf_url", ""),
                 data.get("start_time", ""), data.get("mc", ""), data.get("geo", ""),
                 json.dumps(data.get("agenda") or []),
                 json.dumps(data.get("golf_plans") or []),
                 data.get("source_url", "")),
            )
            row = cur.fetchone()
        conn.commit()
        return _row_to_event(dict(row))
    except Exception:
        conn.rollback()
        raise
    finally:
        _release_conn(conn)


def update_event(event_id: int, data: dict) -> dict | None:
    fields = [f for f in _EVENT_FIELDS if f in data]  # whitelist → safe to interpolate
    sets = [f"{f} = %s" for f in fields]
    vals = [(data[f] or None) if f == "date" else data[f] for f in fields]
    for jf in ("agenda", "golf_plans"):   # stored as JSON strings
        if jf in data:
            sets.append(f"{jf} = %s")
            vals.append(json.dumps(data[jf] or []))
    if not sets:
        return get_event(event_id)
    vals.append(event_id)
    execute(f"UPDATE events SET {', '.join(sets)}, updated_at = NOW() WHERE id = %s", vals)
    return get_event(event_id)


def delete_event(event_id: int) -> None:
    execute("DELETE FROM events WHERE id = %s", (event_id,))
    execute("DELETE FROM event_pdf WHERE event_id = %s", (event_id,))


# ── Per-event stored PDF ─────────────────────────────────────────────────────────
# The agenda PDF generated in calendar.html on save is stored here (bytes), so the
# event's PDF button (GET /events/{id}/pdf) can serve it without Google Drive.
def ensure_event_pdf_table() -> None:
    execute("""
        CREATE TABLE IF NOT EXISTS event_pdf (
            event_id   INT PRIMARY KEY,
            data       BYTEA NOT NULL,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)


def save_event_pdf(event_id: int, data: bytes) -> None:
    execute(
        """
        INSERT INTO event_pdf (event_id, data, updated_at)
        VALUES (%s, %s, NOW())
        ON CONFLICT (event_id) DO UPDATE SET data = EXCLUDED.data, updated_at = NOW()
        """,
        (event_id, psycopg2.Binary(data)),
    )


def get_event_pdf(event_id: int) -> bytes | None:
    rows = query("SELECT data FROM event_pdf WHERE event_id = %s", (event_id,))
    return bytes(rows[0]["data"]) if rows and rows[0]["data"] is not None else None


def event_pdf_ids() -> set[int]:
    return {r["event_id"] for r in query("SELECT event_id FROM event_pdf")}


def ensure_user_state_table() -> None:
    execute("""
        CREATE TABLE IF NOT EXISTS user_state (
            line_user_id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            context JSONB NOT NULL DEFAULT '{}',
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)


def register_event(line_user_id: str, event_id: int) -> bool:
    """Returns True if newly registered, False if already exists."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO registrations (line_user_id, event_id)
                VALUES (%s, %s)
                ON CONFLICT (line_user_id, event_id) DO NOTHING
                RETURNING id
                """,
                (line_user_id, event_id),
            )
            result = cur.fetchone()
        conn.commit()
        return result is not None
    except Exception:
        conn.rollback()
        raise
    finally:
        _release_conn(conn)


def report_payment(line_user_id: str, event_id: int, bank_digits: str = "",
                   handicap: float | None = None, course_plan: str | None = None) -> dict:
    """Ensure the member is registered and record their transfer digits.
    With digits -> payment_status 'uploaded'; without -> keep/register as unpaid.
    handicap (高球差點) and course_plan (球場方案) are only overwritten when a
    new value is supplied. Returns {'was_registered': bool}."""
    existing = get_registration(line_user_id, event_id)
    if bank_digits:
        status = "uploaded"
    else:
        status = existing["payment_status"] if existing else "unpaid"
    execute(
        """
        INSERT INTO registrations (line_user_id, event_id, payment_status, bank_digits,
                                   handicap, course_plan)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (line_user_id, event_id) DO UPDATE SET
            payment_status = EXCLUDED.payment_status,
            bank_digits = COALESCE(NULLIF(EXCLUDED.bank_digits, ''), registrations.bank_digits),
            handicap = COALESCE(EXCLUDED.handicap, registrations.handicap),
            course_plan = COALESCE(EXCLUDED.course_plan, registrations.course_plan)
        """,
        (line_user_id, event_id, status, bank_digits or None, handicap, course_plan),
    )
    return {"was_registered": existing is not None}


def cancel_registration(line_user_id: str, event_id: int) -> None:
    execute(
        "DELETE FROM registrations WHERE line_user_id = %s AND event_id = %s",
        (line_user_id, event_id),
    )


def get_registrations(line_user_id: str) -> list[dict]:
    return query(
        "SELECT * FROM registrations WHERE line_user_id = %s ORDER BY created_at DESC",
        (line_user_id,),
    )


def get_registration(line_user_id: str, event_id: int) -> dict | None:
    rows = query(
        "SELECT * FROM registrations WHERE line_user_id = %s AND event_id = %s",
        (line_user_id, event_id),
    )
    return rows[0] if rows else None


def get_event_registration_count(event_id: int) -> int:
    rows = query(
        "SELECT COUNT(*) AS cnt FROM registrations WHERE event_id = %s",
        (event_id,),
    )
    return rows[0]["cnt"] if rows else 0


def get_event_registrants(event_id: int, club_name: str = "") -> list[dict]:
    """Members registered for an event (optionally limited to one club)."""
    sql = """
        SELECT r.line_user_id,
               COALESCE(pi.full_name, '(未綁定)') AS full_name,
               pi.nickname,
               COALESCE(pi.club_name, '') AS club_name,
               r.checked_in,
               r.payment_status,
               r.registered_by,
               r.handicap,
               r.course_plan
        FROM registrations r
        LEFT JOIN personal_information pi ON pi.line_user_id = r.line_user_id
        WHERE r.event_id = %s
    """
    params: tuple = (event_id,)
    if club_name:
        sql += " AND pi.club_name = %s"
        params = (event_id, club_name)
    sql += " ORDER BY pi.club_name, pi.full_name"
    return query(sql, params)


def get_event_checkin_count(event_id: int) -> int:
    rows = query(
        "SELECT COUNT(*) AS cnt FROM registrations WHERE event_id = %s AND checked_in = TRUE",
        (event_id,),
    )
    return rows[0]["cnt"] if rows else 0


def get_event_stats(event_id: int) -> dict:
    """Per-club registration / payment / check-in breakdown for one event."""
    clubs = query(
        """
        SELECT COALESCE(NULLIF(pi.club_name, ''), '（未綁定社籍）') AS club_name,
               COUNT(*)                                              AS registered,
               COUNT(*) FILTER (WHERE r.payment_status = 'confirmed') AS paid,
               COUNT(*) FILTER (WHERE r.checked_in)                   AS checked_in
        FROM registrations r
        LEFT JOIN personal_information pi ON pi.line_user_id = r.line_user_id
        WHERE r.event_id = %s
        GROUP BY COALESCE(NULLIF(pi.club_name, ''), '（未綁定社籍）')
        ORDER BY registered DESC, club_name ASC
        """,
        (event_id,),
    )
    registered = sum(c["registered"] for c in clubs)
    paid       = sum(c["paid"] for c in clubs)
    checked_in = sum(c["checked_in"] for c in clubs)
    return {
        "kpi": {
            "registered": registered,
            "checked_in": checked_in,
            "paid":       paid,
            "unpaid":     registered - paid,
        },
        "clubs": [
            {
                "club_name":  c["club_name"],
                "registered": c["registered"],
                "paid":       c["paid"],
                "checked_in": c["checked_in"],
                "rate":       round(c["checked_in"] / c["registered"] * 100) if c["registered"] else 0,
            }
            for c in clubs
        ],
    }


def check_in(line_user_id: str, event_id: int) -> str:
    """Mark an attendee checked-in for an event.
    Returns: 'ok' (newly checked in), 'already' (was already), 'not_registered'."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT checked_in FROM registrations WHERE line_user_id = %s AND event_id = %s",
                (line_user_id, event_id),
            )
            row = cur.fetchone()
            if row is None:
                return "not_registered"
            if row[0]:
                return "already"
            cur.execute(
                "UPDATE registrations SET checked_in = TRUE, checked_in_at = NOW() "
                "WHERE line_user_id = %s AND event_id = %s",
                (line_user_id, event_id),
            )
        conn.commit()
        return "ok"
    except Exception:
        conn.rollback()
        raise
    finally:
        _release_conn(conn)


def is_admin(line_user_id: str) -> bool:
    """Any non-member role (or legacy admin_users seed) may enter the admin tab."""
    if get_user_role(line_user_id) != "member":
        return True
    rows = query("SELECT 1 FROM admin_users WHERE line_user_id = %s", (line_user_id,))
    return len(rows) > 0


# ── Roles & viewpoint scope ────────────────────────────────────────────────────
# role ∈ {member, chair_club_golf, chair_club_admin, chair_rye, chair_golf,
#         chair_annual, admin_all}   ·   scope ∈ {district, club}

def ensure_user_roles_table() -> None:
    execute("""
        CREATE TABLE IF NOT EXISTS user_roles (
            line_user_id TEXT PRIMARY KEY,
            role         TEXT NOT NULL DEFAULT 'member',
            club_name    TEXT NOT NULL DEFAULT '',
            scope        TEXT NOT NULL DEFAULT 'district',
            updated_at   TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    # Bridge legacy admin_users seed → admin_all role
    execute("""
        INSERT INTO user_roles (line_user_id, role)
        SELECT line_user_id, 'admin_all' FROM admin_users
        ON CONFLICT (line_user_id) DO NOTHING
    """)


def get_user_role(line_user_id: str) -> str:
    rows = query("SELECT role FROM user_roles WHERE line_user_id = %s", (line_user_id,))
    return rows[0]["role"] if rows else "member"


def set_user_role(line_user_id: str, role: str, club_name: str = "", scope: str = "") -> None:
    """Blank scope keeps the user's current viewpoint; new rows default to district."""
    execute(
        """
        INSERT INTO user_roles (line_user_id, role, club_name, scope, updated_at)
        VALUES (%s, %s, %s, COALESCE(NULLIF(%s, ''), 'district'), NOW())
        ON CONFLICT (line_user_id) DO UPDATE SET
            role = EXCLUDED.role,
            club_name = EXCLUDED.club_name,
            scope = COALESCE(NULLIF(%s, ''), user_roles.scope),
            updated_at = NOW()
        """,
        (line_user_id, role, club_name, scope, scope),
    )


def get_user_scope(line_user_id: str) -> str:
    rows = query("SELECT scope FROM user_roles WHERE line_user_id = %s", (line_user_id,))
    return rows[0]["scope"] if rows else "district"


def get_user_club(line_user_id: str) -> str:
    rows = query("SELECT club_name FROM user_roles WHERE line_user_id = %s", (line_user_id,))
    if rows and rows[0]["club_name"]:
        return rows[0]["club_name"]
    pi = query("SELECT club_name FROM personal_information WHERE line_user_id = %s", (line_user_id,))
    return pi[0]["club_name"] if pi else ""


def get_user_state(line_user_id: str) -> dict | None:
    rows = query(
        "SELECT state, context FROM user_state WHERE line_user_id = %s",
        (line_user_id,),
    )
    return rows[0] if rows else None


def set_user_state(line_user_id: str, state: str, context: dict | None = None) -> None:
    execute(
        """
        INSERT INTO user_state (line_user_id, state, context, updated_at)
        VALUES (%s, %s, %s, NOW())
        ON CONFLICT (line_user_id) DO UPDATE SET
            state = EXCLUDED.state,
            context = EXCLUDED.context,
            updated_at = NOW()
        """,
        (line_user_id, state, json.dumps(context or {})),
    )


def clear_user_state(line_user_id: str) -> None:
    execute("DELETE FROM user_state WHERE line_user_id = %s", (line_user_id,))


def get_all_user_ids() -> list[str]:
    rows = query("SELECT DISTINCT line_user_id FROM personal_information")
    return [r["line_user_id"] for r in rows]


def get_member_attendance(line_user_id: str) -> list[dict]:
    """All of one member's registrations with check-in flag, newest first."""
    return query(
        "SELECT event_id, checked_in, checked_in_at, payment_status, handicap, course_plan "
        "FROM registrations WHERE line_user_id = %s ORDER BY event_id DESC",
        (line_user_id,),
    )


def get_club_attendance(club_name: str) -> list[dict]:
    """Per-member registered / attended counts for a club (attendance leaderboard)."""
    return query(
        """
        SELECT pi.line_user_id,
               pi.full_name,
               pi.nickname,
               COUNT(r.id)                             AS registered,
               COUNT(r.id) FILTER (WHERE r.checked_in) AS attended
        FROM personal_information pi
        LEFT JOIN registrations r ON r.line_user_id = pi.line_user_id
        WHERE pi.club_name = %s
        GROUP BY pi.line_user_id, pi.full_name, pi.nickname
        ORDER BY attended DESC, registered DESC, pi.full_name
        """,
        (club_name,),
    )


def list_clubs() -> list[str]:
    rows = query(
        "SELECT DISTINCT club_name FROM personal_information "
        "WHERE club_name IS NOT NULL AND club_name <> '' ORDER BY club_name"
    )
    return [r["club_name"] for r in rows]


def get_club_members(club_name: str) -> list[dict]:
    return query(
        "SELECT line_user_id, full_name, nickname FROM personal_information "
        "WHERE club_name = %s ORDER BY full_name",
        (club_name,),
    )


def bulk_register(uids: list[str], event_id: int, bank_digits: str = "",
                  registered_by: str = "", handicaps: dict | None = None,
                  course_plans: dict | None = None) -> dict:
    """Register many members for an event at once. Returns {'new': n, 'dup': n}.
    handicaps / course_plans map line_user_id -> 高球差點 / 球場方案代碼; a member
    already registered still gets those updated, so 執秘 can fix a wrong value
    without re-registering."""
    handicaps = handicaps or {}
    course_plans = course_plans or {}
    new_count = 0
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            for uid in uids:
                cur.execute(
                    """
                    INSERT INTO registrations (line_user_id, event_id, payment_status,
                                               bank_digits, registered_by, handicap, course_plan)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (line_user_id, event_id) DO NOTHING
                    RETURNING id
                    """,
                    (uid, event_id, "uploaded" if bank_digits else "unpaid",
                     bank_digits or None, registered_by or None,
                     handicaps.get(uid), course_plans.get(uid)),
                )
                if cur.fetchone() is not None:
                    new_count += 1
                elif handicaps.get(uid) is not None or course_plans.get(uid) is not None:
                    cur.execute(
                        "UPDATE registrations SET "
                        "handicap    = COALESCE(%s, handicap), "
                        "course_plan = COALESCE(%s, course_plan) "
                        "WHERE line_user_id = %s AND event_id = %s",
                        (handicaps.get(uid), course_plans.get(uid), uid, event_id),
                    )
        conn.commit()
        return {"new": new_count, "dup": len(uids) - new_count}
    except Exception:
        conn.rollback()
        raise
    finally:
        _release_conn(conn)


def add_event_guests(event_id: int, names: list, registered_by: str = "",
                     bank_digits: str = "") -> int:
    """names 可以是字串，也可以是 {'name': ..., 'handicap': ..., 'course_plan': ...}
    （高球賽事用）。"""
    rows = []
    for g in names:
        item = g if isinstance(g, dict) else {"name": g}
        nm = str(item.get("name", "")).strip()
        if nm:
            rows.append((event_id, nm, registered_by, bank_digits or None,
                         item.get("handicap"), item.get("course_plan")))
    if not rows:
        return 0
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(
                cur,
                "INSERT INTO event_guests (event_id, name, registered_by, bank_digits, handicap, course_plan) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                rows,
            )
        conn.commit()
        return len(rows)
    except Exception:
        conn.rollback()
        raise
    finally:
        _release_conn(conn)


# ── 待繳費明細 / 催繳 ──────────────────────────────────────────────────────────

def get_event_unpaid(event_id: int, club_name: str = "") -> list[dict]:
    """Registrants who have not been marked 已收繳費 for an event.
    'uploaded' (回報了末 5 碼、等對帳) is still outstanding, but must not be chased."""
    return query(
        """
        SELECT r.line_user_id,
               r.payment_status,
               COALESCE(NULLIF(pi.club_name, ''), '（未綁定社籍）') AS club_name,
               COALESCE(pi.full_name, '') AS full_name,
               COALESCE(pi.nickname, '')  AS nickname
        FROM registrations r
        LEFT JOIN personal_information pi ON pi.line_user_id = r.line_user_id
        WHERE r.event_id = %s
          AND r.payment_status <> 'confirmed'
          AND (%s = '' OR COALESCE(NULLIF(pi.club_name, ''), '（未綁定社籍）') = %s)
        ORDER BY club_name, full_name
        """,
        (event_id, club_name, club_name),
    )


# ── 貴賓唱名 ───────────────────────────────────────────────────────────────────

def ensure_event_vips_table() -> None:
    execute("""
        CREATE TABLE IF NOT EXISTS event_vips (
            id SERIAL PRIMARY KEY,
            event_id    INTEGER NOT NULL,
            name        TEXT NOT NULL,
            title       TEXT NOT NULL DEFAULT '',
            sort_order  INTEGER NOT NULL DEFAULT 0,
            arrived     BOOLEAN NOT NULL DEFAULT FALSE,
            arrive_time TEXT NOT NULL DEFAULT '',
            is_called   BOOLEAN NOT NULL DEFAULT FALSE,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    execute("CREATE INDEX IF NOT EXISTS event_vips_event_idx ON event_vips (event_id)")


def list_event_vips(event_id: int) -> list[dict]:
    return query(
        "SELECT id, name, title, sort_order, arrived, arrive_time, is_called "
        "FROM event_vips WHERE event_id = %s ORDER BY sort_order, id",
        (event_id,),
    )


_VIP_COLS = "id, event_id, name, title, sort_order, arrived, arrive_time, is_called"


def _write_returning(sql: str, params) -> dict | None:
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None
    except Exception:
        conn.rollback()
        raise
    finally:
        _release_conn(conn)


def add_event_vip(event_id: int, name: str, title: str) -> dict:
    return _write_returning(
        f"""
        INSERT INTO event_vips (event_id, name, title, sort_order)
        VALUES (%s, %s, %s,
                COALESCE((SELECT MAX(sort_order) + 1 FROM event_vips WHERE event_id = %s), 1))
        RETURNING {_VIP_COLS}
        """,
        (event_id, name, title, event_id),
    )


def update_event_vip(vip_id: int, fields: dict) -> dict | None:
    allowed = ("name", "title", "sort_order", "arrived", "arrive_time", "is_called")
    sets = [f"{k} = %s" for k in allowed if k in fields]   # whitelist → safe to interpolate
    if not sets:
        return None
    params = [fields[k] for k in allowed if k in fields] + [vip_id]
    return _write_returning(
        f"UPDATE event_vips SET {', '.join(sets)} WHERE id = %s RETURNING {_VIP_COLS}",
        params,
    )


def delete_event_vip(vip_id: int) -> None:
    execute("DELETE FROM event_vips WHERE id = %s", (vip_id,))


# ── 高球即時調組 ───────────────────────────────────────────────────────────────

def ensure_golf_groups_table() -> None:
    execute("""
        CREATE TABLE IF NOT EXISTS golf_groups (
            id SERIAL PRIMARY KEY,
            event_id     INTEGER NOT NULL,
            group_no     INTEGER NOT NULL,
            slot         INTEGER NOT NULL,
            player_name  TEXT NOT NULL DEFAULT '',
            line_user_id TEXT NOT NULL DEFAULT '',
            UNIQUE(event_id, group_no, slot)
        )
    """)
    # 分組表上那個人的差點，分組當下從報名資料帶入。存在這裡，來賓（沒有報名紀錄）
    # 也能有差點，分組表不必再依賴 join 才湊得出來。
    execute("ALTER TABLE golf_groups ADD COLUMN IF NOT EXISTS handicap REAL")
    # 來賓沒有 line_user_id，靠這欄才認得回 event_guests；在分組表上改差點時，
    # 才知道要把來賓的差點寫回哪一筆（不然只能用姓名猜，改過名字就對不上）。
    execute("ALTER TABLE golf_groups ADD COLUMN IF NOT EXISTS guest_id INTEGER")


def list_golf_groups(event_id: int) -> list[dict]:
    # 分組表要看得到差點與方案（分組本來就參考差點），所以帶上報名資料。
    # 來賓沒有 line_user_id，報名那邊 join 不到，改用 guest_id 接回來賓資料。
    return query(
        """
        SELECT g.id, g.group_no, g.slot, g.player_name, g.line_user_id,
               COALESCE(g.handicap, r.handicap, eg.handicap) AS handicap,
               COALESCE(r.course_plan, eg.course_plan) AS course_plan,
               pi.club_name
        FROM golf_groups g
        LEFT JOIN registrations r
               ON r.line_user_id = NULLIF(g.line_user_id, '') AND r.event_id = g.event_id
        LEFT JOIN event_guests eg ON eg.id = g.guest_id
        LEFT JOIN personal_information pi ON pi.line_user_id = NULLIF(g.line_user_id, '')
        WHERE g.event_id = %s
        ORDER BY g.group_no, g.slot
        """,
        (event_id,),
    )


def replace_golf_groups(event_id: int, players: list[dict], per_group: int = 4) -> int:
    """Redraw the whole grouping from an ordered player list (4 per 組 by default)."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM golf_groups WHERE event_id = %s", (event_id,))
            for i, p in enumerate(players):
                cur.execute(
                    "INSERT INTO golf_groups (event_id, group_no, slot, player_name, line_user_id, handicap, guest_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (event_id, i // per_group + 1, i % per_group + 1,
                     p.get("name", ""), p.get("uid", ""), p.get("handicap"), p.get("guest_id")),
                )
        conn.commit()
        return len(players)
    except Exception:
        conn.rollback()
        raise
    finally:
        _release_conn(conn)


def swap_golf_players(event_id: int, id_a: int, id_b: int) -> tuple[dict, dict] | None:
    """Swap two players' seats, so each keeps the other's 組別/順位."""
    return _swap_rows("golf_groups", ("group_no", "slot"), event_id, id_a, id_b)


def get_golf_player(event_id: int, row_id: int) -> dict | None:
    # 帶上目前的方案（社友看報名、來賓看來賓資料），編輯時沒送方案就照原樣留著。
    rows = query(
        """
        SELECT g.*, COALESCE(r.course_plan, eg.course_plan) AS course_plan
        FROM golf_groups g
        LEFT JOIN registrations r
               ON r.line_user_id = NULLIF(g.line_user_id, '') AND r.event_id = g.event_id
        LEFT JOIN event_guests eg ON eg.id = g.guest_id
        WHERE g.event_id = %s AND g.id = %s
        """,
        (event_id, row_id),
    )
    return rows[0] if rows else None


def update_golf_player(event_id: int, row_id: int, name: str, handicap: float | None) -> None:
    execute(
        "UPDATE golf_groups SET player_name = %s, handicap = %s WHERE event_id = %s AND id = %s",
        (name, handicap, event_id, row_id),
    )


def _compact_golf_slots(cur, event_id: int, group_no: int) -> None:
    """把一組的順位重排成 1、2、3…，中間不留空號。
    先整組搬到負數再照原順序排回來；一句 UPDATE 直接補號會在中途撞上
    UNIQUE(event_id, group_no, slot)。"""
    cur.execute(
        "UPDATE golf_groups SET slot = -slot WHERE event_id = %s AND group_no = %s",
        (event_id, group_no),
    )
    cur.execute(
        """
        UPDATE golf_groups g SET slot = s.rn
        FROM (SELECT id, ROW_NUMBER() OVER (ORDER BY slot DESC) AS rn
              FROM golf_groups WHERE event_id = %s AND group_no = %s) s
        WHERE g.id = s.id
        """,
        (event_id, group_no),
    )


def delete_golf_player(event_id: int, row_id: int) -> None:
    """把一位球友從分組表刪掉，同組後面的順位往前補，不留空號。"""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM golf_groups WHERE event_id = %s AND id = %s RETURNING group_no",
                (event_id, row_id),
            )
            row = cur.fetchone()
            if row is not None:
                _compact_golf_slots(cur, event_id, row[0])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _release_conn(conn)


def move_golf_player(event_id: int, row_id: int, group_no: int,
                     per_group: int = 4) -> tuple[str, dict | None]:
    """把一位球友搬到另一組的空位（對調是兩個人互換，這支是搬到沒人的位子）。
    回傳 (狀態, 搬完的那一列)，狀態 ∈ ok / not_found / no_group / full。
    落點取那一組最前面的空號，原本那組的順位往前補。"""
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM golf_groups WHERE event_id = %s AND id = %s FOR UPDATE",
                (event_id, row_id),
            )
            row = cur.fetchone()
            if row is None:
                return "not_found", None
            row = dict(row)
            if row["group_no"] == group_no:
                return "ok", row                      # 已經在這一組，不用搬
            cur.execute(
                "SELECT slot FROM golf_groups WHERE event_id = %s AND group_no = %s",
                (event_id, group_no),
            )
            used = {r["slot"] for r in cur.fetchall()}
            if not used:
                return "no_group", None               # 沒有這一組，不要無中生有
            free = next((s for s in range(1, per_group + 1) if s not in used), None)
            if free is None:
                return "full", None
            cur.execute(
                "UPDATE golf_groups SET group_no = %s, slot = %s WHERE id = %s",
                (group_no, free, row_id),
            )
            _compact_golf_slots(cur, event_id, row["group_no"])
        conn.commit()
        return "ok", {**row, "group_no": group_no, "slot": free}
    except Exception:
        conn.rollback()
        raise
    finally:
        _release_conn(conn)


# ── 年會桌次安排 ───────────────────────────────────────────────────────────────

def ensure_event_seating_table() -> None:
    execute("""
        CREATE TABLE IF NOT EXISTS event_seating (
            id SERIAL PRIMARY KEY,
            event_id     INTEGER NOT NULL,
            table_no     INTEGER NOT NULL,
            seat_no      INTEGER NOT NULL,
            name         TEXT NOT NULL DEFAULT '',
            line_user_id TEXT NOT NULL DEFAULT '',
            club_name    TEXT NOT NULL DEFAULT '',
            UNIQUE(event_id, table_no, seat_no)
        )
    """)


def list_event_seating(event_id: int) -> list[dict]:
    return query(
        "SELECT id, table_no, seat_no, name, line_user_id, club_name "
        "FROM event_seating WHERE event_id = %s ORDER BY table_no, seat_no",
        (event_id,),
    )


def replace_event_seating(event_id: int, people: list[dict], per_table: int = 10) -> int:
    """Re-seat everyone from an ordered list (same club stays together upstream)."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM event_seating WHERE event_id = %s", (event_id,))
            for i, p in enumerate(people):
                cur.execute(
                    "INSERT INTO event_seating (event_id, table_no, seat_no, name, line_user_id, club_name) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (event_id, i // per_table + 1, i % per_table + 1,
                     p.get("name", ""), p.get("uid", ""), p.get("club", "")),
                )
        conn.commit()
        return len(people)
    except Exception:
        conn.rollback()
        raise
    finally:
        _release_conn(conn)


def swap_event_seats(event_id: int, id_a: int, id_b: int) -> tuple[dict, dict] | None:
    return _swap_rows("event_seating", ("table_no", "seat_no"), event_id, id_a, id_b)


def _swap_rows(table: str, cols: tuple[str, str], event_id: int,
               id_a: int, id_b: int) -> tuple[dict, dict] | None:
    """Exchange two rows' position columns. `table`/`cols` are internal constants,
    never user input. Parks one row at (-1,-1) so the UNIQUE index stays happy."""
    c1, c2 = cols
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT * FROM {table} WHERE event_id = %s AND id IN (%s, %s)",
                        (event_id, id_a, id_b))
            rows = [dict(r) for r in cur.fetchall()]
            if len(rows) != 2:
                return None
            a, b = (rows[0], rows[1]) if rows[0]["id"] == id_a else (rows[1], rows[0])
            cur.execute(f"UPDATE {table} SET {c1} = -1, {c2} = -1 WHERE id = %s", (a["id"],))
            cur.execute(f"UPDATE {table} SET {c1} = %s, {c2} = %s WHERE id = %s",
                        (a[c1], a[c2], b["id"]))
            cur.execute(f"UPDATE {table} SET {c1} = %s, {c2} = %s WHERE id = %s",
                        (b[c1], b[c2], a["id"]))
        conn.commit()
        return ({**a, c1: b[c1], c2: b[c2]}, {**b, c1: a[c1], c2: a[c2]})
    except Exception:
        conn.rollback()
        raise
    finally:
        _release_conn(conn)


# ── 年會摸彩 ───────────────────────────────────────────────────────────────────

def ensure_raffle_tables() -> None:
    execute("""
        CREATE TABLE IF NOT EXISTS event_prizes (
            id SERIAL PRIMARY KEY,
            event_id   INTEGER NOT NULL,
            name       TEXT NOT NULL,
            qty        INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0
        )
    """)
    execute("""
        CREATE TABLE IF NOT EXISTS event_raffle_winners (
            id SERIAL PRIMARY KEY,
            event_id     INTEGER NOT NULL,
            prize_id     INTEGER NOT NULL,
            name         TEXT NOT NULL DEFAULT '',
            line_user_id TEXT NOT NULL DEFAULT '',
            club_name    TEXT NOT NULL DEFAULT '',
            drawn_at     TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    execute("CREATE INDEX IF NOT EXISTS raffle_winners_event_idx "
            "ON event_raffle_winners (event_id)")


def list_prizes(event_id: int) -> list[dict]:
    return query(
        "SELECT id, name, qty, sort_order FROM event_prizes "
        "WHERE event_id = %s ORDER BY sort_order, id",
        (event_id,),
    )


def add_prize(event_id: int, name: str, qty: int) -> dict:
    return _write_returning(
        """
        INSERT INTO event_prizes (event_id, name, qty, sort_order)
        VALUES (%s, %s, %s,
                COALESCE((SELECT MAX(sort_order) + 1 FROM event_prizes WHERE event_id = %s), 1))
        RETURNING id, name, qty, sort_order
        """,
        (event_id, name, qty, event_id),
    )


def delete_prize(prize_id: int) -> None:
    execute("DELETE FROM event_raffle_winners WHERE prize_id = %s", (prize_id,))
    execute("DELETE FROM event_prizes WHERE id = %s", (prize_id,))


def get_prize(prize_id: int) -> dict | None:
    rows = query("SELECT id, event_id, name, qty FROM event_prizes WHERE id = %s", (prize_id,))
    return rows[0] if rows else None


def list_winners(event_id: int) -> list[dict]:
    return query(
        "SELECT id, prize_id, name, line_user_id, club_name FROM event_raffle_winners "
        "WHERE event_id = %s ORDER BY id",
        (event_id,),
    )


def raffle_candidates(event_id: int) -> list[dict]:
    """Checked-in attendees who have not won anything at this event yet."""
    return query(
        """
        SELECT r.line_user_id,
               COALESCE(pi.full_name, '') AS full_name,
               COALESCE(pi.nickname, '')  AS nickname,
               COALESCE(pi.club_name, '') AS club_name
        FROM registrations r
        LEFT JOIN personal_information pi ON pi.line_user_id = r.line_user_id
        WHERE r.event_id = %s AND r.checked_in
          AND r.line_user_id NOT IN (
              SELECT line_user_id FROM event_raffle_winners WHERE event_id = %s)
        ORDER BY r.line_user_id
        """,
        (event_id, event_id),
    )


def add_winners(event_id: int, prize_id: int, winners: list[dict]) -> None:
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            for w in winners:
                cur.execute(
                    "INSERT INTO event_raffle_winners (event_id, prize_id, name, line_user_id, club_name) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (event_id, prize_id, w["name"], w["uid"], w["club"]),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _release_conn(conn)


def clear_prize_winners(prize_id: int) -> None:
    execute("DELETE FROM event_raffle_winners WHERE prize_id = %s", (prize_id,))


# ── RYE：面試安排 + 同意書審核 ─────────────────────────────────────────────────

def ensure_rye_applicants_table() -> None:
    execute("""
        CREATE TABLE IF NOT EXISTS rye_applicants (
            id SERIAL PRIMARY KEY,
            event_id       INTEGER NOT NULL,
            name           TEXT NOT NULL,
            club_name      TEXT NOT NULL DEFAULT '',
            line_user_id   TEXT NOT NULL DEFAULT '',
            slot_time      TEXT NOT NULL DEFAULT '',
            interviewer    TEXT NOT NULL DEFAULT '',
            consent_url    TEXT NOT NULL DEFAULT '',
            consent_status TEXT NOT NULL DEFAULT 'none',   -- none | pending | approved | rejected
            consent_note   TEXT NOT NULL DEFAULT '',
            sort_order     INTEGER NOT NULL DEFAULT 0
        )
    """)


_RYE_COLS = ("id, event_id, name, club_name, line_user_id, slot_time, interviewer, "
             "consent_url, consent_status, consent_note, sort_order")


def list_rye_applicants(event_id: int) -> list[dict]:
    return query(
        f"SELECT {_RYE_COLS} FROM rye_applicants WHERE event_id = %s ORDER BY sort_order, id",
        (event_id,),
    )


def add_rye_applicant(event_id: int, name: str, club: str, uid: str) -> dict:
    return _write_returning(
        f"""
        INSERT INTO rye_applicants (event_id, name, club_name, line_user_id, sort_order)
        VALUES (%s, %s, %s, %s,
                COALESCE((SELECT MAX(sort_order) + 1 FROM rye_applicants WHERE event_id = %s), 1))
        RETURNING {_RYE_COLS}
        """,
        (event_id, name, club, uid, event_id),
    )


def update_rye_applicant(applicant_id: int, fields: dict) -> dict | None:
    allowed = ("name", "club_name", "line_user_id", "slot_time", "interviewer",
               "consent_url", "consent_status", "consent_note", "sort_order")
    sets = [f"{k} = %s" for k in allowed if k in fields]   # whitelist → safe to interpolate
    if not sets:
        return None
    params = [fields[k] for k in allowed if k in fields] + [applicant_id]
    return _write_returning(
        f"UPDATE rye_applicants SET {', '.join(sets)} WHERE id = %s RETURNING {_RYE_COLS}",
        params,
    )


def delete_rye_applicant(applicant_id: int) -> None:
    execute("DELETE FROM rye_applicants WHERE id = %s", (applicant_id,))


def set_rye_slots(event_id: int, slots: list[tuple[int, str]]) -> None:
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            for applicant_id, slot in slots:
                cur.execute("UPDATE rye_applicants SET slot_time = %s WHERE id = %s AND event_id = %s",
                            (slot, applicant_id, event_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _release_conn(conn)


# ── 理監事專區：名單 + 議案表決 ────────────────────────────────────────────────

def ensure_board_tables() -> None:
    execute("""
        CREATE TABLE IF NOT EXISTS board_members (
            club_name    TEXT NOT NULL,
            line_user_id TEXT NOT NULL,
            PRIMARY KEY (club_name, line_user_id)
        )
    """)
    execute("""
        CREATE TABLE IF NOT EXISTS board_motions (
            id SERIAL PRIMARY KEY,
            club_name  TEXT NOT NULL,
            title      TEXT NOT NULL,
            detail     TEXT NOT NULL DEFAULT '',
            status     TEXT NOT NULL DEFAULT 'open',   -- open | passed | rejected
            created_by TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    execute("""
        CREATE TABLE IF NOT EXISTS board_votes (
            motion_id    INTEGER NOT NULL,
            line_user_id TEXT NOT NULL,
            vote         TEXT NOT NULL,               -- yes | no | abstain
            voted_at     TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (motion_id, line_user_id)
        )
    """)


def list_board_members(club_name: str) -> list[dict]:
    return query(
        """
        SELECT bm.line_user_id,
               COALESCE(pi.full_name, '') AS full_name,
               COALESCE(pi.nickname, '')  AS nickname
        FROM board_members bm
        LEFT JOIN personal_information pi ON pi.line_user_id = bm.line_user_id
        WHERE bm.club_name = %s
        ORDER BY pi.full_name
        """,
        (club_name,),
    )


def set_board_members(club_name: str, uids: list[str]) -> int:
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM board_members WHERE club_name = %s", (club_name,))
            for uid in uids:
                cur.execute("INSERT INTO board_members (club_name, line_user_id) VALUES (%s, %s) "
                            "ON CONFLICT DO NOTHING", (club_name, uid))
        conn.commit()
        return len(uids)
    except Exception:
        conn.rollback()
        raise
    finally:
        _release_conn(conn)


def add_board_motion(club_name: str, title: str, detail: str, created_by: str) -> dict:
    return _write_returning(
        "INSERT INTO board_motions (club_name, title, detail, created_by) "
        "VALUES (%s, %s, %s, %s) RETURNING id, club_name, title, detail, status",
        (club_name, title, detail, created_by),
    )


def get_board_motion(motion_id: int) -> dict | None:
    rows = query("SELECT id, club_name, title, detail, status FROM board_motions WHERE id = %s",
                 (motion_id,))
    return rows[0] if rows else None


def list_board_motions(club_name: str) -> list[dict]:
    return query(
        "SELECT id, title, detail, status, created_at FROM board_motions "
        "WHERE club_name = %s ORDER BY id DESC",
        (club_name,),
    )


def close_board_motion(motion_id: int, status: str) -> None:
    execute("UPDATE board_motions SET status = %s WHERE id = %s", (status, motion_id))


def cast_board_vote(motion_id: int, line_user_id: str, vote: str) -> None:
    execute(
        """
        INSERT INTO board_votes (motion_id, line_user_id, vote, voted_at)
        VALUES (%s, %s, %s, NOW())
        ON CONFLICT (motion_id, line_user_id) DO UPDATE SET vote = EXCLUDED.vote, voted_at = NOW()
        """,
        (motion_id, line_user_id, vote),
    )


def list_board_votes(motion_id: int) -> list[dict]:
    return query(
        """
        SELECT bv.line_user_id, bv.vote,
               COALESCE(pi.full_name, '') AS full_name,
               COALESCE(pi.nickname, '')  AS nickname
        FROM board_votes bv
        LEFT JOIN personal_information pi ON pi.line_user_id = bv.line_user_id
        WHERE bv.motion_id = %s
        """,
        (motion_id,),
    )


# ── 報名專區：活動意願調查 ──────────────────────────────────────────────────────

def ensure_event_surveys_table() -> None:
    execute("""
        CREATE TABLE IF NOT EXISTS event_surveys (
            id SERIAL PRIMARY KEY,
            event_id     INTEGER NOT NULL,
            line_user_id TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'pending',   -- pending | attending | leave
            reminded     BOOLEAN NOT NULL DEFAULT FALSE,
            sent_at      TIMESTAMPTZ DEFAULT NOW(),
            replied_at   TIMESTAMPTZ,
            UNIQUE(event_id, line_user_id)
        )
    """)


def add_survey_targets(event_id: int, uids: list[str]) -> int:
    """Queue members as survey recipients. Re-sending to someone who already
    replied leaves their answer alone. Returns how many rows were newly added."""
    uids = [u for u in uids if u]
    if not uids:
        return 0
    new_count = 0
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            for uid in uids:
                cur.execute(
                    """
                    INSERT INTO event_surveys (event_id, line_user_id)
                    VALUES (%s, %s)
                    ON CONFLICT (event_id, line_user_id) DO NOTHING
                    RETURNING id
                    """,
                    (event_id, uid),
                )
                if cur.fetchone() is not None:
                    new_count += 1
        conn.commit()
        return new_count
    except Exception:
        conn.rollback()
        raise
    finally:
        _release_conn(conn)


def get_survey(event_id: int, club_name: str = "") -> list[dict]:
    """Everyone the survey was sent to, with their current answer."""
    return query(
        """
        SELECT s.line_user_id, s.status, s.reminded,
               COALESCE(pi.full_name, '') AS full_name,
               COALESCE(pi.nickname, '')  AS nickname,
               COALESCE(pi.club_name, '') AS club_name
        FROM event_surveys s
        LEFT JOIN personal_information pi ON pi.line_user_id = s.line_user_id
        WHERE s.event_id = %s AND (%s = '' OR pi.club_name = %s)
        ORDER BY s.status, pi.full_name
        """,
        (event_id, club_name, club_name),
    )


def set_survey_status(event_id: int, line_user_id: str, status: str) -> bool:
    """Record a member's own answer. False when they were never surveyed."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE event_surveys
                SET status = %s, replied_at = NOW()
                WHERE event_id = %s AND line_user_id = %s
                RETURNING id
                """,
                (status, event_id, line_user_id),
            )
            found = cur.fetchone() is not None
        conn.commit()
        return found
    except Exception:
        conn.rollback()
        raise
    finally:
        _release_conn(conn)


def get_survey_pending(event_id: int, club_name: str = "") -> list[dict]:
    return [r for r in get_survey(event_id, club_name) if r["status"] == "pending"]


def mark_survey_reminded(event_id: int, uids: list[str]) -> None:
    if not uids:
        return
    execute(
        "UPDATE event_surveys SET reminded = TRUE "
        "WHERE event_id = %s AND line_user_id = ANY(%s)",
        (event_id, uids),
    )


def get_club_admins(club_name: str) -> list[str]:
    """LINE ids of a club's 社長 / 秘書 — the people a report is escalated to."""
    rows = query(
        """
        SELECT ur.line_user_id
        FROM user_roles ur
        LEFT JOIN personal_information pi ON pi.line_user_id = ur.line_user_id
        WHERE ur.role = 'chair_club_admin'
          AND COALESCE(NULLIF(ur.club_name, ''), pi.club_name, '') = %s
        """,
        (club_name,),
    )
    return [r["line_user_id"] for r in rows]


def set_guest_golf_info(guest_id: int, handicap: float | None, course_plan: str | None) -> None:
    execute(
        "UPDATE event_guests SET handicap = %s, course_plan = %s WHERE id = %s",
        (handicap, course_plan, guest_id),
    )


def set_registration_golf_info(event_id: int, line_user_id: str,
                               handicap: float | None, course_plan: str | None) -> None:
    """主委在分組表上訂正的差點與方案。這裡是直接覆蓋（含清成空值），不像
    report_payment 只補不蓋——訂正的重點就是蓋掉舊值。"""
    execute(
        "UPDATE registrations SET handicap = %s, course_plan = %s "
        "WHERE event_id = %s AND line_user_id = %s",
        (handicap, course_plan, event_id, line_user_id),
    )


def get_event_guests(event_id: int) -> list[dict]:
    return query(
        "SELECT id, name, registered_by, handicap FROM event_guests WHERE event_id = %s ORDER BY id",
        (event_id,),
    )


def search_member(keyword: str) -> list[dict]:
    return query(
        """
        SELECT line_user_id, club_name, full_name, nickname
        FROM personal_information
        WHERE full_name ILIKE %s OR nickname ILIKE %s OR club_name ILIKE %s
        LIMIT 5
        """,
        (f"%{keyword}%", f"%{keyword}%", f"%{keyword}%"),
    )


def get_messages(session_id: str, limit: int = 20) -> list[dict]:
    return query(
        """
        SELECT role, content FROM (
            SELECT id, role, content FROM message_store
            WHERE session_id = %s ORDER BY id DESC LIMIT %s
        ) t ORDER BY id ASC
        """,
        (session_id, limit),
    )


def add_message(session_id: str, role: str, content: str) -> None:
    execute(
        "INSERT INTO message_store (session_id, role, content) VALUES (%s, %s, %s)",
        (session_id, role, content),
    )


# ── Ingestion helpers ──────────────────────────────────────────────────────────

def delete_documents_by_file_id(file_id: str) -> None:
    execute("DELETE FROM documents WHERE metadata->>'file_id' = %s", (file_id,))


def delete_document_rows_by_dataset_id(dataset_id: str) -> None:
    execute("DELETE FROM document_rows WHERE dataset_id = %s", (dataset_id,))


def upsert_document_metadata(id: str, title: str, url: str, schema: str | None = None) -> None:
    execute(
        """
        INSERT INTO document_metadata (id, title, url, schema)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
            title  = EXCLUDED.title,
            url    = EXCLUDED.url,
            schema = COALESCE(EXCLUDED.schema, document_metadata.schema)
        """,
        (id, title, url, schema),
    )


def update_document_metadata_schema(id: str, schema: str) -> None:
    execute("UPDATE document_metadata SET schema = %s WHERE id = %s", (schema, id))


def insert_document(content: str, metadata: dict, embedding: list[float]) -> None:
    embedding_str = "[" + ",".join(map(str, embedding)) + "]"
    execute(
        "INSERT INTO documents (content, metadata, embedding) VALUES (%s, %s, %s::vector)",
        (content, json.dumps(metadata), embedding_str),
    )


def _clean_value(v):
    import math
    if hasattr(v, "item"):
        v = v.item()
    try:
        if math.isnan(v):
            return None
    except (TypeError, ValueError):
        pass
    return v


def insert_document_rows_bulk(dataset_id: str, rows: list[dict]) -> None:
    if not rows:
        return
    cleaned_rows = [
        {str(k): _clean_value(v) for k, v in row.items()} for row in rows
    ]
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(
                cur,
                "INSERT INTO document_rows (dataset_id, row_data) VALUES (%s, %s)",
                [(dataset_id, json.dumps(r, default=str)) for r in cleaned_rows],
                page_size=200,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _release_conn(conn)
