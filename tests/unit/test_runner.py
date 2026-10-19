"""Runner flow tests with a fake page and fake filler: no browser, no network."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml

from jobhunt.apply import runner as runner_mod
from jobhunt.apply.answers import AnswerBank
from jobhunt.apply.detect import AtsRegistry
from jobhunt.apply.fillers.base import (
    AnswerSet,
    Confirmation,
    FillerError,
    FillReport,
    LoginRequired,
    resolve_answers,
)
from jobhunt.apply.runner import (
    EXIT_CAPTCHA,
    EXIT_DUPLICATE,
    EXIT_LOGIN,
    EXIT_OK,
    EXIT_SAFETY,
    EXIT_SUBMISSION_UNKNOWN,
    EXIT_UNMATCHED,
    EXIT_UNSUPPORTED,
    EXIT_VALIDATION,
    Runner,
)
from jobhunt.apply.schema import FieldKind, FormField, FormSchema
from jobhunt.config import Settings
from jobhunt.models import Status
from jobhunt.repo import applications, events
from jobhunt.repo.jobs import apply_score, current_status, get_job, set_job_status, upsert_job

ROOT = Path(__file__).parent.parent.parent
CONFIG_DIR = ROOT / "config"


class FakePage:
    def __init__(self) -> None:
        self.frames: list = []
        self.main_frame = None
        self.body = "Application form"

    def inner_text(self, _selector: str) -> str:
        return self.body

    def locator(self, _selector: str):
        class _Loc:
            def count(self) -> int:
                return 0

        return _Loc()

    def screenshot(self, path: str, full_page: bool = True) -> None:
        Path(path).write_bytes(b"png")


class FakeFiller:
    KEY = "greenhouse"

    def __init__(self, schema: FormSchema) -> None:
        self.schema = schema
        self.opened: list[str] = []
        self.login_first = False
        self.open_error: str | None = None
        self.applied_already = False
        self.fill_errors: list[str] = []
        self.confirmation: Confirmation | None = Confirmation("https://x/confirmation", "Thank you")
        self.submitted = False
        self.extra_fields: list[FormField] = []

    def matches(self, url: str) -> bool:
        return True

    def open(self, page, url: str) -> None:
        self.opened.append(url)
        if self.open_error:
            raise FillerError(self.open_error)
        if self.login_first:
            self.login_first = False
            raise LoginRequired("sign in required")

    def already_applied(self, page) -> bool:
        return self.applied_already

    def extract_schema(self, page) -> FormSchema:
        return FormSchema(
            self.schema.ats, self.schema.url, [*self.schema.fields, *self.extra_fields]
        )

    def fill(self, page, schema, answers, profile, files) -> FillReport:
        assert files.resume_pdf.name.endswith(".pdf")
        return FillReport(filled=list(answers), errors=list(self.fill_errors))

    def advance(self, page) -> bool:
        # Single-page form: the runner fills it once and never steps forward.
        return False

    def submit(self, page) -> None:
        self.submitted = True

    def confirm(self, page, timeout_seconds: float):
        return self.confirmation


SCHEMA = FormSchema(
    ats="greenhouse",
    url="https://boards.greenhouse.io/acme/jobs/1001",
    fields=[
        FormField("first_name", "First Name", FieldKind.TEXT, True, system=True),
        FormField("resume", "Resume/CV", FieldKind.FILE, True),
        FormField("q_notice", "What is your notice period?", FieldKind.TEXT, True),
        FormField(
            "q_auth",
            "Are you legally authorized to work in India?",
            FieldKind.SELECT,
            True,
            ["Yes", "No"],
        ),
        FormField(
            "q_gender",
            "Gender",
            FieldKind.SELECT,
            False,
            ["Male", "Female", "Decline to self-identify"],
            section="Voluntary Self-Identification",
        ),
        FormField("q_linkedin", "LinkedIn Profile", FieldKind.URL, False),
    ],
)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4")
    return Settings(
        root=tmp_path,
        config_dir=CONFIG_DIR,
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        db_path=tmp_path / "data" / "jobs.db",
        raw={
            "paths": {"applications_dir": "data/applications", "resume_pdf": str(resume)},
            "apply": {
                "daily_cap": 10,
                "workday_daily_cap": 3,
                "min_score": 70,
                "dedupe_days": 90,
                "captcha_wait_seconds": 0,
                "login_wait_seconds": 0,
                "confirm_timeout_seconds": 1,
            },
        },
    )


@pytest.fixture
def bank(tmp_path: Path) -> AnswerBank:
    data = yaml.safe_load((CONFIG_DIR / "answers.example.yaml").read_text(encoding="utf-8"))
    data["profile"]["email"] = "candidate@test.invalid"
    data["profile"]["phone"]["number"] = "9876543210"
    data["compensation"]["current_ctc_lpa"] = 20
    data["compensation"]["expected_ctc_lpa"] = {"min": 28, "max": 34}
    path = tmp_path / "answers.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return AnswerBank.load(path)


@pytest.fixture
def approved_job(conn, norm, priority, make_posting):
    job_id = upsert_job(conn, make_posting(), norm, priority).job_id
    apply_score(conn, job_id, 88, {"reasons": ["fit"]})
    set_job_status(conn, job_id, Status.APPROVED, force=True)
    return job_id


def make_runner(conn, settings, bank, filler: FakeFiller, page: FakePage | None = None) -> Runner:
    page = page or FakePage()

    @contextmanager
    def factory():
        yield page

    def shot(_page, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"png")
        return path

    return Runner(
        conn=conn,
        settings=settings,
        bank=bank,
        registry=AtsRegistry.load(CONFIG_DIR),
        page_factory=factory,
        screenshot=shot,
        filler_lookup=lambda ats: filler if ats == "greenhouse" else None,
    )


class ReviewProbe:
    def __init__(self, page, closed):
        import threading

        self.run_id = "review-test"
        self.cancelled = threading.Event()
        self.page = page
        self.closed = closed
        self.states = []

    def update(self, state, message, *, filled=0, questions=None):
        self.states.append((state, filled, questions or []))

    def pause(self, page, state, message, *, poll_ms, filled=0, questions=None):
        assert page is self.page
        assert not self.closed[0], "browser must remain open during review"
        self.update(state, message, filled=filled, questions=questions)
        return state == "waiting_for_login"


def review_runner(conn, settings, bank, filler):
    page = FakePage()
    page.url = SCHEMA.url
    page.is_closed = lambda: False
    closed = [False]
    runner = make_runner(conn, settings, bank, filler, page)

    @contextmanager
    def factory():
        try:
            yield page
        finally:
            closed[0] = True

    runner.page_factory = factory
    return runner, ReviewProbe(page, closed), closed


def test_review_fill_works_without_prefill_and_holds_browser_without_submitting(
    conn, settings, bank, approved_job
):
    filler = FakeFiller(SCHEMA)
    runner, control, closed = review_runner(conn, settings, bank, filler)
    result = runner.fill_for_review(approved_job, control)
    assert result.code == EXIT_OK
    assert any(state == "ready_for_review" for state, _, _ in control.states)
    assert not filler.submitted
    assert closed[0]
    assert get_job(conn, approved_job)["prefill_state"] == "none"
    assert get_job(conn, approved_job)["submission_state"] == "none"
    assert applications.get_for_job(conn, approved_job) is None


def test_review_fills_known_answers_and_leaves_unknowns_for_the_human(
    conn, settings, bank, approved_job
):
    filler = FakeFiller(SCHEMA)
    filler.extra_fields = [FormField("unknown", "Employer-specific question", FieldKind.TEXT, True)]
    runner, control, _ = review_runner(conn, settings, bank, filler)
    assert runner.fill_for_review(approved_job, control).code == EXIT_OK
    assert any(
        state == "needs_input" and "Employer-specific question" in questions
        for state, _, questions in control.states
    )
    assert not filler.submitted


def test_review_login_can_resume_in_the_same_session(conn, settings, bank, approved_job):
    filler = FakeFiller(SCHEMA)
    filler.login_first = True
    runner, control, _ = review_runner(conn, settings, bank, filler)
    assert runner.fill_for_review(approved_job, control).code == EXIT_OK
    assert len(filler.opened) == 2
    assert control.states[0][0] == "waiting_for_login"
    assert not filler.submitted


@pytest.mark.parametrize("status", [Status.DISCOVERED, Status.DISMISSED, Status.APPLIED])
def test_review_requires_an_approved_job(conn, settings, bank, approved_job, status):
    set_job_status(conn, approved_job, status, force=True)
    filler = FakeFiller(SCHEMA)
    runner, control, _ = review_runner(conn, settings, bank, filler)
    assert runner.fill_for_review(approved_job, control).code == EXIT_SAFETY
    assert filler.opened == []


def test_review_rejects_a_redirect_before_filling(conn, settings, bank, approved_job):
    filler = FakeFiller(SCHEMA)
    runner, control, _ = review_runner(conn, settings, bank, filler)
    control.page.url = "https://untrusted.example/application"
    assert runner.fill_for_review(approved_job, control).code == EXIT_UNSUPPORTED
    assert not filler.submitted


def test_review_never_calls_next_or_submit(conn, settings, bank, approved_job):
    filler = FakeFiller(SCHEMA)

    def forbidden(*args):
        raise AssertionError("review filling cannot navigate or submit a completed form")

    filler.advance = forbidden
    filler.submit = forbidden
    runner, control, _ = review_runner(conn, settings, bank, filler)
    assert runner.fill_for_review(approved_job, control).code == EXIT_OK


def test_review_cancelled_before_start_does_not_open_a_browser(conn, settings, bank, approved_job):
    filler = FakeFiller(SCHEMA)
    runner, control, _ = review_runner(conn, settings, bank, filler)
    control.cancelled.set()
    assert runner.fill_for_review(approved_job, control).code == EXIT_OK
    assert filler.opened == []


@pytest.mark.parametrize(
    "column,value",
    [
        ("score", 10),
        ("prefilter_pass", 0),
        ("snooze_until", "2999-01-01T00:00:00+00:00"),
        ("submission_state", "unknown"),
    ],
)
def test_review_keeps_duplicate_profile_and_score_guards(
    conn, settings, bank, approved_job, column, value
):
    with conn:
        conn.execute(f"UPDATE jobs SET {column} = ? WHERE id = ?", (value, approved_job))
    filler = FakeFiller(SCHEMA)
    runner, control, _ = review_runner(conn, settings, bank, filler)
    assert runner.fill_for_review(approved_job, control).code == EXIT_SAFETY
    assert filler.opened == []


def test_review_rejects_placeholder_answers(conn, settings, approved_job):
    bank = AnswerBank.load(CONFIG_DIR / "answers.example.yaml")
    filler = FakeFiller(SCHEMA)
    runner, control, _ = review_runner(conn, settings, bank, filler)
    assert runner.fill_for_review(approved_job, control).code == EXIT_SAFETY
    assert filler.opened == []


def test_review_cancel_stops_before_another_field(conn, settings, bank, approved_job):
    filler = FakeFiller(SCHEMA)
    runner, control, _ = review_runner(conn, settings, bank, filler)
    calls = []

    def fill(page, schema, answers, profile, files):
        calls.append(schema.fields[0].key)
        control.cancelled.set()
        return FillReport(filled=calls[:])

    filler.fill = fill
    assert runner.fill_for_review(approved_job, control).code == EXIT_OK
    assert calls == ["first_name"]
    assert not filler.submitted


def test_review_real_browser_fills_and_uploads_but_leaves_submit_to_the_human(
    conn, settings, bank, approved_job
):
    from filler_support import launch_browser

    from jobhunt.apply.fillers.common import ConfigFiller

    page = launch_browser().new_page()
    body = """<form id="application_form" onsubmit="window.submissions++; return false">
      <label for="first_name">First name</label><input id="first_name" name="first_name" required>
      <label for="resume">Resume</label><input id="resume" name="resume" type="file" required>
      <label for="unknown">Employer-specific question</label><input id="unknown" required>
      <button id="submit_app" type="submit">Submit application</button></form>
      <script>window.submissions=0;</script>"""
    page.route(
        "**/*",
        lambda route: (
            route.fulfill(content_type="text/html", body=body)
            if route.request.url.startswith("https://boards.greenhouse.io/")
            else route.abort()
        ),
    )
    filler = ConfigFiller.from_config("greenhouse", CONFIG_DIR)
    runner = make_runner(conn, settings, bank, filler, page)
    closed = [False]
    control = ReviewProbe(page, closed)

    @contextmanager
    def factory():
        try:
            yield page
        finally:
            page.close()
            closed[0] = True

    def handoff(current, state, message, **kwargs):
        assert not closed[0] and not current.is_closed()
        assert state == "needs_input"
        assert current.locator("#first_name").input_value() == bank.get("profile.first_name")
        assert current.locator("#resume").evaluate("el => el.files.length") == 1
        assert current.locator("#unknown").input_value() == ""
        assert current.evaluate("window.submissions") == 0
        current.locator("#first_name").fill("Manually corrected")
        assert current.locator("#first_name").input_value() == "Manually corrected"
        return False

    runner.page_factory = factory
    control.pause = handoff
    assert runner.fill_for_review(approved_job, control).code == EXIT_OK
    assert closed[0]
    assert applications.get_for_job(conn, approved_job) is None


def test_resolve_answers_covers_bank_eeo_and_unmatched(bank):
    result = resolve_answers(SCHEMA, bank, job_id=1)
    assert result.answers["q_notice"].value == "60 days, negotiable"
    assert result.answers["q_auth"].option == "Yes"
    assert result.answers["q_gender"].source == "eeo_decline"
    assert result.answers["q_gender"].option == "Decline to self-identify"
    assert result.answers["resume"].value == "resume"
    assert result.answers["q_linkedin"].value.startswith("https://www.linkedin.com/")
    assert result.unmatched == []


COUNTRYLESS_AUTH = FormSchema(
    ats="greenhouse",
    url="https://www.example.com/careers/jobs/id/6146541004/?gh_jid=6146541004",
    fields=[
        FormField(
            "q_eligible",
            "Are you legally eligible to work in the country in which you are applying?",
            FieldKind.SELECT,
            True,
            ["--", "Yes", "No"],
        ),
        FormField(
            "q_sponsorship",
            "Do you now or will you at any time in the future require sponsorship?",
            FieldKind.SELECT,
            True,
            ["--", "Yes", "No"],
        ),
    ],
)


@pytest.fixture
def strict_country_bank(bank: AnswerBank) -> AnswerBank:
    """the user's own bank refuses to answer work authorisation for an unnamed country."""
    bank.data.setdefault("policies", {})["require_explicit_country"] = True
    return bank


