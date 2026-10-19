"""`jobhunt refer ...` and `jobhunt outreach ...`: referral suggestions, drafts, and the
open / mark-sent / mark-replied bookkeeping. The user sends every message himself."""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Mapping
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any

import typer

from jobhunt.apply.tailor import load_tailor_config
from jobhunt.cli_common import EXIT_INVALID, Ctx, build_ctx, echo_json, fail
from jobhunt.clock import now, now_iso, to_iso
from jobhunt.config import ConfigError, build_normalizer, load_company_aliases, load_profile
from jobhunt.db import transaction
from jobhunt.models import EventType
from jobhunt.referral import drafts, second_degree
from jobhunt.referral.importer import ConnectionsFormatError, import_connections
from jobhunt.referral.ranker import suggest
from jobhunt.referral.rules import ReferralConfig
from jobhunt.repo import contacts as contacts_repo
from jobhunt.repo import events as events_repo
from jobhunt.repo import jobs as jobs_repo
from jobhunt.repo import outreach as outreach_repo

log = logging.getLogger(__name__)

EXIT_DRAFT_ISSUES = 1
REFERRED_RESPONSE = "referred"
DECLINED_RESPONSE = "declined"
SOURCE = "cli"

refer_app = typer.Typer(
    no_args_is_help=True,
    help="Referrals: import LinkedIn connections, rank who to ask, draft the message.",
)
outreach_app = typer.Typer(
    no_args_is_help=True,
    help="Outreach bookkeeping: open a profile, mark sent or replied, list follow-ups due.",
)


# --------------------------------------------------------------------------- helpers


def _job(ctx: Ctx, job_id: int) -> sqlite3.Row:
    try:
        return jobs_repo.get_job(ctx.conn, job_id)
    except jobs_repo.JobNotFound as exc:
        raise fail(str(exc)) from exc


def _contact(ctx: Ctx, contact_id: int) -> sqlite3.Row:
    try:
        return contacts_repo.get_contact(ctx.conn, contact_id)
    except contacts_repo.ContactNotFound as exc:
        raise fail(str(exc)) from exc


def _outreach(ctx: Ctx, outreach_id: int) -> sqlite3.Row:
    try:
        return outreach_repo.get(ctx.conn, outreach_id)
    except outreach_repo.OutreachNotFound as exc:
        raise fail(str(exc)) from exc


def _config(ctx: Ctx) -> ReferralConfig:
    try:
        return ReferralConfig.load(ctx.settings.config_dir)
    except ConfigError as exc:
        raise fail(str(exc)) from exc


def _event(ctx: Ctx, job_id: int | None, event_type: EventType, note: str) -> None:
    if job_id is None:
        return
    with transaction(ctx.conn):
        events_repo.add_event(ctx.conn, job_id, event_type, note, source=SOURCE)


def _first_name(full_name: str) -> str:
    return full_name.split(maxsplit=1)[0] if full_name.split() else full_name


