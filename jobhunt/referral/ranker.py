"""Rank contacts at a job's company by how useful (and how fair) it is to ask them."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from jobhunt.clock import parse_iso
from jobhunt.match import phrase_in
from jobhunt.normalize import Normalizer
from jobhunt.referral.importer import alias_index
from jobhunt.referral.rules import ReferralConfig
from jobhunt.repo import contacts as contacts_repo
from jobhunt.repo import outreach as outreach_repo


@dataclass(slots=True)
class Suggestion:
    contact_id: int
    name: str
    title: str | None
    linkedin_url: str | None
    score: int
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def company_keys(company_norm: str, norm: Normalizer, aliases: Mapping[str, list[str]]) -> set[str]:
    """The canonical key plus every alias that folds onto it (or that it folds onto)."""
    index = alias_index(norm, aliases)
    canonical = index.get(company_norm, company_norm)
    keys = {company_norm, canonical}
    keys.update(alias for alias, target in index.items() if target == canonical)
    return keys


def suggest(
    conn: sqlite3.Connection,
    job: Mapping[str, Any],
    cfg: ReferralConfig,
    norm: Normalizer,
    aliases: Mapping[str, list[str]],
    *,
    now: datetime,
) -> list[Suggestion]:
    keys = company_keys(str(job["company_norm"]), norm, aliases)
    ranked: list[Suggestion] = []
    for contact in contacts_repo.contacts_for_company(conn, keys):
        score, reasons = score_contact(
            contact,
            str(job["title"] or ""),
            cfg,
            now=now,
            prior_reply=outreach_repo.has_reply(conn, int(contact["id"])),
            last_asked=outreach_repo.last_asked(conn, int(contact["id"])),
        )
        if score <= 0:
            continue
        ranked.append(
            Suggestion(
                contact_id=int(contact["id"]),
                name=str(contact["name"]),
                title=contact["title"],
                linkedin_url=contact["linkedin_url"],
                score=score,
                reasons=reasons,
            )
        )
    ranked.sort(key=lambda s: (-s.score, s.name))
    return ranked[: cfg.max_suggestions]


def score_contact(
    contact: Mapping[str, Any],
    job_title: str,
    cfg: ReferralConfig,
    *,
    now: datetime,
    prior_reply: bool,
    last_asked: str | None,
) -> tuple[int, list[str]]:
    title = str(contact["title"] or "").lower()
    score, reasons = 0, []
    shared = shared_role_tokens(title, job_title.lower(), cfg.role_tokens)
    if shared:
        score += cfg.weight("same_team_or_role")
        reasons.append(f"same team or role ({', '.join(sorted(shared))})")
    for key, tokens, label in (
        ("engineer", cfg.engineer_tokens, "engineer"),
        ("recruiter", cfg.recruiter_tokens, "recruiter"),
        ("manager", cfg.manager_tokens, "manager or lead"),
    ):
        if _any_token(title, tokens):
            score += cfg.weight(key)
            reasons.append(label)
    if _connected_recently(_field(contact, "connected_on"), now, cfg.connected_within_days):
        score += cfg.weight("recent_connection")
        reasons.append(f"connected within {cfg.connected_within_years}y")
    if prior_reply:
        score += cfg.weight("prior_reply")
        reasons.append("replied before")
    if _within_cooldown(last_asked, now, cfg.cooldown_days):
        score += cfg.weight("cooldown_penalty")
        reasons.append(f"asked within the last {cfg.cooldown_days} days")
    return score, reasons


def _field(row: Mapping[str, Any], key: str) -> Any:
    """sqlite3.Row has no .get(); treat a missing key as None for dicts and rows alike."""
    return row[key] if key in row.keys() else None  # noqa: SIM118 - Row is not a dict


def shared_role_tokens(contact_title: str, job_title: str, tokens: Iterable[str]) -> set[str]:
    return {t for t in tokens if phrase_in(t, contact_title) and phrase_in(t, job_title)}


def _any_token(text: str, tokens: Iterable[str]) -> bool:
    return any(phrase_in(t, text) for t in tokens)


def _connected_recently(value: str | None, now: datetime, within_days: int) -> bool:
    connected = parse_iso(value)
    return connected is not None and now - connected <= timedelta(days=within_days)


def _within_cooldown(last_asked: str | None, now: datetime, cooldown_days: int) -> bool:
    asked = parse_iso(last_asked)
    return asked is not None and now - asked < timedelta(days=cooldown_days)
