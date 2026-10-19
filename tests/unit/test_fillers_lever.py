"""Lever filler against the captured jobs.lever.co legacy form (offline, file://)."""

from __future__ import annotations

import pytest
from filler_support import (
    CONFIG_DIR,
    PROFILE,
    by_key,
    file_count,
    goto_fixture,
    launch_browser,
    offline_page,
    write_pdf,
)

from jobhunt.apply.fillers.base import Answer, FileInputs
from jobhunt.apply.fillers.common import ConfigFiller
from jobhunt.apply.schema import FieldKind

FIXTURE = "lever_spotify.html"
CARD_EMPLOYED = "cards[92a51f92-d683-4f7b-be57-e25376f60abe][field0]"
CARD_RELOCATE = "cards[d090becd-07b8-4536-ac84-dbf3bc07e03d][field0]"
CARD_YEARS = "cards[5da55f6b-ce2a-4247-b741-f1e0b0de33c2][field0]"
LONDON_ID = "0e101fcd-f389-4971-8616-b917fbc34901"
HIDDEN_NAMES = ("accountId", "h-captcha-response", "resumeStorageId", "timezone", "source")


@pytest.fixture(scope="module")
def browser():
    return launch_browser()


@pytest.fixture(scope="module")
def filler() -> ConfigFiller:
    return ConfigFiller.from_config("lever", CONFIG_DIR)


@pytest.fixture
def page(browser):
    page = offline_page(browser)
    goto_fixture(page, FIXTURE)
    yield page
    page.close()


def test_matches_only_lever_urls(filler):
    assert filler.matches("https://jobs.lever.co/spotify/2c0c8d3e/apply")
    assert filler.matches("https://jobs.eu.lever.co/acme/123")
    assert not filler.matches("https://job-boards.greenhouse.io/postman/jobs/1")
    assert not filler.matches("https://jobs.ashbyhq.com/linear/abc")


def test_not_already_applied_and_submit_present_but_not_clicked(filler, page):
    assert not filler.already_applied(page)
    submit = filler.submit_locator(page)
    assert submit.count() == 1
    assert submit.first.is_enabled()
    assert submit.first.inner_text().strip().lower() == "submit application"


def test_extract_schema_system_fields(filler, page):
    fields = by_key(filler.extract_schema(page))
    for key, label in (
        ("name", "Full name"),
        ("email", "Email"),
        ("phone", "Phone"),
        ("location", "Current location"),
        ("org", "Current company"),
    ):
        assert fields[key].system and fields[key].required, key
        assert fields[key].label == label
    assert fields["email"].kind == FieldKind.EMAIL
    assert fields["resume"].kind == FieldKind.FILE and fields["resume"].system
    assert fields["resume"].label == "Resume/CV" and fields["resume"].required
    for key in ("urls[LinkedIn]", "urls[GitHub]", "urls[Portfolio]", "urls[Other]"):
        assert fields[key].system and not fields[key].required, key
    assert fields["urls[LinkedIn]"].label == "LinkedIn URL"
    assert fields["urls[LinkedIn]"].section == "Links"


def test_extract_schema_custom_questions(filler, page):
    fields = by_key(filler.extract_schema(page))

    employed = fields[CARD_EMPLOYED]
    assert employed.kind == FieldKind.MULTISELECT and employed.required and not employed.system
    assert employed.label == "Have you ever been previously employed by Spotify?"
    assert employed.options == ["No", "Yes - Intern", "Yes - Full Time Employment"]
    assert employed.section == "Previous Spotify Employment"

    relocate = fields[CARD_RELOCATE]
    assert relocate.kind == FieldKind.RADIO and relocate.required
    assert relocate.label.startswith("This role requires you to be based in either London")
    assert relocate.options[:2] == ["Yes", "No"] and len(relocate.options) == 3

    years = fields[CARD_YEARS]
    assert years.kind == FieldKind.RADIO and not years.required
    assert years.options == ["0-4", "4-6", "6-9", "9+"]

    office = fields["opportunityLocationId"]
    assert office.kind == FieldKind.SELECT and not office.required
    assert office.options == ["London", "Stockholm"]

    pronouns = fields["pronouns"]
    assert pronouns.kind == FieldKind.MULTISELECT and not pronouns.required
    assert "He/him" in pronouns.options and "Use name only" in pronouns.options

    consent = fields["consent[marketing]"]
    assert consent.kind == FieldKind.CHECKBOX and not consent.required
    assert "consent" in consent.label.lower()

    survey_location = fields["what-is-your-location"]
    assert survey_location.kind == FieldKind.SELECT
    assert survey_location.label == "What is your location?"
    assert "India" in survey_location.options and "Select..." not in survey_location.options