def test_work_authorisation_stays_unmatched_when_no_country_is_known(strict_country_bank):
    """The guard is the point: never guess an authorisation answer for an unnamed country."""
    result = resolve_answers(COUNTRYLESS_AUTH, strict_country_bank, job_id=1)

    assert {field.key for field in result.unmatched} == {"q_eligible", "q_sponsorship"}


def test_runner_answers_work_authorisation_from_the_job_country(
    conn, settings, strict_country_bank, approved_job
):
    """The posting states the country, so the bank's own per-country row is reachable."""
    runner = make_runner(conn, settings, strict_country_bank, FakeFiller(COUNTRYLESS_AUTH))

    result = runner.resolve(approved_job, COUNTRYLESS_AUTH)

    assert result.unmatched == [], "the job is in India, so IN answers apply"
    assert result.answers["q_eligible"].option == "Yes"


def test_runner_leaves_work_authorisation_open_for_a_countryless_job(
    conn, norm, priority, make_posting, settings, strict_country_bank
):
    """A posting with no country in its location must not fall back to a default answer."""
    job_id = upsert_job(conn, make_posting(location="Remote"), norm, priority).job_id
    apply_score(conn, job_id, 88, {"reasons": ["fit"]})
    set_job_status(conn, job_id, Status.APPROVED, force=True)
    runner = make_runner(conn, settings, strict_country_bank, FakeFiller(COUNTRYLESS_AUTH))

    result = runner.resolve(job_id, COUNTRYLESS_AUTH)

    assert {field.key for field in result.unmatched} == {"q_eligible", "q_sponsorship"}


