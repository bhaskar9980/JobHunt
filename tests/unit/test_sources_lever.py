"""Lever fetcher against a recorded (trimmed) postings response. No network."""

from __future__ import annotations

import copy
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from jobhunt.clock import from_epoch_ms
from jobhunt.sources import get_module
from jobhunt.sources.base import FetchError, Http

FIXTURE = "ats/lever_postings.json"
POSTINGS_URL = "https://api.lever.co/v0/postings/spotify"
PAGE_SIZE = 100
CFG = {"name": "Spotify", "ats": "lever", "token": "spotify"}

lever = get_module("lever")


@pytest.fixture
def http() -> Iterator[Http]:
    wrapper = Http(client=httpx.Client(), retries=0, backoff_seconds=0)
    yield wrapper
    wrapper.close()


def _mock_page(respx_mock: respx.MockRouter, skip: int, body: Any, url: str = POSTINGS_URL):
    params = {"mode": "json", "limit": str(PAGE_SIZE), "skip": str(skip)}
    return respx_mock.get(url, params=params).respond(json=body)


def _synthetic_page(template: dict, count: int, prefix: str) -> list[dict]:
    page = []
    for index in range(count):
        item = copy.deepcopy(template)
        item["id"] = f"{prefix}-{index:03d}"
        item["hostedUrl"] = f"https://jobs.lever.co/spotify/{item['id']}"
        item["applyUrl"] = f"{item['hostedUrl']}/apply"
        page.append(item)
    return page


def test_module_contract() -> None:
    assert lever.KEY == "lever"
    assert callable(lever.fetch)
    assert callable(lever.hydrate)


def test_fetch_parses_single_short_page(respx_mock, http, load_fixture) -> None:
    fixture = load_fixture(FIXTURE)
    route = _mock_page(respx_mock, 0, fixture)

    postings = lever.fetch(CFG, http)

    assert route.call_count == 1
    assert len(postings) == len(fixture) == 3
    first = postings[0]
    assert first.source == "lever"
    assert first.ats_type == "lever"
    assert isinstance(first.source_job_id, str)
    assert first.source_job_id == "2193db3f-77c5-43b8-b030-8f92c9882bf1"
    assert first.title == "Android Engineer - Experience"
    assert first.company == "Spotify"
    assert first.url == "https://jobs.lever.co/spotify/2193db3f-77c5-43b8-b030-8f92c9882bf1"
    assert first.apply_url == first.url + "/apply"
    assert first.employment_type == "Permanent"
    assert first.needs_hydration is False


def test_posted_at_from_created_at_epoch_ms(respx_mock, http, load_fixture) -> None:
    _mock_page(respx_mock, 0, load_fixture(FIXTURE))

    first = lever.fetch(CFG, http)[0]

    assert first.posted_at is not None
    assert first.posted_at.tzinfo is not None
    assert first.posted_at == from_epoch_ms(1782214185805)


def test_location_joins_all_locations_when_several(respx_mock, http, load_fixture) -> None:
    _mock_page(respx_mock, 0, load_fixture(FIXTURE))

    by_id = {p.source_job_id: p for p in lever.fetch(CFG, http)}

    assert by_id["2193db3f-77c5-43b8-b030-8f92c9882bf1"].location == "London; Stockholm"
    assert by_id["ddb5dfa9-c5ef-4199-bd19-c179af568cc8"].location == "Dubai"


def test_description_is_markdown_with_list_sections(respx_mock, http, load_fixture) -> None:
    _mock_page(respx_mock, 0, load_fixture(FIXTURE))

    by_id = {p.source_job_id: p for p in lever.fetch(CFG, http)}
    android = by_id["2193db3f-77c5-43b8-b030-8f92c9882bf1"].description_md
    maghreb = by_id["ddb5dfa9-c5ef-4199-bd19-c179af568cc8"].description_md

    assert android is not None
    assert android.startswith("We design Spotify")
    assert "## What You'll Do" in android
    assert "## Who You Are" in android
    assert "- Develop and maintain mobile client components" in android
    assert "Spotify is an equal opportunity employer" in android
    assert "<p>" not in android
    assert "<li>" not in android
    assert maghreb is not None
    assert "&amp;" not in maghreb


def test_remote_detection(respx_mock, http, load_fixture) -> None:
    _mock_page(respx_mock, 0, load_fixture(FIXTURE))

    by_id = {p.source_job_id: p for p in lever.fetch(CFG, http)}

    assert by_id["0b8ea4d4-57ee-49c1-a57f-9a37248ee24f"].is_remote is True
    assert by_id["2193db3f-77c5-43b8-b030-8f92c9882bf1"].is_remote is False
    assert by_id["ddb5dfa9-c5ef-4199-bd19-c179af568cc8"].is_remote is False


def test_remote_detected_from_location_text(respx_mock, http, load_fixture) -> None:
    fixture = copy.deepcopy(load_fixture(FIXTURE))
    fixture[2]["workplaceType"] = "unspecified"
    fixture[2]["categories"]["location"] = "Remote - EMEA"
    fixture[2]["categories"]["allLocations"] = ["Remote - EMEA"]
    _mock_page(respx_mock, 0, fixture)

    posting = lever.fetch(CFG, http)[2]

    assert posting.location == "Remote - EMEA"
    assert posting.is_remote is True


def test_salary_text_from_salary_range(respx_mock, http, load_fixture) -> None:
    fixture = copy.deepcopy(load_fixture(FIXTURE))
    fixture[0]["salaryRange"] = {
        "min": 80000,
        "max": 120000,
        "currency": "USD",
        "interval": "per-year-salary",
    }
    _mock_page(respx_mock, 0, fixture)

    postings = lever.fetch(CFG, http)

    assert postings[0].salary_text == "80000-120000 USD per per-year-salary"
    assert postings[1].salary_text is None


def test_paginates_until_a_short_page(respx_mock, http, load_fixture) -> None:
    fixture = load_fixture(FIXTURE)
    full_page = _synthetic_page(fixture[0], PAGE_SIZE, "page0")
    page0 = _mock_page(respx_mock, 0, full_page)
    page1 = _mock_page(respx_mock, PAGE_SIZE, fixture)

    postings = lever.fetch(CFG, http)

    assert page0.call_count == 1
    assert page1.call_count == 1
    assert len(postings) == PAGE_SIZE + len(fixture)
    assert len({p.source_job_id for p in postings}) == len(postings)


def test_empty_board_returns_empty_list(respx_mock, http) -> None:
    _mock_page(respx_mock, 0, [])

    assert lever.fetch(CFG, http) == []


def test_unknown_site_404_raises_fetch_error(respx_mock, http) -> None:
    url = "https://api.lever.co/v0/postings/nope"
    respx_mock.get(url).respond(status_code=404, json={"ok": False, "error": "Document not found"})

    with pytest.raises(FetchError):
        lever.fetch({"name": "Nope", "token": "nope"}, http)


def test_non_array_payload_raises_fetch_error(respx_mock, http) -> None:
    _mock_page(respx_mock, 0, {"ok": False})

    with pytest.raises(FetchError):
        lever.fetch(CFG, http)


def test_hydrate_is_noop(http, make_posting) -> None:
    posting = make_posting(source="lever", ats_type="lever")

    assert lever.hydrate(posting, http) is posting
