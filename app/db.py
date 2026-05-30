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
