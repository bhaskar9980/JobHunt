"""hydrate_pending persists every field a hydrator returns and respects the predicate."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from jobhunt import pipeline
from jobhunt.config import Settings
from jobhunt.repo.jobs import get_job, needing_hydration, upsert_job

CONFIG_DIR = Path(__file__).parent.parent.parent / "config"
HYDRATE_LIMIT = 50


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        root=tmp_path,
        config_dir=CONFIG_DIR,
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        db_path=tmp_path / "data" / "jobs.db",
        raw={"scan": {"hydrate_max_per_run": HYDRATE_LIMIT}},
    )


@pytest.fixture
def summary_job(conn, norm, priority, make_posting) -> int:
    posting = make_posting(
        source="smartrecruiters",
        source_job_id="sr-1",
        url="https://jobs.smartrecruiters.com/Acme/sr-1",
        apply_url=None,
        ats_type=None,
        location=None,
        description_md=None,
        needs_hydration=True,
        detail_ref="sr-1",
    )
    return upsert_job(conn, posting, norm, priority).job_id


def _fake_module(hydrated_fields: dict, calls: list) -> SimpleNamespace:
    def hydrate(posting, http):
        calls.append(posting)
        return replace(posting, needs_hydration=False, **hydrated_fields)

    return SimpleNamespace(hydrate=hydrate)


def test_hydrate_persists_apply_url_location_type_and_description(
    conn, norm, settings, summary_job, monkeypatch
):
    calls: list = []
    fields = {
        "description_md": "Full description",
        "apply_url": "https://boards.greenhouse.io/acme/jobs/77",
        "location": "Bengaluru, India",
        "employment_type": "Full-time",
        "salary_text": "INR 30-40 LPA",
    }
    monkeypatch.setattr(pipeline, "get_module", lambda key: _fake_module(fields, calls))

    count = pipeline.hydrate_pending(conn, settings, http=object(), norm=norm)

    assert count == 1
    assert calls[0].source_job_id == "sr-1"
    row = get_job(conn, summary_job)
    assert row["description_md"] == "Full description"
    assert row["apply_url"] == "https://boards.greenhouse.io/acme/jobs/77"
    assert row["location"] == "Bengaluru, India"
    assert row["location_bucket"] == "bengaluru"
    assert row["employment_type"] == "Full-time"
    assert row["salary_text"] == "INR 30-40 LPA"
    assert row["needs_hydration"] == 0
    assert needing_hydration(conn, HYDRATE_LIMIT) == []


def test_hydrate_keeps_existing_values_when_detail_returns_nothing(
    conn, norm, settings, summary_job, monkeypatch
):
    monkeypatch.setattr(pipeline, "get_module", lambda key: _fake_module({}, []))
    pipeline.hydrate_pending(conn, settings, http=object(), norm=norm)
    row = get_job(conn, summary_job)
    assert row["title"] == "Software Engineer II"
    assert row["needs_hydration"] == 0


def test_predicate_false_clears_flag_without_fetching(
    conn, norm, settings, summary_job, monkeypatch
):
    def explode(key):
        raise AssertionError("hydrate must not be called")

    monkeypatch.setattr(pipeline, "get_module", explode)
    count = pipeline.hydrate_pending(
        conn, settings, http=object(), should_hydrate=lambda row: False, norm=norm
    )
    assert count == 0
    assert needing_hydration(conn, HYDRATE_LIMIT) == []


def _failing_module() -> SimpleNamespace:
    from jobhunt.sources.base import FetchError

    def hydrate(posting, http):
        raise FetchError("HTTP 503")

    return SimpleNamespace(hydrate=hydrate)


def _mark_prefilter_pass(conn, job_id: int) -> None:
    conn.execute("UPDATE jobs SET prefilter_pass = 1, skill_overlap = 3 WHERE id = ?", (job_id,))
    conn.commit()


def test_scoring_waits_until_the_description_is_hydrated(
    conn, norm, settings, summary_job, monkeypatch
):
    from jobhunt.repo.jobs import unscored_candidates

    _mark_prefilter_pass(conn, summary_job)
    assert unscored_candidates(conn, 10) == []
    fields = {"description_md": "Full description"}
    monkeypatch.setattr(pipeline, "get_module", lambda key: _fake_module(fields, []))
    pipeline.hydrate_pending(conn, settings, http=object(), norm=norm)
    assert get_job(conn, summary_job)["prefilter_pass"] is None
    _mark_prefilter_pass(conn, summary_job)
    assert [r["id"] for r in unscored_candidates(conn, 10)] == [summary_job]


def test_hydration_is_retried_then_abandoned_after_three_failures(
    conn, norm, settings, summary_job, monkeypatch
):
    from jobhunt.repo.events import events_for_job
    from jobhunt.repo.jobs import unscored_candidates

    _mark_prefilter_pass(conn, summary_job)
    monkeypatch.setattr(pipeline, "get_module", lambda key: _failing_module())
    for attempt in range(1, pipeline.MAX_HYDRATE_FAILURES):
        assert pipeline.hydrate_pending(conn, settings, http=object(), norm=norm) == 0
        assert get_job(conn, summary_job)["needs_hydration"] == 1, attempt
        assert unscored_candidates(conn, 10) == []

    assert pipeline.hydrate_pending(conn, settings, http=object(), norm=norm) == 0

    row = get_job(conn, summary_job)
    assert row["needs_hydration"] == 0
    kinds = [e["event_type"] for e in events_for_job(conn, summary_job)]
    assert kinds.count("hydrate_failed") == pipeline.MAX_HYDRATE_FAILURES
    assert any(
        "scored from the summary" in (e["note"] or "") for e in events_for_job(conn, summary_job)
    )
    assert [r["id"] for r in unscored_candidates(conn, 10)] == [summary_job]
    assert needing_hydration(conn, HYDRATE_LIMIT) == []


def test_provisional_prefilter_is_redone_once_the_description_arrives(
    conn, norm, settings, summary_job, monkeypatch
):
    """A summary-only job passes provisionally; the real text must still face the skill gate."""
    from jobhunt.config import Profile
    from jobhunt.match import apply_prefilter, prefilter
    from jobhunt.models import NO_DESCRIPTION_REASON, Status
    from jobhunt.repo.jobs import current_status, prefilter_candidates

    profile = Profile.from_dict(
        {
            "targets": {"target_titles": ["software engineer"], "locations": ["bengaluru"]},
            "skills": {"must_have_any": ["c#", ".net"]},
        }
    )
    aliases = {"c#": ["c#", "csharp"], ".net": [".net", "dotnet"]}

    row = get_job(conn, summary_job)
    provisional = prefilter(row, profile, aliases)
    assert provisional.passed is True
    assert NO_DESCRIPTION_REASON in provisional.reasons
    apply_prefilter(conn, profile, aliases)
    assert get_job(conn, summary_job)["prefilter_pass"] == 1

    fields = {"description_md": "Build Java Spring services", "location": "Bengaluru, India"}
    monkeypatch.setattr(pipeline, "get_module", lambda key: _fake_module(fields, []))
    pipeline.hydrate_pending(conn, settings, http=object(), norm=norm)

    assert [r["id"] for r in prefilter_candidates(conn, rescan=False)] == [summary_job]
    apply_prefilter(conn, profile, aliases)
    row = get_job(conn, summary_job)
    assert row["prefilter_pass"] == 0
    assert "none of c#, .net mentioned" in row["prefilter_reasons"]
    assert current_status(conn, summary_job) == Status.FILTERED


def test_a_machine_score_is_invalidated_by_material_hydration(
    conn, norm, settings, summary_job, monkeypatch
):
    from jobhunt.repo.jobs import apply_score

    conn.execute(
        "UPDATE jobs SET prefilter_pass = 1, prefilter_reasons = ? WHERE id = ?",
        ('["no description yet"]', summary_job),
    )
    conn.commit()
    apply_score(conn, summary_job, 80, {"reasons": ["fit"]})
    monkeypatch.setattr(
        pipeline, "get_module", lambda key: _fake_module({"description_md": "Java only"}, [])
    )

    pipeline.hydrate_pending(conn, settings, http=object(), norm=norm)

    assert get_job(conn, summary_job)["prefilter_pass"] is None
    assert get_job(conn, summary_job)["score"] is None
