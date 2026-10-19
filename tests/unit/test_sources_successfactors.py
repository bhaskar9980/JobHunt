"""SuccessFactors job feed: item parsing, title cleanup, expiry, and no hydration needed."""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
import pytest

from jobhunt.models import JobPosting
from jobhunt.sources import successfactors
from jobhunt.sources.base import FetchError, Http

HOST = "careers.wipro.com"
FEED_URL = f"https://{HOST}/jobfeed.xml"
FIRST_ID = "1365902855"
FIRST_URL = (
    "https://careers.wipro.com/job/Bengaluru-Senior-Consultant-"
    "%28Application-Security-testing-SAST-and-SCA%29-IND-560035/1365902855/"
)


@pytest.fixture
def cfg() -> dict[str, Any]:
    return {
        "name": "Wipro",
        "ats": "successfactors",
        "host": HOST,
        "feed": "/jobfeed.xml",
        "aliases": [],
        "domains": ["wipro.com"],
        "enabled": True,
    }


@pytest.fixture(scope="module")
def http() -> Http:
    """One client per module: building an httpx.Client loads the SSL context (~2s on Windows)."""
    wrapper = Http(client=httpx.Client(), retries=0, backoff_seconds=0.0)
    yield wrapper
    wrapper.close()


@pytest.fixture
def feed(load_fixture) -> str:
    return load_fixture("ats/successfactors_feed.xml")


@pytest.fixture
def unexpired(monkeypatch) -> date:
    today = date(2026, 9, 9)
    monkeypatch.setattr(successfactors, "today", lambda: today)
    return today


def test_fetch_parses_every_item(respx_mock, cfg, http, feed, unexpired):
    route = respx_mock.get(FEED_URL).mock(return_value=httpx.Response(200, text=feed))

    postings = successfactors.fetch(cfg, http)

    assert route.call_count == 1
    assert len(postings) == 3
    first = postings[0]
    assert first.source == "successfactors"
    assert first.ats_type == "successfactors"
    assert first.company == "Wipro"
    assert first.source_job_id == f"{HOST}:wipro:{FIRST_ID}"
    assert first.url == FIRST_URL
    assert first.apply_url == FIRST_URL
    assert first.location == "Bengaluru, IND-29, IN, 560035, India"
    assert first.raw["employer"] == "Wipro Limited"


def test_the_description_arrives_inline_so_nothing_needs_hydration(
    respx_mock, cfg, http, feed, unexpired
):
    respx_mock.get(FEED_URL).mock(return_value=httpx.Response(200, text=feed))

    postings = successfactors.fetch(cfg, http)

    assert all(p.needs_hydration is False for p in postings)
    assert all(p.description_md for p in postings)
    assert all("<div" not in (p.description_md or "") for p in postings)


def test_a_trailing_location_is_removed_from_the_title(respx_mock, cfg, http, feed, unexpired):
    respx_mock.get(FEED_URL).mock(return_value=httpx.Response(200, text=feed))

    titles = [p.title for p in successfactors.fetch(cfg, http)]

    assert "Azure Integration Lead" in titles
    assert "Developer - L3" in titles
    # A parenthetical that is not the location is part of the role and stays.
    assert "Senior Consultant (Application Security testing - SAST and SCA)" in titles


def test_expired_items_are_skipped(respx_mock, cfg, http, feed, monkeypatch):
    monkeypatch.setattr(successfactors, "today", lambda: date(2026, 12, 25))
    respx_mock.get(FEED_URL).mock(return_value=httpx.Response(200, text=feed))

    assert successfactors.fetch(cfg, http) == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("IN", "India"),
        ("US", "United States"),
        ("Bengaluru, IND-29, IN, 560035", "Bengaluru, IND-29, IN, 560035, India"),
        ("", None),
    ],
)
def test_a_bare_country_code_is_expanded(raw, expected):
    assert successfactors.expand_location(raw) == expected


