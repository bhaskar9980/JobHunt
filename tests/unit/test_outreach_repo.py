"""Outreach rows: create, the 7-then-14-then-stop follow-up chain, due list, replies, cooldown."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jobhunt.clock import to_iso
from jobhunt.repo import outreach as outreach_repo
from jobhunt.repo.contacts import ContactRecord, get_contact, upsert_contact
from jobhunt.repo.jobs import upsert_job

T0 = datetime(2026, 9, 8, 9, 0, tzinfo=UTC)
SCHEDULE = [7, 14]
FIRST_GAP, SECOND_GAP = 7, 14


@pytest.fixture
def job_id(conn, norm, priority, make_posting):
    return upsert_job(conn, make_posting(), norm, priority).job_id


@pytest.fixture
def contact_id(conn):
    with conn:
        cid, _ = upsert_contact(
            conn,
            ContactRecord(
                name="Priya Natarajan",
                company_norm="acme",
                linkedin_url="https://www.linkedin.com/in/priya",
            ),
        )
    return cid


def _create(conn, contact_id, job_id, kind="referral"):
    with conn:
        return outreach_repo.create(
            conn,
            contact_id=contact_id,
            job_id=job_id,
            channel="linkedin",
            kind=kind,
            draft_path=f"data/outreach/{job_id}/priya.md",
            hook="hook",
        )


def test_create_and_get_join_contact_and_job(conn, contact_id, job_id):
    outreach_id = _create(conn, contact_id, job_id)
    row = outreach_repo.get(conn, outreach_id)
    assert row["contact_name"] == "Priya Natarajan"
    assert row["job_company"] == "Acme, Inc."
    assert row["kind"] == "referral" and row["sent_at"] is None
    assert [r["id"] for r in outreach_repo.for_job(conn, job_id)] == [outreach_id]
    with pytest.raises(ValueError, match="unknown outreach kind"):
        outreach_repo.create(conn, contact_id=contact_id, job_id=job_id, channel="x", kind="nope")
    with pytest.raises(outreach_repo.OutreachNotFound):
        outreach_repo.get(conn, 999)


def test_followup_chain_seven_then_fourteen_then_stop(conn, contact_id, job_id):
    first = _create(conn, contact_id, job_id)
    stage = outreach_repo.followup_stage(conn, first) + 1
    assert stage == 1
    with conn:
        due = outreach_repo.mark_sent(
            conn, first, to_iso(T0), outreach_repo.followup_days_for(stage, SCHEDULE)
        )
    assert due == to_iso(T0 + timedelta(days=FIRST_GAP))
    assert get_contact(conn, contact_id)["last_contacted_at"] == to_iso(T0)

    followup = _create(conn, contact_id, job_id, kind="followup")
    stage = outreach_repo.followup_stage(conn, followup) + 1
    assert stage == 2
    sent2 = T0 + timedelta(days=FIRST_GAP)
    with conn:
        due = outreach_repo.mark_sent(
            conn, followup, to_iso(sent2), outreach_repo.followup_days_for(stage, SCHEDULE)
        )
    assert due == to_iso(sent2 + timedelta(days=SECOND_GAP))
    assert outreach_repo.get(conn, first)["followup_due"] is None, "stale due date cleared"

    last = _create(conn, contact_id, job_id, kind="followup")
    stage = outreach_repo.followup_stage(conn, last) + 1
    assert stage == 3
    assert outreach_repo.followup_days_for(stage, SCHEDULE) is None
    with conn:
        assert outreach_repo.mark_sent(conn, last, to_iso(T0), None) is None
    assert outreach_repo.due(conn, to_iso(T0 + timedelta(days=365))) == []


def test_due_lists_only_sent_unanswered_past_due(conn, contact_id, job_id):
    outreach_id = _create(conn, contact_id, job_id)
    assert outreach_repo.due(conn, to_iso(T0 + timedelta(days=30))) == []
    with conn:
        outreach_repo.mark_sent(conn, outreach_id, to_iso(T0), FIRST_GAP)
    assert outreach_repo.due(conn, to_iso(T0 + timedelta(days=6))) == []
    due_rows = outreach_repo.due(conn, to_iso(T0 + timedelta(days=FIRST_GAP)))
    assert [r["id"] for r in due_rows] == [outreach_id]
    with conn:
        outreach_repo.mark_replied(conn, outreach_id, to_iso(T0 + timedelta(days=8)), "Sure!")
    row = outreach_repo.get(conn, outreach_id)
    assert row["response"] == "Sure!" and row["followup_due"] is None
    assert outreach_repo.due(conn, to_iso(T0 + timedelta(days=30))) == []
    assert outreach_repo.has_reply(conn, contact_id)


def test_last_asked_ignores_inbound_and_unsent(conn, contact_id, job_id):
    assert outreach_repo.last_asked(conn, contact_id) is None
    _create(conn, contact_id, job_id)
    assert outreach_repo.last_asked(conn, contact_id) is None
    inbound = _create(conn, contact_id, None, kind="inbound")
    with conn:
        outreach_repo.mark_sent(conn, inbound, to_iso(T0), None)
    assert outreach_repo.last_asked(conn, contact_id) is None
    asked = _create(conn, contact_id, job_id)
    with conn:
        outreach_repo.mark_sent(conn, asked, to_iso(T0 + timedelta(days=1)), FIRST_GAP)
    assert outreach_repo.last_asked(conn, contact_id) == to_iso(T0 + timedelta(days=1))


def test_mark_sent_replay_preserves_first_send_and_followup(conn, contact_id, job_id):
    outreach_id = _create(conn, contact_id, job_id)
    with conn:
        original = outreach_repo.mark_sent(conn, outreach_id, to_iso(T0), FIRST_GAP)
        repeated = outreach_repo.mark_sent(
            conn, outreach_id, to_iso(T0 + timedelta(days=3)), SECOND_GAP
        )
    assert repeated == original
    assert outreach_repo.get(conn, outreach_id)["sent_at"] == to_iso(T0)


def test_set_opened_keeps_first_timestamp(conn, contact_id, job_id):
    outreach_id = _create(conn, contact_id, job_id)
    with conn:
        outreach_repo.set_opened(conn, outreach_id, to_iso(T0))
        outreach_repo.set_opened(conn, outreach_id, to_iso(T0 + timedelta(hours=1)))
    assert outreach_repo.get(conn, outreach_id)["opened_at"] == to_iso(T0)
    assert len(outreach_repo.list_all(conn)) == 1


@pytest.mark.parametrize("response", ["Not referred", "Referred you", "I'll ask tomorrow"])
def test_reply_text_keeps_referral_outcome_pending(conn, contact_id, job_id, response):
    outreach_id = _create(conn, contact_id, job_id)
    with conn:
        outreach_repo.mark_sent(conn, outreach_id, to_iso(T0), FIRST_GAP)
        outreach_repo.mark_replied(conn, outreach_id, to_iso(T0 + timedelta(days=1)), response)
    assert outreach_repo.get(conn, outreach_id)["referral_outcome"] == "pending"


@pytest.mark.parametrize("outcome", ["declined", "completed"])
def test_explicit_referral_outcome_survives_later_ordinary_reply(conn, contact_id, job_id, outcome):
    outreach_id = _create(conn, contact_id, job_id)
    with conn:
        outreach_repo.mark_replied(
            conn, outreach_id, to_iso(T0), "Use this link", referral_outcome=outcome
        )
        outreach_repo.mark_replied(conn, outreach_id, to_iso(T0 + timedelta(days=1)), "Thanks")
    row = outreach_repo.get(conn, outreach_id)
    assert row["referral_outcome"] == outcome
    assert row["response"] == "Thanks"


@pytest.mark.parametrize("outcome", [None, "declined", "completed"])
def test_followup_reply_updates_only_its_associated_referral(conn, contact_id, job_id, outcome):
    earlier = _create(conn, contact_id, job_id)
    referral = _create(conn, contact_id, job_id)
    with conn:
        outreach_repo.mark_sent(conn, referral, to_iso(T0), FIRST_GAP)
    followup = _create(conn, contact_id, job_id, kind="followup")
    later = _create(conn, contact_id, job_id)
    with conn:
        outreach_repo.mark_sent(conn, followup, to_iso(T0 + timedelta(days=7)), SECOND_GAP)
        outreach_repo.mark_replied(
            conn, followup, to_iso(T0 + timedelta(days=8)), "The link", referral_outcome=outcome
        )
    for outreach_id in (referral, followup):
        row = outreach_repo.get(conn, outreach_id)
        assert row["referral_outcome"] == (outcome or "pending")
        assert row["replied_at"] == to_iso(T0 + timedelta(days=8))
        assert row["response"] == "The link"
        assert row["followup_due"] is None
    for outreach_id in (earlier, later):
        row = outreach_repo.get(conn, outreach_id)
        assert row["referral_outcome"] == "pending"
        assert row["replied_at"] is None


def test_invalid_referral_outcome_leaves_reply_unchanged(conn, contact_id, job_id):
    outreach_id = _create(conn, contact_id, job_id)
    with pytest.raises(ValueError, match="unknown referral outcome"):
        outreach_repo.mark_replied(
            conn, outreach_id, to_iso(T0), "link", referral_outcome="referred"
        )
    assert outreach_repo.get(conn, outreach_id)["replied_at"] is None


@pytest.mark.parametrize("kind", ["connection_note", "followup"])
@pytest.mark.parametrize("outcome", ["completed", "declined"])
def test_explicit_referral_outcome_on_standalone_messages(conn, contact_id, job_id, kind, outcome):
    outreach_id = _create(conn, contact_id, job_id, kind=kind)
    with conn:
        outreach_repo.mark_replied(
            conn, outreach_id, to_iso(T0), "update", referral_outcome=outcome
        )
    row = outreach_repo.get(conn, outreach_id)
    assert row["referral_outcome"] == outcome
    assert row["replied_at"] == to_iso(T0)
