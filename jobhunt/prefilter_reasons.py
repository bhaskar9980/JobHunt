"""Reading and rewriting the prefilter's reason list.

It lives apart from `match` so the jobs repository can use it without importing the
prefilter, which imports the repository.
"""

from __future__ import annotations

import json

from jobhunt.models import ELIGIBILITY_CHECKED_PREFIX, ELIGIBILITY_REVIEW_REASON


def eligibility_review_pending(prefilter_reasons: str | None) -> bool:
    """True while a job is parked waiting for a human-grade eligibility verdict."""
    return ELIGIBILITY_REVIEW_REASON in (prefilter_reasons or "")


def eligibility_verdict(prefilter_reasons: str | None) -> str | None:
    """The verdict already recorded for a job, or None while none has been given."""
    marker = f"{ELIGIBILITY_CHECKED_PREFIX}: "
    for reason in _reason_list(prefilter_reasons):
        if reason.startswith(marker):
            return reason[len(marker) :].strip()
    return None


def eligibility_settled_ok(prefilter_reasons: str | None) -> bool:
    """True when a human-grade verdict has cleared the job to stay in the pipeline."""
    return eligibility_verdict(prefilter_reasons) == "eligible"


def _reason_list(prefilter_reasons: str | None) -> list[str]:
    try:
        reasons = json.loads(prefilter_reasons or "[]")
    except (TypeError, ValueError):
        return []
    return [str(reason) for reason in reasons] if isinstance(reasons, list) else []


def record_eligibility_verdict(prefilter_reasons: str | None, verdict: str) -> str:
    """Replace the pending marker with the verdict so the job leaves the queue."""
    kept = [r for r in _reason_list(prefilter_reasons) if ELIGIBILITY_REVIEW_REASON not in r]
    return json.dumps([*kept, f"{ELIGIBILITY_CHECKED_PREFIX}: {verdict}"])
