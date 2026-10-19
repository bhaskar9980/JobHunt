"""We Work Remotely category RSS feeds.

Item titles are ``Company: Job Title``. WWR adds custom elements (``region``, ``country``,
``type``, ``skills``, ``category``, ``expires_at``) which feedparser exposes as plain entry keys.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import feedparser

from jobhunt import clock
from jobhunt.models import JobPosting
from jobhunt.sources.base import Http, first_non_empty, html_to_md

log = logging.getLogger(__name__)

KEY = "wwr"

DEFAULT_COMPANY = "Unknown"
TITLE_SEPARATOR = ": "
STRUCT_TIME_DATETIME_FIELDS = 6


def fetch(cfg: Mapping[str, Any], http: Http) -> list[JobPosting]:
    feeds = [str(url) for url in cfg.get("feeds") or []]
    now = clock.now()
    seen: set[str] = set()
    postings: list[JobPosting] = []
    for feed_url in feeds:
        for posting in _fetch_feed(feed_url, http, now):
            if posting.source_job_id in seen:
                continue
            seen.add(posting.source_job_id)
            postings.append(posting)
    log.info("wwr: %d postings from %d feeds", len(postings), len(feeds))
    return postings


def hydrate(posting: JobPosting, http: Http) -> JobPosting:  # noqa: ARG001 - contract
    return posting


def _fetch_feed(feed_url: str, http: Http, now: datetime) -> list[JobPosting]:
    parsed = feedparser.parse(http.get_text(feed_url))
    if parsed.bozo:
        log.warning("wwr: feed %s is malformed: %s", feed_url, parsed.get("bozo_exception"))
    postings: list[JobPosting] = []
    for entry in parsed.entries:
        posting = _to_posting(entry, feed_url, now)
        if posting is not None:
            postings.append(posting)
    return postings


def _to_posting(entry: Mapping[str, Any], feed_url: str, now: datetime) -> JobPosting | None:
    link = first_non_empty(entry.get("link"))
    guid = first_non_empty(entry.get("id")) or link
    if guid is None:
        return None
    expires_at = _parse_rfc822(entry.get("expires_at"))
    if expires_at is not None and expires_at < now:
        log.debug("wwr: skipping expired entry %s", guid)
        return None
    company, title = _split_title(str(entry.get("title") or ""))
    return JobPosting(
        source=KEY,
        source_job_id=guid,
        company=company,
        title=title,
        url=link or guid,
        location=first_non_empty(entry.get("region"), entry.get("country")),
        is_remote=True,
        apply_url=link or guid,
        ats_type=None,
        description_md=html_to_md(entry.get("summary")),
        posted_at=_published_at(entry),
        employment_type=first_non_empty(entry.get("type")),
        raw={
            "feed": feed_url,
            "category": entry.get("category"),
            "skills": first_non_empty(entry.get("skills")),
            "region": first_non_empty(entry.get("region")),
            "country": first_non_empty(entry.get("country")),
            "expires_at": clock.to_iso(expires_at) if expires_at else None,
        },
    )


def _split_title(title: str) -> tuple[str, str]:
    company, separator, role = title.partition(TITLE_SEPARATOR)
    if not separator:
        return DEFAULT_COMPANY, title.strip()
    return company.strip() or DEFAULT_COMPANY, role.strip() or title.strip()


def _published_at(entry: Mapping[str, Any]) -> datetime | None:
    struct = entry.get("published_parsed")
    if struct is not None:
        return datetime(*struct[:STRUCT_TIME_DATETIME_FIELDS], tzinfo=UTC)
    return _parse_rfc822(entry.get("published"))


def _parse_rfc822(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        log.debug("wwr: unparseable date %r", value)
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
