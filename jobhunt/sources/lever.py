"""Lever Postings API fetcher.

Public, unauthenticated. Pages via ``skip``/``limit`` until a short page comes back. Postings
carry their full HTML inline, so ``hydrate`` is a no-op. An unknown site returns HTTP 404,
which surfaces as FetchError; an empty board returns ``[]``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from jobhunt.clock import from_epoch_ms
from jobhunt.models import JobPosting
from jobhunt.sources.base import FetchError, Http, first_non_empty, html_to_md

log = logging.getLogger(__name__)

KEY = "lever"
POSTINGS_URL = "https://api.lever.co/v0/postings/{token}"
PAGE_SIZE = 100
MAX_PAGES = 50
REMOTE_WORKPLACE = "remote"
REMOTE_MARKER = "remote"
LOCATION_SEPARATOR = "; "
SECTION_HEADING = "## {title}"
SECTION_SEPARATOR = "\n\n"
RAW_EXCLUDED_FIELDS = frozenset(
    {
        "description",
        "descriptionPlain",
        "descriptionBody",
        "descriptionBodyPlain",
        "additional",
        "additionalPlain",
        "opening",
        "openingPlain",
        "lists",
    }
)


def fetch(cfg: Mapping[str, Any], http: Http) -> list[JobPosting]:
    token = str(cfg["token"])
    jobs = _fetch_all_pages(token, http)
    company_override = cfg.get("name")
    postings = [_to_posting(job, company_override, token) for job in jobs]
    log.info("lever %s: %d postings", token, len(postings))
    return postings


def hydrate(posting: JobPosting, http: Http) -> JobPosting:
    """Content is already inline; nothing to do."""
    return posting


def _fetch_all_pages(token: str, http: Http) -> list[dict[str, Any]]:
    url = POSTINGS_URL.format(token=token)
    jobs: list[dict[str, Any]] = []
    for page in range(MAX_PAGES):
        params = {"mode": "json", "limit": PAGE_SIZE, "skip": page * PAGE_SIZE}
        batch = http.get_json(url, params=params)
        if not isinstance(batch, list):
            raise FetchError(f"lever {token}: expected a JSON array, got {type(batch).__name__}")
        jobs.extend(batch)
        if len(batch) < PAGE_SIZE:
            return jobs
    log.warning("lever %s: stopped after %d pages", token, MAX_PAGES)
    return jobs


def _to_posting(job: Mapping[str, Any], company_override: str | None, token: str) -> JobPosting:
    categories = job.get("categories") or {}
    location = _location(categories)
    return JobPosting(
        source=KEY,
        source_job_id=str(job["id"]),
        company=first_non_empty(company_override, token) or token,
        title=job["text"],
        url=job["hostedUrl"],
        location=location,
        is_remote=_is_remote(job.get("workplaceType"), location),
        apply_url=job.get("applyUrl"),
        ats_type=KEY,
        description_md=_description(job),
        posted_at=from_epoch_ms(job.get("createdAt")),
        salary_text=_salary_text(job.get("salaryRange")),
        employment_type=categories.get("commitment"),
        raw={k: v for k, v in job.items() if k not in RAW_EXCLUDED_FIELDS},
    )


def _location(categories: Mapping[str, Any]) -> str | None:
    all_locations = [loc for loc in categories.get("allLocations") or [] if loc]
    if len(all_locations) > 1:
        return LOCATION_SEPARATOR.join(all_locations)
    return first_non_empty(categories.get("location"), *all_locations)


def _is_remote(workplace_type: str | None, location: str | None) -> bool:
    if (workplace_type or "").lower() == REMOTE_WORKPLACE:
        return True
    return bool(location) and REMOTE_MARKER in location.lower()


def _description(job: Mapping[str, Any]) -> str | None:
    sections = [html_to_md(job.get("description"))]
    for item in job.get("lists") or []:
        body = html_to_md(item.get("content"))
        title = (item.get("text") or "").strip()
        if title and body:
            sections.append(SECTION_HEADING.format(title=title) + "\n\n" + body)
        elif body:
            sections.append(body)
    sections.append(html_to_md(job.get("additional")))
    joined = SECTION_SEPARATOR.join(s for s in sections if s)
    return joined or None


def _salary_text(salary_range: Mapping[str, Any] | None) -> str | None:
    if not salary_range:
        return None
    bounds = [salary_range.get("min"), salary_range.get("max")]
    amounts = "-".join(_amount(v) for v in bounds if v is not None)
    if not amounts:
        return None
    interval = salary_range.get("interval")
    parts = [amounts, salary_range.get("currency"), f"per {interval}" if interval else None]
    return " ".join(p for p in parts if p)


def _amount(value: Any) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else str(number)
