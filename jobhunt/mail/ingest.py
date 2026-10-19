"""Ingest data/mail_inbox/*.json: classify, match, apply or queue for review.

One file per Gmail message (`{thread_id}_{gmail_id}.json`). Ingestion is idempotent per message
through email_ingest (one row per message id, source "mail:{kind}"); outcomes use message-level
event keys. Later messages in the same thread retain their evidence even when their kind and
the pipeline status are unchanged."""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from jobhunt.clock import now as clock_now
from jobhunt.clock import parse_iso, to_iso
from jobhunt.config import CompanyConfig
from jobhunt.db import read_state, transaction, write_state
from jobhunt.mail.classify import classify
from jobhunt.mail.match import match_application
from jobhunt.mail.models import (
    KIND_RECRUITER,
    KIND_REJECTION,
    KIND_UNCLASSIFIED,
    Classification,
    MailFileError,
    MailMessage,
    MailOutcome,
    MatchResult,
)
from jobhunt.mail.rules import default_lookback_days, max_lookback_days, thresholds
from jobhunt.mail.transitions import apply_outcome, external_id_for
from jobhunt.models import EventType, Status
from jobhunt.normalize import Normalizer
from jobhunt.repo import applications as applications_repo
from jobhunt.repo import events as events_repo

log = logging.getLogger(__name__)

PROCESSED_DIR = "processed"
FAILED_DIR = "failed"
NEEDS_REVIEW_FILE = "needs_review.json"
SOURCE_PREFIX = "mail:"
INGEST_ID_PREFIX = "mail-message:"
KIND_IGNORE = "ignore"
MIN_LOOKBACK_DAYS = 1
SNIPPET_CHARS = 300
RESOLVED_CONFIDENCE = 1.0


@dataclass(slots=True)
class MailSummary:
    processed: int = 0
    auto_applied: int = 0
    needs_review: int = 0
    unclassified: int = 0
    duplicates: int = 0
    failed: list[str] = field(default_factory=list)
    outcomes: list[MailOutcome] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "processed": self.processed,
            "auto_applied": self.auto_applied,
            "needs_review": self.needs_review,
            "unclassified": self.unclassified,
            "duplicates": self.duplicates,
            "failed": list(self.failed),
            "changes": [
                {"job_id": o.job_id, "kind": o.kind, "from": o.from_status, "to": o.to_status}
                for o in self.outcomes
                if o.changed
            ],
        }


@dataclass(slots=True)
class _Deps:
    rules: Mapping[str, Any]
    companies: Sequence[CompanyConfig]
    norm: Normalizer
    stamp: str


def ingest_mail(
    conn: sqlite3.Connection,
    inbox_dir: Path,
    rules: Mapping[str, Any],
    companies: Sequence[CompanyConfig],
    norm: Normalizer,
    *,
    now: datetime | None = None,
) -> MailSummary:
    summary = MailSummary()
    processed_dir, failed_dir = inbox_dir / PROCESSED_DIR, inbox_dir / FAILED_DIR
    processed_dir.mkdir(parents=True, exist_ok=True)
    failed_dir.mkdir(parents=True, exist_ok=True)
    deps = _Deps(rules, companies, norm, to_iso(now or clock_now()))
    for path in sorted(p for p in inbox_dir.glob("*.json") if p.name != NEEDS_REVIEW_FILE):
        try:
            msg = MailMessage.from_dict(json.loads(path.read_text(encoding="utf-8")))
            _ingest_one(conn, inbox_dir, msg, deps, summary)
        except (MailFileError, json.JSONDecodeError, OSError, KeyError, ValueError) as exc:
            summary.failed.append(f"{path.name}: {exc}")
            log.warning("mail file %s rejected: %s", path.name, exc)
            _move(path, failed_dir)
            continue
        _move(path, processed_dir)
    log.info(
        "mail ingest: processed=%s applied=%s review=%s unclassified=%s duplicates=%s failed=%s",
        summary.processed,
        summary.auto_applied,
        summary.needs_review,
        summary.unclassified,
        summary.duplicates,
        len(summary.failed),
    )
    return summary


def _ingest_one(
    conn: sqlite3.Connection, inbox_dir: Path, msg: MailMessage, deps: _Deps, summary: MailSummary
) -> None:
    with transaction(conn):
        _process_message(conn, inbox_dir, msg, deps, summary)


