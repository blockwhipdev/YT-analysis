"""
Postgres logging for Signal / Noise.

Every resolve/analyze request is recorded so /admin can show what's happening.
Fully optional and non-fatal: if DATABASE_URL is unset or the DB is unreachable,
the app keeps working and logging simply no-ops.

Config (env only — never hardcode secrets):
    DATABASE_URL = postgresql://user:pass@host/db?sslmode=require
"""

import os
import sys
import queue
import hashlib
import threading
from contextlib import contextmanager

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor, Json
except Exception:  # library not installed yet
    psycopg2 = None

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()


def enabled() -> bool:
    return bool(DATABASE_URL and psycopg2)


def _conn():
    return psycopg2.connect(DATABASE_URL, connect_timeout=10)


@contextmanager
def _cursor(dict_rows=False):
    conn = _conn()
    try:
        with conn:  # commits on clean exit, rolls back on exception
            cur = conn.cursor(cursor_factory=RealDictCursor) if dict_rows else conn.cursor()
            try:
                yield cur
            finally:
                cur.close()
    finally:
        conn.close()


DDL = [
    """
    CREATE TABLE IF NOT EXISTS events (
        id                BIGSERIAL PRIMARY KEY,
        created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
        kind              TEXT NOT NULL,            -- 'resolve' | 'analyze'
        status            TEXT NOT NULL,            -- 'ok' | 'error'
        video_id          TEXT,
        title             TEXT,
        url               TEXT,
        value_score       INTEGER,
        transcript_chars  INTEGER,
        model             TEXT,
        key_hash          TEXT,                     -- sha256 prefix, NOT the key
        duration_ms       INTEGER,
        ip                TEXT,
        user_agent        TEXT,
        error             TEXT,
        result            JSONB                     -- full generated briefing
    )
    """,
    "ALTER TABLE events ADD COLUMN IF NOT EXISTS result JSONB",
    "CREATE INDEX IF NOT EXISTS events_created_idx ON events (created_at DESC)",
]

_COLS = [
    "kind", "status", "video_id", "title", "url", "value_score",
    "transcript_chars", "model", "key_hash", "duration_ms", "ip",
    "user_agent", "error", "result",
]


# --- Background writer: the request thread never waits on the DB ---------- #
# All writes go onto a bounded queue drained by one daemon thread. If the DB is
# slow or down, items error in the worker (swallowed) or the queue fills and new
# items are dropped — either way the user's request is never blocked or delayed.

_q: "queue.Queue" = queue.Queue(maxsize=1000)
_worker_started = False
_worker_lock = threading.Lock()


def _init_schema():
    with _cursor() as cur:
        for stmt in DDL:
            cur.execute(stmt)


def _insert(fields):
    vals = []
    for c in _COLS:
        v = fields.get(c)
        if c == "result" and isinstance(v, dict):
            v = Json(v)  # adapt dict -> JSONB
        vals.append(v)
    placeholders = ",".join(["%s"] * len(_COLS))
    sql = f"INSERT INTO events ({','.join(_COLS)}) VALUES ({placeholders})"
    with _cursor() as cur:
        cur.execute(sql, vals)


def _worker():
    try:
        _init_schema()
    except Exception as e:
        print(f"[db] schema init failed (logging will retry per-write): {e}", file=sys.stderr)
    while True:
        item = _q.get()
        try:
            if item is not None:
                _insert(item)
        except Exception as e:
            print(f"[db] log dropped: {e}", file=sys.stderr)
        finally:
            _q.task_done()


def _ensure_worker():
    global _worker_started
    if _worker_started or not enabled():
        return
    with _worker_lock:
        if not _worker_started:
            threading.Thread(target=_worker, daemon=True, name="db-logger").start()
            _worker_started = True


def init_db():
    """Start the background writer (which creates the schema on its own thread)."""
    _ensure_worker()


def key_fingerprint(key: str):
    """Non-reversible short hash so the admin can group by user without seeing keys."""
    if not key:
        return None
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def log_event(**fields):
    """Non-blocking: enqueue and return. Never raises, never waits on the DB."""
    if not enabled():
        return
    _ensure_worker()
    try:
        _q.put_nowait(fields)
    except queue.Full:
        pass  # under sustained DB trouble, drop rather than block the request


def fetch_analyses(limit=200):
    """Successful analyses with their full stored briefing — the review feed."""
    if not enabled():
        return []
    try:
        with _cursor(dict_rows=True) as cur:
            cur.execute(
                "SELECT created_at, title, url, video_id, value_score, transcript_chars, "
                "key_hash, result FROM events "
                "WHERE kind='analyze' AND status='ok' AND result IS NOT NULL "
                "ORDER BY created_at DESC LIMIT %s",
                (limit,),
            )
            return cur.fetchall()
    except Exception as e:
        print(f"[db] fetch_analyses failed: {e}", file=sys.stderr)
        return []


def fetch_events(limit=250):
    if not enabled():
        return []
    try:
        with _cursor(dict_rows=True) as cur:
            cur.execute("SELECT * FROM events ORDER BY created_at DESC LIMIT %s", (limit,))
            return cur.fetchall()
    except Exception as e:
        print(f"[db] fetch_events failed: {e}", file=sys.stderr)
        return []


def fetch_stats():
    if not enabled():
        return {}
    try:
        with _cursor(dict_rows=True) as cur:
            cur.execute(
                """
                SELECT
                  count(*) FILTER (WHERE kind='analyze')                          AS analyses,
                  count(*) FILTER (WHERE kind='analyze' AND status='ok')          AS analyses_ok,
                  count(*) FILTER (WHERE kind='analyze' AND status='error')       AS analyses_err,
                  count(*) FILTER (WHERE kind='resolve')                          AS resolves,
                  count(DISTINCT key_hash) FILTER (WHERE key_hash IS NOT NULL)    AS users,
                  count(DISTINCT video_id) FILTER (WHERE video_id IS NOT NULL)    AS videos,
                  round(avg(value_score) FILTER (WHERE status='ok' AND value_score IS NOT NULL)) AS avg_score,
                  count(*) FILTER (WHERE created_at > now() - interval '24 hours') AS last_24h
                FROM events
                """
            )
            return cur.fetchone() or {}
    except Exception as e:
        print(f"[db] fetch_stats failed: {e}", file=sys.stderr)
        return {}
