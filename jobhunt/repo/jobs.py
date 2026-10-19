"""Jobs repository: upsert with dedupe, status transitions, listing."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import replace as replace_posting
from typing import Any

from jobhunt.clock import now_iso, to_iso
from jobhunt.db import transaction
from jobhunt.models import (
    APPLICATION_STAGES,
    ELIGIBILITY_REVIEW_REASON,
    NO_DESCRIPTION_REASON,
    TRANSITIONS,
    EventType,
    JobPosting,
    NormalizedKeys,
    PrefilterResult,
    Status,
    UpsertOutcome,
)
from jobhunt.normalize import Normalizer, fingerprint, is_remote_text
from jobhunt.prefilter_reasons import record_eligibility_verdict
from jobhunt.repo import applications, events

log = logging.getLogger(__name__)

DEFAULT_PRIORITY_KEY = "default"
DEFAULT_PRIORITY = 10


class JobNotFound(LookupError):
    """Raised when a job id does not exist."""


class InvalidTransition(ValueError):
    """Raised when a status change is not allowed by TRANSITIONS and force is not set."""


@dataclass(slots=True)
class UpsertResult:
    job_id: int
    outcome: UpsertOutcome


# --------------------------------------------------------------------------- reads


def get_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM v_pipeline WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise JobNotFound(f"job {job_id} not found")
    return row


def current_status(conn: sqlite3.Connection, job_id: int) -> Status:
    return Status(get_job(conn, job_id)["pipeline_status"])


def list_jobs(
    conn: sqlite3.Connection,
    *,
    statuses: list[str] | None = None,
    min_score: int | None = None,
    since_iso: str | None = None,
    company: str | None = None,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list[Any] = []
    if statuses:
        clauses.append(f"pipeline_status IN ({','.join('?' for _ in statuses)})")
        params.extend(statuses)
    if min_score is not None:
        clauses.append("score >= ?")
        params.append(min_score)
    if since_iso:
        clauses.append("discovered_at >= ?")
        params.append(since_iso)
    if company:
        clauses.append("company_norm LIKE ?")
        params.append(f"%{company.lower()}%")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit_sql = f"LIMIT {int(limit)}" if limit else ""
    order = "ORDER BY score DESC NULLS LAST, discovered_at DESC"
    sql = f"SELECT * FROM v_pipeline {where} {order} {limit_sql}"
    return conn.execute(sql, params).fetchall()


def sources_for_job(conn: sqlite3.Connection, job_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT source, source_job_id, url, first_seen_at, last_seen_at FROM job_sources "
        "WHERE job_id = ? ORDER BY first_seen_at",
        (job_id,),
    ).fetchall()


def job_to_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for key in ("prefilter_reasons", "score_reasons"):
        data[key] = _load_json(data.get(key))
    data["is_remote"] = bool(data.get("is_remote"))
    data["sources"] = [dict(s) for s in sources_for_job(conn, row["id"])]
    app_row = applications.get_for_job(conn, row["id"])
    data["application"] = dict(app_row) if app_row else None
    return data


def _load_json(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


# --------------------------------------------------------------------------- upsert


def upsert_job(
    conn: sqlite3.Connection,
    posting: JobPosting,
    norm: Normalizer,
    priority: Mapping[str, int],
) -> UpsertResult:
    """Insert or merge a posting. Match order: (source, source_job_id) -> url -> fingerprint."""
    keys = norm.keys_for(posting)
    at = now_iso()
    with transaction(conn):
        existing = _find_by_source(conn, posting.source, posting.source_job_id)
        if existing is not None:
            _refresh(conn, existing, posting, keys, priority, at, norm)
            _revive_if_relisted(conn, existing)
            return UpsertResult(existing, UpsertOutcome.REFRESHED)
        existing = _find_by_url(conn, keys.url_canonical) or _find_by_fingerprint(
            conn, keys.fingerprint, exclude_source=posting.source
        )
        if existing is not None:
            _merge(conn, existing, posting, keys, priority, at, norm)
            return UpsertResult(existing, UpsertOutcome.MERGED)
        job_id = _insert(conn, posting, keys, at)
        return UpsertResult(job_id, UpsertOutcome.CREATED)


def _revive_if_relisted(conn: sqlite3.Connection, job_id: int) -> None:
    """A board listing a job again proves it is open, so undo a retirement but never a decision.

    Only a dismissal caused by unlisting is reversible. A skip is the user's call and permanent,
    and every other status is left exactly as it is.
    """
    row = conn.execute(
        "SELECT status, dismissed_reason FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if row is None or row["status"] != Status.DISMISSED.value:
        return
    if row["dismissed_reason"] != DISMISSED_UNLISTED:
        return
    set_job_status(
        conn, job_id, Status.DISCOVERED, "listed again by the source", force=True, source="scan"
    )
    set_dismissed_reason(conn, job_id, None)
    # Back to a plain discovery, score included. A job left in DISCOVERED while still holding a
    # score would never be offered for scoring again (unscored_candidates wants score IS NULL)
    # and DISCOVERED cannot reach SHORTLISTED, so it would be stranded. Clearing the score sends
    # it through prefilter and scoring exactly like anything else newly found.
    conn.execute(
        "UPDATE jobs SET score = NULL, score_reasons = NULL, scored_at = NULL, "
        "prefilter_pass = NULL WHERE id = ?",
        (job_id,),
    )


def _find_by_source(conn: sqlite3.Connection, source: str, source_job_id: str) -> int | None:
    row = conn.execute(
        "SELECT job_id FROM job_sources WHERE source = ? AND source_job_id = ?",
        (source, source_job_id),
    ).fetchone()
    return int(row["job_id"]) if row else None


def _find_by_url(conn: sqlite3.Connection, url_canonical: str) -> int | None:
    row = conn.execute("SELECT id FROM jobs WHERE url_canonical = ?", (url_canonical,)).fetchone()
    return int(row["id"]) if row else None


def _find_by_fingerprint(
    conn: sqlite3.Connection, fingerprint: str, *, exclude_source: str
) -> int | None:
    """Same company+title+city from a different source. Same-source postings never merge."""
    row = conn.execute(
        """
        SELECT j.id FROM jobs j
        WHERE j.fingerprint = ?
          AND NOT EXISTS (SELECT 1 FROM job_sources s WHERE s.job_id = j.id AND s.source = ?)
        ORDER BY j.discovered_at DESC LIMIT 1
        """,
        (fingerprint, exclude_source),
    ).fetchone()
    return int(row["id"]) if row else None


def _priority_of(priority: Mapping[str, int], source: str) -> int:
    return int(priority.get(source, priority.get(DEFAULT_PRIORITY_KEY, DEFAULT_PRIORITY)))


def _insert(conn: sqlite3.Connection, p: JobPosting, k: NormalizedKeys, at: str) -> int:
    cursor = conn.execute(
        """
        INSERT INTO jobs (company, company_norm, title, title_norm, location, location_bucket,
                          is_remote, url, url_canonical, apply_url, ats_type, primary_source,
                          description_md, salary_text, employment_type, posted_at, discovered_at,
                          updated_at, fingerprint, needs_hydration, detail_ref)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            p.company,
            k.company_norm,
            p.title,
            k.title_norm,
            p.location,
            k.location_bucket,
            int(p.is_remote),
            p.url,
            k.url_canonical,
            p.apply_url,
            p.ats_type,
            p.source,
            p.description_md,
            p.salary_text,
            p.employment_type,
            to_iso(p.posted_at) if p.posted_at else None,
            at,
            at,
            k.fingerprint,
            int(p.needs_hydration),
            p.detail_ref,
        ),
    )
    job_id = int(cursor.lastrowid or 0)
    _add_source_row(conn, job_id, p, at)
    return job_id


