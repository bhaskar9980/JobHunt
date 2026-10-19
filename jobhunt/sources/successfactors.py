"""SAP SuccessFactors career sites via their Google-for-Jobs product feed.

`GET https://{host}/jobfeed.xml` (some tenants publish the same document at `/sitemap.xml`)
returns RSS 2.0 where each `<item>` carries the full job description inline, plus
`g:id`, `g:location`, `g:employer`, `g:job_function` and `g:expiration_date`. Because the
description is inline there is nothing to hydrate, so a posting is scoreable on first fetch.

The feeds are whole-tenant dumps of 0.1-18 MB, which is fine once a day but not more often.
Tenants also append the location to the title ("Azure Integration Lead (Bengaluru, IND-29, IN)")
and some report the location as a bare ISO country code.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import date
from typing import Any
from urllib.parse import urlsplit
from xml.etree import ElementTree

from jobhunt.models import JobPosting
from jobhunt.sources.base import Http, first_non_empty, html_to_md

log = logging.getLogger(__name__)

KEY = "successfactors"

GOOGLE_NS = "{http://base.google.com/ns/1.0}"
DEFAULT_FEED_PATH = "/jobfeed.xml"
DESCRIPTION_MAX_CHARS = 20_000
# Feeds that report only a country. Enough to cover the tenants on the watchlist.
COUNTRY_CODES = {
    "IN": "India",
    "US": "United States",
    "GB": "United Kingdom",
    "UK": "United Kingdom",
    "CA": "Canada",
    "AU": "Australia",
    "DE": "Germany",
    "SG": "Singapore",
    "PH": "Philippines",
    "MX": "Mexico",
    "BR": "Brazil",
    "PL": "Poland",
    "JP": "Japan",
    "CN": "China",
}


def today() -> date:
    """Injected in tests so an expiry check does not depend on the calendar."""
    return date.today()


def fetch(cfg: Mapping[str, Any], http: Http) -> list[JobPosting]:
    url = _feed_url(cfg)
    company = str(cfg.get("name") or "")
    terms = _terms(cfg.get("search_text"))
    items = _items(http.get_text(url), url)
    parsed = (_to_posting(item, company) for item in items)
    postings = [p for p in parsed if p is not None and _matches(p, terms)]
    log.info(
        "successfactors %s: %s of %s items kept (terms=%s)",
        company,
        len(postings),
        len(items),
        ",".join(terms) or "none",
    )
    return postings


def _terms(search_text: Any) -> list[str]:
    """A whole-tenant dump has no server-side query, so the board's search runs here."""
    return [term.strip().lower() for term in str(search_text or "").split(",") if term.strip()]


def _matches(posting: JobPosting, terms: list[str]) -> bool:
    if not terms:
        return True
    haystack = f"{posting.title}\n{posting.description_md or ''}".lower()
    return any(term in haystack for term in terms)


def hydrate(posting: JobPosting, http: Http) -> JobPosting:  # noqa: ARG001 - contract
    """No-op: the feed already carries the whole description."""
    return posting


def expand_location(raw: str | None) -> str | None:
    """Name the country these feeds only give as a code.

    "IN" becomes "India". "Chennai, IND-32, IN" keeps its city but gains the country name, so
    the location buckets as India even when the city is not in ``locations.yaml`` yet.
    """
    text = (raw or "").strip()
    if not text:
        return None
    whole = COUNTRY_CODES.get(text.upper())
    if whole:
        return whole
    country = _trailing_country(text)
    return f"{text}, {country}" if country else text


def _trailing_country(text: str) -> str | None:
    """The last comma-separated part, when it is a country code and not already spelled out."""
    parts = [part.strip() for part in text.split(",") if part.strip()]
    for part in reversed(parts):
        country = COUNTRY_CODES.get(part.upper())
        if country:
            return None if country.lower() in text.lower() else country
    return None


def _feed_url(cfg: Mapping[str, Any]) -> str:
    feed = str(cfg.get("feed") or DEFAULT_FEED_PATH)
    if feed.startswith("http"):
        return feed
    host = cfg.get("host")
    if not host:
        raise ValueError("successfactors source requires 'host' (or a full 'feed' url)")
    return f"https://{host}{feed if feed.startswith('/') else '/' + feed}"


def _items(body: str, url: str) -> list[ElementTree.Element]:
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as exc:
        raise ValueError(f"could not parse the feed at {url}: {exc}") from exc
    items = root.findall(".//item")
    if not items:
        raise ValueError(f"no RSS items in the feed at {url}")
    return items


def _to_posting(item: ElementTree.Element, company: str) -> JobPosting | None:
    job_id = first_non_empty(item.findtext(GOOGLE_NS + "id"), item.findtext("guid"))
    link = first_non_empty(item.findtext("link"))
    if not (job_id and link):
        log.debug("successfactors %s: dropping item without id or link", company)
        return None
    if _expired(item):
        return None
    # The title carries the location exactly as the feed spells it, so clean against the raw
    # value and store the expanded one.
    raw_location = item.findtext(GOOGLE_NS + "location")
    location = expand_location(raw_location)
    return JobPosting(
        source=KEY,
        source_job_id=f"{urlsplit(link).hostname}:{company.casefold()}:{job_id}",
        company=company,
        title=_clean_title(item.findtext("title"), raw_location),
        url=link,
        location=location,
        is_remote=False,
        apply_url=link,
        ats_type=KEY,
        description_md=html_to_md(item.findtext("description"), DESCRIPTION_MAX_CHARS),
        needs_hydration=False,
        raw={
            "employer": item.findtext(GOOGLE_NS + "employer"),
            "job_function": item.findtext(GOOGLE_NS + "job_function"),
            "expiration_date": item.findtext(GOOGLE_NS + "expiration_date"),
        },
    )


def _expired(item: ElementTree.Element) -> bool:
    raw = (item.findtext(GOOGLE_NS + "expiration_date") or "").strip()
    if not raw:
        return False
    try:
        expires = date.fromisoformat(raw)
    except ValueError:
        log.debug("successfactors: unparseable expiration_date %r", raw)
        return False
    return expires < today()


def _clean_title(raw: str | None, location: str | None) -> str:
    """Drop the trailing "(location)" tenants append, but keep parentheses that name the role."""
    title = (raw or "").strip()
    if not location:
        return title
    suffix = f"({location})"
    if title.endswith(suffix):
        return title[: -len(suffix)].strip()
    return title
