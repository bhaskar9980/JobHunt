"""Typer CLI. Thin handlers only; logic lives in pipeline/repo modules."""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import date, timedelta
from typing import Annotated

import typer

from jobhunt import cli_ops, dashboard, pipeline, review, scoring
from jobhunt.apply import browser, tailor
from jobhunt.apply.answers import AnswerBank, AnswerBankError
from jobhunt.apply.detect import AtsRegistry
from jobhunt.apply.fillers.base import AnswerSet
from jobhunt.apply.runner import (
    ANSWERS_FILE,
    COVER_LETTER_FILE,
    COVER_LETTER_PDF,
    EXIT_SAFETY,
    Runner,
)
from jobhunt.apply.safety import SafetyGate
from jobhunt.cli_common import EXIT_INVALID, Ctx
from jobhunt.cli_common import build_ctx as _ctx
from jobhunt.cli_common import echo_json as _echo_json
from jobhunt.cli_mail import mail_app
from jobhunt.cli_refer import outreach_app, refer_app
from jobhunt.clock import now, now_iso, to_iso
from jobhunt.config import (
    Settings,
    build_normalizer,
    load_profile,
    load_skill_aliases,
    source_priority,
)
from jobhunt.db import transaction
from jobhunt.match import apply_prefilter, title_predicate
from jobhunt.models import APPLICATION_STAGES, EventType, Status
from jobhunt.referral.enrich import build_enricher
from jobhunt.repo import applications as applications_repo
from jobhunt.repo import events as events_repo
from jobhunt.repo import jobs as jobs_repo
from jobhunt.repo import stats as stats_repo
from jobhunt.sources import gmail_alerts

log = logging.getLogger(__name__)

app = typer.Typer(
    no_args_is_help=True,
    help="JobHunt: discovery, tracking and approve-then-submit applications.",
    add_completion=False,
)

_SINCE = re.compile(r"^(\d+)([hd])$")
EXIT_TAILOR_ISSUES = 1
EXIT_SCAN_FAILED = 1
ANSWERS_FILE_NAME = "answers.yaml"
ANSWER_SHEET_FILE = "answer_sheet.md"

answers_app = typer.Typer(no_args_is_help=True, help="Answer bank: learn answers, print sheets.")
tailor_app = typer.Typer(no_args_is_help=True, help="Cover-letter and resume-facts guards.")
app.add_typer(answers_app, name="answers")
app.add_typer(tailor_app, name="tailor")
app.add_typer(mail_app, name="mail")
app.add_typer(refer_app, name="refer")
app.add_typer(outreach_app, name="outreach")
app.command("plan")(cli_ops.plan_cmd)
app.command("serve")(cli_ops.serve_cmd)
app.command("dashboard-progress")(cli_ops.progress_cmd)
app.command("doctor")(cli_ops.doctor_cmd)
app.command("backup")(cli_ops.backup_cmd)
app.command("firecrawl")(cli_ops.firecrawl_cmd)


def _since_iso(value: str | None) -> str | None:
    if not value:
        return None
    match = _SINCE.match(value.strip().lower())
    if not match:
        raise typer.BadParameter("use forms like 24h or 3d")
    amount, unit = int(match.group(1)), match.group(2)
    delta = timedelta(hours=amount) if unit == "h" else timedelta(days=amount)
    return to_iso(now() - delta)


def _bank(ctx: Ctx) -> AnswerBank:
    try:
        return AnswerBank.load(ctx.settings.config_file(ANSWERS_FILE_NAME))
    except AnswerBankError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(EXIT_INVALID) from exc


def _runner(ctx: Ctx, headless: bool) -> Runner:
    settings = ctx.settings
    return Runner(
        conn=ctx.conn,
        settings=settings,
        bank=_bank(ctx),
        registry=AtsRegistry.load(settings.config_dir),
        page_factory=lambda: browser.open_page(settings, headless=headless),
        screenshot=browser.screenshot,
    )


def runner_for_server(conn: sqlite3.Connection, settings: Settings) -> Runner:
    """A Runner for the dashboard's review-only fill button, on its worker's connection.

    Always headed: filling can stop for a sign-in or a CAPTCHA, and the user reviews and
    submits in that browser window himself.
    """
    return Runner(
        conn=conn,
        settings=settings,
        bank=AnswerBank.load(settings.config_file(ANSWERS_FILE_NAME)),
        registry=AtsRegistry.load(settings.config_dir),
        page_factory=lambda: browser.open_page(settings, headless=False),
        screenshot=browser.screenshot,
    )


