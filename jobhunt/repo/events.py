"""Event log. Callers manage the transaction."""

from __future__ import annotations

import sqlite3

from jobhunt.clock import now_iso
from jobhunt.models import EventType

DEFAULT_SOURCE = "cli"


def add_event(  # noqa: PLR0913 - one row, one call; a spec object would only move the fields
    conn: sqlite3.Connection,
    job_id: int,
    event_type: EventType | str,
    note: str | None = None,
    *,
    application_id: int | None = None,
    from_status: str | None = None,
    to_status: str | None = None,
    source: str = DEFAULT_SOURCE,
    source_email_id: str | None = None,
    external_id: str | None = None,
    at: str | None = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO events (job_id, application_id, event_type, at, note, from_status, to_status,
                            source, source_email_id, external_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            application_id,
            str(event_type),
            at or now_iso(),
            note,
            from_status,
            to_status,
            source,
            source_email_id,
            external_id,
        ),
    )
    return int(cursor.lastrowid or 0)


def has_external_id(conn: sqlite3.Connection, external_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM events WHERE external_id = ?", (external_id,)).fetchone()
    return row is not None


def events_for_job(conn: sqlite3.Connection, job_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM events WHERE job_id = ? ORDER BY at, id", (job_id,)
    ).fetchall()


def events_since(conn: sqlite3.Connection, since_iso: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM events WHERE at >= ? ORDER BY at, id", (since_iso,)
    ).fetchall()


def latest_event_id_of_type(
    conn: sqlite3.Connection, job_id: int, event_type: EventType | str
) -> int | None:
    """Row id of the newest event of that type; ids are monotonic where timestamps may tie."""
    row = conn.execute(
        "SELECT MAX(id) AS id FROM events WHERE job_id = ? AND event_type = ?",
        (job_id, str(event_type)),
    ).fetchone()
    return int(row["id"]) if row and row["id"] is not None else None


def latest_event_at(conn: sqlite3.Connection, job_id: int) -> str | None:
    row = conn.execute("SELECT MAX(at) AS at FROM events WHERE job_id = ?", (job_id,)).fetchone()
    return row["at"] if row else None
