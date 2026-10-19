"""Eightfold (PCSX) fetcher: search paging, position parsing, envelope errors, hydration."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from jobhunt.models import JobPosting
from jobhunt.sources import eightfold
from jobhunt.sources.base import FetchError, Http

HOST = "apply.careers.microsoft.com"
DOMAIN = "microsoft.com"
SEARCH_URL = f"https://{HOST}/api/pcsx/search"
FIRST_ID = "1970393556911611"
FIRST_URL = f"https://{HOST}/careers/job/{FIRST_ID}"
FIRST_DETAIL_URL = f"https://{HOST}/api/apply/v2/jobs/{FIRST_ID}"
FIRST_REF = f"{DOMAIN}:{FIRST_ID}"
POSTED_AT = datetime(2026, 9, 8, 10, 39, 22, tzinfo=UTC)


@pytest.fixture
def cfg() -> dict[str, Any]:
    return {
        "name": "Microsoft",
        "ats": "eightfold",
        "token": DOMAIN,
        "host": HOST,
        "search_text": ".NET software engineer",
        "location": "India",
        "aliases": [],
        "domains": ["microsoft.com"],
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
    return load_fixture("ats/eightfold_list.json")


@pytest.fixture
def detail(load_fixture) -> dict[str, Any]:
    return load_fixture("ats/eightfold_detail.json")


def _envelope(positions: list[dict[str, Any]], count: int) -> dict[str, Any]:
    return {
        "status": 200,
        "error": {"message": "", "body": ""},
        "data": {"positions": positions, "count": count, "sortBy": "relevance"},
        "metadata": None,
    }


def _serve_pages(pages: dict[int, dict[str, Any]]):
    """respx side effect: pick the page keyed by the ``start`` query parameter."""

    def _respond(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params.get("start", 0))
        if start in pages:
            return httpx.Response(200, json=pages[start])
        return httpx.Response(200, json=_envelope([], 0))

    return _respond


def _row_posting(**overrides: Any) -> JobPosting:
    """Mirror pipeline._posting_from_row: only the columns the DB keeps."""
    base: dict[str, Any] = {
        "source": "eightfold",
        "source_job_id": FIRST_ID,
        "company": "Microsoft",
        "title": "Software Engineer II",
        "url": FIRST_URL,
        "location": "India, Karnataka, Bangalore",
        "detail_ref": FIRST_REF,
        "needs_hydration": True,
    }
    base.update(overrides)
    return JobPosting(**base)


def test_fetch_parses_positions(respx_mock, cfg, http, list_page):
    page = copy.deepcopy(list_page)
    page["data"]["count"] = len(page["data"]["positions"])
    route = respx_mock.get(SEARCH_URL).mock(side_effect=_serve_pages({0: page}))

    postings = eightfold.fetch(cfg, http)

    assert route.call_count == 1
    assert len(postings) == 3
    first = postings[0]
    assert first.source == "eightfold"
    assert first.ats_type == "eightfold"
    assert first.company == "Microsoft"
    assert first.source_job_id == FIRST_REF
    assert first.title == "Software Engineer II"
    assert first.url == FIRST_URL
    assert first.apply_url == FIRST_URL
    assert first.location == "India, Karnataka, Bangalore"
    assert first.is_remote is False
    assert first.posted_at == POSTED_AT
    assert first.needs_hydration is True
    assert first.detail_ref == FIRST_REF
    assert first.description_md is None
    assert first.raw["displayJobId"] == "200041076"


def test_fetch_sends_the_domain_query_location_and_start(respx_mock, cfg, http, list_page):
    page = copy.deepcopy(list_page)
    page["data"]["count"] = len(page["data"]["positions"])
    route = respx_mock.get(SEARCH_URL).mock(side_effect=_serve_pages({0: page}))

    eightfold.fetch(cfg, http)

    params = route.calls[0].request.url.params
    assert params["domain"] == DOMAIN
    assert params["query"] == ".NET software engineer"
    assert params["location"] == "India"
    assert params["start"] == "0"


def test_fetch_pages_by_the_size_the_server_returned(respx_mock, cfg, http, list_page):
    first = copy.deepcopy(list_page)
    first["data"]["count"] = 5
    second = copy.deepcopy(list_page)
    second["data"]["count"] = 5
    for offset, position in enumerate(second["data"]["positions"]):
        position["id"] = 999000 + offset
    route = respx_mock.get(SEARCH_URL).mock(side_effect=_serve_pages({0: first, 3: second}))

    postings = eightfold.fetch(cfg, http)

    assert route.call_count == 2
    assert len(postings) == 6


def test_fetch_stops_at_the_page_cap(respx_mock, cfg, http, list_page):
    def _always_new(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params.get("start", 0))
        page = copy.deepcopy(list_page)
        page["data"]["count"] = 10_000
        for offset, position in enumerate(page["data"]["positions"]):
            position["id"] = start * 100 + offset + 1
        return httpx.Response(200, json=page)

    route = respx_mock.get(SEARCH_URL).mock(side_effect=_always_new)

    postings = eightfold.fetch(cfg, http)

    assert route.call_count == eightfold.MAX_PAGES
    assert len(postings) == eightfold.MAX_PAGES * 3


def test_fetch_dedupes_a_position_served_on_two_pages(respx_mock, cfg, http, list_page):
    first = copy.deepcopy(list_page)
    first["data"]["count"] = 5
    second = copy.deepcopy(list_page)
    second["data"]["count"] = 5
    respx_mock.get(SEARCH_URL).mock(side_effect=_serve_pages({0: first, 3: second}))

    postings = eightfold.fetch(cfg, http)

    assert len(postings) == 3


def test_fetch_drops_positions_without_an_id(respx_mock, cfg, http, list_page):
    page = copy.deepcopy(list_page)
    page["data"]["count"] = len(page["data"]["positions"])
    page["data"]["positions"][1].pop("id")
    respx_mock.get(SEARCH_URL).mock(side_effect=_serve_pages({0: page}))

    postings = eightfold.fetch(cfg, http)

    assert len(postings) == 2


def test_fetch_marks_a_remote_position(respx_mock, cfg, http, list_page):
    page = copy.deepcopy(list_page)
    page["data"]["count"] = len(page["data"]["positions"])
    page["data"]["positions"][0]["workLocationOption"] = "remote"
    respx_mock.get(SEARCH_URL).mock(side_effect=_serve_pages({0: page}))

    postings = eightfold.fetch(cfg, http)

    assert postings[0].is_remote is True


def test_fetch_rejects_an_envelope_that_carries_an_error(respx_mock, cfg, http):
    body = _envelope([], 0)
    body["error"] = {"message": "Not authorized for PCSX", "body": ""}
    respx_mock.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=body))

    with pytest.raises(ValueError, match="Not authorized for PCSX"):
        eightfold.fetch(cfg, http)


@pytest.mark.parametrize("missing", ["token", "host"])
def test_fetch_needs_the_domain_and_the_host(cfg, http, missing):
    cfg[missing] = None

    with pytest.raises(ValueError, match="eightfold source requires"):
        eightfold.fetch(cfg, http)


def test_hydrate_adds_the_description_and_clears_the_flag(respx_mock, http, detail):
    route = respx_mock.get(FIRST_DETAIL_URL).mock(return_value=httpx.Response(200, json=detail))

    result = eightfold.hydrate(_row_posting(), http)

    assert route.calls[0].request.url.params["domain"] == DOMAIN
    assert result.needs_hydration is False
    assert result.description_md is not None
    assert "Overview" in result.description_md
    assert "<b>" not in result.description_md
    assert result.location == "India, Karnataka, Bangalore"
    assert result.apply_url == FIRST_URL
    assert result.posted_at == POSTED_AT


def test_hydrate_keeps_what_the_detail_call_leaves_out(respx_mock, http, detail):
    thin = {"id": int(FIRST_ID), "name": "Software Engineer II"}
    respx_mock.get(FIRST_DETAIL_URL).mock(return_value=httpx.Response(200, json=thin))

    result = eightfold.hydrate(_row_posting(location="Bengaluru"), http)

    assert result.location == "Bengaluru"
    assert result.description_md is None
    assert result.needs_hydration is False


@pytest.mark.parametrize(
    "ref",
    ["1970393556911611", "", "microsoft.com:", ":1970393556911611"],
)
def test_hydrate_refuses_a_detail_ref_without_domain_and_id(http, ref):
    with pytest.raises(ValueError, match="detail_ref"):
        eightfold.hydrate(_row_posting(detail_ref=ref or None), http)


def test_hydrate_refuses_a_url_it_cannot_read_a_host_from(http):
    with pytest.raises(ValueError, match="host"):
        eightfold.hydrate(_row_posting(url="not-a-url"), http)


def test_hydrate_propagates_a_failed_detail_call(respx_mock, http):
    respx_mock.get(FIRST_DETAIL_URL).mock(return_value=httpx.Response(404, json={}))

    with pytest.raises(FetchError):
        eightfold.hydrate(_row_posting(), http)
