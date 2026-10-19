"""Deterministic prefilter: cheap profile rules applied before any LLM scoring."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from functools import lru_cache

from jobhunt.clock import now_iso
from jobhunt.config import Profile, Targets
from jobhunt.db import read_state, transaction, write_state
from jobhunt.models import (
    ELIGIBILITY_REVIEW_REASON,
    NO_DESCRIPTION_REASON,
    EventType,
    PrefilterResult,
    Status,
)
from jobhunt.normalize import REMOTE_BUCKET, UNKNOWN_BUCKET, normalize_title
from jobhunt.repo import applications, events
from jobhunt.repo import jobs as jobs_repo

log = logging.getLogger(__name__)

_TWO_LETTER_CODE = re.compile(r",\s*([A-Z]{2})(?![A-Za-z])")
# Real US state codes, so an ISO country code such as PT or NL is not called a state.
_US_STATE_CODES = frozenset(
    # noqa lives here because the rule flags the first line of the expression.
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IA KS KY LA ME MD MA MI MN MS MO MT "  # noqa: SIM905
    "NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split()
)
# "IN" is both Indiana and India. In this search it means India far more often, and reading
# "Coimbatore, IND-31, IN" as Indiana would silently drop a role he wants.
_INDIA_CODE = "IN"

CORE_WEIGHT = 1.0
NICE_WEIGHT = 0.5
EXPERIENCE_WINDOW_CHARS = 90
MAX_YEARS_SANE = 30


def _evidence_matches(text: str, targets: Targets, key: str) -> bool:
    return any(
        re.search(pattern, text, re.I) for pattern in targets.international_rules.get(key, [])
    )


def _international_location(job, profile: Profile) -> tuple[bool, str]:
    targets = profile.targets
    bucket = str(job["location_bucket"])
    location = _text(job, "location") or ""
    remote = bool(job["is_remote"]) or bucket == REMOTE_BUCKET
    domestic = _marker_in(location, targets.international_rules.get("domestic_markers", []))
    if not remote and (
        bucket in targets.international_rules.get("domestic_buckets", [])
        or (bucket == UNKNOWN_BUCKET and domestic)
    ):
        return location_ok(bucket, False, profile, location)
    description = _text(job, "description_md") or ""
    if (_text(job, "needs_hydration") or "").lower() in {"1", "true"}:
        return True, NO_DESCRIPTION_REASON
    statements = " ".join(
        part for part in re.split(r"(?<=[.!?])\s+|\n", description) if "?" not in part
    )
    text = f"{location}\n{statements}"
    if _evidence_matches(text, targets, "international_exclusions"):
        return False, f"{ELIGIBILITY_REVIEW_REASON}: restrictive residence or work conditions"
    restricted = _marker_in(
        location, [*targets.remote_regions_excluded, *targets.foreign_markers]
    ) or _us_state_in(location)
    remote_evidence = domestic or _evidence_matches(location, targets, "worldwide_location")
    remote_evidence = remote_evidence or _evidence_matches(statements, targets, "remote_support")
    if (
        remote
        and targets.remote_ok
        and remote_evidence
        and not restricted
        and not _evidence_matches(text, targets, "remote_exclusions")
    ):
        return True, "remote eligibility: India/worldwide stated; verify employer conditions"
    if (
        targets.relocate_ok
        and _evidence_matches(statements, targets, "visa_support")
        and _evidence_matches(statements, targets, "relocation_support")
        and not _evidence_matches(text, targets, "visa_exclusions")
        and not _evidence_matches(text, targets, "relocation_exclusions")
    ):
        return True, "overseas eligibility: visa sponsorship and relocation support stated"
    return (
        False,
        f"{ELIGIBILITY_REVIEW_REASON}: India-eligible remote or visa plus relocation required",
    )


def job_location_ok(job, profile: Profile) -> tuple[bool, str]:
    if profile.targets.international_remote_or_relocation:
        return _international_location(job, profile)
    return location_ok(
        str(job["location_bucket"]), bool(job["is_remote"]), profile, _text(job, "location")
    )


_YEARS = re.compile(
    r"(?<!\d)(\d{1,2})\s*\+?\s*(?:(?:-|to|–|—)\s*(\d{1,2})\s*\+?\s*)?(?:years?|yrs?)\b", re.I
)
_EXPERIENCE_HINT = re.compile(r"\b(experience|exp\.?|background|working with)\b", re.I)
_PHRASE_BOUNDARY = r"(?<![\w#+]){phrase}(?![\w#+])"


@dataclass(slots=True)
class PrefilterSummary:
    evaluated: int = 0
    passed: int = 0
    filtered: int = 0
    restored: int = 0
    reasons: dict[str, int] = field(default_factory=dict)


@lru_cache(maxsize=4096)
def _phrase_regex(phrase: str) -> re.Pattern[str]:
    return re.compile(_PHRASE_BOUNDARY.format(phrase=re.escape(phrase)), re.I)


def phrase_in(phrase: str, text: str) -> bool:
    return bool(phrase) and bool(text) and _phrase_regex(phrase.lower()).search(text) is not None


@lru_cache(maxsize=512)
def _normalized_phrase(phrase: str) -> str:
    return normalize_title(phrase) or phrase.lower()


def title_matches(
    title_norm: str, include: Iterable[str], exclude: Iterable[str]
) -> tuple[bool, str]:
    """Exclusions win, except when the excluded word is part of a matched target title.

    So "staff" still drops "Staff Software Engineer" while "Member of Technical Staff" (a target
    title that itself contains "staff") gets through.
    """
    matched = [
        (phrase, _normalized_phrase(phrase))
        for phrase in include
        if phrase_in(_normalized_phrase(phrase), title_norm)
    ]
    for phrase in exclude:
        excluded = _normalized_phrase(phrase)
        if not phrase_in(excluded, title_norm):
            continue
        if any(phrase_in(excluded, target_norm) for _, target_norm in matched):
            continue
        return False, f"title excluded: {phrase}"
    if matched:
        return True, f"title matched: {matched[0][0]}"
    return False, "title not in targets"


def location_ok(
    bucket: str, is_remote: bool, profile: Profile, location_text: str | None = None
) -> tuple[bool, str]:
    targets = profile.targets
    if is_remote or bucket == REMOTE_BUCKET:
        return _remote_ok(targets, location_text)
    if bucket == UNKNOWN_BUCKET:
        marker = _marker_in(location_text, targets.foreign_markers) or _us_state_in(location_text)
        if marker:
            return False, f"location {marker} not in targets"
        return True, "location unknown"
    return _city_ok(targets, bucket)


def _remote_ok(targets: Targets, location_text: str | None) -> tuple[bool, str]:
    if not targets.remote_ok:
        return False, "remote not wanted"
    excluded = targets.remote_regions_excluded
    region = _marker_in(location_text, excluded) or _us_state_in(location_text)
    return (False, f"remote restricted to {region}") if region else (True, "remote")


def _city_ok(targets: Targets, bucket: str) -> tuple[bool, str]:
    if bucket in targets.locations:
        return True, f"location {bucket}"
    if targets.relocate_ok and bucket in targets.relocatable_locations:
        return True, f"relocatable to {bucket}"
    return False, f"location {bucket} not in targets"


def _marker_in(text: str | None, markers: Iterable[str]) -> str | None:
    if not text:
        return None
    lowered = text.lower()
    for marker in markers:
        if re.search(rf"(?<!\w){re.escape(marker)}(?!\w)", lowered):
            return marker
    return None


def _us_state_in(text: str | None) -> str | None:
    """'Chicago, IL' or 'Lisbon, PT' suffixes: a comma then a two-letter upper-case code.

    Reached only when no city and no region matched, so the code is the last thing left to read.
    A real state code is reported as one; any other code is reported as a country code rather
    than mislabelled as a state. ``IN`` is never a rejection: see ``_INDIA_CODE``.
    """
    if not text:
        return None
    for match in _TWO_LETTER_CODE.finditer(text):
        code = match.group(1)
        if code == _INDIA_CODE:
            return None
        if code in _US_STATE_CODES:
            return f"us state {code}"
        return f"country code {code}"
    return None


def parse_min_years(text: str | None) -> int | None:
    if not text:
        return None
    candidates: list[int] = []
    for match in _YEARS.finditer(text):
        start = max(0, match.start() - EXPERIENCE_WINDOW_CHARS)
        end = min(len(text), match.end() + EXPERIENCE_WINDOW_CHARS)
        if not _EXPERIENCE_HINT.search(text[start:end]):
            continue
        years = int(match.group(1))
        if 0 < years <= MAX_YEARS_SANE:
            candidates.append(years)
    return min(candidates) if candidates else None


def skill_hits(text: str, skills: Iterable[str], aliases: Mapping[str, list[str]]) -> list[str]:
    found: list[str] = []
    for skill in skills:
        phrases = aliases.get(skill, []) or [skill]
        if any(phrase_in(p, text) for p in [skill, *phrases]):
            found.append(skill)
    return found


def skill_overlap(
    text: str, core: list[str], nice: list[str], aliases: Mapping[str, list[str]]
) -> float:
    total = CORE_WEIGHT * len(core) + NICE_WEIGHT * len(nice)
    if total == 0:
        return 0.0
    score = CORE_WEIGHT * len(skill_hits(text, core, aliases))
    score += NICE_WEIGHT * len(skill_hits(text, nice, aliases))
    return round(score / total, 3)


def has_deal_breaker(text: str, terms: Iterable[str]) -> str | None:
    for term in terms:
        if phrase_in(term, text):
            return term
    return None


def prefilter(
    job: Mapping[str, object] | sqlite3.Row,
    profile: Profile,
    aliases: Mapping[str, list[str]],
) -> PrefilterResult:
    """Title exclude -> title include -> location -> deal breakers -> years -> must-have skills."""
    reasons: list[str] = []
    title_norm = str(job["title_norm"] or "")
    ok, reason = title_matches(
        title_norm, profile.targets.target_titles, profile.targets.exclude_titles
    )
    reasons.append(reason)
    if not ok:
        return PrefilterResult(False, reasons, 0.0, None)
    language = _other_stack_in_title(f"{job['title'] or ''} {title_norm}", profile, aliases)
    if language:
        wanted = " or ".join(profile.skills.must_have_any)
        reasons.append(f"title names {language}, not {wanted}")
        return PrefilterResult(False, reasons, 0.0, None)
    ok, reason = job_location_ok(job, profile)
    reasons.append(reason)
    if not ok:
        return PrefilterResult(False, reasons, 0.0, None)
    if reason == NO_DESCRIPTION_REASON:
        return PrefilterResult(True, reasons, 0.0, None)
    text = f"{job['title'] or ''}\n{job['description_md'] or ''}".lower()
    if not job["description_md"]:
        reasons.append(NO_DESCRIPTION_REASON)
        return PrefilterResult(True, reasons, 0.0, None)
    return _content_checks(text, profile, aliases, reasons)


def _other_stack_in_title(
    title: str, profile: Profile, aliases: Mapping[str, list[str]]
) -> str | None:
    """A title built around another language fails, whatever the description says.

    "Java Full Stack Developer" is out; "Java/.NET Developer" stays in and the skills gate
    decides. Reads the raw title, so "c#" and "c++" keep their symbols, together with the
    normalised one, so "Dot Net" counts as .NET.
    """
    languages = profile.targets.exclude_title_languages
    if not languages or skill_hits(title, profile.skills.must_have_any, aliases):
        return None
    return _marker_in(title, languages)


def _text(job: Mapping[str, object] | sqlite3.Row, key: str) -> str | None:
    try:
        value = job[key]
    except (KeyError, IndexError):
        return None
    return str(value) if value is not None else None


def _content_checks(
    text: str, profile: Profile, aliases: Mapping[str, list[str]], reasons: list[str]
) -> PrefilterResult:
    breaker = has_deal_breaker(text, profile.skills.deal_breakers)
    overlap = skill_overlap(text, profile.skills.core, profile.skills.nice_to_have, aliases)
    min_years = parse_min_years(text)
    if breaker:
        reasons.append(f"deal breaker: {breaker}")
        return PrefilterResult(False, reasons, overlap, min_years)
    limit = profile.targets.seniority_max_years_required
    if min_years is not None and min_years > limit:
        reasons.append(f"asks {min_years}+ years (max {limit})")
        return PrefilterResult(False, reasons, overlap, min_years)
    if profile.skills.must_have_any and not skill_hits(text, profile.skills.must_have_any, aliases):
        reasons.append(f"none of {', '.join(profile.skills.must_have_any)} mentioned")
        return PrefilterResult(False, reasons, overlap, min_years)
    reasons.append(f"skill overlap {overlap:.2f}")
    if min_years is not None:
        reasons.append(f"asks {min_years}+ years")
    return PrefilterResult(True, reasons, overlap, min_years)


def apply_prefilter(
    conn: sqlite3.Connection,
    profile: Profile,
    aliases: Mapping[str, list[str]],
    *,
    rescan: bool = False,
) -> PrefilterSummary:
    """Recheck machine decisions when the profile changes, preserving application history."""
    encoded = json.dumps({"profile": asdict(profile), "aliases": aliases}, sort_keys=True)
    signature = hashlib.sha256(encoded.encode()).hexdigest()
    with transaction(conn):
        refresh = rescan or read_state(conn, "prefilter_profile") != signature
        if refresh:
            _reset_profile_scores(conn)
        summary = _evaluate_prefilter(conn, profile, aliases, rescan=refresh)
        write_state(conn, "prefilter_profile", signature, now_iso())
    return summary


def _reset_profile_scores(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT id, status FROM jobs WHERE status IN ('scored', 'shortlisted')"
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE jobs SET status = 'discovered', score = NULL, score_reasons = NULL, "
            "scored_at = NULL, prefilter_pass = NULL, prefilter_reasons = NULL WHERE id = ?",
            (row["id"],),
        )
        events.add_event(
            conn,
            row["id"],
            EventType.STATUS_CHANGE,
            "Profile changed; reassess fit",
            from_status=row["status"],
            to_status=Status.DISCOVERED,
            source="prefilter",
        )
    conn.execute(
        "UPDATE jobs SET prefill_state = 'none' WHERE status = 'approved' "
        "AND submission_state = 'none'"
    )


def _evaluate_prefilter(
    conn: sqlite3.Connection, profile: Profile, aliases: Mapping[str, list[str]], *, rescan: bool
) -> PrefilterSummary:
    summary = PrefilterSummary()
    for row in jobs_repo.prefilter_candidates(conn, rescan=rescan):
        result = prefilter(row, profile, aliases)
        jobs_repo.mark_prefilter(conn, row["id"], result)
        summary.evaluated += 1
        current = Status(row["status"])
        if result.passed:
            summary.passed += 1
            if current == Status.FILTERED:
                jobs_repo.set_job_status(conn, row["id"], Status.DISCOVERED, "prefilter rescan")
                summary.restored += 1
            if (
                current == Status.APPROVED
                and row["prefilter_pass"] == 0
                and not applications.submission_unresolved(conn, row["id"])
            ):
                jobs_repo.set_prefill_state(conn, row["id"], "none")
            continue
        summary.filtered += 1
        key = result.reasons[-1].split(":")[0]
        summary.reasons[key] = summary.reasons.get(key, 0) + 1
        if current == Status.DISCOVERED:
            jobs_repo.set_job_status(conn, row["id"], Status.FILTERED, result.reasons[-1])
        elif current == Status.APPROVED:
            jobs_repo.set_prefill_state(conn, row["id"], "blocked")
    log.info(
        "prefilter: evaluated=%s passed=%s filtered=%s restored=%s",
        summary.evaluated,
        summary.passed,
        summary.filtered,
        summary.restored,
    )
    return summary


def title_predicate(profile: Profile):
    """For pipeline.hydrate_pending: only fetch full descriptions for plausible titles."""

    def _ok(row: sqlite3.Row) -> bool:
        passed, _ = title_matches(
            str(row["title_norm"] or ""),
            profile.targets.target_titles,
            profile.targets.exclude_titles,
        )
        if not passed:
            return False
        if profile.targets.international_remote_or_relocation:
            return True
        location_pass, _ = location_ok(
            str(row["location_bucket"]), bool(row["is_remote"]), profile, row["location"]
        )
        return location_pass

    return _ok
