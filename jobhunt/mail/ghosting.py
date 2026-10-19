"""Applications that went quiet for ghost_days become ghosted with a follow-up suggested."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta

from jobhunt.clock import now as clock_now
from jobhunt.clock import parse_iso, to_iso
from jobhunt.models import EventType, Status
from jobhunt.repo import applications as applications_repo
from jobhunt.repo import events as events_repo
from jobhunt.repo import jobs as jobs_repo

log = logging.getLogger(__name__)

SOURCE = "ghost_check"


def stale_applications(
    conn: sqlite3.Connection, ghost_days: int, *, now: datetime | None = None
) -> list[sqlite3.Row]:
    """Active applications with no activity and no event within ghost_days."""
    reference = now or clock_now()
    cutoff = to_iso(reference - timedelta(days=ghost_days))
    stale: list[sqlite3.Row] = []
    for app in applications_repo.active(conn):
        last_activity = app["last_activity_at"] or app["applied_at"]
        latest_event = events_repo.latest_event_at(conn, int(app["job_id"]))
        if _older(last_activity, cutoff) and _older(latest_event, cutoff):
            stale.append(app)
    return stale


def ghost_check(
    conn: sqlite3.Connection,
    ghost_days: int,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
) -> list[int]:
    """Move stale applications to ghosted; idempotent because ghosted is no longer active."""
    reference = now or clock_now()
    stamp = to_iso(reference)
    moved: list[int] = []
    for app in stale_applications(conn, ghost_days, now=reference):
        job_id = int(app["job_id"])
        moved.append(job_id)
        if dry_run:
            continue
        jobs_repo.set_job_status(
            conn,
            job_id,
            Status.GHOSTED,
            f"{EventType.GHOST_CHECK}: no activity for {ghost_days} days",
            source=SOURCE,
            at=stamp,
        )
        with conn:
            app_row = applications_repo.get_for_job(conn, job_id)
            events_repo.add_event(
                conn,
                job_id,
                EventType.FOLLOWUP_SUGGESTED,
                f"no reply in {ghost_days} days; consider a follow-up",
                application_id=int(app_row["id"]) if app_row else None,
                source=SOURCE,
                at=stamp,
            )
            applications_repo.set_next_follow_up(conn, job_id, stamp)
    log.info("ghost check: %d application(s) %s", len(moved), "would move" if dry_run else "moved")
    return moved


def _older(value: str | None, cutoff_iso: str) -> bool:
    if not value:
        return True
    parsed = parse_iso(value)
    return parsed is not None and to_iso(parsed) < cutoff_iso
