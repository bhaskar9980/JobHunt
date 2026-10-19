"""Applications: one row per job once it leaves the pre-apply funnel.

Callers manage transactions.
"""

from __future__ import annotations

import sqlite3

from jobhunt.clock import now_iso
from jobhunt.models import EventType, Status, SubmissionState
from jobhunt.repo import events

UNRESOLVED_SUBMISSIONS = (SubmissionState.PENDING, SubmissionState.UNKNOWN)


def submission_unresolved(conn: sqlite3.Connection, job_id: int) -> bool:
    if get_for_job(conn, job_id) is not None:
        return False
    row = conn.execute("SELECT submission_state FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row and row["submission_state"] in UNRESOLVED_SUBMISSIONS:
        return True
    unknown = events.latest_event_id_of_type(conn, job_id, EventType.SUBMISSION_UNKNOWN)
    resolved = events.latest_event_id_of_type(conn, job_id, EventType.SUBMISSION_RESOLVED)
    return unknown is not None and (resolved is None or unknown > resolved)


def begin_submission(conn: sqlite3.Connection, job_id: int) -> bool:
    if submission_unresolved(conn, job_id):
        return False
    stamp = now_iso()
    cursor = conn.execute(
        """
        UPDATE jobs SET submission_state = ?, submission_started_at = ?,
                        prefill_state = 'blocked', updated_at = ?
        WHERE id = ? AND submission_state = ? AND status = ? AND prefill_state = 'ready'
          AND NOT EXISTS (SELECT 1 FROM applications WHERE job_id = jobs.id)
        """,
        (SubmissionState.PENDING, stamp, stamp, job_id, SubmissionState.NONE, Status.APPROVED),
    )
    if not cursor.rowcount:
        return False
    events.add_event(
        conn, job_id, EventType.SUBMISSION_PENDING, "submission attempt reserved", source="apply"
    )
    return True


def mark_submission_unknown(conn: sqlite3.Connection, job_id: int) -> None:
    conn.execute(
        "UPDATE jobs SET submission_state = ?, prefill_state = 'blocked', updated_at = ? "
        "WHERE id = ? AND submission_state IN (?, ?)",
        (SubmissionState.UNKNOWN, now_iso(), job_id, *UNRESOLVED_SUBMISSIONS),
    )


def resolve_not_submitted(
    conn: sqlite3.Connection, job_id: int, note: str, *, ready: bool = False
) -> None:
    if not note.strip():
        raise ValueError("a verification note is required")
    if not submission_unresolved(conn, job_id):
        raise ValueError("job has no unresolved submission")
    conn.execute(
        "UPDATE jobs SET submission_state = ?, prefill_state = ?, updated_at = ? WHERE id = ?",
        (SubmissionState.NONE, "ready" if ready else "none", now_iso(), job_id),
    )
    events.add_event(conn, job_id, EventType.SUBMISSION_RESOLVED, note.strip(), source="apply")


def get_for_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM applications WHERE job_id = ?", (job_id,)).fetchone()


def ensure_for_status(conn: sqlite3.Connection, job_id: int, status: Status, at: str) -> int:
    """Create the application row on first post-apply status, otherwise update it."""
    conn.execute(
        "UPDATE jobs SET submission_state = ? WHERE id = ?", (SubmissionState.CONFIRMED, job_id)
    )
    existing = get_for_job(conn, job_id)
    if existing is None:
        cursor = conn.execute(
            """
            INSERT INTO applications (job_id, status, applied_at, last_activity_at)
            VALUES (?, ?, ?, ?)
            """,
            (job_id, str(status), at, at),
        )
        return int(cursor.lastrowid or 0)
    conn.execute("UPDATE applications SET status = ? WHERE id = ?", (str(status), existing["id"]))
    touch(conn, job_id, at)
    return int(existing["id"])


def record_submission(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    submitted_at: str,
    submitted_via: str,
    resume_variant: str | None = None,
    cover_letter_path: str | None = None,
    confirmation_ref: str | None = None,
) -> None:
    existing = get_for_job(conn, job_id)
    if existing is None:
        raise ValueError("record an application status before submission evidence")
    conn.execute(
        """
        UPDATE applications
        SET submitted_at = COALESCE(submitted_at, ?), submitted_via = COALESCE(submitted_via, ?),
            resume_variant = COALESCE(resume_variant, ?),
            cover_letter_path = COALESCE(cover_letter_path, ?),
            confirmation_ref = COALESCE(confirmation_ref, ?)
        WHERE job_id = ?
        """,
        (submitted_at, submitted_via, resume_variant, cover_letter_path, confirmation_ref, job_id),
    )
    if existing["submitted_at"] is None:
        touch(conn, job_id, submitted_at)


def set_next_follow_up(conn: sqlite3.Connection, job_id: int, at_iso: str | None) -> None:
    conn.execute("UPDATE applications SET next_follow_up_at = ? WHERE job_id = ?", (at_iso, job_id))


def touch(conn: sqlite3.Connection, job_id: int, at: str) -> None:
    conn.execute(
        "UPDATE applications SET last_activity_at = CASE "
        "WHEN julianday(?) > julianday(last_activity_at) THEN ? ELSE last_activity_at END "
        "WHERE job_id = ?",
        (at, at, job_id),
    )


def active(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT a.*, j.company, j.company_norm, j.title, j.title_norm
        FROM applications a JOIN jobs j ON j.id = a.job_id
        WHERE a.status IN ('applied', 'screening', 'interview')
        ORDER BY a.last_activity_at
        """
    ).fetchall()
