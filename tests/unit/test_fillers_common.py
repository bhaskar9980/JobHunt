"""Shared filler helpers: pure functions, then DOM introspection on small inline pages."""

from __future__ import annotations

from pathlib import Path

import pytest
from filler_support import CONFIG_DIR, PROFILE, launch_browser, offline_page, write_pdf

from jobhunt.apply.fillers import common
from jobhunt.apply.fillers.base import Answer, FileInputs, FillerError, LoginRequired
from jobhunt.apply.schema import FieldKind

TEST_CFG = {
    "open": {"form_root": ["form"], "reveal": [], "timeout_ms": 500},
    "already_applied": ["you have already applied"],
    "extract": {
        "heading_selector": "h2, h3, legend",
        "field_selector": "input, textarea, select",
        "question_container": ".q",
        "question_label": ".q-label",
        "yesno_container": "",
    },
    "combobox": {"option_selector": "[role='option']", "options_timeout_ms": 200},
    "system_fields": {"email": "email", "resume": "resume"},
    "system_label_patterns": [{"profile": "linkedin", "regex": "linkedin"}],
    "submit": {"selector": "button[type=submit]"},
    "confirm": {"url_patterns": ["/thanks"], "text_patterns": ["thank you for applying"]},
}

INLINE_FORM = """
<form>
  <h2>About you</h2>
  <label for="email">Email <span aria-hidden="true">*</span></label>
  <input id="email" name="email" type="email" aria-required="true">
  <span id="lb">LinkedIn profile</span><input id="li" aria-labelledby="lb" type="text">
  <label>Current location <span>✱</span><select name="loc">
    <option value="">Select...</option><option value="in">India</option>
    <option value="gb">United Kingdom</option></select></label>
  <input name="nick" placeholder="Nickname" type="text">
  <div class="q"><div class="q-label">Relocate?</div>
    <label><input type="radio" name="reloc" value="Yes" required>Yes</label>
    <label><input type="radio" name="reloc" value="No">No</label></div>
  <div class="q"><div class="q-label">Languages</div>
    <label><input type="checkbox" name="lang" value="py">Python</label>
    <label><input type="checkbox" name="lang" value="cs">C#</label></div>
  <label><input type="checkbox" name="consent">I agree</label>
  <h3>Files</h3>
  <div role="group" aria-labelledby="up" aria-required="true">
    <div id="up">Resume/CV *</div>
    <label for="resume">Attach</label><input id="resume" name="resume" type="file"></div>
  <textarea name="why" required></textarea>
  <input type="hidden" name="token" value="x">
  <input name="ghost" type="text" style="display:none">
  <input required tabindex="-1" aria-hidden="true">
  <button type="submit">Submit</button>
</form>
"""


# --------------------------------------------------------------------------- pure helpers


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("First Name*", "First Name"),
        ("Full name ✱", "Full name"),
        ("Notice  Period", "Notice Period"),
        ("  spaced   out  ", "spaced out"),
        (None, ""),
    ],
)
def test_clean_label(raw, expected):
    assert common.clean_label(raw) == expected


def test_has_required_mark():
    assert common.has_required_mark("Full name✱")
    assert common.has_required_mark("Email *")
    assert not common.has_required_mark("Email")
    assert not common.has_required_mark("")


def test_slug_key():
    assert common.slug_key("What is your location?") == "what-is-your-location"
    assert len(common.slug_key("x" * 200)) <= 60


@pytest.mark.parametrize(
    ("tag", "input_type", "role", "multiple", "expected"),
    [
        ("input", "text", "", False, FieldKind.TEXT),
        ("input", "email", "", False, FieldKind.EMAIL),
        ("input", "tel", "", False, FieldKind.PHONE),
        ("input", "file", "", False, FieldKind.FILE),
        ("input", "checkbox", "", False, FieldKind.CHECKBOX),
        ("input", "radio", "", False, FieldKind.RADIO),
        ("input", "text", "combobox", False, FieldKind.SELECT),
        ("select", "", "", False, FieldKind.SELECT),
        ("select", "", "", True, FieldKind.MULTISELECT),
        ("textarea", "", "", False, FieldKind.TEXTAREA),
        ("div", "", "", False, FieldKind.UNKNOWN),
    ],
)
def test_kind_for(tag, input_type, role, multiple, expected):
    assert common.kind_for(tag, input_type, role, multiple=multiple) == expected


def test_to_bool_and_choice_text():
    assert common.to_bool(True) and common.to_bool("Yes") and common.to_bool("1")
    assert not common.to_bool(False) and not common.to_bool("no")
    assert common.choice_text(True) == "Yes"
    assert common.choice_text(False) == "No"
    assert common.choice_text(" 0-4 ") == "0-4"


def test_best_option_exact_then_fuzzy():
    options = ["Select...", "Yes", "No", "N/A (I am based in London)"]
    assert common.best_option("no", options) == 2
    assert common.best_option("Not applicable, based in London", options) == 3
    assert common.best_option("Purple", options) is None


