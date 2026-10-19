"""Eightfold AI career sites (Microsoft and friends) via their public ``/api/pcsx`` JSON API.

List:   GET https://{host}/api/pcsx/search?domain={domain}&query=...&location=...&start=N
Detail: GET https://{host}/api/apply/v2/jobs/{id}?domain={domain}

The page size is fixed by the server (``num`` is ignored), so paging advances by however many
positions came back. ``domain`` is the registered tenant and is required on both calls, which is
why it travels in ``detail_ref`` as ``{domain}:{id}``: hydration runs from the database row, and
the row keeps no raw payload. A wrong ``domain`` answers 403 "Not authorized for PCSX".
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from jobhunt.models import JobPosting
from jobhunt.sources.base import Http, first_non_empty, html_to_md

log = logging.getLogger(__name__)

KEY = "eightfold"

SEARCH_URL = "https://{host}/api/pcsx/search"
DETAIL_URL = "https://{host}/api/apply/v2/jobs/{job_id}"
PUBLIC_URL = "https://{host}/careers/job/{job_id}"
MAX_PAGES = 30
HEADERS = {"Accept": "application/json"}
LOCATION_SEPARATOR = "; "
REMOTE_MARKERS = ("remote", "work from home")
REF_SEPARATOR = ":"

_HOST_RE = re.compile(r"^https?://(?P<host>[^/?#]+)/")


def fetch(cfg: Mapping[str, Any], http: Http) -> list[JobPosting]:
    host, domain = _target_from_cfg(cfg)
    company = str(cfg.get("name") or domain)
    params = {
        "domain": domain,
        "query": str(cfg.get("search_text") or ""),
        "location": str(cfg.get("location") or ""),
    }
    postings: list[JobPosting] = []
    start = 0
    for _ in range(MAX_PAGES):
        page = http.get_json(
            SEARCH_URL.format(host=host), params={**params, "start": start}, headers=HEADERS
        )
        data = _payload(page)
        positions = data.get("positions") or []
        parsed = (_parse_position(item, company, host, domain) for item in positions)
        postings.extend(p for p in parsed if p is not None)
        start += len(positions)
        if not positions or start >= int(data.get("count") or 0):
            break
    else:
        log.warning("eightfold %s: stopped after %s pages", domain, MAX_PAGES)
    return _dedupe(postings)


def hydrate(posting: JobPosting, http: Http) -> JobPosting:
    host = _host_from_url(posting.url)
    domain, job_id = _ref_parts(posting.detail_ref)
    detail = http.get_json(
        DETAIL_URL.format(host=host, job_id=job_id),
        params={"domain": domain},
        headers=HEADERS,
    )
    return replace(
        posting,
        description_md=html_to_md(detail.get("job_description")),
        location=_detail_location(detail) or posting.location,
        employment_type=first_non_empty(detail.get("type"), posting.employment_type),
        apply_url=first_non_empty(detail.get("canonicalPositionUrl"), posting.apply_url),
        posted_at=_epoch(detail.get("t_update")) or posting.posted_at,
        needs_hydration=False,
    )


def _payload(page: Any) -> Mapping[str, Any]:
    """Unwrap the ``{status, error, data}`` envelope; a filled error message means no results."""
    if not isinstance(page, Mapping):
        raise ValueError("eightfold search did not return an object")
    message = str((page.get("error") or {}).get("message") or "")
    if message:
        raise ValueError(f"eightfold search refused the request: {message}")
    data = page.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("eightfold search returned no data block")
    return data


def _parse_position(
    item: Mapping[str, Any], company: str, host: str, domain: str
) -> JobPosting | None:
    job_id = item.get("id")
    if not job_id:
        log.debug("eightfold %s: skipping position without an id: %r", company, item.get("name"))
        return None
    job_id = str(job_id)
    url = PUBLIC_URL.format(host=host, job_id=job_id)
    location = _join_locations(item.get("locations"))
    return JobPosting(
        source=KEY,
        source_job_id=f"{domain}{REF_SEPARATOR}{job_id}",
        company=company,
        title=str(item.get("name") or ""),
        url=url,
        location=location,
        is_remote=_looks_remote(item.get("workLocationOption")) or _looks_remote(location),
        apply_url=url,
        ats_type=KEY,
        posted_at=_epoch(item.get("postedTs")),
        needs_hydration=True,
        detail_ref=f"{domain}{REF_SEPARATOR}{job_id}",
        raw=dict(item),
    )


def _dedupe(postings: list[JobPosting]) -> list[JobPosting]:
    """Relevance ordering is not stable, so the same id can appear on two pages."""
    seen: set[str] = set()
    unique: list[JobPosting] = []
    for posting in postings:
        if posting.source_job_id in seen:
            continue
        seen.add(posting.source_job_id)
        unique.append(posting)
    if len(unique) != len(postings):
        log.debug("eightfold: dropped %s duplicate positions", len(postings) - len(unique))
    return unique


def _join_locations(locations: Any) -> str | None:
    if not isinstance(locations, list):
        return first_non_empty(str(locations) if locations else None)
    return LOCATION_SEPARATOR.join(str(loc) for loc in locations if loc) or None


def _detail_location(detail: Mapping[str, Any]) -> str | None:
    return _join_locations(detail.get("locations")) or first_non_empty(detail.get("location"))


def _looks_remote(value: Any) -> bool:
    text = str(value or "").lower()
    return any(marker in text for marker in REMOTE_MARKERS)


def _epoch(value: Any) -> datetime | None:
    """Eightfold timestamps are epoch seconds."""
    try:
        return datetime.fromtimestamp(int(value), UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _target_from_cfg(cfg: Mapping[str, Any]) -> tuple[str, str]:
    host, domain = cfg.get("host"), cfg.get("token")
    if not (host and domain):
        raise ValueError("eightfold source requires 'host' and 'token' (the Eightfold domain)")
    return str(host), str(domain)


def _host_from_url(url: str | None) -> str:
    match = _HOST_RE.match(url or "")
    if match is None:
        raise ValueError(f"cannot derive the eightfold host from {url!r}")
    return match.group("host")


def _ref_parts(detail_ref: str | None) -> tuple[str, str]:
    domain, _, job_id = (detail_ref or "").partition(REF_SEPARATOR)
    if not (domain and job_id):
        raise ValueError(f"eightfold detail_ref must be 'domain:id', got {detail_ref!r}")
    return domain, job_id