def _add_source_row(conn: sqlite3.Connection, job_id: int, p: JobPosting, at: str) -> None:
    conn.execute(
        """
        INSERT INTO job_sources (job_id, source, source_job_id, url, first_seen_at,
                                 last_seen_at, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source, source_job_id) DO UPDATE SET last_seen_at = excluded.last_seen_at,
                                                        url = excluded.url,
                                                        raw_json = excluded.raw_json
        """,
        (job_id, p.source, p.source_job_id, p.url, at, at, _raw_json(p)),
    )


def _raw_json(p: JobPosting) -> str | None:
    if not p.raw:
        return None
    try:
        return json.dumps(p.raw, default=str)[:20000]
    except (TypeError, ValueError):
        return None


def _refresh(
    conn: sqlite3.Connection,
    job_id: int,
    p: JobPosting,
    k: NormalizedKeys,
    priority: Mapping[str, int],
    at: str,
    norm: Normalizer,
) -> None:
    _add_source_row(conn, job_id, p, at)
    row = conn.execute("SELECT primary_source FROM jobs WHERE id = ?", (job_id,)).fetchone()
    authoritative = p.source == row["primary_source"] or (
        _priority_of(priority, p.source) > _priority_of(priority, row["primary_source"])
    )
    _apply_content(conn, job_id, p, at, norm, replace=authoritative)
    if authoritative and _find_by_url(conn, k.url_canonical) in (None, job_id):
        conn.execute(
            "UPDATE jobs SET url = ?, url_canonical = ?, primary_source = ? WHERE id = ?",
            (p.url, k.url_canonical, p.source, job_id),
        )


