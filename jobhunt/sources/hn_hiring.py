"""Hacker News "Ask HN: Who is hiring?" via the Algolia HN Search API.

Top-level comments follow the convention ``Company | Role | Location | REMOTE | ...``; the first
line is parsed into structured fields and the whole comment becomes the description.
"""

from __future__ import annotations

import html as html_lib
import logging
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from jobhunt.clock import parse_iso
from jobhunt.models import JobPosting
from jobhunt.sources.base import Http, html_to_md

log = logging.getLogger(__name__)

KEY = "hn"

SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
ITEM_URL = "https://news.ycombinator.com/item?id={item_id}"
STORY_TAGS = "story,author_whoishiring"
COMMENT_TAGS = "comment,story_{story_id}"
STORY_HITS_PER_PAGE = 6
COMMENT_HITS_PER_PAGE = 1000
DEFAULT_TITLE_PREFIX = "Ask HN: Who is hiring?"
DEFAULT_MAX_COMMENTS = 2000

SEGMENT_SEPARATOR = "|"
LOCATION_JOINER = " / "
MIN_SEGMENTS = 2
MAX_COMPANY_CHARS = 80
MAX_TITLE_CHARS = 160
MAX_LOCATION_CHARS = 120

# Segments that describe the work arrangement rather than a place (compared after stripping
# everything but letters).
ARRANGEMENT_WORDS = frozenset(
    {
        "remote",
        "onsite",
        "hybrid",
        "fulltime",
        "parttime",
        "contract",
        "contractor",
        "freelance",
        "intern",
        "interns",
        "internship",
        "permanent",
        "visa",
        "visasponsorship",
        "novisa",
    }
)
APPLY_URL_HINTS = (
    "career",
    "job",
    "apply",
    "hiring",
    "greenhouse",
    "lever.co",
    "ashbyhq",
    "workable",
    "recruitee",
    "bamboohr",
    "smartrecruiters",
    "myworkdayjobs",
    "rippling",
    "wellfound",
)

_FIRST_LINE_BREAK = re.compile(r"<p>|<br\s*/?>|\n", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_HREF = re.compile(r'href="([^"]+)"', re.IGNORECASE)
_NON_LETTERS = re.compile(r"[^a-z]")
_URL_SEGMENT = re.compile(r"^(https?://|www\.)", re.IGNORECASE)
_MONEY_SEGMENT = re.compile(r"[$€£₹]|\b\d{2,3}k\b|\b(usd|eur|gbp|inr)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class FirstLine:
    company: str
    title: str
    location: str | None
    is_remote: bool
    segments: tuple[str, ...]


def fetch(cfg: Mapping[str, Any], http: Http) -> list[JobPosting]:
    prefix = str(cfg.get("title_prefix", DEFAULT_TITLE_PREFIX))
    max_comments = int(cfg.get("max_comments", DEFAULT_MAX_COMMENTS))
    thread = _find_thread(prefix, http)
    if thread is None:
        log.warning("hn: no recent whoishiring story starts with %r", prefix)
        return []
    story_id = str(thread["objectID"])
    log.info("hn: using thread %s %r", story_id, thread.get("title"))
    postings: list[JobPosting] = []
    for hit in _iter_comments(story_id, max_comments, http):
        if not _is_top_level(hit, story_id):
            continue
        posting = _to_posting(hit, thread)
        if posting is not None:
            postings.append(posting)
    log.info("hn: %d job comments parsed", len(postings))
    return postings


def hydrate(posting: JobPosting, http: Http) -> JobPosting:  # noqa: ARG001 - contract
    return posting


def _find_thread(prefix: str, http: Http) -> dict[str, Any] | None:
    params = {"tags": STORY_TAGS, "hitsPerPage": STORY_HITS_PER_PAGE}
    data = http.get_json(SEARCH_URL, params=params)
    matches = [hit for hit in data.get("hits", []) if str(hit.get("title", "")).startswith(prefix)]
    if not matches:
        return None
    return max(matches, key=lambda hit: str(hit.get("created_at", "")))


def _iter_comments(story_id: str, max_comments: int, http: Http) -> Iterator[dict[str, Any]]:
    page = 0
    seen = 0
    while True:
        params = {
            "tags": COMMENT_TAGS.format(story_id=story_id),
            "hitsPerPage": COMMENT_HITS_PER_PAGE,
            "page": page,
        }
        data = http.get_json(SEARCH_URL, params=params)
        hits = data.get("hits") or []
        for hit in hits:
            yield hit
            seen += 1
            if seen >= max_comments:
                log.info("hn: stopping at max_comments=%d", max_comments)
                return
        if not hits or page >= int(data.get("nbPages", 0)) - 1:
            return
        page += 1


def _is_top_level(hit: Mapping[str, Any], story_id: str) -> bool:
    parent = hit.get("parent_id")
    story = hit.get("story_id") or story_id
    return parent is not None and str(parent) == str(story)


def _to_posting(hit: Mapping[str, Any], thread: Mapping[str, Any]) -> JobPosting | None:
    text = str(hit.get("comment_text") or "")
    parsed = _parse_first_line(_first_line(text))
    if parsed is None:
        return None
    object_id = str(hit["objectID"])
    return JobPosting(
        source=KEY,
        source_job_id=object_id,
        company=parsed.company,
        title=parsed.title,
        url=ITEM_URL.format(item_id=object_id),
        location=parsed.location,
        is_remote=parsed.is_remote,
        apply_url=_apply_url(text),
        ats_type=None,
        description_md=html_to_md(text),
        posted_at=parse_iso(hit.get("created_at")),
        raw={
            "segments": list(parsed.segments),
            "thread_id": str(thread.get("objectID")),
            "thread_title": thread.get("title"),
            "author": hit.get("author"),
        },
    )


def _first_line(comment_html: str) -> str:
    head = _FIRST_LINE_BREAK.split(comment_html, maxsplit=1)[0]
    return html_lib.unescape(_TAG.sub("", head)).strip()


def _parse_first_line(line: str) -> FirstLine | None:
    segments = tuple(seg.strip() for seg in line.split(SEGMENT_SEPARATOR) if seg.strip())
    if len(segments) < MIN_SEGMENTS:
        return None
    company, title, *rest = segments
    places = [seg for seg in rest if _is_place(seg)]
    location = LOCATION_JOINER.join(places)[:MAX_LOCATION_CHARS] or None
    return FirstLine(
        company=company[:MAX_COMPANY_CHARS],
        title=title[:MAX_TITLE_CHARS],
        location=location,
        is_remote=any("remote" in seg.lower() for seg in segments),
        segments=segments,
    )


def _is_place(segment: str) -> bool:
    if _NON_LETTERS.sub("", segment.lower()) in ARRANGEMENT_WORDS:
        return False
    return not (_URL_SEGMENT.search(segment) or _MONEY_SEGMENT.search(segment))


def _apply_url(comment_html: str) -> str | None:
    for href in _HREF.findall(comment_html):
        url = html_lib.unescape(href).strip()
        lowered = url.lower()
        if not lowered.startswith(("http://", "https://")):
            continue
        if any(hint in lowered for hint in APPLY_URL_HINTS):
            return url
    return None
