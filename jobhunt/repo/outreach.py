"""Outreach rows: one per message the user drafts, opens, sends and (hopefully) gets a reply to.

Follow-up schedule: the first send sets followup_due = sent + schedule[0]; the first follow-up
sets sent + schedule[1]; after the schedule is exhausted nothing more is due. Callers manage
transactions.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import timedelta

from jobhunt.clock import parse_iso, to_iso

CHANNEL_LINKEDIN = "linkedin"
CHANNEL_EMAIL = "email"
KIND_REFERRAL = "referral"
KIND_CONNECTION_NOTE = "connection_note"
KIND_FOLLOWUP = "followup"
KIND_INBOUND = "inbound"
KINDS = frozenset({KIND_REFERRAL, KIND_CONNECTION_NOTE, KIND_FOLLOWUP, KIND_INBOUND})
OUTCOME_PENDING = "pending"
OUTCOME_DECLINED = "declined"
OUTCOME_COMPLETED = "completed"
REFERRAL_OUTCOMES = frozenset({OUTCOME_PENDING, OUTCOME_DECLINED, OUTCOME_COMPLETED})

_SELECT = """
SELECT o.*, c.name AS contact_name, c.linkedin_url, c.company AS contact_company,
       c.title AS contact_title, j.company AS job_company, j.title AS job_title
FROM outreach o
JOIN contacts c ON c.id = o.contact_id
LEFT JOIN jobs j ON j.id = o.job_id
"""


class OutreachNotFound(LookupError):
    pass


def create(
    conn: sqlite3.Connection,
    *,
    contact_id: int,
    job_id: int | None,
    channel: str,
    kind: str,
    draft_path: str | None = None,
    hook: str | None = None,
    followup_due: str | None = None,
) -> int:
    if kind not in KINDS:
        raise ValueError(f"unknown outreach kind {kind!r}")
    cursor = conn.execute(
        """
        INSERT INTO outreach (contact_id, job_id, channel, kind, draft_path, hook, followup_due)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (contact_id, job_id, channel, kind, draft_path, hook, followup_due),
    )
    return int(cursor.lastrowid or 0)


def get(conn: sqlite3.Connection, outreach_id: int) -> sqlite3.Row:
    row = conn.execute(f"{_SELECT} WHERE o.id = ?", (outreach_id,)).fetchone()
    if row is None:
        raise OutreachNotFound(f"outreach {outreach_id} not found")
    return row


def for_job(conn: sqlite3.Connection, job_id: int) -> list[sqlite3.Row]:
    return conn.execute(f"{_SELECT} WHERE o.job_id = ? ORDER BY o.id", (job_id,)).fetchall()


def list_all(conn: sqlite3.Connection, job_id: int | None = None) -> list[sqlite3.Row]:
    if job_id is not None:
        return for_job(conn, job_id)
    return conn.execute(f"{_SELECT} ORDER BY o.id").fetchall()


def set_opened(conn: sqlite3.Connection, outreach_id: int, at: str) -> None:
    conn.execute(
        "UPDATE outreach SET opened_at = COALESCE(opened_at, ?) WHERE id = ?", (at, outreach_id)
    )


def mark_sent(
    conn: sqlite3.Connection, outreach_id: int, at: str, followup_days: int | None
) -> str | None:
    """Record the send, schedule the next follow-up (or none) and clear stale due dates."""
    row = get(conn, outreach_id)
    if row["sent_at"] is not None:
        return row["followup_due"]
    due_at = _plus_days(at, followup_days)
    conn.execute(
        "UPDATE outreach SET sent_at = ?, followup_due = ? WHERE id = ?",
        (at, due_at, outreach_id),
    )
    conn.execute(
        """
        UPDATE outreach SET followup_due = NULL
        WHERE contact_id = ? AND COALESCE(job_id, -1) = COALESCE(?, -1) AND id != ?
        """,
        (row["contact_id"], row["job_id"], outreach_id),
    )
    conn.execute("UPDATE contacts SET last_contacted_at = ? WHERE id = ?", (at, row["contact_id"]))
    return due_at


