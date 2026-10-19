"""Playwright browser lifecycle for the Python fillers: persistent profile, system Chrome."""

from __future__ import annotations

import logging
import os
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from jobhunt.config import Settings

log = logging.getLogger(__name__)

ENV_HEADLESS = "JOBHUNT_HEADLESS"
DEFAULT_CHANNEL = "chrome"
DEFAULT_VIEWPORT = {"width": 1280, "height": 900}
DEFAULT_PROFILE_DIR = "data/browser/py-profile"
NAVIGATION_TIMEOUT_MS = 45_000
ACTION_TIMEOUT_MS = 15_000


def headless_from(settings: Settings) -> bool:
    env = os.getenv(ENV_HEADLESS)
    if env is not None:
        return env.strip() in ("1", "true", "yes")
    return bool(settings.section("apply").get("headless", False))


@contextmanager
def open_page(settings: Settings, *, headless: bool | None = None) -> Generator[Any]:
    """Yield a Playwright page in a persistent context. Imports Playwright lazily."""
    from playwright.sync_api import sync_playwright  # noqa: PLC0415 - heavy, only for real runs

    apply_cfg: Mapping[str, Any] = settings.section("apply")
    profile_dir = settings.resolve(apply_cfg.get("py_profile_dir", DEFAULT_PROFILE_DIR))
    profile_dir.mkdir(parents=True, exist_ok=True)
    viewport = dict(apply_cfg.get("viewport") or DEFAULT_VIEWPORT)
    use_headless = headless_from(settings) if headless is None else headless
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile_dir),
            headless=use_headless,
            channel=str(apply_cfg.get("browser_channel", DEFAULT_CHANNEL)),
            viewport=viewport,
            accept_downloads=False,
        )
        context.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
        context.set_default_timeout(ACTION_TIMEOUT_MS)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            yield page
        finally:
            close_quietly(context)


def screenshot(page: Any, path: Path, *, full_page: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(path), full_page=full_page)
    return path


def close_quietly(context: Any) -> None:
    try:
        context.close()
    except Exception as exc:  # noqa: BLE001 - closing must never mask the real outcome
        log.debug("browser close raised: %s", exc)
