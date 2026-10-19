"""Operational commands: plan (the morning view), doctor (setup checks), backup."""

from __future__ import annotations

import logging
import shutil
import sqlite3
import zipfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any

import typer

from jobhunt import dashboard, server, web_discovery
from jobhunt import plan as plan_mod
from jobhunt.apply import tailor
from jobhunt.apply.answers import AnswerBank, AnswerBankError
from jobhunt.apply.detect import AtsRegistry
from jobhunt.cli_common import EXIT_INVALID, Ctx, build_ctx, echo_json
from jobhunt.clock import now, to_iso
from jobhunt.config import (
    ConfigError,
    Settings,
    load_companies,
    load_profile,
    load_sources,
    load_yaml,
)
from jobhunt.mail import ingest as mail_ingest
from jobhunt.referral.enrich import build_enricher
from jobhunt.referral.rules import ReferralConfig

log = logging.getLogger(__name__)

EXIT_DOCTOR_FAIL = 1
ANSWERS_FILE_NAME = "answers.yaml"
MAIL_RULES_FILE = "mail_rules.yaml"
DEFAULT_BACKUP_DIR = "backups"
STALE_SCAN_DAYS = 2
CHROME_PATHS = (
    Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
    Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
)
PLAYWRIGHT_MCP_CLI = Path("node_modules/@playwright/mcp/cli.js")
BACKUP_FILES = ("resume_facts",)
BACKUP_DIRS = ("applications_dir", "outreach_dir")
SEVERITY_FAIL = "fail"
SEVERITY_WARN = "warn"


def firecrawl_cmd(
    action: Annotated[
        str, typer.Argument(help="status, plan, complete, fail, or leads")
    ] = "status",
    request_id: Annotated[str | None, typer.Argument()] = None,
    response_file: Annotated[Path | None, typer.Option("--response-file")] = None,
) -> None:
    ctx = build_ctx()
    actions = {
        "status": lambda: web_discovery.budget_status(ctx.conn, ctx.settings),
        "plan": lambda: web_discovery.plan_request(ctx.conn, ctx.settings),
        "complete": lambda: web_discovery.complete_request(
            ctx.conn, ctx.settings, request_id or "", response_file=response_file
        ),
        "fail": lambda: web_discovery.fail_request(ctx.conn, ctx.settings, request_id or ""),
        "leads": lambda: web_discovery.review_leads(ctx.conn),
    }
    if action not in actions or (action in {"complete", "fail"} and not request_id):
        raise typer.BadParameter(
            "use status, plan, leads, complete <request_id>, or fail <request_id>"
        )
    try:
        echo_json(actions[action]())
    except (OSError, ValueError, ConfigError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(EXIT_INVALID) from exc


@dataclass(slots=True)
class Check:
    name: str
    ok: bool
    detail: str
    severity: str = SEVERITY_FAIL

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail, "severity": self.severity}


# --------------------------------------------------------------------------- plan


def plan_cmd(
    as_json: Annotated[bool, typer.Option("--json")] = False,
    no_referrals: Annotated[bool, typer.Option("--no-referrals")] = False,
) -> None:
    """Everything for today: ready to apply, referral recommended, needs input, progress."""
    ctx = build_ctx()
    enrich = None if no_referrals else build_enricher(ctx.conn, ctx.settings)
    payload = plan_mod.build_plan(ctx.conn, ctx.settings, enrich)
    if as_json:
        echo_json(payload)
        return
    typer.echo(plan_mod.render_markdown(payload))


# --------------------------------------------------------------------------- serve


def serve_cmd(
    port: Annotated[
        int | None, typer.Option("--port", min=0, max=65535, help="0 picks a free port")
    ] = None,
    no_open: Annotated[bool, typer.Option("--no-open", help="do not open a browser")] = False,
) -> None:
    """Open or reuse the private local dashboard. Ctrl+C stops a newly started server."""
    ctx = build_ctx()
    ctx.conn.close()
    chosen = (
        port
        if port is not None
        else int(ctx.settings.section("dashboard").get("port", server.DEFAULT_PORT))
    )
    try:
        server.serve(ctx.settings, port=chosen, open_browser=not no_open, announce=typer.echo)
    except OSError as exc:
        typer.echo(f"Dashboard could not start: {exc}. Try --port 0 for a free port.", err=True)
        raise typer.Exit(EXIT_DOCTOR_FAIL) from exc