def mark_replied(
    conn: sqlite3.Connection,
    outreach_id: int,
    at: str,
    response: str | None,
    *,
    referral_outcome: str | None = None,
) -> None:
    """Record a reply; only an explicit outcome resolves the associated referral request."""
    if referral_outcome is not None and referral_outcome not in REFERRAL_OUTCOMES:
        raise ValueError(f"unknown referral outcome {referral_outcome!r}")
    row = get(conn, outreach_id)
    referral = referral_for_reply(conn, outreach_id)
    conn.execute(
        """
        UPDATE outreach SET replied_at = ?, response = COALESCE(?, response), followup_due = NULL,
                            referral_outcome = COALESCE(?, referral_outcome)
        WHERE id = ? OR id = ?
        """,
        (at, response, referral_outcome, outreach_id, referral["id"] if referral else None),
    )
    conn.execute(
        """
        UPDATE outreach SET followup_due = NULL
        WHERE contact_id = ? AND COALESCE(job_id, -1) = COALESCE(?, -1)
        """,
        (row["contact_id"], row["job_id"]),
    )
    if referral_outcome in (OUTCOME_DECLINED, OUTCOME_COMPLETED) and row["job_id"] is not None:
        _unpark_resolved_referral(conn, int(row["job_id"]), at)


def _unpark_resolved_referral(conn: sqlite3.Connection, job_id: int, at: str) -> None:
    conn.execute(
        """
        UPDATE jobs SET snooze_until = NULL, updated_at = ? WHERE id = ? AND (
            EXISTS (SELECT 1 FROM outreach WHERE job_id = ? AND referral_outcome = ?)
            OR NOT EXISTS (SELECT 1 FROM outreach WHERE job_id = ? AND kind = ?
                           AND sent_at IS NOT NULL AND referral_outcome = ?)
        )
        """,
        (
            at,
            job_id,
            job_id,
            OUTCOME_COMPLETED,
            job_id,
            KIND_REFERRAL,
            OUTCOME_PENDING,
        ),
    )


def referral_for_reply(conn: sqlite3.Connection, outreach_id: int) -> sqlite3.Row | None:
    """A follow-up belongs to the latest preceding sent referral for its contact and job."""
    row = get(conn, outreach_id)
    if row["kind"] == KIND_REFERRAL:
        return row
    if row["kind"] != KIND_FOLLOWUP:
        return None
    return conn.execute(
        f"""
        {_SELECT}
        WHERE o.contact_id = ? AND COALESCE(o.job_id, -1) = COALESCE(?, -1)
          AND o.kind = ? AND o.sent_at IS NOT NULL AND o.id < ?
        ORDER BY o.id DESC LIMIT 1
        """,
        (row["contact_id"], row["job_id"], KIND_REFERRAL, outreach_id),
    ).fetchone()


def due(conn: sqlite3.Connection, now_iso: str) -> list[sqlite3.Row]:
    return conn.execute(
        f"""
        {_SELECT}
        WHERE o.sent_at IS NOT NULL AND o.replied_at IS NULL
          AND o.followup_due IS NOT NULL AND o.followup_due <= ?
        ORDER BY o.followup_due
        """,
        (now_iso,),
    ).fetchall()


def last_asked(conn: sqlite3.Connection, contact_id: int) -> str | None:
    row = conn.execute(
        "SELECT MAX(sent_at) AS at FROM outreach WHERE contact_id = ? AND kind != ?",
        (contact_id, KIND_INBOUND),
    ).fetchone()
    return row["at"] if row and row["at"] else None


def has_reply(conn: sqlite3.Connection, contact_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM outreach WHERE contact_id = ? AND replied_at IS NOT NULL LIMIT 1",
        (contact_id,),
    ).fetchone()
    return row is not None


def followup_stage(conn: sqlite3.Connection, outreach_id: int) -> int:
    """How many messages (first ask plus follow-ups) are already sent for this contact and job."""
    row = get(conn, outreach_id)
    count = conn.execute(
        """
        SELECT COUNT(*) FROM outreach
        WHERE contact_id = ? AND COALESCE(job_id, -1) = COALESCE(?, -1)
          AND sent_at IS NOT NULL AND kind != ?
        """,
        (row["contact_id"], row["job_id"], KIND_INBOUND),
    ).fetchone()[0]
    return int(count)


def followup_days_for(stage: int, schedule: Sequence[int]) -> int | None:
    """Days until the next follow-up after `stage` messages have been sent; None means stop."""
    index = stage - 1
    if index < 0 or index >= len(schedule):
        return None
    return int(schedule[index])


def _plus_days(at: str, days: int | None) -> str | None:
    if days is None:
        return None
    base = parse_iso(at)
    if base is None:
        return None
    return to_iso(base + timedelta(days=days))
