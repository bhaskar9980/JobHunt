"""Shared helpers for the offline filler tests: headless system Chrome over file:// fixtures."""

from __future__ import annotations

import atexit
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "forms"
CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
CHANNEL = "chrome"
PDF_STUB = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"

PROFILE: dict[str, Any] = {
    "first_name": "Test",
    "last_name": "Candidate",
    "email": "candidate@example.com",
    "phone": {"country_code": "+91", "number": "9000000000"},
    "location": {"city": "Chennai", "state": "Tamil Nadu", "country": "India"},
    "links": {
        "linkedin": "https://www.linkedin.com/in/test-candidate/",
        "github": "https://github.com/test-candidate",
        "portfolio": "https://test-candidate.example/",
    },
    "current_company": "Example Corp",
    "current_title": "Software Engineer",
}


_BROWSER: Any = None
_PLAYWRIGHT: Any = None


def _shutdown() -> None:
    global _BROWSER, _PLAYWRIGHT
    if _BROWSER is not None:
        _BROWSER.close()
        _BROWSER = None
    if _PLAYWRIGHT is not None:
        _PLAYWRIGHT.stop()
        _PLAYWRIGHT = None


def launch_browser():
    """Headless system Chrome shared by every filler test module; skips when it cannot start.

    Playwright's sync API dislikes being started twice in one thread, so the browser is a
    process-wide singleton closed at interpreter exit rather than per module.
    """
    global _BROWSER, _PLAYWRIGHT
    if _BROWSER is not None:
        return _BROWSER
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - playwright is a hard dependency
        pytest.skip(f"playwright not importable: {exc}")
    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.launch(channel=CHANNEL, headless=True)
    except PlaywrightError as exc:
        playwright.stop()
        pytest.skip(f"system Chrome could not be launched: {str(exc).splitlines()[0]}")
    _PLAYWRIGHT, _BROWSER = playwright, browser
    atexit.register(_shutdown)
    return browser


def offline_page(browser):
    """New page with every network request aborted; only file:// content loads."""
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.route("http*://**", lambda route: route.abort())
    return page


def goto_fixture(page, name: str) -> None:
    """Wait for `load` so inline stylesheets apply, as they do on the live page."""
    page.goto((FIXTURES / name).resolve().as_uri(), wait_until="load")


def write_pdf(tmp_path: Path) -> Path:
    path = tmp_path / "resume.pdf"
    path.write_bytes(PDF_STUB)
    return path


def file_count(page, selector: str) -> int:
    return int(page.locator(selector).first.evaluate("e => e.files ? e.files.length : 0"))


def by_key(schema) -> dict[str, Any]:
    return {f.key: f for f in schema.fields}
