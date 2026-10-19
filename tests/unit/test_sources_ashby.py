"""Ashby fetcher against a recorded (trimmed) job-board response. No network."""

from __future__ import annotations

import copy
from collections.abc import Iterator

import httpx
import pytest
import respx

from jobhunt.clock import parse_iso
from jobhunt.sources import get_module
from jobhunt.sources.base import FetchError, Http

FIXTURE = "ats/ashby_board.json"
BOARD_URL = "https://api.ashbyhq.com/posting-api/job-board/linear"
CFG = {"name": "Linear", "ats": "ashby", "token": "linear"}

ashby = get_module("ashby")


@pytest.fixture
def http() -> Iterator[Http]:
    wrapper = Http(client=httpx.Client(), retries=0, backoff_seconds=0)
    yield wrapper
    wrapper.close()


def _mock_board(respx_mock: respx.MockRouter, payload: dict, url: str = BOARD_URL) -> respx.Route:
    return respx_mock.get(url, params={"includeCompensation": "true"}).respond(json=payload)


def test_module_contract() -> None:
    assert ashby.KEY == "ashby"
    assert callable(ashby.fetch)
    assert callable(ashby.hydrate)


def test_fetch_parses_every_listed_job(respx_mock, http, load_fixture) -> None:
    payload = load_fixture(FIXTURE)
    _mock_board(respx_mock, payload)

    postings = ashby.fetch(CFG, http)

    assert len(postings) == len(payload["jobs"]) == 3
    first = postings[0]
    assert first.source == "ashby"
    assert first.ats_type == "ashby"
    assert isinstance(first.source_job_id, str)
    assert first.source_job_id == "d3bc1ced-3ce4-4086-a050-555055dbb1ff"
    assert first.title == "Senior / Staff Fullstack Engineer"
    assert first.company == "Linear"
    assert first.url == "https://jobs.ashbyhq.com/linear/d3bc1ced-3ce4-4086-a050-555055dbb1ff"
    assert first.apply_url == first.url + "/application"
    assert first.location == "Europe"
    assert first.employment_type == "FullTime"
    assert first.needs_hydration is False


def test_posted_at_is_tz_aware(respx_mock, http, load_fixture) -> None:
    _mock_board(respx_mock, load_fixture(FIXTURE))

    first = ashby.fetch(CFG, http)[0]

    assert first.posted_at is not None
    assert first.posted_at.tzinfo is not None
    assert first.posted_at == parse_iso("2021-04-27T20:13:45.158+00:00")


def test_description_is_markdown(respx_mock, http, load_fixture) -> None:
    _mock_board(respx_mock, load_fixture(FIXTURE))

    description = ashby.fetch(CFG, http)[0].description_md

    assert description is not None
    assert description.startswith("At Linear, we're building")
    assert "<p" not in description
    assert "&amp;" not in description
    assert "&nbsp;" not in description


def test_secondary_locations_are_joined(respx_mock, http, load_fixture) -> None:
    _mock_board(respx_mock, load_fixture(FIXTURE))

    by_id = {p.source_job_id: p for p in ashby.fetch(CFG, http)}

    assert by_id["1d652292-04d9-405c-8101-578efd020e94"].location == "North America; Europe"


def test_skips_unlisted_jobs(respx_mock, http, load_fixture) -> None:
    payload = copy.deepcopy(load_fixture(FIXTURE))
    payload["jobs"][1]["isListed"] = False
    _mock_board(respx_mock, payload)

    postings = ashby.fetch(CFG, http)

    assert len(postings) == 2
    assert "d37b3d76-3080-47f9-8a19-60505573112c" not in {p.source_job_id for p in postings}


def test_remote_detection(respx_mock, http, load_fixture) -> None:
    payload = copy.deepcopy(load_fixture(FIXTURE))
    payload["jobs"][0]["isRemote"] = False
    payload["jobs"][0]["workplaceType"] = "Remote"
    payload["jobs"][1]["isRemote"] = False
    payload["jobs"][1]["workplaceType"] = "OnSite"
    payload["jobs"][2]["isRemote"] = True
    payload["jobs"][2]["workplaceType"] = "Hybrid"
    _mock_board(respx_mock, payload)

    postings = ashby.fetch(CFG, http)

    assert [p.is_remote for p in postings] == [True, False, True]


def test_salary_text_from_compensation_summaries(respx_mock, http, load_fixture) -> None:
    payload = copy.deepcopy(load_fixture(FIXTURE))
    payload["jobs"][0]["compensation"]["compensationTierSummary"] = "$180K – $220K • Offers Equity"
    payload["jobs"][1]["compensation"]["scrapeableCompensationSalarySummary"] = "$150K - $190K"
    payload["jobs"][2]["compensation"] = None
    _mock_board(respx_mock, payload)

    postings = ashby.fetch(CFG, http)

    assert postings[0].salary_text == "$180K – $220K • Offers Equity"
    assert postings[1].salary_text == "$150K - $190K"
    assert postings[2].salary_text is None


def test_salary_text_none_when_summaries_null(respx_mock, http, load_fixture) -> None:
    _mock_board(respx_mock, load_fixture(FIXTURE))

    assert all(p.salary_text is None for p in ashby.fetch(CFG, http))


def test_company_falls_back_to_token(respx_mock, http, load_fixture) -> None:
    _mock_board(respx_mock, load_fixture(FIXTURE))

    postings = ashby.fetch({"token": "linear"}, http)

    assert {p.company for p in postings} == {"linear"}


def test_raw_drops_html_bodies(respx_mock, http, load_fixture) -> None:
    _mock_board(respx_mock, load_fixture(FIXTURE))

    first = ashby.fetch(CFG, http)[0]

    assert first.raw["department"] == "Product"
    assert "descriptionHtml" not in first.raw
    assert "descriptionPlain" not in first.raw


def test_unknown_board_raises_fetch_error(respx_mock, http) -> None:
    url = "https://api.ashbyhq.com/posting-api/job-board/nope"
    respx_mock.get(url).respond(status_code=404, json={"errors": ["Job board not found"]})

    with pytest.raises(FetchError):
        ashby.fetch({"name": "Nope", "token": "nope"}, http)


def test_hydrate_is_noop(http, make_posting) -> None:
    posting = make_posting(source="ashby", ats_type="ashby")

    assert ashby.hydrate(posting, http) is posting
