"""The review dashboard, served on localhost so its buttons can change the database.

`jobhunt dashboard` writes a static file for reading. This module serves the same page with a
small JSON API behind it. Review decisions and verified status updates are available here;
submitting an application stays in `/jobs-apply`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import webbrowser
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from jinja2 import Environment, FileSystemLoader, select_autoescape

from jobhunt import dashboard
from jobhunt import plan as plan_mod
from jobhunt.apply.safety import SafetyGate
from jobhunt.apply_runs import AlreadyRunning, ApplyRuns
from jobhunt.clock import now, now_iso, parse_iso, to_iso
from jobhunt.config import Settings, build_normalizer, load_company_aliases
from jobhunt.db import connect
from jobhunt.models import APPLICATION_STAGES, TRANSITIONS, EventType, Status
from jobhunt.referral import ranker
from jobhunt.referral.enrich import build_enricher
from jobhunt.referral.rules import ReferralConfig
from jobhunt.repo import applications as applications_repo
from jobhunt.repo import contacts as contacts_repo
from jobhunt.repo import events as events_repo
from jobhunt.repo import jobs as jobs_repo
from jobhunt.repo import outreach as outreach_repo
from jobhunt.repo.applications import UNRESOLVED_SUBMISSIONS
from jobhunt.repo.jobs import JobNotFound
from jobhunt.review import Decision, decide

log = logging.getLogger(__name__)

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
TOKEN_PARAM = "t"
TOKEN_HEADER = "X-JobHunt-Token"
MAX_BODY_BYTES = 64 * 1024
MAX_BATCH_SIZE = 50
MAX_NOTE_CHARS = 2000
MAX_IDENTIFIER = 2**63 - 1
CONTACT_LIMIT = 8
INSTANCE_FILE = "dashboard-server.json"
SERVICE_NAME = "jobhunt-dashboard"
PROBE_TIMEOUT_SECONDS = 1
REQUEST_TIMEOUT_SECONDS = 10
REJECT_BODY_TIMEOUT_SECONDS = 1
JSON_TYPE = "application/json; charset=utf-8"
HTML_TYPE = "text/html; charset=utf-8"
MANUAL_STATUSES = frozenset(
    {
        Status.SCREENING,
        Status.INTERVIEW,
        Status.OFFER,
        Status.REJECTED,
        Status.WITHDRAWN,
        Status.SHORTLISTED,
        Status.GHOSTED,
    }
)
# The dashboard offers approve and skip only. Snoozing stays a CLI review decision, and
# `snooze_until` is still set by a referral request to park the job while a reply is awaited.
DASHBOARD_DECISIONS = ("approve", "skip")
MANUAL_APPROVAL_STATUSES = frozenset({Status.DISCOVERED, Status.SCORED, Status.SHORTLISTED})
APPLICATION_CHANNELS = frozenset({"manual", "easy_apply", "email", "assisted", "other"})
REFERRAL_CHANNELS = frozenset({"linkedin", "email", "other"})
MAX_CONTACT_NAME_CHARS = 150
MAX_REFERENCE_CHARS = 2048
MAX_FILL_REQUEST_ID_CHARS = 80
REVIEW_STATUSES = frozenset(
    {Status.DISCOVERED, Status.FILTERED, Status.SCORED, Status.SHORTLISTED, Status.APPROVED}
)


def _code_revision() -> str:
    root = Path(__file__).parent
    digest = hashlib.sha256()
    for path in sorted([*root.rglob("*.py"), *root.rglob("*.j2")]):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


CODE_REVISION = _code_revision()


class ApiError(Exception):
    """A request the page got wrong: reported as JSON, never as a stack trace."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _database_id(settings: Settings) -> str:
    identity = (
        os.path.normcase(str(settings.root.resolve()))
        + "|"
        + os.path.normcase(str(settings.db_path.resolve()))
        + "|"
        + os.path.normcase(str(settings.data_dir.resolve()))
        + "|"
        + json.dumps(settings.raw, sort_keys=True, default=str)
    )
    return hashlib.sha256(identity.encode()).hexdigest()