def test_profile_value():
    assert common.profile_value("full_name", PROFILE) == "Test Candidate"
    assert common.profile_value("phone", PROFILE) == "+91 9000000000"
    assert common.profile_value("phone_national", PROFILE) == "9000000000"
    assert common.profile_value("phone_country", PROFILE) == "India"
    assert common.profile_value("location", PROFILE) == "Chennai, Tamil Nadu, India"
    assert common.profile_value("linkedin", PROFILE) == PROFILE["links"]["linkedin"]
    assert common.profile_value("current_company", PROFILE) == "Example Corp"
    assert common.profile_value("other_website", PROFILE) is None
    assert common.profile_value("email", {}) is None
    assert common.profile_value("location", {"location": "Remote"}) == "Remote"


def test_system_profile_key_by_name_then_label():
    assert common.system_profile_key("email", "Email", TEST_CFG) == "email"
    assert common.system_profile_key("q_1", "LinkedIn Profile", TEST_CFG) == "linkedin"
    assert common.system_profile_key("q_2", "Why us?", TEST_CFG) is None


def test_cfg_get_nested_defaults():
    assert common.cfg_get(TEST_CFG, "open", "timeout_ms") == 500
    assert common.cfg_get(TEST_CFG, "open", "missing", default=3) == 3
    assert common.cfg_get(TEST_CFG, "nope", "deeper", default=[]) == []


@pytest.mark.parametrize(
    ("key", "url"),
    [
        ("greenhouse", "https://job-boards.greenhouse.io/postman/jobs/7452539003"),
        ("greenhouse", "https://boards.greenhouse.io/stripe/jobs/123?gh_jid=123"),
        ("lever", "https://jobs.lever.co/spotify/abc-123/apply"),
        ("ashby", "https://jobs.ashbyhq.com/linear/uuid/application"),
    ],
)
def test_url_matches_registry(key, url):
    assert common.url_matches(key, url, CONFIG_DIR)
    for other in {"greenhouse", "lever", "ashby"} - {key}:
        assert not common.url_matches(other, url, CONFIG_DIR)


def test_load_ats_config_for_each_supported_ats():
    for key in ("greenhouse", "lever", "ashby"):
        cfg = common.load_ats_config(key, CONFIG_DIR)
        assert cfg["open"]["form_root"]
        assert cfg["submit"]["selector"]
        assert cfg["confirm"]["text_patterns"]


def test_confirm_page_returns_none_after_timeout_without_sleeping_for_real():
    class FakePage:
        url = "https://example.com/apply"

        def inner_text(self, _selector: str) -> str:
            return "still filling"

    slept: list[float] = []
    result = common.confirm_page(FakePage(), ["/thanks"], ["thank you"], 0.0, sleep=slept.append)
    assert result is None
    assert slept == []


def test_confirm_page_matches_url_or_text():
    class FakePage:
        def __init__(self, url: str, text: str) -> None:
            self.url, self.text = url, text

        def inner_text(self, _selector: str) -> str:
            return self.text

    by_url = common.confirm_page(FakePage("https://x/apply/thanks", ""), ["/thanks"], [], 0.0)
    assert by_url is not None and by_url.url.endswith("/thanks")
    by_text = common.confirm_page(
        FakePage("https://x/apply", "Done. Thank You For Applying!"),
        [],
        ["thank you for applying"],
        0.0,
    )
    assert by_text is not None and by_text.text == "Thank You For Applying"


# --------------------------------------------------------------------------- browser-backed


@pytest.fixture(scope="module")
def browser():
    return launch_browser()


@pytest.fixture
def page(browser):
    page = offline_page(browser)
    yield page
    page.close()


@pytest.fixture
def filler() -> common.ConfigFiller:
    return common.ConfigFiller(KEY="test", cfg=TEST_CFG, _confirm_sleep=lambda _s: None)


def test_extract_schema_from_inline_form(page, filler):
    page.set_content(INLINE_FORM)
    schema = filler.extract_schema(page)
    fields = {f.key: f for f in schema.fields}

    assert set(fields) == {
        "email",
        "li",
        "loc",
        "nick",
        "reloc",
        "lang",
        "consent",
        "resume",
        "why",
    }
    assert fields["email"].kind == FieldKind.EMAIL and fields["email"].required
    assert fields["email"].label == "Email" and fields["email"].system
    assert fields["email"].section == "About you"
    assert fields["li"].label == "LinkedIn profile" and fields["li"].system
    assert fields["loc"].kind == FieldKind.SELECT
    assert fields["loc"].label == "Current location" and fields["loc"].required
    assert fields["loc"].options == ["India", "United Kingdom"]
    assert fields["nick"].label == "Nickname" and not fields["nick"].required
    assert fields["reloc"].kind == FieldKind.RADIO and fields["reloc"].required
    assert fields["reloc"].label == "Relocate?" and fields["reloc"].options == ["Yes", "No"]
    assert fields["lang"].kind == FieldKind.MULTISELECT and fields["lang"].options == [
        "Python",
        "C#",
    ]
    assert fields["consent"].kind == FieldKind.CHECKBOX and fields["consent"].label == "I agree"
    assert fields["resume"].kind == FieldKind.FILE and fields["resume"].system
    assert fields["resume"].label == "Resume/CV" and fields["resume"].required
    assert fields["resume"].section == "Files"
    assert fields["why"].kind == FieldKind.TEXTAREA and fields["why"].required


