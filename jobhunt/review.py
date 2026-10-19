"""Review queue: what /jobs-review shows, and how approve/skip/snooze decisions are recorded."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from jobhunt.clock import now, now_iso, to_iso
from jobhunt.db import transaction
from jobhunt.models import Status
from jobhunt.repo import jobs as jobs_repo

log = logging.getLogger(__name__)

DEFAULT_LIMIT = 8
DECISIONS = ("approve", "skip", "snooze")
_QUEUE_LINE = re.compile(
    r"^- \[(?P<a>[ xX])\] approve\s*\|\s*\[(?P<s>[ xX])\] skip\s*\|\s*\[(?P<z>[ xX])\] snooze"
    r":?\s*(?P<until>\d{4}-\d{2}-\d{2})?\s+#(?P<id>\d+)\b"
)


@dataclass(slots=True)
class Decision:
    job_id: int
    action: str
    until: date | None = None


Enricher = Callable[[sqlite3.Row], dict[str, Any]]
DEFAULT_ROUTE = "apply"


def queue(
    conn: sqlite3.Connection,
    statuses: list[str],
    limit: int,
    referral_counts: dict[int, int] | None = None,
    *,
    enrich: Enricher | None = None,
    include_snoozed: bool = False,
) -> list[dict[str, Any]]:
    """Shortlisted (or requested) jobs, richest score first; snoozed hidden unless asked for.

    `enrich` (see jobhunt.referral.enrich) adds referral counts and the route recommendation.
    """
    rows = jobs_repo.list_jobs(conn, statuses=statuses)
    current = now_iso()
    items: list[dict[str, Any]] = []
    for row in rows:
        snoozed = bool(row["snooze_until"] and row["snooze_until"] > current)
        if snoozed and not include_snoozed:
            continue
        extra = enrich(row) if enrich else {}
        extra.setdefault("referral_count", (referral_counts or {}).get(row["id"], 0))
        item = _item(conn, row, int(extra["referral_count"]))
        item.update(extra)
        item["snoozed"] = snoozed
        items.append(item)
        if len(items) >= limit:
            break
    return items


def _item(conn: sqlite3.Connection, row: sqlite3.Row, referral_count: int) -> dict[str, Any]:
    reasons = _json(row["score_reasons"]) or {}
    return {
        "route": DEFAULT_ROUTE,
        "route_reason": "",
        "referral_top": [],
        "job_id": row["id"],
        "company": row["company"],
        "title": row["title"],
        "location": row["location"],
        "is_remote": bool(row["is_remote"]),
        "score": row["score"],
        "status": row["pipeline_status"],
        "salary_text": row["salary_text"],
        "why": reasons.get("reasons", []),
        "red_flags": reasons.get("red_flags", []),
        "resume_variant_hint": reasons.get("resume_variant_hint"),
        "ats": row["ats_type"],
        "url": row["url"],
        "apply_url": row["apply_url"] or row["url"],
        "posted_at": row["posted_at"],
        "referral_count": referral_count,
        "prefill_state": row["prefill_state"],
        "sources": [s["source"] for s in jobs_repo.sources_for_job(conn, row["id"])],
    }


def _json(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def render_markdown(items: list[dict[str, Any]]) -> str:
    """Checklist the user can tick when the batch is too big for AskUserQuestion."""
    lines = [
        "# Review queue",
        "",
        "Tick exactly one box per line. For snooze, set the date: `[x] snooze:2026-09-20`.",
        "Then run: `uv run jobhunt decide --from-file data/review_queue.md`",
        "",
    ]
    for item in items:
        flags = f" | flags: {'; '.join(item['red_flags'])}" if item["red_flags"] else ""
        lines.append(
            f"- [ ] approve | [ ] skip | [ ] snooze:  #{item['job_id']}  {item['company']}: "
            f"{item['title']}  score={item['score']}  {item['location'] or ''}"
        )
        lines.append(f"    why: {'; '.join(item['why'])}{flags}")
        if item.get("route", DEFAULT_ROUTE) != DEFAULT_ROUTE:
            lines.append(f"    route: {item['route']} ({item.get('route_reason', '')})")
        elif item.get("referral_count"):
            names = ", ".join(item.get("referral_top", []))
            lines.append(f"    contacts: {item['referral_count']} ({names})")
        lines.append(f"    {item['apply_url']}")
    return "\n".join(lines) + "\n"


def parse_markdown(text: str) -> list[Decision]:
    decisions: list[Decision] = []
    for line in text.splitlines():
        match = _QUEUE_LINE.match(line.strip())
        if not match:
            continue
        ticked = [
            action
            for action, flag in (
                ("approve", match["a"]),
                ("skip", match["s"]),
                ("snooze", match["z"]),
            )
            if flag.lower() == "x"
        ]
        if len(ticked) != 1:
            continue
        until = date.fromisoformat(match["until"]) if match["until"] else None
        decisions.append(Decision(int(match["id"]), ticked[0], until))
    return decisions


def decide(conn: sqlite3.Connection, decision: Decision) -> Status:
    if decision.action not in DECISIONS:
        raise ValueError(f"unknown decision {decision.action!r}; use {', '.join(DECISIONS)}")
    with transaction(conn):
        if decision.action == "approve":
            jobs_repo.set_snooze(conn, decision.job_id, None)
            return jobs_repo.set_job_status(
                conn, decision.job_id, Status.APPROVED, "approved in review"
            )
        if decision.action == "skip":
            status = jobs_repo.set_job_status(
                conn, decision.job_id, Status.DISMISSED, "skipped in review"
            )
            # Marked as a decision, so a later relisting by the board cannot undo it.
            jobs_repo.set_dismissed_reason(conn, decision.job_id, jobs_repo.DISMISSED_SKIPPED)
            return status
        until = decision.until or (now().date().fromordinal(now().date().toordinal() + 7))
        if until <= now().date():
            raise ValueError("snooze date must be in the future")
        stamp = to_iso(datetime.combine(until, datetime.min.time()))
        jobs_repo.set_snooze(conn, decision.job_id, stamp)
        jobs_repo.add_note(conn, decision.job_id, f"snoozed until {until.isoformat()}")
        return jobs_repo.current_status(conn, decision.job_id)
