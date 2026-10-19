"""SmartRecruiters fetcher: list parsing, offset pagination, detail hydration. No network."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from jobhunt.models import JobPosting
from jobhunt.sources import smartrecruiters
from jobhunt.sources.base import FetchError, Http

LIST_URL = "https://api.smartrecruiters.com/v1/companies/freshworks/postings"
FIRST_ID = "744000147982299"
FIRST_DETAIL_URL = f"{LIST_URL}/{FIRST_ID}"
FIRST_PUBLIC_URL = f"https://jobs.smartrecruiters.com/Freshworks/{FIRST_ID}"


@pytest.fixture
def cfg() -> dict[str, Any]:
    return {
        "name": "Freshworks",
        "ats": "smartrecruiters",
        "token": "freshworks",
        "tenant": None,
        "wd": None,
        "site": None,
        "search_text": None,
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
def list_page(load_fixture) -> dict[str, Any]:
    return load_fixture("ats/smartrecruiters_list.json")


@pytest.fixture
def detail(load_fixture) -> dict[str, Any]:
    return load_fixture("ats/smartrecruiters_detail.json")


def _serve_pages(pages: list[dict[str, Any]]):
    """respx side effect: pick the page whose offset matches the ``offset`` query param."""

    def _respond(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("offset", "0"))
        for page in pages:
            if page["offset"] == offset:
                return httpx.Response(200, json=page)
        return httpx.Response(404, json={"message": f"no page at offset {offset}"})

    return _respond


def _row_posting(**overrides: Any) -> JobPosting:
    """Mirror pipeline._posting_from_row: only the columns the DB keeps."""
    base: dict[str, Any] = {
        "source": "smartrecruiters",
        "source_job_id": FIRST_ID,
        "company": "Freshworks",
        "title": "Channel Partner Manager - France & Nordics",
        "url": FIRST_PUBLIC_URL,
        "location": "Paris, IDF, France",
        "detail_ref": FIRST_DETAIL_URL,
        "needs_hydration": True,
    }
    base.update(overrides)
    return JobPosting(**base)


def test_fetch_parses_list_items(respx_mock, cfg, http, list_page):
    route = respx_mock.get(LIST_URL).mock(side_effect=_serve_pages([list_page]))

    postings = smartrecruiters.fetch(cfg, http)

    assert route.call_count == 1
    assert len(postings) == len(list_page["content"])
    assert {p.source_job_id for p in postings} == {i["id"] for i in list_page["content"]}
    first = postings[0]
    assert first.source == "smartrecruiters"
    assert first.ats_type == "smartrecruiters"
    assert first.company == "Freshworks"
    assert first.source_job_id == FIRST_ID
    assert first.title == "Channel Partner Manager - France & Nordics"
    assert first.url == FIRST_PUBLIC_URL
    assert first.location == "Paris, IDF, France"
    assert first.is_remote is False
    assert first.employment_type == "Full-time"
    assert first.posted_at == datetime(2026, 9, 7, 19, 21, 18, 720000, tzinfo=UTC)
    assert first.description_md is None
    assert first.raw["refNumber"] == "REF8769I"
    # real API emits "Paris, , France" when region is blank; empty segments are dropped
    assert postings[1].location == "Paris, France"


def test_fetch_marks_postings_for_hydration_with_detail_api_ref(respx_mock, cfg, http, list_page):
    respx_mock.get(LIST_URL).mock(side_effect=_serve_pages([list_page]))

    postings = smartrecruiters.fetch(cfg, http)

    assert all(p.needs_hydration for p in postings)
    assert postings[0].detail_ref == FIRST_DETAIL_URL
    assert [p.detail_ref for p in postings] == [i["ref"] for i in list_page["content"]]


def test_fetch_requests_page_size_100(respx_mock, cfg, http, list_page):
    route = respx_mock.get(LIST_URL).mock(side_effect=_serve_pages([list_page]))

    smartrecruiters.fetch(cfg, http)

    params = route.calls.last.request.url.params
    assert params["limit"] == "100"
    assert params["offset"] == "0"


def test_fetch_paginates_by_offset_until_total_found(respx_mock, cfg, http, list_page):
    page_one = copy.deepcopy(list_page)
    page_two = copy.deepcopy(list_page)
    page_two["offset"] = len(page_one["content"])
    page_two["content"] = page_two["content"][:2]
    for index, item in enumerate(page_two["content"]):
        item["id"] = f"90000000000{index}"
        item["ref"] = f"{LIST_URL}/{item['id']}"
    total = len(page_one["content"]) + len(page_two["content"])
    page_one["totalFound"] = page_two["totalFound"] = total
    route = respx_mock.get(LIST_URL).mock(side_effect=_serve_pages([page_one, page_two]))

    postings = smartrecruiters.fetch(cfg, http)

    assert route.call_count == 2
    offsets = [int(call.request.url.params["offset"]) for call in route.calls]
    assert offsets == [0, len(page_one["content"])]
    assert len(postings) == total
    assert len({p.source_job_id for p in postings}) == total


def test_fetch_stops_on_empty_page_even_if_total_says_more(respx_mock, cfg, http, list_page):
    page = copy.deepcopy(list_page)
    page["totalFound"] = 500
    page["content"] = []
    route = respx_mock.get(LIST_URL).mock(side_effect=_serve_pages([page]))

    assert smartrecruiters.fetch(cfg, http) == []
    assert route.call_count == 1


def test_fetch_caps_pages(respx_mock, cfg, http, list_page, monkeypatch):
    monkeypatch.setattr(smartrecruiters, "MAX_PAGES", 2)
    page = copy.deepcopy(list_page)
    page["totalFound"] = 10_000

    def _endless(request: httpx.Request) -> httpx.Response:
        body = copy.deepcopy(page)
        body["offset"] = int(request.url.params["offset"])
        return httpx.Response(200, json=body)

    route = respx_mock.get(LIST_URL).mock(side_effect=_endless)

    postings = smartrecruiters.fetch(cfg, http)

    assert route.call_count == 2
    assert len(postings) == 2 * len(page["content"])


def test_fetch_requires_token(cfg, http):
    cfg["token"] = None
    with pytest.raises(ValueError, match="token"):
        smartrecruiters.fetch(cfg, http)


def test_fetch_propagates_fetch_error(respx_mock, cfg, http):
    respx_mock.get(LIST_URL).mock(return_value=httpx.Response(500))
    with pytest.raises(FetchError):
        smartrecruiters.fetch(cfg, http)


def test_parse_item_falls_back_to_city_region_country_and_remote_flag():
    item = {
        "id": "1",
        "name": "Remote Engineer",
        "location": {"city": "Chennai", "region": "TN", "country": "in", "remote": True},
        "company": {"identifier": "Acme"},
    }

    posting = smartrecruiters._parse_item(item, "Acme", "acme")

    assert posting.location == "Chennai, TN, in"
    assert posting.is_remote is True
    assert posting.url == "https://jobs.smartrecruiters.com/Acme/1"
    assert posting.detail_ref == "https://api.smartrecruiters.com/v1/companies/acme/postings/1"
    assert posting.employment_type is None
    assert posting.posted_at is None


def test_hydrate_fills_description_from_detail_sections(respx_mock, http, detail):
    route = respx_mock.get(FIRST_DETAIL_URL).mock(return_value=httpx.Response(200, json=detail))

    result = smartrecruiters.hydrate(_row_posting(), http)

    assert route.call_count == 1
    assert result.needs_hydration is False
    assert result.description_md is not None
    assert result.description_md.startswith("## Company Description\n")
    headings = ["## Company Description", "## Job Description", "## Qualifications"]
    headings.append("## Additional Information")
    positions = [result.description_md.index(h) for h in headings]
    assert positions == sorted(positions)
    assert "<p>" not in result.description_md
    assert "7+ years" in result.description_md


def test_hydrate_takes_apply_and_posting_urls_from_detail(respx_mock, http, detail):
    respx_mock.get(FIRST_DETAIL_URL).mock(return_value=httpx.Response(200, json=detail))

    result = smartrecruiters.hydrate(_row_posting(), http)

    assert result.apply_url == detail["applyUrl"]
    assert result.url == detail["postingUrl"]
    assert result.employment_type == "Full-time"
    assert result.posted_at == datetime(2026, 9, 7, 19, 21, 18, 720000, tzinfo=UTC)
    assert result.source_job_id == FIRST_ID
    assert result.company == "Freshworks"


def test_hydrate_derives_detail_url_from_public_url_when_detail_ref_missing(
    respx_mock, http, detail
):
    derived = f"https://api.smartrecruiters.com/v1/companies/Freshworks/postings/{FIRST_ID}"
    route = respx_mock.get(derived).mock(return_value=httpx.Response(200, json=detail))

    result = smartrecruiters.hydrate(_row_posting(detail_ref=None), http)

    assert route.call_count == 1
    assert result.description_md


def test_hydrate_rejects_unrecognised_url_without_detail_ref(http):
    posting = _row_posting(detail_ref=None, url="https://example.com/jobs/1")
    with pytest.raises(ValueError):
        smartrecruiters.hydrate(posting, http)


def test_hydrate_with_no_sections_leaves_description_none(respx_mock, http, detail):
    body = copy.deepcopy(detail)
    body["jobAd"] = {"sections": {}}
    respx_mock.get(FIRST_DETAIL_URL).mock(return_value=httpx.Response(200, json=body))

    result = smartrecruiters.hydrate(_row_posting(), http)

    assert result.description_md is None
    assert result.needs_hydration is False


def test_sections_to_md_appends_unknown_sections_after_known_order():
    sections = {
        "extra": {"title": "Perks", "text": "<p>Snacks</p>"},
        "jobDescription": {"title": "Job Description", "text": "<p>Build things</p>"},
        "companyDescription": {"title": "Company", "text": ""},
    }

    markdown = smartrecruiters._sections_to_md(sections)

    assert markdown == "## Job Description\nBuild things\n\n## Perks\nSnacks"