@pytest.fixture
def onboarding_bank() -> AnswerBank:
    return AnswerBank(
        {
            "policies": {"eeo": "provided", "gdpr_consent": True, "marketing_consent": False},
            "demographics": {"race": "Examplean", "disability": False},
        }
    )


def resolve_onboarding_field(bank, label, kind=FieldKind.SELECT, options=(), required=True, **kw):
    form_field = FormField("question", label, kind, required, list(options), **kw)
    return resolve_answers(FormSchema("fake", "https://form.invalid", [form_field]), bank, job_id=1)


@pytest.mark.parametrize("label", ["Race", "Race / Ethnicity", "What is your race?"])
@pytest.mark.parametrize("kind", [FieldKind.SELECT, FieldKind.RADIO, FieldKind.MULTISELECT])
def test_onboarding_provided_race_matches_only_explicit_options(onboarding_bank, label, kind):
    result = resolve_onboarding_field(
        onboarding_bank,
        label,
        kind,
        ["Examplean (Not Hispanic or Latino)", "  EXAMPLEAN  ", "Prefer not to say"],
    )
    answer = result.answers["question"]
    assert answer.option == "  EXAMPLEAN  "
    assert answer.source == "demographics.race"
    assert result.unmatched == []


@pytest.mark.parametrize("policy", [None, "decline", "unknown"])
def test_onboarding_old_banks_still_decline_eeo(onboarding_bank, policy):
    if policy is None:
        onboarding_bank.data.pop("policies")
    else:
        onboarding_bank.data["policies"]["eeo"] = policy
    result = resolve_onboarding_field(
        onboarding_bank, "Race", options=["Examplean", "Decline to self-identify"]
    )
    assert result.answers["question"].option == "Decline to self-identify"
    assert result.answers["question"].source == "eeo_decline"


@pytest.mark.parametrize(
    "label,options",
    [
        ("Race", ["Exampleana", "Examplean (Not Hispanic or Latino)"]),
        ("Are you Hispanic or Latino?", ["Yes", "No"]),
        ("Gender", ["Woman", "Man"]),
        ("Sex", ["Female", "Male"]),
        ("Veteran status", ["Veteran", "Not a veteran"]),
        ("Race and gender", ["Examplean", "Woman"]),
        ("Do you need an accommodation?", ["Yes", "No"]),
    ],
)
@pytest.mark.parametrize("required", [False, True])
@pytest.mark.parametrize("decline", [False, True])
def test_onboarding_missing_specific_facts_never_infers(
    onboarding_bank, label, options, required, decline
):
    onboarding_bank.data["question_map"] = [{"regex": ".*", "value": "demographics.race"}]
    options = [*options, "Prefer not to say"] if decline else options
    result = resolve_onboarding_field(onboarding_bank, label, options=options, required=required)
    if decline:
        assert result.answers["question"].option == "Prefer not to say"
    elif required:
        assert [f.key for f in result.unmatched_required] == ["question"]
    else:
        assert result.answers["question"].skipped


@pytest.mark.parametrize(
    "label,option,value",
    [
        ("Disability status", "No", False),
        ("Do you have a disability?", "I do not have a disability", False),
        ("Disability", "No, I don't have a disability", False),
        ("Do you currently have a disability?", "Yes, I have a disability", True),
        ("Disability status", "Yes", True),
    ],
)
def test_onboarding_disability_only_current_status(onboarding_bank, label, option, value):
    onboarding_bank.data["demographics"]["disability"] = value
    result = resolve_onboarding_field(onboarding_bank, label, options=[option, "Prefer not to say"])
    assert result.answers["question"].option == option
    assert result.answers["question"].source == "demographics.disability"


