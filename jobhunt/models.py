"""Domain models shared by every layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class SubmissionState(StrEnum):
    NONE = "none"
    PENDING = "pending"
    UNKNOWN = "unknown"
    CONFIRMED = "confirmed"


class Status(StrEnum):
    """One pipeline enum. Pre-apply stages live on jobs; post-apply stages also on applications."""

    DISCOVERED = "discovered"
    FILTERED = "filtered"
    SCORED = "scored"
    SHORTLISTED = "shortlisted"
    APPROVED = "approved"
    DISMISSED = "dismissed"
    APPLIED = "applied"
    SCREENING = "screening"
    INTERVIEW = "interview"
    OFFER = "offer"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    GHOSTED = "ghosted"


JOB_STAGES: frozenset[Status] = frozenset(
    {
        Status.DISCOVERED,
        Status.FILTERED,
        Status.SCORED,
        Status.SHORTLISTED,
        Status.APPROVED,
        Status.DISMISSED,
    }
)

APPLICATION_STAGES: frozenset[Status] = frozenset(
    {
        Status.APPLIED,
        Status.SCREENING,
        Status.INTERVIEW,
        Status.OFFER,
        Status.REJECTED,
        Status.WITHDRAWN,
        Status.GHOSTED,
    }
)

ACTIVE_APPLICATION_STAGES: frozenset[Status] = frozenset(
    {Status.APPLIED, Status.SCREENING, Status.INTERVIEW}
)

_POST_APPLY_EXITS = frozenset(
    {
        Status.SCREENING,
        Status.INTERVIEW,
        Status.OFFER,
        Status.REJECTED,
        Status.WITHDRAWN,
        Status.GHOSTED,
    }
)

TRANSITIONS: dict[Status, frozenset[Status]] = {
    Status.DISCOVERED: frozenset({Status.FILTERED, Status.SCORED, Status.DISMISSED}),
    Status.FILTERED: frozenset({Status.DISCOVERED, Status.DISMISSED}),
    Status.SCORED: frozenset({Status.SHORTLISTED, Status.DISMISSED}),
    Status.SHORTLISTED: frozenset({Status.APPROVED, Status.DISMISSED}),
    Status.APPROVED: frozenset({Status.APPLIED, Status.SHORTLISTED, Status.DISMISSED}),
    Status.DISMISSED: frozenset({Status.SHORTLISTED}),
    Status.APPLIED: _POST_APPLY_EXITS,
    Status.SCREENING: frozenset(
        {Status.INTERVIEW, Status.OFFER, Status.REJECTED, Status.WITHDRAWN, Status.GHOSTED}
    ),
    Status.INTERVIEW: frozenset({Status.OFFER, Status.REJECTED, Status.WITHDRAWN, Status.GHOSTED}),
    Status.OFFER: frozenset({Status.WITHDRAWN, Status.REJECTED}),
    Status.GHOSTED: frozenset({Status.SCREENING, Status.INTERVIEW, Status.OFFER, Status.REJECTED}),
    Status.REJECTED: frozenset(),
    Status.WITHDRAWN: frozenset(),
}


class EventType(StrEnum):
    STATUS_CHANGE = "status_change"
    NOTE = "note"
    PREFILTER = "prefilter"
    SCORED = "scored"
    EMAIL_RECEIVED = "email_received"
    MAIL_UNCLASSIFIED = "mail_unclassified"
    FOLLOW_UP_SENT = "follow_up_sent"
    FOLLOWUP_SUGGESTED = "followup_suggested"
    INTERVIEW_SCHEDULED = "interview_scheduled"
    OUTREACH_SENT = "outreach_sent"
    OUTREACH_REPLY = "outreach_reply"
    PREFILLED = "prefilled"
    DRY_RUN = "dry_run"
    SUBMITTED = "submitted"
    CAPTCHA_BLOCKED = "captcha_blocked"
    LOGIN_REQUIRED = "login_required"
    DUPLICATE_BLOCKED = "duplicate_blocked"
    SAFETY_BLOCKED = "safety_blocked"
    VALIDATION_ERROR = "validation_error"
    UNMATCHED_QUESTIONS = "unmatched_questions"
    MAIL_NEEDS_REVIEW = "mail_needs_review"
    GHOST_CHECK = "ghost_check"
    OUTREACH_DRAFTED = "outreach_drafted"
    OUTREACH_OPENED = "outreach_opened"
    CONTACT_IMPORTED = "contact_imported"
    SUBMISSION_UNKNOWN = "submission_unknown"
    SUBMISSION_PENDING = "submission_pending"
    SUBMISSION_RESOLVED = "submission_resolved"
    HYDRATE_FAILED = "hydrate_failed"
    UNLISTED = "unlisted"
    ROUTE_DECIDED = "route_decided"
    REFERRAL_WAIT = "referral_wait"
    REFERRAL_RECEIVED = "referral_received"
    REFERRAL_DEADLINE = "referral_deadline"


class UpsertOutcome(StrEnum):
    CREATED = "created"
    MERGED = "merged"
    REFRESHED = "refreshed"


@dataclass(slots=True)
class JobPosting:
    """What every source fetcher returns. Sources fill what they can; None is fine."""

    source: str
    source_job_id: str
    company: str
    title: str
    url: str
    location: str | None = None
    is_remote: bool = False
    apply_url: str | None = None
    ats_type: str | None = None
    description_md: str | None = None
    posted_at: datetime | None = None
    salary_text: str | None = None
    employment_type: str | None = None
    needs_hydration: bool = False
    detail_ref: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NormalizedKeys:
    company_norm: str
    title_norm: str
    location_bucket: str
    url_canonical: str
    fingerprint: str


# A prefilter verdict reached before the description arrived. Hydration makes it stale.
NO_DESCRIPTION_REASON = "no description yet"
ELIGIBILITY_REVIEW_REASON = "eligibility needs verification"
ELIGIBILITY_CHECKED_PREFIX = "eligibility checked"
ELIGIBILITY_VERDICTS = ("eligible", "not_eligible", "unclear")


@dataclass(slots=True)
class PrefilterResult:
    passed: bool
    reasons: list[str]
    overlap: float
    min_years: int | None


@dataclass(slots=True)
class ScoredJob:
    job_id: int
    score: int
    reasons: list[str]
    red_flags: list[str]
    resume_variant_hint: str | None = None
    eligibility: str | None = None
