"""Workday fetcher: cxs list parsing, 20-per-page pagination, postedOn parsing, hydration."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from jobhunt.models import JobPosting
from jobhunt.sources import workday
from jobhunt.sources.base import FetchError, Http
from jobhunt.sources.workday import Site

HOST = "https://salesforce.wd12.myworkdayjobs.com"
LIST_URL = f"{HOST}/wday/cxs/salesforce/External_Career_Site/jobs"
FIRST_PATH = "/job/India---Bangalore/Software-Engineering-MTS_JR338172-1"
FIRST_PUBLIC_URL = f"{HOST}/en-US/External_Career_Site{FIRST_PATH}"
FIRST_DETAIL_URL = f"{HOST}/wday/cxs/salesforce/External_Career_Site{FIRST_PATH}"
REFERENCE = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)


def test_detail_timestamp_preserves_instant_with_offset():
    result = workday._detail_posted_at({"startDate": "2026-09-08T09:00:00+05:30"})
    assert result == datetime(2026, 9, 8, 3, 30, tzinfo=UTC)


def test_cutoff_compares_instants_not_iso_text():
    from jobhunt.pipeline import _too_old

    posting = JobPosting(
        "workday",
        "test",
        "Example",
        "Engineer",
        "https://example.invalid",
        posted_at=datetime.fromisoformat("2026-09-08T01:00:00+05:30"),
    )
    assert _too_old(posting, "2026-09-08T00:00:00+00:00")


@pytest.fixture
def cfg() -> dict[str, Any]:
    return {
        "name": "Salesforce",
        "ats": "workday",
        "token": None,
        "tenant": "salesforce",
        "wd": "wd12",
        "site": "External_Career_Site",
        "search_text": "Software Engineer",
        "aliases": [],
        "domains": [],
        "enabled": True,
    }


@pytest.fixture(scope="module")
def http() -> Http:
    """One client per module: building an httpx.Client loads the SSL context (~2s on Windows)."""
    wrapper = Http(client=httpx.Client(), retries=0, backoff_seconds=0.0)
    yield wrapper
    wrapper.close()


@pytest.fixture
def frozen_now(monkeypatch) -> datetime:
    monkeypatch.setattr(workday, "now", lambda: REFERENCE)
    return REFERENCE


@pytest.fixture
def list_page(load_fixture) -> dict[str, Any]:
    return load_fixture("ats/workday_list.json")


@pytest.fixture
def detail(load_fixture) -> dict[str, Any]:
    return load_fixture("ats/workday_detail.json")


def _serve_pages(pages: dict[int, dict[str, Any]]):
    """respx side effect: pick the page keyed by the ``offset`` in the POSTed JSON body."""

    def _respond(request: httpx.Request) -> httpx.Response:
        offset = json.loads(request.content)["offset"]
        if offset in pages:
            return httpx.Response(200, json=pages[offset])
        return httpx.Response(404, json={"message": f"no page at offset {offset}"})

    return _respond


def _row_posting(**overrides: Any) -> JobPosting:
    """Mirror pipeline._posting_from_row: only the columns the DB keeps."""
    base: dict[str, Any] = {
        "source": "workday",
        "source_job_id": FIRST_PATH,
        "company": "Salesforce",
        "title": "Software Engineering, MTS/ SMTS (Full-Stack)",
        "url": FIRST_PUBLIC_URL,
        "location": "2 Locations",
        "detail_ref": FIRST_PATH,
        "needs_hydration": True,
    }
    base.update(overrides)
    return JobPosting(**base)


def test_fetch_parses_list_items(respx_mock, cfg, http, list_page, frozen_now):
    route = respx_mock.post(LIST_URL).mock(side_effect=_serve_pages({0: list_page}))

    postings = workday.fetch(cfg, http)

    assert route.call_count == 1
    assert len(postings) == len(list_page["jobPostings"])
    first = postings[0]
    assert first.source == "workday"
    assert first.ats_type == "workday"
    assert first.company == "Salesforce"
    assert first.source_job_id == f"salesforce:External_Career_Site:{FIRST_PATH}"
    assert first.title == "Software Engineering, MTS/ SMTS (Full-Stack)"
    assert first.url == FIRST_PUBLIC_URL
    assert first.apply_url == FIRST_PUBLIC_URL + "/apply"
    assert first.location == "2 Locations"
    assert first.is_remote is False
    assert first.posted_at == frozen_now
    assert first.raw["req_id"] == "JR338172"
    assert first.description_md is None


def test_fetch_marks_postings_for_hydration_with_external_path(
    respx_mock, cfg, http, list_page, frozen_now
):
    respx_mock.post(LIST_URL).mock(side_effect=_serve_pages({0: list_page}))

    postings = workday.fetch(cfg, http)

    assert all(p.needs_hydration for p in postings)
    assert [p.detail_ref for p in postings] == [i["externalPath"] for i in list_page["jobPostings"]]


def test_fetch_parses_older_posted_on_relative_to_now(respx_mock, cfg, http, list_page, frozen_now):
    respx_mock.post(LIST_URL).mock(side_effect=_serve_pages({0: list_page}))

    postings = workday.fetch(cfg, http)

    old = next(p for p in postings if p.raw["postedOn"] == "Posted 30+ Days Ago")
    assert old.posted_at == datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC)
    assert old.location == "Mexico - Mexico City"


def test_fetch_sends_limit_20_search_text_and_json_headers(
    respx_mock, cfg, http, list_page, frozen_now
):
    route = respx_mock.post(LIST_URL).mock(side_effect=_serve_pages({0: list_page}))

    workday.fetch(cfg, http)

    request = route.calls.last.request
    body = json.loads(request.content)
    assert body == {
        "appliedFacets": {},
        "limit": 20,
        "offset": 0,
        "searchText": "Software Engineer",
    }
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["Accept"] == "application/json"
    assert request.headers["Accept-Language"] == "en-US"


def test_fetch_uses_empty_search_text_when_not_configured(
    respx_mock, cfg, http, list_page, frozen_now
):
    cfg["search_text"] = None
    route = respx_mock.post(LIST_URL).mock(side_effect=_serve_pages({0: list_page}))

    workday.fetch(cfg, http)

    assert json.loads(route.calls.last.request.content)["searchText"] == ""


def _synth_items(prefix: str, count: int) -> list[dict[str, Any]]:
    return [
        {
            "title": f"{prefix} {index}",
            "externalPath": f"/job/Remote/{prefix}-{index}_JR{index}",
            "locationsText": "Remote - US",
            "postedOn": "Posted 3 Days Ago",
            "bulletFields": [f"JR{index}"],
        }
        for index in range(count)
    ]


def test_fetch_paginates_in_steps_of_20_until_total(respx_mock, cfg, http, list_page, frozen_now):
    page_one = copy.deepcopy(list_page)
    page_two = {"total": 0, "jobPostings": _synth_items("Page2", 2)}
    total = 20 + len(page_two["jobPostings"])
    page_one["total"] = total
    route = respx_mock.post(LIST_URL).mock(side_effect=_serve_pages({0: page_one, 20: page_two}))

    postings = workday.fetch(cfg, http)

    assert route.call_count == 2
    offsets = [json.loads(call.request.content)["offset"] for call in route.calls]
    assert offsets == [0, 20]
    assert len(postings) == len(page_one["jobPostings"]) + 2
    assert postings[-1].is_remote is True
    assert postings[-1].posted_at == datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


def test_fetch_keeps_first_page_total_when_later_pages_report_zero(
    respx_mock, cfg, http, list_page, frozen_now
):
    """Live Workday returns total=0 on offset>0 pages; pagination must not stop early."""
    page_one = copy.deepcopy(list_page)
    page_one["total"] = 45
    pages = {
        0: page_one,
        20: {"total": 0, "jobPostings": _synth_items("P2", 2)},
        40: {"total": 0, "jobPostings": _synth_items("P3", 1)},
    }
    route = respx_mock.post(LIST_URL).mock(side_effect=_serve_pages(pages))

    postings = workday.fetch(cfg, http)

    assert route.call_count == 3
    assert len(postings) == len(page_one["jobPostings"]) + 3


def test_fetch_drops_duplicates_that_straddle_pages(respx_mock, cfg, http, list_page, frozen_now):
    """Seen live: 500 rows fetched, 499 unique externalPaths."""
    page_one = copy.deepcopy(list_page)
    page_one["total"] = 22
    repeated = copy.deepcopy(list_page["jobPostings"][0])
    page_two = {"total": 0, "jobPostings": [repeated, *_synth_items("P2", 1)]}
    respx_mock.post(LIST_URL).mock(side_effect=_serve_pages({0: page_one, 20: page_two}))

    postings = workday.fetch(cfg, http)

    assert len(postings) == len(page_one["jobPostings"]) + 1
    assert len({p.source_job_id for p in postings}) == len(postings)


def test_fetch_stops_on_empty_page(respx_mock, cfg, http, frozen_now):
    route = respx_mock.post(LIST_URL).mock(
        side_effect=_serve_pages({0: {"total": 100, "jobPostings": []}})
    )

    assert workday.fetch(cfg, http) == []
    assert route.call_count == 1


def test_fetch_caps_pages(respx_mock, cfg, http, list_page, frozen_now, monkeypatch):
    monkeypatch.setattr(workday, "MAX_PAGES", 3)

    def _endless(request: httpx.Request) -> httpx.Response:
        offset = json.loads(request.content)["offset"]
        return httpx.Response(
            200, json={"total": 10_000, "jobPostings": _synth_items(f"O{offset}", 20)}
        )

    route = respx_mock.post(LIST_URL).mock(side_effect=_endless)

    postings = workday.fetch(cfg, http)

    assert route.call_count == 3
    assert len(postings) == 3 * 20


def test_fetch_requires_tenant_wd_and_site(cfg, http):
    cfg["wd"] = None
    with pytest.raises(ValueError, match="tenant"):
        workday.fetch(cfg, http)


def test_fetch_propagates_fetch_error(respx_mock, cfg, http):
    respx_mock.post(LIST_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(FetchError):
        workday.fetch(cfg, http)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Posted Today", datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)),
        ("Posted Yesterday", datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)),
        ("Posted 3 Days Ago", datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)),
        ("Posted 1 Day Ago", datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)),
        ("Posted 30+ Days Ago", datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC)),
        ("posted  today", datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)),
        ("Apply by Friday", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_posted_on(text, expected):
    assert workday._parse_posted_on(text, REFERENCE) == expected


@pytest.mark.parametrize(
    ("url", "expected_site", "expected_path"),
    [
        (
            FIRST_PUBLIC_URL,
            Site("salesforce", "wd12", "External_Career_Site"),
            FIRST_PATH,
        ),
        (
            "https://acme.wd5.myworkdayjobs.com/Careers/job/Remote/Engineer_R1",
            Site("acme", "wd5", "Careers"),
            "/job/Remote/Engineer_R1",
        ),
        (
            "https://acme.wd1.myworkdayjobs.com/en-GB/Global-Jobs/job/UK---London/Dev_R2?src=x",
            Site("acme", "wd1", "Global-Jobs"),
            "/job/UK---London/Dev_R2",
        ),
    ],
)
def test_site_from_url(url, expected_site, expected_path):
    assert workday._site_from_url(url) == (expected_site, expected_path)


@pytest.mark.parametrize(
    "url", ["https://example.com/jobs/1", "https://acme.wd5.myworkdayjobs.com/", None]
)
def test_site_from_url_rejects_unrecognised(url):
    with pytest.raises(ValueError):
        workday._site_from_url(url)


def test_hydrate_fills_description_from_detail(respx_mock, http, detail, frozen_now):
    route = respx_mock.get(FIRST_DETAIL_URL).mock(return_value=httpx.Response(200, json=detail))

    result = workday.hydrate(_row_posting(), http)

    assert route.call_count == 1
    assert result.needs_hydration is False
    assert result.description_md is not None
    assert "candidate experience" in result.description_md
    assert "<p" not in result.description_md
    assert result.source_job_id == FIRST_PATH
    assert result.url == FIRST_PUBLIC_URL
    request = route.calls.last.request
    assert request.headers["Accept"] == "application/json"
    assert request.headers["Accept-Language"] == "en-US"


def test_hydrate_takes_type_apply_url_location_and_date_from_detail(
    respx_mock, http, detail, frozen_now
):
    respx_mock.get(FIRST_DETAIL_URL).mock(return_value=httpx.Response(200, json=detail))

    result = workday.hydrate(_row_posting(), http)

    info = detail["jobPostingInfo"]
    assert result.employment_type == "Full time"
    assert result.apply_url == info["externalUrl"] + "/apply"
    assert result.location == "India - Bangalore; India - Hyderabad"
    assert result.posted_at == datetime(2026, 9, 8, tzinfo=UTC)


def test_hydrate_derives_path_from_url_when_detail_ref_missing(
    respx_mock, http, detail, frozen_now
):
    route = respx_mock.get(FIRST_DETAIL_URL).mock(return_value=httpx.Response(200, json=detail))

    result = workday.hydrate(_row_posting(detail_ref=None), http)

    assert route.call_count == 1
    assert result.description_md


def test_hydrate_falls_back_to_posted_on_when_start_date_missing(
    respx_mock, http, detail, frozen_now
):
    body = copy.deepcopy(detail)
    body["jobPostingInfo"].pop("startDate")
    body["jobPostingInfo"]["postedOn"] = "Posted Yesterday"
    body["jobPostingInfo"].pop("externalUrl")
    respx_mock.get(FIRST_DETAIL_URL).mock(return_value=httpx.Response(200, json=body))

    result = workday.hydrate(_row_posting(apply_url="keep-me"), http)

    assert result.posted_at == datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
    assert result.apply_url == "keep-me"


def test_hydrate_rejects_non_workday_url(http):
    with pytest.raises(ValueError):
        workday.hydrate(_row_posting(url="https://example.com/x"), http)


def test_parse_item_flags_remote_from_locations_text_or_path(frozen_now):
    site = Site("acme", "wd5", "Careers")
    by_text = {"title": "SWE", "externalPath": "/job/US/SWE_R1", "locationsText": "Remote - US"}
    by_path = {"title": "SWE", "externalPath": "/job/Virginia---Remote/SWE_R2", "locationsText": ""}
    office = {
        "title": "SWE",
        "externalPath": "/job/Ohio/SWE_R3",
        "locationsText": "Ohio - Columbus",
    }

    assert workday._parse_item(by_text, "Acme", site, REFERENCE).is_remote is True
    assert workday._parse_item(by_path, "Acme", site, REFERENCE).is_remote is True
    assert workday._parse_item(office, "Acme", site, REFERENCE).is_remote is False
    assert workday._parse_item(by_path, "Acme", site, REFERENCE).location is None
    assert workday._parse_item(by_path, "Acme", site, REFERENCE).raw["req_id"] is None


def test_fetch_skips_items_without_external_path(respx_mock, cfg, http, list_page, frozen_now):
    page = dict(list_page)
    teaser = {"title": "Featured: Join our talent community"}
    page["jobPostings"] = [teaser, *list_page["jobPostings"]]
    respx_mock.post(LIST_URL).mock(side_effect=_serve_pages({0: page}))

    postings = workday.fetch(cfg, http)

    assert len(postings) == len(list_page["jobPostings"])
    assert all(p.source_job_id for p in postings)
