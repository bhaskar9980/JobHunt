"""We Work Remotely RSS fetcher: title split, custom elements, expiry, dedupe across feeds."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import httpx
import pytest
from freezegun import freeze_time

from jobhunt.sources import wwr
from jobhunt.sources.base import Http

FEED_A = "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss"
FEED_B = "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss"
WWR_JOB_BASE = "https://weworkremotely.com/remote-jobs/"
PRINCETON_GUID = WWR_JOB_BASE + "princeton-university-senior-peoplesoft-developer-analyst-ii"
# Fixture items expire on 2 and 3 Oct 2026; freeze the clock so the tests never age out.
FROZEN_NOW = "2026-09-08T12:00:00+00:00"
BETWEEN_EXPIRIES = "2026-10-03T00:00:00+00:00"


@pytest.fixture
def http() -> Iterator[Http]:
    client = httpx.Client()
    yield Http(client=client, retries=0, backoff_seconds=0.0)
    client.close()


def _mock_feed(respx_mock, url: str, body: str):
    return respx_mock.get(url).mock(return_value=httpx.Response(200, text=body))


@freeze_time(FROZEN_NOW)
def test_fetch_parses_entries_and_custom_elements(http, load_fixture, respx_mock):
    _mock_feed(respx_mock, FEED_A, load_fixture("ats/wwr.rss"))

    postings = wwr.fetch({"enabled": True, "feeds": [FEED_A]}, http)

    assert len(postings) == 2
    princeton, grafana = postings
    assert princeton.company == "Princeton University"
    assert princeton.title == "Senior PeopleSoft Developer/Analyst II"
    assert grafana.company == "Grafana Labs"
    assert grafana.title == "Backend Engineer - Platform - Stacks | Ireland | Remote"
    assert princeton.location == "Anywhere in the World"
    assert princeton.employment_type == "Full-Time"
    assert princeton.is_remote is True
    assert princeton.posted_at == datetime(2026, 9, 3, 13, 24, 14, tzinfo=UTC)
    assert princeton.source_job_id == PRINCETON_GUID
    assert princeton.url == princeton.apply_url == PRINCETON_GUID
    assert "Headquarters" in princeton.description_md
    assert princeton.raw["category"] == "Back-End Programming"
    assert princeton.raw["expires_at"] == "2026-10-03T13:24:14+00:00"
    assert princeton.raw["feed"] == FEED_A
    assert all(p.source == "wwr" and p.ats_type is None for p in postings)


@freeze_time(BETWEEN_EXPIRIES)
def test_expired_entries_are_skipped(http, load_fixture, respx_mock):
    _mock_feed(respx_mock, FEED_A, load_fixture("ats/wwr.rss"))

    postings = wwr.fetch({"feeds": [FEED_A]}, http)

    assert [p.company for p in postings] == ["Princeton University"]


@freeze_time(FROZEN_NOW)
def test_same_guid_across_two_feeds_is_kept_once(http, load_fixture, respx_mock):
    body = load_fixture("ats/wwr.rss")
    feed_a = _mock_feed(respx_mock, FEED_A, body)
    feed_b = _mock_feed(respx_mock, FEED_B, body)

    postings = wwr.fetch({"feeds": [FEED_A, FEED_B]}, http)

    assert feed_a.called and feed_b.called
    assert len(postings) == 2
    assert len({p.source_job_id for p in postings}) == 2


def test_malformed_feed_yields_no_postings(http, respx_mock):
    _mock_feed(respx_mock, FEED_A, "definitely not xml")

    assert wwr.fetch({"feeds": [FEED_A]}, http) == []


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Acme: Backend Engineer", ("Acme", "Backend Engineer")),
        ("Acme: Senior Engineer: Platform", ("Acme", "Senior Engineer: Platform")),
        ("Backend Engineer", ("Unknown", "Backend Engineer")),
        ("", ("Unknown", "")),
    ],
)
def test_split_title(title, expected):
    assert wwr._split_title(title) == expected


def test_rfc822_dates_become_tz_aware_utc():
    parsed = wwr._parse_rfc822("Sat, 03 Oct 2026 13:24:14 +0000")
    assert parsed == datetime(2026, 10, 3, 13, 24, 14, tzinfo=UTC)
    assert wwr._parse_rfc822("garbage") is None
    assert wwr._parse_rfc822(None) is None


def test_hydrate_is_a_noop(http, make_posting):
    posting = make_posting(source="wwr")
    assert wwr.hydrate(posting, http) is posting
