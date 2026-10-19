"""The rules a daily run must keep, whichever model drives it.

1. A job the user skipped never returns to the queue.
2. Re-listing a job tomorrow refreshes it; it never resurrects a decision.
3. Nothing the dashboard shows is left unscored.

Each rule is written here as an executable check so that breaking one turns the suite red
rather than quietly changing what the user sees the next morning.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jobhunt import match
from jobhunt.config import Profile, Settings
from jobhunt.models import Status
from jobhunt.repo import jobs as jobs_repo
from jobhunt.review import Decision, decide

CONFIG_DIR = Path(__file__).parent.parent.parent / "config"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        root=tmp_path,
        config_dir=CONFIG_DIR,
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        db_path=tmp_path / "data" / "jobs.db",
        raw={"dashboard": {"description_chars": 200}},
    )


def skipped_job(conn, norm, priority, make_posting, **overrides) -> int:
    job_id = jobs_repo.upsert_job(conn, make_posting(**overrides), norm, priority).job_id
    decide(conn, Decision(job_id, "skip", None))
    assert jobs_repo.get_job(conn, job_id)["pipeline_status"] == Status.DISMISSED.value
    return job_id


def test_skipping_a_job_records_it_as_dismissed(conn, norm, priority, make_posting):
    """The rest of this file depends on skip meaning dismissed, so pin it down."""
    assert skipped_job(conn, norm, priority, make_posting)


def test_a_prefilter_rescan_never_restores_a_skipped_job(conn, norm, priority, make_posting):
    """Rescan restores jobs the rules had filtered. A human decision is not the rules."""
    job_id = skipped_job(conn, norm, priority, make_posting)

    assert job_id not in {row["id"] for row in jobs_repo.prefilter_candidates(conn, rescan=True)}


def test_a_full_profile_rescan_leaves_a_skipped_job_dismissed(conn, norm, priority, make_posting):
    """The end-to-end version: change nothing, rescan everything, the decision must hold."""
    job_id = skipped_job(conn, norm, priority, make_posting)

    match.apply_prefilter(conn, Profile.from_dict({}), {}, rescan=True)

    assert jobs_repo.get_job(conn, job_id)["pipeline_status"] == Status.DISMISSED.value


def test_re_listing_a_skipped_job_tomorrow_does_not_bring_it_back(
    conn, norm, priority, make_posting
):
    """Boards re-list the same requisition daily. An upsert must refresh, never revive."""
    posting = make_posting()
    job_id = skipped_job(conn, norm, priority, make_posting)

    result = jobs_repo.upsert_job(conn, posting, norm, priority)

    assert result.job_id == job_id, "the same posting must not create a second row"
    assert jobs_repo.get_job(conn, job_id)["pipeline_status"] == Status.DISMISSED.value


def test_re_listing_refreshes_content_without_touching_the_decision(
    conn, norm, priority, make_posting
):
    """A refreshed description is welcome. A refreshed status is not."""
    job_id = skipped_job(conn, norm, priority, make_posting)

    jobs_repo.upsert_job(
        conn, make_posting(description_md="Now hiring for .NET and Kafka."), norm, priority
    )
    row = jobs_repo.get_job(conn, job_id)

    assert row["pipeline_status"] == Status.DISMISSED.value
    assert "Kafka" in (row["description_md"] or "")


def test_a_vanished_job_is_retired_but_a_skipped_one_is_left_alone(
    conn, norm, priority, make_posting
):
    """Retirement must not rewrite a decision the user already made."""
    skipped = skipped_job(conn, norm, priority, make_posting)

    jobs_repo.mark_unlisted(conn, "greenhouse", "acme", "2099-01-01T00:00:00+00:00")

    assert jobs_repo.get_job(conn, skipped)["pipeline_status"] == Status.DISMISSED.value


def test_the_dashboard_never_shows_an_unscored_job(
    conn, norm, priority, make_posting, settings, tmp_path
):
    """Rule 3. A visible job with no score means the day's scoring pass was left unfinished."""
    from jobhunt import dashboard
    from jobhunt.cli_ops import unscored_on_dashboard

    job_id = jobs_repo.upsert_job(conn, make_posting(), norm, priority).job_id
    jobs_repo.set_job_status(conn, job_id, Status.SHORTLISTED, force=True)

    assert unscored_on_dashboard(conn, settings) == 1

    jobs_repo.apply_score(conn, job_id, 80, {"reasons": ["fits"], "red_flags": []})

    assert unscored_on_dashboard(conn, settings) == 0
    assert all(row["score"] is not None for row in dashboard.collect(conn, settings)["rows"])


def test_scoring_covers_a_job_parked_for_eligibility_too(conn, norm, priority, make_posting):
    """Parked jobs are visible, so rule 3 reaches them: they must be offered for scoring."""
    job_id = jobs_repo.upsert_job(conn, make_posting(), norm, priority).job_id
    conn.execute(
        "UPDATE jobs SET status='filtered', prefilter_pass=0, needs_hydration=0, score=NULL, "
        "prefilter_reasons='[\"eligibility needs verification: check visa\"]' WHERE id=?",
        (job_id,),
    )
    conn.commit()

    assert job_id in {row["id"] for row in jobs_repo.unscored_candidates(conn, 25)}


def test_profile_scoring_defaults_are_loadable(settings):
    """A rescan needs a real profile; this guards the fixture, not the rules."""
    assert Profile.from_dict({}).scoring.shortlist_min_score > 0
