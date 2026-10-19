"""End-to-end ingest over the fixture inbox: auto-apply, review queue, duplicates, resolve,
last-sync and lookback clamping."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from mail_support import COMPANIES, EMAILS_DIR, RULES, load_email, seed, stage

from jobhunt.clock import to_iso
from jobhunt.mail import ingest
from jobhunt.mail.models import (
    KIND_ASSESSMENT,
    KIND_CONFIRMATION,
    KIND_INTERVIEW,
    KIND_RECRUITER,
    KIND_REJECTION,
    KIND_UNCLASSIFIED,
    Classification,
    MatchResult,
)
from jobhunt.models import EventType, Status
from jobhunt.repo import applications as applications_repo
from jobhunt.repo import events as events_repo
from jobhunt.repo.jobs import current_status

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
ALL_FIXTURES = sorted(p.name for p in EMAILS_DIR.glob("*.json"))


@pytest.fixture
def inbox(tmp_path: Path) -> Path:
    target = tmp_path / "mail_inbox"
    target.mkdir()
    for name in ALL_FIXTURES:
        shutil.copy(EMAILS_DIR / name, target / name)
    return target


def _seed_all(conn, norm, priority, make_posting) -> dict[str, int]:
    return {
        "postman": seed(
            conn,
            norm,
            priority,
            make_posting,
            company="Postman",
            title="Software Engineer II",
            status=Status.APPROVED,
        ),
        "spotify": seed(
            conn,
            norm,
            priority,
            make_posting,
            company="Spotify",
            title="Backend Engineer, Platform",
            status=Status.APPROVED,
            sid="2",
        ),
        "linear": seed(
            conn,
            norm,
            priority,
            make_posting,
            company="Linear",
            title="Software Engineer, Backend",
            sid="3",
        ),
        "salesforce": seed(
            conn,
            norm,
            priority,
            make_posting,
            company="Salesforce",
            title="Software Engineer",
            status=Status.APPROVED,
            sid="4",
        ),
        "stripe": seed(
            conn, norm, priority, make_posting, company="Stripe", title="Backend Engineer", sid="5"
        ),
        "figma": seed(
            conn, norm, priority, make_posting, company="Figma", title="Software Engineer", sid="6"
        ),
        "coinbase": seed(
            conn,
            norm,
            priority,
            make_posting,
            company="Coinbase",
            title="Software Engineer, Backend",
            sid="7",
        ),
    }


def _run(conn, inbox, norm):
    return ingest.ingest_mail(conn, inbox, RULES, COMPANIES, norm, now=NOW)


def test_full_inbox_round_trip(conn, norm, priority, make_posting, inbox):
    jobs = _seed_all(conn, norm, priority, make_posting)
    summary = _run(conn, inbox, norm)
    assert summary.processed == len(ALL_FIXTURES)
    assert summary.failed == []
    assert summary.unclassified == 1  # the LinkedIn newsletter
    # One Postman confirmation and one Postman rejection share a job: rejection wins later.
    assert current_status(conn, jobs["postman"]) == Status.REJECTED
    assert current_status(conn, jobs["spotify"]) == Status.APPLIED
    assert current_status(conn, jobs["salesforce"]) == Status.APPLIED
    assert current_status(conn, jobs["stripe"]) == Status.REJECTED
    assert current_status(conn, jobs["figma"]) == Status.INTERVIEW
    assert current_status(conn, jobs["coinbase"]) == Status.SCREENING
    assert current_status(conn, jobs["linear"]) == Status.OFFER
    # Recruiter mail about Ramp has no tracked job but still creates the contact.
    assert conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 1
    assert summary.auto_applied == len(ALL_FIXTURES) - 1
    assert summary.needs_review == 0
    assert not list(inbox.glob("*.json"))
    assert len(list((inbox / ingest.PROCESSED_DIR).glob("*.json"))) == len(ALL_FIXTURES)
    assert ingest.last_sync(conn) is None


def test_second_run_is_all_duplicates(conn, norm, priority, make_posting, inbox):
    _seed_all(conn, norm, priority, make_posting)
    _run(conn, inbox, norm)
    processed = inbox / ingest.PROCESSED_DIR
    for path in processed.glob("*.json"):
        shutil.copy(path, inbox / path.name)
    events_before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    summary = _run(conn, inbox, norm)
    assert summary.duplicates == len(ALL_FIXTURES)
    assert summary.processed == 0
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == events_before


def test_ambiguous_match_goes_to_review_then_resolve(conn, norm, priority, make_posting, inbox):
    # Neither title appears in the fixture body, so the matcher cannot pick one.
    a = seed(
        conn,
        norm,
        priority,
        make_posting,
        company="Postman",
        title="Backend Engineer",
        status=Status.APPROVED,
    )
    b = seed(
        conn,
        norm,
        priority,
        make_posting,
        company="Postman",
        title="Platform Engineer",
        status=Status.APPROVED,
        sid="2",
    )
    for path in inbox.glob("*.json"):
        if path.name != "confirmation_greenhouse.json":
            path.unlink()
    summary = _run(conn, inbox, norm)
    assert summary.needs_review == 1
    assert summary.auto_applied == 0
    items = ingest.load_needs_review(inbox)
    assert len(items) == 1
    assert {c["job_id"] for c in items[0]["candidates"]} == {a, b}
    assert items[0]["kind"] == KIND_CONFIRMATION
    outcome = ingest.resolve_review(
        conn, inbox, "t_conf_gh", job_id=a, kind=KIND_CONFIRMATION, rules=RULES, now=NOW
    )
    assert outcome is not None and outcome.to_status == "applied"
    assert current_status(conn, a) == Status.APPLIED
    assert current_status(conn, b) == Status.APPROVED
    assert ingest.load_needs_review(inbox) == []
    with pytest.raises(KeyError):
        ingest.resolve_review(
            conn, inbox, "t_conf_gh", job_id=a, kind=KIND_CONFIRMATION, rules=RULES, now=NOW
        )


def test_resolve_ignore_drops_item_without_touching_pipeline(
    conn, norm, priority, make_posting, inbox
):
    job = seed(
        conn,
        norm,
        priority,
        make_posting,
        company="Postman",
        title="Backend Engineer",
        status=Status.APPROVED,
    )
    seed(
        conn,
        norm,
        priority,
        make_posting,
        company="Postman",
        title="Platform Engineer",
        status=Status.APPROVED,
        sid="2",
    )
    for path in inbox.glob("*.json"):
        if path.name != "rejection_unfortunately.json":
            path.unlink()
    _run(conn, inbox, norm)
    dropped = ingest.resolve_review(
        conn, inbox, "t_rej_gh", job_id=None, kind="ignore", rules=RULES
    )
    assert dropped is None
    assert ingest.load_needs_review(inbox) == []
    assert current_status(conn, job) == Status.APPROVED


def test_single_candidate_review_records_needs_review_event(
    conn, norm, priority, make_posting, inbox
):
    job = seed(conn, norm, priority, make_posting, company="Stripe", title="Backend Engineer")
    for path in inbox.glob("*.json"):
        path.unlink()
    weak = {
        "schema_version": 1,
        "thread_id": "t_weak",
        "gmail_id": "m_weak",
        "from": "Stripe Recruiting <recruiting@stripe.com>",
        "date": "2026-09-07T10:00:00Z",
        "subject": "Quick update",
        "body_text": "Unfortunately we are still reviewing applications and will be in touch.",
    }
    (inbox / "weak.json").write_text(json.dumps(weak), encoding="utf-8")
    summary = _run(conn, inbox, norm)
    assert summary.needs_review == 1
    types = [row["event_type"] for row in events_repo.events_for_job(conn, job)]
    assert EventType.MAIL_NEEDS_REVIEW in types
    assert current_status(conn, job) == Status.APPLIED


def test_high_confidence_without_any_job_goes_to_review(conn, norm, priority, make_posting, inbox):
    seed(conn, norm, priority, make_posting, company="Figma", title="Software Engineer")
    for path in inbox.glob("*.json"):
        if path.name != "rejection_other_candidates.json":
            path.unlink()
    summary = _run(conn, inbox, norm)
    assert summary.needs_review == 1
    item = ingest.load_needs_review(inbox)[0]
    assert item["kind"] == KIND_REJECTION and item["candidates"] == []


def test_malformed_file_moves_to_failed(conn, norm, inbox):
    for path in inbox.glob("*.json"):
        path.unlink()
    (inbox / "bad.json").write_text("{not json", encoding="utf-8")
    (inbox / "wrong.json").write_text(json.dumps({"schema_version": 9}), encoding="utf-8")
    summary = _run(conn, inbox, norm)
    assert len(summary.failed) == 2
    assert sorted(p.name for p in (inbox / ingest.FAILED_DIR).glob("*.json")) == [
        "bad.json",
        "wrong.json",
    ]


def test_only_completed_fetches_advance_sync_checkpoint(conn):
    ingest._record_ingest(conn, "partial", "confirmation", True, to_iso(NOW))
    ingest._record_ingest(conn, "old:resolved", "rejection", True, to_iso(NOW))
    assert ingest.last_sync(conn) is None
    through = NOW - timedelta(days=2)
    ingest.complete_sync(conn, through, now=NOW)
    assert ingest.last_sync(conn) == through
    ingest.complete_sync(conn, through - timedelta(days=1), now=NOW)
    assert ingest.last_sync(conn) == through
    with pytest.raises(ValueError):
        ingest.complete_sync(conn, NOW + timedelta(days=1), now=NOW)


def test_failed_review_resolution_preserves_queue(conn, tmp_path):
    from mail_support import load_email

    from jobhunt.repo.jobs import JobNotFound

    message = load_email("confirmation_greenhouse")
    item = {
        "gmail_id": message.gmail_id,
        "thread_id": message.thread_id,
        "message": message.to_dict(),
    }
    ingest.save_needs_review(tmp_path, [item])
    with pytest.raises(JobNotFound):
        ingest.resolve_review(
            conn,
            tmp_path,
            message.gmail_id,
            job_id=999,
            kind=KIND_CONFIRMATION,
            rules=RULES,
            now=NOW,
        )
    assert ingest.load_needs_review(tmp_path) == [item]


def test_older_rejection_cannot_silently_close_newer_interview(
    conn, norm, priority, make_posting, inbox
):
    job = seed(conn, norm, priority, make_posting, company="Postman", title="Software Engineer II")
    from jobhunt.repo.jobs import set_job_status

    set_job_status(conn, job, Status.INTERVIEW, at="2026-09-09T12:00:00+00:00")
    _empty(inbox)
    stage(inbox, "rejection_unfortunately")
    result = _run(conn, inbox, norm)
    assert result.needs_review == 1
    assert current_status(conn, job) == Status.INTERVIEW


def test_lookback_days_clamps(conn):
    assert ingest.last_sync(conn) is None
    assert ingest.lookback_days(conn, RULES, now=NOW) == RULES["default_lookback_days"]
    ingest.complete_sync(conn, NOW - timedelta(days=90), now=NOW)
    assert ingest.lookback_days(conn, RULES, now=NOW) == RULES["max_lookback_days"]
    ingest.complete_sync(conn, NOW - timedelta(days=2), now=NOW)
    assert ingest.lookback_days(conn, RULES, now=NOW) == 3
    ingest.complete_sync(conn, NOW - timedelta(days=90), now=NOW)
    assert ingest.lookback_days(conn, RULES, now=NOW) == 3  # newest sync wins
    ingest.complete_sync(conn, NOW, now=NOW)
    assert ingest.lookback_days(conn, RULES, now=NOW) == 1


# --------------------------------------------------------------------------- message-level sync


def _empty(inbox: Path) -> None:
    for path in inbox.glob("*.json"):
        path.unlink()


def test_later_message_in_same_thread_applies_new_kind(conn, norm, priority, make_posting, inbox):
    """Confirmation then rejection in one thread: the second message must not be skipped."""
    job = seed(
        conn,
        norm,
        priority,
        make_posting,
        company="Postman",
        title="Software Engineer II",
        status=Status.APPROVED,
    )
    _empty(inbox)
    stage(inbox, "confirmation_greenhouse")
    first = _run(conn, inbox, norm)
    assert first.auto_applied == 1
    assert current_status(conn, job) == Status.APPLIED

    stage(inbox, "rejection_same_thread")
    second = _run(conn, inbox, norm)
    assert second.processed == 1
    assert second.duplicates == 0
    assert second.auto_applied == 1
    assert current_status(conn, job) == Status.REJECTED
    processed = sorted(p.name for p in (inbox / ingest.PROCESSED_DIR).glob("*.json"))
    assert processed == ["t_conf_gh_m_t_conf_gh.json", "t_conf_gh_m_t_conf_gh_2.json"]


def test_same_message_ingested_twice_is_a_duplicate(conn, norm, priority, make_posting, inbox):
    seed(
        conn,
        norm,
        priority,
        make_posting,
        company="Postman",
        title="Software Engineer II",
        status=Status.APPROVED,
    )
    _empty(inbox)
    stage(inbox, "confirmation_greenhouse")
    _run(conn, inbox, norm)
    (inbox / "again.json").write_text(
        (inbox / ingest.PROCESSED_DIR / "t_conf_gh_m_t_conf_gh.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    summary = _run(conn, inbox, norm)
    assert summary.duplicates == 1
    assert summary.processed == 0


def test_later_message_processed_when_earlier_one_sits_in_review(
    conn, norm, priority, make_posting, inbox
):
    a = seed(
        conn,
        norm,
        priority,
        make_posting,
        company="Postman",
        title="Backend Engineer",
        status=Status.APPROVED,
    )
    b = seed(
        conn,
        norm,
        priority,
        make_posting,
        company="Postman",
        title="Platform Engineer",
        status=Status.APPROVED,
        sid="2",
    )
    _empty(inbox)
    stage(inbox, "confirmation_greenhouse")
    assert _run(conn, inbox, norm).needs_review == 1

    stage(inbox, "rejection_same_thread")
    summary = _run(conn, inbox, norm)
    assert summary.needs_review == 1
    assert summary.duplicates == 0
    items = ingest.load_needs_review(inbox)
    assert [i["gmail_id"] for i in items] == ["m_t_conf_gh", "m_t_conf_gh_2"]
    assert all(i["thread_id"] == "t_conf_gh" for i in items)

    with pytest.raises(KeyError, match="2 queued messages"):
        ingest.resolve_review(
            conn, inbox, "t_conf_gh", job_id=a, kind=KIND_REJECTION, rules=RULES, now=NOW
        )
    outcome = ingest.resolve_review(
        conn, inbox, "m_t_conf_gh_2", job_id=a, kind=KIND_REJECTION, rules=RULES, now=NOW
    )
    assert outcome is not None and outcome.to_status == "rejected"
    assert current_status(conn, a) == Status.REJECTED
    assert current_status(conn, b) == Status.APPROVED
    remaining = ingest.load_needs_review(inbox)
    assert [i["gmail_id"] for i in remaining] == ["m_t_conf_gh"]
    # Now the thread id is unambiguous again and still works as a key.
    dropped = ingest.resolve_review(
        conn, inbox, "t_conf_gh", job_id=None, kind="ignore", rules=RULES, now=NOW
    )
    assert dropped is None
    assert ingest.load_needs_review(inbox) == []


def test_file_without_message_id_is_rejected(conn, norm, inbox):
    _empty(inbox)
    data = json.loads((EMAILS_DIR / "confirmation_greenhouse.json").read_text(encoding="utf-8"))
    del data["gmail_id"]
    (inbox / "t_conf_gh.json").write_text(json.dumps(data), encoding="utf-8")
    summary = _run(conn, inbox, norm)
    assert summary.processed == 0
    assert len(summary.failed) == 1 and "gmail_id" in summary.failed[0]
    assert [p.name for p in (inbox / ingest.FAILED_DIR).glob("*.json")] == ["t_conf_gh.json"]


@pytest.mark.parametrize(
    ("name", "kind", "status"),
    [
        ("interview_calendly", KIND_INTERVIEW, Status.INTERVIEW),
        ("assessment_hackerrank", KIND_ASSESSMENT, Status.SCREENING),
        ("recruiter_cold", KIND_RECRUITER, Status.APPLIED),
        ("interview_calendly", KIND_UNCLASSIFIED, Status.APPLIED),
    ],
)
def test_same_kind_messages_in_one_thread_are_ingested_individually(
    conn, norm, priority, make_posting, inbox, monkeypatch, name, kind, status
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
    _empty(inbox)
    first = replace(load_email(name), date="2026-09-05T10:00:00+00:00")
    later = replace(first, gmail_id=f"{first.gmail_id}_later", date="2026-09-07T10:00:00+00:00")
    monkeypatch.setattr(ingest, "classify", Mock(return_value=Classification(kind, 0.95)))
    monkeypatch.setattr(ingest, "match_application", Mock(return_value=MatchResult(job, 0.9)))
    for msg in (first, later):
        (inbox / msg.file_name).write_text(json.dumps(msg.to_dict()), encoding="utf-8")
        summary = _run(conn, inbox, norm)
        assert summary.processed == 1
        assert summary.duplicates == 0
        assert summary.failed == []
        assert summary.unclassified == int(kind == KIND_UNCLASSIFIED)
        assert summary.auto_applied == int(kind != KIND_UNCLASSIFIED)
    assert current_status(conn, job) == status
    if kind != KIND_UNCLASSIFIED:
        assert applications_repo.get_for_job(conn, job)["last_activity_at"] == later.date
    rows = events_repo.events_for_job(conn, job)
    assert {row["source_email_id"] for row in rows if row["source"] == "gmail"} == {
        first.gmail_id,
        later.gmail_id,
    }
    for msg in (first, later):
        (inbox / msg.file_name).write_text(json.dumps(msg.to_dict()), encoding="utf-8")
    replay = _run(conn, inbox, norm)
    assert replay.duplicates == 2
    assert replay.processed == 0
    assert events_repo.events_for_job(conn, job) == rows


def test_same_kind_review_messages_resolve_out_of_order(
    conn, norm, priority, make_posting, inbox, monkeypatch
):
    job = seed(
        conn,
        norm,
        priority,
        make_posting,
        company="Figma",
        title="Engineer",
        at="2026-09-01T10:00:00+00:00",
    )
    _empty(inbox)
    first = replace(load_email("interview_calendly"), date="2026-09-05T10:00:00+00:00")
    later = replace(first, gmail_id=f"{first.gmail_id}_round2", date="2026-09-07T10:00:00+00:00")
    monkeypatch.setattr(ingest, "classify", Mock(return_value=Classification(KIND_INTERVIEW, 0.5)))
    monkeypatch.setattr(ingest, "match_application", Mock(return_value=MatchResult(job, 0.9)))
    for msg in (first, later):
        (inbox / msg.file_name).write_text(json.dumps(msg.to_dict()), encoding="utf-8")
    assert _run(conn, inbox, norm).needs_review == 2
    for msg in (later, first):
        outcome = ingest.resolve_review(
            conn, inbox, msg.gmail_id, job_id=job, kind=KIND_INTERVIEW, rules=RULES, now=NOW
        )
        assert outcome is not None and not outcome.duplicate
    assert current_status(conn, job) == Status.INTERVIEW
    assert applications_repo.get_for_job(conn, job)["last_activity_at"] == later.date
    interviews = [
        row
        for row in events_repo.events_for_job(conn, job)
        if row["event_type"] == EventType.INTERVIEW_SCHEDULED
    ]
    assert {row["source_email_id"] for row in interviews} == {first.gmail_id, later.gmail_id}
    assert ingest.load_needs_review(inbox) == []


@pytest.mark.parametrize("kind", [KIND_INTERVIEW, KIND_UNCLASSIFIED])
@pytest.mark.parametrize("original", [True, False])
def test_ingest_respects_legacy_message_evidence_without_skipping_thread(
    conn, norm, priority, make_posting, inbox, monkeypatch, kind, original
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
    _empty(inbox)
    msg = load_email("interview_calendly")
    source_id = msg.gmail_id if original else f"{msg.gmail_id}_previous"
    event_type = (
        EventType.MAIL_UNCLASSIFIED if kind == KIND_UNCLASSIFIED else EventType.EMAIL_RECEIVED
    )
    with conn:
        events_repo.add_event(
            conn,
            job,
            event_type,
            source="gmail",
            source_email_id=source_id,
            external_id=f"{msg.thread_id}:{kind}",
        )
    monkeypatch.setattr(ingest, "classify", Mock(return_value=Classification(kind, 0.95)))
    monkeypatch.setattr(ingest, "match_application", Mock(return_value=MatchResult(job, 0.9)))
    before = events_repo.events_for_job(conn, job)
    (inbox / msg.file_name).write_text(json.dumps(msg.to_dict()), encoding="utf-8")
    summary = _run(conn, inbox, norm)
    assert summary.failed == []
    rows = events_repo.events_for_job(conn, job)
    if original:
        assert rows == before
    else:
        assert len(rows) > len(before)
        assert any(row["source_email_id"] == msg.gmail_id for row in rows)
    assert current_status(conn, job) == Status.INTERVIEW
