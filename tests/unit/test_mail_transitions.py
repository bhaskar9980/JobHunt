"""Transitions: kind -> status, never regress, idempotent, recruiter inbound rows."""

from __future__ import annotations

from dataclasses import replace

import pytest
from mail_support import load_email, seed

from jobhunt.mail.models import (
    KIND_ASSESSMENT,
    KIND_CONFIRMATION,
    KIND_INTERVIEW,
    KIND_OFFER,
    KIND_RECRUITER,
    KIND_REJECTION,
    Classification,
    MatchResult,
)
from jobhunt.mail.transitions import apply_outcome, external_id_for
from jobhunt.models import EventType, Status
from jobhunt.repo import applications as applications_repo
from jobhunt.repo import events as events_repo
from jobhunt.repo.jobs import current_status


def _apply(conn, name, kind, job_id):
    msg = load_email(name)
    return apply_outcome(conn, msg, Classification(kind, 0.95), MatchResult(job_id, 0.9))


def _event_types(conn, job_id):
    return [row["event_type"] for row in events_repo.events_for_job(conn, job_id)]


def test_confirmation_moves_approved_to_applied(conn, norm, priority, make_posting):
    job = seed(
        conn,
        norm,
        priority,
        make_posting,
        company="Postman",
        title="Software Engineer II",
        status=Status.APPROVED,
    )
    outcome = _apply(conn, "confirmation_greenhouse", KIND_CONFIRMATION, job)
    assert (outcome.from_status, outcome.to_status) == ("approved", "applied")
    assert current_status(conn, job) == Status.APPLIED
    app = applications_repo.get_for_job(conn, job)
    assert app["confirmation_email_id"] == "m_t_conf_gh"
    assert app["applied_at"] == "2026-09-05T10:00:00+00:00"
    assert events_repo.has_external_id(conn, external_id_for("m_t_conf_gh", KIND_CONFIRMATION))


def test_confirmation_when_already_applied_only_records_event(conn, norm, priority, make_posting):
    job = seed(conn, norm, priority, make_posting, company="Postman", title="Software Engineer II")
    outcome = _apply(conn, "confirmation_greenhouse", KIND_CONFIRMATION, job)
    assert not outcome.changed
    assert current_status(conn, job) == Status.APPLIED
    assert EventType.EMAIL_RECEIVED in _event_types(conn, job)
    assert applications_repo.get_for_job(conn, job)["confirmation_email_id"] == "m_t_conf_gh"


def test_assessment_moves_to_screening(conn, norm, priority, make_posting):
    job = seed(conn, norm, priority, make_posting, company="Coinbase", title="Software Engineer")
    outcome = _apply(conn, "assessment_hackerrank", KIND_ASSESSMENT, job)
    assert outcome.to_status == "screening"
    assert current_status(conn, job) == Status.SCREENING


def test_interview_moves_and_records_interview_event(conn, norm, priority, make_posting):
    job = seed(conn, norm, priority, make_posting, company="Figma", title="Software Engineer")
    _apply(conn, "interview_calendly", KIND_INTERVIEW, job)
    assert current_status(conn, job) == Status.INTERVIEW
    assert EventType.INTERVIEW_SCHEDULED in _event_types(conn, job)


def test_offer_and_rejection(conn, norm, priority, make_posting):
    linear = seed(conn, norm, priority, make_posting, company="Linear", title="Software Engineer")
    stripe = seed(conn, norm, priority, make_posting, company="Stripe", title="Backend Engineer")
    _apply(conn, "offer", KIND_OFFER, linear)
    _apply(conn, "rejection_other_candidates", KIND_REJECTION, stripe)
    assert current_status(conn, linear) == Status.OFFER
    assert current_status(conn, stripe) == Status.REJECTED


