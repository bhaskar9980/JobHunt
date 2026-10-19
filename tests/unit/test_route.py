"""Referral-first routing: apply now versus ask first, and the waiting/deadline/received states."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jobhunt.clock import to_iso
from jobhunt.referral.ranker import Suggestion
from jobhunt.referral.route import (
    ROUTE_APPLY,
    ROUTE_REFERRAL_DEADLINE,
    ROUTE_REFERRAL_FIRST,
    ROUTE_REFERRAL_PENDING,
    ROUTE_REFERRAL_RECEIVED,
    recommend,
)
from jobhunt.referral.rules import ReferralConfig

NOW = datetime(2026, 9, 9, 9, 0, tzinfo=UTC)
CFG = ReferralConfig.from_dict(
    {"routing": {"min_contact_score": 50, "wait_days": 5, "posted_within_days": 21}}
)


def _job(**overrides):
    base = {"id": 1, "posted_at": to_iso(NOW - timedelta(days=3)), "snooze_until": None}
    base.update(overrides)
    return base


def _contact(score: int, name: str = "Priya") -> Suggestion:
    return Suggestion(1, name, "Backend Engineer", "https://linkedin.com/in/p", score, ["x"])


def _outreach(**overrides):
    base = {
        "kind": "referral",
        "contact_name": "Priya",
        "sent_at": to_iso(NOW - timedelta(days=2)),
        "replied_at": None,
        "response": None,
        "referral_outcome": "pending",
    }
    base.update(overrides)
    return base


def test_no_contacts_means_apply():
    route = recommend(_job(), [], [], CFG, now=NOW)
    assert route.route == ROUTE_APPLY
    assert "no contacts" in route.reason


def test_weak_contact_does_not_delay_application():
    route = recommend(_job(), [_contact(30)], [], CFG, now=NOW)
    assert route.route == ROUTE_APPLY
    assert "below 50" in route.reason


def test_strong_contact_and_fresh_posting_recommends_referral_first():
    route = recommend(_job(), [_contact(70)], [], CFG, now=NOW)
    assert route.route == ROUTE_REFERRAL_FIRST
    assert route.contact_name == "Priya" and route.contact_score == 70
    assert "5 days" in route.reason


def test_old_posting_applies_directly_even_with_strong_contact():
    old = _job(posted_at=to_iso(NOW - timedelta(days=40)))
    route = recommend(old, [_contact(90)], [], CFG, now=NOW)
    assert route.route == ROUTE_APPLY
    assert "40 days ago" in route.reason


def test_unknown_posting_date_still_allows_referral_first():
    route = recommend(_job(posted_at=None), [_contact(90)], [], CFG, now=NOW)
    assert route.route == ROUTE_REFERRAL_FIRST


def test_sent_request_with_future_deadline_is_pending():
    job = _job(snooze_until=to_iso(NOW + timedelta(days=3)))
    route = recommend(job, [_contact(90)], [_outreach()], CFG, now=NOW)
    assert route.route == ROUTE_REFERRAL_PENDING
    assert route.wait_until == job["snooze_until"]


def test_sent_request_past_deadline_says_apply_directly():
    job = _job(snooze_until=to_iso(NOW - timedelta(hours=1)))
    route = recommend(job, [_contact(90)], [_outreach()], CFG, now=NOW)
    assert route.route == ROUTE_REFERRAL_DEADLINE
    assert "apply directly" in route.reason


def test_referred_reply_wins_over_everything():
    replied = _outreach(
        replied_at=to_iso(NOW), response="Use this link", referral_outcome="completed"
    )
    job = _job(snooze_until=to_iso(NOW + timedelta(days=3)))
    route = recommend(job, [_contact(20)], [replied], CFG, now=NOW)
    assert route.route == ROUTE_REFERRAL_RECEIVED
    assert "Priya" in route.reason


@pytest.mark.parametrize(
    "response", ["not referred", "I haven't referred you yet", "I can refer you", "referred"]
)
def test_reply_text_never_completes_or_restarts_a_pending_referral(response):
    job = _job(snooze_until=to_iso(NOW + timedelta(days=3)))
    replied = _outreach(replied_at=to_iso(NOW), response=response)
    route = recommend(job, [_contact(90)], [replied], CFG, now=NOW)
    assert route.route == ROUTE_REFERRAL_PENDING
    assert route.wait_until == job["snooze_until"]


def test_unresolved_reply_past_deadline_allows_direct_application():
    replied = _outreach(replied_at=to_iso(NOW), response="I'll check tomorrow")
    route = recommend(_job(), [_contact(90)], [replied], CFG, now=NOW)
    assert route.route == ROUTE_REFERRAL_DEADLINE
    assert "no reply" not in route.reason


def test_declined_referral_allows_apply_instead_of_asking_again():
    replied = _outreach(
        replied_at=to_iso(NOW), response="Not referred", referral_outcome="declined"
    )
    route = recommend(_job(), [_contact(90)], [replied], CFG, now=NOW)
    assert route.route == ROUTE_APPLY
    assert "declined" in route.reason


def test_declined_contact_does_not_end_another_pending_request():
    declined = _outreach(referral_outcome="declined", replied_at=to_iso(NOW))
    pending = _outreach(contact_name="Arjun")
    job = _job(snooze_until=to_iso(NOW + timedelta(days=3)))
    route = recommend(job, [_contact(90)], [declined, pending], CFG, now=NOW)
    assert route.route == ROUTE_REFERRAL_PENDING
    assert route.contact_name == "Arjun"


def test_completed_outcome_does_not_depend_on_reply_metadata():
    route = recommend(_job(), [], [_outreach(referral_outcome="completed")], CFG, now=NOW)
    assert route.route == ROUTE_REFERRAL_RECEIVED


def test_non_referral_outreach_is_ignored():
    note = _outreach(kind="connection_note")
    route = recommend(_job(), [_contact(70)], [note], CFG, now=NOW)
    assert route.route == ROUTE_REFERRAL_FIRST


@pytest.mark.parametrize("kind", ["connection_note", "followup", "inbound"])
@pytest.mark.parametrize("outcome", ["completed", "declined"])
def test_explicit_outcome_is_authoritative_even_without_a_referral_request(kind, outcome):
    message = _outreach(kind=kind, referral_outcome=outcome)
    route = recommend(_job(), [_contact(90)], [message], CFG, now=NOW)
    assert route.route == (ROUTE_REFERRAL_RECEIVED if outcome == "completed" else ROUTE_APPLY)


def test_routing_can_be_disabled():
    cfg = ReferralConfig.from_dict({"routing": {"enabled": False}})
    assert recommend(_job(), [_contact(90)], [], cfg, now=NOW).route == ROUTE_APPLY
