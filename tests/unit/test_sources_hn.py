"""HN "Who is hiring?" fetcher: thread selection, top-level filtering, first-line parsing."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import httpx
import pytest

from jobhunt.sources import hn_hiring
from jobhunt.sources.base import Http

HIRING_THREAD_ID = "49522897"
STORY_PARAMS = {"tags": "story,author_whoishiring"}
COMMENT_PARAMS = {"tags": f"comment,story_{HIRING_THREAD_ID}"}
CFG = {"enabled": True, "max_comments": 2000, "title_prefix": "Ask HN: Who is hiring?"}


@pytest.fixture
def http() -> Iterator[Http]:
    client = httpx.Client()
    yield Http(client=client, retries=0, backoff_seconds=0.0)
    client.close()


def _mock_stories(respx_mock, payload):
    return respx_mock.get(hn_hiring.SEARCH_URL, params__contains=STORY_PARAMS).mock(
        return_value=httpx.Response(200, json=payload)
    )


def _mock_comments(respx_mock, pages: dict[str, dict]):
    def _respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[request.url.params.get("page", "0")])

    return respx_mock.get(hn_hiring.SEARCH_URL, params__contains=COMMENT_PARAMS).mock(
        side_effect=_respond
    )


def test_fetch_picks_hiring_thread_and_keeps_only_top_level_job_comments(
    http, load_fixture, respx_mock
):
    _mock_stories(respx_mock, load_fixture("ats/hn_stories.json"))
    comments = _mock_comments(respx_mock, {"0": load_fixture("ats/hn_comments.json")})

    postings = hn_hiring.fetch(CFG, http)

    assert comments.call_count == 1  # comments were requested for the hiring thread only
    assert [p.source_job_id for p in postings] == ["49576039", "49573833", "49571280"]
    permitflow, supero, deeter = postings
    assert permitflow.company == "PermitFlow (YC W22)"
    assert permitflow.title == "Staff, Fullstack & Frontend Software Engineers"
    assert permitflow.location == "New York, NY"
    assert permitflow.is_remote is False
    assert permitflow.url == "https://news.ycombinator.com/item?id=49576039"
    assert permitflow.posted_at == datetime(2026, 9, 5, 12, 47, 58, tzinfo=UTC)
    assert "Series B" in permitflow.description_md
    assert permitflow.raw["thread_id"] == HIRING_THREAD_ID
    assert permitflow.raw["segments"][:2] == [
        "PermitFlow (YC W22)",
        "Staff, Fullstack & Frontend Software Engineers",
    ]
    assert supero.title == "Cloud / Platform Engineer"
    assert supero.is_remote is True
    assert supero.location.startswith("REMOTE (SF Bay Area")
    assert "Bengaluru" in supero.location
    assert deeter.location == "REMOTE (WORLDWIDE)"
    assert deeter.is_remote is True
    assert all(p.source == "hn" and p.ats_type is None for p in postings)
    assert all(p.posted_at.tzinfo is not None for p in postings)


def test_fetch_extracts_apply_urls_from_careers_and_ats_links(http, load_fixture, respx_mock):
    _mock_stories(respx_mock, load_fixture("ats/hn_stories.json"))
    _mock_comments(respx_mock, {"0": load_fixture("ats/hn_comments.json")})

    permitflow, supero, deeter = hn_hiring.fetch(CFG, http)

    # The first link in the PermitFlow comment is the homepage; the Ashby board must win.
    assert permitflow.apply_url.startswith("https://jobs.ashbyhq.com/permitflow")
    assert supero.apply_url == "https://www.supero.dev/careers/cloud-platform-engineer/"
    assert deeter.apply_url.startswith("https://jobs.ashbyhq.com/deeter-analytics/")


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        (
            '<a href="https:&#x2F;&#x2F;acme.com">home</a> '
            '<a href="https:&#x2F;&#x2F;acme.com&#x2F;careers&#x2F;123">apply</a>',
            "https://acme.com/careers/123",
        ),
        (
            '<a href="https://boards.greenhouse.io/acme/jobs/1">x</a>',
            ("https://boards.greenhouse.io/acme/jobs/1"),
        ),
        ('<a href="https://acme.com">home</a>', None),
        ('<a href="mailto:jobs@acme.com">mail</a>', None),
        ("no links here", None),
    ],
)
def test_apply_url_extraction(html, expected):
    assert hn_hiring._apply_url(html) == expected


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (
            "Acme | Software Engineer | Remote (US)",
            ("Acme", "Software Engineer", "Remote (US)", True),
        ),
        (
            "Acme | Backend Engineer | Bengaluru, India | ONSITE",
            ("Acme", "Backend Engineer", "Bengaluru, India", False),
        ),
        (
            "Acme | Staff Engineer | REMOTE | Full-time | $150k-$200k | https://acme.com/jobs",
            ("Acme", "Staff Engineer", None, True),
        ),
        (
            "Acme | Engineer | Hybrid | London, UK | 120k GBP",
            ("Acme", "Engineer", "London, UK", False),
        ),
        ("Acme | Engineer", ("Acme", "Engineer", None, False)),
    ],
)
def test_parse_first_line(line, expected):
    parsed = hn_hiring._parse_first_line(line)
    assert parsed is not None
    assert (parsed.company, parsed.title, parsed.location, parsed.is_remote) == expected


@pytest.mark.parametrize("line", ["Just a comment without pipes", "", "Acme |", "   |   "])
def test_parse_first_line_rejects_non_job_lines(line):
    assert hn_hiring._parse_first_line(line) is None


def test_first_line_stops_at_paragraph_break_or_newline_and_unescapes():
    html = 'Acme | Cloud &#x2F; Platform Engineer | <a href="x">https:&#x2F;&#x2F;a.b</a>\nBody'
    assert hn_hiring._first_line(html) == "Acme | Cloud / Platform Engineer | https://a.b"
    assert hn_hiring._first_line("Acme | Role<p>Body") == "Acme | Role"


def test_deleted_comment_without_text_is_skipped():
    hit = {"objectID": "1", "comment_text": None, "parent_id": 9, "story_id": 9}
    assert hn_hiring._to_posting(hit, {"objectID": "9", "title": "t"}) is None


def test_max_comments_caps_how_many_comments_are_considered(http, load_fixture, respx_mock):
    _mock_stories(respx_mock, load_fixture("ats/hn_stories.json"))
    _mock_comments(respx_mock, {"0": load_fixture("ats/hn_comments.json")})

    postings = hn_hiring.fetch({**CFG, "max_comments": 2}, http)

    assert [p.source_job_id for p in postings] == ["49576039", "49573833"]


def test_fetch_pages_through_all_comment_pages(http, load_fixture, respx_mock):
    full = load_fixture("ats/hn_comments.json")
    page0 = {**full, "hits": full["hits"][:2], "nbPages": 2, "page": 0}
    page1 = {**full, "hits": full["hits"][2:], "nbPages": 2, "page": 1}
    _mock_stories(respx_mock, load_fixture("ats/hn_stories.json"))
    comments = _mock_comments(respx_mock, {"0": page0, "1": page1})

    postings = hn_hiring.fetch(CFG, http)

    assert comments.call_count == 2
    assert [p.source_job_id for p in postings] == ["49576039", "49573833", "49571280"]


def test_fetch_returns_empty_when_no_story_matches_prefix(http, load_fixture, respx_mock):
    _mock_stories(respx_mock, load_fixture("ats/hn_stories.json"))

    assert hn_hiring.fetch({**CFG, "title_prefix": "Ask HN: Freelancer?"}, http) == []


def test_hydrate_is_a_noop(http, make_posting):
    posting = make_posting(source="hn")
    assert hn_hiring.hydrate(posting, http) is posting
