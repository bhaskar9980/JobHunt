"""Workday is the first multi-page filler, so these tests cover the page walk itself.

The fixture is a local three-page wizard shaped like Workday: one section visible at a time,
a Next button that swaps sections, and a Submit button that only exists on the last page.
Nothing here touches the network or a real employer.
"""

from __future__ import annotations

import pytest
from filler_support import CONFIG_DIR, goto_fixture, launch_browser, offline_page

from jobhunt.apply.fillers.common import ConfigFiller

FIXTURE = "wizard_three_pages.html"
PAGE_ONE = "[data-automation-id='contactInformationPage']"
PAGE_THREE = "[data-automation-id='voluntaryDisclosuresPage']"


@pytest.fixture(scope="module")
def browser():
    return launch_browser()


@pytest.fixture
def page(browser):
    handle = offline_page(browser)
    goto_fixture(handle, FIXTURE)
    yield handle
    handle.close()


@pytest.fixture
def filler() -> ConfigFiller:
    return ConfigFiller.from_config("workday", CONFIG_DIR)


def test_the_config_declares_a_page_walk(filler):
    """A single-page filler has no `pages:` block; Workday must have one."""
    assert filler.cfg.get("pages", {}).get("next_selector")


def test_the_first_page_is_the_one_extracted(filler, page):
    keys = {field.key for field in filler.extract_schema(page).fields}

    assert any("first" in key.lower() or "firstname" in key.lower() for key in keys)
    assert filler.page_marker(page) == PAGE_ONE


def test_advance_moves_to_the_next_page(filler, page):
    assert filler.advance(page) is True
    assert filler.page_marker(page) != PAGE_ONE


def test_the_walk_ends_on_the_page_that_carries_submit(filler, page):
    """advance() must answer False exactly once, on the final page, or the runner would loop."""
    steps = 0
    while filler.advance(page):
        steps += 1
        assert steps < 10, "advance never reported the last page"

    assert steps == 2, "a three-page wizard takes two advances"
    assert filler.page_marker(page) == PAGE_THREE
    assert page.locator("#submit").is_visible()


def test_a_single_page_filler_never_advances(page):
    """Greenhouse declares no pages block, so the runner fills it exactly once."""
    assert ConfigFiller.from_config("greenhouse", CONFIG_DIR).advance(page) is False


def test_submit_is_only_reachable_on_the_last_page(filler, page):
    """The submit selector must not resolve while earlier pages are showing."""
    assert filler.submit_locator(page).count() == 0 or not page.locator("#submit").is_visible()

    while filler.advance(page):
        pass

    assert page.locator("#submit").is_visible()


SHARED_SUBMIT_FIXTURE = "wizard_shared_submit.html"


@pytest.fixture
def shared_submit_page(browser):
    handle = offline_page(browser)
    goto_fixture(handle, SHARED_SUBMIT_FIXTURE)
    yield handle
    handle.close()


def test_advance_refuses_to_click_a_control_that_would_submit(filler, shared_submit_page):
    """Workday reuses one automation id for Next and Submit. Stepping must never send."""
    page = shared_submit_page

    assert filler.advance(page) is True, "the first page's Next is a real next"
    assert page.locator("#nav").inner_text().strip() == "Submit"

    assert filler.advance(page) is False, "the last page's control is Submit, not Next"
    assert page.locator("body").get_attribute("data-submitted") is None, "it submitted the form"


def test_the_page_walk_stops_on_the_shared_control_without_submitting(filler, shared_submit_page):
    page = shared_submit_page
    steps = 0
    while filler.advance(page):
        steps += 1
        assert steps < 6

    assert steps == 1
    assert page.locator("body").get_attribute("data-submitted") is None
