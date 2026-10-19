"""Greenhouse filler against the captured job-boards.greenhouse.io form (offline, file://)."""

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

FIXTURE = "greenhouse_postman.html"
Q_OFFICE = "question_27918901003"
Q_LINKEDIN = "question_27918898003"
Q_WORK_AUTH = "question_27918899003"
Q_SPONSORSHIP = "question_27918900003"
EEO_FIELDS = ("gender", "hispanic_ethnicity", "veteran_status", "disability_status")


@pytest.fixture(scope="module")
def browser():
    return launch_browser()


@pytest.fixture(scope="module")
def filler() -> ConfigFiller:
    return ConfigFiller.from_config("greenhouse", CONFIG_DIR)


@pytest.fixture
def page(browser):
    page = offline_page(browser)
    goto_fixture(page, FIXTURE)
    yield page
    page.close()


def test_matches_only_greenhouse_urls(filler):
    assert filler.matches("https://job-boards.greenhouse.io/postman/jobs/7452539003")
    assert filler.matches("https://boards.greenhouse.io/stripe/jobs/1?gh_jid=1")
    assert not filler.matches("https://jobs.lever.co/spotify/abc")
    assert not filler.matches("https://jobs.ashbyhq.com/linear/abc")


def test_not_already_applied_and_submit_present_but_not_clicked(filler, page):
    assert not filler.already_applied(page)
    submit = filler.submit_locator(page)
    assert submit.count() == 1
    assert submit.first.is_enabled()
    assert submit.first.inner_text().strip() == "Submit application"


def test_extract_schema_system_and_custom_fields(filler, page):
    fields = by_key(filler.extract_schema(page))

    for key in ("first_name", "last_name", "email", "phone", "resume"):
        assert fields[key].system and fields[key].required, key
    assert (
        fields["first_name"].kind == FieldKind.TEXT and fields["first_name"].label == "First Name"
    )
    assert fields["phone"].kind == FieldKind.PHONE
    assert fields["country"].kind == FieldKind.SELECT and fields["country"].system
    assert fields["resume"].kind == FieldKind.FILE and fields["resume"].label == "Resume/CV"
    assert fields["cover_letter"].kind == FieldKind.FILE
    assert fields["cover_letter"].label == "Cover Letter" and not fields["cover_letter"].required
    assert not fields["cover_letter"].system

    office = fields[Q_OFFICE]
    assert office.kind == FieldKind.SELECT and office.required and not office.system
    assert office.label.startswith("Postman we value in person collaboration")
    assert office.options == []  # react-select renders options only with JavaScript
    assert fields[Q_LINKEDIN].system and fields[Q_LINKEDIN].label == "LinkedIn Profile"
    assert fields[Q_WORK_AUTH].kind == FieldKind.SELECT and fields[Q_WORK_AUTH].required
    assert fields[Q_SPONSORSHIP].label.startswith("If hired, do you now or in the future")
    for key in EEO_FIELDS:
        assert fields[key].kind == FieldKind.SELECT and not fields[key].required, key
        assert (
            fields[key].section is not None
            and "Voluntary Self-Identification" in fields[key].section
        )
    assert fields["first_name"].section is None


def test_no_hidden_or_proxy_inputs_leak_into_the_schema(filler, page):
    fields = by_key(filler.extract_schema(page))
    labels = {f.label.lower() for f in fields.values()}
    assert "attach" not in labels and "enter manually" not in labels
    assert all(f.key for f in fields.values())
    assert "resume_text" not in fields and "cover_letter_text" not in fields


def test_schema_keys_are_stable(filler, page):
    first = filler.extract_schema(page)
    second = filler.extract_schema(page)
    assert [f.key for f in first.fields] == [f.key for f in second.fields]
    assert first.diff_new(second) == []