def _process_message(
    conn: sqlite3.Connection, inbox_dir: Path, msg: MailMessage, deps: _Deps, summary: MailSummary
) -> None:
    if _already_ingested(conn, msg.gmail_id):
        summary.duplicates += 1
        return
    classification = classify(msg, deps.rules)
    match = match_application(conn, msg, deps.rules, deps.companies, deps.norm)
    auto, _ = thresholds(deps.rules)
    kind = classification.kind
    older = _older_rejection(conn, msg, kind, match.job_id)
    if older:
        match = MatchResult(
            match.job_id,
            match.confidence,
            match.candidates,
            "rejection predates newer application progress; verify the chronology",
        )
    if kind == KIND_UNCLASSIFIED:
        _record_unclassified(conn, msg, match, deps.stamp)
        summary.unclassified += 1
    elif (
        classification.confidence >= auto
        and (match.job_id is not None or kind == KIND_RECRUITER)
        and not older
    ):
        outcome = apply_outcome(conn, msg, classification, match)
        summary.outcomes.append(outcome)
        summary.duplicates += int(outcome.duplicate)
        summary.auto_applied += int(not outcome.duplicate)
    else:
        _queue_for_review(conn, inbox_dir, msg, classification, match, deps.stamp)
        summary.needs_review += 1
    _record_ingest(conn, msg.gmail_id, kind, match.job_id is not None, deps.stamp)
    summary.processed += 1


def _older_rejection(
    conn: sqlite3.Connection, msg: MailMessage, kind: str, job_id: int | None
) -> bool:
    if kind != KIND_REJECTION or job_id is None:
        return False
    app = applications_repo.get_for_job(conn, job_id)
    if app is None or app["status"] not in (Status.SCREENING, Status.INTERVIEW, Status.OFFER):
        return False
    received, activity = parse_iso(msg.date), parse_iso(app["last_activity_at"])
    return received is None or (activity is not None and received < activity)


def _record_unclassified(
    conn: sqlite3.Connection, msg: MailMessage, match: MatchResult, stamp: str
) -> None:
    if match.job_id is None:
        return
    external_id = external_id_for(msg.gmail_id, KIND_UNCLASSIFIED)
    existing = conn.execute(
        "SELECT 1 FROM events WHERE source = ? AND source_email_id = ? AND event_type = ? LIMIT 1",
        ("gmail", msg.gmail_id, EventType.MAIL_UNCLASSIFIED),
    ).fetchone()
    if existing is not None or events_repo.has_external_id(conn, external_id):
        return
    with transaction(conn):
        events_repo.add_event(
            conn,
            match.job_id,
            EventType.MAIL_UNCLASSIFIED,
            msg.subject[:SNIPPET_CHARS],
            source="gmail",
            source_email_id=msg.gmail_id,
            external_id=external_id,
            at=stamp,
        )


def _queue_for_review(
    conn: sqlite3.Connection,
    inbox_dir: Path,
    msg: MailMessage,
    classification: Classification,
    match: MatchResult,
    stamp: str,
) -> None:
    items = load_needs_review(inbox_dir)
    items = [item for item in items if item.get("gmail_id") != msg.gmail_id]
    items.append(
        {
            "thread_id": msg.thread_id,
            "gmail_id": msg.gmail_id,
            "date": msg.date,
            "subject": msg.subject,
            "from": msg.sender,
            "kind": classification.kind,
            "confidence": classification.confidence,
            "evidence": classification.evidence,
            "candidates": [c.to_dict() for c in match.candidates],
            "match_reason": match.reason,
            "snippet": (msg.snippet or msg.body_text)[:SNIPPET_CHARS],
            "message": msg.to_dict(),
        }
    )
    save_needs_review(inbox_dir, items)
    if len(match.candidates) == 1 or match.job_id is not None:
        job_id = match.job_id if match.job_id is not None else match.candidates[0].job_id
        external_id = f"{msg.gmail_id}:review"
        if not events_repo.has_external_id(conn, external_id):
            with transaction(conn):
                events_repo.add_event(
                    conn,
                    job_id,
                    EventType.MAIL_NEEDS_REVIEW,
                    f"{classification.kind} {classification.confidence:.2f}: {msg.subject}"[
                        :SNIPPET_CHARS
                    ],
                    source="gmail",
                    source_email_id=msg.gmail_id,
                    external_id=external_id,
                    at=stamp,
                )


# --------------------------------------------------------------------------- review file


