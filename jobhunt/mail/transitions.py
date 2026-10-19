"""Turn a classified, matched email into pipeline changes without regressing a status.

Outcomes are keyed by Gmail message id and kind, not thread. Legacy events identify an
already-applied message through source_email_id, without suppressing later thread activity.
"""

from __future__ import annotations

import logging
import sqlite3

from jobhunt.clock import now_iso, parse_iso, to_iso
from jobhunt.db import transaction
from jobhunt.mail.models import (
    KIND_ASSESSMENT,
    KIND_CONFIRMATION,
    KIND_INTERVIEW,
    KIND_OFFER,
    KIND_RECRUITER,
    KIND_REJECTION,
    Classification,
    MailMessage,
    MailOutcome,
    MatchResult,
)
from jobhunt.models import EventType, Status
from jobhunt.repo import applications as applications_repo
from jobhunt.repo import events as events_repo
from jobhunt.repo import jobs as jobs_repo

log = logging.getLogger(__name__)

SOURCE = "gmail"
CHANNEL_EMAIL = "email"
OUTREACH_KIND_INBOUND = "inbound"
CONTACT_SOURCE = "gmail"
MAX_RESPONSE_CHARS = 500
NOTE_MAX_CHARS = 200
MIN_DOMAIN_LABELS = 2

TARGET_BY_KIND: dict[str, Status] = {
    KIND_CONFIRMATION: Status.APPLIED,
    KIND_ASSESSMENT: Status.SCREENING,
    KIND_INTERVIEW: Status.INTERVIEW,
    KIND_OFFER: Status.OFFER,
    KIND_REJECTION: Status.REJECTED,
}
# Higher rank never moves to lower rank on mail evidence. Ghosted sits with applied so a late
# assessment or interview revives it but a late confirmation does not.
RANK: dict[Status, int] = {
    Status.APPROVED: 0,
    Status.APPLIED: 1,
    Status.GHOSTED: 1,
    Status.SCREENING: 2,
    Status.INTERVIEW: 3,
    Status.OFFER: 4,
    Status.REJECTED: 5,
}
TERMINAL: frozenset[Status] = frozenset({Status.REJECTED, Status.WITHDRAWN})


def external_id_for(gmail_id: str, kind: str) -> str:
    return f"{SOURCE}:{gmail_id}:{kind}"


def _has_applied_message(conn: sqlite3.Connection, gmail_id: str, external_id: str) -> bool:
    if events_repo.has_external_id(conn, external_id):
        return True
    row = conn.execute(
        "SELECT 1 FROM events WHERE source = ? AND source_email_id = ? "
        "AND event_type IN (?, ?, ?) LIMIT 1",
        (
            SOURCE,
            gmail_id,
            EventType.STATUS_CHANGE,
            EventType.EMAIL_RECEIVED,
            EventType.INTERVIEW_SCHEDULED,
        ),
    ).fetchone()
    return row is not None


def apply_outcome(
    conn: sqlite3.Connection,
    msg: MailMessage,
    classification: Classification,
    match: MatchResult,
    *,
    at: str | None = None,
) -> MailOutcome:
    with transaction(conn):
        return _apply_outcome(conn, msg, classification, match, at=at)


def _apply_outcome(
    conn: sqlite3.Connection,
    msg: MailMessage,
    classification: Classification,
    match: MatchResult,
    *,
    at: str | None = None,
) -> MailOutcome:
    kind = classification.kind
    stamp = _stamp(at or msg.date)
    external_id = external_id_for(msg.gmail_id, kind)
    outcome = MailOutcome(msg.thread_id, kind, match.job_id)
    if _has_applied_message(conn, msg.gmail_id, external_id):
        outcome.duplicate = True
        outcome.note = "message already processed"
        return outcome
    if kind == KIND_RECRUITER:
        return _inbound(conn, msg, outcome, external_id, stamp)
    if match.job_id is None:
        outcome.note = "no matched job"
        return outcome
    target = TARGET_BY_KIND.get(kind)
    current = jobs_repo.current_status(conn, match.job_id)
    outcome.from_status = str(current)
    if target is None or current in TERMINAL or RANK.get(target, 0) <= RANK.get(current, 0):
        return _event_only(conn, msg, outcome, external_id, stamp, current)
    return _advance(conn, msg, outcome, target, external_id, stamp, current)