def _job_or_exit(ctx: Ctx, job_id: int) -> sqlite3.Row:
    try:
        return jobs_repo.get_job(ctx.conn, job_id)
    except jobs_repo.JobNotFound as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(EXIT_INVALID) from exc


def _table(rows: list[sqlite3.Row]) -> None:
    if not rows:
        typer.echo("(no jobs)")
        return
    typer.echo(f"{'id':>5}  {'score':>5}  {'status':<12} {'company':<22} {'title':<40} location")
    for row in rows:
        score = row["score"] if row["score"] is not None else "-"
        typer.echo(
            f"{row['id']:>5}  {score!s:>5}  {row['pipeline_status']:<12} "
            f"{(row['company'] or '')[:22]:<22} {(row['title'] or '')[:40]:<40} "
            f"{(row['location'] or '')[:30]}"
        )


# --------------------------------------------------------------------------- commands


@app.command("init-db")
def init_db_cmd() -> None:
    """Create or migrate the SQLite database."""
    ctx = _ctx()
    typer.echo(f"database ready at {ctx.settings.db_path}")


@app.command("scan")
def scan_cmd(
    source: Annotated[
        str | None, typer.Option("--source", help="greenhouse, lever, hn ...")
    ] = None,
    company: Annotated[str | None, typer.Option("--company")] = None,
    no_hydrate: Annotated[bool, typer.Option("--no-hydrate")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Fetch every configured source, dedupe into the tracker, hydrate plausible descriptions."""
    ctx = _ctx(verbose=verbose)
    profile = load_profile(ctx.settings)
    summary = pipeline.scan(
        ctx.conn,
        ctx.settings,
        only_source=source,
        only_company=company,
        hydrate=not no_hydrate,
        dry_run=dry_run,
        should_hydrate=title_predicate(profile),
    )
    for stats in summary.sources:
        line = (
            f"{stats.label:<32} fetched={stats.fetched:<4} created={stats.created:<4} "
            f"merged={stats.merged:<4} refreshed={stats.refreshed:<4} old={stats.skipped_old}"
        )
        typer.echo(line + (f"  ERROR {stats.error}" if stats.error else ""))
    typer.echo(
        f"scan done: created={summary.created} merged={summary.merged} "
        f"hydrated={summary.hydrated} errors={len(summary.errors)}"
    )
    if summary.errors:
        raise typer.Exit(EXIT_SCAN_FAILED)


@app.command("prefilter")
def prefilter_cmd(
    rescan: Annotated[bool, typer.Option("--rescan", help="re-evaluate filtered jobs too")] = False,
) -> None:
    """Apply the deterministic profile rules; failures move to 'filtered'."""
    ctx = _ctx()
    profile = load_profile(ctx.settings)
    aliases = load_skill_aliases(ctx.settings)
    summary = apply_prefilter(ctx.conn, profile, aliases, rescan=rescan)
    typer.echo(
        f"prefilter: evaluated={summary.evaluated} passed={summary.passed} "
        f"filtered={summary.filtered} restored={summary.restored}"
    )
    for reason, count in sorted(summary.reasons.items(), key=lambda kv: -kv[1]):
        typer.echo(f"  {count:>4}  {reason}")


@app.command("score-batch")
def score_batch_cmd(
    export: Annotated[str | None, typer.Option("--export", help="write unscored jobs here")] = None,
    import_path: Annotated[str | None, typer.Option("--import", help="read scored results")] = None,
    limit: Annotated[int | None, typer.Option("--limit")] = None,
) -> None:
    """Export a batch for the LLM scorer, or import its results."""
    if bool(export) == bool(import_path):
        raise typer.BadParameter("pass exactly one of --export or --import")
    ctx = _ctx()
    profile = load_profile(ctx.settings)
    if export:
        count = scoring.export_batch(
            ctx.conn, profile, ctx.settings.resolve(export), limit or profile.scoring.batch_size
        )
        typer.echo(f"exported {count} jobs to {export}")
        return
    summary = scoring.import_batch(ctx.conn, ctx.settings.resolve(str(import_path)), profile)
    typer.echo(f"imported {summary.imported} scores, shortlisted {summary.shortlisted}")
    for error in summary.errors:
        typer.echo(f"  skipped: {error}", err=True)
    if summary.errors:
        raise typer.Exit(EXIT_INVALID)


@app.command("ingest-inbox")
def ingest_inbox_cmd(
    directory: Annotated[
        str | None, typer.Option("--dir", help="defaults to paths.inbox_dir")
    ] = None,
) -> None:
    """Load alert-email JSON files written by /jobs-scan into the tracker."""
    ctx = _ctx()
    inbox_dir = ctx.settings.resolve(directory) if directory else ctx.settings.path("inbox_dir")
    summary = gmail_alerts.ingest_inbox(
        ctx.conn, inbox_dir, build_normalizer(ctx.settings), source_priority(ctx.settings)
    )
    typer.echo(
        f"inbox: files={summary.files} emails={summary.emails} jobs={summary.jobs} "
        f"created={summary.created} merged={summary.merged} skipped={summary.skipped_emails}"
    )
    for failure in summary.failed:
        typer.echo(f"  failed: {failure}", err=True)
    if summary.failed:
        raise typer.Exit(EXIT_INVALID)


@app.command("dashboard")
def dashboard_cmd(
    open_after: Annotated[bool, typer.Option("--open", help="open in the default browser")] = False,
) -> None:
    """Render data/dashboard.html (self-contained; also publishable as an Artifact)."""
    ctx = _ctx()
    path = dashboard.render(ctx.conn, ctx.settings)
    typer.echo(f"dashboard written to {path}")
    if open_after:
        typer.launch(str(path))


@app.command("queue")
def queue_cmd(
    status: Annotated[list[str] | None, typer.Option("--status", "-s")] = None,
    limit: Annotated[int, typer.Option("--limit")] = review.DEFAULT_LIMIT,
    fmt: Annotated[str, typer.Option("--format", help="json | md")] = "json",
    out: Annotated[str | None, typer.Option("--out", help="write the md checklist here")] = None,
) -> None:
    """Jobs awaiting a decision (default: shortlisted), richest score first, snoozed hidden."""
    ctx = _ctx()
    items = review.queue(
        ctx.conn,
        status or [Status.SHORTLISTED.value],
        limit,
        enrich=build_enricher(ctx.conn, ctx.settings),
    )
    if fmt == "md":
        text = review.render_markdown(items)
        if out:
            target = ctx.settings.resolve(out)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            typer.echo(f"wrote {len(items)} items to {target}")
        else:
            typer.echo(text)
        return
    _echo_json(items)


@app.command("decide")
def decide_cmd(
    job_id: Annotated[int | None, typer.Argument()] = None,
    action: Annotated[str | None, typer.Argument(help="approve | skip | snooze")] = None,
    until: Annotated[str | None, typer.Option("--until", help="YYYY-MM-DD for snooze")] = None,
    from_file: Annotated[
        str | None, typer.Option("--from-file", help="ticked review_queue.md")
    ] = None,
) -> None:
    """Record approve/skip/snooze for one job, or for every ticked line in a checklist file."""
    ctx = _ctx()
    decisions: list[review.Decision] = []
    if from_file:
        decisions = review.parse_markdown(
            ctx.settings.resolve(from_file).read_text(encoding="utf-8")
        )
    elif job_id is not None and action:
        parsed_until = date.fromisoformat(until) if until else None
        decisions = [review.Decision(job_id, action.lower(), parsed_until)]
    else:
        raise typer.BadParameter("pass JOB_ID ACTION, or --from-file PATH")
    for decision in decisions:
        try:
            result = review.decide(ctx.conn, decision)
        except (ValueError, jobs_repo.JobNotFound) as exc:
            typer.echo(f"job {decision.job_id}: {exc}", err=True)
            continue
        typer.echo(f"job {decision.job_id}: {decision.action} -> {result}")


@app.command("prefill")
def prefill_cmd(
    job_id: int,
    headless: Annotated[bool, typer.Option("--headless")] = False,
) -> None:
    """Open the form, extract its schema, resolve answers from the bank. Exit code = runner code."""
    ctx = _ctx()
    _job_or_exit(ctx, job_id)
    result = _runner(ctx, headless).prefill(job_id)
    typer.echo(f"job {job_id}: {result.message}")
    for name, path in result.artefacts.items():
        typer.echo(f"  {name}: {path}")
    raise typer.Exit(result.code)


@app.command("apply")
def apply_cmd(
    job_id: int,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="fill and screenshot, never submit")
    ] = False,
    headless: Annotated[bool, typer.Option("--headless")] = False,
) -> None:
    """Submit an approved, prefilled job through its Python filler. Exit code = runner code."""
    ctx = _ctx()
    _job_or_exit(ctx, job_id)
    env_dry = (ctx.settings.raw.get("apply") or {}).get("dry_run_default", False)
    result = _runner(ctx, headless).apply(job_id, dry_run=dry_run or bool(env_dry))
    typer.echo(f"job {job_id}: [{result.code}] {result.message}")
    for name, path in result.artefacts.items():
        typer.echo(f"  {name}: {path}")
    raise typer.Exit(result.code)


@app.command("reserve-application")
def reserve_application_cmd(job_id: int) -> None:
    """Reserve a supervised assisted/manual attempt immediately before its submit click."""
    ctx = _ctx()
    row = _job_or_exit(ctx, job_id)
    registry = AtsRegistry.load(ctx.settings.config_dir)
    ats = registry.detect(row["apply_url"] or row["url"], fallback=row["ats_type"])
    issues = _bank(ctx).placeholder_issues()
    verdict = SafetyGate.from_settings(ctx.conn, ctx.settings.section("apply")).check(row, ats)
    if issues or not verdict.ok:
        typer.echo("; ".join([*issues, *verdict.reasons]), err=True)
        raise typer.Exit(EXIT_SAFETY)
    verdict = SafetyGate.from_settings(ctx.conn, ctx.settings.section("apply")).reserve(job_id, ats)
    if not verdict.ok:
        typer.echo("; ".join(verdict.reasons), err=True)
        raise typer.Exit(EXIT_SAFETY)
    typer.echo(f"job {job_id}: attempt reserved; record confirmation or explicitly reconcile")


@app.command("reconcile-submission")
def reconcile_submission_cmd(
    job_id: int,
    not_submitted: Annotated[
        bool,
        typer.Option("--not-submitted", help="You verified that no application was submitted."),
    ] = False,
    note: Annotated[
        str, typer.Option("--note", help="What you checked to verify the outcome.")
    ] = "",
) -> None:
    if not not_submitted or not note.strip():
        raise typer.BadParameter("verify the outcome, then pass --not-submitted and --note")
    ctx = _ctx()
    _job_or_exit(ctx, job_id)
    try:
        with transaction(ctx.conn):
            applications_repo.resolve_not_submitted(ctx.conn, job_id, note)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(EXIT_INVALID) from exc
    typer.echo(f"job {job_id}: verified not submitted; run prefill before a new attempt")


@app.command("mark-applied")
def mark_applied_cmd(
    job_id: int,
    ref: Annotated[str | None, typer.Option("--ref", help="confirmation text or URL")] = None,
    screenshot: Annotated[str | None, typer.Option("--screenshot")] = None,
    via: Annotated[str, typer.Option("--via", help="assisted | manual | easy_apply")] = "assisted",
) -> None:
    """Record an application submitted outside the Python fillers (Workday, Naukri, manual)."""
    ctx = _ctx()
    _job_or_exit(ctx, job_id)
    stamp = now_iso()
    with transaction(ctx.conn):
        current = jobs_repo.current_status(ctx.conn, job_id)
        previous = applications_repo.get_for_job(ctx.conn, job_id)
        if current not in APPLICATION_STAGES:
            jobs_repo.set_job_status(
                ctx.conn, job_id, Status.APPLIED, f"submitted via {via}", force=True
            )
        applications_repo.record_submission(
            ctx.conn, job_id, submitted_at=stamp, submitted_via=via, confirmation_ref=ref
        )
        if previous is None or previous["submitted_at"] is None:
            events_repo.add_event(
                ctx.conn,
                job_id,
                EventType.SUBMITTED,
                f"{via}: {ref or ''} {screenshot or ''}".strip(),
                source="skill",
            )
    typer.echo(
        f"job {job_id}: application evidence recorded; "
        f"current stage {jobs_repo.current_status(ctx.conn, job_id)}"
    )


@answers_app.command("learn")
def answers_learn_cmd(
    job: Annotated[int, typer.Option("--job")],
    question: Annotated[str, typer.Option("--question")],
    answer: Annotated[str, typer.Option("--answer")],
) -> None:
    """Persist a new answer in config/answers.yaml and re-resolve that job's answers.json."""
    ctx = _ctx()
    _job_or_exit(ctx, job)
    bank = _bank(ctx)
    key = bank.learn(question, answer, job_id=job)
    bank.save()
    result = _runner(ctx, headless=True).re_resolve(job)
    typer.echo(f"learned {key!r}; job {job}: {result.message}")


@answers_app.command("sheet")
def answers_sheet_cmd(job_id: int) -> None:
    """Print the assisted-mode cheat-sheet (profile + resolved answers) for a job."""
    ctx = _ctx()
    row = _job_or_exit(ctx, job_id)
    runner = _runner(ctx, headless=True)
    answers_path = runner.job_dir(job_id) / ANSWERS_FILE
    if not answers_path.exists():
        runner.prefill(job_id)
    answer_set = AnswerSet.load(answers_path)
    sheet = tailor.render_answer_sheet(dict(row), answer_set, runner.bank.get("profile") or {})
    (runner.job_dir(job_id) / ANSWER_SHEET_FILE).write_text(sheet, encoding="utf-8")
    typer.echo(sheet)


@tailor_app.command("check")
def tailor_check_cmd(job_id: int) -> None:
    """Validate data/applications/<id>/cover_letter.md against the resume rulebook."""
    ctx = _ctx()
    row = _job_or_exit(ctx, job_id)
    letter_path = ctx.settings.path("applications_dir") / str(job_id) / COVER_LETTER_FILE
    if not letter_path.exists():
        typer.echo(f"no cover letter at {letter_path}", err=True)
        raise typer.Exit(EXIT_INVALID)
    facts_path = ctx.settings.path("resume_facts")
    facts = facts_path.read_text(encoding="utf-8") if facts_path.exists() else ""
    if not facts:
        typer.echo("resume facts missing; run `uv run jobhunt facts` first", err=True)
    issues = tailor.validate_cover_letter(
        letter_path.read_text(encoding="utf-8"),
        facts,
        tailor.load_tailor_config(ctx.settings.config_dir),
        job_description=row["description_md"],
    )
    if not issues:
        typer.echo("cover letter passes")
        return
    for issue in issues:
        typer.echo(f"  - {issue}")
    raise typer.Exit(EXIT_TAILOR_ISSUES)


@tailor_app.command("pdf")
def tailor_pdf_cmd(job_id: int) -> None:
    """Render cover_letter.md to a one-page cover_letter.pdf (Greenhouse wants a file)."""
    ctx = _ctx()
    _job_or_exit(ctx, job_id)
    job_dir = ctx.settings.path("applications_dir") / str(job_id)
    letter_path = job_dir / COVER_LETTER_FILE
    if not letter_path.exists():
        typer.echo(f"no cover letter at {letter_path}", err=True)
        raise typer.Exit(EXIT_INVALID)
    cfg = tailor.load_tailor_config(ctx.settings.config_dir)
    profile = _bank(ctx).get("profile") or {}
    try:
        out = tailor.render_cover_letter_pdf(
            letter_path,
            job_dir / COVER_LETTER_PDF,
            profile,
            cfg,
            pdflatex=tailor.resolve_pdflatex(cfg),
        )
    except tailor.CoverLetterError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(EXIT_TAILOR_ISSUES) from exc
    typer.echo(f"cover letter PDF written to {out}")


@app.command("facts")
def facts_cmd() -> None:
    """Regenerate data/resume_facts.md from the resume .tex and Accomplishments.md."""
    ctx = _ctx()
    cfg = tailor.load_tailor_config(ctx.settings.config_dir)
    accomplishments = ctx.settings.path("accomplishments_md")
    text = tailor.extract_resume_facts(
        ctx.settings.path("resume_tex"),
        accomplishments if accomplishments.exists() else None,
        cfg,
    )
    out = ctx.settings.path("resume_facts")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    typer.echo(f"resume facts written to {out}")


@app.command("list")
def list_cmd(
    status: Annotated[list[str] | None, typer.Option("--status", "-s")] = None,
    min_score: Annotated[int | None, typer.Option("--min-score")] = None,
    since: Annotated[str | None, typer.Option("--since", help="e.g. 24h or 3d")] = None,
    company: Annotated[str | None, typer.Option("--company")] = None,
    limit: Annotated[int | None, typer.Option("--limit")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List jobs, filtered by pipeline status, score, recency or company."""
    ctx = _ctx()
    rows = jobs_repo.list_jobs(
        ctx.conn,
        statuses=status,
        min_score=min_score,
        since_iso=_since_iso(since),
        company=company,
        limit=limit,
    )
    if as_json:
        _echo_json([jobs_repo.job_to_dict(ctx.conn, r) for r in rows])
        return
    _table(rows)


@app.command("show")
def show_cmd(
    job_id: int,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show one job with its sources, application and event history."""
    ctx = _ctx()
    try:
        row = jobs_repo.get_job(ctx.conn, job_id)
    except jobs_repo.JobNotFound as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(EXIT_INVALID) from exc
    data = jobs_repo.job_to_dict(ctx.conn, row)
    data["events"] = [dict(e) for e in events_repo.events_for_job(ctx.conn, job_id)]
    if as_json:
        _echo_json(data)
        return
    typer.echo(f"[{data['id']}] {data['company']}: {data['title']}  ({data['pipeline_status']})")
    typer.echo(
        f"  location: {data['location']}  remote: {data['is_remote']}  score: {data['score']}"
    )
    typer.echo(f"  url: {data['url']}")
    if data.get("apply_url"):
        typer.echo(f"  apply: {data['apply_url']}")
    for src in data["sources"]:
        typer.echo(f"  source: {src['source']} #{src['source_job_id']}")
    for event in data["events"]:
        detail = f"{event['from_status']} -> {event['to_status']}" if event["to_status"] else ""
        typer.echo(f"  {event['at']}  {event['event_type']:<16} {detail} {event['note'] or ''}")


@app.command("set-status")
def set_status_cmd(
    job_id: int,
    status: str,
    note: Annotated[str | None, typer.Option("--note")] = None,
    force: Annotated[bool, typer.Option("--force")] = False,
) -> None:
    """Move a job to a new pipeline status (validated unless --force)."""
    ctx = _ctx()
    try:
        result = jobs_repo.set_job_status(ctx.conn, job_id, Status(status), note, force=force)
    except (ValueError, jobs_repo.JobNotFound) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(EXIT_INVALID) from exc
    typer.echo(f"job {job_id} -> {result}")


@app.command("note")
def note_cmd(job_id: int, text: str) -> None:
    """Attach a free-text note to a job."""
    ctx = _ctx()
    try:
        jobs_repo.add_note(ctx.conn, job_id, text)
    except jobs_repo.JobNotFound as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(EXIT_INVALID) from exc
    typer.echo("noted")


@app.command("stats")
def stats_cmd(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Funnel counts, discovered today, response rate."""
    ctx = _ctx()
    since_today = to_iso(now() - timedelta(days=1))
    payload = {
        "funnel": stats_repo.funnel_counts(ctx.conn),
        "discovered_24h": stats_repo.discovered_since(ctx.conn, since_today),
        "response_rate": stats_repo.response_rate(ctx.conn),
    }
    if as_json:
        _echo_json(payload)
        return
    for status, count in payload["funnel"].items():
        if count:
            typer.echo(f"{status:<12} {count}")
    typer.echo(f"discovered in last 24h: {payload['discovered_24h']}")
    typer.echo(f"response rate: {payload['response_rate']}")


@app.command("follow-ups")
def follow_ups_cmd(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Applications with no activity past the follow-up window."""
    ctx = _ctx()
    days = int(ctx.settings.section("follow_up").get("days_after_apply", 10))
    rows = stats_repo.follow_ups_due(ctx.conn, days)
    if as_json:
        _echo_json([dict(r) for r in rows])
        return
    if not rows:
        typer.echo("no follow-ups due")
        return
    for row in rows:
        typer.echo(
            f"[{row['job_id']}] {row['company']}: {row['title']}  last {row['last_activity_at']}"
        )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
