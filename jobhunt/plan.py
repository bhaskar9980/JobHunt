"""The morning plan: one structure with everything /jobs-day needs to show and act on."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

from jobhunt import review
from jobhunt.clock import now, to_iso
from jobhunt.config import Settings
from jobhunt.mail import ingest as mail_ingest
from jobhunt.match import ELIGIBILITY_REVIEW_REASON
from jobhunt.models import EventType, Status
from jobhunt.referral.enrich import build_tracking_enricher
from jobhunt.referral.route import (
    ROUTE_REFERRAL_DEADLINE,
    ROUTE_REFERRAL_FIRST,
    ROUTE_REFERRAL_PENDING,
)
from jobhunt.repo import stats as stats_repo

PLAN_QUEUE_LIMIT = 200
STATE_NONE = "none"
STATE_READY = "ready"
STATE_SCHEMA = "schema"
STATE_BLOCKED = "blocked"
LAST_SCAN_FILE = "last_scan.json"
FOLLOW_UP_LIMIT = 10
DEFAULT_FOLLOW_UP_DAYS = 10
STALE_SCAN_HOURS = 36
DATE_CHARS = 10

Enricher = Callable[[sqlite3.Row], dict[str, Any]]


def build_plan(
    conn: sqlite3.Connection, settings: Settings, enrich: Enricher | None = None
) -> dict[str, Any]:
    reference = now()
    items = review.queue(
        conn,
        [Status.SHORTLISTED.value, Status.APPROVED.value],
        PLAN_QUEUE_LIMIT,
        enrich=enrich if enrich is not None else build_tracking_enricher(conn),
        include_snoozed=True,
    )
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        group = _group_for(item)
        if group is not None:
            groups[group].append(_brief(item))
    groups["eligibility_review"] = [
        {**dict(row), "why": [row["prefilter_reasons"]], "red_flags": [ELIGIBILITY_REVIEW_REASON]}
        for row in conn.execute(
            "SELECT id AS job_id, company, title, location, url, score, status, is_remote, "
            "discovered_at, prefilter_reasons FROM jobs WHERE status = 'filtered' "
            "AND prefilter_reasons LIKE ? ORDER BY discovered_at DESC LIMIT ?",
            (f"%{ELIGIBILITY_REVIEW_REASON}%", PLAN_QUEUE_LIMIT),
        )
    ]
    return {
        "generated_at": to_iso(reference),
        "ready_to_apply": groups["ready_to_apply"],
        "referral_recommended": groups["referral_recommended"],
        "awaiting_referral": groups["awaiting_referral"],
        "needs_input": _needs_input(settings, groups),
        "progress": _progress(conn, settings, reference),
        "health": _health(conn, settings, reference),
    }


def _group_for(item: dict[str, Any]) -> str | None:
    if item.get("snoozed") or item["route"] == ROUTE_REFERRAL_PENDING:
        return "awaiting_referral" if item["route"] == ROUTE_REFERRAL_PENDING else None
    approved = item["status"] == Status.APPROVED.value
    if approved and item["prefill_state"] == STATE_BLOCKED:
        return "blocked"
    if item["route"] == ROUTE_REFERRAL_FIRST:
        return "referral_recommended"
    if approved and item["prefill_state"] == STATE_READY:
        return "ready_to_apply"
    if item["route"] == ROUTE_REFERRAL_DEADLINE:
        return "referral_deadline"
    preparation_group = {STATE_NONE: "to_prepare", STATE_SCHEMA: "unanswered_questions"}.get(
        item["prefill_state"]
    )
    return preparation_group if approved else "to_review"


def _needs_input(settings: Settings, groups: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    applications_dir = settings.path("applications_dir")
    return {
        "to_review": groups["to_review"],
        "eligibility_review": groups["eligibility_review"],
        "to_prepare": groups["to_prepare"],
        "unanswered_questions": [
            {**i, "questions": _unmatched_labels(applications_dir, i["job_id"])}
            for i in groups["unanswered_questions"]
        ],
        "blocked": groups["blocked"],
        "referral_deadline": groups["referral_deadline"],
        "mail_review": mail_ingest.load_needs_review(settings.path("mail_inbox_dir")),
    }


def _progress(conn: sqlite3.Connection, settings: Settings, reference) -> dict[str, Any]:
    today = to_iso(reference)[:DATE_CHARS]
    funnel = stats_repo.funnel_counts(conn)
    follow_up_days = int(
        settings.section("follow_up").get("days_after_apply", DEFAULT_FOLLOW_UP_DAYS)
    )
    return {
        "submitted_today": [
            dict(r)
            for r in conn.execute(
                """
                SELECT a.job_id, j.company, j.title, a.submitted_via, a.confirmation_ref,
                       a.confirmation_email_id
                FROM applications a JOIN jobs j ON j.id = a.job_id
                WHERE substr(COALESCE(a.submitted_at, a.applied_at), 1, 10) = ?
                ORDER BY a.job_id
                """,
                (today,),
            ).fetchall()
        ],
        "in_process": {
            "screening": funnel.get("screening", 0),
            "interview": funnel.get("interview", 0),
            "offer": funnel.get("offer", 0),
        },
        "interviews": [
            dict(r)
            for r in conn.execute(
                "SELECT id AS job_id, company, title, last_activity_at FROM v_pipeline "
                "WHERE pipeline_status = 'interview' ORDER BY last_activity_at DESC"
            ).fetchall()
        ],
        "outreach_awaiting_reply": [
            dict(r)
            for r in conn.execute(
                """
                SELECT o.id AS outreach_id, o.job_id, c.name AS contact_name, o.kind,
                       o.sent_at, o.followup_due
                FROM outreach o JOIN contacts c ON c.id = o.contact_id
                WHERE o.sent_at IS NOT NULL AND o.replied_at IS NULL
                ORDER BY o.sent_at
                """
            ).fetchall()
        ],
        "referrals_received": int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM events WHERE event_type = ?",
                (EventType.REFERRAL_RECEIVED.value,),
            ).fetchone()["n"]
        ),
        "follow_ups_due": [
            dict(r) for r in stats_repo.follow_ups_due(conn, follow_up_days)[:FOLLOW_UP_LIMIT]
        ],
    }


def _health(conn: sqlite3.Connection, settings: Settings, reference) -> dict[str, Any]:
    latest = conn.execute(
        """
        SELECT source, company, started_at, finished_at, fetched, new_jobs, error
        FROM scan_runs s
        WHERE id = (
            SELECT MAX(id) FROM scan_runs
            WHERE source = s.source AND COALESCE(company, '') = COALESCE(s.company, '')
        )
        ORDER BY source, company
        """
    ).fetchall()
    last_scan_at = max((str(r["started_at"]) for r in latest), default=None)
    stale_before = to_iso(reference - timedelta(hours=STALE_SCAN_HOURS))
    last_sync = mail_ingest.last_sync(conn)
    return {
        "last_scan_at": last_scan_at,
        "scan_stale": last_scan_at is None or last_scan_at < stale_before,
        "sources_ok": sum(1 for r in latest if r["error"] is None and r["finished_at"]),
        "sources_running": [
            {"source": r["source"], "company": r["company"], "started_at": r["started_at"]}
            for r in latest
            if not r["finished_at"] and r["error"] is None
        ],
        "sources_failed": [
            {"source": r["source"], "company": r["company"], "error": r["error"]}
            for r in latest
            if r["error"]
        ],
        "last_mail_sync_at": to_iso(last_sync) if last_sync else None,
        "last_scheduled_run": _last_scheduled_run(settings),
    }


def _last_scheduled_run(settings: Settings) -> dict[str, Any] | None:
    path: Path = settings.data_dir / LAST_SCAN_FILE
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"error": f"unreadable {path.name}"}


def _unmatched_labels(applications_dir: Path, job_id: int) -> list[str]:
    path = applications_dir / str(job_id) / "answers.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return [str(f.get("label", "")) for f in data.get("unmatched", []) if f.get("required")]


def _brief(item: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "job_id",
        "company",
        "title",
        "location",
        "score",
        "status",
        "ats",
        "apply_url",
        "prefill_state",
        "referral_count",
        "referral_top",
        "route",
        "route_reason",
        "route_contact",
        "wait_until",
        "why",
        "red_flags",
    )
    return {k: item.get(k) for k in keys}


def render_markdown(plan: dict[str, Any]) -> str:
    lines = [f"# Today ({plan['generated_at'][:DATE_CHARS]})", ""]
    health = plan["health"]
    lines.append(
        f"Scan: last {health['last_scan_at'] or 'never'}"
        f"{' (stale)' if health['scan_stale'] else ''}; "
        f"{health['sources_ok']} sources ok, {len(health['sources_failed'])} failed, "
        f"{len(health['sources_running'])} without a completed result. "
        f"Mail sync: {health['last_mail_sync_at'] or 'never'}."
    )
    for failed in health["sources_failed"]:
        lines.append(f"  - FAILED {failed['source']} {failed['company'] or ''}: {failed['error']}")
    lines += _section("Ready to apply", plan["ready_to_apply"])
    lines += _section("Referral recommended", plan["referral_recommended"])
    lines += _section("Awaiting referral", plan["awaiting_referral"])
    needs = plan["needs_input"]
    lines += _section("Needs your decision (review)", needs["to_review"])
    lines += _section("Eligibility verification", needs.get("eligibility_review", []))
    lines += _section("Needs preparation", needs["to_prepare"])
    lines += _section("Unanswered questions", needs["unanswered_questions"])
    lines += _section("Blocked (login wall or unconfirmed submission)", needs["blocked"])
    lines += _section("Referral deadline passed", needs["referral_deadline"])
    lines += ["", f"Mail needing review: {len(needs['mail_review'])}"]
    progress = plan["progress"]
    counts = ", ".join(f"{k} {v}" for k, v in progress["in_process"].items())
    lines += [
        "",
        "## Progress",
        f"- submitted today: {len(progress['submitted_today'])}",
        f"- in process: {counts}",
        f"- outreach awaiting reply: {len(progress['outreach_awaiting_reply'])}",
        f"- referrals received: {progress['referrals_received']}",
        f"- follow-ups due: {len(progress['follow_ups_due'])}",
    ]
    return "\n".join(lines) + "\n"


def _section(title: str, items: list[dict[str, Any]]) -> list[str]:
    lines = ["", f"## {title} ({len(items)})"]
    for item in items:
        route = f"  route={item['route']}" if item.get("route") else ""
        lines.append(
            f"- #{item['job_id']} {item['company']}: {item['title']} score={item['score']}{route}"
        )
        if item.get("route_reason") and item.get("route") != "apply":
            lines.append(f"    {item['route_reason']}")
        if item.get("questions"):
            lines.append(f"    questions: {'; '.join(item['questions'])}")
    return lines