@pytest.mark.parametrize(
    "label,options,section",
    [
        (
            "Disability status",
            ["No, I do not have a disability and have never had one"],
            None,
        ),
        ("Have you ever had a disability?", ["Yes", "No"], None),
        ("Do you have or have you ever had a disability?", ["Yes", "No"], None),
        ("Disability or accommodation required?", ["Yes", "No"], None),
        ("Do you have no disability?", ["Yes", "No"], None),
        ("Disability", ["Yes", "No"], "Current or past disability"),
        ("Disability", ["Yes, I have or have had a disability", "No"], None),
        ("Disability", ["Yes, I have a history of disability", "No"], None),
        ("Disability", ["Yes, I need an accommodation", "No"], None),
    ],
)
@pytest.mark.parametrize("decline", [False, True])
def test_onboarding_disability_never_claims_history_or_accommodation(
    onboarding_bank, label, options, section, decline
):
    options = [*options, "Prefer not to say"] if decline else options
    result = resolve_onboarding_field(onboarding_bank, label, options=options, section=section)
    if decline:
        assert result.answers["question"].option == "Prefer not to say"
    else:
        assert [f.key for f in result.unmatched_required] == ["question"]


@pytest.mark.parametrize("label", ["Race", "Disability"])
@pytest.mark.parametrize("kind", [FieldKind.TEXT, FieldKind.TEXTAREA])
@pytest.mark.parametrize("required", [False, True])
def test_onboarding_demographics_text_without_options_needs_input(
    onboarding_bank, label, kind, required
):
    result = resolve_onboarding_field(onboarding_bank, label, kind, required=required)
    if required:
        assert [f.key for f in result.unmatched_required] == ["question"]
    else:
        assert result.answers["question"].skipped


@pytest.mark.parametrize("value", [None, "false", 0])
def test_onboarding_disability_requires_actual_boolean(onboarding_bank, value):
    onboarding_bank.data["demographics"]["disability"] = value
    result = resolve_onboarding_field(onboarding_bank, "Disability", options=["Yes", "No"])
    assert [f.key for f in result.unmatched_required] == ["question"]


@pytest.mark.parametrize(
    "label,key",
    [
        ("I consent to processing my personal data for this application", "gdpr_consent"),
        ("Do you consent to the processing of your personal data for recruitment?", "gdpr_consent"),
        ("I consent to receive marketing emails", "marketing_consent"),
        ("Would you like to receive promotional emails?", "marketing_consent"),
    ],
)
@pytest.mark.parametrize("kind", [FieldKind.CHECKBOX, FieldKind.SELECT, FieldKind.RADIO])
def test_onboarding_consent_only_for_explicit_scope(onboarding_bank, label, key, kind):
    options = [] if kind == FieldKind.CHECKBOX else ["Yes", "No"]
    result = resolve_onboarding_field(onboarding_bank, label, kind, options)
    answer = result.answers["question"]
    assert answer.value is onboarding_bank.get(f"policies.{key}")
    assert answer.source == f"policies.{key}"
    if kind != FieldKind.CHECKBOX:
        assert answer.option == ("Yes" if key == "gdpr_consent" else "No")


@pytest.mark.parametrize(
    "label,section",
    [
        ("I consent to processing my personal data and receiving marketing emails", None),
        ("I agree to the privacy policy and terms of service", None),
        ("I agree to all terms and conditions", None),
        ("Consent", None),
        ("I consent to receiving emails", None),
        ("I do not want to receive marketing emails", None),
        ("I certify that all information is true", None),
        ("I authorize a background check", None),
        ("I consent to processing my personal data for this application", "Privacy and marketing"),
        ("I consent to receive marketing emails", "Privacy and marketing"),
        ("I consent to processing my personal data for this application", "Legal agreements"),
    ],
)
def test_onboarding_ambiguous_consent_cannot_fall_through_to_bank(onboarding_bank, label, section):
    onboarding_bank.data["question_map"] = [{"regex": ".*", "value": "policies.gdpr_consent"}]
    result = resolve_onboarding_field(onboarding_bank, label, FieldKind.CHECKBOX, section=section)
    assert [f.key for f in result.unmatched_required] == ["question"]


@pytest.mark.parametrize("value", [None, "true", 1])
def test_onboarding_consent_requires_boolean_policy(onboarding_bank, value):
    onboarding_bank.data["policies"]["gdpr_consent"] = value
    result = resolve_onboarding_field(
        onboarding_bank,
        "I consent to processing my personal data for this application",
        FieldKind.CHECKBOX,
    )
    assert [f.key for f in result.unmatched_required] == ["question"]


@pytest.mark.parametrize("kind", [FieldKind.TEXT, FieldKind.SELECT, FieldKind.CHECKBOX])
def test_onboarding_consent_never_fuzzy_matches_bundled_options(onboarding_bank, kind):
    result = resolve_onboarding_field(
        onboarding_bank,
        "I consent to processing my personal data for this application",
        kind,
        ["Yes, and send me marketing emails", "No"],
    )
    assert [f.key for f in result.unmatched_required] == ["question"]


@pytest.mark.parametrize(
    "value",
    [
        "42-48 LPA XXX",
        "42000-48000",
        "Negotiable",
        "NaN",
        "Infinity",
        "1e999",
        "",
        "42_000",
        "42,000",
        "42000 XXX",
        float("nan"),
        float("inf"),
        float("-inf"),
        True,
        None,
    ],
)
def test_onboarding_number_rejects_ranges_and_nonfinite_values(value):
    bank = AnswerBank({"learned": {"expected salary": {"answer": value}}})
    result = resolve_onboarding_field(bank, "Expected salary", FieldKind.NUMBER)
    assert [f.key for f in result.unmatched_required] == ["question"]


@pytest.mark.parametrize("value", [0, 42000, 42.5, "42000", "42.5", "-1", "4.2e4"])
def test_onboarding_number_accepts_finite_numbers(value):
    bank = AnswerBank({"learned": {"expected salary": {"answer": value}}})
    result = resolve_onboarding_field(bank, "Expected salary", FieldKind.NUMBER)
    assert result.answers["question"].value == value


def test_onboarding_pay_range_still_allowed_in_text():
    value = "42-48 LPA XXX"
    bank = AnswerBank({"learned": {"expected salary": {"answer": value}}})
    result = resolve_onboarding_field(bank, "Expected salary", FieldKind.TEXT)
    assert result.answers["question"].value == value


