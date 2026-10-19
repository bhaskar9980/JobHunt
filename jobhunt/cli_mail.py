"""`jobhunt mail ...` sub-app: ingest inbox JSON, review queue, resolve, last-sync, ghost check."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Annotated

import typer

from jobhunt.cli_common import Ctx, build_ctx, echo_json, fail
from jobhunt.clock import now, parse_iso, to_iso
from jobhunt.config import build_normalizer as _build_normalizer
from jobhunt.config import load_companies
from jobhunt.mail import ghosting, ingest
from jobhunt.mail.models import KINDS
from jobhunt.mail.rules import load_mail_rules, render_queries

log = logging.getLogger(__name__)

mail_app = typer.Typer(
    no_args_is_help=True,
    help="Inbox sync: classify application emails and move statuses.",
)

MAIL_INBOX_KEY = "mail_inbox_dir"
DEFAULT_GHOST_DAYS = 21
RESOLVE_KINDS = (*KINDS, ingest.KIND_IGNORE)


def _mail_dir(ctx: Ctx):
    path = ctx.settings.path(MAIL_INBOX_KEY)
    path.mkdir(parents=True, exist_ok=True)
    return path


@mail_app.command("ingest")
def ingest_cmd(
    as_json: Annotated[bool, typer.Option("--json")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Classify and apply every JSON message file in data/mail_inbox/."""
    ctx = build_ctx(verbose)
    rules = load_mail_rules(ctx.settings.config_dir)
    summary = ingest.ingest_mail(
        ctx.conn,
        _mail_dir(ctx),
        rules,
        load_companies(ctx.settings),
        _build_normalizer(ctx.settings),
    )
    if as_json:
        echo_json(summary.to_dict())
        raise typer.Exit(1 if summary.failed else 0)
    typer.echo(
        f"processed={summary.processed} applied={summary.auto_applied} "
        f"needs_review={summary.needs_review} unclassified={summary.unclassified} "
        f"duplicates={summary.duplicates} failed={len(summary.failed)}"
    )
    for outcome in summary.outcomes:
        if outcome.changed:
            typer.echo(
                f"  job {outcome.job_id}: {outcome.from_status} -> {outcome.to_status} "
                f"({outcome.kind})"
            )
    for failure in summary.failed:
        typer.echo(f"  failed: {failure}", err=True)
    if summary.failed:
        raise typer.Exit(1)


@mail_app.command("sync-complete")
def sync_complete_cmd(through: Annotated[str, typer.Option("--through")]) -> None:
    """Checkpoint a fully fetched and ingested inbox pass, using its start timestamp."""
    ctx = build_ctx()
    try:
        checkpoint = parse_iso(through)
        if checkpoint is None:
            raise ValueError("--through is required")
        ingest.complete_sync(ctx.conn, checkpoint)
    except ValueError as exc:
        raise fail("Use a valid, non-future ISO timestamp from mail last-sync.") from exc
    typer.echo("Gmail sync checkpoint recorded")


@mail_app.command("review")
def review_cmd(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List messages waiting for a human decision (thread id, message id, kind, subject)."""
    ctx = build_ctx()
    items = ingest.load_needs_review(_mail_dir(ctx))
    if as_json:
        echo_json(items)
        return
    if not items:
        typer.echo("(nothing to review)")
        return
    for item in items:
        typer.echo(
            f"thread {item['thread_id']} message {item.get('gmail_id', '?')}  "
            f"{item['kind']} {item['confidence']:.2f}  {item['subject'][:70]}  <{item['from']}>"
        )
        for cand in item.get("candidates") or []:
            typer.echo(
                f"    job {cand['job_id']}: {cand['company']} / {cand['title']} "
                f"[{cand['status']}] {cand['score']}"
            )


@mail_app.command("resolve")
def resolve_cmd(
    message_id: Annotated[
        str, typer.Argument(help="Gmail message id from `mail review` (thread id if unambiguous)")
    ],
    job: Annotated[int | None, typer.Option("--job")] = None,
    kind: Annotated[str | None, typer.Option("--kind")] = None,
    ignore: Annotated[bool, typer.Option("--ignore")] = False,
) -> None:
    """Apply a human decision for one needs-review message (or --ignore to drop it)."""
    ctx = build_ctx()
    decided_kind = ingest.KIND_IGNORE if ignore else kind
    if decided_kind not in RESOLVE_KINDS:
        raise fail(f"--kind must be one of {', '.join(RESOLVE_KINDS)} or pass --ignore")
    if decided_kind != ingest.KIND_IGNORE and job is None:
        raise fail("--job is required unless --ignore")
    rules = load_mail_rules(ctx.settings.config_dir)
    try:
        outcome = ingest.resolve_review(
            ctx.conn, _mail_dir(ctx), message_id, job_id=job, kind=decided_kind, rules=rules
        )
    except KeyError as exc:
        raise fail(str(exc)) from exc
    if outcome is None:
        typer.echo(f"{message_id}: ignored")
        return
    typer.echo(
        f"{message_id}: job {outcome.job_id} {outcome.from_status} -> {outcome.to_status} "
        f"({outcome.kind}){' [duplicate]' if outcome.duplicate else ''}"
    )


@mail_app.command("last-sync")
def last_sync_cmd(
    as_json: Annotated[bool, typer.Option("--json")] = False,
    days: Annotated[int | None, typer.Option("--days", min=1)] = None,
) -> None:
    """Last completed Gmail sync, the search boundary, and queries for this lookback."""
    ctx = build_ctx()
    rules = load_mail_rules(ctx.settings.config_dir)
    reference = now()
    last = ingest.last_sync(ctx.conn)
    days = days if days is not None else ingest.lookback_days(ctx.conn, rules, now=reference)
    queries = render_queries(rules, days)
    if as_json:
        echo_json(
            {
                "last_sync": to_iso(last) if last else None,
                "now": to_iso(reference),
                "search_after": to_iso(reference - timedelta(days=days)),
                "lookback_days": days,
                "queries": queries,
            }
        )
        return
    typer.echo(f"last sync: {to_iso(last) if last else 'never'}; lookback {days}d")
    for kind, query in queries.items():
        typer.echo(f"  {kind}: {query}")


@mail_app.command("ghost-check")
def ghost_check_cmd(
    days: Annotated[int | None, typer.Option("--days")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Mark applications with no activity for N days as ghosted and suggest a follow-up."""
    ctx = build_ctx()
    ghost_days = days or int(
        ctx.settings.section("follow_up").get("ghost_days", DEFAULT_GHOST_DAYS)
    )
    moved = ghosting.ghost_check(ctx.conn, ghost_days, dry_run=dry_run)
    if as_json:
        echo_json({"ghost_days": ghost_days, "dry_run": dry_run, "job_ids": moved})
        return
    verb = "would move" if dry_run else "moved"
    typer.echo(f"ghost check ({ghost_days}d): {verb} {len(moved)} application(s) to ghosted")
    for job_id in moved:
        typer.echo(f"  job {job_id}")