def test_hidden_inputs_are_excluded(filler, page):
    fields = by_key(filler.extract_schema(page))
    for name in HIDDEN_NAMES:
        assert name not in fields, name
    assert "customPronounsTextField" not in fields  # display:none until "Custom" is ticked


def test_schema_keys_are_stable(filler, page):
    first = filler.extract_schema(page)
    second = filler.extract_schema(page)
    assert [f.key for f in first.fields] == [f.key for f in second.fields]


def test_fill_writes_profile_and_answers_without_submitting(filler, page, tmp_path):
    schema = filler.extract_schema(page)
    answers = {
        CARD_EMPLOYED: Answer(CARD_EMPLOYED, "employed", "No", "rule", option="No"),
        CARD_RELOCATE: Answer(CARD_RELOCATE, "relocate", "Yes", "rule", option="Yes"),
        CARD_YEARS: Answer(CARD_YEARS, "years", "0-4", "rule", option="0-4"),
        "opportunityLocationId": Answer(
            "opportunityLocationId", "office", "London", "rule", option="London"
        ),
        "consent[marketing]": Answer("consent[marketing]", "consent", False, "rule"),
    }
    url_before = page.url
    report = filler.fill(page, schema, answers, PROFILE, FileInputs(resume_pdf=write_pdf(tmp_path)))

    assert report.errors == []
    expected = {"name", "email", "phone", "location", "org", "resume", "urls[LinkedIn]", *answers}
    assert expected <= set(report.filled)
    assert any(s.startswith("urls[Other]: no profile value") for s in report.skipped)
    assert any(s.startswith("pronouns: no answer") for s in report.skipped)

    assert page.locator("input[name=name]").input_value() == "Test Candidate"
    assert page.locator("input[name=email]").input_value() == "candidate@example.com"
    assert page.locator("input[name=phone]").input_value() == "+91 9000000000"
    assert page.locator("input[name=org]").input_value() == "Example Corp"
    assert page.locator("#location-input").input_value() == "Chennai, Tamil Nadu, India"
    assert (
        page.locator('input[name="urls[LinkedIn]"]').input_value() == PROFILE["links"]["linkedin"]
    )
    assert page.locator('input[name="urls[GitHub]"]').input_value() == PROFILE["links"]["github"]
    assert page.locator('input[name="urls[Other]"]').input_value() == ""
    assert file_count(page, "input[name=resume]") == 1
    assert page.locator(f'input[name="{CARD_EMPLOYED}"][value="No"]').is_checked()
    assert not page.locator(f'input[name="{CARD_EMPLOYED}"][value="Yes - Intern"]').is_checked()
    assert page.locator(f'input[name="{CARD_RELOCATE}"][value="Yes"]').is_checked()
    assert page.locator(f'input[name="{CARD_YEARS}"][value="0-4"]').is_checked()
    assert page.locator("select[name=opportunityLocationId]").input_value() == LONDON_ID
    assert not page.locator('input[type=checkbox][name="consent[marketing]"]').is_checked()
    assert page.url == url_before


def test_confirm_detects_lever_thanks_page(filler, page):
    page.set_content("<h2>Application submitted</h2><p>Thanks!</p>")
    found = filler.confirm(page, 0.0)
    assert found is not None and found.text.lower() == "application submitted"
    page.set_content("<h2>Review</h2>")
    assert filler.confirm(page, 0.0) is None
