"""Tal "jobs in the wild" (jobsinthewild.tal.af): LinkedIn/X hiring posts curated for Bangalore.

Discovery only. Every post links back to the original LinkedIn or X post, so applying is always
manual and the LinkedIn link is never followed by code. Budget: one request per configured query
per scan plus one per hydrated card. No paging (robots.txt disallows ``/feed/page``).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from bs4 import BeautifulSoup, Tag

from jobhunt import clock
from jobhunt.models import JobPosting
from jobhunt.sources.base import Http

log = logging.getLogger(__name__)

KEY = "tal"

DEFAULT_BASE_URL = "https://jobsinthewild.tal.af/"
DEFAULT_QUERIES = ("sort=new",)
DEFAULT_COMPANY = "Unknown"
CARD_SELECTOR = "article.post-frame[data-href]"
DETAIL_ARTICLE_SELECTOR = "article.post-frame"
TAG_SELECTOR = ".tag-strip .tag"
INDIA_SUFFIX = ", India"
ORIGINAL_POST_MARKERS = ("linkedin.com/posts/", "x.com/", "twitter.com/")
WORK_MODE_TAGS = frozenset({"remote", "hybrid", "in-office", "onsite", "on-site"})
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY = 86_400
SECONDS_PER_WEEK = 7 * SECONDS_PER_DAY
SECONDS_PER_MONTH = 30 * SECONDS_PER_DAY
UNIT_SECONDS = {
    "mo": SECONDS_PER_MONTH,
    "m": SECONDS_PER_MINUTE,
    "h": SECONDS_PER_HOUR,
    "d": SECONDS_PER_DAY,
    "w": SECONDS_PER_WEEK,
}

_POST_ID = re.compile(r"/j/(\d+)")
_RELATIVE = re.compile(r"(?P<n>\d+)\s*(?P<unit>mo|m|h|d|w)\b", re.I)
_JUST_NOW = re.compile(r"\b(just now|now)\b", re.I)
_HTTP_LINK = re.compile(r"https?://[^\s<>\"')\]]+")
_REMOTE = re.compile(r"\bremote\b", re.I)


def fetch(cfg: Mapping[str, Any], http: Http) -> list[JobPosting]:
    base_url = str(cfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/") + "/"
    queries = [str(q) for q in (cfg.get("queries") or DEFAULT_QUERIES)]
    default_location = cfg.get("default_location")
    reference = clock.now()
    seen: set[str] = set()
    postings: list[JobPosting] = []
    for query in queries:
        soup = BeautifulSoup(http.get_text(f"{base_url}?{query}"), "html.parser")
        for card in soup.select(CARD_SELECTOR):
            posting = _card_to_posting(card, base_url, query, default_location, reference)
            if posting is None or posting.source_job_id in seen:
                continue
            seen.add(posting.source_job_id)
            postings.append(posting)
    log.info("tal: %d postings from %d queries", len(postings), len(queries))
    return postings


def hydrate(posting: JobPosting, http: Http) -> JobPosting:
    """Fetch the post page: full body, tags (role, level, city, stage), original post link."""
    soup = BeautifulSoup(http.get_text(posting.url), "html.parser")
    article = soup.select_one(DETAIL_ARTICLE_SELECTOR) or soup
    body = article.select_one(".body")
    description = body.get_text("\n", strip=True) if body is not None else None
    tags = [tag.get_text(strip=True) for tag in article.select(TAG_SELECTOR)]
    city = _city_tag(article)
    apply_hint = _HTTP_LINK.search(description or "")
    return replace(
        posting,
        description_md=description or posting.description_md,
        apply_url=_original_post_url(article) or posting.apply_url,
        location=f"{city}{INDIA_SUFFIX}" if city else posting.location,
        is_remote=posting.is_remote or any(t.lower() == "remote" for t in tags),
        needs_hydration=False,
        raw={
            **posting.raw,
            "tags": tags,
            "apply_hint": apply_hint.group(0).rstrip(".,;") if apply_hint else None,
        },
    )


def parse_relative(text: str | None, reference: datetime) -> datetime | None:
    """'8h ago', '1d ago', '2w ago', '3mo ago', 'just now' -> tz-aware datetime."""
    if not text:
        return None
    if _JUST_NOW.search(text):
        return reference
    match = _RELATIVE.search(text)
    if match is None:
        return None
    seconds = int(match.group("n")) * UNIT_SECONDS[match.group("unit").lower()]
    return reference - timedelta(seconds=seconds)


def _card_to_posting(
    card: Tag,
    base_url: str,
    query: str,
    default_location: Any,
    reference: datetime,
) -> JobPosting | None:
    match = _POST_ID.search(str(card.get("data-href") or ""))
    if match is None:
        return None
    post_id = match.group(1)
    title = _text(card, ".role-chip-role") or _text(card, ".role-name")
    if not title:
        log.debug("tal: skipping card %s without a role", post_id)
        return None
    body = card.select_one(".body")
    when = _text(card, ".when")
    return JobPosting(
        source=KEY,
        source_job_id=post_id,
        company=_text(card, ".role-chip-company") or DEFAULT_COMPANY,
        title=title,
        url=f"{base_url}j/{post_id}",
        location=str(default_location) if default_location else None,
        is_remote=bool(_REMOTE.search(card.get_text(" ", strip=True))),
        apply_url=None,
        ats_type=None,
        description_md=body.get_text("\n", strip=True) if body is not None else None,
        posted_at=parse_relative(when, reference),
        needs_hydration=True,
        detail_ref=f"/j/{post_id}",
        raw={
            "author": _text(card, ".name"),
            "headline": _text(card, ".headline"),
            "when": when,
            "query": query,
        },
    )


def _text(root: Tag, selector: str) -> str | None:
    node = root.select_one(selector)
    if node is None:
        return None
    text = node.get_text(" ", strip=True)
    return text or None


def _city_tag(article: Tag) -> str | None:
    """The city is the one tag without a role/seniority/stage modifier class."""
    for span in article.select(TAG_SELECTOR):
        classes = [c for c in (span.get("class") or []) if c != "tag"]
        text = span.get_text(strip=True)
        if not classes and text and text.lower() not in WORK_MODE_TAGS:
            return text
    return None


def _original_post_url(article: Tag) -> str | None:
    for anchor in article.select("a[href]"):
        href = str(anchor.get("href") or "")
        if any(marker in href for marker in ORIGINAL_POST_MARKERS):
            return href
    return None