def test_approved_job_with_interview_mail_steps_through_applied(conn, norm, priority, make_posting):
    job = seed(
        conn,
        norm,
        priority,
        make_posting,
        company="Figma",
        title="Software Engineer",
        status=Status.APPROVED,
    )
    _apply(conn, "interview_calendly", KIND_INTERVIEW, job)
    assert current_status(conn, job) == Status.INTERVIEW
    changes = [
        (row["from_status"], row["to_status"])
        for row in events_repo.events_for_job(conn, job)
        if row["event_type"] == EventType.STATUS_CHANGE and row["source"] == "gmail"
    ]
    assert changes == [("approved", "applied"), ("applied", "interview")]


def test_never_regress_interview_then_late_confirmation(conn, norm, priority, make_posting):
    job = seed(conn, norm, priority, make_posting, company="Postman", title="Software Engineer II")
    _apply(conn, "interview_calendly", KIND_INTERVIEW, job)
    outcome = _apply(conn, "confirmation_greenhouse", KIND_CONFIRMATION, job)
    assert not outcome.changed
    assert current_status(conn, job) == Status.INTERVIEW
    assert "kept interview" in outcome.note


def test_terminal_status_only_gets_an_event(conn, norm, priority, make_posting):
    job = seed(
        conn,
        norm,
        priority,
        make_posting,
        company="Figma",
        title="Software Engineer",
        status=Status.REJECTED,
    )
    _apply(conn, "interview_calendly", KIND_INTERVIEW, job)
    assert current_status(conn, job) == Status.REJECTED
    assert EventType.EMAIL_RECEIVED in _event_types(conn, job)


def test_same_thread_twice_is_a_duplicate(conn, norm, priority, make_posting):
    job = seed(
        conn,
        norm,
        priority,
        make_posting,
        company="Postman",
        title="Software Engineer II",
        status=Status.APPROVED,
    )
    first = _apply(conn, "confirmation_greenhouse", KIND_CONFIRMATION, job)
    before = len(events_repo.events_for_job(conn, job))
    second = _apply(conn, "confirmation_greenhouse", KIND_CONFIRMATION, job)
    assert first.changed and second.duplicate
    assert len(events_repo.events_for_job(conn, job)) == before


def test_recruiter_outreach_creates_contact_and_inbound_row(conn):
    msg = load_email("recruiter_cold")
    outcome = apply_outcome(conn, msg, Classification(KIND_RECRUITER, 0.9), MatchResult(None, 0.0))
    assert not outcome.duplicate
    contact = conn.execute(
        "SELECT * FROM contacts WHERE email = ?", ("priya@talentco.io",)
    ).fetchone()
    assert contact["name"] == "Priya Nair"
    assert contact["company"] == "Talentco"
    assert contact["relationship"] == "recruiter"
    outreach = conn.execute(
        "SELECT * FROM outreach WHERE contact_id = ?", (contact["id"],)
    ).fetchall()
    assert len(outreach) == 1
    assert (outreach[0]["kind"], outreach[0]["channel"]) == ("inbound", "email")
    assert outreach[0]["hook"] == msg.subject
    again = apply_outcome(conn, msg, Classification(KIND_RECRUITER, 0.9), MatchResult(None, 0.0))
    assert again.duplicate
    assert conn.execute("SELECT COUNT(*) FROM outreach").fetchone()[0] == 1


def test_recruiter_outreach_with_job_records_event(conn, norm, priority, make_posting):
    job = seed(conn, norm, priority, make_posting, company="Ramp", title="Backend Engineer")
    _apply(conn, "recruiter_cold", KIND_RECRUITER, job)
    assert current_status(conn, job) == Status.APPLIED
    assert EventType.EMAIL_RECEIVED in _event_types(conn, job)
    assert events_repo.has_external_id(conn, external_id_for("m_t_recruiter", KIND_RECRUITER))