def _merge(
    conn: sqlite3.Connection,
    job_id: int,
    p: JobPosting,
    k: NormalizedKeys,
    priority: Mapping[str, int],
    at: str,
    norm: Normalizer,
) -> None:
    _add_source_row(conn, job_id, p, at)
    row = conn.execute("SELECT primary_source FROM jobs WHERE id = ?", (job_id,)).fetchone()
    higher = _priority_of(priority, p.source) > _priority_of(priority, row["primary_source"])
    _apply_content(conn, job_id, p, at, norm, replace=higher or p.source == row["primary_source"])
    if higher and _find_by_url(conn, k.url_canonical) in (None, job_id):
        conn.execute(
            "UPDATE jobs SET url = ?, url_canonical = ?, primary_source = ?, "
            "ats_type = COALESCE(?, ats_type) WHERE id = ?",
            (p.url, k.url_canonical, p.source, p.ats_type, job_id),
        )
    log.debug("merged %s/%s into job %s", p.source, p.source_job_id, job_id)


def _apply_content(
    conn: sqlite3.Connection,
    job_id: int,
    p: JobPosting,
    at: str,
    norm: Normalizer,
    *,
    replace: bool,
) -> None:
    """Merge source content without replacing hydrated details with a listing summary."""
    before = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    values = _content_values(before, p, replace=replace)
    values["company_norm"] = norm.company(values["company"])
    values["title_norm"] = norm.title(values["title"])
    values["location_bucket"] = norm.bucket(values["location"], bool(values["is_remote"]))
    values["fingerprint"] = fingerprint(
        values["company_norm"], values["title_norm"], values["location_bucket"]
    )
    values["updated_at"] = at
    assignments = ", ".join(f"{key} = ?" for key in values)
    conn.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", (*values.values(), job_id))
    material = ("company", "title", "location", "is_remote", "description_md", "salary_text")
    if any(values[key] != before[key] for key in material):
        _invalidate_content(conn, before)


