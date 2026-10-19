"""Ashby filler against the captured jobs.ashbyhq.com form (offline, file://)."""

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

FIXTURE = "ashby_linear.html"
Q_COVER = "1ed7df4b-e6d4-484e-92c3-4a5f5e07fd0c"
Q_GITHUB = "1c843a13-4fe3-43df-afcf-37a69d88591b"
Q_LINKEDIN = "ca5c9d78-ec13-4bf2-86b8-bce6ff32e45e"
Q_X = "2165840e-d858-4ff0-ad26-bd48832fe3a7"
Q_EU_TIMEZONE = "9fd0020d-14f0-47f3-b076-f31e5eaa8295"
Q_COUNTRY = "b617d074-3a16-46ec-bb02-550e30cb0e50"
Q_NOTICE = "ce587bdf-31fa-4768-9e92-7741b7ad2317"
Q_WORK_ON = "7d3da580-9e6d-494f-a63b-0a889e0a157a"
Q_PM_HAT = "e8a6b6ee-0550-4c69-85ab-05bd9b26c702"
Q_GOOD_AT = "c4b19ee4-d084-47fa-89df-ca00e63a7721"
Q_REFERRED = "018585ea-12fd-472a-bdd6-9ef643dfabdd"
COVER_TEXT = "Dear Linear team, I build backend systems in .NET and Python."


@pytest.fixture(scope="module")
def browser():
    return launch_browser()


@pytest.fixture(scope="module")
def filler() -> ConfigFiller:
    return ConfigFiller.from_config("ashby", CONFIG_DIR)


@pytest.fixture
def page(browser):
    page = offline_page(browser)
    goto_fixture(page, FIXTURE)
    yield page
    page.close()


def test_matches_only_ashby_urls(filler):
    assert filler.matches("https://jobs.ashbyhq.com/linear/6588c837/application")
    assert not filler.matches("https://job-boards.greenhouse.io/postman/jobs/1")
    assert not filler.matches("https://jobs.lever.co/spotify/abc")


def test_not_already_applied_and_submit_present_but_not_clicked(filler, page):
    assert not filler.already_applied(page)
    submit = filler.submit_locator(page)
    assert submit.count() == 1
    assert submit.first.is_enabled()
    assert submit.first.inner_text().strip() == "Submit Application"


def test_extract_schema(filler, page):
    fields = by_key(filler.extract_schema(page))

    assert fields["_systemfield_name"].system and fields["_systemfield_name"].required
    assert fields["_systemfield_name"].label == "Name"
    assert fields["_systemfield_name"].section == "Contact Details"
    assert (
        fields["_systemfield_email"].kind == FieldKind.EMAIL and fields["_systemfield_email"].system
    )
    assert fields["_systemfield_resume"].kind == FieldKind.FILE
    assert fields["_systemfield_resume"].system and fields["_systemfield_resume"].required

    assert fields[Q_COVER].kind == FieldKind.TEXTAREA and fields[Q_COVER].required
    assert fields[Q_COVER].label == "Cover letter" and not fields[Q_COVER].system
    assert fields[Q_GITHUB].system and fields[Q_GITHUB].label == "Github"
    assert fields[Q_LINKEDIN].system and fields[Q_LINKEDIN].label == "LinkedIn"
    assert not fields[Q_LINKEDIN].required
    assert not fields[Q_X].system and fields[Q_X].label == "X" and not fields[Q_X].required

    eu = fields[Q_EU_TIMEZONE]
    assert eu.kind == FieldKind.RADIO and eu.required and eu.options == ["Yes", "No"]
    assert eu.label == "Are you based in a EU or equivalent timezone?"
    assert eu.section == "Location"
    assert eu.selector is not None and eu.selector.startswith("[data-field-path=")

    assert (
        fields[Q_COUNTRY].label == "What country are you based in?" and fields[Q_COUNTRY].required
    )
    assert fields[Q_NOTICE].label == "Notice Period"
    for key in (Q_WORK_ON, Q_PM_HAT, Q_GOOD_AT):
        assert fields[key].kind == FieldKind.TEXTAREA and fields[key].required, key
        assert fields[key].section == "Tell us more"
    assert fields[Q_REFERRED].kind == FieldKind.TEXT and not fields[Q_REFERRED].required


