"""Remotive public job feed. One request per fetch; their terms allow four calls per day and
require attribution, which is appended to every description.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from jobhunt.clock import parse_iso
from jobhunt.models import JobPosting
from jobhunt.sources.base import Http, first_non_empty, html_to_md

log = logging.getLogger(__name__)

KEY = "remotive"

API_URL = "https://remotive.com/api/remote-jobs"
DEFAULT_CATEGORY = "software-dev"
DEFAULT_LIMIT = 200
DEFAULT_COMPANY = "Unknown"
DEFAULT_KEEP_CATEGORIES = ("software development",)
ATTRIBUTION_LINE = "Source: Remotive ({url})"


def fetch(cfg: Mapping[str, Any], http: Http) -> list[JobPosting]:
    params = {
        "category": str(cfg.get("category", DEFAULT_CATEGORY)),
        "limit": int(cfg.get("limit", DEFAULT_LIMIT)),
    }
    data = http.get_json(API_URL, params=params)
    jobs = data.get("jobs") or []
    log.info("remotive: %d jobs returned (job-count=%s)", len(jobs), data.get("job-count"))
    keep = {str(c).lower() for c in cfg.get("keep_categories", DEFAULT_KEEP_CATEGORIES)}
    kept = [job for job in jobs if _category_ok(job, keep)]
    if len(kept) != len(jobs):
        log.info("remotive: %d jobs outside %s dropped client-side", len(jobs) - len(kept), keep)
    return [posting for posting in map(_to_posting, kept) if posting is not None]


def _category_ok(job: Mapping[str, Any], keep: set[str]) -> bool:
    """The server ignores the category param, so filter here; keep jobs with no category."""
    category = job.get("category")
    return not keep or category is None or str(category).lower() in keep


def hydrate(posting: JobPosting, http: Http) -> JobPosting:  # noqa: ARG001 - contract
    return posting


def _to_posting(job: Mapping[str, Any]) -> JobPosting | None:
    url = first_non_empty(job.get("url"))
    title = first_non_empty(job.get("title"))
    job_id = job.get("id")
    if url is None or title is None or job_id is None:
        log.debug("remotive: skipping job without id/url/title: %r", job.get("id"))
        return None
    return JobPosting(
        source=KEY,
        source_job_id=str(job_id),
        company=first_non_empty(job.get("company_name")) or DEFAULT_COMPANY,
        title=title,
        url=url,
        location=first_non_empty(job.get("candidate_required_location")),
        is_remote=True,
        apply_url=url,
        ats_type=None,
        description_md=_describe(job.get("description"), url),
        posted_at=parse_iso(job.get("publication_date")),
        salary_text=first_non_empty(job.get("salary")),
        employment_type=first_non_empty(job.get("job_type")),
        raw={"category": job.get("category"), "tags": job.get("tags")},
    )


def _describe(description_html: str | None, url: str) -> str:
    attribution = ATTRIBUTION_LINE.format(url=url)
    body = html_to_md(description_html)
    return f"{body}\n\n{attribution}" if body else attribution
