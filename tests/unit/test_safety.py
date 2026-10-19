import pytest

from jobhunt.apply.safety import UNCONFIRMED_REASON, SafetyGate
from jobhunt.models import EventType, Status
from jobhunt.repo import events
from jobhunt.repo.jobs import apply_score, get_job, set_job_status, set_prefill_state, upsert_job


@pytest.fixture
def gate(conn):
    return SafetyGate(conn, daily_cap=2, workday_daily_cap=1, min_score=70, dedupe_days=90)


def _approved(conn, norm, priority, make_posting, **overrides):
    job_id = upsert_job(conn, make_posting(**overrides), norm, priority).job_id
    apply_score(conn, job_id, 85, {"reasons": ["fit"]})
    set_job_status(conn, job_id, Status.APPROVED, force=True)
    set_prefill_state(conn, job_id, "ready")
    return job_id


def test_passes_when_everything_is_in_order(conn, gate, norm, priority, make_posting):
    job_id = _approved(conn, norm, priority, make_posting)
    verdict = gate.check(get_job(conn, job_id), "greenhouse")
    assert verdict.ok, verdict.reasons


def test_requires_approved_and_ready(conn, gate, norm, priority, make_posting):
    job_id = upsert_job(conn, make_posting(), norm, priority).job_id
    apply_score(conn, job_id, 90, {})
    verdict = gate.check(get_job(conn, job_id), "greenhouse")
    assert not verdict.ok
    assert any("expected approved" in r for r in verdict.reasons)
    assert any("prefill_state" in r for r in verdict.reasons)


def test_low_score_blocks(conn, gate, norm, priority, make_posting):
    job_id = _approved(conn, norm, priority, make_posting)
    apply_score(conn, job_id, 50, {})
    verdict = gate.check(get_job(conn, job_id), "greenhouse")
    assert not verdict.ok
    assert any("below minimum" in r for r in verdict.reasons)


def test_daily_cap(conn, gate, norm, priority, make_posting):
    for n in range(2):
        job_id = _approved(
            conn,
            norm,
            priority,
            make_posting,
            source_job_id=str(n),
            url=f"https://boards.greenhouse.io/acme/jobs/{n}",
            company=f"Company{n}",
        )
        set_job_status(conn, job_id, Status.APPLIED)
    third = _approved(
        conn,
        norm,
        priority,
        make_posting,
        source_job_id="9",
        url="https://boards.greenhouse.io/acme/jobs/9",
        company="Other",
    )
    verdict = gate.check(get_job(conn, third), "greenhouse")
    assert not verdict.ok
    assert any("daily cap reached (2/2)" in r for r in verdict.reasons)


def test_workday_cap_is_separate(conn, gate, norm, priority, make_posting):
    first = _approved(
        conn,
        norm,
        priority,
        make_posting,
        source="workday",
        source_job_id="w1",
        ats_type="workday",
        url="https://acme.wd5.myworkdayjobs.com/en-US/careers/job/x",
        company="WD Co",
    )
    set_job_status(conn, first, Status.APPLIED)
    second = _approved(
        conn,
        norm,
        priority,
        make_posting,
        source="workday",
        source_job_id="w2",
        ats_type="workday",
        url="https://acme.wd5.myworkdayjobs.com/en-US/careers/job/y",
        company="WD Two",
    )
    verdict = gate.check(get_job(conn, second), "workday")
    assert any("workday daily cap" in r for r in verdict.reasons)
    other = _approved(
        conn,
        norm,
        priority,
        make_posting,
        source_job_id="g1",
        url="https://boards.greenhouse.io/acme/jobs/g1",
        company="GH Co",
    )
    assert gate.check(get_job(conn, other), "greenhouse").ok


def test_recent_duplicate_same_company_similar_title(conn, gate, norm, priority, make_posting):
    earlier = _approved(conn, norm, priority, make_posting, source_job_id="1")
    set_job_status(conn, earlier, Status.APPLIED)
    again = _approved(
        conn,
        norm,
        priority,
        make_posting,
        source_job_id="2",
        url="https://boards.greenhouse.io/acme/jobs/2",
        title="Software Engineer 2 (Backend)",
    )
    verdict = gate.check(get_job(conn, again), "greenhouse")
    assert not verdict.ok
    assert any("already applied" in r for r in verdict.reasons)
    different = _approved(
        conn,
        norm,
        priority,
        make_posting,
        source_job_id="3",
        url="https://boards.greenhouse.io/acme/jobs/3",
        title="Platform Reliability Engineer",
    )
    assert gate.check(get_job(conn, different), "greenhouse").ok


def test_unconfirmed_submission_blocks_until_explicit_reconciliation(
    conn, gate, norm, priority, make_posting
):
    job_id = _approved(conn, norm, priority, make_posting)
    with conn:
        events.add_event(conn, job_id, EventType.PREFILLED, "3 answered", at="2026-09-09T08:00:00Z")
        events.add_event(
            conn, job_id, EventType.SUBMISSION_UNKNOWN, "no confirmation", at="2026-09-09T08:05:00Z"
        )
    verdict = gate.check(get_job(conn, job_id), "greenhouse")
    assert not verdict.ok
    assert UNCONFIRMED_REASON in verdict.reasons

    with conn:
        events.add_event(conn, job_id, EventType.PREFILLED, "again", at="2026-09-09T09:00:00Z")
    assert not gate.check(get_job(conn, job_id), "greenhouse").ok
    from jobhunt.repo import applications

    with conn:
        applications.resolve_not_submitted(conn, job_id, "confirmed no submission at ATS")
    assert not gate.check(get_job(conn, job_id), "greenhouse").ok
    set_prefill_state(conn, job_id, "ready")
    assert gate.check(get_job(conn, job_id), "greenhouse").ok


def test_unconfirmed_submission_ignored_once_application_exists(
    conn, gate, norm, priority, make_posting
):
    job_id = _approved(conn, norm, priority, make_posting)
    with conn:
        events.add_event(conn, job_id, EventType.SUBMISSION_UNKNOWN, "x", at="2026-09-09T08:05:00Z")
    set_job_status(conn, job_id, Status.APPLIED)
    assert not gate.unconfirmed_submission(job_id)


def test_pending_attempts_count_toward_daily_cap(conn, gate, norm, priority, make_posting):
    from jobhunt.repo import applications

    job = _approved(conn, norm, priority, make_posting)
    with conn:
        assert applications.begin_submission(conn, job)
    assert gate.submitted_today() == 1


def test_snoozed_job_is_not_submittable(conn, gate, norm, priority, make_posting):
    from jobhunt.repo.jobs import set_snooze

    job = _approved(conn, norm, priority, make_posting)
    set_snooze(conn, job, "2999-01-01T00:00:00+00:00")
    assert not gate.check(get_job(conn, job), "greenhouse").ok


def test_concurrent_reservations_cannot_exceed_cap(conn, norm, priority, make_posting, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from jobhunt.db import connect

    jobs = [
        _approved(
            conn,
            norm,
            priority,
            make_posting,
            source_job_id=str(n),
            url=f"https://example.invalid/{n}",
            company=f"Company {n}",
        )
        for n in range(2)
    ]
    path = tmp_path / "caps.db"
    disk = connect(path)
    conn.backup(disk)
    disk.close()
    barrier = Barrier(2)

    def reserve(job_id):
        connection = connect(path)
        try:
            barrier.wait(timeout=10)
            return SafetyGate(connection, daily_cap=1).reserve(job_id, "greenhouse").ok
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve, jobs)) == [False, True]