def progress_cmd(
    stage: str,
    state: Annotated[str, typer.Option("--state")] = "running",
    message: Annotated[str, typer.Option("--message")] = "",
    new_run: Annotated[bool, typer.Option("--new-run")] = False,
) -> None:
    ctx = build_ctx()
    ctx.conn.close()
    try:
        dashboard.update_workflow(ctx.settings, stage, state, message=message, new_run=new_run)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Dashboard progress: {stage} {state}")


# --------------------------------------------------------------------------- doctor


def doctor_cmd(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Check config, resume, answer bank, tools and data health. Exit 1 on any hard failure."""
    ctx = build_ctx()
    checks = run_checks(ctx)
    if as_json:
        echo_json([c.to_dict() for c in checks])
    else:
        for check in checks:
            mark = "ok  " if check.ok else ("WARN" if check.severity == SEVERITY_WARN else "FAIL")
            typer.echo(f"{mark} {check.name:<22} {check.detail}")
    if any(not c.ok and c.severity == SEVERITY_FAIL for c in checks):
        raise typer.Exit(EXIT_DOCTOR_FAIL)


def run_checks(ctx: Ctx) -> list[Check]:
    settings = ctx.settings
    checks = _config_checks(settings)
    checks += _path_checks(settings)
    checks += _answer_bank_checks(settings)
    checks += _tool_checks(settings)
    checks += _data_checks(ctx.conn)
    checks += _invariant_checks(ctx.conn, settings)
    return checks


def _config_checks(settings) -> list[Check]:
    loaders = {
        "profile.yaml": lambda: load_profile(settings),
        "companies.yaml": lambda: load_companies(settings),
        "sources.yaml": lambda: load_sources(settings),
        "referrals.yaml": lambda: ReferralConfig.load(settings.config_dir),
        "mail_rules.yaml": lambda: load_yaml(settings.config_file(MAIL_RULES_FILE)),
        "tailor.yaml": lambda: tailor.load_tailor_config(settings.config_dir),
        "ats/registry.yaml": lambda: AtsRegistry.load(settings.config_dir),
    }
    checks: list[Check] = []
    for name, loader in loaders.items():
        try:
            loader()
            checks.append(Check(name, True, "loads"))
        except (ConfigError, OSError, ValueError, KeyError) as exc:
            checks.append(Check(name, False, str(exc)))
    return checks


def _path_checks(settings) -> list[Check]:
    checks: list[Check] = []
    for key, severity in (
        ("resume_pdf", SEVERITY_FAIL),
        ("resume_tex", SEVERITY_WARN),
        ("accomplishments_md", SEVERITY_WARN),
        ("resume_facts", SEVERITY_WARN),
    ):
        path = settings.path(key)
        detail = str(path) if path.exists() else f"missing: {path}"
        if key == "resume_facts" and not path.exists():
            detail = "missing; run `uv run jobhunt facts`"
        checks.append(Check(key, path.exists(), detail, severity))
    return checks


def _answer_bank_checks(settings) -> list[Check]:
    path = settings.config_file(ANSWERS_FILE_NAME)
    try:
        bank = AnswerBank.load(path)
    except AnswerBankError as exc:
        return [Check("answers.yaml", False, str(exc))]
    issues = bank.placeholder_issues()
    if issues:
        return [Check("answers.yaml", False, "placeholders: " + "; ".join(issues), SEVERITY_WARN)]
    return [Check("answers.yaml", True, "filled in")]


def _tool_checks(settings) -> list[Check]:
    checks: list[Check] = []
    try:
        checks.append(
            Check(
                "pdflatex",
                True,
                tailor.resolve_pdflatex(tailor.load_tailor_config(settings.config_dir)),
                SEVERITY_WARN,
            )
        )
    except (tailor.CoverLetterError, ConfigError) as exc:
        checks.append(Check("pdflatex", False, str(exc), SEVERITY_WARN))
    chrome = next((p for p in CHROME_PATHS if p.exists()), None) or shutil.which("chrome")
    checks.append(
        Check(
            "chrome",
            chrome is not None,
            str(chrome or "not found (fillers need it)"),
            SEVERITY_WARN,
        )
    )
    mcp = settings.root / PLAYWRIGHT_MCP_CLI
    checks.append(
        Check(
            "playwright mcp",
            mcp.exists(),
            str(mcp) if mcp.exists() else "missing; run `npm install`",
            SEVERITY_WARN,
        )
    )
    return checks


def unscored_on_dashboard(conn: sqlite3.Connection, settings: Settings) -> int:
    """How many jobs the dashboard shows without a score. The daily rule is that this is zero."""
    rows = dashboard.collect(conn, settings)["rows"]
    return sum(1 for row in rows if row["score"] is None)


def _invariant_checks(conn: sqlite3.Connection, settings: Settings) -> list[Check]:
    """Rules a daily run must leave true, checked against live data rather than trusted."""
    unscored = unscored_on_dashboard(conn, settings)
    return [
        Check(
            "everything scored",
            unscored == 0,
            "every visible job has a score"
            if unscored == 0
            else f"{unscored} visible job(s) unscored; run score-batch --export until it reports 0",
        )
    ]


def _data_checks(conn: sqlite3.Connection) -> list[Check]:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    contacts = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
    last_scan = conn.execute("SELECT MAX(started_at) FROM scan_runs").fetchone()[0]
    failed = conn.execute(
        """
        SELECT COUNT(*) FROM scan_runs s
        WHERE error IS NOT NULL AND id = (
            SELECT MAX(id) FROM scan_runs
            WHERE source = s.source AND COALESCE(company, '') = COALESCE(s.company, '')
        )
        """
    ).fetchone()[0]
    stale_before = to_iso(now() - timedelta(days=STALE_SCAN_DAYS))
    last_sync = mail_ingest.last_sync(conn)
    return [
        Check("database", True, f"schema v{version}, {jobs} jobs"),
        Check(
            "contacts",
            contacts > 0,
            f"{contacts} imported" if contacts else "none; `refer import-connections <csv>`",
            SEVERITY_WARN,
        ),
        Check(
            "last scan",
            bool(last_scan) and str(last_scan) >= stale_before,
            str(last_scan or "never"),
            SEVERITY_WARN,
        ),
        Check(
            "sources failing",
            failed == 0,
            f"{failed} source(s) failed on their latest run" if failed else "none",
            SEVERITY_WARN,
        ),
        Check(
            "mail sync",
            last_sync is not None,
            to_iso(last_sync) if last_sync else "never; run /jobs-sync once interactively",
            SEVERITY_WARN,
        ),
    ]


# --------------------------------------------------------------------------- backup


def backup_cmd(
    to: Annotated[str | None, typer.Option("--to", help="target folder")] = None,
) -> None:
    """Copy the database and a zip of drafts, evidence and the answer bank to a backup folder."""
    ctx = build_ctx()
    paths = ctx.settings.section("paths")
    target = ctx.settings.resolve(to or str(paths.get("backup_dir", DEFAULT_BACKUP_DIR)))
    db_copy, archive = run_backup(ctx, Path(target))
    typer.echo(f"database -> {db_copy}")
    typer.echo(f"archive  -> {archive}")


def run_backup(ctx: Ctx, target: Path) -> tuple[Path, Path]:
    target.mkdir(parents=True, exist_ok=True)
    stamp = to_iso(now()).replace(":", "").replace("-", "")[:15]
    db_copy = target / f"jobs-{stamp}.db"
    with sqlite3.connect(db_copy) as dest:
        ctx.conn.backup(dest)
    archive = target / f"jobhunt-{stamp}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for key in BACKUP_DIRS:
            _add_tree(bundle, ctx.settings.path(key), ctx.settings.root)
        for key in BACKUP_FILES:
            _add_file(bundle, ctx.settings.path(key), ctx.settings.root)
        _add_file(bundle, ctx.settings.config_file(ANSWERS_FILE_NAME), ctx.settings.root)
    log.info("backup written to %s and %s", db_copy, archive)
    return db_copy, archive


def _add_tree(bundle: zipfile.ZipFile, folder: Path, root: Path) -> None:
    if not folder.exists():
        return
    for path in folder.rglob("*"):
        if path.is_file():
            _add_file(bundle, path, root)


def _add_file(bundle: zipfile.ZipFile, path: Path, root: Path) -> None:
    if not path.exists():
        return
    try:
        arcname = str(path.relative_to(root))
    except ValueError:
        arcname = path.name
    bundle.write(path, arcname)
