"""CAPTCHA and login-wall detection, plus waiting for the human to clear them in the browser."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

POLL_SECONDS = 2.0
_CAPTCHA_IFRAME = re.compile(r"recaptcha|hcaptcha|turnstile|captcha", re.I)
_CAPTCHA_TEXT = re.compile(
    r"verify you are human|are you a robot|i'm not a robot|complete the captcha|security check",
    re.I,
)
_LOGIN_TEXT = re.compile(r"\b(sign in|log in|login|create an account|create account)\b", re.I)


def detect_captcha(page: Any) -> str | None:
    """Return a short description if a CAPTCHA widget or challenge text is visible."""
    try:
        for frame in page.frames:
            src = frame.url or ""
            is_widget = _CAPTCHA_IFRAME.search(src) and frame != page.main_frame
            if is_widget and _frame_visible(page, src):
                return f"captcha iframe: {src[:80]}"
        body = page.inner_text("body")
    except Exception as exc:  # noqa: BLE001 - detection must not crash the run
        log.debug("captcha detection error: %s", exc)
        return None
    match = _CAPTCHA_TEXT.search(body or "")
    return f"captcha text: {match.group(0)}" if match else None


def _frame_visible(page: Any, src: str) -> bool:
    try:
        locator = page.locator(f'iframe[src*="{_key(src)}"]')
        return locator.count() > 0 and locator.first.is_visible()
    except Exception:  # noqa: BLE001
        return True


def _key(src: str) -> str:
    for token in ("recaptcha", "hcaptcha", "turnstile", "captcha"):
        if token in src.lower():
            return token
    return src[:40]


def looks_like_login(page: Any) -> bool:
    try:
        has_password = page.locator('input[type="password"]').count() > 0
        text = page.inner_text("body")[:4000]
    except Exception:  # noqa: BLE001
        return False
    return has_password and bool(_LOGIN_TEXT.search(text or ""))


def wait_for_human(
    page: Any,
    seconds: float,
    cleared: Callable[[Any], bool],
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Poll until `cleared(page)` is true or the budget runs out. Returns True when cleared."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if cleared(page):
            return True
        sleep(POLL_SECONDS)
    return cleared(page)
