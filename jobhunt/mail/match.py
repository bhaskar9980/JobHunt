"""Match an email to one open application: subject templates -> sender domain -> company
mentions, then a fuzzy title check when the company has more than one open application."""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from rapidfuzz import fuzz

from jobhunt.config import CompanyConfig
from jobhunt.mail.models import Candidate, MailMessage, MatchResult
from jobhunt.mail.rules import is_ats_domain
from jobhunt.match import phrase_in
from jobhunt.normalize import Normalizer

log = logging.getLogger(__name__)

OPEN_STATUSES: tuple[str, ...] = (
    "approved",
    "applied",
    "screening",
    "interview",
    "offer",
    "ghosted",
)
SINGLE_MATCH_CONFIDENCE = 0.9
TITLE_MATCH_CONFIDENCE = 0.85
STRONG_TITLE_RATIO = 85.0
TITLE_GAP = 10.0
MENTION_SCORE = 100.0
MIN_COMPANY_CHARS = 3
MAX_COMPANY_CHARS = 60
_PLACEHOLDER = re.compile(r"\{(company|title)\}")
_ESCAPED_SPACES = re.compile(r"(?:\\ )+")
_STOP_CHARS = r"\-:|(\["
_NON_WORD = re.compile(r"[^\w\s]+")
_LEADING_NOISE = re.compile(r"^(?:the|team)\s+", re.I)
_TRAILING_NOISE = re.compile(r"\s+(?:team|careers|recruiting|talent)$", re.I)


@dataclass(slots=True)
class SubjectHints:
    companies: list[str] = field(default_factory=list)
    title: str | None = None
    explicit_title: bool = False


@dataclass(slots=True)
class _OpenApp:
    job_id: int
    company: str
    company_norm: str
    title: str
    title_norm: str
    status: str


def match_application(
    conn: sqlite3.Connection,
    msg: MailMessage,
    rules: Mapping[str, Any],
    companies: Sequence[CompanyConfig],
    norm: Normalizer,
) -> MatchResult:
    open_apps = _open_applications(conn)
    if not open_apps:
        return MatchResult(None, 0.0, [], "no open applications")
    alias_map = _alias_map(companies, norm)
    hints = extract_subject_hints(msg.subject, rules)
    wanted = {alias_map.get(norm.company(c), norm.company(c)) for c in hints.companies}
    if not is_ats_domain(msg.from_domain, rules):
        wanted |= _companies_from_domain(msg.from_domain, companies, norm)
    reason = "subject/domain"
    if not wanted:
        wanted = _mentioned_companies(msg.text, open_apps, alias_map)
        reason = "company mentioned in body"
    candidates = [a for a in open_apps if alias_map.get(a.company_norm, a.company_norm) in wanted]
    if not candidates:
        return MatchResult(None, 0.0, [], "no tracked company found in the email")
    return _pick(candidates, hints.title, msg.text, reason, strict_title=hints.explicit_title)


# --------------------------------------------------------------------------- subject templates


def extract_subject_hints(subject: str, rules: Mapping[str, Any]) -> SubjectHints:
    lowered = " ".join((subject or "").lower().split())
    for template in rules.get("subject_templates") or []:
        pattern = _template_regex(str(template).lower())
        found = pattern.search(lowered)
        if not found:
            continue
        groups = found.groupdict()
        company = _clean_capture(groups.get("company"))
        title = _clean_capture(groups.get("title"))
        if company:
            explicit = bool(
                re.search(
                    r"(?:application|applying|position|role)\s+(?:for|as)\s+\{title\}",
                    str(template),
                    re.I,
                )
            )
            return SubjectHints([company], title or None, explicit_title=explicit)
    return SubjectHints()


@lru_cache(maxsize=128)
def _template_regex(template: str) -> re.Pattern[str]:
    parts: list[str] = []
    last = 0
    for match in _PLACEHOLDER.finditer(template):
        parts.append(_literal(template[last : match.start()]))
        parts.append(rf"(?P<{match.group(1)}>[^{_STOP_CHARS}]+?)")
        last = match.end()
    parts.append(_literal(template[last:]))
    body = "".join(parts)
    prefix = "^" if template.startswith("{") else ""
    suffix = rf"\s*(?:[{_STOP_CHARS}].*)?$" if template.endswith("}") else ""
    return re.compile(prefix + body + suffix)


