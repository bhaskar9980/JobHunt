"""Ghost check: stale active applications become ghosted once; fresh ones are untouched."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mail_support import seed

from jobhunt.clock import to_iso
from jobhunt.mail.ghosting import ghost_check, stale_applications
from jobhunt.models import EventType, Status
from jobhunt.repo import applications as applications_repo
from jobhunt.repo import events as events_repo
from jobhunt.repo.jobs import current_status

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
GHOST_DAYS = 21


def _days_ago(days: int) -> str:
    return to_iso(NOW - timedelta(days=days))


def test_only_stale_applications_move(conn, norm, priority, make_posting):
    stale = seed(
        conn,
        norm,
        priority,
        make_posting,
        company="Postman",
        title="Software Engineer II",
        at=_days_ago(30),
    )
    fresh = seed(
        conn,
        norm,
        priority,
        make_posting,
        company="Stripe",
        title="Backend Engineer",
        at=_days_ago(5),
    )
    assert [row["job_id"] for row in stale_applications(conn, GHOST_DAYS, now=NOW)] == [stale]
    moved = ghost_check(conn, GHOST_DAYS, now=NOW)
    assert moved == [stale]
    assert current_status(conn, stale) == Status.GHOSTED
    assert current_status(conn, fresh) == Status.APPLIED
    types = [row["event_type"] for row in events_repo.events_for_job(conn, stale)]
    assert EventType.FOLLOWUP_SUGGESTED in types
    assert applications_repo.get_for_job(conn, stale)["next_follow_up_at"] == to_iso(NOW)


def test_recent_event_keeps_application_alive(conn, norm, priority, make_posting):
    job = seed(
        conn,
        norm,
        priority,
        make_posting,
        company="Postman",
        title="Software Engineer II",
        at=_days_ago(40),
    )
    with conn:
        events_repo.add_event(conn, job, EventType.NOTE, "spoke to recruiter", at=_days_ago(3))
    assert ghost_check(conn, GHOST_DAYS, now=NOW) == []
    assert current_status(conn, job) == Status.APPLIED


def test_ghost_check_is_idempotent_and_dry_run_changes_nothing(conn, norm, priority, make_posting):
    job = seed(
        conn,
        norm,
        priority,
        make_posting,
        company="Postman",
        title="Software Engineer II",
        at=_days_ago(25),
    )
    assert ghost_check(conn, GHOST_DAYS, now=NOW, dry_run=True) == [job]
    assert current_status(conn, job) == Status.APPLIED
    assert ghost_check(conn, GHOST_DAYS, now=NOW) == [job]
    assert ghost_check(conn, GHOST_DAYS, now=NOW) == []
    followups = [
        row
        for row in events_repo.events_for_job(conn, job)
        if row["event_type"] == EventType.FOLLOWUP_SUGGESTED
    ]
    assert len(followups) == 1


def test_interview_stage_also_ghosts(conn, norm, priority, make_posting):
    job = seed(
        conn,
        norm,
        priority,
        make_posting,
        company="Figma",
        title="Software Engineer",
        status=Status.INTERVIEW,
        at=_days_ago(22),
    )
    assert ghost_check(conn, GHOST_DAYS, now=NOW) == [job]
    assert current_status(conn, job) == Status.GHOSTED
