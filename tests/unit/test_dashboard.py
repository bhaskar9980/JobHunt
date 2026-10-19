import re
from pathlib import Path

import pytest

from jobhunt.config import Settings
from jobhunt.dashboard import collect, render
from jobhunt.models import Status
from jobhunt.repo.jobs import set_job_status, upsert_job


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        root=tmp_path,
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        db_path=tmp_path / "data" / "jobs.db",
        raw={
            "paths": {"dashboard_path": "data/dashboard.html"},
            "dashboard": {"title": "Test Board", "description_chars": 50},
            "follow_up": {"days_after_apply": 10},
        },
    )


@pytest.fixture
def populated(conn, norm, priority, make_posting):
    a = upsert_job(conn, make_posting(source_job_id="1"), norm, priority).job_id
    b = upsert_job(
        conn,
        make_posting(
            source_job_id="2",
            url="https://boards.greenhouse.io/acme/jobs/2",
            title="Backend Developer",
            company="Globex",
        ),
        norm,
        priority,
    ).job_id
    c = upsert_job(
        conn,
        make_posting(
            source_job_id="3",
            url="https://boards.greenhouse.io/acme/jobs/3",
            title="Senior Engineer",
        ),
        norm,
        priority,
    ).job_id
    set_job_status(conn, a, Status.SHORTLISTED, force=True)
    set_job_status(conn, b, Status.APPLIED, force=True)
    set_job_status(conn, c, Status.FILTERED)
    return a, b, c


def test_collect_shapes(conn, settings, populated):
    data = collect(conn, settings)
    assert data["title"] == "Test Board"
    statuses = [col["status"] for col in data["kanban"]]
    assert statuses == [
        "shortlisted",
        "approved",
        "applied",
        "screening",
        "interview",
        "offer",
        "ghosted",
        "rejected",
        "withdrawn",
    ]
    shortlisted = next(col for col in data["kanban"] if col["status"] == "shortlisted")
    assert shortlisted["count"] == 1
    assert shortlisted["cards"][0]["company"] == "Acme, Inc."
    # filtered jobs are excluded from the table
    assert {r["status"] for r in data["rows"]} == {"shortlisted", "applied"}
    assert all(len(r["description"]) <= 50 for r in data["rows"])
    labels = [k["label"] for k in data["kpis"]]
    assert "Response rate" in labels


def test_render_writes_self_contained_html(conn, settings, populated):
    out = render(conn, settings)
    html = out.read_text(encoding="utf-8")
    assert out == settings.root / "data" / "dashboard.html"
    assert "<title>Test Board</title>" in html
    for status in ("shortlisted", "approved", "applied", "screening", "interview", "offer"):
        assert status in html
    assert "Globex" in html
    assert not re.search(r"<script[^>]+src=", html), "dashboard must not load external scripts"
    # A data: URI is part of the file, so the favicon is allowed; a remote href is not.
    remote = re.findall(r"<link[^>]+href=\"(?!data:)([^\"]+)", html)
    assert remote == [], f"dashboard must not load external assets: {remote}"
    assert 'rel="icon" href="data:' in html
    assert 'id="jobs-data"' in html


def test_render_overwrites_atomically(conn, settings, populated):
    first = render(conn, settings)
    second = render(conn, settings)
    assert first == second
    leftovers = [p for p in first.parent.iterdir() if p.name.startswith(".dashboard-")]
    assert leftovers == []


def test_corrupt_nested_workflow_is_reported_safely(settings):
    import json

    from jobhunt import dashboard

    settings.data_dir.mkdir(parents=True)
    (settings.data_dir / dashboard.WORKFLOW_FILE).write_text(
        json.dumps({"run_id": "test", "status": "running", "steps": {"health": None}})
    )
    assert dashboard.workflow_state(settings)["status"] == "failed"
    dashboard.update_workflow(settings, "health", "running", new_run=True)
    assert dashboard.workflow_state(settings)["steps"]["health"]["state"] == "running"


def test_parallel_workflow_updates_do_not_lose_stages(settings):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from jobhunt import dashboard

    dashboard.update_workflow(settings, "health", "running", new_run=True)
    stages = [stage for stage, _ in dashboard.WORKFLOW_STAGES]
    barrier = Barrier(len(stages))

    def finish(stage):
        barrier.wait(timeout=10)
        dashboard.update_workflow(settings, stage, "done")

    with ThreadPoolExecutor(max_workers=len(stages)) as pool:
        list(pool.map(finish, stages))
    result = dashboard.workflow_state(settings)
    assert set(result["steps"]) == set(stages)
    assert result["status"] == "done"


def test_finish_does_not_hide_running_stage(settings):
    from jobhunt import dashboard

    dashboard.update_workflow(settings, "health", "running", new_run=True)
    dashboard.update_workflow(settings, "finish", "done")
    assert dashboard.workflow_state(settings)["status"] != "done"
