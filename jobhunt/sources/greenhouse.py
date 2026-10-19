"""Greenhouse Job Board API fetcher.

Public, unauthenticated, single request per board (no pagination). Job content comes inline
as entity-escaped HTML, so ``hydrate`` is a no-op.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from jobhunt.clock import parse_iso
from jobhunt.models import JobPosting
from jobhunt.sources.base import Http, first_non_empty, html_to_md

log = logging.getLogger(__name__)

KEY = "greenhouse"
BOARD_JOBS_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
BOARD_JOBS_PARAMS = {"content": "true"}
REMOTE_MARKER = "remote"
RAW_EXCLUDED_FIELDS = frozenset({"content"})


def fetch(cfg: Mapping[str, Any], http: Http) -> list[JobPosting]:
    """Return every job on the board. A 404 (unknown token) surfaces as FetchError."""
    token = str(cfg["token"])
    payload = http.get_json(BOARD_JOBS_URL.format(token=token), params=BOARD_JOBS_PARAMS)
    jobs = payload.get("jobs") or []
    company_override = cfg.get("name")
    postings = [_to_posting(job, company_override, token) for job in jobs]
    log.info("greenhouse %s: %d postings", token, len(postings))
    return postings


def hydrate(posting: JobPosting, http: Http) -> JobPosting:
    """Content is already inline; nothing to do."""
    return posting


def _to_posting(job: Mapping[str, Any], company_override: str | None, token: str) -> JobPosting:
    location = (job.get("location") or {}).get("name")
    url = job["absolute_url"]
    company = first_non_empty(company_override, job.get("company_name"), token) or token
    return JobPosting(
        source=KEY,
        source_job_id=str(job["id"]),
        company=company,
        title=job["title"],
        url=url,
        location=location,
        is_remote=_is_remote(location),
        apply_url=url,
        ats_type=KEY,
        description_md=html_to_md(job.get("content")),
        posted_at=parse_iso(job.get("first_published") or job.get("updated_at")),
        raw={k: v for k, v in job.items() if k not in RAW_EXCLUDED_FIELDS},
    )


def _is_remote(location: str | None) -> bool:
    return bool(location) and REMOTE_MARKER in location.lower()