def _content_values(before: sqlite3.Row, p: JobPosting, *, replace: bool) -> dict[str, Any]:
    fields = (
        "company",
        "title",
        "description_md",
        "apply_url",
        "salary_text",
        "employment_type",
        "location",
        "detail_ref",
        "ats_type",
    )
    values = {key: before[key] for key in fields}
    full_description = bool(before["description_md"]) and not before["needs_hydration"]
    for key in fields:
        incoming = getattr(p, key)
        if key == "description_md" and p.needs_hydration and full_description:
            continue
        if incoming not in (None, "") and (replace or not values[key]):
            values[key] = incoming
    remote = before["is_remote"]
    if (replace and (p.location or not p.needs_hydration)) or not before["location"]:
        remote = int(p.is_remote or is_remote_text(values["location"]))
    values["is_remote"] = remote
    values["posted_at"] = before["posted_at"]
    if p.posted_at is not None and (replace or not before["posted_at"]):
        values["posted_at"] = to_iso(p.posted_at)
    values["needs_hydration"] = before["needs_hydration"]
    if replace:
        if p.needs_hydration:
            values["needs_hydration"] = int(not full_description)
        elif p.description_md:
            values["needs_hydration"] = 0
    return values


def _invalidate_content(conn: sqlite3.Connection, before: sqlite3.Row) -> None:
    machine_stages = (Status.DISCOVERED, Status.FILTERED, Status.SCORED, Status.SHORTLISTED)
    if before["status"] in machine_stages:
        conn.execute(
            "UPDATE jobs SET status = ?, prefilter_pass = NULL, prefilter_reasons = NULL, "
            "skill_overlap = NULL, score = NULL, score_reasons = NULL, "
            "scored_at = NULL WHERE id = ?",
            (Status.DISCOVERED, before["id"]),
        )
    elif before["status"] == Status.APPROVED and before["submission_state"] == "none":
        conn.execute("UPDATE jobs SET prefill_state = 'none' WHERE id = ?", (before["id"],))
    events.add_event(
        conn,
        before["id"],
        EventType.NOTE,
        "Listing details changed; matching or preparation must be refreshed.",
        source="scan",
    )


def set_description(conn: sqlite3.Connection, job_id: int, description_md: str) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE jobs SET description_md = ?, needs_hydration = 0, updated_at = ? WHERE id = ?",
            (description_md, now_iso(), job_id),
        )


def apply_hydration(
    conn: sqlite3.Connection, job_id: int, result: JobPosting, norm: Normalizer
) -> None:
    """Persist hydrated details and recompute matching keys from the corrected content."""
    with transaction(conn):
        _apply_content(
            conn,
            job_id,
            replace_posting(result, needs_hydration=False),
            now_iso(),
            norm,
            replace=True,
        )
        conn.execute("UPDATE jobs SET needs_hydration = 0 WHERE id = ?", (job_id,))
        _invalidate_provisional_prefilter(conn, job_id, True)


def _invalidate_provisional_prefilter(
    conn: sqlite3.Connection, job_id: int, got_description: bool
) -> None:
    """A verdict reached with no description is provisional; a description makes it stale.

    Clearing prefilter_pass puts the job back in the queue that `prefilter` picks up, so the
    skill and deal-breaker checks finally run against the real text. Jobs that already have a
    score or a decision are left alone: re-filtering them would undo a human choice.
    """
    if not got_description:
        return
    conn.execute(
        """
        UPDATE jobs SET prefilter_pass = NULL, prefilter_reasons = NULL
        WHERE id = ? AND status = 'discovered' AND score IS NULL
          AND prefilter_reasons LIKE ?
        """,
        (job_id, f"%{NO_DESCRIPTION_REASON}%"),
    )


def needing_hydration(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    """Prefilter-passed jobs first: they are the ones waiting to be scored."""
    return conn.execute(
        "SELECT * FROM jobs WHERE needs_hydration = 1 AND status = 'discovered' "
        "ORDER BY COALESCE(prefilter_pass, 0) DESC, discovered_at DESC LIMIT ?",
        (limit,),
    ).fetchall()


def clear_hydration(conn: sqlite3.Connection, job_id: int, note: str) -> None:
    """Give up hydrating (after repeated failures) so the summary can be scored as is."""
    with transaction(conn):
        conn.execute(
            "UPDATE jobs SET needs_hydration = 0, updated_at = ? WHERE id = ?",
            (now_iso(), job_id),
        )
        _invalidate_provisional_prefilter(conn, job_id, True)
        events.add_event(conn, job_id, EventType.NOTE, note, source="scan")


def hydrate_failures(conn: sqlite3.Connection, job_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE job_id = ? AND event_type = ?",
        (job_id, EventType.HYDRATE_FAILED.value),
    ).fetchone()
    return int(row["n"])


def mark_seen(conn: sqlite3.Connection, posting: JobPosting, norm: Normalizer, at: str) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE job_sources SET last_seen_at = ? "
            "WHERE source = ? AND source_job_id = ? "
            "AND job_id IN (SELECT id FROM jobs WHERE company_norm = ?)",
            (at, posting.source, posting.source_job_id, norm.company(posting.company)),
        )


