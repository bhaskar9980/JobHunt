"""Funnel and follow-up queries for the CLI and dashboard."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from jobhunt.clock import now, to_iso
from jobhunt.models import Status


def funnel_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts = {status.value: 0 for status in Status}
    rows = conn.execute(
        "SELECT pipeline_status AS s, COUNT(*) AS n FROM v_pipeline GROUP BY pipeline_status"
    ).fetchall()
    for row in rows:
        counts[row["s"]] = int(row["n"])
    return counts


def discovered_since(conn: sqlite3.Connection, since_iso: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE discovered_at >= ?", (since_iso,)
    ).fetchone()
    return int(row["n"])


def response_rate(conn: sqlite3.Connection) -> float | None:
    """Share of applications that produced any human response (screening or later)."""
    total = conn.execute("SELECT COUNT(*) AS n FROM applications").fetchone()["n"]
    if not total:
        return None
    responded = conn.execute(
        "SELECT COUNT(*) AS n FROM applications "
        "WHERE status IN ('screening','interview','offer','rejected')"
    ).fetchone()["n"]
    return round(int(responded) / int(total), 3)


def follow_ups_due(conn: sqlite3.Connection, days_after_apply: int) -> list[sqlite3.Row]:
    reference = now()
    cutoff = to_iso(reference - timedelta(days=days_after_apply))
    return conn.execute(
        """
        SELECT a.*, j.company, j.title, j.url
        FROM applications a JOIN jobs j ON j.id = a.job_id
        WHERE a.status IN ('applied', 'screening')
          AND (a.next_follow_up_at <= ?
               OR (a.next_follow_up_at IS NULL AND a.last_activity_at <= ?))
        ORDER BY a.last_activity_at
        """,
        (to_iso(reference), cutoff),
    ).fetchall()


def recent_scan_runs(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM scan_runs ORDER BY started_at DESC LIMIT ?", (limit,)
    ).fetchall()
