"""Jobs parked for eligibility verification are scored and decided, not left in limbo."""

from __future__ import annotations

import json

import pytest

from jobhunt import dashboard, scoring
from jobhunt.config import Profile
from jobhunt.models import ELIGIBILITY_CHECKED_PREFIX, ELIGIBILITY_REVIEW_REASON
from jobhunt.repo import jobs as jobs_repo

REVIEW_REASON = f"{ELIGIBILITY_REVIEW_REASON}: India-eligible remote or visa plus relocation needed"


def park_for_review(conn, job_id: int, reason: str = REVIEW_REASON) -> None:
    conn.execute(
        "UPDATE jobs SET status = 'filtered', prefilter_pass = 0, prefilter_reasons = ?, "
        "needs_hydration = 0, score = NULL WHERE id = ?",
        (json.dumps([reason]), job_id),
    )
    conn.commit()


def import_results(conn, tmp_path, results):
    path = tmp_path / "scored.json"
    path.write_text(json.dumps({"results": results}), encoding="utf-8")
    return scoring.import_batch(conn, path, Profile.from_dict({}))


def make_job(conn, norm, priority, make_posting, **overrides):
    posting = make_posting(**overrides)
    return jobs_repo.upsert_job(conn, posting, norm, priority).job_id


def test_a_parked_job_is_offered_for_scoring(conn, norm, priority, make_posting):
    job_id = make_job(conn, norm, priority, make_posting)
    park_for_review(conn, job_id)

    rows = jobs_repo.unscored_candidates(conn, 25)

    assert [row["id"] for row in rows] == [job_id]


def test_a_plainly_filtered_job_is_not_offered_for_scoring(conn, norm, priority, make_posting):
    job_id = make_job(conn, norm, priority, make_posting)
    park_for_review(conn, job_id, reason="title names java, not c# or .net")

    assert jobs_repo.unscored_candidates(conn, 25) == []


def test_the_export_asks_for_an_eligibility_verdict(conn, norm, priority, make_posting, tmp_path):
    job_id = make_job(conn, norm, priority, make_posting)
    park_for_review(conn, job_id)
    out = tmp_path / "batch.json"

    scoring.export_batch(conn, Profile.from_dict({}), out, 25)
    job = json.loads(out.read_text(encoding="utf-8"))["jobs"][0]

    assert job["eligibility_review"] is True
    assert ELIGIBILITY_REVIEW_REASON in job["eligibility_question"]


@pytest.mark.parametrize(
    ("verdict", "expected_status", "expected_pass"),
    [("eligible", "shortlisted", 1), ("not_eligible", "filtered", 0), ("unclear", "filtered", 0)],
)
def test_the_verdict_decides_whether_the_job_rejoins_the_pipeline(
    conn, tmp_path, norm, priority, make_posting, verdict, expected_status, expected_pass
):
    job_id = make_job(conn, norm, priority, make_posting)
    park_for_review(conn, job_id)
    results = [
        {
            "job_id": job_id,
            "score": 85,
            "reasons": ["strong .NET backend match"],
            "red_flags": [],
            "resume_variant_hint": "dotnet-backend",
            "eligibility": verdict,
        }
    ]

    summary = import_results(conn, tmp_path, results)
    row = jobs_repo.get_job(conn, job_id)

    assert summary.errors == []
    assert summary.imported == 1
    assert row["pipeline_status"] == expected_status
    assert row["prefilter_pass"] == expected_pass
    assert row["score"] == 85
    assert ELIGIBILITY_CHECKED_PREFIX in row["prefilter_reasons"]
    assert verdict in row["prefilter_reasons"]


def test_a_decided_job_leaves_the_verification_queue(conn, tmp_path, norm, priority, make_posting):
    job_id = make_job(conn, norm, priority, make_posting)
    park_for_review(conn, job_id)
    results = [
        {
            "job_id": job_id,
            "score": 40,
            "reasons": ["US-only remote"],
            "red_flags": ["not India eligible"],
            "eligibility": "not_eligible",
        }
    ]
    import_results(conn, tmp_path, results)

    assert jobs_repo.unscored_candidates(conn, 25) == []


def test_a_parked_job_without_a_verdict_is_rejected(conn, tmp_path, norm, priority, make_posting):
    job_id = make_job(conn, norm, priority, make_posting)
    park_for_review(conn, job_id)
    results = [{"job_id": job_id, "score": 85, "reasons": ["looks fine"], "red_flags": []}]

    summary = import_results(conn, tmp_path, results)

    assert summary.imported == 0
    assert any("eligibility" in error for error in summary.errors)


def test_an_unknown_verdict_is_rejected(conn, tmp_path, norm, priority, make_posting):
    job_id = make_job(conn, norm, priority, make_posting)
    park_for_review(conn, job_id)
    results = [
        {
            "job_id": job_id,
            "score": 85,
            "reasons": ["looks fine"],
            "red_flags": [],
            "eligibility": "maybe",
        }
    ]

    summary = import_results(conn, tmp_path, results)

    assert summary.imported == 0
    assert any("eligibility" in error for error in summary.errors)


def test_the_all_jobs_table_shows_what_the_verification_queue_shows(
    conn, norm, priority, make_posting
):
    """Today's counts must never exceed All jobs: both draw on the same rows."""
    parked = make_job(conn, norm, priority, make_posting)
    park_for_review(conn, parked)
    plainly_filtered = make_job(
        conn, norm, priority, make_posting, source_job_id="2002", url="https://x.test/2002"
    )
    park_for_review(conn, plainly_filtered, reason="title names java, not c# or .net")

    rows = dashboard._table_rows(conn, dashboard.now(), 200)
    shown = {row["id"] for row in rows}

    assert parked in shown
    assert plainly_filtered not in shown


def international_profile() -> Profile:
    """A profile that parks unclear overseas postings instead of passing them straight through."""
    return Profile.from_dict(
        {
            "targets": {
                "locations": ["chennai", "remote"],
                "international_remote_or_relocation": True,
                "foreign_markers": ["united states", "germany"],
            }
        }
    )


def test_a_job_already_judged_eligible_stays_in_the_all_jobs_table(
    conn, tmp_path, norm, priority, make_posting
):
    """Once a verdict is recorded the table trusts it. Re-deriving would hide the job again."""
    job_id = make_job(conn, norm, priority, make_posting, location="2 Locations", is_remote=False)
    park_for_review(conn, job_id)
    import_results(
        conn,
        tmp_path,
        [
            {
                "job_id": job_id,
                "score": 87,
                "reasons": ["posting states relocation support and sponsorship"],
                "red_flags": [],
                "eligibility": "eligible",
            }
        ],
    )

    rows = dashboard._table_rows(conn, dashboard.now(), 200, international_profile())

    assert job_id in {row["id"] for row in rows}


def test_the_exported_question_makes_unclear_the_default_for_a_silent_posting(
    conn, norm, priority, make_posting, tmp_path
):
    """Scorers split on this once: a foreign city alone is not a refusal the employer made."""
    park_for_review(conn, make_job(conn, norm, priority, make_posting))
    out = tmp_path / "batch.json"

    scoring.export_batch(conn, Profile.from_dict({}), out, 25)
    question = json.loads(out.read_text(encoding="utf-8"))["jobs"][0]["eligibility_question"]

    assert "explicit restriction" in question
    assert "'unclear', never 'not_eligible'" in question