def _literal(text: str) -> str:
    """Escape a template fragment; runs of spaces become one-or-more whitespace."""
    return _ESCAPED_SPACES.sub(r"\\s+", re.escape(text))


def _clean_capture(value: str | None) -> str:
    text = (value or "").strip(" .,!?\"'")
    text = _LEADING_NOISE.sub("", text)
    text = _TRAILING_NOISE.sub("", text)
    if len(text) < MIN_COMPANY_CHARS or len(text) > MAX_COMPANY_CHARS:
        return ""
    return text


# --------------------------------------------------------------------------- companies


def _alias_map(companies: Iterable[CompanyConfig], norm: Normalizer) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for company in companies:
        canonical = norm.company(company.name)
        for alias in company.aliases:
            mapping[norm.company(alias)] = canonical
    return mapping


def _companies_from_domain(
    domain: str | None, companies: Iterable[CompanyConfig], norm: Normalizer
) -> set[str]:
    if not domain:
        return set()
    domain = domain.lower()
    found: set[str] = set()
    for company in companies:
        for known in company.domains:
            if domain == known or domain.endswith("." + known):
                found.add(norm.company(company.name))
    return found


def _mentioned_companies(
    text: str, open_apps: Sequence[_OpenApp], alias_map: Mapping[str, str]
) -> set[str]:
    reverse: dict[str, set[str]] = {}
    for alias, canonical in alias_map.items():
        reverse.setdefault(canonical, set()).add(alias)
    found: set[str] = set()
    for app in open_apps:
        canonical = alias_map.get(app.company_norm, app.company_norm)
        names = {app.company_norm, app.company.lower(), canonical} | reverse.get(canonical, set())
        if any(len(n) >= MIN_COMPANY_CHARS and phrase_in(n, text) for n in names):
            found.add(canonical)
    return found


# --------------------------------------------------------------------------- pick


def _open_applications(conn: sqlite3.Connection) -> list[_OpenApp]:
    placeholders = ",".join("?" for _ in OPEN_STATUSES)
    rows = conn.execute(
        f"""
        SELECT id, company, company_norm, title, title_norm, pipeline_status
        FROM v_pipeline WHERE pipeline_status IN ({placeholders}) ORDER BY id
        """,
        OPEN_STATUSES,
    ).fetchall()
    return [
        _OpenApp(
            int(r["id"]),
            r["company"],
            r["company_norm"],
            r["title"],
            r["title_norm"],
            r["pipeline_status"],
        )
        for r in rows
    ]


def _pick(
    candidates: Sequence[_OpenApp],
    title_hint: str | None,
    text: str,
    reason: str,
    *,
    strict_title: bool = False,
) -> MatchResult:
    if len(candidates) == 1:
        only = candidates[0]
        if (
            strict_title
            and title_hint
            and _title_score(only, title_hint, text) < STRONG_TITLE_RATIO
        ):
            return MatchResult(
                None,
                0.0,
                [_cand(only, _title_score(only, title_hint, text))],
                "email explicitly names a different role; review required",
            )
        cands = [_cand(only, MENTION_SCORE)]
        return MatchResult(only.job_id, SINGLE_MATCH_CONFIDENCE, cands, reason)
    scored = sorted(
        (_cand(app, _title_score(app, title_hint, text)) for app in candidates),
        key=lambda c: c.score,
        reverse=True,
    )
    top, second = scored[0], scored[1]
    if top.score >= STRONG_TITLE_RATIO and top.score - second.score >= TITLE_GAP:
        return MatchResult(top.job_id, TITLE_MATCH_CONFIDENCE, scored, f"{reason} + title")
    return MatchResult(None, 0.0, scored, "several open applications at this company")


def _title_score(app: _OpenApp, title_hint: str | None, text: str) -> float:
    if title_hint:
        return float(fuzz.token_set_ratio(_plain(title_hint), _plain(app.title)))
    plain = _plain(text)
    if phrase_in(app.title_norm, plain) or phrase_in(app.title.lower(), plain):
        return MENTION_SCORE
    return 0.0


def _plain(text: str) -> str:
    """Lower-case, punctuation to spaces, single spaces (so 'Engineer,' matches 'engineer')."""
    return " ".join(_NON_WORD.sub(" ", text.lower()).split())


def _cand(app: _OpenApp, score: float) -> Candidate:
    return Candidate(app.job_id, app.company, app.title, app.status, round(score, 1))
