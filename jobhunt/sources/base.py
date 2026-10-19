"""Shared fetcher plumbing: retrying HTTP wrapper, HTML to markdown, the source module protocol.

Every source module exposes:
    KEY: str
    def fetch(cfg: Mapping[str, Any], http: Http) -> list[JobPosting]
    def hydrate(posting: JobPosting, http: Http) -> JobPosting   # no-op when content is inline
"""

from __future__ import annotations

import html as html_lib
import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from markdownify import markdownify

from jobhunt.models import JobPosting

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_RETRIES = 2
DEFAULT_BACKOFF_SECONDS = 1.5
DEFAULT_USER_AGENT = "jobhunt/0.1"
JSON_HEADERS = {"Accept": "application/json"}

_BLANK_LINES = re.compile(r"\n{3,}")
_ESCAPED_TAG = re.compile(r"&lt;\s*/?\s*[a-zA-Z][^&]*&gt;")


class FetchError(RuntimeError):
    """Raised when a source cannot be fetched after retries."""


class SourceModule(Protocol):
    KEY: str

    def fetch(self, cfg: Mapping[str, Any], http: Http) -> list[JobPosting]: ...

    def hydrate(self, posting: JobPosting, http: Http) -> JobPosting: ...


@dataclass(slots=True)
class Http:
    """Thin retrying wrapper over httpx.Client. Tests mock the client with respx."""

    client: httpx.Client
    retries: int = DEFAULT_RETRIES
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS

    def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        response = self._request(
            "GET", url, params=params, headers={**JSON_HEADERS, **(headers or {})}
        )
        return _decode_json(response, url)

    def post_json(
        self,
        url: str,
        body: Mapping[str, Any],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        merged = {**JSON_HEADERS, "Content-Type": "application/json", **(headers or {})}
        response = self._request("POST", url, json=body, headers=merged)
        return _decode_json(response, url)

    def get_text(self, url: str, *, params: Mapping[str, Any] | None = None) -> str:
        return self._request("GET", url, params=params).text

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                response = self.client.request(method, url, **kwargs)
                response.raise_for_status()
                return response
            except httpx.HTTPStatusError as exc:
                last_error = exc
                if exc.response.status_code < 500 and exc.response.status_code != 429:
                    break
            except httpx.HTTPError as exc:
                last_error = exc
            if attempt < self.retries:
                time.sleep(self.backoff_seconds * (attempt + 1))
        raise FetchError(f"{method} {url} failed: {last_error}") from last_error

    def close(self) -> None:
        self.client.close()


def _decode_json(response: httpx.Response, url: str) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise FetchError(f"{url} returned non-JSON body") from exc


def build_http(http_cfg: Mapping[str, Any]) -> Http:
    client = httpx.Client(
        timeout=float(http_cfg.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
        headers={"User-Agent": str(http_cfg.get("user_agent", DEFAULT_USER_AGENT))},
        follow_redirects=True,
    )
    return Http(
        client=client,
        retries=int(http_cfg.get("retries", DEFAULT_RETRIES)),
        backoff_seconds=float(http_cfg.get("backoff_seconds", DEFAULT_BACKOFF_SECONDS)),
    )


def html_to_md(raw: str | None, max_chars: int | None = None) -> str | None:
    """Convert (possibly entity-escaped) HTML to compact markdown."""
    if not raw:
        return None
    text = raw
    if "<" not in text and _ESCAPED_TAG.search(text):
        text = html_lib.unescape(text)
    markdown = markdownify(text, heading_style="ATX", strip=["img"])
    markdown = _BLANK_LINES.sub("\n\n", markdown).strip()
    if max_chars and len(markdown) > max_chars:
        markdown = markdown[:max_chars].rstrip() + "\n\n[truncated]"
    return markdown or None


def first_non_empty(*values: str | None) -> str | None:
    for value in values:
        if value and value.strip():
            return value.strip()
    return None
