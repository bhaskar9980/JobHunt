"""Source fetches overlap in a thread pool; every SQLite write stays on the calling thread."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from jobhunt import pipeline
from jobhunt.sources.base import FetchError

WORKER_COUNT = 4
BARRIER_TIMEOUT_SECONDS = 10


def works(count: int) -> list[pipeline.SourceWork]:
    return [
        pipeline.SourceWork(source_key="greenhouse", cfg={"name": f"Acme {i}"}, label=f"acme-{i}")
        for i in range(count)
    ]


def scan_sources(conn, tasks, norm, priority, *, max_workers):
    return pipeline.scan_sources(
        conn,
        tasks,
        object(),
        norm,
        priority,
        cutoff=None,
        dry_run=False,
        max_workers=max_workers,
    )


def test_fetches_overlap_instead_of_running_one_after_another(
    conn, norm, priority, make_posting, monkeypatch
):
    """Every fetch must be in flight at once, or the barrier never releases."""
    barrier = threading.Barrier(WORKER_COUNT, timeout=BARRIER_TIMEOUT_SECONDS)

    def fetch(cfg, http):
        barrier.wait()
        return [make_posting(source_job_id=cfg["name"], url=f"https://x.test/{cfg['name']}")]

    monkeypatch.setattr(pipeline, "get_module", lambda key: SimpleNamespace(fetch=fetch))
    stats = scan_sources(conn, works(WORKER_COUNT), norm, priority, max_workers=WORKER_COUNT)

    assert [s.fetched for s in stats] == [1] * WORKER_COUNT
    assert [s.created for s in stats] == [1] * WORKER_COUNT


def test_database_writes_never_leave_the_calling_thread(
    conn, norm, priority, make_posting, monkeypatch
):
    fetch_threads: set[int] = set()
    write_threads: set[int] = set()
    real_upsert = pipeline.jobs_repo.upsert_job

    def fetch(cfg, http):
        fetch_threads.add(threading.get_ident())
        return [make_posting(source_job_id=cfg["name"], url=f"https://x.test/{cfg['name']}")]

    def upsert_job(*args, **kwargs):
        write_threads.add(threading.get_ident())
        return real_upsert(*args, **kwargs)

    monkeypatch.setattr(pipeline, "get_module", lambda key: SimpleNamespace(fetch=fetch))
    monkeypatch.setattr(pipeline.jobs_repo, "upsert_job", upsert_job)
    scan_sources(conn, works(WORKER_COUNT), norm, priority, max_workers=WORKER_COUNT)

    assert write_threads == {threading.get_ident()}
    assert fetch_threads - {threading.get_ident()}, "fetches never left the main thread"


def test_one_failing_source_does_not_stop_the_others(
    conn, norm, priority, make_posting, monkeypatch
):
    def fetch(cfg, http):
        if cfg["name"] == "Acme 1":
            raise FetchError("boom")
        return [make_posting(source_job_id=cfg["name"], url=f"https://x.test/{cfg['name']}")]

    monkeypatch.setattr(pipeline, "get_module", lambda key: SimpleNamespace(fetch=fetch))
    stats = scan_sources(conn, works(3), norm, priority, max_workers=3)

    assert [bool(s.error) for s in stats] == [False, True, False]
    assert [s.created for s in stats] == [1, 0, 1]


def test_an_unexpected_error_is_recorded_and_the_run_continues(
    conn, norm, priority, make_posting, monkeypatch
):
    def fetch(cfg, http):
        if cfg["name"] == "Acme 0":
            raise RuntimeError("driver blew up")
        return [make_posting(source_job_id=cfg["name"], url=f"https://x.test/{cfg['name']}")]

    monkeypatch.setattr(pipeline, "get_module", lambda key: SimpleNamespace(fetch=fetch))
    stats = scan_sources(conn, works(3), norm, priority, max_workers=3)

    assert "driver blew up" in (stats[0].error or "")
    assert [s.created for s in stats] == [0, 1, 1]


@pytest.mark.parametrize("max_workers", [1, 2, 8])
def test_results_keep_configuration_order_whatever_finishes_first(
    conn, norm, priority, make_posting, monkeypatch, max_workers
):
    def fetch(cfg, http):
        return [make_posting(source_job_id=cfg["name"], url=f"https://x.test/{cfg['name']}")]

    monkeypatch.setattr(pipeline, "get_module", lambda key: SimpleNamespace(fetch=fetch))
    stats = scan_sources(conn, works(5), norm, priority, max_workers=max_workers)

    assert [s.label for s in stats] == [f"acme-{i}" for i in range(5)]


def test_worker_count_comes_from_settings_and_is_at_least_one():
    assert pipeline.fetch_workers({"max_workers": 12}) == 12
    assert pipeline.fetch_workers({"max_workers": 0}) == 1
    assert pipeline.fetch_workers({}) == pipeline.DEFAULT_FETCH_WORKERS
