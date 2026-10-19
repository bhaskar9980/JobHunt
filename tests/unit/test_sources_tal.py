"""Tal "jobs in the wild" fetcher: card parsing, relative times, dedupe, detail hydration."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from freezegun import freeze_time

from jobhunt.sources import get_module, tal
from jobhunt.sources.base import FetchError, Http

BASE = "https://jobsinthewild.tal.af/"
CFG = {
    "enabled": True,
    "base_url": BASE,
    "queries": ["sort=new", "sort=hot"],
    "default_location": "Bengaluru, India",
}
FROZEN_NOW = "2026-09-08T12:00:00+00:00"
REFERENCE = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
PEEKO_POST = (
    "https://www.linkedin.com/posts/alex-founder_product-manager-at-peeko-share-"
    "7503039556879982592-6yQO"
)


@pytest.fixture(scope="module")
def http() -> Iterator[Http]:
    """One client per module: building an httpx.Client loads the SSL context (~2s on Windows)."""
    client = httpx.Client()
    yield Http(client=client, retries=0, backoff_seconds=0.0)
    client.close()


@pytest.fixture
def listing(load_fixture) -> str:
    return load_fixture("ats/tal_list.html")


def _mock_listing(respx_mock, listing: str):
    return {
        query: respx_mock.get(f"{BASE}?{query}").mock(
            return_value=httpx.Response(200, text=listing)
        )
        for query in CFG["queries"]
    }


@freeze_time(FROZEN_NOW)
def test_fetch_parses_cards(http, listing, respx_mock):
    routes = _mock_listing(respx_mock, listing)

    postings = tal.fetch(CFG, http)

    assert all(route.call_count == 1 for route in routes.values())
    assert [p.source_job_id for p in postings] == ["2494", "2492", "2493"]
    peeko, designer, bricx = postings
    assert peeko.source == "tal"
    assert peeko.company == "Peeko"
    assert peeko.title == "Product Manager"
    assert peeko.url == f"{BASE}j/2494"
    assert peeko.detail_ref == "/j/2494"
    assert peeko.location == "Bengaluru, India"
    assert peeko.is_remote is False
    assert peeko.needs_hydration is True
    assert peeko.apply_url is None
    assert peeko.description_md.startswith("🚀 We’re Hiring | Product Manager at Peeko")
    assert peeko.posted_at == REFERENCE - timedelta(hours=8)
    assert peeko.raw["author"] == "Alex Founder"
    assert peeko.raw["headline"] == "Building Peeko"
    assert peeko.raw["when"] == "8h ago"
    assert peeko.raw["query"] == "sort=new"
    assert designer.company == "Unknown"
    assert designer.title == "Product Designer"
    assert designer.posted_at == REFERENCE - timedelta(days=1)
    assert bricx.company == "Bricx"
    assert bricx.is_remote is True
    assert bricx.posted_at == REFERENCE - timedelta(days=6)


@freeze_time(FROZEN_NOW)
def test_fetch_dedupes_ids_across_queries(http, listing, respx_mock):
    _mock_listing(respx_mock, listing)
    postings = tal.fetch(CFG, http)
    assert len(postings) == len({p.source_job_id for p in postings}) == 3


@freeze_time(FROZEN_NOW)
def test_fetch_uses_defaults_when_cfg_is_sparse(http, listing, respx_mock):
    route = respx_mock.get(f"{tal.DEFAULT_BASE_URL}?sort=new").mock(
        return_value=httpx.Response(200, text=listing)
    )
    postings = tal.fetch({}, http)
    assert route.called
    assert postings[0].location is None


def test_cards_without_role_are_skipped(http, respx_mock):
    html = """<section class="posts">
      <article class="post-frame" data-href="/j/1"><div class="body">no role here</div></article>
      <article class="post-frame" data-href="/j/2">
        <div class="role-panel"><div class="role-name">Hiring a Backend Engineer</div></div>
      </article>
      <article class="post-frame" data-href="/nope"><span class="role-chip-role">X</span></article>
    </section>"""
    respx_mock.get(f"{BASE}?sort=new").mock(return_value=httpx.Response(200, text=html))
    postings = tal.fetch({"base_url": BASE, "queries": ["sort=new"]}, http)
    assert [(p.source_job_id, p.title) for p in postings] == [("2", "Hiring a Backend Engineer")]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("8h ago", REFERENCE - timedelta(hours=8)),
        ("1d ago", REFERENCE - timedelta(days=1)),
        ("3d ago", REFERENCE - timedelta(days=3)),
        ("2w ago", REFERENCE - timedelta(weeks=2)),
        ("45m ago", REFERENCE - timedelta(minutes=45)),
        ("3mo ago", REFERENCE - timedelta(days=90)),
        ("just now", REFERENCE),
        ("yesterday", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_relative(text, expected):
    assert tal.parse_relative(text, REFERENCE) == expected


@freeze_time(FROZEN_NOW)
def test_hydrate_maps_detail_page(http, listing, load_fixture, respx_mock):
    _mock_listing(respx_mock, listing)
    peeko = tal.fetch(CFG, http)[0]
    respx_mock.get(peeko.url).mock(
        return_value=httpx.Response(200, text=load_fixture("ats/tal_detail.html"))
    )

    hydrated = tal.hydrate(peeko, http)

    assert hydrated.apply_url == PEEKO_POST
    assert hydrated.location == "Bangalore, India"
    assert hydrated.needs_hydration is False
    assert hydrated.is_remote is False
    assert hydrated.raw["tags"] == ["product", "mid", "Bangalore", "seed"]
    assert hydrated.raw["apply_hint"] == "https://lnkd.in/gDac89vv"
    assert hydrated.raw["author"] == "Alex Founder"
    assert "Apply: https://lnkd.in/gDac89vv" in hydrated.description_md
    assert hydrated.source_job_id == "2494"
    assert hydrated.url == peeko.url


def test_hydrate_keeps_values_when_detail_is_bare(http, respx_mock, make_posting):
    posting = make_posting(
        source="tal",
        source_job_id="9",
        url=f"{BASE}j/9",
        location="Bengaluru, India",
        description_md="card text",
        needs_hydration=True,
    )
    respx_mock.get(posting.url).mock(
        return_value=httpx.Response(200, text="<html><body><p>gone</p></body></html>")
    )
    hydrated = tal.hydrate(posting, http)
    assert hydrated.description_md == "card text"
    assert hydrated.location == "Bengaluru, India"
    assert hydrated.apply_url == posting.apply_url
    assert hydrated.raw["tags"] == []
    assert hydrated.raw["apply_hint"] is None
    assert hydrated.needs_hydration is False


def test_hydrate_remote_tag_and_work_mode_is_not_a_city(http, respx_mock, make_posting):
    posting = make_posting(source="tal", source_job_id="7", url=f"{BASE}j/7", needs_hydration=True)
    html = """<article class="post-frame"><div class="body">Hiring</div>
      <div class="tag-strip"><span class="tag role">eng</span><span class="tag">remote</span>
      <span class="tag">Pune</span></div>
      <a class="btn-apply" href="https://x.com/someone/status/1">message boss</a></article>"""
    respx_mock.get(posting.url).mock(return_value=httpx.Response(200, text=html))
    hydrated = tal.hydrate(posting, http)
    assert hydrated.is_remote is True
    assert hydrated.location == "Pune, India"
    assert hydrated.apply_url == "https://x.com/someone/status/1"


def test_server_error_raises_fetch_error(http, respx_mock):
    respx_mock.get(f"{BASE}?sort=new").mock(return_value=httpx.Response(503))
    with pytest.raises(FetchError):
        tal.fetch({"base_url": BASE, "queries": ["sort=new"]}, http)


def test_module_is_registered():
    assert get_module("tal") is tal
    assert get_module("jobfound").KEY == "jobfound"
