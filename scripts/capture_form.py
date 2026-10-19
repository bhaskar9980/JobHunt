"""Capture a public ATS application form as an offline HTML fixture.

Usage: uv run python scripts/capture_form.py <apply_url> <out_path> [--timeout SECONDS]

Loads the page in headless system Chrome, waits for the application form to render, then
writes ``page.content()`` with ``<script>``, ``<iframe>`` and ``<noscript>`` blocks removed and
inline SVG bodies emptied. Form markup (labels, inputs, selects, hidden file inputs, buttons)
is left intact. The script never types into or submits anything.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

log = logging.getLogger("capture_form")

CHANNEL = "chrome"
DEFAULT_TIMEOUT_SECONDS = 45.0
SETTLE_MS = 1_500
FORM_SELECTOR = (
    "#application_form, #application-form, form[action*='apply'], "
    "input[type=file], input[name^=_systemfield_], input[name=name]"
)
REVEAL_BUTTON = "a#apply_button, a[href$='#app'], button:has-text('Apply for this job')"
STRIP_BLOCKS = (
    re.compile(r"<script\b[^>]*>.*?</script>", re.S | re.I),
    re.compile(r"<noscript\b[^>]*>.*?</noscript>", re.S | re.I),
    re.compile(r"<iframe\b[^>]*>.*?</iframe>", re.S | re.I),
)
SVG_BLOCK = re.compile(r"(<svg\b[^>]*>).*?(</svg>)", re.S | re.I)
KILOBYTE = 1024


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("apply_url")
    parser.add_argument("out_path", type=Path)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    return parser.parse_args(argv)


def scrub(html: str) -> str:
    for pattern in STRIP_BLOCKS:
        html = pattern.sub("", html)
    return SVG_BLOCK.sub(r"\1\2", html)


def reveal_form(page, timeout_ms: float) -> None:
    """Legacy Greenhouse boards hide the form behind an "Apply for this job" anchor."""
    button = page.locator(REVEAL_BUTTON)
    try:
        if button.count() and button.first.is_visible():
            button.first.click(timeout=timeout_ms)
    except PlaywrightError as exc:
        log.debug("reveal click skipped: %s", exc)


def capture(url: str, timeout_seconds: float) -> str:
    timeout_ms = timeout_seconds * 1000
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=CHANNEL, headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            page.wait_for_selector(FORM_SELECTOR, state="attached", timeout=timeout_ms)
        except PlaywrightError:
            reveal_form(page, timeout_ms)
            page.wait_for_selector(FORM_SELECTOR, state="attached", timeout=timeout_ms)
        try:
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except PlaywrightError:
            log.info("network did not go idle; capturing current DOM")
        page.wait_for_timeout(SETTLE_MS)
        html = page.content()
        browser.close()
    return html


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(sys.argv[1:] if argv is None else argv)
    html = scrub(capture(args.apply_url, args.timeout))
    args.out_path.parent.mkdir(parents=True, exist_ok=True)
    args.out_path.write_text(html, encoding="utf-8")
    log.info("wrote %s (%.0f KB)", args.out_path, len(html.encode("utf-8")) / KILOBYTE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