def load_needs_review(inbox_dir: Path) -> list[dict[str, Any]]:
    path = inbox_dir / NEEDS_REVIEW_FILE
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def save_needs_review(inbox_dir: Path, items: list[dict[str, Any]]) -> None:
    inbox_dir.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=".mail-review-", dir=inbox_dir)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(items, stream, indent=2, default=str)
        os.replace(name, inbox_dir / NEEDS_REVIEW_FILE)
    except OSError:
        Path(name).unlink(missing_ok=True)
        raise


def resolve_review(
    conn: sqlite3.Connection,
    inbox_dir: Path,
    message_id: str,
    *,
    job_id: int | None,
    kind: str,
    rules: Mapping[str, Any],
    now: datetime | None = None,
) -> MailOutcome | None:
    """Apply the human decision for one needs-review item and drop it from the queue.

    `message_id` is the Gmail message id; a thread id is accepted when only one queued item
    belongs to that thread. kind "ignore" (or job_id None) removes the item without touching
    the pipeline.
    """
    outcome = None
    with transaction(conn):
        item = find_review_item(load_needs_review(inbox_dir), message_id)
        if kind != KIND_IGNORE and job_id is not None:
            msg = MailMessage.from_dict(item["message"])
            classification = Classification(kind, RESOLVED_CONFIDENCE, ["resolved by user"])
            outcome = apply_outcome(
                conn, msg, classification, MatchResult(job_id, RESOLVED_CONFIDENCE, [])
            )
            _record_ingest(conn, f"{msg.gmail_id}:resolved", kind, True, to_iso(now or clock_now()))
    key = item.get("gmail_id") or item["thread_id"]
    with transaction(conn):
        remaining = [
            i
            for i in load_needs_review(inbox_dir)
            if (i.get("gmail_id") or i.get("thread_id")) != key
        ]
        save_needs_review(inbox_dir, remaining)
    return outcome


def find_review_item(items: list[dict[str, Any]], key: str) -> dict[str, Any]:
    """Locate a queued item by message id, falling back to an unambiguous thread id."""
    by_message = [i for i in items if i.get("gmail_id") == key]
    if len(by_message) == 1:
        return by_message[0]
    by_thread = [i for i in items if i.get("thread_id") == key]
    if len(by_thread) == 1:
        return by_thread[0]
    if len(by_thread) > 1:
        ids = ", ".join(str(i.get("gmail_id")) for i in by_thread)
        raise KeyError(f"thread {key} has {len(by_thread)} queued messages ({ids}); pass one")
    raise KeyError(f"message {key} is not in {NEEDS_REVIEW_FILE}")


# --------------------------------------------------------------------------- sync bookkeeping


def last_sync(conn: sqlite3.Connection) -> datetime | None:
    return parse_iso(read_state(conn, "mail_sync_completed"))


def complete_sync(
    conn: sqlite3.Connection, through: datetime, *, now: datetime | None = None
) -> None:
    checkpoint = parse_iso(to_iso(through))
    reference = now or clock_now()
    if checkpoint > reference:
        raise ValueError("sync checkpoint cannot be in the future")
    with transaction(conn):
        previous = last_sync(conn)
        if previous is None or checkpoint > previous:
            write_state(conn, "mail_sync_completed", to_iso(checkpoint), to_iso(reference))


def lookback_days(
    conn: sqlite3.Connection, rules: Mapping[str, Any], *, now: datetime | None = None
) -> int:
    """Days to search back in Gmail: since the last sync plus one, clamped to [1, max]."""
    reference = now or clock_now()
    last = last_sync(conn)
    if last is None:
        days = default_lookback_days(rules)
    else:
        days = math.ceil((reference - last).total_seconds() / 86400) + 1
    return max(MIN_LOOKBACK_DAYS, min(max_lookback_days(rules), days))


def _already_ingested(conn: sqlite3.Connection, gmail_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM email_ingest WHERE gmail_id IN (?, ?) AND source LIKE ?",
        (gmail_id, INGEST_ID_PREFIX + gmail_id, SOURCE_PREFIX + "%"),
    ).fetchone()
    return row is not None


def _record_ingest(
    conn: sqlite3.Connection, gmail_id: str, kind: str, matched: bool, stamp: str
) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT OR REPLACE INTO email_ingest (gmail_id, source, ingested_at, jobs_found) "
            "VALUES (?, ?, ?, ?)",
            (INGEST_ID_PREFIX + gmail_id, f"{SOURCE_PREFIX}{kind}", stamp, int(matched)),
        )


def _move(path: Path, target_dir: Path) -> None:
    os.replace(path, target_dir / path.name)