def test_prefill_writes_schema_and_answers_and_marks_ready(conn, settings, bank, approved_job):
    filler = FakeFiller(SCHEMA)
    runner = make_runner(conn, settings, bank, filler)
    result = runner.prefill(approved_job)
    assert result.code == EXIT_OK
    job_dir = settings.path("applications_dir") / str(approved_job)
    assert (job_dir / "form_schema.json").exists()
    answers = AnswerSet.load(job_dir / "answers.json")
    assert "q_notice" in answers.answers
    assert get_job(conn, approved_job)["prefill_state"] == "ready"


def test_prefill_with_unmatched_required_marks_schema(conn, settings, bank, approved_job):
    schema = FormSchema(
        SCHEMA.ats,
        SCHEMA.url,
        [
            *SCHEMA.fields,
            FormField("q_why", "Describe your proudest project", FieldKind.TEXTAREA, True),
        ],
    )
    runner = make_runner(conn, settings, bank, FakeFiller(schema))
    result = runner.prefill(approved_job)
    assert result.code == EXIT_UNMATCHED
    assert get_job(conn, approved_job)["prefill_state"] == "schema"
    # learn the answer, re-resolve without a browser -> ready
    bank.learn("Describe your proudest project", "BlockPay, IEEE TrustCom 2023.", approved_job)
    again = runner.re_resolve(approved_job)
    assert again.code == EXIT_OK
    assert get_job(conn, approved_job)["prefill_state"] == "ready"


def test_prefill_login_wall_blocks(conn, settings, bank, approved_job):
    filler = FakeFiller(SCHEMA)
    filler.login_first = True
    result = make_runner(conn, settings, bank, filler).prefill(approved_job)
    assert result.code == EXIT_LOGIN
    assert get_job(conn, approved_job)["prefill_state"] == "blocked"
    assert any(
        e["event_type"] == "login_required" for e in events.events_for_job(conn, approved_job)
    )


def test_prefill_assisted_mode_writes_sheet_without_browser(
    conn, settings, bank, norm, priority, make_posting
):
    job_id = upsert_job(
        conn,
        make_posting(
            source="smartrecruiters",
            source_job_id="s1",
            ats_type="smartrecruiters",
            url="https://jobs.smartrecruiters.com/Acme/12345-software-engineer",
            apply_url=None,
        ),
        norm,
        priority,
    ).job_id
    result = make_runner(conn, settings, bank, FakeFiller(SCHEMA)).prefill(job_id)
    assert result.code == EXIT_UNSUPPORTED
    assert "assisted" in result.message
    assert get_job(conn, job_id)["prefill_state"] == "ready"


def test_apply_happy_path_records_application(conn, settings, bank, approved_job):
    filler = FakeFiller(SCHEMA)
    runner = make_runner(conn, settings, bank, filler)
    assert runner.prefill(approved_job).code == EXIT_OK
    result = runner.apply(approved_job)
    assert result.code == EXIT_OK, result.message
    assert filler.submitted
    assert current_status(conn, approved_job) == Status.APPLIED
    app = applications.get_for_job(conn, approved_job)
    assert app["submitted_via"] == "ats_form"
    assert app["confirmation_ref"] == "https://x/confirmation"
    job_dir = settings.path("applications_dir") / str(approved_job)
    assert (job_dir / "prefilled.png").exists()
    assert (job_dir / "confirmation.png").exists()
    assert json.loads((job_dir / "confirmation.json").read_text())["text"] == "Thank you"
    kinds = [e["event_type"] for e in events.events_for_job(conn, approved_job)]
    assert "submitted" in kinds and "status_change" in kinds


def test_apply_dry_run_never_submits(conn, settings, bank, approved_job):
    filler = FakeFiller(SCHEMA)
    runner = make_runner(conn, settings, bank, filler)
    runner.prefill(approved_job)
    result = runner.apply(approved_job, dry_run=True)
    assert result.code == EXIT_OK
    assert not filler.submitted
    assert current_status(conn, approved_job) == Status.APPROVED
    assert any(e["event_type"] == "dry_run" for e in events.events_for_job(conn, approved_job))


def test_apply_requires_prefill_and_safety(conn, settings, bank, approved_job):
    runner = make_runner(conn, settings, bank, FakeFiller(SCHEMA))
    result = runner.apply(approved_job)
    assert result.code == EXIT_SAFETY  # prefill_state is 'none'
    assert "prefill_state" in result.message


def test_apply_duplicate_and_new_required_fields(conn, settings, bank, approved_job):
    filler = FakeFiller(SCHEMA)
    runner = make_runner(conn, settings, bank, filler)
    runner.prefill(approved_job)
    filler.applied_already = True
    assert runner.apply(approved_job).code == EXIT_DUPLICATE
    filler.applied_already = False
    filler.extra_fields = [FormField("q_new", "Why this role?", FieldKind.TEXTAREA, True)]
    result = runner.apply(approved_job)
    assert result.code == EXIT_UNMATCHED
    assert get_job(conn, approved_job)["prefill_state"] == "schema"
    assert not filler.submitted


def test_apply_validation_error_before_submit_is_retryable(conn, settings, bank, approved_job):
    filler = FakeFiller(SCHEMA)
    runner = make_runner(conn, settings, bank, filler)
    runner.prefill(approved_job)
    filler.fill_errors = ["phone rejected"]
    assert runner.apply(approved_job).code == EXIT_VALIDATION
    assert not filler.submitted
    filler.fill_errors = []
    assert runner.apply(approved_job).code == EXIT_OK


def test_unconfirmed_submission_is_exit_16_and_blocks_retry(conn, settings, bank, approved_job):
    filler = FakeFiller(SCHEMA)
    runner = make_runner(conn, settings, bank, filler)
    runner.prefill(approved_job)
    filler.confirmation = None
    result = runner.apply(approved_job)
    assert result.code == EXIT_SUBMISSION_UNKNOWN
    assert "not confirmed" in result.message
    assert filler.submitted
    assert current_status(conn, approved_job) == Status.APPROVED
    assert get_job(conn, approved_job)["prefill_state"] == "blocked"
    assert applications.get_for_job(conn, approved_job) is None
    kinds = [e["event_type"] for e in events.events_for_job(conn, approved_job)]
    assert "submission_unknown" in kinds
    assert (settings.path("applications_dir") / str(approved_job) / "confirmation.png").exists()

    filler.submitted = False
    filler.confirmation = Confirmation("https://x/confirmation", "Thank you")
    second = runner.apply(approved_job)
    assert second.code == EXIT_SAFETY
    assert "not confirmed" in second.message
    assert not filler.submitted

    # Re-running prefill prepares a new attempt only after explicit reconciliation.
    assert runner.prefill(approved_job).code == EXIT_SAFETY
    with conn:
        applications.resolve_not_submitted(conn, approved_job, "verified no application on site")
    assert runner.prefill(approved_job).code == EXIT_OK
    third = runner.apply(approved_job)
    assert third.code == EXIT_OK
    assert filler.submitted
    assert current_status(conn, approved_job) == Status.APPLIED


