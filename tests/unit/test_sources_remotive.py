"""Remotive fetcher: one call, field mapping, attribution line, tz-aware dates."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import httpx
import pytest

from jobhunt.sources import remotive
from jobhunt.sources.base import FetchError, Http

CFG = {"enabled": True, "category": "software-dev", "limit": 200}


@pytest.fixture
def http() -> Iterator[Http]:
    client = httpx.Client()
    yield Http(client=client, retries=0, backoff_seconds=0.0)
    client.close()


def test_fetch_maps_jobs_to_postings(http, load_fixture, respx_mock):
    route = respx_mock.get(
        remotive.API_URL, params__contains={"category": "software-dev", "limit": "200"}
    ).mock(return_value=httpx.Response(200, json=load_fixture("ats/remotive.json")))

    postings = remotive.fetch(CFG, http)

    assert route.call_count == 1
    assert [p.source_job_id for p in postings] == ["2091101", "2069746"]
    lemon, mitre = postings
    assert lemon.company == "Lemon.io"
    assert lemon.title == "Senior React Full-stack Developer"
    assert lemon.url.startswith("https://remotive.com/remote-jobs/")
    assert lemon.apply_url == lemon.url
    assert lemon.employment_type == "full_time"
    assert lemon.salary_text is None
    assert mitre.company == "Mitre Media"
    assert mitre.salary_text == "$170k - $200k"
    assert mitre.location == "USA, Canada, USA timezones"
    assert lemon.location == "LATAM, Europe, USA, Canada, APAC"
    assert all(p.is_remote for p in postings)
    assert all(p.source == "remotive" and p.ats_type is None for p in postings)
    assert lemon.raw["category"] == "Software Development"


def test_posted_at_is_tz_aware_utc(http, load_fixture, respx_mock):
    respx_mock.get(remotive.API_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("ats/remotive.json"))
    )

    lemon, mitre = remotive.fetch(CFG, http)

    assert lemon.posted_at == datetime(2026, 8, 27, 14, 36, 9, tzinfo=UTC)
    assert mitre.posted_at == datetime(2026, 8, 14, 20, 33, 39, tzinfo=UTC)
    assert lemon.posted_at.tzinfo is not None


def test_description_ends_with_attribution_line(http, load_fixture, respx_mock):
    respx_mock.get(remotive.API_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("ats/remotive.json"))
    )

    for posting in remotive.fetch(CFG, http):
        assert posting.description_md.endswith(f"Source: Remotive ({posting.url})")
        assert "Lemon.io" in posting.description_md or "Mitre Media" in posting.description_md


def test_fetch_sends_category_and_limit_from_cfg(http, respx_mock):
    route = respx_mock.get(
        remotive.API_URL, params__contains={"category": "devops", "limit": "50"}
    ).mock(return_value=httpx.Response(200, json={"job-count": 0, "jobs": []}))

    assert remotive.fetch({"category": "devops", "limit": 50}, http) == []
    assert route.called


def test_server_error_raises_fetch_error(http, respx_mock):
    respx_mock.get(remotive.API_URL).mock(return_value=httpx.Response(500))

    with pytest.raises(FetchError):
        remotive.fetch(CFG, http)


def test_job_without_url_or_title_is_skipped():
    assert remotive._to_posting({"id": 1, "title": "x"}) is None
    assert remotive._to_posting({"id": 1, "url": "https://remotive.com/remote-jobs/x-1"}) is None


def test_attribution_present_even_without_description():
    posting = remotive._to_posting(
        {"id": 5, "url": "https://remotive.com/remote-jobs/x-5", "title": "X", "salary": ""}
    )
    assert posting is not None
    assert posting.company == "Unknown"
    assert posting.description_md == "Source: Remotive (https://remotive.com/remote-jobs/x-5)"


def test_hydrate_is_a_noop(http, make_posting):
    posting = make_posting(source="remotive")
    assert remotive.hydrate(posting, http) is posting


def test_client_side_category_filter_drops_other_categories(http, load_fixture, respx_mock):
    payload = load_fixture("ats/remotive.json")
    payload["jobs"][0]["category"] = "Marketing"
    respx_mock.get(remotive.API_URL).respond(json=payload)
    postings = remotive.fetch({}, http)
    assert len(postings) == len(payload["jobs"]) - 1
    assert all(p.raw["category"] == "Software Development" for p in postings)


def test_keep_categories_can_be_widened(http, load_fixture, respx_mock):
    payload = load_fixture("ats/remotive.json")
    payload["jobs"][0]["category"] = "DevOps / Sysadmin"
    respx_mock.get(remotive.API_URL).respond(json=payload)
    cfg = {"keep_categories": ["software development", "devops / sysadmin"]}
    assert len(remotive.fetch(cfg, http)) == len(payload["jobs"])
