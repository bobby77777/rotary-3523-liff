import json
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


def upsert_personal_info(
    line_user_id: str,
    club_name: str,
    full_name: str,
    nickname: str,
    diet_type: str,
) -> None:
    execute(
        """
        INSERT INTO personal_information (line_user_id, club_name, full_name, nickname, diet_type)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (line_user_id) DO UPDATE SET
            club_name  = EXCLUDED.club_name,
            full_name  = EXCLUDED.full_name,
            nickname   = EXCLUDED.nickname,
            diet_type  = EXCLUDED.diet_type
        """,
        (line_user_id, club_name, full_name, nickname, diet_type),
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


def upsert_golf_score(event_id: int, line_user_id: str, player_name: str, scores: list) -> None:
    execute(
        """
        INSERT INTO golf_scores (event_id, line_user_id, player_name, scores, updated_at)
        VALUES (%s, %s, %s, %s, NOW())
        ON CONFLICT (event_id, line_user_id) DO UPDATE SET
            player_name = EXCLUDED.player_name,
            scores = EXCLUDED.scores,
            updated_at = NOW()
        """,
        (event_id, line_user_id, player_name, json.dumps(scores)),
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
        SELECT g.line_user_id, g.player_name, g.scores, pi.club_name, pi.full_name
        FROM golf_scores g
        LEFT JOIN personal_information pi ON pi.line_user_id = g.line_user_id
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


def ensure_admin_users_table() -> None:
    execute("""
        CREATE TABLE IF NOT EXISTS admin_users (
            line_user_id TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)


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


def set_user_role(line_user_id: str, role: str, club_name: str = "") -> None:
    execute(
        """
        INSERT INTO user_roles (line_user_id, role, club_name, updated_at)
        VALUES (%s, %s, %s, NOW())
        ON CONFLICT (line_user_id) DO UPDATE SET
            role = EXCLUDED.role,
            club_name = EXCLUDED.club_name,
            updated_at = NOW()
        """,
        (line_user_id, role, club_name),
    )


def get_user_scope(line_user_id: str) -> str:
    rows = query("SELECT scope FROM user_roles WHERE line_user_id = %s", (line_user_id,))
    return rows[0]["scope"] if rows else "district"


def set_user_scope(line_user_id: str, scope: str) -> None:
    execute(
        """
        INSERT INTO user_roles (line_user_id, scope, updated_at)
        VALUES (%s, %s, NOW())
        ON CONFLICT (line_user_id) DO UPDATE SET
            scope = EXCLUDED.scope,
            updated_at = NOW()
        """,
        (line_user_id, scope),
    )


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
        "SELECT event_id, checked_in, checked_in_at, payment_status "
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
                  registered_by: str = "") -> dict:
    """Register many members for an event at once. Returns {'new': n, 'dup': n}."""
    new_count = 0
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            for uid in uids:
                cur.execute(
                    """
                    INSERT INTO registrations (line_user_id, event_id, payment_status, bank_digits, registered_by)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (line_user_id, event_id) DO NOTHING
                    RETURNING id
                    """,
                    (uid, event_id, "uploaded" if bank_digits else "unpaid",
                     bank_digits or None, registered_by or None),
                )
                if cur.fetchone() is not None:
                    new_count += 1
        conn.commit()
        return {"new": new_count, "dup": len(uids) - new_count}
    except Exception:
        conn.rollback()
        raise
    finally:
        _release_conn(conn)


def add_event_guests(event_id: int, names: list[str], registered_by: str = "",
                     bank_digits: str = "") -> int:
    names = [n.strip() for n in names if n and n.strip()]
    if not names:
        return 0
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(
                cur,
                "INSERT INTO event_guests (event_id, name, registered_by, bank_digits) "
                "VALUES (%s, %s, %s, %s)",
                [(event_id, n, registered_by, bank_digits or None) for n in names],
            )
        conn.commit()
        return len(names)
    except Exception:
        conn.rollback()
        raise
    finally:
        _release_conn(conn)


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