def test_confirm_step_error_after_submit_is_exit_16(conn, settings, bank, approved_job):
    class ConfirmExplodes(FakeFiller):
        def confirm(self, page, timeout_seconds: float):
            raise FillerError("confirmation locator vanished")

    filler = ConfirmExplodes(SCHEMA)
    runner = make_runner(conn, settings, bank, filler)
    runner.prefill(approved_job)
    result = runner.apply(approved_job)
    assert result.code == EXIT_SUBMISSION_UNKNOWN
    assert filler.submitted
    assert applications.get_for_job(conn, approved_job) is None


def test_recording_the_application_clears_the_unconfirmed_block(conn, settings, bank, approved_job):
    filler = FakeFiller(SCHEMA)
    runner = make_runner(conn, settings, bank, filler)
    runner.prefill(approved_job)
    filler.confirmation = None
    assert runner.apply(approved_job).code == EXIT_SUBMISSION_UNKNOWN
    gate = runner_mod.SafetyGate.from_settings(conn, settings.section("apply"))
    assert gate.unconfirmed_submission(approved_job)
    with conn:
        applications.ensure_for_status(conn, approved_job, Status.APPLIED, "2026-09-09T00:00:00Z")
    assert not gate.unconfirmed_submission(approved_job)


def test_apply_captcha_blocks_before_submit(conn, settings, bank, approved_job):
    filler = FakeFiller(SCHEMA)
    page = FakePage()
    page.body = "Please verify you are human to continue"
    runner = make_runner(conn, settings, bank, filler, page)
    runner.prefill(approved_job)
    result = runner.apply(approved_job)
    assert result.code == EXIT_CAPTCHA
    assert not filler.submitted
    assert any(
        e["event_type"] == "captcha_blocked" for e in events.events_for_job(conn, approved_job)
    )


def test_apply_unsupported_ats(conn, settings, bank, norm, priority, make_posting):
    job_id = upsert_job(
        conn,
        make_posting(
            source="linkedin_email",
            source_job_id="l1",
            ats_type=None,
            url="https://www.linkedin.com/jobs/view/4012345678/",
            apply_url=None,
        ),
        norm,
        priority,
    ).job_id
    result = make_runner(conn, settings, bank, FakeFiller(SCHEMA)).apply(job_id)
    assert result.code == EXIT_UNSUPPORTED


def test_exit_codes_are_distinct():
    codes = [
        EXIT_OK,
        EXIT_CAPTCHA,
        EXIT_LOGIN,
        EXIT_DUPLICATE,
        EXIT_UNMATCHED,
        EXIT_VALIDATION,
        EXIT_SAFETY,
        EXIT_SUBMISSION_UNKNOWN,
        EXIT_UNSUPPORTED,
    ]
    assert len(set(codes)) == len(codes)
    assert runner_mod.EXIT_OK == 0


def test_apply_refuses_placeholder_answer_bank(conn, settings, tmp_path, approved_job):
    data = yaml.safe_load((CONFIG_DIR / "answers.example.yaml").read_text(encoding="utf-8"))
    path = tmp_path / "placeholders.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    placeholder_bank = AnswerBank.load(path)
    filler = FakeFiller(SCHEMA)
    runner = make_runner(conn, settings, placeholder_bank, filler)
    runner.prefill(approved_job)
    assert runner.apply(approved_job, dry_run=True).code == EXIT_OK
    result = runner.apply(approved_job)
    assert result.code == EXIT_SAFETY
    assert "placeholders" in result.message
    assert not filler.submitted
    assert any(
        e["event_type"] == "safety_blocked" for e in events.events_for_job(conn, approved_job)
    )


def test_open_filler_error_maps_to_validation(conn, settings, bank, approved_job):
    filler = FakeFiller(SCHEMA)
    filler.open_error = "no application form found"
    runner = make_runner(conn, settings, bank, filler)
    assert runner.prefill(approved_job).code == EXIT_VALIDATION
    kinds = [e["event_type"] for e in events.events_for_job(conn, approved_job)]
    assert "validation_error" in kinds
    filler.open_error = None
    assert runner.prefill(approved_job).code == EXIT_OK
    filler.open_error = "no application form found"
    result = runner.apply(approved_job)
    assert result.code == EXIT_VALIDATION
    assert "open failed" in result.message
    assert not filler.submitted


def test_new_required_question_can_be_learned_without_an_endless_loop(
    conn, settings, bank, approved_job
):
    filler = FakeFiller(SCHEMA)
    runner = make_runner(conn, settings, bank, filler)
    runner.prefill(approved_job)
    filler.extra_fields = [FormField("new", "Describe a project", FieldKind.TEXTAREA, True)]
    assert runner.apply(approved_job).code == EXIT_UNMATCHED
    bank.learn("Describe a project", "A verified example", approved_job)
    assert runner.re_resolve(approved_job).code == EXIT_OK
    assert runner.apply(approved_job).code == EXIT_OK
    assert filler.submitted


def test_changed_question_meaning_never_reuses_old_answer(conn, settings, bank, approved_job):
    from dataclasses import replace

    filler = FakeFiller(SCHEMA)
    runner = make_runner(conn, settings, bank, filler)
    runner.prefill(approved_job)
    filler.schema = replace(
        SCHEMA,
        fields=[
            replace(f, label="Current CTC") if f.key == "q_notice" else f for f in SCHEMA.fields
        ],
    )
    assert runner.apply(approved_job).code == EXIT_UNMATCHED
    assert not filler.submitted
    refreshed = AnswerSet.load(runner.job_dir(approved_job) / "answers.json")
    assert refreshed.answers["q_notice"].value == bank.get("compensation.current_ctc_lpa")


def test_apply_rechecks_cap_after_opening_form(
    conn, settings, bank, approved_job, norm, priority, make_posting, monkeypatch
):
    filler = FakeFiller(SCHEMA)
    runner = make_runner(conn, settings, bank, filler)
    runner.prefill(approved_job)
    other = upsert_job(
        conn,
        make_posting(
            source_job_id="other", company="Another company", url="https://example.invalid/other"
        ),
        norm,
        priority,
    ).job_id
    settings.raw["apply"]["daily_cap"] = 1
    monkeypatch.setattr(
        filler, "open", lambda *args: set_job_status(conn, other, Status.APPLIED, force=True)
    )
    assert runner.apply(approved_job).code == EXIT_SAFETY
    assert not filler.submitted


