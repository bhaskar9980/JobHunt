import json

import pytest

from jobhunt.config import Profile
from jobhunt.models import PrefilterResult, Status
from jobhunt.repo.jobs import current_status, get_job, mark_prefilter, upsert_job
from jobhunt.scoring import export_batch, import_batch, validate_scored

PROFILE = Profile.from_dict(
    {
        "targets": {"target_titles": ["software engineer"], "locations": ["chennai"]},
        "skills": {"core": ["c#"]},
        "scoring": {"shortlist_min_score": 70, "batch_size": 25, "description_max_chars": 20},
    }
)


@pytest.fixture
def three_jobs(conn, norm, priority, make_posting):
    ids = []
    for n in range(3):
        job_id = upsert_job(
            conn,
            make_posting(
                source_job_id=str(n),
                url=f"https://boards.greenhouse.io/acme/jobs/{n}",
                description_md="A long description that exceeds twenty characters easily.",
            ),
            norm,
            priority,
        ).job_id
        ids.append(job_id)
    mark_prefilter(conn, ids[0], PrefilterResult(True, ["ok"], 0.8, 3))
    mark_prefilter(conn, ids[1], PrefilterResult(True, ["ok"], 0.4, None))
    mark_prefilter(conn, ids[2], PrefilterResult(False, ["title excluded"], 0.0, None))
    return ids


def test_export_selects_only_passing_unscored_and_truncates(conn, three_jobs, tmp_path):
    out = tmp_path / "batch.json"
    count = export_batch(conn, PROFILE, out, limit=10)
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert count == 2
    assert [j["job_id"] for j in payload["jobs"]] == [three_jobs[0], three_jobs[1]]
    assert len(payload["jobs"][0]["description_md"]) == 20
    assert payload["shortlist_min_score"] == 70
    assert payload["rubric_path"].endswith("scoring_rubric.md")


def test_export_respects_limit(conn, three_jobs, tmp_path):
    assert export_batch(conn, PROFILE, tmp_path / "b.json", limit=1) == 1


def test_import_scores_and_shortlists(conn, three_jobs, tmp_path):
    scored = tmp_path / "scored.json"
    scored.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "job_id": three_jobs[0],
                        "score": 85,
                        "reasons": ["great stack"],
                        "red_flags": [],
                        "resume_variant_hint": "dotnet-backend",
                    },
                    {"job_id": three_jobs[1], "score": 40, "reasons": ["frontend heavy"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    summary = import_batch(conn, scored, PROFILE)
    assert summary.imported == 2
    assert summary.shortlisted == 1
    assert summary.errors == []
    assert current_status(conn, three_jobs[0]) == Status.SHORTLISTED
    assert current_status(conn, three_jobs[1]) == Status.SCORED
    row = get_job(conn, three_jobs[0])
    assert row["score"] == 85
    assert json.loads(row["score_reasons"])["resume_variant_hint"] == "dotnet-backend"
    # scored jobs no longer appear in the next export
    assert export_batch(conn, PROFILE, tmp_path / "next.json", limit=10) == 0


def test_import_is_idempotent(conn, three_jobs, tmp_path):
    scored = tmp_path / "scored.json"
    scored.write_text(
        json.dumps({"results": [{"job_id": three_jobs[0], "score": 90, "reasons": ["x"]}]}),
        encoding="utf-8",
    )
    import_batch(conn, scored, PROFILE)
    summary = import_batch(conn, scored, PROFILE)
    assert summary.imported == 1
    assert summary.shortlisted == 0
    assert current_status(conn, three_jobs[0]) == Status.SHORTLISTED


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ({"job_id": 999, "score": 50, "reasons": ["x"]}, "not awaiting"),
        ({"job_id": 1, "score": 150, "reasons": ["x"]}, "score must be"),
        ({"job_id": 1, "score": "80", "reasons": ["x"]}, "score must be"),
        ({"job_id": 1, "score": 80, "reasons": []}, "reasons must be"),
        ({"job_id": "1", "score": 80, "reasons": ["x"]}, "bad or duplicate"),
        ("junk", "not an object"),
    ],
)
def test_validate_rejects_bad_rows(raw, fragment):
    scored, errors = validate_scored([raw], {1})
    assert scored == []
    assert len(errors) == 1
    assert fragment in errors[0]


def test_validate_rejects_duplicates_and_unknown_hint():
    scored, errors = validate_scored(
        [
            {"job_id": 1, "score": 80, "reasons": ["x"], "resume_variant_hint": "weird"},
            {"job_id": 1, "score": 70, "reasons": ["y"]},
        ],
        {1},
    )
    assert len(scored) == 1
    assert scored[0].resume_variant_hint is None
    assert "duplicate" in errors[0]


@pytest.mark.parametrize(
    "change",
    [
        {"job_id": True},
        {"score": True},
        {"red_flags": "warning"},
        {"red_flags": 12},
        {"reasons": [None]},
    ],
)
def test_malformed_score_fields_are_rejected(change):
    results, errors = validate_scored(
        [{"job_id": 1, "score": 80, "reasons": ["fit"], **change}], {1}
    )
    assert not results and errors


def test_rescore_below_threshold_removes_shortlist(conn, three_jobs, tmp_path):
    path = tmp_path / "scores.json"
    for score in (90, 30):
        path.write_text(
            json.dumps(
                {"results": [{"job_id": three_jobs[0], "score": score, "reasons": ["updated fit"]}]}
            )
        )
        import_batch(conn, path, PROFILE)
    assert current_status(conn, three_jobs[0]) == Status.SCORED


def test_scores_cannot_bypass_prefilter_or_hydration(conn, three_jobs, tmp_path):
    path = tmp_path / "scores.json"
    path.write_text(
        json.dumps(
            {
                "results": [
                    {"job_id": three_jobs[2], "score": 95, "reasons": ["not actually eligible"]}
                ]
            }
        )
    )
    result = import_batch(conn, path, PROFILE)
    assert result.imported == 0 and result.errors
