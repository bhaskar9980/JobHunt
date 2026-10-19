"""SmartRecruiters public postings API (no auth).

List:   GET https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100&offset=N
Detail: GET {ref}  (the ``ref`` URL from each list item; adds ``jobAd.sections`` HTML)
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from jobhunt.clock import parse_iso
from jobhunt.models import JobPosting
from jobhunt.sources.base import Http, first_non_empty, html_to_md

log = logging.getLogger(__name__)

KEY = "smartrecruiters"

LIST_URL = "https://api.smartrecruiters.com/v1/companies/{token}/postings"
DETAIL_URL = LIST_URL + "/{posting_id}"
PUBLIC_URL = "https://jobs.smartrecruiters.com/{company}/{posting_id}"
PAGE_SIZE = 100
MAX_PAGES = 20
SECTION_ORDER = (
    "companyDescription",
    "jobDescription",
    "qualifications",
    "additionalInformation",
)

_PUBLIC_URL_RE = re.compile(
    r"^https://jobs\.smartrecruiters\.com/(?P<company>[^/?#]+)/(?P<posting_id>\d+)"
)


def fetch(cfg: Mapping[str, Any], http: Http) -> list[JobPosting]:
    token = cfg.get("token")
    if not token:
        raise ValueError("smartrecruiters source requires 'token'")
    company = str(cfg.get("name") or token)
    list_url = LIST_URL.format(token=token)
    postings: list[JobPosting] = []
    offset = 0
    for _ in range(MAX_PAGES):
        page = http.get_json(list_url, params={"limit": PAGE_SIZE, "offset": offset})
        content = page.get("content") or []
        postings.extend(_parse_item(item, company, token) for item in content)
        offset += len(content)
        if not content or offset >= int(page.get("totalFound") or 0):
            break
    else:
        log.warning("smartrecruiters %s: stopped after %s pages", token, MAX_PAGES)
    return postings


def hydrate(posting: JobPosting, http: Http) -> JobPosting:
    detail_url = posting.detail_ref or _detail_url_from_posting(posting)
    detail = http.get_json(detail_url)
    sections = (detail.get("jobAd") or {}).get("sections") or {}
    return replace(
        posting,
        description_md=_sections_to_md(sections),
        apply_url=first_non_empty(detail.get("applyUrl"), posting.apply_url),
        url=first_non_empty(detail.get("postingUrl"), posting.url) or posting.url,
        employment_type=first_non_empty(
            _label(detail.get("typeOfEmployment")), posting.employment_type
        ),
        posted_at=parse_iso(detail.get("releasedDate")) or posting.posted_at,
        needs_hydration=False,
    )


def _parse_item(item: Mapping[str, Any], company: str, token: str) -> JobPosting:
    posting_id = str(item["id"])
    location = item.get("location") or {}
    identifier = (item.get("company") or {}).get("identifier") or token
    detail_ref = item.get("ref") or DETAIL_URL.format(token=token, posting_id=posting_id)
    return JobPosting(
        source=KEY,
        source_job_id=posting_id,
        company=company,
        title=str(item.get("name") or ""),
        url=PUBLIC_URL.format(company=identifier, posting_id=posting_id),
        location=_location_text(location),
        is_remote=bool(location.get("remote")),
        ats_type=KEY,
        posted_at=parse_iso(item.get("releasedDate")),
        employment_type=_label(item.get("typeOfEmployment")),
        needs_hydration=True,
        detail_ref=detail_ref,
        raw=dict(item),
    )


def _location_text(location: Mapping[str, Any]) -> str | None:
    """``fullLocation`` when present (it may contain empty segments: "Paris, , France")."""
    full = str(location.get("fullLocation") or "")
    cleaned = ", ".join(part.strip() for part in full.split(",") if part.strip())
    parts = (location.get("city"), location.get("region"), location.get("country"))
    joined = ", ".join(str(part) for part in parts if part)
    return first_non_empty(cleaned, joined)


def _label(field: Mapping[str, Any] | None) -> str | None:
    if not field:
        return None
    return first_non_empty(field.get("label"))


def _sections_to_md(sections: Mapping[str, Any]) -> str | None:
    ordered = [key for key in SECTION_ORDER if key in sections]
    ordered += [key for key in sections if key not in SECTION_ORDER]
    parts: list[str] = []
    for key in ordered:
        section = sections.get(key) or {}
        body = html_to_md(section.get("text"))
        if not body:
            continue
        title = first_non_empty(section.get("title")) or key
        parts.append(f"## {title}\n{body}")
    return "\n\n".join(parts) or None


def _detail_url_from_posting(posting: JobPosting) -> str:
    match = _PUBLIC_URL_RE.match(posting.url or "")
    if match is None:
        raise ValueError(f"cannot derive smartrecruiters detail URL from {posting.url!r}")
    return DETAIL_URL.format(
        token=match.group("company"), posting_id=posting.source_job_id or match.group("posting_id")
    )
