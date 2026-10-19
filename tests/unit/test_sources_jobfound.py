"""Jobfound RSS fetcher: slug parsing, guids, dates, remote flag, hydration from the job page."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import httpx
import pytest

from jobhunt.sources import get_module, jobfound
from jobhunt.sources.base import FetchError, Http

FEED = "https://jobfound.org/feed.xml"
HIVER_URL = "https://jobfound.org/job/hiver-is-hiring-for-sde-intern-bangalore-hybrid-india-8-september-2026"
HIVER_APPLY = (
    "https://hiverhq.pinpointhq.com/postings/5380a35c-4644-4a46-bf6b-7ecb42e285dd"
    "?source=jobfound.org"
)


@pytest.fixture(scope="module")
def http() -> Iterator[Http]:
    """One client per module: building an httpx.Client loads the SSL context (~2s on Windows)."""
    client = httpx.Client()
    yield Http(client=client, retries=0, backoff_seconds=0.0)
    client.close()


@pytest.fixture
def feed_text(load_fixture) -> str:
    return load_fixture("ats/jobfound.rss")


def test_fetch_maps_entries(http, load_fixture, feed_text, respx_mock):
    route = respx_mock.get(FEED).mock(return_value=httpx.Response(200, text=feed_text))

    postings = jobfound.fetch({"enabled": True, "feed": FEED}, http)

    assert route.call_count == 1
    assert [p.source_job_id for p in postings] == [
        "6aa06169000999b57ae5",
        "6aa061350007fce878c4",
        "6aa0423b0014d24fa439",
    ]
    swift, hiver, superkalam = postings
    assert swift.source == "jobfound"
    assert swift.company == "Swift"
    assert swift.title == "Data Analyst Intern"
    assert swift.location == "dehradun india"
    assert swift.url.startswith("https://jobfound.org/job/swift-is-hiring-for-data-analyst-intern")
    assert swift.needs_hydration is True
    assert swift.detail_ref == swift.url
    assert swift.description_md.startswith("About the Job")
    assert hiver.company == "Hiver"
    assert hiver.location == "bangalore hybrid india"
    assert hiver.is_remote is False
    assert superkalam.title == "AI/ML Research Intern"
    assert superkalam.location == "bengaluru remote india"
    assert superkalam.is_remote is True


def test_posted_at_is_tz_aware_utc(http, feed_text, respx_mock):
    respx_mock.get(FEED).mock(return_value=httpx.Response(200, text=feed_text))
    postings = jobfound.fetch({"feed": FEED}, http)
    assert postings[0].posted_at == datetime(2026, 9, 8, 19, 26, 32, tzinfo=UTC)
    assert all(p.posted_at.tzinfo is not None for p in postings)


def test_fetch_defaults_to_public_feed_url(http, feed_text, respx_mock):
    route = respx_mock.get(jobfound.DEFAULT_FEED).mock(
        return_value=httpx.Response(200, text=feed_text)
    )
    jobfound.fetch({}, http)
    assert route.called


@pytest.mark.parametrize(
    ("slug", "title", "expected"),
    [
        (
            "swift-is-hiring-for-data-analyst-intern-dehradun-india-8-september-2026",
            "Data Analyst Intern",
            ("Swift", "dehradun india"),
        ),
        (
            "brevo-is-hiring-for-associate-platform-engineer-data-storage-noida-uttar-pradesh"
            "-hybrid-india-8-september-2026",
            "Associate Platform Engineer - Data Storage",
            ("Brevo", "noida uttar pradesh hybrid india"),
        ),
        (
            "magellanix-technology-solutions-is-hiring-for-software-technology-intern-pan-india"
            "-remote-india-8-september-2026",
            "Software / Technology Intern",
            ("Magellanix Technology Solutions", "pan india remote india"),
        ),
        # slug title shorter than the entry title: strip the matching prefix only
        (
            "think41-is-hiring-for-full-stack-development-bangalore-india-8-september-2026",
            "Full Stack Development - Internship",
            ("Think41", "bangalore india"),
        ),
        # title does not appear in the slug: location cannot be trusted
        (
            "acme-is-hiring-for-something-else-entirely-8-september-2026",
            "Backend Engineer",
            ("Acme", None),
        ),
        # location missing: date directly after the title
        (
            "acme-is-hiring-for-backend-engineer-8-september-2026",
            "Backend Engineer",
            ("Acme", None),
        ),
        ("not-a-jobfound-slug", "Backend Engineer", ("Unknown", None)),
        ("-is-hiring-for-backend-engineer-8-september-2026", "Backend Engineer", ("Unknown", None)),
    ],
)
def test_parse_slug(slug, title, expected):
    assert jobfound.parse_slug(slug, title) == expected


def test_entries_without_link_or_title_are_skipped(http, respx_mock):
    body = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
      <item><title>Only title</title><guid isPermaLink="false">abc</guid></item>
      <item><link>https://jobfound.org/job/acme-is-hiring-for-x-pune-india-1-may-2026</link>
            <guid isPermaLink="false">def</guid></item>
      <item><title>Kept</title>
            <link>https://jobfound.org/job/acme-is-hiring-for-kept-pune-india-1-may-2026</link>
            <guid isPermaLink="false">ghi</guid></item>
    </channel></rss>"""
    respx_mock.get(FEED).mock(return_value=httpx.Response(200, text=body))
    postings = jobfound.fetch({"feed": FEED}, http)
    assert [p.source_job_id for p in postings] == ["ghi"]
    assert postings[0].posted_at is None


