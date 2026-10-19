"""Canonical forms for URLs, company names, titles and locations. Dedupe correctness lives here."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from jobhunt.models import JobPosting, NormalizedKeys

FINGERPRINT_LENGTH = 16
REMOTE_BUCKET = "remote"
UNKNOWN_BUCKET = "unknown"

_LINKEDIN_VIEW = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(?:[^/?#]*?-)?(\d{6,})", re.I)
_LINKEDIN_CURRENT = re.compile(r"[?&]currentJobId=(\d{6,})", re.I)
_LINKEDIN_CANONICAL = "https://www.linkedin.com/jobs/view/{job_id}"
_INDEED_HOST = re.compile(r"^(?:[a-z]{2}\.|www\.)?indeed\.com$", re.I)
_INDEED_JK = re.compile(r"[?&](?:jk|vjk)=([0-9a-f]{8,})", re.I)
_INDEED_CANONICAL = "https://{host}/viewjob?jk={jk}"

_REMOTE_HINT = re.compile(r"\b(remote|work from home|wfh|anywhere|distributed)\b", re.I)

_PARENS = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_REQ_ID = re.compile(r"\b[a-z]{0,4}[-_ ]?\d{4,}\b")
_NON_WORD = re.compile(r"[^\w]+")
_TITLE_PRE_MAP = (
    # Indian boards spell it "Dot Net" and "Dot-Net" as often as ".NET".
    (re.compile(r"\bdot[\s-]*net\b", re.I), " dotnet "),
    (re.compile(r"c#", re.I), " csharp "),
    (re.compile(r"c\+\+", re.I), " cpp "),
    (re.compile(r"\.net", re.I), " dotnet "),
    (re.compile(r"node\.js", re.I), " nodejs "),
)
_TITLE_TOKEN_MAP = {
    "sde": "software engineer",
    "swe": "software engineer",
    "engg": "engineer",
    "dev": "developer",
    "sr": "senior",
    "jr": "junior",
    "ii": "2",
    "iii": "3",
    "iv": "4",
    "i": "1",
    "l2": "2",
    "l3": "3",
    "csharp": "c#",
    "dotnet": ".net",
    "cpp": "c++",
}
_TITLE_DROP = frozenset(
    {
        "remote",
        "hybrid",
        "onsite",
        "on",
        "site",
        "india",
        "wfh",
        "urgent",
        "hiring",
        "immediate",
        "joiner",
        "joiners",
        "opening",
        "openings",
        "job",
        "jobs",
        "position",
        "role",
        "the",
        "a",
        "an",
        "of",
        "for",
        "and",
    }
)


def canonical_url(url: str, keep_query: Iterable[str] = ()) -> str:
    """Lower-case host, drop tracking params and fragments, collapse LinkedIn job URLs."""
    text = (url or "").strip()
    linkedin_id = _linkedin_job_id(text)
    if linkedin_id:
        return _LINKEDIN_CANONICAL.format(job_id=linkedin_id)
    if "://" not in text:
        text = "https://" + text
    parts = urlsplit(text)
    indeed = _indeed_canonical(parts.netloc, text)
    if indeed:
        return indeed
    keep = {k.lower() for k in keep_query}
    query = sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False) if k.lower() in keep
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(
        (parts.scheme.lower() or "https", parts.netloc.lower(), path, urlencode(query), "")
    )


def _indeed_canonical(host: str, url: str) -> str | None:
    """Indeed click links (/rc/clk, /pagead/clk, /viewjob) all carry the job key as jk."""
    if not _INDEED_HOST.match(host.lower()):
        return None
    match = _INDEED_JK.search(url)
    if not match:
        return None
    return _INDEED_CANONICAL.format(host=host.lower(), jk=match.group(1).lower())


def indeed_job_key(url: str) -> str | None:
    match = _INDEED_JK.search(url or "")
    return match.group(1).lower() if match else None


def _linkedin_job_id(url: str) -> str | None:
    host = (urlsplit(url if "://" in url else "https://" + url).hostname or "").lower()
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return None
    match = _LINKEDIN_VIEW.search(url) or _LINKEDIN_CURRENT.search(url)
    return match.group(1) if match else None


def normalize_company(
    name: str, suffixes: Iterable[str] = (), drop_anywhere: Iterable[str] = ()
) -> str:
    text = (name or "").lower().replace("&", " and ")
    text = _NON_WORD.sub(" ", text)
    all_tokens = text.split()
    drop = set(drop_anywhere)
    tokens = [t for t in all_tokens if t not in drop]
    suffix_set = set(suffixes)
    while len(tokens) > 1 and tokens[-1] in suffix_set:
        tokens.pop()
    return " ".join(tokens or all_tokens)


def normalize_title(title: str) -> str:
    text = (title or "").lower()
    text = _PARENS.sub(" ", text)
    for pattern, replacement in _TITLE_PRE_MAP:
        text = pattern.sub(replacement, text)
    text = _REQ_ID.sub(" ", text)
    text = _NON_WORD.sub(" ", text)
    tokens: list[str] = []
    for token in text.split():
        mapped = _TITLE_TOKEN_MAP.get(token, token)
        if mapped in _TITLE_DROP:
            continue
        tokens.extend(mapped.split())
    return " ".join(tokens)


def is_remote_text(text: str | None) -> bool:
    return bool(text) and bool(_REMOTE_HINT.search(text or ""))


def location_bucket(
    text: str | None,
    aliases: Mapping[str, str],
    regions: Mapping[str, str] | None = None,
) -> str:
    """A city always wins over a country, so "Pune, India" is pune and a bare "India" is india."""
    if not text:
        return UNKNOWN_BUCKET
    lowered = text.lower()
    if _REMOTE_HINT.search(lowered):
        return REMOTE_BUCKET
    for table in (aliases, regions or {}):
        hit = _matching_alias(lowered, table)
        if hit is not None:
            return hit
    return UNKNOWN_BUCKET


def _matching_alias(lowered: str, table: Mapping[str, str]) -> str | None:
    """Longest alias first, so "new delhi" is not decided by "delhi"."""
    for alias in sorted(table, key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", lowered):
            return table[alias]
    return None


def fingerprint(company_norm: str, title_norm: str, bucket: str) -> str:
    digest = hashlib.sha1(f"{company_norm}|{title_norm}|{bucket}".encode()).hexdigest()
    return digest[:FINGERPRINT_LENGTH]


@dataclass(frozen=True, slots=True)
class Normalizer:
    company_suffixes: frozenset[str] = frozenset()
    company_drop_anywhere: frozenset[str] = frozenset()
    keep_query: frozenset[str] = frozenset()
    location_aliases: Mapping[str, str] = None  # type: ignore[assignment]
    region_aliases: Mapping[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.location_aliases is None:
            object.__setattr__(self, "location_aliases", {})
        if self.region_aliases is None:
            object.__setattr__(self, "region_aliases", {})

    def company(self, name: str) -> str:
        return normalize_company(name, self.company_suffixes, self.company_drop_anywhere)

    def title(self, title: str) -> str:
        return normalize_title(title)

    def url(self, url: str) -> str:
        return canonical_url(url, self.keep_query)

    def bucket(self, location: str | None, is_remote: bool = False) -> str:
        if is_remote:
            return REMOTE_BUCKET
        return location_bucket(location, self.location_aliases, self.region_aliases)

    def keys_for(self, posting: JobPosting) -> NormalizedKeys:
        company_norm = self.company(posting.company)
        title_norm = self.title(posting.title)
        bucket = self.bucket(posting.location, posting.is_remote)
        return NormalizedKeys(
            company_norm=company_norm,
            title_norm=title_norm,
            location_bucket=bucket,
            url_canonical=self.url(posting.url),
            fingerprint=fingerprint(company_norm, title_norm, bucket),
        )