DISMISSED_UNLISTED = "unlisted"
DISMISSED_SKIPPED = "skipped"


def set_dismissed_reason(conn: sqlite3.Connection, job_id: int, reason: str | None) -> None:
    """Why this job is dismissed. Only 'unlisted' is reversible when a board relists the job."""
    conn.execute("UPDATE jobs SET dismissed_reason = ? WHERE id = ?", (reason, job_id))


def mark_unlisted(
    conn: sqlite3.Connection, source: str, company_norm: str, seen_before: str
) -> tuple[int, int]:
    """Jobs from `source` at this company that a full fetch no longer returned.

    Pre-decision jobs are dismissed with a note; approved jobs only get the note (the user
    decided to apply, so they should see it rather than lose it). Returns (dismissed, noted).
    """
    with transaction(conn):
        return _mark_unlisted(conn, source, company_norm, seen_before)


def _mark_unlisted(
    conn: sqlite3.Connection, source: str, company_norm: str, seen_before: str
) -> tuple[int, int]:
    rows = conn.execute(
        """
        SELECT j.id, j.status FROM jobs j
        WHERE j.primary_source = ? AND j.company_norm = ?
          AND EXISTS (SELECT 1 FROM job_sources s
                      WHERE s.job_id = j.id AND s.source = j.primary_source)
          AND NOT EXISTS (SELECT 1 FROM job_sources s WHERE s.job_id = j.id
                          AND s.source = j.primary_source AND s.last_seen_at >= ?)
          AND j.status IN ('discovered', 'filtered', 'scored', 'shortlisted', 'approved')
        """,
        (source, company_norm, seen_before),
    ).fetchall()
    dismissed = noted = 0
    note = f"no longer listed at {source}"
    for row in rows:
        if row["status"] == Status.APPROVED.value:
            with transaction(conn):
                events.add_event(conn, row["id"], EventType.UNLISTED, note, source="scan")
            noted += 1
            continue
        set_job_status(conn, row["id"], Status.DISMISSED, note, force=True, source="scan")
        with transaction(conn):
            set_dismissed_reason(conn, row["id"], DISMISSED_UNLISTED)
            events.add_event(conn, row["id"], EventType.UNLISTED, note, source="scan")
        dismissed += 1
    return dismissed, noted


# --------------------------------------------------------------------------- status


def set_job_status(
    conn: sqlite3.Connection,
    job_id: int,
    to: Status | str,
    note: str | None = None,
    *,
    force: bool = False,
    source: str = events.DEFAULT_SOURCE,
    at: str | None = None,
    external_id: str | None = None,
) -> Status:
    """Move a job through the pipeline, keeping jobs, applications and events consistent."""
    target = Status(to)
    stamp = at or now_iso()
    with transaction(conn):
        current = current_status(conn, job_id)
        if target == current and not force:
            return current
        if target not in APPLICATION_STAGES and applications.get_for_job(conn, job_id) is not None:
            raise InvalidTransition("An existing application must remain in an application stage.")
        if not force and target not in TRANSITIONS.get(current, frozenset()):
            raise InvalidTransition(f"job {job_id}: {current} -> {target} is not allowed")
        conn.execute(
            "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?", (str(target), stamp, job_id)
        )
        app_id = None
        if target in APPLICATION_STAGES:
            app_id = applications.ensure_for_status(conn, job_id, target, stamp)
        events.add_event(
            conn,
            job_id,
            EventType.STATUS_CHANGE,
            note,
            application_id=app_id,
            from_status=str(current),
            to_status=str(target),
            source=source,
            at=stamp,
            external_id=external_id,
        )
    log.info("job %s: %s -> %s%s", job_id, current, target, f" ({note})" if note else "")
    return target


