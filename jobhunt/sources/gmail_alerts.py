"""Ingest job-alert emails that the /jobs-scan skill wrote as JSON files into data/inbox/.

File contract (one file per email, schema_version 1):
{
  "schema_version": 1,
  "email": {"gmail_id", "thread_id", "from", "subject", "received_at", "alert_source"},
  "jobs": [{"title", "company", "location", "url", "posted_text", "salary_text", "snippet"}]
}
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jobhunt.clock import now_iso
from jobhunt.db import transaction
from jobhunt.models import JobPosting, UpsertOutcome
from jobhunt.normalize import Normalizer, canonical_url, indeed_job_key, is_remote_text
from jobhunt.repo import jobs as jobs_repo

log = logging.getLogger(__name__)

KEY = "gmail_alerts"
SCHEMA_VERSION = 1
PROCESSED_DIR = "processed"
FAILED_DIR = "failed"
ALLOWED_ALERT_SOURCES = frozenset(
    {
        "linkedin",
        "naukri",
        "instahyre",
        "cutshort",
        "wellfound",
        "indeed",
        "foundit",
        "hirist",
        "ziprecruiter",
        "wttj",
    }
)
SOURCE_SUFFIX = "_email"
INGEST_ID_PREFIX = "alert-message:"
FALLBACK_ID_LENGTH = 16
MAX_SNIPPET_CHARS = 2000

_LINKEDIN_HOST = "linkedin.com"
# Per board: a regex whose group(1) is the stable job id inside the job URL.
_ID_PATTERNS: dict[str, re.Pattern[str]] = {
    "naukri": re.compile(r"-(\d{8,})(?:[/?#]|$)"),
    "foundit": re.compile(r"/job/[^?#]*?-(\d{6,})(?:[/?#]|$)", re.I),
    "hirist": re.compile(r"[?&]jobId=(\d{5,})|/j/[^?#]*?-(\d{5,})(?:\.html)?(?:[/?#]|$)", re.I),
    "ziprecruiter": re.compile(
        r"[?&]jid=([0-9a-f]{6,})|/jobs/[^?#]*?-([0-9a-f]{8,})(?:[/?#]|$)", re.I
    ),
    "wttj": re.compile(r"/jobs/([a-z0-9][a-z0-9-]{3,})(?:[/?#]|$)", re.I),
}


class InboxFileError(ValueError):
    """Raised when an inbox JSON file does not follow the contract."""


@dataclass(slots=True)
class IngestSummary:
    files: int = 0
    emails: int = 0
    jobs: int = 0
    created: int = 0
    merged: int = 0
    refreshed: int = 0
    skipped_emails: int = 0
    failed: list[str] = field(default_factory=list)


def ingest_inbox(
    conn: sqlite3.Connection,
    inbox_dir: Path,
    norm: Normalizer,
    priority: Mapping[str, int],
) -> IngestSummary:
    summary = IngestSummary()
    processed = inbox_dir / PROCESSED_DIR
    failed = inbox_dir / FAILED_DIR
    processed.mkdir(parents=True, exist_ok=True)
    failed.mkdir(parents=True, exist_ok=True)
    for path in sorted(p for p in inbox_dir.glob("*.json") if p.is_file()):
        summary.files += 1
        try:
            payload = _load(path)
            _ingest_one(conn, payload, norm, priority, summary)
        except (InboxFileError, json.JSONDecodeError, OSError) as exc:
            summary.failed.append(f"{path.name}: {exc}")
            log.warning("inbox file %s rejected: %s", path.name, exc)
            _move(path, failed)
            continue
        _move(path, processed)
    log.info(
        "inbox ingest: files=%s emails=%s jobs=%s created=%s merged=%s skipped=%s failed=%s",
        summary.files,
        summary.emails,
        summary.jobs,
        summary.created,
        summary.merged,
        summary.skipped_emails,
        len(summary.failed),
    )
    return summary


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise InboxFileError("top level must be an object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise InboxFileError(f"schema_version must be {SCHEMA_VERSION}")
    email = payload.get("email")
    if not isinstance(email, dict) or not email.get("gmail_id"):
        raise InboxFileError("email.gmail_id is required")
    if email.get("alert_source") not in ALLOWED_ALERT_SOURCES:
        raise InboxFileError(f"alert_source must be one of {sorted(ALLOWED_ALERT_SOURCES)}")
    if not isinstance(payload.get("jobs"), list):
        raise InboxFileError("jobs must be a list")
    if any(not isinstance(card, dict) for card in payload["jobs"]):
        raise InboxFileError("each job card must be an object")
    return payload


def _ingest_one(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    norm: Normalizer,
    priority: Mapping[str, int],
    summary: IngestSummary,
) -> None:
    with transaction(conn):
        _process_email(conn, payload, norm, priority, summary)


def _process_email(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    norm: Normalizer,
    priority: Mapping[str, int],
    summary: IngestSummary,
) -> None:
    email = payload["email"]
    gmail_id = str(email["gmail_id"])
    if _already_ingested(conn, gmail_id):
        summary.skipped_emails += 1
        return
    source = f"{email['alert_source']}{SOURCE_SUFFIX}"
    postings = [p for p in (to_posting(card, email) for card in payload["jobs"]) if p]
    for posting in postings:
        outcome = jobs_repo.upsert_job(conn, posting, norm, priority).outcome
        _count(summary, outcome)
    with transaction(conn):
        conn.execute(
            "INSERT INTO email_ingest (gmail_id, source, ingested_at, jobs_found) "
            "VALUES (?, ?, ?, ?)",
            (INGEST_ID_PREFIX + gmail_id, source, now_iso(), len(postings)),
        )
    summary.emails += 1
    summary.jobs += len(postings)


def _already_ingested(conn: sqlite3.Connection, gmail_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM email_ingest WHERE gmail_id IN (?, ?) AND source GLOB '*_email'",
        (gmail_id, INGEST_ID_PREFIX + gmail_id),
    ).fetchone()
    return row is not None


def _count(summary: IngestSummary, outcome: UpsertOutcome) -> None:
    if outcome == UpsertOutcome.CREATED:
        summary.created += 1
    elif outcome == UpsertOutcome.MERGED:
        summary.merged += 1
    else:
        summary.refreshed += 1


def to_posting(card: Mapping[str, Any], email: Mapping[str, Any]) -> JobPosting | None:
    """One alert card -> JobPosting. Cards without url, title or company are dropped."""
    url = str(card.get("url") or "").strip()
    title = str(card.get("title") or "").strip()
    company = str(card.get("company") or "").strip()
    if not (url.startswith("http") and title and company):
        return None
    alert_source = str(email["alert_source"])
    location = card.get("location")
    snippet = card.get("snippet")
    return JobPosting(
        source=f"{alert_source}{SOURCE_SUFFIX}",
        source_job_id=source_job_id_for(alert_source, url),
        company=company,
        title=title,
        url=url,
        location=str(location) if location else None,
        is_remote=is_remote_text(str(location or "")) or is_remote_text(title),
        description_md=str(snippet)[:MAX_SNIPPET_CHARS] if snippet else None,
        salary_text=card.get("salary_text") or None,
        raw={
            "posted_text": card.get("posted_text"),
            "gmail_id": email.get("gmail_id"),
            "subject": email.get("subject"),
        },
    )


def source_job_id_for(alert_source: str, url: str) -> str:
    """Stable per-board id from the URL; falls back to a hash of the canonical URL."""
    canonical = canonical_url(url)
    if alert_source == "linkedin" and _LINKEDIN_HOST in canonical:
        return canonical.rstrip("/").rsplit("/", 1)[-1]
    if alert_source == "indeed":
        key = indeed_job_key(url)
        if key:
            return key
    pattern = _ID_PATTERNS.get(alert_source)
    if pattern:
        match = pattern.search(url)
        if match:
            return next(g for g in match.groups() if g).lower()
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:FALLBACK_ID_LENGTH]


def _move(path: Path, target_dir: Path) -> None:
    destination = target_dir / path.name
    os.replace(path, destination)
