"""Ashby public posting API fetcher.

Public, unauthenticated, single request per board (no pagination). Descriptions come inline as
HTML, so ``hydrate`` is a no-op. Unlisted jobs are skipped.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from jobhunt.clock import parse_iso
from jobhunt.models import JobPosting
from jobhunt.sources.base import Http, first_non_empty, html_to_md

log = logging.getLogger(__name__)

KEY = "ashby"
BOARD_URL = "https://api.ashbyhq.com/posting-api/job-board/{token}"
BOARD_PARAMS = {"includeCompensation": "true"}
REMOTE_WORKPLACE = "Remote"
LOCATION_SEPARATOR = "; "
COMPENSATION_SUMMARY_FIELDS = ("compensationTierSummary", "scrapeableCompensationSalarySummary")
RAW_EXCLUDED_FIELDS = frozenset({"descriptionHtml", "descriptionPlain"})


def fetch(cfg: Mapping[str, Any], http: Http) -> list[JobPosting]:
    token = str(cfg["token"])
    payload = http.get_json(BOARD_URL.format(token=token), params=BOARD_PARAMS)
    jobs = payload.get("jobs") or []
    company_override = cfg.get("name")
    postings = [
        _to_posting(job, company_override, token) for job in jobs if job.get("isListed", True)
    ]
    log.info("ashby %s: %d listed of %d postings", token, len(postings), len(jobs))
    return postings


def hydrate(posting: JobPosting, http: Http) -> JobPosting:
    """Content is already inline; nothing to do."""
    return posting


def _to_posting(job: Mapping[str, Any], company_override: str | None, token: str) -> JobPosting:
    return JobPosting(
        source=KEY,
        source_job_id=str(job["id"]),
        company=first_non_empty(company_override, token) or token,
        title=job["title"],
        url=job["jobUrl"],
        location=_location(job),
        is_remote=bool(job.get("isRemote")) or job.get("workplaceType") == REMOTE_WORKPLACE,
        apply_url=job.get("applyUrl"),
        ats_type=KEY,
        description_md=html_to_md(job.get("descriptionHtml")),
        posted_at=parse_iso(job.get("publishedAt")),
        salary_text=_salary_text(job.get("compensation")),
        employment_type=job.get("employmentType"),
        raw={k: v for k, v in job.items() if k not in RAW_EXCLUDED_FIELDS},
    )


def _location(job: Mapping[str, Any]) -> str | None:
    names = [job.get("location")]
    names.extend(entry.get("location") for entry in job.get("secondaryLocations") or [])
    unique: list[str] = []
    for name in names:
        if name and name not in unique:
            unique.append(name)
    return LOCATION_SEPARATOR.join(unique) or None


def _salary_text(compensation: Mapping[str, Any] | None) -> str | None:
    if not compensation:
        return None
    return first_non_empty(*(compensation.get(field) for field in COMPENSATION_SUMMARY_FIELDS))