def _outreach_view(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def _print_outreach(rows: list[sqlite3.Row]) -> None:
    if not rows:
        typer.echo("(none)")
        return
    typer.echo(f"{'id':>4}  {'kind':<16} {'contact':<24} {'job':<30} sent / due")
    for row in rows:
        job_label = f"{row['job_company'] or ''} {row['job_title'] or ''}".strip() or "-"
        typer.echo(
            f"{row['id']:>4}  {row['kind']:<16} {row['contact_name'][:24]:<24} "
            f"{job_label[:30]:<30} {row['sent_at'] or '-'} / {row['followup_due'] or '-'}"
        )


# --------------------------------------------------------------------------- refer


@refer_app.command("import-connections")
def import_connections_cmd(
    csv_path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Import the LinkedIn Connections.csv export (Settings > Data privacy > Get a copy)."""
    ctx = build_ctx()
    norm = build_normalizer(ctx.settings)
    aliases = load_company_aliases(ctx.settings)
    try:
        summary = import_connections(ctx.conn, csv_path, norm, aliases)
    except ConnectionsFormatError as exc:
        raise fail(str(exc)) from exc
    if as_json:
        echo_json(asdict(summary))
        return
    typer.echo(
        f"{summary.rows} rows: {summary.created} new contacts, {summary.updated} updated, "
        f"{summary.skipped_no_name} skipped (no name)"
    )


@refer_app.command("suggest")
def suggest_cmd(
    job_id: int,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Rank contacts at the job's company by how sensible it is to ask them for a referral."""
    ctx = build_ctx()
    job = _job(ctx, job_id)
    cfg = _config(ctx)
    suggestions = suggest(
        ctx.conn,
        job,
        cfg,
        build_normalizer(ctx.settings),
        load_company_aliases(ctx.settings),
        now=now(),
    )
    if as_json:
        echo_json({"job_id": job_id, "suggestions": [s.to_dict() for s in suggestions]})
        return
    if not suggestions:
        typer.echo(f"no contacts at {job['company']}; try `refer people-search {job_id}`")
        return
    typer.echo(f"Contacts at {job['company']} for #{job_id} {job['title']}:")
    for item in suggestions:
        typer.echo(
            f"  [{item.contact_id}] {item.score:>4}  {item.name} ({item.title or 'no title'}) "
            f"- {'; '.join(item.reasons)}"
        )


@refer_app.command("draft")
def draft_cmd(
    job_id: int,
    contact_id: int,
    template: Annotated[str, typer.Option("--template", "-t")],
    hook: Annotated[
        str, typer.Option("--hook", help="One concrete line about them or the company.")
    ],
    one_liner: Annotated[str | None, typer.Option("--one-liner")] = None,
    force: Annotated[bool, typer.Option("--force", help="Write even if checks fail.")] = False,
) -> None:
    """Render a template for one contact, check it, write data/outreach/<job>/<contact>.md."""
    ctx = build_ctx()
    job, contact, cfg = _job(ctx, job_id), _contact(ctx, contact_id), _config(ctx)
    try:
        tpl = drafts.load_template(ctx.settings.config_dir, template)
    except ConfigError as exc:
        raise fail(f"{exc}; available: {drafts.list_templates(ctx.settings.config_dir)}") from exc
    try:
        candidate = load_profile(ctx.settings).candidate
    except ConfigError as exc:
        raise fail(str(exc)) from exc
    text = drafts.render(tpl, _draft_context(job, contact, cfg, hook, one_liner, candidate))
    issues = drafts.check_draft(text, tpl, load_tailor_config(ctx.settings.config_dir))
    for issue in issues:
        typer.echo(f"issue: {issue}", err=True)
    if issues and not force:
        raise typer.Exit(EXIT_DRAFT_ISSUES)
    path = drafts.write_draft(ctx.settings.path("outreach_dir"), job_id, contact["name"], text)
    with transaction(ctx.conn):
        outreach_id = outreach_repo.create(
            ctx.conn,
            contact_id=contact_id,
            job_id=job_id,
            channel=tpl.channel,
            kind=tpl.kind,
            draft_path=str(path),
            hook=hook,
        )
    _event(ctx, job_id, EventType.OUTREACH_DRAFTED, f"{tpl.name} for {contact['name']}")
    typer.echo(f"outreach {outreach_id}: {path}\n")
    typer.echo(text)


def _draft_context(
    job: sqlite3.Row,
    contact: sqlite3.Row,
    cfg: ReferralConfig,
    hook: str,
    one_liner: str | None,
    candidate: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Template variables. The sender's own details come from config/profile.yaml (gitignored):
    candidate.name, candidate.current_company, candidate.one_liner, candidate.links.portfolio.
    referrals.yaml resume_link / default_one_liner and the --one-liner flag still win."""
    candidate = candidate or {}
    links = candidate.get("links") or {}
    return {
        "first_name": _first_name(str(contact["name"])),
        "company": str(job["company"]),
        "role": str(job["title"]),
        "job_url": str(job["url"]),
        "hook": hook.strip(),
        "one_liner": (
            one_liner or cfg.default_one_liner or str(candidate.get("one_liner") or "")
        ).strip(),
        "resume_link": cfg.resume_link or str(links.get("portfolio") or ""),
        "signature": _first_name(str(candidate.get("name") or "")),
        "current_company": str(candidate.get("current_company") or ""),
    }


@refer_app.command("contacts")
def contacts_cmd(
    company: Annotated[str | None, typer.Option("--company")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List contacts, optionally filtered by a company or name fragment."""
    ctx = build_ctx()
    rows = contacts_repo.search_contacts(ctx.conn, company)
    if as_json:
        echo_json([dict(r) for r in rows])
        return
    if not rows:
        typer.echo("(no contacts; run `refer import-connections <Connections.csv>`)")
        return
    for row in rows:
        typer.echo(
            f"  [{row['id']}] {row['name']} - {row['title'] or '?'} @ {row['company'] or '?'}"
            f" (since {row['connected_on'] or '?'})"
        )


@refer_app.command("people-search")
def people_search_cmd(
    job_id: int,
    role: Annotated[
        list[str] | None, typer.Option("--role", help="Keywords, e.g. recruiter. Repeatable.")
    ] = None,
) -> None:
    """Print a LinkedIn people-search URL for the job's company. You open it; nothing is fetched."""
    ctx = build_ctx()
    job = _job(ctx, job_id)
    keywords = role or list(second_degree.DEFAULT_ROLE_KEYWORDS)
    typer.echo(second_degree.people_search_url(str(job["company"]), keywords))


# --------------------------------------------------------------------------- outreach


@outreach_app.command("open")
def open_cmd(outreach_id: int) -> None:
    """Open the contact's LinkedIn profile in your default browser and record opened_at."""
    ctx = build_ctx()
    row = _outreach(ctx, outreach_id)
    url = row["linkedin_url"]
    if not url:
        raise fail(f"contact {row['contact_name']} has no LinkedIn URL")
    os.startfile(url)  # noqa: S606 - user's own default browser, by design
    with transaction(ctx.conn):
        outreach_repo.set_opened(ctx.conn, outreach_id, now_iso())
    _event(ctx, row["job_id"], EventType.OUTREACH_OPENED, f"{row['contact_name']}: {url}")
    typer.echo(f"opened {url}")
    if row["draft_path"]:
        typer.echo(f"paste from {row['draft_path']}")


@outreach_app.command("mark-sent")
def mark_sent_cmd(outreach_id: int) -> None:
    """Record that you sent it; schedules the next follow-up from referrals.yaml."""
    ctx = build_ctx()
    cfg = _config(ctx)
    stamp = now_iso()
    with transaction(ctx.conn):
        row = _outreach(ctx, outreach_id)
        if row["sent_at"] is not None or row["referral_outcome"] != outreach_repo.OUTCOME_PENDING:
            typer.echo("send already recorded or referral already resolved; nothing changed")
            return
        stage = outreach_repo.followup_stage(ctx.conn, outreach_id) + 1
        days = outreach_repo.followup_days_for(stage, cfg.followup_days)
        due_at = outreach_repo.mark_sent(ctx.conn, outreach_id, stamp, days)
        _event(
            ctx, row["job_id"], EventType.OUTREACH_SENT, f"{row['kind']} to {row['contact_name']}"
        )
        if row["kind"] == outreach_repo.KIND_REFERRAL and row["job_id"] is not None:
            wait_until = to_iso(now() + timedelta(days=cfg.route_wait_days))
            jobs_repo.set_snooze(ctx.conn, int(row["job_id"]), wait_until)
            _event(
                ctx,
                row["job_id"],
                EventType.REFERRAL_WAIT,
                f"awaiting referral from {row['contact_name']} until {wait_until[:10]}",
            )
            typer.echo(f"job {row['job_id']} parked until {wait_until[:10]} awaiting the referral")
    typer.echo(f"sent; next follow-up {due_at or 'none (sequence complete)'}")


@outreach_app.command("mark-replied")
def mark_replied_cmd(
    outreach_id: int,
    response: Annotated[str | None, typer.Option("--response")] = None,
    referred: Annotated[
        bool, typer.Option("--referred", help="Confirm the referral is completed: unpark the job.")
    ] = False,
    declined: Annotated[
        bool,
        typer.Option("--declined", help="Record a refusal; unpark if no referrals remain pending."),
    ] = False,
) -> None:
    """Record a reply; referral completion or refusal requires an explicit outcome flag."""
    if referred and declined:
        raise fail("--referred and --declined are mutually exclusive")
    ctx = build_ctx()
    row = _outreach(ctx, outreach_id)
    outcome = None
    if referred:
        outcome = outreach_repo.OUTCOME_COMPLETED
    elif declined:
        outcome = outreach_repo.OUTCOME_DECLINED
    text = response or (REFERRED_RESPONSE if referred else DECLINED_RESPONSE if declined else None)
    try:
        _record_reply(ctx, row, text, outcome)
    except ValueError as exc:
        raise fail(str(exc)) from exc
    if referred and row["job_id"] is not None:
        typer.echo(f"referral received; job {row['job_id']} is back in the apply queue")
    if declined and row["job_id"] is not None:
        job = _job(ctx, int(row["job_id"]))
        if job["snooze_until"] is None:
            typer.echo(f"referral declined; job {row['job_id']} is back in the apply queue")
        else:
            typer.echo("referral declined; another request is still pending")
    typer.echo("reply recorded")


def _record_reply(ctx: Ctx, row: sqlite3.Row, text: str | None, outcome: str | None) -> None:
    with transaction(ctx.conn):
        row = _outreach(ctx, int(row["id"]))
        referral = outreach_repo.referral_for_reply(ctx.conn, int(row["id"])) or row
        newly_completed = (
            outcome == outreach_repo.OUTCOME_COMPLETED
            and referral["referral_outcome"] != outreach_repo.OUTCOME_COMPLETED
        )
        outreach_repo.mark_replied(
            ctx.conn, int(row["id"]), now_iso(), text, referral_outcome=outcome
        )
        if row["job_id"] is None:
            return
        events_repo.add_event(
            ctx.conn,
            row["job_id"],
            EventType.OUTREACH_REPLY,
            text or str(row["contact_name"]),
            source=SOURCE,
        )
        if newly_completed:
            events_repo.add_event(
                ctx.conn,
                row["job_id"],
                EventType.REFERRAL_RECEIVED,
                f"{row['contact_name']} completed the referral; apply through their link",
                source=SOURCE,
            )


@outreach_app.command("due")
def due_cmd(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Sent messages with no reply whose follow-up date has passed."""
    ctx = build_ctx()
    rows = outreach_repo.due(ctx.conn, now_iso())
    if as_json:
        echo_json([_outreach_view(r) for r in rows])
        return
    _print_outreach(rows)


@outreach_app.command("list")
def list_cmd(
    job: Annotated[int | None, typer.Option("--job")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """All outreach rows, optionally for one job."""
    ctx = build_ctx()
    rows = outreach_repo.list_all(ctx.conn, job)
    if as_json:
        echo_json([_outreach_view(r) for r in rows])
        return
    _print_outreach(rows)


__all__ = ["EXIT_INVALID", "outreach_app", "refer_app"]
