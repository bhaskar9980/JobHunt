"""Safety gate: rules every submission must pass, checked right before the browser opens."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from rapidfuzz import fuzz

from jobhunt.clock import now, parse_iso, to_iso
from jobhunt.db import transaction
from jobhunt.models import Status
from jobhunt.repo import applications as applications_repo
from jobhunt.repo import jobs as jobs_repo

DEFAULT_DAILY_CAP = 10
DEFAULT_WORKDAY_CAP = 3
DEFAULT_MIN_SCORE = 70
DEFAULT_DEDUPE_DAYS = 90
DEFAULT_TITLE_RATIO = 90
READY_STATE = "ready"
WORKDAY = "workday"
UNCONFIRMED_REASON = (
    "a previous submission was not confirmed; check Gmail or the site, then `mark-applied` "
    "if it went through or `reconcile-submission --not-submitted --note ...` if it did not"
)


@dataclass(slots=True)
class Verdict:
    ok: bool
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SafetyGate:
    conn: sqlite3.Connection
    daily_cap: int = DEFAULT_DAILY_CAP
    workday_daily_cap: int = DEFAULT_WORKDAY_CAP
    min_score: int = DEFAULT_MIN_SCORE
    dedupe_days: int = DEFAULT_DEDUPE_DAYS
    dedupe_title_ratio: int = DEFAULT_TITLE_RATIO

    @classmethod
    def from_settings(cls, conn: sqlite3.Connection, apply_cfg: Mapping[str, Any]) -> SafetyGate:
        return cls(
            conn=conn,
            daily_cap=int(apply_cfg.get("daily_cap", DEFAULT_DAILY_CAP)),
            workday_daily_cap=int(apply_cfg.get("workday_daily_cap", DEFAULT_WORKDAY_CAP)),
            min_score=int(apply_cfg.get("min_score", DEFAULT_MIN_SCORE)),
            dedupe_days=int(apply_cfg.get("dedupe_days", DEFAULT_DEDUPE_DAYS)),
            dedupe_title_ratio=int(apply_cfg.get("dedupe_title_ratio", DEFAULT_TITLE_RATIO)),
        )

    def check(self, job: sqlite3.Row, ats: str | None, *, require_ready: bool = True) -> Verdict:
        reasons: list[str] = []
        if job["pipeline_status"] != Status.APPROVED.value:
            reasons.append(f"status is {job['pipeline_status']}, expected approved")
        if require_ready and job["prefill_state"] != READY_STATE:
            reasons.append(f"prefill_state is {job['prefill_state']}, expected {READY_STATE}")
        if job["score"] is None or int(job["score"]) < self.min_score:
            reasons.append(f"score {job['score']} below minimum {self.min_score}")
        if job["snooze_until"] and parse_iso(job["snooze_until"]) > now():
            reasons.append("job is snoozed or waiting for a referral")
        if job["prefilter_pass"] == 0:
            reasons.append("job does not pass the current profile rules")
        reasons.extend(self._cap_reasons(ats))
        duplicate = self.recent_duplicate(job)
        if duplicate is not None:
            reasons.append(duplicate)
        if self.unconfirmed_submission(int(job["id"])):
            reasons.append(UNCONFIRMED_REASON)
        return Verdict(ok=not reasons, reasons=reasons)

    def reserve(self, job_id: int, ats: str | None) -> Verdict:
        with transaction(self.conn):
            verdict = self.check(jobs_repo.get_job(self.conn, job_id), ats)
            if not verdict.ok:
                return verdict
            if not applications_repo.begin_submission(self.conn, job_id):
                return Verdict(False, ["submission reservation refused; check job state"])
            return verdict

    def unconfirmed_submission(self, job_id: int) -> bool:
        """Pending attempts require confirmed application evidence or explicit reconciliation."""
        return applications_repo.submission_unresolved(self.conn, job_id)

    def submitted_today(self, ats: str | None = None) -> int:
        today = to_iso(now())[:10]
        sql = (
            "SELECT COUNT(*) AS n FROM jobs j LEFT JOIN applications a ON j.id = a.job_id "
            "WHERE substr(COALESCE(a.submitted_at, a.applied_at, "
            "CASE WHEN j.submission_state IN ('pending','unknown') "
            "THEN j.submission_started_at END), "
            "1, 10) = ?"
        )
        params: list[object] = [today]
        if ats:
            sql += " AND j.ats_type = ?"
            params.append(ats)
        return int(self.conn.execute(sql, params).fetchone()["n"])

    def _cap_reasons(self, ats: str | None) -> list[str]:
        reasons: list[str] = []
        total = self.submitted_today()
        if total >= self.daily_cap:
            reasons.append(f"daily cap reached ({total}/{self.daily_cap})")
        if ats == WORKDAY:
            workday_total = self.submitted_today(WORKDAY)
            if workday_total >= self.workday_daily_cap:
                reasons.append(
                    f"workday daily cap reached ({workday_total}/{self.workday_daily_cap})"
                )
        return reasons

    def recent_duplicate(self, job: sqlite3.Row) -> str | None:
        cutoff = to_iso(now() - timedelta(days=self.dedupe_days))
        rows = self.conn.execute(
            """
            SELECT j.id, j.title, j.title_norm, a.applied_at
            FROM applications a JOIN jobs j ON j.id = a.job_id
            WHERE j.company_norm = ? AND j.id != ? AND COALESCE(a.applied_at, '') >= ?
            """,
            (job["company_norm"], job["id"], cutoff),
        ).fetchall()
        for row in rows:
            ratio = fuzz.token_set_ratio(job["title_norm"] or "", row["title_norm"] or "")
            if ratio >= self.dedupe_title_ratio:
                return (
                    f"already applied to {job['company']} for '{row['title']}' "
                    f"on {row['applied_at']} (job {row['id']}, title match {ratio:.0f}%)"
                )
        return None