@pytest.mark.parametrize(
    ("name", "kind", "status"),
    [
        ("interview_calendly", KIND_INTERVIEW, Status.INTERVIEW),
        ("assessment_hackerrank", KIND_ASSESSMENT, Status.SCREENING),
        ("recruiter_cold", KIND_RECRUITER, Status.APPLIED),
    ],
)
def test_later_same_kind_message_records_evidence_and_activity(
    conn, norm, priority, make_posting, name, kind, status
):
    job = seed(
        conn,
        norm,
        priority,
        make_posting,
        company="Acme",
        title="Engineer",
        at="2026-09-01T10:00:00+00:00",
    )
    first = replace(load_email(name), date="2026-09-05T10:00:00+00:00")
    later = replace(first, gmail_id=f"{first.gmail_id}_round2", date="2026-09-07T10:00:00+00:00")
    classification, match = Classification(kind, 0.95), MatchResult(job, 0.9)
    apply_outcome(conn, first, classification, match)
    outcome = apply_outcome(conn, later, classification, match)
    assert not outcome.duplicate
    assert not outcome.changed
    assert current_status(conn, job) == status
    assert applications_repo.get_for_job(conn, job)["last_activity_at"] == later.date
    rows = events_repo.events_for_job(conn, job)
    assert {row["source_email_id"] for row in rows if row["source"] == "gmail"} == {
        first.gmail_id,
        later.gmail_id,
    }
    if kind == KIND_INTERVIEW:
        interviews = [row for row in rows if row["event_type"] == EventType.INTERVIEW_SCHEDULED]
        assert [row["source_email_id"] for row in interviews] == [first.gmail_id, later.gmail_id]
    if kind == KIND_RECRUITER:
        assert conn.execute("SELECT COUNT(*) FROM outreach").fetchone()[0] == 2
    replay = apply_outcome(conn, later, classification, match)
    assert replay.duplicate
    assert events_repo.events_for_job(conn, job) == rows
    assert applications_repo.get_for_job(conn, job)["last_activity_at"] == later.date


@pytest.mark.parametrize("legacy_source", ["original", "different", None])
@pytest.mark.parametrize("legacy_type", [EventType.EMAIL_RECEIVED, EventType.STATUS_CHANGE])
def test_legacy_thread_key_only_dedupes_original_message(
    conn, norm, priority, make_posting, legacy_source, legacy_type
):
    job = seed(
        conn,
        norm,
        priority,
        make_posting,
        company="Figma",
        title="Engineer",
        status=Status.INTERVIEW,
        at="2026-09-01T10:00:00+00:00",
    )
    msg = load_email("interview_calendly")
    msg = replace(msg, gmail_id=msg.thread_id)
    source_id = msg.gmail_id if legacy_source == "original" else legacy_source
    with conn:
        legacy_id = events_repo.add_event(
            conn,
            job,
            legacy_type,
            source="gmail",
            source_email_id=source_id,
            external_id=f"{msg.thread_id}:{KIND_INTERVIEW}",
            at="2026-09-01T10:00:00+00:00",
        )
    before = events_repo.events_for_job(conn, job)
    classification, match = Classification(KIND_INTERVIEW, 0.95), MatchResult(job, 0.9)
    outcome = apply_outcome(conn, msg, classification, match)
    assert outcome.duplicate is (legacy_source == "original")
    assert current_status(conn, job) == Status.INTERVIEW
    if legacy_source == "original":
        assert events_repo.events_for_job(conn, job) == before
    else:
        rows = events_repo.events_for_job(conn, job)
        assert any(row["source_email_id"] == msg.gmail_id for row in rows)
        legacy = next(row for row in rows if row["id"] == legacy_id)
        assert legacy["external_id"] == f"{msg.thread_id}:{KIND_INTERVIEW}"
        assert legacy["source_email_id"] == source_id
        assert apply_outcome(conn, msg, classification, match).duplicate
        assert events_repo.events_for_job(conn, job) == rows