def test_hydrate_reads_apply_url_description_location_from_job_page(
    http, load_fixture, feed_text, respx_mock
):
    respx_mock.get(FEED).mock(return_value=httpx.Response(200, text=feed_text))
    hiver = jobfound.fetch({"feed": FEED}, http)[1]
    respx_mock.get(HIVER_URL).mock(
        return_value=httpx.Response(200, text=load_fixture("ats/jobfound_detail.html"))
    )

    hydrated = jobfound.hydrate(hiver, http)

    assert hydrated.apply_url == HIVER_APPLY
    assert hydrated.location == "Bangalore, India"
    assert hydrated.employment_type == "INTERN"
    assert hydrated.needs_hydration is False
    assert hydrated.description_md.startswith("### About us:")
    assert "### Key Responsibilities" in hydrated.description_md
    assert len(hydrated.description_md) > len(hiver.description_md)
    # unchanged identity fields
    assert hydrated.source_job_id == hiver.source_job_id
    assert hydrated.url == hiver.url


def test_hydrate_keeps_existing_values_when_page_has_no_payload(http, respx_mock, make_posting):
    posting = make_posting(
        source="jobfound",
        source_job_id="x1",
        url="https://jobfound.org/job/acme-is-hiring-for-x-pune-india-1-may-2026",
        location="pune india",
        description_md="summary",
        needs_hydration=True,
    )
    respx_mock.get(posting.url).mock(
        return_value=httpx.Response(200, text="<html><body><p>nothing here</p></body></html>")
    )
    hydrated = jobfound.hydrate(posting, http)
    assert hydrated.apply_url == posting.apply_url
    assert hydrated.location == "pune india"
    assert hydrated.description_md == "summary"
    assert hydrated.needs_hydration is False


def test_payload_value_unescapes_js_string_escapes():
    html = r"x \"applyUrl\":\"https://a.example/apply?a=1&b=2\" y"
    assert jobfound._payload_value(jobfound._APPLY_URL, html) == "https://a.example/apply?a=1&b=2"


def test_server_error_raises_fetch_error(http, respx_mock):
    respx_mock.get(FEED).mock(return_value=httpx.Response(500))
    with pytest.raises(FetchError):
        jobfound.fetch({"feed": FEED}, http)


def test_module_is_registered():
    assert get_module("jobfound") is jobfound