def test_changed_answer_bank_requires_review_before_submit(conn, settings, bank, approved_job):
    filler = FakeFiller(SCHEMA)
    runner = make_runner(conn, settings, bank, filler)
    runner.prefill(approved_job)
    bank.data["notice_period"]["text"] = "90 days"
    assert runner.apply(approved_job).code == EXIT_UNMATCHED
    assert not filler.submitted
    answers = AnswerSet.load(runner.job_dir(approved_job) / "answers.json")
    assert answers.answers["q_notice"].value == "90 days"


def test_assisted_submission_reservation_uses_same_gate(
    conn, settings, bank, approved_job, monkeypatch
):
    from typer.testing import CliRunner

    from jobhunt import cli
    from jobhunt.cli_common import Ctx
    from jobhunt.repo.jobs import set_prefill_state

    url = "https://example.wd5.myworkdayjobs.com/en-US/careers/job/role"
    with conn:
        conn.execute(
            "UPDATE jobs SET ats_type = 'workday', url = ?, apply_url = ? WHERE id = ?",
            (url, url, approved_job),
        )
    set_prefill_state(conn, approved_job, "ready")
    monkeypatch.setattr(cli, "_ctx", lambda **kwargs: Ctx(settings, conn))
    monkeypatch.setattr(cli, "_bank", lambda ctx: bank)
    command = ["reserve-application", str(approved_job)]
    result = CliRunner().invoke(cli.app, command)
    assert result.exit_code == 0, result.output
    assert get_job(conn, approved_job)["submission_state"] == "pending"
    assert CliRunner().invoke(cli.app, command).exit_code == EXIT_SAFETY


def test_failed_prefill_write_cannot_leave_old_ready_state(
    conn, settings, bank, approved_job, monkeypatch
):
    runner = make_runner(conn, settings, bank, FakeFiller(SCHEMA))
    assert runner.prefill(approved_job).code == EXIT_OK

    def fail_save(*args):
        raise OSError("simulated unavailable storage")

    monkeypatch.setattr(AnswerSet, "save", fail_save)
    with pytest.raises(OSError):
        runner.prefill(approved_job)
    assert get_job(conn, approved_job)["prefill_state"] != "ready"


def test_files_include_cover_letter_pdf_when_present(conn, settings, bank, approved_job):
    runner = make_runner(conn, settings, bank, FakeFiller(SCHEMA))
    job_dir = settings.path("applications_dir") / str(approved_job)
    job_dir.mkdir(parents=True, exist_ok=True)
    assert runner._files(approved_job).cover_letter_pdf is None
    (job_dir / "cover_letter.pdf").write_bytes(b"%PDF-1.4")
    assert runner._files(approved_job).cover_letter_pdf == job_dir / "cover_letter.pdf"


@pytest.mark.parametrize("confirmed", [False, True])
def test_confirmation_screenshot_failure_never_allows_resubmission(
    conn, settings, bank, approved_job, confirmed
):
    filler = FakeFiller(SCHEMA)
    if not confirmed:
        filler.confirmation = None
    runner = make_runner(conn, settings, bank, filler)
    runner.prefill(approved_job)
    original = runner.screenshot

    def failing_shot(page, path):
        if path.name == "confirmation.png":
            raise OSError("screenshot storage unavailable")
        return original(page, path)

    runner.screenshot = failing_shot
    result = runner.apply(approved_job)
    assert result.code == (EXIT_OK if confirmed else EXIT_SUBMISSION_UNKNOWN)
    filler.submitted = False
    assert runner.apply(approved_job).code == EXIT_SAFETY
    assert not filler.submitted


def test_interrupted_submit_is_durable_across_connections(
    conn, settings, bank, approved_job, tmp_path
):
    from jobhunt.db import connect, init_db

    class InterruptedFiller(FakeFiller):
        def submit(self, page):
            super().submit(page)
            raise KeyboardInterrupt

    path = tmp_path / "durable.db"
    disk = connect(path)
    conn.backup(disk)
    filler = InterruptedFiller(SCHEMA)
    runner = make_runner(disk, settings, bank, filler)
    runner.prefill(approved_job)
    with pytest.raises(KeyboardInterrupt):
        runner.apply(approved_job)
    disk.close()
    reopened = connect(path)
    try:
        init_db(reopened)
        row = get_job(reopened, approved_job)
        assert row["submission_state"] == "pending"
        assert row["prefill_state"] == "blocked"
        second_filler = FakeFiller(SCHEMA)
        second = make_runner(reopened, settings, bank, second_filler)
        assert second.apply(approved_job).code == EXIT_SAFETY
        assert not second_filler.submitted
    finally:
        reopened.close()


def test_prefill_and_answer_learning_cannot_clear_uncertain_submission(
    conn, settings, bank, approved_job
):
    filler = FakeFiller(SCHEMA)
    filler.confirmation = None
    runner = make_runner(conn, settings, bank, filler)
    runner.prefill(approved_job)
    assert runner.apply(approved_job).code == EXIT_SUBMISSION_UNKNOWN
    assert runner.prefill(approved_job).code == EXIT_SAFETY
    assert runner.re_resolve(approved_job).code == EXIT_SAFETY
    assert get_job(conn, approved_job)["prefill_state"] == "blocked"


def test_post_submit_captcha_does_not_create_retryable_outcome(
    conn, settings, bank, approved_job, monkeypatch
):
    filler = FakeFiller(SCHEMA)
    filler.confirmation = None
    runner = make_runner(conn, settings, bank, filler)
    runner.prefill(approved_job)
    checks = iter([None, runner_mod.RunResult(approved_job, EXIT_CAPTCHA, "captcha")])
    monkeypatch.setattr(Runner, "_captcha_blocked", lambda *args: next(checks))
    assert runner.apply(approved_job).code == EXIT_SUBMISSION_UNKNOWN
    filler.submitted = False
    assert runner.apply(approved_job).code == EXIT_SAFETY
    assert not filler.submitted


def test_reconcile_requires_explicit_verified_decision(
    conn, settings, bank, approved_job, monkeypatch
):
    from typer.testing import CliRunner

    from jobhunt import cli
    from jobhunt.cli_common import Ctx

    filler = FakeFiller(SCHEMA)
    filler.confirmation = None
    runner = make_runner(conn, settings, bank, filler)
    runner.prefill(approved_job)
    assert runner.apply(approved_job).code == EXIT_SUBMISSION_UNKNOWN
    monkeypatch.setattr(cli, "_ctx", lambda **kwargs: Ctx(settings, conn))
    command = ["reconcile-submission", str(approved_job)]
    assert CliRunner().invoke(cli.app, [*command, "--note", "checked"]).exit_code != 0
    assert CliRunner().invoke(cli.app, [*command, "--not-submitted", "--note", " "]).exit_code != 0
    assert applications.submission_unresolved(conn, approved_job)
    result = CliRunner().invoke(
        cli.app, [*command, "--not-submitted", "--note", "checked ATS: no application"]
    )
    assert result.exit_code == 0, result.output
    assert not applications.submission_unresolved(conn, approved_job)
    assert get_job(conn, approved_job)["prefill_state"] == "none"
    assert runner.prefill(approved_job).code == EXIT_OK