def test_items_without_a_link_or_id_are_dropped(respx_mock, cfg, http, feed, unexpired):
    broken = feed.replace(
        "<link>https://careers.wipro.com/job/Kochi-Developer-L3-IND/1169278255/</link>", ""
    )
    respx_mock.get(FEED_URL).mock(return_value=httpx.Response(200, text=broken))

    postings = successfactors.fetch(cfg, http)

    assert len(postings) == 2
    assert all(p.title != "Developer - L3" for p in postings)


def test_an_explicit_full_feed_url_is_used_as_given(respx_mock, cfg, http, feed, unexpired):
    cfg["feed"] = "https://jobs.sap.com/sitemap.xml"
    route = respx_mock.get("https://jobs.sap.com/sitemap.xml").mock(
        return_value=httpx.Response(200, text=feed)
    )

    successfactors.fetch(cfg, http)

    assert route.call_count == 1


def test_the_default_feed_path_is_used_when_none_is_configured(
    respx_mock, cfg, http, feed, unexpired
):
    cfg.pop("feed")
    route = respx_mock.get(FEED_URL).mock(return_value=httpx.Response(200, text=feed))

    successfactors.fetch(cfg, http)

    assert route.call_count == 1


def test_search_text_filters_the_dump_client_side(respx_mock, cfg, http, feed, unexpired):
    cfg["search_text"] = "azure, c#"
    respx_mock.get(FEED_URL).mock(return_value=httpx.Response(200, text=feed))

    titles = [p.title for p in successfactors.fetch(cfg, http)]

    assert titles == ["Azure Integration Lead"]


def test_search_text_also_reads_the_description(respx_mock, cfg, http, feed, unexpired):
    cfg["search_text"] = "wipro limited"
    respx_mock.get(FEED_URL).mock(return_value=httpx.Response(200, text=feed))

    assert len(successfactors.fetch(cfg, http)) == 3


def test_a_search_text_that_matches_nothing_returns_nothing(respx_mock, cfg, http, feed, unexpired):
    cfg["search_text"] = "cobol"
    respx_mock.get(FEED_URL).mock(return_value=httpx.Response(200, text=feed))

    assert successfactors.fetch(cfg, http) == []


def test_fetch_needs_a_host(cfg, http):
    cfg["host"] = None

    with pytest.raises(ValueError, match="successfactors source requires"):
        successfactors.fetch(cfg, http)


def test_a_body_that_is_not_a_feed_is_rejected(respx_mock, cfg, http):
    respx_mock.get(FEED_URL).mock(return_value=httpx.Response(200, text="<html>nope</html>"))

    with pytest.raises(ValueError, match="no RSS items"):
        successfactors.fetch(cfg, http)


def test_malformed_xml_is_rejected(respx_mock, cfg, http):
    respx_mock.get(FEED_URL).mock(return_value=httpx.Response(200, text="<rss><channel>"))

    with pytest.raises(ValueError, match="not parse"):
        successfactors.fetch(cfg, http)


def test_a_failed_feed_call_propagates(respx_mock, cfg, http):
    respx_mock.get(FEED_URL).mock(return_value=httpx.Response(503, text=""))

    with pytest.raises(FetchError):
        successfactors.fetch(cfg, http)


def test_hydrate_is_a_no_op(http):
    posting = JobPosting(
        source="successfactors",
        source_job_id=FIRST_ID,
        company="Wipro",
        title="Azure Integration Lead",
        url=FIRST_URL,
    )

    assert successfactors.hydrate(posting, http) is posting


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Chennai, IND-32, IN", "Chennai, IND-32, IN, India"),
        ("Bengaluru, IND-29, IN, 560035", "Bengaluru, IND-29, IN, 560035, India"),
        ("Warsaw, PL", "Warsaw, PL, Poland"),
        # Already spelled out, so nothing is appended.
        ("Bengaluru, India", "Bengaluru, India"),
        # No country part at all.
        ("Remote", "Remote"),
    ],
)
def test_a_trailing_country_code_is_named(raw, expected):
    """So an Indian city the alias table does not know yet still buckets as India."""
    assert successfactors.expand_location(raw) == expected
