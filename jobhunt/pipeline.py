"""Orchestration: scan sources, hydrate descriptions, ingest inbox files, run the prefilter."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any

from jobhunt.clock import now, now_iso, parse_iso, to_iso
from jobhunt.config import (
    CompanyConfig,
    Settings,
    build_normalizer,
    load_companies,
    load_sources,
    source_priority,
)
from jobhunt.models import EventType, JobPosting, UpsertOutcome
from jobhunt.normalize import Normalizer
from jobhunt.repo import events as events_repo
from jobhunt.repo import jobs as jobs_repo
from jobhunt.sources import AGGREGATOR_SOURCES, FULL_INVENTORY_SOURCES, UnknownSource, get_module
from jobhunt.sources.base import FetchError, Http, build_http

log = logging.getLogger(__name__)

HydratePredicate = Callable[[sqlite3.Row], bool]
MAX_HYDRATE_FAILURES = 3
DEFAULT_FETCH_WORKERS = 8
DEFAULT_HYDRATE_LIMIT = 500


@dataclass(slots=True)
class SourceStats:
    label: str
    fetched: int = 0
    created: int = 0
    merged: int = 0
    refreshed: int = 0
    skipped_old: int = 0
    unlisted: int = 0
    error: str | None = None


@dataclass(slots=True)
class SourceWork:
    """One source to fetch: an ATS company entry or an aggregator feed."""

    source_key: str
    cfg: Mapping[str, Any]
    label: str


@dataclass(slots=True)
class Fetched:
    """A finished network fetch, waiting for its turn to be written to SQLite."""

    work: SourceWork
    stats: SourceStats
    postings: list[JobPosting]
    started_at: str


@dataclass(slots=True)
class ScanSummary:
    sources: list[SourceStats] = field(default_factory=list)
    hydrated: int = 0

    @property
    def created(self) -> int:
        return sum(s.created for s in self.sources)

    @property
    def merged(self) -> int:
        return sum(s.merged for s in self.sources)

    @property
    def errors(self) -> list[str]:
        return [f"{s.label}: {s.error}" for s in self.sources if s.error]


def scan(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    only_source: str | None = None,
    only_company: str | None = None,
    hydrate: bool = True,
    dry_run: bool = False,
    should_hydrate: HydratePredicate | None = None,
) -> ScanSummary:
    norm = build_normalizer(settings)
    priority = source_priority(settings)
    http = build_http(settings.section("http"))
    cutoff = _posted_cutoff(settings)
    summary = ScanSummary()
    try:
        works = _scan_work(settings, only_source, only_company)
        workers = fetch_workers(settings.section("scan"))
        summary.sources = scan_sources(conn, works, http, norm, priority, cutoff, dry_run, workers)
        if hydrate and not dry_run:
            summary.hydrated = hydrate_all(conn, settings, http, should_hydrate, norm=norm)
    finally:
        http.close()
    return summary


def _scan_work(
    settings: Settings, only_source: str | None, only_company: str | None
) -> list[SourceWork]:
    works = [
        SourceWork(
            source_key=company.ats, cfg=asdict(company), label=f"{company.ats}:{company.name}"
        )
        for company in load_companies(settings)
        if _company_selected(company, only_source, only_company)
    ]
    if only_company:
        return works
    aggregators = load_sources(settings).get("aggregators") or {}
    for cfg_key, source_key in AGGREGATOR_SOURCES.items():
        cfg = aggregators.get(cfg_key) or {}
        if not cfg.get("enabled", False) or (only_source and only_source != source_key):
            continue
        works.append(SourceWork(source_key=source_key, cfg=cfg, label=source_key))
    return works


def _company_selected(
    company: CompanyConfig, only_source: str | None, only_company: str | None
) -> bool:
    if not company.enabled:
        return False
    if only_source and only_source.lower() != company.ats:
        return False
    return not only_company or only_company.lower() in company.name.lower()


def _posted_cutoff(settings: Settings) -> str | None:
    days = settings.section("scan").get("posted_within_days")
    if not days:
        return None
    return (now() - timedelta(days=int(days))).isoformat(timespec="seconds")


def fetch_workers(scan_settings: Mapping[str, Any]) -> int:
    """How many sources to fetch at once. One means the old sequential behaviour."""
    return max(1, int(scan_settings.get("max_workers", DEFAULT_FETCH_WORKERS)))


def fetch_source(work: SourceWork, http: Http) -> Fetched:
    """Network only, so this is safe to run off the main thread.

    It never raises: one unexpected failure among 169 sources must not discard the
    results of every source that already succeeded.
    """
    stats = SourceStats(label=work.label)
    started_at = now_iso()
    postings: list[JobPosting] = []
    try:
        postings = _fetch_variants(work.source_key, work.cfg, http, stats)
    except Exception as exc:  # noqa: BLE001 - task boundary; the error is reported in stats
        stats.error = f"unexpected fetch failure: {exc}"
        log.exception("source %s (%s) raised", work.source_key, work.cfg.get("name"))
    stats.fetched = len(postings)
    return Fetched(work=work, stats=stats, postings=postings, started_at=started_at)


def scan_sources(
    conn: sqlite3.Connection,
    works: list[SourceWork],
    http: Http,
    norm: Normalizer,
    priority: Mapping[str, int],
    cutoff: str | None,
    dry_run: bool,
    max_workers: int = 1,
) -> list[SourceStats]:
    """Fetch every source concurrently, then write each result on the calling thread.

    SQLite connections are not shareable between threads, so only the fetch is parallel.
    Results are stored as they arrive and reported back in configuration order.
    """
    if max_workers <= 1 or len(works) < 2:
        fetched = (fetch_source(work, http) for work in works)
        return [_store_fetched(conn, item, norm, priority, cutoff, dry_run) for item in fetched]
    stats: list[SourceStats | None] = [None] * len(works)
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="jobhunt-fetch") as pool:
        for index, item in enumerate(pool.map(lambda work: fetch_source(work, http), works)):
            stats[index] = _store_fetched(conn, item, norm, priority, cutoff, dry_run)
    return [item for item in stats if item is not None]


def _run_source(
    conn: sqlite3.Connection,
    source_key: str,
    cfg: Mapping[str, Any],
    http: Http,
    norm: Normalizer,
    priority: Mapping[str, int],
    cutoff: str | None,
    dry_run: bool,
) -> SourceStats:
    work = SourceWork(source_key=source_key, cfg=cfg, label=source_key)
    return _store_fetched(conn, fetch_source(work, http), norm, priority, cutoff, dry_run)


def _store_fetched(
    conn: sqlite3.Connection,
    fetched: Fetched,
    norm: Normalizer,
    priority: Mapping[str, int],
    cutoff: str | None,
    dry_run: bool,
) -> SourceStats:
    source_key, cfg = fetched.work.source_key, fetched.work.cfg
    stats, postings, started_at = fetched.stats, fetched.postings, fetched.started_at
    run_id = _start_run(conn, source_key, cfg.get("name"), started_at)
    for posting in postings:
        if _too_old(posting, cutoff):
            if not dry_run:
                jobs_repo.mark_seen(conn, posting, norm, started_at)
            stats.skipped_old += 1
            continue
        if dry_run:
            continue
        outcome = jobs_repo.upsert_job(conn, posting, norm, priority).outcome
        _count(stats, outcome)
    if not dry_run and not stats.error:
        stats.unlisted = _retire_unlisted(conn, source_key, cfg, norm, postings, started_at)
    _finish_run(conn, run_id, stats)
    log.info(
        "%s %s: fetched=%s created=%s merged=%s refreshed=%s old=%s unlisted=%s",
        source_key,
        cfg.get("name") or "",
        stats.fetched,
        stats.created,
        stats.merged,
        stats.refreshed,
        stats.skipped_old,
        stats.unlisted,
    )
    return stats


def _fetch_variants(
    source_key: str, cfg: Mapping[str, Any], http: Http, stats: SourceStats
) -> list[JobPosting]:
    queries = list(dict.fromkeys(cfg.get("search_terms") or [None]))
    postings: dict[tuple[str, str], JobPosting] = {}
    errors = []
    for query in queries:
        effective = {**cfg, "search_text": query} if query is not None else cfg
        try:
            for posting in get_module(source_key).fetch(effective, http):
                postings.setdefault((posting.source, posting.source_job_id or posting.url), posting)
        except (FetchError, UnknownSource, ValueError, KeyError) as exc:
            errors.append(f"{query}: {exc}" if query is not None else str(exc))
            log.warning("source %s (%s) failed: %s", source_key, cfg.get("name"), exc)
    stats.error = "; ".join(errors) or None
    return list(postings.values())


def _retire_unlisted(
    conn: sqlite3.Connection,
    source_key: str,
    cfg: Mapping[str, Any],
    norm: Normalizer,
    postings: list[JobPosting],
    started_at: str,
) -> int:
    """Retire only from full-board feeds; bounded searches cannot prove a job closed."""
    if (
        source_key not in FULL_INVENTORY_SOURCES
        or not postings
        or not cfg.get("name")
        or cfg.get("search_text")
        or cfg.get("search_terms")
    ):
        return 0
    company_norm = norm.company(str(cfg["name"]))
    dismissed, noted = jobs_repo.mark_unlisted(conn, source_key, company_norm, started_at)
    if dismissed or noted:
        log.info(
            "%s %s: %s unlisted jobs dismissed, %s approved jobs noted",
            source_key,
            cfg["name"],
            dismissed,
            noted,
        )
    return dismissed + noted


def _too_old(posting: JobPosting, cutoff: str | None) -> bool:
    if not cutoff or posting.posted_at is None:
        return False
    return parse_iso(to_iso(posting.posted_at)) < parse_iso(cutoff)


def _count(stats: SourceStats, outcome: UpsertOutcome) -> None:
    if outcome == UpsertOutcome.CREATED:
        stats.created += 1
    elif outcome == UpsertOutcome.MERGED:
        stats.merged += 1
    else:
        stats.refreshed += 1


def _start_run(
    conn: sqlite3.Connection, source: str, company: str | None, started_at: str | None = None
) -> int:
    with conn:
        cursor = conn.execute(
            "INSERT INTO scan_runs (source, company, started_at) VALUES (?, ?, ?)",
            (source, company, started_at or now_iso()),
        )
    return int(cursor.lastrowid or 0)


def _finish_run(conn: sqlite3.Connection, run_id: int, stats: SourceStats) -> None:
    with conn:
        conn.execute(
            "UPDATE scan_runs SET finished_at = ?, fetched = ?, new_jobs = ?, merged = ?, "
            "error = ? WHERE id = ?",
            (now_iso(), stats.fetched, stats.created, stats.merged, stats.error, run_id),
        )


def hydrate_pending(
    conn: sqlite3.Connection,
    settings: Settings,
    http: Http,
    should_hydrate: HydratePredicate | None = None,
    norm: Normalizer | None = None,
) -> int:
    """Fetch full details for jobs whose source only returned a summary.

    Persists description, apply URL, location, type, salary and date from the detail call.
    """
    scan_cfg = settings.section("scan")
    limit = int(scan_cfg.get("hydrate_max_per_run", 200)) or DEFAULT_HYDRATE_LIMIT
    normalizer = norm or build_normalizer(settings)
    rows = jobs_repo.needing_hydration(conn, limit)
    # Build the postings here: reading job_sources is a database call, and only this thread
    # may touch the connection. The pool then does nothing but network work.
    pending = [
        (row, _posting_from_row(conn, row))
        for row in rows
        if _wants_hydration(conn, row, should_hydrate)
    ]
    hydrated = 0
    for row, result, error in _fetch_details(pending, http, fetch_workers(scan_cfg)):
        if error is not None:
            _record_hydrate_failure(conn, int(row["id"]), error)
            continue
        jobs_repo.apply_hydration(conn, row["id"], result, normalizer)
        hydrated += 1
    return hydrated


def _wants_hydration(
    conn: sqlite3.Connection, row: sqlite3.Row, should_hydrate: HydratePredicate | None
) -> bool:
    """Jobs the caller does not want fetched keep the summary they already have."""
    if should_hydrate and not should_hydrate(row):
        jobs_repo.set_description(conn, row["id"], row["description_md"] or "")
        return False
    return True


def _fetch_one(
    row: sqlite3.Row, posting: JobPosting | None, http: Http
) -> tuple[sqlite3.Row, Any, str | None]:
    if posting is None:
        return row, None, "job has no source row to fetch the detail page from"
    try:
        return row, get_module(row["primary_source"]).hydrate(posting, http), None
    except (FetchError, UnknownSource, ValueError, KeyError) as exc:
        return row, None, str(exc)
    except Exception as exc:  # noqa: BLE001 - task boundary; the error is recorded per job
        log.exception("hydrate raised for job %s", row["id"])
        return row, None, f"unexpected hydrate failure: {exc}"


def _fetch_details(
    pending: list[tuple[sqlite3.Row, JobPosting | None]], http: Http, workers: int
) -> list[tuple[sqlite3.Row, Any, str | None]]:
    if workers <= 1 or len(pending) < 2:
        return [_fetch_one(row, posting, http) for row, posting in pending]
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="jobhunt-hydrate") as pool:
        return list(pool.map(lambda item: _fetch_one(item[0], item[1], http), pending))


def hydrate_all(
    conn: sqlite3.Connection,
    settings: Settings,
    http: Http,
    should_hydrate: HydratePredicate | None = None,
    norm: Normalizer | None = None,
) -> int:
    """Hydrate every waiting job, a page at a time, so nothing is left without a description.

    Stops as soon as a pass hydrates nothing, so a permanently failing source cannot spin.
    """
    normalizer = norm or build_normalizer(settings)
    total = 0
    while True:
        hydrated = hydrate_pending(conn, settings, http, should_hydrate, norm=normalizer)
        total += hydrated
        if hydrated == 0:
            return total


def _record_hydrate_failure(conn: sqlite3.Connection, job_id: int, error: str) -> None:
    """Count failures; after MAX_HYDRATE_FAILURES the summary is scored as is."""
    log.warning("hydrate failed for job %s: %s", job_id, error)
    with conn:
        events_repo.add_event(conn, job_id, EventType.HYDRATE_FAILED, error[:500], source="scan")
    failures = jobs_repo.hydrate_failures(conn, job_id)
    if failures >= MAX_HYDRATE_FAILURES:
        jobs_repo.clear_hydration(
            conn, job_id, f"description fetch failed {failures} times; scored from the summary"
        )


def _posting_from_row(conn: sqlite3.Connection, row: sqlite3.Row) -> JobPosting | None:
    src = conn.execute(
        "SELECT source, source_job_id, url FROM job_sources WHERE job_id = ? AND source = ?",
        (row["id"], row["primary_source"]),
    ).fetchone()
    if src is None:
        return None
    return JobPosting(
        source=src["source"],
        source_job_id=src["source_job_id"],
        company=row["company"],
        title=row["title"],
        url=row["url"],
        location=row["location"],
        is_remote=bool(row["is_remote"]),
        apply_url=row["apply_url"],
        ats_type=row["ats_type"],
        detail_ref=row["detail_ref"],
        posted_at=parse_iso(row["posted_at"]),
        needs_hydration=True,
    )