def add_note(conn: sqlite3.Connection, job_id: int, note: str, source: str = "cli") -> int:
    get_job(conn, job_id)
    with transaction(conn):
        app_row = applications.get_for_job(conn, job_id)
        return events.add_event(
            conn,
            job_id,
            EventType.NOTE,
            note,
            application_id=app_row["id"] if app_row else None,
            source=source,
        )


def set_snooze(conn: sqlite3.Connection, job_id: int, until_iso: str | None) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE jobs SET snooze_until = ?, updated_at = ? WHERE id = ?",
            (until_iso, now_iso(), job_id),
        )


def set_prefill_state(conn: sqlite3.Connection, job_id: int, state: str) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE jobs SET prefill_state = ?, updated_at = ? WHERE id = ?",
            (state, now_iso(), job_id),
        )


# --------------------------------------------------------------------------- matching support


def mark_prefilter(conn: sqlite3.Connection, job_id: int, result: PrefilterResult) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE jobs SET prefilter_pass = ?, prefilter_reasons = ?, skill_overlap = ?, "
            "updated_at = ? WHERE id = ?",
            (
                int(result.passed),
                json.dumps(result.reasons),
                result.overlap,
                now_iso(),
                job_id,
            ),
        )


def apply_score(
    conn: sqlite3.Connection,
    job_id: int,
    score: int,
    reasons: dict[str, Any],
    *,
    at: str | None = None,
) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE jobs SET score = ?, score_reasons = ?, scored_at = ?, updated_at = ? "
            "WHERE id = ?",
            (score, json.dumps(reasons), at or now_iso(), at or now_iso(), job_id),
        )


def unscored_candidates(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    """Jobs the prefilter passed, plus those it parked for an eligibility verdict.

    A parked job is filtered but undecided: the rules could not tell whether the user may
    work it. It still needs a score and a verdict, so it belongs in the batch.
    """
    return conn.execute(
        """
        SELECT * FROM jobs
        WHERE score IS NULL AND needs_hydration = 0
          AND (
            (status = 'discovered' AND prefilter_pass = 1)
            OR (status = 'filtered' AND prefilter_reasons LIKE ?)
          )
        ORDER BY skill_overlap DESC, discovered_at DESC
        LIMIT ?
        """,
        (f"%{ELIGIBILITY_REVIEW_REASON}%", limit),
    ).fetchall()


def prefilter_candidates(conn: sqlite3.Connection, *, rescan: bool) -> list[sqlite3.Row]:
    if rescan:
        return conn.execute(
            "SELECT * FROM jobs WHERE status IN ('discovered', 'filtered', 'approved') ORDER BY id"
        ).fetchall()
    return conn.execute(
        "SELECT * FROM jobs WHERE status = 'discovered' AND prefilter_pass IS NULL ORDER BY id"
    ).fetchall()


def settle_eligibility(
    conn: sqlite3.Connection, job_id: int, verdict: str, *, eligible: bool
) -> None:
    """Write the eligibility verdict into prefilter_reasons so the job leaves the queue."""
    row = conn.execute("SELECT prefilter_reasons FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise JobNotFound(f"job {job_id} not found")
    conn.execute(
        "UPDATE jobs SET prefilter_reasons = ?, prefilter_pass = ?, updated_at = ? WHERE id = ?",
        (
            record_eligibility_verdict(row["prefilter_reasons"], verdict),
            int(eligible),
            now_iso(),
            job_id,
        ),
    )
    events.add_event(conn, job_id, EventType.NOTE, f"eligibility {verdict}", source="cli")