# --------------------------------------------------------------------------- status moves


def _advance(
    conn: sqlite3.Connection,
    msg: MailMessage,
    outcome: MailOutcome,
    target: Status,
    external_id: str,
    stamp: str,
    current: Status,
) -> MailOutcome:
    job_id = int(outcome.job_id or 0)
    note = f"{outcome.kind} email: {msg.subject[:NOTE_MAX_CHARS]}"
    try:
        if current == Status.APPROVED and target != Status.APPLIED:
            # The only pre-apply status that can receive mail evidence; the email proves the
            # application went in, so record applied first (legal move) then the real target.
            jobs_repo.set_job_status(
                conn,
                job_id,
                Status.APPLIED,
                f"implied by {outcome.kind} email",
                source=SOURCE,
                at=stamp,
            )
        jobs_repo.set_job_status(
            conn, job_id, target, note, source=SOURCE, at=stamp, external_id=external_id
        )
    except jobs_repo.InvalidTransition as exc:
        log.warning("mail %s: %s; recording event only", msg.thread_id, exc)
        return _event_only(conn, msg, outcome, external_id, stamp, current)
    outcome.to_status = str(target)
    outcome.note = note
    _after_change(conn, msg, outcome, stamp)
    return outcome


def _after_change(
    conn: sqlite3.Connection, msg: MailMessage, outcome: MailOutcome, stamp: str
) -> None:
    job_id = int(outcome.job_id or 0)
    app_row = applications_repo.get_for_job(conn, job_id)
    app_id = int(app_row["id"]) if app_row else None
    with transaction(conn):
        conn.execute(
            "UPDATE events SET source_email_id = ? WHERE external_id = ?",
            (msg.gmail_id, external_id_for(msg.gmail_id, outcome.kind)),
        )
        if outcome.kind == KIND_CONFIRMATION:
            _set_confirmation_email(conn, job_id, msg.gmail_id)
        if outcome.kind == KIND_INTERVIEW:
            _record_interview(conn, msg, job_id, app_id, stamp)
        applications_repo.touch(conn, job_id, stamp)


def _record_interview(
    conn: sqlite3.Connection, msg: MailMessage, job_id: int, app_id: int | None, stamp: str
) -> None:
    events_repo.add_event(
        conn,
        job_id,
        EventType.INTERVIEW_SCHEDULED,
        msg.subject[:NOTE_MAX_CHARS],
        application_id=app_id,
        source=SOURCE,
        source_email_id=msg.gmail_id,
        at=stamp,
    )


def _event_only(
    conn: sqlite3.Connection,
    msg: MailMessage,
    outcome: MailOutcome,
    external_id: str,
    stamp: str,
    current: Status,
) -> MailOutcome:
    job_id = int(outcome.job_id or 0)
    app_row = applications_repo.get_for_job(conn, job_id)
    note = f"{outcome.kind} email kept {current}: {msg.subject[:NOTE_MAX_CHARS]}"
    with transaction(conn):
        events_repo.add_event(
            conn,
            job_id,
            EventType.EMAIL_RECEIVED,
            note,
            application_id=int(app_row["id"]) if app_row else None,
            source=SOURCE,
            source_email_id=msg.gmail_id,
            external_id=external_id,
            at=stamp,
        )
        if outcome.kind == KIND_CONFIRMATION and app_row is not None:
            _set_confirmation_email(conn, job_id, msg.gmail_id)
        if outcome.kind == KIND_INTERVIEW and current == Status.INTERVIEW:
            _record_interview(conn, msg, job_id, int(app_row["id"]) if app_row else None, stamp)
        if app_row is not None and current not in TERMINAL:
            applications_repo.touch(conn, job_id, stamp)
    outcome.to_status = str(current)
    outcome.note = note
    return outcome