def _allowed_decisions(row: sqlite3.Row) -> list[str]:
    if row["submission_state"] in UNRESOLVED_SUBMISSIONS:
        return []
    status = Status(row["pipeline_status"])
    return (["approve"] if status in MANUAL_APPROVAL_STATUSES else []) + (
        ["skip"] if status in REVIEW_STATUSES else []
    )


def _check_revision(row: sqlite3.Row, payload: Mapping[str, Any]) -> None:
    for field, column in (
        ("expected_status", "pipeline_status"),
        ("expected_updated_at", "updated_at"),
    ):
        if field in payload and payload[field] != row[column]:
            raise ApiError(409, "This job changed. Refresh it before making a decision.")


class Api:
    """Everything the page may ask for. One connection per call, so threads stay independent."""

    def __init__(
        self, settings: Settings, *, token: str | None = None, runs: ApplyRuns | None = None
    ) -> None:
        self.settings = settings
        self.token = token or secrets.token_urlsafe(32)
        self.revision = CODE_REVISION
        self._env = Environment(
            loader=FileSystemLoader(str(dashboard.TEMPLATE_DIR)),
            autoescape=select_autoescape(["html", "j2"]),
        )
        self.runs = runs or ApplyRuns(self._run_fill)

    @contextmanager
    def _db(self) -> Generator[sqlite3.Connection]:
        conn = connect(self.settings.db_path)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def page(self) -> str:
        data = {
            "title": self.settings.section("dashboard").get("title", "JobHunt"),
            "generated_at": "",
            "rows": [],
            "kpis": [],
            "kanban": [],
            "follow_ups": [],
            "funnel": {},
            "live": True,
            "live_cfg": dashboard.client_config(self.settings),
        }
        return self._env.get_template(dashboard.TEMPLATE_NAME).render(**data)

    def state(self) -> dict[str, Any]:
        with self._db() as conn:
            conn.execute("BEGIN")
            data = dashboard.collect(conn, self.settings)
            plan = plan_mod.build_plan(conn, self.settings, build_enricher(conn, self.settings))
            tracking = _tracking(conn, self.settings, data)
            for row in data["rows"]:
                row["allowed_moves"] = _moves(row)
        return {
            **data,
            "plan": plan,
            "tracking": tracking,
            "workflow": dashboard.workflow_state(self.settings),
        }

    def job(self, job_id: int) -> dict[str, Any]:
        with self._db() as conn:
            conn.execute("BEGIN")
            row = _require_job(conn, job_id)
            data = jobs_repo.job_to_dict(conn, row)
            data["events"] = [dict(e) for e in events_repo.events_for_job(conn, job_id)]
            data["outreach"] = [
                dict(o)
                for o in outreach_repo.for_job(conn, job_id)
                if o["kind"] == outreach_repo.KIND_REFERRAL and o["sent_at"]
            ]
            data["allowed_moves"] = _moves({**dict(row), "status": row["pipeline_status"]})
            data["follow_up_at"] = None
            if data["application"]:
                app = data["application"]
                days = int(
                    self.settings.section("follow_up").get(
                        "days_after_apply", plan_mod.DEFAULT_FOLLOW_UP_DAYS
                    )
                )
                data["follow_up_at"] = app["next_follow_up_at"] or to_iso(
                    parse_iso(app["last_activity_at"]) + timedelta(days=days)
                )
            data["fill_readiness"] = self.fill_readiness(conn, row)
            data["fill_run"] = self.fill_status(job_id)["run"]
            data["allowed_decisions"] = _allowed_decisions(row)
            data["allowed_statuses"] = sorted(
                str(s)
                for s in TRANSITIONS.get(Status(row["pipeline_status"]), ())
                if s in MANUAL_STATUSES
            )
        return data

    def _run_fill(self, job_id: int) -> tuple[int, str]:
        """Fill one form for human review. Imports the browser stack lazily; it is heavy."""
        from jobhunt.cli import runner_for_server  # noqa: PLC0415 - avoids a CLI import cycle

        conn = connect(self.settings.db_path)
        try:
            result = runner_for_server(conn, self.settings).fill_for_review(
                job_id, self.runs.control(job_id)
            )
        finally:
            conn.close()
        return result.code, result.message

    def fill_readiness(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        """Whether review filling is available for this job, and why not when it is not.

        This only reports. The worker rechecks the guards before entering personal data;
        the dashboard never invokes the runner's submission path.
        """
        from jobhunt.apply.detect import AtsRegistry  # noqa: PLC0415 - config load, not hot path

        registry = AtsRegistry.load(self.settings.config_dir)
        ats = registry.detect(row["apply_url"] or row["url"], fallback=row["ats_type"])
        mode = registry.mode_for(ats)
        blocked = _fill_blocked_reason(row, ats, mode)
        if not blocked and not registry.review_url_allowed(row["apply_url"] or row["url"], ats):
            blocked = (
                "This URL is not a supported direct ATS form. Open the posting and fill it by hand."
            )
        if not blocked:
            verdict = SafetyGate.from_settings(conn, self.settings.section("apply")).check(
                row, ats, require_ready=False
            )
            blocked = "; ".join(verdict.reasons) if not verdict.ok else None
        active = self.runs.active()
        if not blocked and active and active.job_id != row["id"]:
            blocked = f"The fill window for job #{active.job_id} is still open. Close it first."
        return {"ok": blocked is None, "reason": blocked or "", "ats": ats or "", "mode": mode}

    def start_fill(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """The click permits filling and uploads, never an automated Submit click."""
        if payload.get("confirmed") is not True:
            raise ApiError(400, "Confirm filling your details on the employer's site first.")
        job_id = _int_field(payload, "job_id")
        identifier = _fill_identifier(payload, "request_id")
        with self._db() as conn:
            conn.execute("BEGIN")
            row = _require_job(conn, job_id)
            _check_revision(row, payload)
            readiness = self.fill_readiness(conn, row)
        if not readiness["ok"]:
            raise ApiError(409, readiness["reason"])
        try:
            state = self.runs.start(job_id, request_id=identifier)
        except (AlreadyRunning, ValueError) as exc:
            raise ApiError(409, str(exc)) from exc
        return {"ok": True, "run": state.to_dict()}

    def fill_status(self, job_id: int) -> dict[str, Any]:
        state = self.runs.status(job_id)
        return {"run": state.to_dict() if state else None}

    def control_fill(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if payload.get("confirmed") is not True:
            raise ApiError(400, "Confirm this fill-session action first.")
        job_id = _int_field(payload, "job_id")
        identifier = _fill_identifier(payload, "run_id")
        try:
            state = self.runs.command(job_id, identifier, str(payload.get("action") or ""))
        except ValueError as exc:
            raise ApiError(409, str(exc)) from exc
        return {"run": state.to_dict()}

    def apply_decision(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        job_id = _int_field(payload, "job_id")
        action = str(payload.get("action") or "")
        if action not in DASHBOARD_DECISIONS:
            raise ApiError(400, f"unknown action {action!r}; use {', '.join(DASHBOARD_DECISIONS)}")
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = _require_job(conn, job_id)
            _check_revision(row, payload)
            if action not in _allowed_decisions(row):
                raise ApiError(409, "That decision is not available for the job's current status.")
            if action == "approve" and row["pipeline_status"] != Status.SHORTLISTED:
                status = jobs_repo.set_job_status(
                    conn,
                    job_id,
                    Status.APPROVED,
                    "Approved for manual tracking; automated submission checks still apply",
                    force=True,
                    source="dashboard",
                )
            else:
                status = decide(conn, Decision(job_id, action))
        log.info("dashboard: job %s %s -> %s", job_id, action, status.value)
        return {"job_id": job_id, "action": action, "status": status.value}

    def batch_decision(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        items = payload.get("jobs")
        if not isinstance(items, list) or not 0 < len(items) <= MAX_BATCH_SIZE:
            raise ApiError(400, f"Select between 1 and {MAX_BATCH_SIZE} jobs.")
        if any(not isinstance(item, dict) for item in items):
            raise ApiError(400, "jobs must contain objects")
        results = []
        for item in items:
            try:
                result = self.apply_decision({**item, "action": payload.get("action")})
                results.append({**result, "ok": True})
            except (ApiError, jobs_repo.InvalidTransition) as exc:
                results.append(
                    {
                        "job_id": item.get("job_id"),
                        "ok": False,
                        "code": exc.status if isinstance(exc, ApiError) else 409,
                        "error": str(exc),
                    }
                )
        return {"results": results}

    def add_note(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        job_id = _int_field(payload, "job_id")
        text = _note_field(payload)
        with self._db() as conn:
            _require_job(conn, job_id)
            jobs_repo.add_note(conn, job_id, text)
        return {"job_id": job_id, "note": text}

    def update_status(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        job_id = _int_field(payload, "job_id")
        target = payload.get("status")
        if not isinstance(target, str) or target not in MANUAL_STATUSES:
            raise ApiError(400, "Only verified post-application progress can be recorded here.")
        text = _note_field(payload)
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = _require_job(conn, job_id)
            _check_revision(row, payload)
            if target == Status.SHORTLISTED and applications_repo.submission_unresolved(
                conn, job_id
            ):
                raise ApiError(
                    409, "Verify the unresolved submission before changing review status."
                )
            status = jobs_repo.set_job_status(conn, job_id, target, text, source="dashboard")
        return {"job_id": job_id, "status": status.value}

    def record_application(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        _confirmed(payload)
        job_id = _int_field(payload, "job_id")
        at = _past_time(payload)
        note = _note_field(payload)
        channel = _channel(payload, APPLICATION_CHANNELS)
        reference = _optional_text(payload, "reference", MAX_REFERENCE_CHARS)
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = _require_job(conn, job_id)
            _check_revision(row, payload)
            previous = applications_repo.get_for_job(conn, job_id)
            recorded = previous is not None and previous["submitted_at"] is not None
            if Status(row["pipeline_status"]) not in APPLICATION_STAGES:
                jobs_repo.set_job_status(
                    conn,
                    job_id,
                    Status.APPLIED,
                    "Manually verified completed application",
                    force=True,
                    source="dashboard",
                    at=at,
                )
            applications_repo.record_submission(
                conn,
                job_id,
                submitted_at=at,
                submitted_via=channel,
                confirmation_ref=reference or note,
            )
            if not recorded:
                events_repo.add_event(
                    conn, job_id, EventType.SUBMITTED, note, source="dashboard", at=at
                )
            jobs_repo.set_snooze(conn, job_id, None)
            status = jobs_repo.current_status(conn, job_id)
        return {"job_id": job_id, "status": str(status), "already_recorded": recorded}

    def record_referral(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        _confirmed(payload)
        job_id = _int_field(payload, "job_id")
        at, note = _past_time(payload), _note_field(payload)
        channel = _channel(payload, REFERRAL_CHANNELS)
        cfg = ReferralConfig.load(self.settings.config_dir)
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = _require_job(conn, job_id)
            _check_revision(job, payload)
            contact_id = _record_contact(conn, payload, job_id, at, channel)
            existing = conn.execute(
                "SELECT id FROM outreach WHERE job_id = ? AND contact_id = ? "
                "AND kind = 'referral' AND sent_at IS NOT NULL "
                "AND (referral_outcome = 'pending' OR sent_at = ?) "
                "ORDER BY id DESC LIMIT 1",
                (job_id, contact_id, at),
            ).fetchone()
            if existing:
                return {"job_id": job_id, "outreach_id": existing["id"], "already_recorded": True}
            outreach_id = outreach_repo.create(
                conn,
                contact_id=contact_id,
                job_id=job_id,
                channel=channel,
                kind=outreach_repo.KIND_REFERRAL,
            )
            days = outreach_repo.followup_days_for(1, cfg.followup_days)
            due = outreach_repo.mark_sent(conn, outreach_id, at, days)
            events_repo.add_event(
                conn, job_id, EventType.OUTREACH_SENT, note, source="dashboard", at=at
            )
            if job["pipeline_status"] in (
                Status.SHORTLISTED,
                Status.APPROVED,
            ) and not applications_repo.submission_unresolved(conn, job_id):
                until = to_iso(parse_iso(at) + timedelta(days=cfg.route_wait_days))
                jobs_repo.set_snooze(conn, job_id, until)
                events_repo.add_event(
                    conn,
                    job_id,
                    EventType.REFERRAL_WAIT,
                    f"Referral requested; waiting until {until[:10]}",
                    source="dashboard",
                    at=at,
                )
        return {
            "job_id": job_id,
            "outreach_id": outreach_id,
            "followup_due": due,
            "already_recorded": False,
        }

    def referral_outcome(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        _confirmed(payload)
        job_id, identifier = _int_field(payload, "job_id"), _int_field(payload, "outreach_id")
        at, note = _past_time(payload), _note_field(payload)
        outcome = payload.get("outcome")
        if not isinstance(outcome, str) or outcome not in ("completed", "declined", "reply"):
            raise ApiError(400, "Choose completed, declined, or reply.")
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            _require_job(conn, job_id)
            try:
                row = outreach_repo.get(conn, identifier)
            except outreach_repo.OutreachNotFound as exc:
                raise ApiError(404, str(exc)) from exc
            if (
                row["job_id"] != job_id
                or row["kind"] != outreach_repo.KIND_REFERRAL
                or not row["sent_at"]
            ):
                raise ApiError(400, "Select a recorded referral request for this job.")
            if parse_iso(at) < parse_iso(row["sent_at"]):
                raise ApiError(400, "The reply cannot precede the request.")
            if row["referral_outcome"] != "pending":
                if row["referral_outcome"] == outcome:
                    return {"outreach_id": identifier, "already_recorded": True}
                raise ApiError(
                    409, "This referral already has a recorded outcome. Refresh its history."
                )
            outreach_repo.mark_replied(
                conn, identifier, at, note, referral_outcome=None if outcome == "reply" else outcome
            )
            events_repo.add_event(
                conn, job_id, EventType.OUTREACH_REPLY, note, source="dashboard", at=at
            )
            if outcome == "completed":
                events_repo.add_event(
                    conn,
                    job_id,
                    EventType.REFERRAL_RECEIVED,
                    "Referral completion confirmed by the user",
                    source="dashboard",
                    at=at,
                )
        return {"job_id": job_id, "outreach_id": identifier, "outcome": outcome}

    def referrals(self, job_id: int) -> dict[str, Any]:
        cfg = ReferralConfig.load(self.settings.config_dir)
        norm = build_normalizer(self.settings)
        aliases = load_company_aliases(self.settings)
        with self._db() as conn:
            row = _require_job(conn, job_id)
            found = ranker.suggest(conn, dict(row), cfg, norm, aliases, now=now())
        return {"job_id": job_id, "contacts": [s.to_dict() for s in found[:CONTACT_LIMIT]]}


def _fill_identifier(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= MAX_FILL_REQUEST_ID_CHARS
        or not re.fullmatch(r"[A-Za-z0-9_-]+", value)
    ):
        raise ApiError(400, f"{key} must be a valid fill-session identifier.")
    return value


def _fill_blocked_reason(row: sqlite3.Row, ats: str | None, mode: str) -> str | None:
    """Why this job cannot be filled for review, or None when it can."""
    if mode != "python":
        label = ats or "this job board"
        return f"{label} has no automated filler yet, so apply to it by hand."
    if Status(row["pipeline_status"]) in APPLICATION_STAGES:
        return "This application has already been recorded."
    if row["pipeline_status"] != Status.APPROVED.value:
        return "Approve the job first."
    if row["submission_state"] in UNRESOLVED_SUBMISSIONS:
        return "A previous submission is unconfirmed. Check that before trying again."
    return None


def _confirmed(payload: Mapping[str, Any]) -> None:
    if payload.get("confirmed") is not True:
        raise ApiError(400, "Confirm that this action already happened before recording it.")


def _past_time(payload: Mapping[str, Any]) -> str:
    value = payload.get("at")
    try:
        if not isinstance(value, str) or not value or len(value) > 64:
            raise ValueError
        stamp = parse_iso(value)
        if stamp is None or stamp > now():
            raise ValueError
        return to_iso(stamp)
    except ValueError as exc:
        raise ApiError(400, "Choose a valid date/time that is not in the future.") from exc


def _channel(payload: Mapping[str, Any], choices: frozenset[str]) -> str:
    value = payload.get("channel")
    if not isinstance(value, str) or value not in choices:
        raise ApiError(400, "Choose a supported channel.")
    return value


def _optional_text(payload: Mapping[str, Any], name: str, limit: int) -> str | None:
    value = payload.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise ApiError(400, f"{name} must be text of at most {limit} characters")
    return value.strip() or None


def _record_contact(
    conn: sqlite3.Connection, payload: Mapping[str, Any], job_id: int, at: str, channel: str
) -> int:
    if payload.get("contact_id") not in (None, ""):
        identifier = _int_field(payload, "contact_id")
        try:
            contacts_repo.get_contact(conn, identifier)
        except contacts_repo.ContactNotFound as exc:
            raise ApiError(404, str(exc)) from exc
        return identifier
    name = _optional_text(payload, "contact_name", MAX_CONTACT_NAME_CHARS)
    if not name:
        raise ApiError(400, "Select a contact or enter the person's name.")
    existing = conn.execute(
        "SELECT c.id FROM contacts c JOIN outreach o ON o.contact_id = c.id "
        "WHERE o.job_id = ? AND lower(c.name) = lower(?) AND o.sent_at = ? AND o.channel = ? "
        "AND o.kind = 'referral' ORDER BY o.id DESC LIMIT 1",
        (job_id, name, at, channel),
    ).fetchone()
    if existing:
        return int(existing["id"])
    cursor = conn.execute("INSERT INTO contacts (name, source) VALUES (?, 'dashboard')", (name,))
    return int(cursor.lastrowid)


def _moves(row: Mapping[str, Any]) -> list[str]:
    status = Status(row["status"])
    targets = [str(s) for s in TRANSITIONS.get(status, ()) if s in MANUAL_STATUSES]
    if status not in APPLICATION_STAGES:
        targets.append(str(Status.APPLIED))
        if status in MANUAL_APPROVAL_STATUSES:
            targets.append(str(Status.APPROVED))
    if row["submission_state"] in UNRESOLVED_SUBMISSIONS:
        return [str(Status.APPLIED)]
    return sorted(set(targets))


def _tracking(
    conn: sqlite3.Connection, settings: Settings, data: Mapping[str, Any]
) -> dict[str, Any]:
    days = int(
        settings.section("follow_up").get("days_after_apply", plan_mod.DEFAULT_FOLLOW_UP_DAYS)
    )
    application_due = [
        {
            **dict(row),
            "kind": "application",
            "due_at": row["next_follow_up_at"]
            or to_iso(parse_iso(row["last_activity_at"]) + timedelta(days=days)),
        }
        for row in data["follow_ups"]
    ]
    referral_due = [
        {
            **dict(row),
            "kind": "referral",
            "company": row["job_company"],
            "title": row["job_title"],
            "due_at": row["followup_due"],
        }
        for row in outreach_repo.due(conn, now_iso())
    ]
    requests = [
        dict(row)
        for row in outreach_repo.list_all(conn)
        if row["kind"] == outreach_repo.KIND_REFERRAL and row["sent_at"] and row["job_id"]
    ]
    return {
        "active_applications": sum(
            data["funnel"].get(s, 0) for s in ("applied", "screening", "interview", "offer")
        ),
        "follow_up_due_count": len(application_due) + len(referral_due),
        "follow_ups": sorted(application_due + referral_due, key=lambda item: item["due_at"]),
        "referrals": requests,
        "follow_up_days": days,
    }


def _first(query: Mapping[str, list[str]], key: str) -> str | None:
    values = query.get(key) or []
    return values[0] if values else None


def _require_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row:
    try:
        return jobs_repo.get_job(conn, job_id)
    except JobNotFound as exc:
        raise ApiError(404, str(exc)) from exc


def _int_field(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    try:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError
        number = int(value)
        if not 0 < number <= MAX_IDENTIFIER:
            raise ValueError
        return number
    except (TypeError, ValueError) as exc:
        raise ApiError(400, f"{key} must be a positive integer") from exc


def _note_field(payload: Mapping[str, Any]) -> str:
    value = payload.get("note")
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_NOTE_CHARS:
        raise ApiError(400, f"A note of 1-{MAX_NOTE_CHARS} characters is required.")
    return value.strip()


class _Handler(BaseHTTPRequestHandler):
    server_version = "JobHunt"
    protocol_version = "HTTP/1.1"

    @property
    def api(self) -> Api:
        return self.server.api  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        log.debug("dashboard HTTP %s %s", self.command, urlparse(self.path).path)

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        query = parse_qs(url.query)
        token = self.headers.get(TOKEN_HEADER)
        self._guard(lambda: self._route_get(url.path, query), token=token, public=url.path == "/")

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        token = self.headers.get(TOKEN_HEADER)
        self._guard(lambda: self._route_post(url.path), token=token)

    def _route_get(self, path: str, query: dict[str, list[str]]) -> None:
        if path == "/":
            self._send(200, HTML_TYPE, self.api.page().encode("utf-8"))
            return
        if path == "/api/health":
            self._send_json(
                200,
                {
                    "service": SERVICE_NAME,
                    "database": _database_id(self.api.settings),
                    "revision": self.api.revision,
                },
            )
            return
        if path == "/api/state":
            self._send_json(200, self.api.state())
            return
        readers = {
            "/api/job": self.api.job,
            "/api/referrals": self.api.referrals,
            "/api/fill-status": self.api.fill_status,
        }
        if path in readers:
            job_id = _int_field({"job_id": _first(query, "job_id")}, "job_id")
            self._send_json(200, readers[path](job_id))
            return
        raise ApiError(404, f"no route for {path}")

    def _route_post(self, path: str) -> None:
        handlers = {
            "/api/decide": self.api.apply_decision,
            "/api/decisions": self.api.batch_decision,
            "/api/note": self.api.add_note,
            "/api/status": self.api.update_status,
            "/api/applied": self.api.record_application,
            "/api/referral": self.api.record_referral,
            "/api/referral-outcome": self.api.referral_outcome,
            "/api/fill-review": self.api.start_fill,
            "/api/fill-control": self.api.control_fill,
        }
        if path not in handlers:
            raise ApiError(404, f"no route for {path}")
        self._send_json(200, handlers[path](self._body()))

    def _guard(self, work: Any, *, token: str | None, public: bool = False) -> None:
        try:
            port = self.server.server_address[1]
            hosts = {f"{HOST}:{port}", f"localhost:{port}"}
            if self.headers.get("Host") not in hosts:
                raise ApiError(403, "Only local dashboard access is permitted.")
            origin = self.headers.get("Origin")
            if origin is not None and origin not in {f"http://{h}" for h in hosts}:
                raise ApiError(403, "Cross-origin dashboard requests are not permitted.")
            if not public and (
                not token or not secrets.compare_digest(token.encode(), self.api.token.encode())
            ):
                raise ApiError(
                    403, "Access expired or missing. Run `uv run jobhunt serve` to reopen."
                )
            work()
        except ApiError as exc:
            self._send_json(exc.status, {"error": exc.message})
        except jobs_repo.InvalidTransition as exc:
            self._send_json(409, {"error": str(exc)})
        except (sqlite3.Error, OSError, ValueError, KeyError, RuntimeError, JobNotFound):
            log.exception("dashboard request failed: %s", urlparse(self.path).path)
            self._send_json(500, {"error": "request failed; see logs/jobhunt.log"})

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise ApiError(400, "invalid Content-Length") from exc
        if length < 0 or self.headers.get("Transfer-Encoding"):
            raise ApiError(400, "invalid request framing")
        if length > MAX_BODY_BYTES:
            raise ApiError(413, "body too large")
        if not length:
            return {}
        if self.headers.get_content_type() != "application/json":
            raise ApiError(415, "Content-Type must be application/json")
        self.connection.settimeout(REQUEST_TIMEOUT_SECONDS)
        try:
            raw = self.rfile.read(length)
            self._body_consumed = True
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(400, "body must be JSON") from exc
        if not isinstance(payload, dict):
            raise ApiError(400, "body must be a JSON object")
        return payload

    def _send_json(self, status: int, payload: Mapping[str, Any]) -> None:
        self._send(status, JSON_TYPE, json.dumps(payload, default=str).encode("utf-8"))

    def _discard_body(self) -> None:
        if self.command != "POST" or getattr(self, "_body_consumed", False):
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if 0 < length <= MAX_BODY_BYTES and not self.headers.get("Transfer-Encoding"):
                self.connection.settimeout(REJECT_BODY_TIMEOUT_SECONDS)
                self.rfile.read(length)
            self._body_consumed = True
        except (OSError, ValueError):
            pass

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self._discard_body()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # Never share the port. On Windows SO_REUSEADDR lets a second process bind an address that
    # is already being listened on, and connections go to whoever bound first. A local process
    # squatting the dashboard port would then be handed the token that serve() puts in the
    # browser URL. Refusing to share turns that into a bind error the CLI already reports.
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], api: Api) -> None:
        super().__init__(address, _Handler)
        self.api = api


def _access_url(port: int, token: str) -> str:
    return f"http://{HOST}:{port}/#{TOKEN_PARAM}={token}"


def find_running(settings: Settings) -> str | None:
    path = settings.data_dir / INSTANCE_FILE
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        port, token = record["port"], record["token"]
        if (
            isinstance(port, bool)
            or not isinstance(port, int)
            or not 0 < port < 65536
            or not isinstance(token, str)
            or record.get("database") != _database_id(settings)
        ):
            return None
        with httpx.Client(timeout=PROBE_TIMEOUT_SECONDS, trust_env=False) as client:
            response = client.get(f"http://{HOST}:{port}/api/health", headers={TOKEN_HEADER: token})
        if response.status_code == 200 and response.json() == {
            "service": SERVICE_NAME,
            "database": _database_id(settings),
            "revision": CODE_REVISION,
        }:
            return _access_url(port, token)
    except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError):
        pass
    return None


def start(
    settings: Settings,
    *,
    port: int = DEFAULT_PORT,
    token: str | None = None,
    runs: ApplyRuns | None = None,
) -> tuple:
    """Bind a background server and return (server, url). The caller closes the server.

    `runs` is injected by the tests so an Apply click never opens a real browser.
    """
    api = Api(settings, token=token, runs=runs)
    server = _Server((HOST, port), api)
    actual_port = server.server_address[1]
    record = {
        "port": actual_port,
        "token": api.token,
        "database": _database_id(settings),
        "started_at": now_iso(),
    }
    try:
        dashboard._write_atomic(settings.data_dir / INSTANCE_FILE, json.dumps(record))
    except OSError:
        server.server_close()
        raise
    thread = threading.Thread(target=server.serve_forever, name="jobhunt-dashboard", daemon=True)
    thread.start()
    log.info("dashboard serving on %s:%s", HOST, actual_port)
    return server, _access_url(actual_port, api.token)


def serve(
    settings: Settings,
    *,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
    announce: Callable[[str], None] | None = None,
) -> str:
    """Open the matching local server, or serve until interrupted if it is not running."""
    url = find_running(settings)
    server = None
    if url is None:
        server, url = start(settings, port=port)
    if announce:
        announce(f"Dashboard {'started' if server else 'reused'}: {url.split('#', 1)[0]}")
    if open_browser:
        webbrowser.open(url)
    if server is None:
        return url
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        log.info("dashboard stopped")
    finally:
        server.shutdown()
        server.server_close()
        # The record holds the live token, so it must not outlive the server that issued it.
        (settings.data_dir / INSTANCE_FILE).unlink(missing_ok=True)
    return url