def test_extract_is_stable_across_runs(page, filler):
    page.set_content(INLINE_FORM)
    first = filler.extract_schema(page)
    second = filler.extract_schema(page)
    assert [f.key for f in first.fields] == [f.key for f in second.fields]
    assert first.diff_new(second) == []


def test_fill_inline_form(page, filler, tmp_path: Path):
    page.set_content(INLINE_FORM)
    schema = filler.extract_schema(page)
    answers = {
        "loc": Answer("loc", "Current location", "India", "rule", option="India"),
        "reloc": Answer("reloc", "Relocate?", True, "rule", option="Yes"),
        "lang": Answer("lang", "Languages", ["C#", "Python"], "rule", option="C#"),
        "consent": Answer("consent", "I agree", True, "rule"),
        "why": Answer("why", "Why", "Because.", "user"),
        "nick": Answer("nick", "Nickname", None, "system", skipped=True, reason="optional"),
    }
    report = filler.fill(page, schema, answers, PROFILE, FileInputs(resume_pdf=write_pdf(tmp_path)))

    assert report.errors == []
    assert set(report.filled) == {"email", "li", "loc", "reloc", "lang", "consent", "resume", "why"}
    assert any(s.startswith("nick:") for s in report.skipped)
    assert page.locator("#email").input_value() == "candidate@example.com"
    assert page.locator("#li").input_value() == PROFILE["links"]["linkedin"]
    assert page.locator("select[name=loc]").input_value() == "in"
    assert page.locator("input[name=reloc][value=Yes]").is_checked()
    assert not page.locator("input[name=reloc][value=No]").is_checked()
    assert page.locator("input[name=lang][value=cs]").is_checked()
    assert page.locator("input[name=consent]").is_checked()
    assert page.locator("textarea[name=why]").input_value() == "Because."
    assert page.locator("#resume").evaluate("e => e.files.length") == 1


def test_fill_required_failure_is_an_error_but_optional_failure_is_skipped(page, filler, tmp_path):
    page.set_content(INLINE_FORM)
    schema = filler.extract_schema(page)
    answers = {
        "reloc": Answer("reloc", "Relocate?", "Maybe", "rule", option="Maybe"),
        "lang": Answer("lang", "Languages", "Rust", "rule", option="Rust"),
    }
    report = filler.fill(page, schema, answers, PROFILE, FileInputs(resume_pdf=write_pdf(tmp_path)))
    assert any(e.startswith("reloc:") for e in report.errors)
    assert any(s.startswith("lang: fill failed") for s in report.skipped)


def test_missing_resume_file_is_reported(page, filler, tmp_path):
    page.set_content(INLINE_FORM)
    schema = filler.extract_schema(page)
    report = filler.fill(page, schema, {}, PROFILE, FileInputs(resume_pdf=tmp_path / "nope.pdf"))
    assert any(e.startswith("resume: file not found") for e in report.errors)


def test_open_raises_when_no_form_and_login_when_password_present(page, filler, tmp_path):
    plain = tmp_path / "plain.html"
    plain.write_text("<h1>Nothing here</h1>", encoding="utf-8")
    with pytest.raises(FillerError):
        filler.open(page, plain.resolve().as_uri())
    login = tmp_path / "login.html"
    login.write_text("<h1>Sign in to continue</h1><input type=password name=pw>", encoding="utf-8")
    with pytest.raises(LoginRequired):
        filler.open(page, login.resolve().as_uri())


def test_open_returns_when_form_present_and_already_applied_reads_body(page, filler, tmp_path):
    ok = tmp_path / "ok.html"
    ok.write_text(INLINE_FORM, encoding="utf-8")
    filler.open(page, ok.resolve().as_uri())
    assert not filler.already_applied(page)
    page.set_content("<p>You have already applied to this job.</p>")
    assert filler.already_applied(page)


def test_submit_requires_an_enabled_button(page, filler):
    page.set_content("<form><button type=submit disabled>Go</button></form>")
    with pytest.raises(FillerError):
        filler.submit(page)
    page.set_content("<form></form>")
    with pytest.raises(FillerError):
        filler.submit(page)
    page.set_content("<form onsubmit='return false'><button type=submit>Go</button></form>")
    filler.submit(page)


def test_confirm_on_inline_pages(page, filler):
    page.set_content("<h1>Thank you for applying</h1>")
    found = filler.confirm(page, 0.0)
    assert found is not None and found.text.lower() == "thank you for applying"
    page.set_content("<h1>Still here</h1>")
    assert filler.confirm(page, 0.0) is None