def _set_confirmation_email(conn: sqlite3.Connection, job_id: int, gmail_id: str) -> None:
    conn.execute(
        "UPDATE applications SET confirmation_email_id = COALESCE(confirmation_email_id, ?) "
        "WHERE job_id = ?",
        (gmail_id, job_id),
    )


# --------------------------------------------------------------------------- recruiter inbound


def _inbound(
    conn: sqlite3.Connection, msg: MailMessage, outcome: MailOutcome, external_id: str, stamp: str
) -> MailOutcome:
    applied = conn.execute(
        "SELECT 1 FROM email_ingest WHERE gmail_id = ? AND source = ?", (external_id, SOURCE)
    ).fetchone()
    if applied is not None:
        outcome.duplicate = True
        outcome.note = "message already processed"
        return outcome
    with transaction(conn):
        contact_id = _upsert_contact(conn, msg, stamp)
        _create_inbound_outreach(conn, contact_id, outcome.job_id, msg, stamp)
        if outcome.job_id is not None:
            events_repo.add_event(
                conn,
                outcome.job_id,
                EventType.EMAIL_RECEIVED,
                f"recruiter outreach: {msg.subject[:NOTE_MAX_CHARS]}",
                source=SOURCE,
                source_email_id=msg.gmail_id,
                external_id=external_id,
                at=stamp,
            )
            if jobs_repo.current_status(conn, outcome.job_id) not in TERMINAL:
                applications_repo.touch(conn, outcome.job_id, stamp)
        else:
            conn.execute(
                "INSERT INTO email_ingest (gmail_id, source, ingested_at, jobs_found) "
                "VALUES (?, ?, ?, ?)",
                (external_id, SOURCE, stamp, 0),
            )
    outcome.note = f"contact {contact_id}; inbound outreach created"
    return outcome


def _upsert_contact(conn: sqlite3.Connection, msg: MailMessage, stamp: str) -> int:
    email = msg.sender_email
    row = None
    if email:
        row = conn.execute("SELECT id FROM contacts WHERE email = ?", (email,)).fetchone()
    if row is not None:
        conn.execute(
            "UPDATE contacts SET last_contacted_at = ? WHERE id = ? "
            "AND (julianday(last_contacted_at) IS NULL "
            "OR julianday(last_contacted_at) < julianday(?))",
            (stamp, row["id"], stamp),
        )
        return int(row["id"])
    company = _company_from_domain(msg.from_domain)
    cursor = conn.execute(
        """
        INSERT INTO contacts (name, company, company_norm, title, email, relationship, source,
                              last_contacted_at)
        VALUES (?, ?, ?, NULL, ?, 'recruiter', ?, ?)
        """,
        (
            msg.from_name or email or "unknown",
            company,
            company.lower(),
            email,
            CONTACT_SOURCE,
            stamp,
        ),
    )
    return int(cursor.lastrowid or 0)


def _create_inbound_outreach(
    conn: sqlite3.Connection, contact_id: int, job_id: int | None, msg: MailMessage, stamp: str
) -> None:
    conn.execute(
        """
        INSERT INTO outreach (contact_id, job_id, channel, kind, hook, replied_at, response)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            contact_id,
            job_id,
            CHANNEL_EMAIL,
            OUTREACH_KIND_INBOUND,
            msg.subject,
            stamp,
            (msg.snippet or msg.body_text)[:MAX_RESPONSE_CHARS],
        ),
    )


def _company_from_domain(domain: str) -> str:
    labels = [part for part in (domain or "").split(".") if part]
    return labels[-2].capitalize() if len(labels) >= MIN_DOMAIN_LABELS else (domain or "")


def _stamp(value: str | None) -> str:
    try:
        parsed = parse_iso(value)
    except ValueError:
        parsed = None
    return to_iso(parsed) if parsed else now_iso()
