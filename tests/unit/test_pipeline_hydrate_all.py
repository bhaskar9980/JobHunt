"""Hydration fetches concurrently and keeps going until nothing is left to fetch."""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from jobhunt import pipeline
from jobhunt.config import Settings
from jobhunt.repo import jobs as jobs_repo

BATCH = 6
CONFIG_DIR = Path(__file__).parent.parent.parent / "config"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        root=tmp_path,
        config_dir=CONFIG_DIR,
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        db_path=tmp_path / "data" / "jobs.db",
        raw={"scan": {"hydrate_max_per_run": 2, "max_workers": BATCH}},
    )


def seed(conn, norm, priority, make_posting, count: int) -> list[int]:
    ids = []
    for index in range(count):
        posting = make_posting(
            source_job_id=f"h{index}",
            url=f"https://x.test/h{index}",
            description_md=None,
            needs_hydration=True,
        )
        ids.append(jobs_repo.upsert_job(conn, posting, norm, priority).job_id)
    return ids


def hydrating_module(seen: set[int] | None = None, barrier=None):
    def hydrate(posting, http):
        if barrier is not None:
            barrier.wait()
        if seen is not None:
            seen.add(threading.get_ident())
        return replace(posting, description_md="Build .NET services.")

    return SimpleNamespace(hydrate=hydrate)


def test_every_pending_job_is_hydrated_even_past_the_per_call_cap(
    conn, norm, priority, make_posting, settings, monkeypatch
):
    seed(conn, norm, priority, make_posting, BATCH)
    monkeypatch.setattr(pipeline, "get_module", lambda key: hydrating_module())
    monkeypatch.setitem(settings.section("scan"), "hydrate_max_per_run", 2)

    hydrated = pipeline.hydrate_all(conn, settings, object())

    assert hydrated == BATCH
    assert jobs_repo.needing_hydration(conn, 100) == []


def test_hydration_fetches_run_concurrently(
    conn, norm, priority, make_posting, settings, monkeypatch
):
    seed(conn, norm, priority, make_posting, BATCH)
    barrier = threading.Barrier(BATCH, timeout=10)
    threads: set[int] = set()
    monkeypatch.setattr(pipeline, "get_module", lambda key: hydrating_module(threads, barrier))
    monkeypatch.setitem(settings.section("scan"), "hydrate_max_per_run", 0)
    monkeypatch.setitem(settings.section("scan"), "max_workers", BATCH)

    hydrated = pipeline.hydrate_all(conn, settings, object())

    assert hydrated == BATCH
    assert threads - {threading.get_ident()}, "hydration never left the main thread"


def test_hydration_stops_when_a_pass_makes_no_progress(
    conn, norm, priority, make_posting, settings, monkeypatch
):
    """A source that always fails must not spin forever."""
    from jobhunt.sources.base import FetchError

    seed(conn, norm, priority, make_posting, 3)

    def always_fails(posting, http):
        raise FetchError("detail page is gone")

    monkeypatch.setattr(pipeline, "get_module", lambda key: SimpleNamespace(hydrate=always_fails))
    monkeypatch.setitem(settings.section("scan"), "hydrate_max_per_run", 2)

    assert pipeline.hydrate_all(conn, settings, object()) == 0
