"""SQLite connection and schema management."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 4
MEMORY_DB = ":memory:"
BUSY_TIMEOUT_SECONDS = 15.0


class SchemaError(RuntimeError):
    """Raised when the on-disk schema cannot be brought to SCHEMA_VERSION."""


def _migrate_reliability(conn: sqlite3.Connection) -> None:
    conn.execute(
        "ALTER TABLE jobs ADD COLUMN submission_state TEXT NOT NULL DEFAULT 'none' "
        "CHECK (submission_state IN ('none','pending','unknown','confirmed'))"
    )
    conn.execute("ALTER TABLE jobs ADD COLUMN submission_started_at TEXT")
    conn.execute(
        "ALTER TABLE outreach ADD COLUMN referral_outcome TEXT NOT NULL DEFAULT 'pending' "
        "CHECK (referral_outcome IN ('pending','declined','completed'))"
    )
    conn.execute(
        """
        UPDATE jobs SET submission_state = 'unknown', prefill_state = 'blocked',
            submission_started_at = (
                SELECT MAX(at) FROM events WHERE job_id = jobs.id
                  AND event_type = 'submission_unknown'
            )
        WHERE id IN (SELECT job_id FROM events WHERE event_type = 'submission_unknown')
          AND id NOT IN (SELECT job_id FROM applications)
        """
    )
    conn.execute(
        "UPDATE jobs SET submission_state = 'confirmed' "
        "WHERE id IN (SELECT job_id FROM applications)"
    )


def _migrate_runtime_state(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS runtime_state "
        "(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_dismissed_reason(conn: sqlite3.Connection) -> None:
    """Record why each dismissal happened so a relisted job can be told from a rejected one."""
    conn.execute(
        "ALTER TABLE jobs ADD COLUMN dismissed_reason TEXT "
        "CHECK (dismissed_reason IN ('unlisted','skipped'))"
    )
    # Existing rows carry the cause only in their event history, so read it back from there.
    # A database old enough to predate event notes cannot say, and 'skipped' is the safe guess:
    # it never resurrects a job whose dismissal we cannot explain.
    if "status" not in _columns(conn, "jobs"):
        return  # A database this old has no dismissals to explain.
    if "note" not in _columns(conn, "events"):
        conn.execute("UPDATE jobs SET dismissed_reason = 'skipped' WHERE status = 'dismissed'")
        return
    conn.execute(
        """
        UPDATE jobs SET dismissed_reason = CASE
            WHEN EXISTS (SELECT 1 FROM events e WHERE e.job_id = jobs.id
                         AND e.note LIKE '%skipped in review%') THEN 'skipped'
            WHEN EXISTS (SELECT 1 FROM events e WHERE e.job_id = jobs.id
                         AND e.event_type = 'status_change' AND e.to_status = 'dismissed'
                         AND e.note LIKE '%no longer listed at%') THEN 'unlisted'
            ELSE 'skipped'
        END
        WHERE status = 'dismissed'
        """
    )


MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    2: _migrate_reliability,
    3: _migrate_runtime_state,
    4: _migrate_dismissed_reason,
}


def read_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM runtime_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def write_state(conn: sqlite3.Connection, key: str, value: str, at: str) -> None:
    conn.execute(
        "INSERT INTO runtime_state (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, at),
    )


def connect(path: str | Path) -> sqlite3.Connection:
    """Open the tracker.

    The dashboard server, a scan and a scoring import can all want the write lock at once.
    Without a busy timeout the loser fails immediately with "database is locked" and a click
    is silently lost, so every connection waits instead. The wait is finite: a real deadlock
    must still surface rather than hang.
    """
    is_memory = str(path) == MEMORY_DB
    if not is_memory:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_SECONDS * 1000)}")
    if not is_memory:
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    nested = conn.in_transaction
    savepoint = "jobhunt_" + uuid4().hex
    conn.execute(f"SAVEPOINT {savepoint}" if nested else "BEGIN IMMEDIATE")
    try:
        yield conn
        if nested:
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        else:
            conn.commit()
    except BaseException:
        if nested:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        else:
            conn.rollback()
        raise


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def init_db(conn: sqlite3.Connection) -> int:
    """Create the schema on a fresh database, or migrate an older one. Returns the version."""
    version = schema_version(conn)
    if version == 0:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
        log.info("schema created at version %s", SCHEMA_VERSION)
        return SCHEMA_VERSION
    return migrate(conn, version)


def migrate(conn: sqlite3.Connection, from_version: int) -> int:
    if from_version > SCHEMA_VERSION:
        raise SchemaError(f"database is version {from_version}, code supports {SCHEMA_VERSION}")
    with conn:
        if from_version < SCHEMA_VERSION and not conn.in_transaction:
            conn.execute("BEGIN")
        for version in range(from_version + 1, SCHEMA_VERSION + 1):
            step = MIGRATIONS.get(version)
            if step is None:
                raise SchemaError(f"no migration registered for version {version}")
            step(conn)
            conn.execute(f"PRAGMA user_version = {version}")
            log.info("migrated schema to version %s", version)
    return SCHEMA_VERSION


def table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')").fetchall()
    return {row["name"] for row in rows}
