"""Referral first or apply now: one recommendation per job, including the waiting state.

States, derived from outreach rows and the job's snooze:
    apply              no useful contact, declined request, or posting too old to wait
    referral_first     ask the top contact, apply directly if not completed by the deadline
    referral_pending   asked, outcome pending (jobs.snooze_until holds the deadline)
    referral_deadline  no completed referral by the deadline: apply directly now
    referral_received  explicitly recorded as completed: apply through their link
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from jobhunt.clock import parse_iso, to_iso
from jobhunt.referral.ranker import Suggestion
from jobhunt.referral.rules import ReferralConfig
from jobhunt.repo.outreach import OUTCOME_COMPLETED, OUTCOME_DECLINED, OUTCOME_PENDING

ROUTE_APPLY = "apply"
ROUTE_REFERRAL_FIRST = "referral_first"
ROUTE_REFERRAL_PENDING = "referral_pending"
ROUTE_REFERRAL_DEADLINE = "referral_deadline"
ROUTE_REFERRAL_RECEIVED = "referral_received"
KIND_REFERRAL = "referral"
DATE_CHARS = 10


@dataclass(slots=True)
class Route:
    route: str
    reason: str
    contact_name: str | None = None
    contact_score: int | None = None
    wait_until: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def recommend(
    job: Mapping[str, Any],
    suggestions: Sequence[Suggestion],
    outreach_rows: Sequence[Mapping[str, Any]],
    cfg: ReferralConfig,
    *,
    now: datetime,
) -> Route:
    referrals = [
        o
        for o in outreach_rows
        if o["kind"] == KIND_REFERRAL or o["referral_outcome"] != OUTCOME_PENDING
    ]
    received = next((o for o in referrals if is_referred(o)), None)
    if received is not None:
        name = str(received["contact_name"])
        return Route(
            ROUTE_REFERRAL_RECEIVED,
            f"referral received from {name}; apply through their link",
            name,
        )
    pending = [o for o in referrals if o["sent_at"] and o["referral_outcome"] == OUTCOME_PENDING]
    if pending:
        return _pending_route(job, pending[-1], now)
    declined = next((o for o in referrals if o["referral_outcome"] == OUTCOME_DECLINED), None)
    if declined is not None:
        name = str(declined["contact_name"])
        return Route(ROUTE_APPLY, f"referral declined by {name}; apply directly", name)
    return _fresh_route(job, suggestions, cfg, now)


def _pending_route(job: Mapping[str, Any], asked: Mapping[str, Any], now: datetime) -> Route:
    name = str(asked["contact_name"])
    sent_on = str(asked["sent_at"])[:DATE_CHARS]
    wait_until = job["snooze_until"] if "snooze_until" in job.keys() else None  # noqa: SIM118
    if wait_until and str(wait_until) > to_iso(now):
        return Route(
            ROUTE_REFERRAL_PENDING,
            f"asked {name} on {sent_on}; waiting until {str(wait_until)[:DATE_CHARS]}",
            name,
            wait_until=str(wait_until),
        )
    return Route(
        ROUTE_REFERRAL_DEADLINE,
        f"no completed referral from {name} since {sent_on}; apply directly",
        name,
    )


def _fresh_route(
    job: Mapping[str, Any], suggestions: Sequence[Suggestion], cfg: ReferralConfig, now: datetime
) -> Route:
    if not suggestions:
        return Route(ROUTE_APPLY, "no contacts at this company")
    if not cfg.route_enabled:
        return Route(ROUTE_APPLY, "referral routing disabled")
    top = suggestions[0]
    if top.score < cfg.route_min_contact_score:
        return Route(
            ROUTE_APPLY,
            f"best contact {top.name} scores {top.score}, below {cfg.route_min_contact_score}",
            top.name,
            top.score,
        )
    age = _age_days(job.get("posted_at") if isinstance(job, dict) else job["posted_at"], now)
    if age is not None and age > cfg.route_posted_within_days:
        return Route(
            ROUTE_APPLY, f"posted {age} days ago; do not wait for a referral", top.name, top.score
        )
    return Route(
        ROUTE_REFERRAL_FIRST,
        f"ask {top.name} (score {top.score}) first; apply directly after "
        f"{cfg.route_wait_days} days without a completed referral",
        top.name,
        top.score,
    )


def is_referred(outreach: Mapping[str, Any]) -> bool:
    return outreach["referral_outcome"] == OUTCOME_COMPLETED


def _age_days(posted_at: Any, now: datetime) -> int | None:
    posted = parse_iso(posted_at) if isinstance(posted_at, str) else posted_at
    if posted is None:
        return None
    return max(0, (now - posted).days)
