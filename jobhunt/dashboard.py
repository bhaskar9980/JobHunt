"""Render data/dashboard.html: KPIs, kanban, sortable table, follow-ups. Inline assets only."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from jinja2 import Environment, FileSystemLoader, select_autoescape

from jobhunt.clock import now, parse_iso, to_iso
from jobhunt.config import PROFILE_FILE, Profile, Settings, load_profile
from jobhunt.db import connect, init_db, read_state, transaction, write_state
from jobhunt.match import job_location_ok
from jobhunt.models import ELIGIBILITY_REVIEW_REASON, NO_DESCRIPTION_REASON, Status
from jobhunt.prefilter_reasons import eligibility_review_pending, eligibility_settled_ok
from jobhunt.repo import stats as stats_repo

log = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).with_name("templates")
TEMPLATE_NAME = "dashboard.html.j2"
KANBAN_STATUSES = (
    Status.SHORTLISTED,
    Status.APPROVED,
    Status.APPLIED,
    Status.SCREENING,
    Status.INTERVIEW,
    Status.OFFER,
    Status.GHOSTED,
    Status.REJECTED,
    Status.WITHDRAWN,
)
TABLE_EXCLUDED = ("filtered", "dismissed")
KANBAN_CARD_LIMIT = 40
DEFAULT_DESCRIPTION_CHARS = 400
DEFAULT_FOLLOW_UP_DAYS = 10
HOURS_PER_DAY = 24
DEFAULT_REFRESH_SECONDS = 5
DEFAULT_PAGE_SIZE = 24
MIN_REFRESH_SECONDS = 2
MAX_REFRESH_SECONDS = 60
MAX_PAGE_SIZE = 100
WORKFLOW_FILE = "dashboard-workflow.json"
WORKFLOW_MESSAGE_LIMIT = 300
WORKFLOW_STAGES = (
    ("health", "Setup"),
    ("sync", "Inbox"),
    ("scan", "Find & score"),
    ("review", "Review"),
    ("applications", "Applications"),
    ("referrals", "Referrals"),
    ("finish", "Summary"),
)
WORKFLOW_STATES = frozenset({"running", "done", "failed", "skipped"})


def client_config(settings: Settings) -> dict[str, Any]:
    cfg = settings.section("dashboard")
    return {
        "refresh_seconds": max(
            MIN_REFRESH_SECONDS,
            min(MAX_REFRESH_SECONDS, float(cfg.get("refresh_seconds", DEFAULT_REFRESH_SECONDS))),
        ),
        "page_size": max(1, min(MAX_PAGE_SIZE, int(cfg.get("page_size", DEFAULT_PAGE_SIZE)))),
    }


def workflow_state(settings: Settings) -> dict[str, Any]:
    base = {
        "status": "idle",
        "steps": {},
        "stages": [{"id": key, "label": label} for key, label in WORKFLOW_STAGES],
    }
    try:
        raw = None
        if settings.db_path.exists():
            uri = settings.db_path.resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True)) as conn:
                conn.row_factory = sqlite3.Row
                if conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name = 'runtime_state'"
                ).fetchone():
                    raw = read_state(conn, "dashboard_workflow")
        record = _workflow_record(settings, raw)
        return {**base, **record, "stages": base["stages"]}
    except (OSError, ValueError, sqlite3.Error):
        return {**base, "status": "failed", "message": "Workflow progress could not be read."}


def _workflow_record(settings: Settings, raw: str | None) -> dict[str, Any]:
    path = settings.data_dir / WORKFLOW_FILE
    if raw is None and path.exists():
        raw = path.read_text(encoding="utf-8")
    if raw is None:
        return {}
    record = json.loads(raw)
    if not isinstance(record, dict) or not isinstance(record.get("steps"), dict):
        raise ValueError("invalid workflow record")
    if any(
        not isinstance(step, dict)
        or not isinstance(step.get("state"), str)
        or step["state"] not in WORKFLOW_STATES
        for step in record["steps"].values()
    ):
        raise ValueError("invalid workflow steps")
    return record


def update_workflow(
    settings: Settings, stage: str, state: str, *, message: str = "", new_run: bool = False
) -> dict[str, Any]:
    if stage not in dict(WORKFLOW_STAGES) or state not in WORKFLOW_STATES:
        raise ValueError("unknown workflow stage or state")
    if len(message) > WORKFLOW_MESSAGE_LIMIT:
        raise ValueError(f"progress message must be at most {WORKFLOW_MESSAGE_LIMIT} characters")
    with closing(connect(settings.db_path)) as conn:
        init_db(conn)
        with transaction(conn):
            record = (
                {}
                if new_run
                else _workflow_record(settings, read_state(conn, "dashboard_workflow"))
            )
            stamp = to_iso(now())
            if not record.get("run_id"):
                record = {"run_id": uuid4().hex, "started_at": stamp, "steps": {}}
            previous = record["steps"].get(stage, {})
            record["steps"][stage] = {
                "state": state,
                "message": message,
                "started_at": stamp if state == "running" else previous.get("started_at", stamp),
                "updated_at": stamp,
            }
            record.update(updated_at=stamp, active_stage=stage, message=message)
            record["status"] = _workflow_status(record["steps"])
            if record["status"] == "done":
                record["finished_at"] = stamp
            elif record["status"] == "running":
                record.pop("finished_at", None)
            write_state(conn, "dashboard_workflow", json.dumps(record), stamp)
    return record


def _workflow_status(steps: dict[str, Any]) -> str:
    states = [step["state"] for step in steps.values()]
    if "running" in states:
        return "running"
    if "failed" in states:
        return "failed"
    if all(stage in steps for stage, _ in WORKFLOW_STAGES):
        return "done"
    return "running"


def render(conn: sqlite3.Connection, settings: Settings) -> Path:
    out_path = settings.path("dashboard_path")
    data = collect(conn, settings)
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "j2"]),
    )
    html = env.get_template(TEMPLATE_NAME).render(**data)
    _write_atomic(out_path, html)
    log.info("dashboard written to %s", out_path)
    return out_path


def collect(conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    reference = now()
    cfg = settings.section("dashboard")
    follow_up_days = int(
        settings.section("follow_up").get("days_after_apply", DEFAULT_FOLLOW_UP_DAYS)
    )
    funnel = stats_repo.funnel_counts(conn)
    profile = load_profile(settings) if settings.config_file(PROFILE_FILE).exists() else None
    rows = _table_rows(
        conn, reference, int(cfg.get("description_chars", DEFAULT_DESCRIPTION_CHARS)), profile
    )
    return {
        "title": str(cfg.get("title", "JobHunt")),
        "generated_at": to_iso(reference),
        "kpis": _kpis(conn, funnel, reference),
        "kanban": [
            {
                "status": status.value,
                "count": funnel.get(status.value, 0),
                "cards": [r for r in rows if r["status"] == status.value][:KANBAN_CARD_LIMIT],
            }
            for status in KANBAN_STATUSES
        ],
        "rows": rows,
        "follow_ups": [dict(r) for r in stats_repo.follow_ups_due(conn, follow_up_days)],
        "funnel": funnel,
        "live_cfg": client_config(settings),
    }


def _kpis(conn: sqlite3.Connection, funnel: dict[str, int], reference) -> list[dict[str, Any]]:
    since = to_iso(reference - timedelta(days=1))
    rate = stats_repo.response_rate(conn)
    return [
        {"label": "Discovered 24h", "value": stats_repo.discovered_since(conn, since)},
        {"label": "Shortlisted", "value": funnel.get("shortlisted", 0)},
        {"label": "Approved", "value": funnel.get("approved", 0)},
        {"label": "Applied", "value": funnel.get("applied", 0)},
        {
            "label": "In process",
            "value": funnel.get("screening", 0) + funnel.get("interview", 0),
        },
        {"label": "Offers", "value": funnel.get("offer", 0)},
        {"label": "Response rate", "value": f"{rate:.0%}" if rate is not None else "n/a"},
    ]


def _table_rows(
    conn: sqlite3.Connection, reference, description_chars: int, profile: Profile | None = None
) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in TABLE_EXCLUDED)
    rows = conn.execute(
        f"""
        SELECT id, company, title, location, is_remote, url, apply_url, score, pipeline_status,
               discovered_at, posted_at, salary_text, description_md, applied_at, last_activity_at,
               ats_type, prefill_state, submission_state, snooze_until, updated_at, score_reasons,
               location_bucket, needs_hydration, prefilter_reasons
        FROM v_pipeline
        WHERE pipeline_status NOT IN ({placeholders})
           OR prefilter_reasons LIKE ?
        ORDER BY score DESC NULLS LAST, discovered_at DESC
        """,
        (*TABLE_EXCLUDED, f"%{ELIGIBILITY_REVIEW_REASON}%"),
    ).fetchall()
    if profile and profile.targets.international_remote_or_relocation:
        search_stages = {Status.DISCOVERED, Status.SCORED, Status.SHORTLISTED}
        rows = [
            row
            for row in rows
            if row["pipeline_status"] not in search_stages
            or eligibility_review_pending(row["prefilter_reasons"])
            or eligibility_settled_ok(row["prefilter_reasons"])
            or _eligible_search_row(row, profile)
        ]
    return [_row_view(r, reference, description_chars) for r in rows]


def _eligible_search_row(row: sqlite3.Row, profile: Profile) -> bool:
    passed, reason = job_location_ok(row, profile)
    return passed and reason != NO_DESCRIPTION_REASON


def _row_view(row: sqlite3.Row, reference, description_chars: int) -> dict[str, Any]:
    discovered = parse_iso(row["discovered_at"])
    age_days = None
    if discovered is not None:
        age_days = int((reference - discovered).total_seconds() // (HOURS_PER_DAY * 3600))
    description = (row["description_md"] or "")[:description_chars]
    try:
        reasons = json.loads(row["score_reasons"] or "{}")
    except (TypeError, ValueError):
        reasons = {}
    if not isinstance(reasons, dict):
        reasons = {}
    return {
        "id": row["id"],
        "company": row["company"],
        "title": row["title"],
        "location": row["location"] or ("Remote" if row["is_remote"] else ""),
        "is_remote": bool(row["is_remote"]),
        "url": row["url"],
        "apply_url": row["apply_url"],
        "score": row["score"],
        "status": row["pipeline_status"],
        "age_days": age_days,
        "discovered_at": row["discovered_at"],
        "applied_at": row["applied_at"],
        "last_activity_at": row["last_activity_at"],
        "salary_text": row["salary_text"],
        "description": description,
        "ats": row["ats_type"],
        "posted_at": row["posted_at"],
        "updated_at": row["updated_at"],
        "prefill_state": row["prefill_state"],
        "submission_state": row["submission_state"],
        "snooze_until": row["snooze_until"],
        "why": reasons.get("reasons", []),
        "red_flags": reasons.get("red_flags", []),
    }


def _write_atomic(path: Path, html: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(prefix=".dashboard-", suffix=".html", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as tmp:
            tmp.write(html)
        os.replace(tmp_name, path)
    except OSError:
        Path(tmp_name).unlink(missing_ok=True)
        raise
