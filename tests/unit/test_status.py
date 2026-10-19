import pytest

from jobhunt.models import Status
from jobhunt.repo import applications, events
from jobhunt.repo.jobs import (
    InvalidTransition,
    JobNotFound,
    add_note,
    current_status,
    set_job_status,
    upsert_job,
)


@pytest.fixture
def job_id(conn, norm, priority, make_posting):
    return upsert_job(conn, make_posting(), norm, priority).job_id


def test_new_job_starts_discovered(conn, job_id):
    assert current_status(conn, job_id) == Status.DISCOVERED


def test_valid_transition_writes_event(conn, job_id):
    set_job_status(conn, job_id, Status.SCORED, "batch 1")
    assert current_status(conn, job_id) == Status.SCORED
    log = events.events_for_job(conn, job_id)
    assert len(log) == 1
    assert log[0]["event_type"] == "status_change"
    assert log[0]["from_status"] == "discovered"
    assert log[0]["to_status"] == "scored"
    assert log[0]["note"] == "batch 1"


def test_invalid_transition_raises_and_changes_nothing(conn, job_id):
    with pytest.raises(InvalidTransition):
        set_job_status(conn, job_id, Status.APPLIED)
    assert current_status(conn, job_id) == Status.DISCOVERED
    assert events.events_for_job(conn, job_id) == []


def test_force_allows_any_transition(conn, job_id):
    set_job_status(conn, job_id, Status.INTERVIEW, force=True)
    assert current_status(conn, job_id) == Status.INTERVIEW


def test_same_status_is_a_noop(conn, job_id):
    set_job_status(conn, job_id, Status.DISCOVERED)
    assert events.events_for_job(conn, job_id) == []


def test_applied_creates_application_row(conn, job_id):
    for status in (Status.SCORED, Status.SHORTLISTED, Status.APPROVED, Status.APPLIED):
        set_job_status(conn, job_id, status)
    app = applications.get_for_job(conn, job_id)
    assert app is not None
    assert app["status"] == "applied"
    assert app["applied_at"] is not None
    row = conn.execute(
        "SELECT pipeline_status, status FROM v_pipeline WHERE id = ?", (job_id,)
    ).fetchone()
    assert row["pipeline_status"] == "applied"
    assert row["status"] == "applied"


def test_post_apply_transitions_update_application_not_duplicate(conn, job_id):
    set_job_status(conn, job_id, Status.APPLIED, force=True)
    set_job_status(conn, job_id, Status.SCREENING)
    set_job_status(conn, job_id, Status.INTERVIEW)
    assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 1
    assert applications.get_for_job(conn, job_id)["status"] == "interview"
    assert current_status(conn, job_id) == Status.INTERVIEW


def test_rejected_is_terminal(conn, job_id):
    set_job_status(conn, job_id, Status.APPLIED, force=True)
    set_job_status(conn, job_id, Status.REJECTED)
    with pytest.raises(InvalidTransition):
        set_job_status(conn, job_id, Status.INTERVIEW)


def test_dismiss_and_undismiss(conn, job_id):
    set_job_status(conn, job_id, Status.DISMISSED)
    set_job_status(conn, job_id, Status.SHORTLISTED)
    assert current_status(conn, job_id) == Status.SHORTLISTED


def test_external_id_is_unique(conn, job_id):
    import sqlite3

    set_job_status(conn, job_id, Status.APPLIED, force=True, external_id="thread1:confirmation")
    with pytest.raises(sqlite3.IntegrityError):
        set_job_status(conn, job_id, Status.SCREENING, external_id="thread1:confirmation")
    assert events.has_external_id(conn, "thread1:confirmation")


def test_note_and_missing_job(conn, job_id):
    add_note(conn, job_id, "spoke to recruiter")
    assert events.events_for_job(conn, job_id)[0]["event_type"] == "note"
    with pytest.raises(JobNotFound):
        add_note(conn, 9999, "nope")


def test_outer_transaction_can_roll_back_status_and_event(conn, job_id):
    conn.execute("BEGIN IMMEDIATE")
    set_job_status(conn, job_id, Status.SCORED)
    conn.rollback()
    assert current_status(conn, job_id) == Status.DISCOVERED
    assert events.events_for_job(conn, job_id) == []


def test_failed_approval_does_not_clear_snooze(conn, job_id):
    from jobhunt import review
    from jobhunt.repo.jobs import get_job, set_snooze

    set_snooze(conn, job_id, "2030-01-01T00:00:00+00:00")
    with pytest.raises(InvalidTransition):
        review.decide(conn, review.Decision(job_id, "approve"))
    assert get_job(conn, job_id)["snooze_until"] == "2030-01-01T00:00:00+00:00"


def test_force_cannot_split_job_and_application_status(conn, job_id):
    set_job_status(conn, job_id, Status.APPLIED, force=True)
    with pytest.raises(InvalidTransition):
        set_job_status(conn, job_id, Status.DISMISSED, force=True)
    assert current_status(conn, job_id) == Status.APPLIED


def test_mark_applied_does_not_regress_interview_or_duplicate_event(conn, job_id, monkeypatch):
    from types import SimpleNamespace

    from typer.testing import CliRunner

    from jobhunt import cli

    set_job_status(conn, job_id, Status.INTERVIEW, force=True)
    monkeypatch.setattr(cli, "_ctx", lambda: SimpleNamespace(conn=conn))
    for _ in range(2):
        result = CliRunner().invoke(cli.app, ["mark-applied", str(job_id), "--ref", "confirmed"])
        assert result.exit_code == 0, result.output
    assert current_status(conn, job_id) == Status.INTERVIEW
    assert (
        len([e for e in events.events_for_job(conn, job_id) if e["event_type"] == "submitted"]) == 1
    )


def test_submission_evidence_is_idempotent(conn, job_id):
    set_job_status(conn, job_id, Status.APPLIED, force=True, at="2026-09-01T00:00:00+00:00")
    with conn:
        applications.record_submission(
            conn,
            job_id,
            submitted_at="2026-09-01T00:00:00+00:00",
            submitted_via="ats_form",
            confirmation_ref="first",
        )
        applications.record_submission(
            conn,
            job_id,
            submitted_at="2026-09-03T00:00:00+00:00",
            submitted_via="manual",
            confirmation_ref="second",
        )
    row = applications.get_for_job(conn, job_id)
    assert row["submitted_at"] == "2026-09-01T00:00:00+00:00"
    assert row["submitted_via"] == "ats_form"
    assert row["confirmation_ref"] == "first"