def test_legacy_interview_evidence_dedupes_original_without_status_source(
    conn, norm, priority, make_posting
):
    job = seed(
        conn,
        norm,
        priority,
        make_posting,
        company="Figma",
        title="Engineer",
        status=Status.INTERVIEW,
    )
    msg = load_email("interview_calendly")
    with conn:
        events_repo.add_event(
            conn,
            job,
            EventType.STATUS_CHANGE,
            source="gmail",
            external_id=f"{msg.thread_id}:{KIND_INTERVIEW}",
        )
        events_repo.add_event(
            conn,
            job,
            EventType.INTERVIEW_SCHEDULED,
            source="gmail",
            source_email_id=msg.gmail_id,
        )
    before = events_repo.events_for_job(conn, job)
    outcome = apply_outcome(conn, msg, Classification(KIND_INTERVIEW, 0.95), MatchResult(job, 0.9))
    assert outcome.duplicate
    assert events_repo.events_for_job(conn, job) == before


@pytest.mark.parametrize(
    ("name", "kind", "initial", "expected"),
    [
        ("interview_calendly", KIND_INTERVIEW, Status.APPLIED, Status.INTERVIEW),
        ("interview_calendly", KIND_INTERVIEW, Status.INTERVIEW, Status.INTERVIEW),
        ("assessment_hackerrank", KIND_ASSESSMENT, Status.SCREENING, Status.SCREENING),
        ("confirmation_greenhouse", KIND_CONFIRMATION, Status.INTERVIEW, Status.INTERVIEW),
        ("recruiter_cold", KIND_RECRUITER, Status.INTERVIEW, Status.INTERVIEW),
    ],
)
def test_old_mail_keeps_latest_application_activity(
    conn, norm, priority, make_posting, name, kind, initial, expected
):
    latest = "2026-09-08T10:00:00+00:00"
    job = seed(
        conn,
        norm,
        priority,
        make_posting,
        company="Acme",
        title="Engineer",
        status=initial,
        at=latest,
    )
    msg = replace(load_email(name), date="2026-09-05T10:00:00+00:00")
    outcome = apply_outcome(conn, msg, Classification(kind, 0.95), MatchResult(job, 0.9))
    assert not outcome.duplicate
    assert current_status(conn, job) == expected
    assert applications_repo.get_for_job(conn, job)["last_activity_at"] == latest
    assert any(
        row["at"] == msg.date and row["source_email_id"] == msg.gmail_id
        for row in events_repo.events_for_job(conn, job)
    )


def test_unmatched_recruiter_messages_with_same_subject_are_distinct(conn):
    first = replace(load_email("recruiter_cold"), date="2026-09-05T10:00:00+00:00")
    later = replace(first, gmail_id=f"{first.gmail_id}_later", date="2026-09-07T10:00:00+00:00")
    classification, match = Classification(KIND_RECRUITER, 0.95), MatchResult(None, 0.0)
    assert not apply_outcome(conn, first, classification, match).duplicate
    assert not apply_outcome(conn, later, classification, match).duplicate
    assert conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM outreach").fetchone()[0] == 2
    assert apply_outcome(conn, first, classification, match).duplicate
    assert apply_outcome(conn, later, classification, match).duplicate
    assert conn.execute("SELECT COUNT(*) FROM outreach").fetchone()[0] == 2
    contact = conn.execute("SELECT * FROM contacts").fetchone()
    assert contact["last_contacted_at"] == later.date


def test_old_recruiter_message_preserves_latest_contact_time(conn):
    later = replace(load_email("recruiter_cold"), date="2026-09-07T10:00:00+00:00")
    older = replace(later, gmail_id=f"{later.gmail_id}_older", date="2026-09-05T10:00:00+00:00")
    classification, match = Classification(KIND_RECRUITER, 0.95), MatchResult(None, 0.0)
    apply_outcome(conn, later, classification, match)
    apply_outcome(conn, older, classification, match)
    assert conn.execute("SELECT last_contacted_at FROM contacts").fetchone()[0] == later.date
    assert conn.execute("SELECT COUNT(*) FROM outreach").fetchone()[0] == 2
