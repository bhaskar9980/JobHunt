"""A job retired because a board stopped listing it must come back if the board relists it.

Rule 1 in CLAUDE.md says a run is additive. Retirement is the one path that removed a job for a
reason that can reverse itself, so it has to be reversible. A skip never is.
"""

from __future__ import annotations

from jobhunt.models import Status
from jobhunt.repo import jobs as jobs_repo
from jobhunt.review import Decision, decide

VANISHED = "2099-01-01T00:00:00+00:00"


def retire(conn, posting, norm, priority) -> int:
    """Store a job, then have a full board fetch stop returning it."""
    job_id = jobs_repo.upsert_job(conn, posting, norm, priority).job_id
    jobs_repo.mark_unlisted(conn, posting.source, norm.company(posting.company), VANISHED)
    assert jobs_repo.get_job(conn, job_id)["pipeline_status"] == Status.DISMISSED.value
    return job_id


def test_retirement_records_why_the_job_was_dismissed(conn, norm, priority, make_posting):
    job_id = retire(conn, make_posting(), norm, priority)

    assert jobs_repo.get_job(conn, job_id)["dismissed_reason"] == jobs_repo.DISMISSED_UNLISTED


def test_a_skip_records_a_different_reason(conn, norm, priority, make_posting):
    job_id = jobs_repo.upsert_job(conn, make_posting(), norm, priority).job_id

    decide(conn, Decision(job_id, "skip", None))

    assert jobs_repo.get_job(conn, job_id)["dismissed_reason"] == jobs_repo.DISMISSED_SKIPPED


def test_a_relisted_job_returns_to_the_pipeline(conn, norm, priority, make_posting):
    posting = make_posting()
    job_id = retire(conn, posting, norm, priority)

    result = jobs_repo.upsert_job(conn, posting, norm, priority)
    row = jobs_repo.get_job(conn, job_id)

    assert result.job_id == job_id
    assert row["pipeline_status"] == Status.DISCOVERED.value
    assert row["dismissed_reason"] is None


def test_a_relisted_job_that_the_user_skipped_stays_dismissed(conn, norm, priority, make_posting):
    """Rule 2 outranks rule 1. A decision is permanent even when the board relists the job."""
    posting = make_posting()
    job_id = jobs_repo.upsert_job(conn, posting, norm, priority).job_id
    decide(conn, Decision(job_id, "skip", None))

    jobs_repo.upsert_job(conn, posting, norm, priority)

    assert jobs_repo.get_job(conn, job_id)["pipeline_status"] == Status.DISMISSED.value


def test_a_relisted_job_comes_back_ready_to_be_scored_again(conn, norm, priority, make_posting):
    """It returns as a plain discovery, score included.

    Leaving the old score behind would strand it: scoring only offers jobs with no score, and
    DISCOVERED cannot reach SHORTLISTED, so nothing would ever move it forward again.
    """
    posting = make_posting()
    job_id = retire(conn, posting, norm, priority)
    jobs_repo.apply_score(conn, job_id, 78, {"reasons": ["good fit"], "red_flags": []})

    jobs_repo.upsert_job(conn, posting, norm, priority)
    row = jobs_repo.get_job(conn, job_id)

    assert row["pipeline_status"] == Status.DISCOVERED.value
    assert row["score"] is None
    assert row["prefilter_pass"] is None, "it must be re-judged by the prefilter too"


def test_a_relisted_job_is_never_left_in_a_state_nothing_can_promote(
    conn, norm, priority, make_posting
):
    """The stranding check: after revival the job is reachable by the normal daily pass."""
    from jobhunt.models import TRANSITIONS

    posting = make_posting()
    job_id = retire(conn, posting, norm, priority)
    jobs_repo.apply_score(conn, job_id, 78, {"reasons": ["good fit"], "red_flags": []})

    jobs_repo.upsert_job(conn, posting, norm, priority)
    row = jobs_repo.get_job(conn, job_id)

    # Either the prefilter can re-file it, or scoring can pick it up. Both must be open.
    assert Status.SCORED in TRANSITIONS[Status(row["pipeline_status"])]
    assert row["score"] is None, "scoring only offers jobs that have no score"


def test_an_applied_job_is_never_restored_by_a_relisting(conn, norm, priority, make_posting):
    """Only a dismissal caused by unlisting is reversible; nothing else is touched."""
    posting = make_posting()
    job_id = jobs_repo.upsert_job(conn, posting, norm, priority).job_id
    jobs_repo.set_job_status(conn, job_id, Status.APPLIED, force=True)

    jobs_repo.upsert_job(conn, posting, norm, priority)

    assert jobs_repo.get_job(conn, job_id)["pipeline_status"] == Status.APPLIED.value