def test_submission_reservation_is_exclusive_across_connections(
    conn, settings, bank, approved_job, tmp_path
):
    from jobhunt.db import connect

    runner = make_runner(conn, settings, bank, FakeFiller(SCHEMA))
    runner.prefill(approved_job)
    path = tmp_path / "exclusive.db"
    first = connect(path)
    conn.backup(first)
    second = connect(path)
    try:
        with first:
            assert applications.begin_submission(first, approved_job)
        with second:
            assert not applications.begin_submission(second, approved_job)
        assert get_job(second, approved_job)["submission_state"] == "pending"
    finally:
        first.close()
        second.close()


def test_known_preclick_failure_is_retryable(conn, settings, bank, approved_job):
    from jobhunt.apply.fillers.base import SubmissionNotStarted

    class DisabledFiller(FakeFiller):
        def submit(self, page):
            raise SubmissionNotStarted("submit button is disabled")

    runner = make_runner(conn, settings, bank, DisabledFiller(SCHEMA))
    runner.prefill(approved_job)
    assert runner.apply(approved_job).code == EXIT_VALIDATION
    assert get_job(conn, approved_job)["submission_state"] == "none"
    assert not applications.submission_unresolved(conn, approved_job)


def test_playwright_error_during_click_is_uncertain(conn, settings, bank, approved_job):
    from playwright.sync_api import Error as PlaywrightError

    class LostPage(FakeFiller):
        def submit(self, page):
            super().submit(page)
            raise PlaywrightError("page closed during navigation")

    runner = make_runner(conn, settings, bank, LostPage(SCHEMA))
    runner.prefill(approved_job)
    assert runner.apply(approved_job).code == EXIT_SUBMISSION_UNKNOWN
    assert applications.submission_unresolved(conn, approved_job)


def test_a_wizard_that_never_advances_is_bounded_and_never_submits(
    conn, settings, bank, approved_job
):
    """A page stuck on a validation error must not loop forever, and must not submit."""

    class StuckWizard(FakeFiller):
        def __init__(self, schema):
            super().__init__(schema)
            self.pages_filled = 0

        def fill(self, page, schema, answers, profile, files):
            self.pages_filled += 1
            return FillReport(filled=list(answers))

        def advance(self, page) -> bool:
            return True  # always claims another page, never actually moves on

    filler = StuckWizard(SCHEMA)
    runner = make_runner(conn, settings, bank, filler)
    assert runner.prefill(approved_job).code == EXIT_OK
    filler.pages_filled = 0  # prefill does not fill; start the count at the apply run
    result = runner.apply(approved_job)

    assert filler.submitted is False, "a wizard that never finishes must not be submitted"
    assert filler.pages_filled <= runner_mod.MAX_PAGES
    assert result.code == EXIT_VALIDATION
    assert "not finished" in result.message


def test_a_two_page_wizard_fills_both_pages_then_submits(conn, settings, bank, approved_job):
    """The happy path for a multi-page form: every page filled, one submit at the end."""

    class TwoPages(FakeFiller):
        def __init__(self, schema):
            super().__init__(schema)
            self.pages_filled = 0

        def fill(self, page, schema, answers, profile, files):
            self.pages_filled += 1
            return FillReport(filled=list(answers))

        def advance(self, page) -> bool:
            return self.pages_filled < 2

    filler = TwoPages(SCHEMA)
    runner = make_runner(conn, settings, bank, filler)
    assert runner.prefill(approved_job).code == EXIT_OK
    filler.pages_filled = 0
    result = runner.apply(approved_job)

    assert filler.pages_filled == 2
    assert filler.submitted is True
    assert result.code == EXIT_OK


def test_a_later_wizard_page_is_answered_from_the_bank_not_from_page_one(
    conn, settings, bank, approved_job
):
    """Page two was never prefilled, so its answers must be resolved when it appears."""
    extra = FormField(
        key="page2_note",
        label="Why do you want this role?",
        kind=FieldKind.TEXTAREA,
        required=False,
    )

    class SecondPage(FakeFiller):
        def __init__(self, schema):
            super().__init__(schema)
            self.pages = 0
            self.answers_seen: list[set[str]] = []

        def extract_schema(self, page):
            base = FormSchema(self.schema.ats, self.schema.url, list(self.schema.fields))
            if self.pages >= 1:
                base.fields.append(extra)
            return base

        def fill(self, page, schema, answers, profile, files):
            self.pages += 1
            self.answers_seen.append(set(answers))
            return FillReport(filled=list(answers))

        def advance(self, page) -> bool:
            return self.pages < 2

    filler = SecondPage(SCHEMA)
    runner = make_runner(conn, settings, bank, filler)
    assert runner.prefill(approved_job).code == EXIT_OK
    result = runner.apply(approved_job)

    assert result.code == EXIT_OK, result.message
    assert filler.pages == 2
    assert filler.submitted is True


def test_a_later_page_question_does_not_destroy_the_reviewed_prefill(
    conn, settings, bank, approved_job
):
    """Overwriting form_schema.json with page two made the next run loop forever."""
    blocker = FormField(
        key="page2_required",
        label="Describe your preferred Kafka partition rebalancing strategy",
        kind=FieldKind.TEXT,
        required=True,
    )

    class AsksLater(FakeFiller):
        def __init__(self, schema):
            super().__init__(schema)
            self.pages = 0

        def extract_schema(self, page):
            base = FormSchema(self.schema.ats, self.schema.url, list(self.schema.fields))
            if self.pages >= 1:
                base.fields = [blocker]
            return base

        def fill(self, page, schema, answers, profile, files):
            self.pages += 1
            return FillReport(filled=list(answers))

        def advance(self, page) -> bool:
            return self.pages < 2

    filler = AsksLater(SCHEMA)
    runner = make_runner(conn, settings, bank, filler)
    assert runner.prefill(approved_job).code == EXIT_OK
    job_dir = settings.path("applications_dir") / str(approved_job)
    page_one = (job_dir / "form_schema.json").read_text(encoding="utf-8")

    result = runner.apply(approved_job)

    assert result.code == EXIT_UNMATCHED
    assert "Kafka partition" in result.message
    assert filler.submitted is False
    assert (job_dir / "form_schema.json").read_text(encoding="utf-8") == page_one
    assert (job_dir / "form_schema_page2.json").exists(), "the new page is recorded separately"
