"""Workday career sites via the undocumented but stable ``/wday/cxs`` JSON API.

List:   POST https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
        body {"appliedFacets": {}, "limit": 20, "offset": N, "searchText": "..."}
Detail: GET  https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{externalPath}

``limit`` is hard-capped at 20 by Workday. ``postedOn`` is display text ("Posted 3 Days Ago").
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

from jobhunt.clock import now, parse_iso
from jobhunt.models import JobPosting
from jobhunt.sources.base import Http, first_non_empty, html_to_md

log = logging.getLogger(__name__)

KEY = "workday"

HOST_URL = "https://{tenant}.{wd}.myworkdayjobs.com"
LIST_URL = HOST_URL + "/wday/cxs/{tenant}/{site}/jobs"
DETAIL_URL = HOST_URL + "/wday/cxs/{tenant}/{site}{path}"
PUBLIC_URL = HOST_URL + "/en-US/{site}{path}"
APPLY_SUFFIX = "/apply"
PAGE_SIZE = 20
MAX_PAGES = 30
HEADERS = {"Accept": "application/json", "Accept-Language": "en-US"}
LOCATION_SEPARATOR = "; "
REMOTE_MARKER = "remote"
DAYS_TODAY = 0
DAYS_YESTERDAY = 1

_POSTED_ON_RE = re.compile(
    r"posted\s+(?:(?P<today>today)|(?P<yesterday>yesterday)|(?P<days>\d+)\s*\+?\s*days?\s+ago)",
    re.IGNORECASE,
)
_PUBLIC_URL_RE = re.compile(
    r"^https://(?P<tenant>[^./]+)\.(?P<wd>wd\d+)\.myworkdayjobs\.com/"
    r"(?:[a-zA-Z]{2}-[a-zA-Z]{2}/)?(?P<site>[^/?#]+)(?P<path>/job/[^?#]*)"
)


class Site(NamedTuple):
    tenant: str
    wd: str
    site: str

    def format(self, template: str, **extra: str) -> str:
        return template.format(tenant=self.tenant, wd=self.wd, site=self.site, **extra)


def fetch(cfg: Mapping[str, Any], http: Http) -> list[JobPosting]:
    site = _site_from_cfg(cfg)
    company = str(cfg.get("name") or site.tenant)
    search_text = str(cfg.get("search_text") or "")
    list_url = site.format(LIST_URL)
    reference = now()
    postings: list[JobPosting] = []
    offset = 0
    total = 0
    for _ in range(MAX_PAGES):
        body = {
            "appliedFacets": {},
            "limit": PAGE_SIZE,
            "offset": offset,
            "searchText": search_text,
        }
        page = http.post_json(list_url, body, headers=HEADERS)
        items = page.get("jobPostings") or []
        parsed = (_parse_item(item, company, site, reference) for item in items)
        postings.extend(p for p in parsed if p is not None)
        offset += PAGE_SIZE
        # Workday only reports ``total`` on the first page; later pages say 0.
        total = int(page.get("total") or 0) or total
        if not items or offset >= total:
            break
    else:
        log.warning("workday %s/%s: stopped after %s pages", site.tenant, site.site, MAX_PAGES)
    return _dedupe(postings)


def _dedupe(postings: list[JobPosting]) -> list[JobPosting]:
    """Workday paging is not stable; the same externalPath can straddle two pages."""
    seen: set[str] = set()
    unique: list[JobPosting] = []
    for posting in postings:
        if posting.source_job_id in seen:
            continue
        seen.add(posting.source_job_id)
        unique.append(posting)
    if len(unique) != len(postings):
        log.debug("workday: dropped %s duplicate postings", len(postings) - len(unique))
    return unique


def hydrate(posting: JobPosting, http: Http) -> JobPosting:
    site, url_path = _site_from_url(posting.url)
    path = posting.detail_ref or url_path
    detail = http.get_json(site.format(DETAIL_URL, path=path), headers=HEADERS)
    info = detail.get("jobPostingInfo") or {}
    external_url = first_non_empty(info.get("externalUrl"))
    return replace(
        posting,
        description_md=html_to_md(info.get("jobDescription")),
        location=_detail_location(info) or posting.location,
        employment_type=first_non_empty(info.get("timeType"), posting.employment_type),
        apply_url=external_url + APPLY_SUFFIX if external_url else posting.apply_url,
        posted_at=_detail_posted_at(info) or posting.posted_at,
        needs_hydration=False,
    )


def _parse_item(
    item: Mapping[str, Any], company: str, site: Site, reference: datetime
) -> JobPosting | None:
    raw_path = item.get("externalPath")
    if not raw_path:
        log.debug("workday %s: skipping item without externalPath: %r", company, item.get("title"))
        return None
    path = str(raw_path)
    url = site.format(PUBLIC_URL, path=path)
    location = first_non_empty(item.get("locationsText"))
    bullets = item.get("bulletFields") or []
    return JobPosting(
        source=KEY,
        source_job_id=f"{site.tenant}:{site.site}:{path}",
        company=company,
        title=str(item.get("title") or ""),
        url=url,
        location=location,
        is_remote=_looks_remote(location) or _looks_remote(path),
        apply_url=url + APPLY_SUFFIX,
        ats_type=KEY,
        posted_at=_parse_posted_on(item.get("postedOn"), reference),
        needs_hydration=True,
        detail_ref=path,
        raw={**item, "req_id": bullets[0] if bullets else None},
    )


def _parse_posted_on(text: str | None, reference: datetime) -> datetime | None:
    """Turn Workday's display text ("Posted 3 Days Ago", "Posted 30+ Days Ago") into a datetime."""
    if not text:
        return None
    match = _POSTED_ON_RE.search(text)
    if match is None:
        return None
    if match.group("today"):
        days = DAYS_TODAY
    elif match.group("yesterday"):
        days = DAYS_YESTERDAY
    else:
        days = int(match.group("days"))
    return reference - timedelta(days=days)


def _looks_remote(location: str | None) -> bool:
    return REMOTE_MARKER in (location or "").lower()


def _site_from_cfg(cfg: Mapping[str, Any]) -> Site:
    tenant, wd, site = cfg.get("tenant"), cfg.get("wd"), cfg.get("site")
    if not (tenant and wd and site):
        raise ValueError("workday source requires 'tenant', 'wd' and 'site'")
    return Site(str(tenant), str(wd), str(site))


def _site_from_url(url: str | None) -> tuple[Site, str]:
    """Recover tenant/wd/site (and the job path) from a public posting URL."""
    match = _PUBLIC_URL_RE.match(url or "")
    if match is None:
        raise ValueError(f"cannot derive workday site from {url!r}")
    site = Site(match.group("tenant"), match.group("wd"), match.group("site"))
    return site, match.group("path")


def _detail_location(info: Mapping[str, Any]) -> str | None:
    primary = first_non_empty(info.get("location"))
    extra = [str(loc) for loc in info.get("additionalLocations") or [] if loc]
    if not primary:
        return None
    return LOCATION_SEPARATOR.join([primary, *extra])


def _detail_posted_at(info: Mapping[str, Any]) -> datetime | None:
    start_date = first_non_empty(info.get("startDate"))
    if start_date:
        try:
            return parse_iso(start_date).astimezone(UTC)
        except ValueError:
            log.debug("workday: unparseable startDate %r", start_date)
    return _parse_posted_on(info.get("postedOn"), now())
