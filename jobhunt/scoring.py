"""LLM scoring round-trip: Python exports a batch, the /jobs-scan skill scores, Python imports."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jobhunt.clock import now_iso
from jobhunt.config import Profile
from jobhunt.db import transaction
from jobhunt.models import (
    ELIGIBILITY_REVIEW_REASON,
    ELIGIBILITY_VERDICTS,
    ScoredJob,
    Status,
)
from jobhunt.prefilter_reasons import eligibility_review_pending
from jobhunt.repo import jobs as jobs_repo

log = logging.getLogger(__name__)

SCORE_MIN = 0
SCORE_MAX = 100
RUBRIC_RELATIVE_PATH = "config/scoring_rubric.md"
SCHEMA_VERSION = 1
VARIANT_HINTS = ("dotnet-backend", "python-platform", "general")
ELIGIBILITY_QUESTION = (
    f"{ELIGIBILITY_REVIEW_REASON}: read the posting and answer 'eligibility' with "
    "'eligible' only when it states remote work open to India or worldwide, or states both "
    "visa sponsorship and relocation assistance; 'not_eligible' only when the posting itself "
    "states an explicit restriction, such as remote limited to named countries, 'must reside "
    "in', sponsorship unavailable, a clearance or citizenship requirement, or export control; "
    "'unclear' for everything else. A foreign location alone is not a restriction, so an "
    "overseas posting that says nothing about visas is 'unclear', never 'not_eligible'. "
    "Never infer eligibility from the employer's reputation."
)


@dataclass(slots=True)
class ImportSummary:
    imported: int = 0
    shortlisted: int = 0
    errors: list[str] = field(default_factory=list)


def export_batch(conn: sqlite3.Connection, profile: Profile, out_path: Path, limit: int) -> int:
    rows = jobs_repo.unscored_candidates(conn, limit)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "batch_id": now_iso(),
        "rubric_path": RUBRIC_RELATIVE_PATH,
        "shortlist_min_score": profile.scoring.shortlist_min_score,
        "profile": _profile_summary(profile),
        "jobs": [_export_row(row, profile.scoring.description_max_chars) for row in rows],
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    log.info("exported %s jobs for scoring to %s", len(rows), out_path)
    return len(rows)


def _profile_summary(profile: Profile) -> dict[str, Any]:
    return {
        "candidate": profile.candidate,
        "target_titles": profile.targets.target_titles,
        "locations": profile.targets.locations,
        "remote_ok": profile.targets.remote_ok,
        "core_skills": profile.skills.core,
        "nice_to_have": profile.skills.nice_to_have,
        "deal_breakers": profile.skills.deal_breakers,
        "comp": profile.comp,
    }


def _export_row(row: sqlite3.Row, max_chars: int) -> dict[str, Any]:
    description = row["description_md"] or ""
    pending = eligibility_review_pending(row["prefilter_reasons"])
    return {
        "eligibility_review": pending,
        "eligibility_question": ELIGIBILITY_QUESTION if pending else None,
        "job_id": row["id"],
        "company": row["company"],
        "title": row["title"],
        "location": row["location"],
        "is_remote": bool(row["is_remote"]),
        "url": row["url"],
        "salary_text": row["salary_text"],
        "employment_type": row["employment_type"],
        "posted_at": row["posted_at"],
        "skill_overlap": row["skill_overlap"],
        "prefilter_reasons": json.loads(row["prefilter_reasons"] or "[]"),
        "description_md": description[:max_chars],
    }


def import_batch(conn: sqlite3.Connection, in_path: Path, profile: Profile) -> ImportSummary:
    payload = json.loads(in_path.read_text(encoding="utf-8"))
    results = payload.get("results") if isinstance(payload, dict) else payload
    if not isinstance(results, list):
        raise ValueError("scored file must contain a 'results' list")
    known = _scorable_ids(conn)
    scored, errors = validate_scored(results, known, _eligibility_ids(conn))
    summary = ImportSummary(errors=errors)
    for item in scored:
        _apply(conn, item, profile.scoring.shortlist_min_score, summary)
    log.info(
        "imported %s scores (%s shortlisted, %s errors)",
        summary.imported,
        summary.shortlisted,
        len(summary.errors),
    )
    return summary


def _scorable_ids(conn: sqlite3.Connection) -> set[int]:
    rows = conn.execute(
        "SELECT id FROM jobs WHERE needs_hydration = 0 AND ("
        "  (status IN ('discovered', 'scored', 'shortlisted') AND prefilter_pass = 1)"
        "  OR (status = 'filtered' AND prefilter_reasons LIKE ?)"
        ")",
        (f"%{ELIGIBILITY_REVIEW_REASON}%",),
    ).fetchall()
    return {int(r["id"]) for r in rows}


def _eligibility_ids(conn: sqlite3.Connection) -> set[int]:
    rows = conn.execute(
        "SELECT id FROM jobs WHERE status = 'filtered' AND needs_hydration = 0 "
        "AND prefilter_reasons LIKE ?",
        (f"%{ELIGIBILITY_REVIEW_REASON}%",),
    ).fetchall()
    return {int(r["id"]) for r in rows}


def _apply(
    conn: sqlite3.Connection, item: ScoredJob, threshold: int, summary: ImportSummary
) -> None:
    reasons = {
        "reasons": item.reasons,
        "red_flags": item.red_flags,
        "resume_variant_hint": item.resume_variant_hint,
    }
    with transaction(conn):
        row = jobs_repo.get_job(conn, item.job_id)
        current = Status(row["pipeline_status"])
        parked = current == Status.FILTERED and eligibility_review_pending(row["prefilter_reasons"])
        if row["needs_hydration"] or not (
            parked
            or (
                current in (Status.DISCOVERED, Status.SCORED, Status.SHORTLISTED)
                and row["prefilter_pass"] == 1
            )
        ):
            summary.errors.append(f"job {item.job_id} changed and is no longer ready to score")
            return
        jobs_repo.apply_score(conn, item.job_id, item.score, reasons)
        if parked:
            current = _settle_eligibility(conn, item, row, summary)
            if current is None:
                return
        note = f"score {item.score}: {'; '.join(item.reasons)[:300]}"
        if current == Status.DISCOVERED:
            jobs_repo.set_job_status(conn, item.job_id, Status.SCORED, note)
            current = Status.SCORED
        summary.imported += 1
        if item.score >= threshold and current == Status.SCORED:
            jobs_repo.set_job_status(
                conn, item.job_id, Status.SHORTLISTED, f"score {item.score} >= {threshold}"
            )
            summary.shortlisted += 1
        elif item.score < threshold and current == Status.SHORTLISTED:
            jobs_repo.set_job_status(conn, item.job_id, Status.SCORED, note, force=True)


def _settle_eligibility(
    conn: sqlite3.Connection, item: ScoredJob, row: sqlite3.Row, summary: ImportSummary
) -> Status | None:
    """Record the verdict. Only an 'eligible' job rejoins the pipeline as scored."""
    verdict = item.eligibility or "unclear"
    eligible = verdict == "eligible"
    jobs_repo.settle_eligibility(conn, item.job_id, verdict, eligible=eligible)
    if not eligible:
        summary.imported += 1
        return None
    jobs_repo.set_job_status(
        conn, item.job_id, Status.DISCOVERED, f"eligibility {verdict}", force=True
    )
    return Status.DISCOVERED


def validate_scored(
    results: list[Any], known_ids: set[int], eligibility_ids: set[int] | None = None
) -> tuple[list[ScoredJob], list[str]]:
    needs_verdict = eligibility_ids or set()
    scored: list[ScoredJob] = []
    errors: list[str] = []
    seen: set[int] = set()
    for index, raw in enumerate(results):
        problem = _validate_one(raw, known_ids, seen) or _validate_eligibility(raw, needs_verdict)
        if problem:
            errors.append(f"result[{index}]: {problem}")
            continue
        seen.add(int(raw["job_id"]))
        scored.append(
            ScoredJob(
                job_id=int(raw["job_id"]),
                score=int(raw["score"]),
                reasons=[str(r) for r in raw.get("reasons", [])],
                red_flags=[str(r) for r in raw.get("red_flags", []) or []],
                resume_variant_hint=_hint(raw.get("resume_variant_hint")),
                eligibility=raw.get("eligibility"),
            )
        )
    return scored, errors


def _validate_eligibility(raw: Any, needs_verdict: set[int]) -> str | None:
    if not isinstance(raw, dict) or raw.get("job_id") not in needs_verdict:
        return None
    verdict = raw.get("eligibility")
    if verdict not in ELIGIBILITY_VERDICTS:
        allowed = ", ".join(ELIGIBILITY_VERDICTS)
        return f"eligibility must be one of {allowed} for this job, got {verdict!r}"
    return None


def _validate_one(raw: Any, known_ids: set[int], seen: set[int]) -> str | None:
    if not isinstance(raw, dict):
        return "not an object"
    job_id = raw.get("job_id")
    if type(job_id) is not int or job_id in seen:
        return f"bad or duplicate job_id {job_id!r}"
    if job_id not in known_ids:
        return f"job {job_id} is not awaiting a score"
    score = raw.get("score")
    if type(score) is not int or not SCORE_MIN <= score <= SCORE_MAX:
        return f"score must be an int in [{SCORE_MIN}, {SCORE_MAX}], got {score!r}"
    reasons = raw.get("reasons")
    if (
        not isinstance(reasons, list)
        or not reasons
        or any(not isinstance(reason, str) or not reason.strip() for reason in reasons)
    ):
        return "reasons must be a non-empty list of non-empty strings"
    flags = raw.get("red_flags")
    invalid_flags = flags is not None and (
        not isinstance(flags, list)
        or any(not isinstance(flag, str) or not flag.strip() for flag in flags)
    )
    return "red_flags must be a list of non-empty strings" if invalid_flags else None


def _hint(value: Any) -> str | None:
    if isinstance(value, str) and value in VARIANT_HINTS:
        return value
    return None