def test_fill_writes_profile_and_answers_without_submitting(filler, page, tmp_path):
    schema = filler.extract_schema(page)
    answers = {
        Q_OFFICE: Answer(Q_OFFICE, "office", "Yes", "rule"),
        Q_WORK_AUTH: Answer(Q_WORK_AUTH, "work auth", "No", "rule"),
        Q_SPONSORSHIP: Answer(Q_SPONSORSHIP, "sponsorship", "Yes", "rule"),
        "cover_letter": Answer("cover_letter", "Cover Letter", "cover_letter", "cover_letter"),
        "gender": Answer("gender", "Gender", None, "eeo_decline", skipped=True, reason="no option"),
    }
    url_before = page.url
    report = filler.fill(page, schema, answers, PROFILE, FileInputs(resume_pdf=write_pdf(tmp_path)))

    assert report.errors == []
    assert {"first_name", "last_name", "email", "country", "phone", "resume", Q_LINKEDIN} <= set(
        report.filled
    )
    assert {Q_OFFICE, Q_WORK_AUTH, Q_SPONSORSHIP} <= set(report.filled)
    assert any(s.startswith("cover_letter: no cover letter file") for s in report.skipped)
    assert any(s.startswith("gender:") for s in report.skipped)
    assert page.locator("#first_name").input_value() == "Test"
    assert page.locator("#last_name").input_value() == "Candidate"
    assert page.locator("#email").input_value() == "candidate@example.com"
    assert page.locator("#phone").input_value() == "9000000000"
    assert page.locator("#country").input_value() == "India"
    assert page.locator(f"#{Q_LINKEDIN}").input_value() == PROFILE["links"]["linkedin"]
    assert page.locator(f"#{Q_OFFICE}").input_value() == "Yes"
    assert page.locator(f"#{Q_WORK_AUTH}").input_value() == "No"
    assert file_count(page, "#resume") == 1
    assert file_count(page, "#cover_letter") == 0
    assert page.url == url_before


def test_confirm_detects_greenhouse_thank_you_page(filler, page):
    page.set_content("<main><h1>Thank you for applying to Postman.</h1></main>")
    found = filler.confirm(page, 0.0)
    assert found is not None
    assert found.text.lower() == "thank you for applying"
    page.set_content("<main><h1>Please review your application</h1></main>")
    assert filler.confirm(page, 0.0) is None


def test_open_follows_an_embedded_form_on_a_company_careers_page(filler, browser):
    """Employers often host the posting themselves and iframe in the board's form."""
    page = offline_page(browser)
    try:
        goto_fixture(page, "greenhouse_embedded.html")
        assert page.locator("form#application-form, form#application_form").count() == 0

        filler.open(page, page.url)

        assert page.locator(filler.root_selector()).count() > 0
        assert "greenhouse_postman" in page.url, "it should have navigated into the frame source"
    finally:
        page.close()


def test_open_finds_a_greenhouse_form_the_employer_renders_itself(filler, browser):
    """Kaseya serves the Greenhouse form from its own site: no iframe, no #application_form."""
    page = offline_page(browser)
    try:
        goto_fixture(page, "greenhouse_selfhosted.html")
        assert page.locator("form#application-form, form#application_form").count() == 0
        assert page.locator("#grnhse_iframe").count() == 0

        filler.open(page, page.url)

        root = page.locator(filler.root_selector())
        assert root.count() == 1, "the newsletter form must not be mistaken for the application"
        assert root.locator("input[name='resume']").count() == 1
    finally:
        page.close()


def test_extract_reads_the_employer_rendered_form(filler, browser):
    """The labels sit in the employer's own markup, not Greenhouse's .field-wrapper."""
    page = offline_page(browser)
    try:
        goto_fixture(page, "greenhouse_selfhosted.html")
        filler.open(page, page.url)
        schema = filler.extract_schema(page)

        fields = by_key(schema)
        assert "csrf_token" not in fields, "a hidden proxy input is not a question"
        assert {"first_name", "email", "resume"} <= set(fields)

        notice = fields["question_19467882004"]
        assert notice.kind == FieldKind.SELECT
        assert "30 days" in notice.options
        assert "most recent employer" in fields["question_19467876004"].label.lower()
    finally:
        page.close()


def test_open_still_fails_loudly_when_there_is_no_form_anywhere(filler, browser):
    """The iframe fallback must not turn a missing form into a silent success."""
    from jobhunt.apply.fillers.base import FillerError

    page = offline_page(browser)
    try:
        page.set_content("<html><body><h1>Careers</h1><p>No openings.</p></body></html>")
        with pytest.raises(FillerError):
            filler.open(page, page.url)
    finally:
        page.close()
