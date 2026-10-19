"""A full company fetch that no longer returns a job retires it (closed-job detection)."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from freezegun import freeze_time

from jobhunt import pipeline
from jobhunt.clock import now_iso, parse_iso
from jobhunt.models import Status
from jobhunt.repo.events import events_for_job
from jobhunt.repo.jobs import current_status, set_job_status, upsert_job
from jobhunt.sources.base import FetchError

CFG = {"name": "Acme", "ats": "greenhouse", "token": "acme"}
FIRST_RUN = "2026-09-08 08:30:00"
SECOND_RUN = "2026-09-09 08:30:00"


@pytest.mark.parametrize("fail_second", [False, True])
def test_search_variants_deduplicate_and_preserve_successful_results(
    conn, norm, priority, make_posting, monkeypatch, fail_second
):
    calls = []
    common = make_posting(source="workday")
    extra = make_posting(source="workday", source_job_id="extra", url="https://example.com/extra")

    def fetch(cfg, http):
        calls.append(cfg["search_text"])
        if cfg["search_text"] == ".NET":
            if fail_second:
                raise FetchError("temporary source failure")
            return [common, extra]
        return [common]

    monkeypatch.setattr(pipeline, "get_module", lambda key: SimpleNamespace(fetch=fetch))
    stats = pipeline._run_source(
        conn,
        "workday",
        {"name": "Acme", "search_terms": ["C#", ".NET", "C#"]},
        object(),
        norm,
        priority,
        None,
        False,
    )
    assert calls == ["C#", ".NET"]
    assert stats.fetched == (1 if fail_second else 2)
    assert stats.created == (1 if fail_second else 2)
    assert bool(stats.error) is fail_second
    assert stats.unlisted == 0


@pytest.mark.parametrize(
    "source", ["workday", "lever", "smartrecruiters", "eightfold", "successfactors"]
)
def test_bounded_or_filtered_inventory_is_not_proof_of_closure(
    conn, norm, priority, make_posting, source
):
    old = make_posting(source=source)
    job = upsert_job(conn, old, norm, priority).job_id
    result = pipeline._retire_unlisted(
        conn, source, {"name": old.company}, norm, [old], "2999-01-01T00:00:00+00:00"
    )
    assert result == 0
    assert current_status(conn, job) == Status.DISCOVERED


def test_current_source_alias_prevents_unlisting(conn, norm, priority, make_posting, monkeypatch):
    original = make_posting()
    with freeze_time(FIRST_RUN):
        job = upsert_job(conn, original, norm, priority).job_id
    current = replace(original, source_job_id="new-source-id")
    result = _run(conn, norm, priority, monkeypatch, [current])
    assert result.unlisted == 0
    assert current_status(conn, job) == Status.DISCOVERED


def _module(postings):
    return SimpleNamespace(fetch=lambda cfg, http: list(postings))


def _run(
    conn,
    norm,
    priority,
    monkeypatch,
    postings,
    source="greenhouse",
    at=SECOND_RUN,
    cutoff=None,
    dry_run=False,
):
    monkeypatch.setattr(pipeline, "get_module", lambda key: _module(postings))
    with freeze_time(at):
        return pipeline._run_source(conn, source, CFG, object(), norm, priority, cutoff, dry_run)


def test_missing_job_is_dismissed_with_unlisted_event(
    conn, norm, priority, make_posting, monkeypatch
):
    first = make_posting(source_job_id="1", url="https://boards.greenhouse.io/acme/jobs/1")
    second = make_posting(source_job_id="2", url="https://boards.greenhouse.io/acme/jobs/2")
    stats = _run(conn, norm, priority, monkeypatch, [first, second], at=FIRST_RUN)
    assert stats.created == 2 and stats.unlisted == 0
    ids = {r["source_job_id"]: r["job_id"] for r in conn.execute("SELECT * FROM job_sources")}

    stats = _run(conn, norm, priority, monkeypatch, [first])

    assert stats.unlisted == 1
    assert current_status(conn, ids["1"]) == Status.DISCOVERED
    assert current_status(conn, ids["2"]) == Status.DISMISSED
    kinds = [e["event_type"] for e in events_for_job(conn, ids["2"])]
    assert "unlisted" in kinds and "status_change" in kinds


def test_approved_job_is_only_noted(conn, norm, priority, make_posting, monkeypatch):
    gone = make_posting(source_job_id="9", url="https://boards.greenhouse.io/acme/jobs/9")
    keep = make_posting(source_job_id="8", url="https://boards.greenhouse.io/acme/jobs/8")
    _run(conn, norm, priority, monkeypatch, [gone, keep], at=FIRST_RUN)
    with freeze_time(FIRST_RUN):
        gone_id = upsert_job(conn, gone, norm, priority).job_id
        set_job_status(conn, gone_id, Status.APPROVED, force=True)

    stats = _run(conn, norm, priority, monkeypatch, [keep])

    assert stats.unlisted == 1
    assert current_status(conn, gone_id) == Status.APPROVED
    assert any(e["event_type"] == "unlisted" for e in events_for_job(conn, gone_id))


def test_empty_fetch_never_retires_anything(conn, norm, priority, make_posting, monkeypatch):
    posting = make_posting(source_job_id="1", url="https://boards.greenhouse.io/acme/jobs/1")
    _run(conn, norm, priority, monkeypatch, [posting], at=FIRST_RUN)
    stats = _run(conn, norm, priority, monkeypatch, [])
    assert stats.unlisted == 0
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'dismissed'").fetchone()[0] == 0


def test_aggregators_are_partial_and_never_retire(conn, norm, priority, make_posting, monkeypatch):
    a = make_posting(source="hn", source_job_id="a", url="https://news.ycombinator.com/item?id=1")
    b = make_posting(source="hn", source_job_id="b", url="https://news.ycombinator.com/item?id=2")
    _run(conn, norm, priority, monkeypatch, [a, b], source="hn", at=FIRST_RUN)
    stats = _run(conn, norm, priority, monkeypatch, [a], source="hn")
    assert stats.unlisted == 0
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'dismissed'").fetchone()[0] == 0


@pytest.mark.parametrize(
    "status",
    [
        Status.DISCOVERED,
        Status.FILTERED,
        Status.SCORED,
        Status.SHORTLISTED,
        Status.APPROVED,
    ],
)
def test_old_listed_job_updates_presence_not_content(
    conn,
    norm,
    priority,
    make_posting,
    monkeypatch,
    status,
):
    old = make_posting(posted_at=parse_iso("2026-07-01T00:00:00+00:00"), raw={"original": True})
    with freeze_time(FIRST_RUN):
        job_id = upsert_job(conn, old, norm, priority).job_id
        set_job_status(conn, job_id, status, force=True)
    before = dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())
    source_before = dict(conn.execute("SELECT * FROM job_sources").fetchone())
    changed = replace(old, company="Acme", description_md="Do not import old content", raw={})

    stats = _run(
        conn,
        norm,
        priority,
        monkeypatch,
        [changed],
        cutoff="2026-09-01T00:00:00+00:00",
    )

    assert stats.unlisted == 0
    assert stats.skipped_old == 1
    assert stats.created == stats.merged == stats.refreshed == 0
    assert dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()) == before
    source_after = dict(conn.execute("SELECT * FROM job_sources").fetchone())
    with freeze_time(SECOND_RUN):
        assert source_after.pop("last_seen_at") == now_iso()
    source_before.pop("last_seen_at")
    assert source_after == source_before
    assert not any(e["event_type"] == "unlisted" for e in events_for_job(conn, job_id))


def test_old_listed_jobs_do_not_hide_removed_jobs_or_import_old_new_jobs(
    conn,
    norm,
    priority,
    make_posting,
    monkeypatch,
):
    old = make_posting(posted_at=parse_iso("2026-07-01T00:00:00+00:00"))
    gone = replace(old, source_job_id="gone", url="https://example.test/gone")
    newly_found = replace(old, source_job_id="new", url="https://example.test/new")
    _run(conn, norm, priority, monkeypatch, [old, gone], at=FIRST_RUN)
    stats = _run(
        conn,
        norm,
        priority,
        monkeypatch,
        [old, newly_found],
        cutoff="2026-09-01T00:00:00+00:00",
    )

    assert stats.skipped_old == 2
    assert stats.created == stats.merged == 0
    assert stats.unlisted == 1
    rows = conn.execute(
        "SELECT s.source_job_id, j.status FROM jobs j JOIN job_sources s ON s.job_id = j.id"
    ).fetchall()
    assert {r["source_job_id"]: r["status"] for r in rows} == {
        old.source_job_id: Status.DISCOVERED,
        "gone": Status.DISMISSED,
    }


def test_old_presence_matches_source_id_and_normalized_company(
    conn,
    norm,
    priority,
    make_posting,
    monkeypatch,
):
    old = make_posting(posted_at=parse_iso("2026-07-01T00:00:00+00:00"))
    other_source = replace(
        old, source="lever", title="Different role", url="https://example.test/1"
    )
    other_company = replace(
        old,
        source_job_id="other",
        company="Other, Inc.",
        url="https://example.test/2",
    )
    with freeze_time(FIRST_RUN):
        for posting in (old, other_source, other_company):
            upsert_job(conn, posting, norm, priority)
        previous = now_iso()
    stats = _run(
        conn,
        norm,
        priority,
        monkeypatch,
        [replace(old, company="Acme"), replace(other_company, company="Acme")],
        cutoff="2026-09-01T00:00:00+00:00",
    )

    assert stats.unlisted == 0
    seen = {
        (r["source"], r["source_job_id"]): r["last_seen_at"]
        for r in conn.execute("SELECT * FROM job_sources")
    }
    with freeze_time(SECOND_RUN):
        assert seen[(old.source, old.source_job_id)] == now_iso()
    assert seen[(other_source.source, other_source.source_job_id)] == previous
    assert seen[(other_company.source, other_company.source_job_id)] == previous


def test_old_aggregator_presence_is_refreshed_without_retiring_omissions(
    conn,
    norm,
    priority,
    make_posting,
    monkeypatch,
):
    old = make_posting(source="hn", posted_at=parse_iso("2026-07-01T00:00:00+00:00"))
    omitted = replace(old, source_job_id="omitted", url="https://example.test/omitted")
    _run(conn, norm, priority, monkeypatch, [old, omitted], source="hn", at=FIRST_RUN)
    stats = _run(
        conn,
        norm,
        priority,
        monkeypatch,
        [old],
        source="hn",
        cutoff="2026-09-01T00:00:00+00:00",
    )

    assert stats.unlisted == 0
    assert stats.skipped_old == 1
    with freeze_time(SECOND_RUN):
        assert (
            conn.execute(
                "SELECT last_seen_at FROM job_sources WHERE source_job_id = ?",
                (old.source_job_id,),
            ).fetchone()[0]
            == now_iso()
        )
    assert all(r[0] == Status.DISCOVERED for r in conn.execute("SELECT status FROM jobs"))


def test_dry_run_does_not_refresh_old_presence(conn, norm, priority, make_posting, monkeypatch):
    old = make_posting(posted_at=parse_iso("2026-07-01T00:00:00+00:00"))
    _run(conn, norm, priority, monkeypatch, [old], at=FIRST_RUN)
    before = dict(conn.execute("SELECT * FROM job_sources").fetchone())
    stats = _run(
        conn,
        norm,
        priority,
        monkeypatch,
        [old],
        cutoff="2026-09-01T00:00:00+00:00",
        dry_run=True,
    )
    assert stats.unlisted == 0
    assert stats.skipped_old == 1
    assert dict(conn.execute("SELECT * FROM job_sources").fetchone()) == before


def test_failed_fetch_never_retires_jobs(conn, norm, priority, make_posting, monkeypatch):
    _run(conn, norm, priority, monkeypatch, [make_posting()], at=FIRST_RUN)

    def fail(cfg, http):
        raise FetchError("mock source unavailable")

    monkeypatch.setattr(pipeline, "get_module", lambda key: SimpleNamespace(fetch=fail))
    with freeze_time(SECOND_RUN):
        stats = pipeline._run_source(conn, "greenhouse", CFG, object(), norm, priority, None, False)
    assert stats.error == "mock source unavailable"
    assert stats.unlisted == 0
    assert conn.execute("SELECT status FROM jobs").fetchone()[0] == Status.DISCOVERED
