"""Jobfound (jobfound.org) RSS feed: an aggregator that re-lists postings from other boards.

robots.txt content signals: ``search=yes, ai-train=no``. One feed request per scan. Hydration
fetches the jobfound job page, whose Next.js payload embeds a schema.org JobPosting with the real
``applyUrl``, and whose rendered ``div.prose`` block holds the full description.

Company and location are encoded in the link slug:
``/job/{company}-is-hiring-for-{title}-{location}-{d}-{month}-{yyyy}``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import feedparser
from bs4 import BeautifulSoup

from jobhunt.models import JobPosting
from jobhunt.sources.base import Http, first_non_empty, html_to_md

log = logging.getLogger(__name__)

KEY = "jobfound"

DEFAULT_FEED = "https://jobfound.org/feed.xml"
DEFAULT_COMPANY = "Unknown"
HIRING_MARKER = "-is-hiring-for-"
PROSE_SELECTOR = "div.prose"
STRUCT_TIME_DATETIME_FIELDS = 6
MIN_TITLE_TOKEN_MATCH_RATIO = 0.5
COUNTRY_NAMES = {"IN": "India", "US": "United States", "GB": "United Kingdom", "CA": "Canada"}

_DATE_SUFFIX = re.compile(r"-\d{1,2}-[a-z]+-\d{4}$")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_REMOTE = re.compile(r"\bremote\b", re.I)
# The JobPosting JSON sits inside a JS string, so its quotes arrive backslash-escaped.
_APPLY_URL = re.compile(r'\\?"applyUrl\\?"\s*:\s*\\?"(https?://[^"\\]+)')
_LOCALITY = re.compile(r'\\?"addressLocality\\?"\s*:\s*\\?"([^"\\]+)')
_COUNTRY = re.compile(r'\\?"addressCountry\\?"\s*:\s*\\?"([^"\\]+)')
_EMPLOYMENT = re.compile(r'\\?"employmentType\\?"\s*:\s*\\?"([^"\\]+)')
_JS_ESCAPES = (("\\u0026", "&"), ("\\/", "/"))


def fetch(cfg: Mapping[str, Any], http: Http) -> list[JobPosting]:
    feed_url = str(cfg.get("feed") or DEFAULT_FEED)
    parsed = feedparser.parse(http.get_text(feed_url))
    if parsed.bozo:
        log.warning("jobfound: feed %s is malformed: %s", feed_url, parsed.get("bozo_exception"))
    postings = [p for p in (_to_posting(entry) for entry in parsed.entries) if p is not None]
    log.info("jobfound: %d postings from %s", len(postings), feed_url)
    return postings


def hydrate(posting: JobPosting, http: Http) -> JobPosting:
    """Fetch the jobfound job page for the full description and the outbound apply link."""
    html = http.get_text(posting.detail_ref or posting.url)
    soup = BeautifulSoup(html, "html.parser")
    prose = soup.select_one(PROSE_SELECTOR)
    description = html_to_md(str(prose)) if prose is not None else None
    apply_url = _payload_value(_APPLY_URL, html)
    return replace(
        posting,
        description_md=description or posting.description_md,
        apply_url=apply_url or posting.apply_url,
        location=_payload_location(html) or posting.location,
        employment_type=first_non_empty(_payload_value(_EMPLOYMENT, html), posting.employment_type),
        needs_hydration=False,
    )


def parse_slug(slug: str, title: str) -> tuple[str, str | None]:
    """``acme-corp-is-hiring-for-backend-engineer-pune-india-8-september-2026`` ->
    ("Acme Corp", "pune india"). Unknown shapes give ("Unknown", None)."""
    head, marker, rest = slug.lower().partition(HIRING_MARKER)
    if not marker or not head:
        return DEFAULT_COMPANY, None
    company = head.replace("-", " ").strip().title() or DEFAULT_COMPANY
    rest_tokens = [t for t in _DATE_SUFFIX.sub("", rest).split("-") if t]
    title_tokens = [t for t in _NON_ALNUM.sub("-", title.lower()).split("-") if t]
    matched = 0
    while (
        matched < len(title_tokens)
        and matched < len(rest_tokens)
        and rest_tokens[matched] == title_tokens[matched]
    ):
        matched += 1
    if title_tokens and matched / len(title_tokens) < MIN_TITLE_TOKEN_MATCH_RATIO:
        return company, None
    location = " ".join(rest_tokens[matched:])
    return company, location or None


def _to_posting(entry: Mapping[str, Any]) -> JobPosting | None:
    link = first_non_empty(entry.get("link"))
    title = first_non_empty(entry.get("title"))
    guid = first_non_empty(entry.get("id")) or link
    if link is None or title is None or guid is None:
        log.debug("jobfound: skipping entry without link/title: %r", entry.get("title"))
        return None
    slug = urlsplit(link).path.rstrip("/").rsplit("/", 1)[-1]
    company, location = parse_slug(slug, title)
    return JobPosting(
        source=KEY,
        source_job_id=guid,
        company=company,
        title=title,
        url=link,
        location=location,
        is_remote=bool(_REMOTE.search(title) or _REMOTE.search(slug)),
        apply_url=None,
        ats_type=None,
        description_md=first_non_empty(entry.get("summary")),
        posted_at=_published_at(entry),
        needs_hydration=True,
        detail_ref=link,
        raw={"slug": slug, "category": first_non_empty(entry.get("category"))},
    )


def _payload_value(pattern: re.Pattern[str], html: str) -> str | None:
    match = pattern.search(html)
    if match is None:
        return None
    value = match.group(1)
    for escaped, plain in _JS_ESCAPES:
        value = value.replace(escaped, plain)
    return value.strip() or None


def _payload_location(html: str) -> str | None:
    locality = _payload_value(_LOCALITY, html)
    country = _payload_value(_COUNTRY, html)
    country_name = COUNTRY_NAMES.get(country or "", country)
    return first_non_empty(
        f"{locality}, {country_name}" if locality and country_name else None, locality, country_name
    )


def _published_at(entry: Mapping[str, Any]) -> datetime | None:
    struct = entry.get("published_parsed")
    if struct is not None:
        return datetime(*struct[:STRUCT_TIME_DATETIME_FIELDS], tzinfo=UTC)
    value = entry.get("published")
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(str(value))
    except (TypeError, ValueError):
        log.debug("jobfound: unparseable date %r", value)
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
