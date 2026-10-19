"""Attach referral counts and the route recommendation to review-queue items."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from jobhunt.clock import now
from jobhunt.config import Settings, build_normalizer, load_company_aliases
from jobhunt.normalize import Normalizer
from jobhunt.referral import ranker
from jobhunt.referral.route import Route, recommend
from jobhunt.referral.rules import ReferralConfig
from jobhunt.repo import outreach as outreach_repo

TOP_NAMES = 3

Enricher = Callable[[sqlite3.Row], dict[str, Any]]


def build_enricher(conn: sqlite3.Connection, settings: Settings) -> Enricher:
    cfg = ReferralConfig.load(settings.config_dir)
    norm = build_normalizer(settings)
    aliases = load_company_aliases(settings)

    def enrich(row: sqlite3.Row) -> dict[str, Any]:
        return enrich_row(conn, row, cfg, norm, aliases, now=now())

    return enrich


def build_tracking_enricher(conn: sqlite3.Connection) -> Enricher:
    """Keep existing referral outcomes and waits visible without ranking new contacts."""
    cfg = ReferralConfig(route_enabled=False)

    def enrich(row: sqlite3.Row) -> dict[str, Any]:
        outreach_rows = outreach_repo.for_job(conn, int(row["id"]))
        return _route_fields(recommend(row, [], outreach_rows, cfg, now=now()))

    return enrich


def enrich_row(
    conn: sqlite3.Connection,
    row: Mapping[str, Any],
    cfg: ReferralConfig,
    norm: Normalizer,
    aliases: Mapping[str, list[str]],
    *,
    now: datetime,
) -> dict[str, Any]:
    suggestions = ranker.suggest(conn, row, cfg, norm, aliases, now=now)
    outreach_rows = outreach_repo.for_job(conn, int(row["id"]))
    route = recommend(row, suggestions, outreach_rows, cfg, now=now)
    return {
        "referral_count": len(suggestions),
        "referral_top": [s.name for s in suggestions[:TOP_NAMES]],
        **_route_fields(route),
    }


def _route_fields(route: Route) -> dict[str, Any]:
    return {
        "route": route.route,
        "route_reason": route.reason,
        "route_contact": route.contact_name,
        "wait_until": route.wait_until,
    }