def test_autofill_dropzone_and_recaptcha_are_excluded(filler, page):
    fields = by_key(filler.extract_schema(page))
    assert "g-recaptcha-response" not in fields
    assert all(f.key for f in fields.values())
    assert len([f for f in fields.values() if f.kind == FieldKind.FILE]) == 1


def test_schema_keys_are_stable(filler, page):
    first = filler.extract_schema(page)
    second = filler.extract_schema(page)
    assert [f.key for f in first.fields] == [f.key for f in second.fields]


def test_fill_writes_profile_and_answers_without_submitting(filler, page, tmp_path):
    schema = filler.extract_schema(page)
    answers = {
        Q_COVER: Answer(Q_COVER, "Cover letter", COVER_TEXT, "cover_letter"),
        Q_EU_TIMEZONE: Answer(Q_EU_TIMEZONE, "EU timezone", False, "rule", option="No"),
        Q_COUNTRY: Answer(Q_COUNTRY, "Country", "India", "rule"),
        Q_NOTICE: Answer(Q_NOTICE, "Notice Period", "60 days, negotiable", "rule"),
        Q_WORK_ON: Answer(Q_WORK_ON, "Work on", "Sync engine reliability.", "user"),
        Q_PM_HAT: Answer(Q_PM_HAT, "PM hat", "Payroll batching redesign.", "user"),
        Q_GOOD_AT: Answer(Q_GOOD_AT, "Good at", "Event-driven services.", "user"),
    }
    url_before = page.url
    report = filler.fill(page, schema, answers, PROFILE, FileInputs(resume_pdf=write_pdf(tmp_path)))

    assert report.errors == []
    expected = {
        "_systemfield_name",
        "_systemfield_email",
        "_systemfield_resume",
        Q_GITHUB,
        Q_LINKEDIN,
    }
    assert expected | set(answers) <= set(report.filled)
    assert any(s.startswith(f"{Q_X}: no answer") for s in report.skipped)
    assert any(s.startswith(f"{Q_REFERRED}: no answer") for s in report.skipped)

    assert page.locator("#_systemfield_name").input_value() == "Test Candidate"
    assert page.locator("#_systemfield_email").input_value() == "candidate@example.com"
    assert file_count(page, "#_systemfield_resume") == 1
    assert page.locator(f'[id="{Q_GITHUB}"]').input_value() == PROFILE["links"]["github"]
    assert page.locator(f'[id="{Q_LINKEDIN}"]').input_value() == PROFILE["links"]["linkedin"]
    assert page.locator(f'[id="{Q_X}"]').input_value() == ""
    assert page.locator(f'[id="{Q_COVER}"]').input_value() == COVER_TEXT
    assert page.locator(f'[id="{Q_COUNTRY}"]').input_value() == "India"
    assert page.locator(f'[id="{Q_NOTICE}"]').input_value() == "60 days, negotiable"
    assert page.locator(f'[id="{Q_WORK_ON}"]').input_value() == "Sync engine reliability."
    assert page.url == url_before


def test_yes_no_rejects_values_outside_yes_no(filler, page, tmp_path):
    schema = filler.extract_schema(page)
    answers = {Q_EU_TIMEZONE: Answer(Q_EU_TIMEZONE, "EU", "Sometimes", "rule", option="Sometimes")}
    report = filler.fill(page, schema, answers, PROFILE, FileInputs(resume_pdf=write_pdf(tmp_path)))
    assert any(e.startswith(f"{Q_EU_TIMEZONE}:") for e in report.errors)


def test_confirm_detects_ashby_success_text(filler, page):
    page.set_content("<h1>Your application has been submitted.</h1>")
    found = filler.confirm(page, 0.0)
    assert found is not None and found.text.lower() == "application has been submitted"
    page.set_content("<h1>Application</h1>")
    assert filler.confirm(page, 0.0) is None
