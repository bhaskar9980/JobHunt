"""Greenhouse fetcher against a recorded (trimmed) board response. No network."""

from __future__ import annotations

import copy
from collections.abc import Iterator

import httpx
import pytest
import respx

from jobhunt.clock import parse_iso
from jobhunt.sources import get_module
from jobhunt.sources.base import FetchError, Http

FIXTURE = "ats/greenhouse_jobs.json"
BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/postman/jobs"
CFG = {"name": "Postman", "ats": "greenhouse", "token": "postman"}

greenhouse = get_module("greenhouse")


@pytest.fixture
def http() -> Iterator[Http]:
    wrapper = Http(client=httpx.Client(), retries=0, backoff_seconds=0)
    yield wrapper
    wrapper.close()


def _mock_board(respx_mock: respx.MockRouter, payload: dict) -> respx.Route:
    return respx_mock.get(BOARD_URL, params={"content": "true"}).respond(json=payload)


def test_module_contract() -> None:
    assert greenhouse.KEY == "greenhouse"
    assert callable(greenhouse.fetch)
    assert callable(greenhouse.hydrate)


def test_fetch_parses_every_job(respx_mock, http, load_fixture) -> None:
    payload = load_fixture(FIXTURE)
    _mock_board(respx_mock, payload)

    postings = greenhouse.fetch(CFG, http)

    assert len(postings) == len(payload["jobs"]) == 3
    first = postings[0]
    assert first.source == "greenhouse"
    assert first.ats_type == "greenhouse"
    assert isinstance(first.source_job_id, str)
    assert first.source_job_id == "7979886003"
    assert first.title == "Account Development Representative"
    assert first.company == "Postman"
    assert first.url == "https://job-boards.greenhouse.io/postman/jobs/7979886003"
    assert first.apply_url == first.url
    assert first.location == "New York, New York, United States"
    assert first.needs_hydration is False


def test_posted_at_uses_first_published_and_is_tz_aware(respx_mock, http, load_fixture) -> None:
    payload = load_fixture(FIXTURE)
    _mock_board(respx_mock, payload)

    first = greenhouse.fetch(CFG, http)[0]

    assert first.posted_at is not None
    assert first.posted_at.tzinfo is not None
    assert first.posted_at == parse_iso("2026-08-31T12:14:49-04:00")


def test_posted_at_falls_back_to_updated_at(respx_mock, http, load_fixture) -> None:
    payload = copy.deepcopy(load_fixture(FIXTURE))
    payload["jobs"][0]["first_published"] = None
    _mock_board(respx_mock, payload)

    first = greenhouse.fetch(CFG, http)[0]

    assert first.posted_at == parse_iso("2026-08-31T12:17:49-04:00")


def test_description_is_markdown_with_entities_unescaped(respx_mock, http, load_fixture) -> None:
    payload = load_fixture(FIXTURE)
    _mock_board(respx_mock, payload)

    description = greenhouse.fetch(CFG, http)[0].description_md

    assert description is not None
    assert description.startswith("## ")
    assert "Who Are We?" in description
    assert "Postman is the world" in description
    assert "<p>" not in description
    assert "&lt;" not in description
    assert "&quot;" not in description


def test_remote_detected_from_location_text(respx_mock, http, load_fixture) -> None:
    _mock_board(respx_mock, load_fixture(FIXTURE))

    by_id = {p.source_job_id: p for p in greenhouse.fetch(CFG, http)}

    assert by_id["7529471003"].location == "Remote, Denmark"
    assert by_id["7529471003"].is_remote is True
    assert by_id["7979886003"].is_remote is False


def test_company_falls_back_to_api_company_name(respx_mock, http, load_fixture) -> None:
    _mock_board(respx_mock, load_fixture(FIXTURE))

    postings = greenhouse.fetch({"token": "postman"}, http)

    assert {p.company for p in postings} == {"Postman"}


def test_raw_keeps_metadata_but_drops_html_content(respx_mock, http, load_fixture) -> None:
    _mock_board(respx_mock, load_fixture(FIXTURE))

    first = greenhouse.fetch(CFG, http)[0]

    assert first.raw["requisition_id"] == "2156"
    assert first.raw["departments"][0]["name"] == "Sales"
    assert "content" not in first.raw


def test_unknown_board_raises_fetch_error(respx_mock, http) -> None:
    respx_mock.get(
        "https://boards-api.greenhouse.io/v1/boards/nope/jobs", params={"content": "true"}
    ).respond(status_code=404, json={"status": 404, "error": "Not Found"})

    with pytest.raises(FetchError):
        greenhouse.fetch({"name": "Nope", "token": "nope"}, http)


def test_hydrate_is_noop(http, make_posting) -> None:
    posting = make_posting()

    assert greenhouse.hydrate(posting, http) is posting
