import sqlite3
import json
import os
from datetime import datetime, timezone

DB_PATH = os.environ.get("MEMORY_DB_PATH", "/data/memory.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                content   TEXT    NOT NULL,
                tags      TEXT    NOT NULL DEFAULT '[]',
                source    TEXT,
                created_at TEXT   NOT NULL,
                updated_at TEXT   NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
                USING fts5(content, tags, content='memories', content_rowid='id');

            CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                INSERT INTO memories_fts(rowid, content, tags)
                VALUES (new.id, new.content, new.tags);
            END;

            CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, content, tags)
                VALUES ('delete', old.id, old.content, old.tags);
            END;

            CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, content, tags)
                VALUES ('delete', old.id, old.content, old.tags);
                INSERT INTO memories_fts(rowid, content, tags)
                VALUES (new.id, new.content, new.tags);
            END;
        """)


def _now():
    return datetime.now(timezone.utc).isoformat()


def store(content: str, tags: list[str], source: str | None) -> dict:
    tags_json = json.dumps(tags)
    now = _now()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO memories (content, tags, source, created_at, updated_at) VALUES (?,?,?,?,?)",
            (content, tags_json, source, now, now),
        )
        return {"id": cur.lastrowid, "content": content, "tags": tags, "source": source, "created_at": now}


def search(query: str, limit: int = 10) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT m.id, m.content, m.tags, m.source, m.created_at, m.updated_at,
                   rank
            FROM memories_fts
            JOIN memories m ON m.id = memories_fts.rowid
            WHERE memories_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()
        return [_row(r) for r in rows]


def list_all(tags_filter: list[str], limit: int) -> list[dict]:
    with get_conn() as conn:
        if tags_filter:
            # Match any of the filter tags via JSON text search
            conditions = " OR ".join(["tags LIKE ?" for _ in tags_filter])
            params = [f'%"{t}"%' for t in tags_filter] + [limit]
            rows = conn.execute(
                f"SELECT * FROM memories WHERE {conditions} ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM memories ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row(r) for r in rows]


def get_one(memory_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        return _row(row) if row else None


def update(memory_id: int, content: str | None, tags: list[str] | None) -> dict | None:
    existing = get_one(memory_id)
    if not existing:
        return None
    new_content = content if content is not None else existing["content"]
    new_tags = tags if tags is not None else existing["tags"]
    now = _now()
    with get_conn() as conn:
        conn.execute(
            "UPDATE memories SET content=?, tags=?, updated_at=? WHERE id=?",
            (new_content, json.dumps(new_tags), now, memory_id),
        )
    return get_one(memory_id)


def delete(memory_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        return cur.rowcount > 0


def stats() -> dict:
    with get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        oldest = conn.execute("SELECT MIN(created_at) FROM memories").fetchone()[0]
        newest = conn.execute("SELECT MAX(created_at) FROM memories").fetchone()[0]
        return {"total_memories": count, "oldest": oldest, "newest": newest, "db_path": DB_PATH}


def _row(r) -> dict:
    d = dict(r)
    try:
        d["tags"] = json.loads(d.get("tags", "[]"))
    except Exception:
        d["tags"] = []
    d.pop("rank", None)
    return d
