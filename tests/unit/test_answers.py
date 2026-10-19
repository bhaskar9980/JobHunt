from pathlib import Path

import pytest
import yaml

from jobhunt.apply.answers import (
    AnswerBank,
    AnswerBankError,
    coerce_to_option,
    decline_option,
    detect_country,
    is_eeo_question,
    normalise_question,
)

EXAMPLE = Path(__file__).parent.parent.parent / "config" / "answers.example.yaml"


@pytest.fixture
def bank(tmp_path: Path) -> AnswerBank:
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    data["compensation"]["current_ctc_lpa"] = 20
    data["compensation"]["expected_ctc_lpa"] = {"min": 28, "max": 34}
    path = tmp_path / "answers.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return AnswerBank.load(path)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "Are you legally authorized to work in the United States? *",
            "are you legally authorized to work in the united states",
        ),
        ("  Notice Period (required) ", "notice period"),
        ("LinkedIn Profile:", "linkedin profile"),
    ],
)
def test_normalise_question(raw, expected):
    assert normalise_question(raw) == expected


def test_missing_file_gives_actionable_error(tmp_path):
    with pytest.raises(AnswerBankError, match="answers.example.yaml"):
        AnswerBank.load(tmp_path / "nope.yaml")


def test_work_authorisation_per_country(bank):
    us = bank.match("Are you legally authorized to work in the United States?")
    india = bank.match("Are you authorised to work in India?")
    sponsorship = bank.match("Will you now or in the future require visa sponsorship?")
    assert us is not None and us.value is False and us.source == "rule"
    assert india is not None and india.value is True
    assert sponsorship is not None and sponsorship.value is True  # default country


def test_rule_values_and_compensation_rendering(bank):
    assert bank.match("What is your notice period?").value == "60 days, negotiable"
    assert bank.match("Expected CTC").value == "28-34 LPA INR"
    assert bank.match("Current salary").value == 20
    assert bank.match("LinkedIn URL").value == "https://www.linkedin.com/in/your-handle/"
    assert bank.match("How many years of experience with Python?").value == 3


def test_unknown_question_returns_none(bank):
    assert bank.match("Describe your favourite architecture decision") is None


def test_learn_then_exact_and_fuzzy_match_and_save(bank):
    bank.learn("Why do you want to join Acme?", "Because payroll at scale is hard.", job_id=7)
    exact = bank.match("Why do you want to join Acme?")
    assert exact is not None and exact.source == "learned"
    fuzzy = bank.match("Why do you want to join Acme? *")
    assert fuzzy is not None and fuzzy.source in {"learned", "fuzzy"}
    bank.save()
    reloaded = AnswerBank.load(bank.path)
    learned = reloaded.data["learned"]
    key = next(iter(learned))
    assert learned[key]["job_id"] == 7
    assert learned[key]["answer"] == "Because payroll at scale is hard."


def test_fuzzy_respects_threshold(bank):
    bank.learn("Tell us about a project you are proud of", "BlockPay")
    assert bank.match("Tell us about a project you are proud of!") is not None
    assert bank.match("Favourite colour?") is None


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("authorized to work in the U.S.", "US"),
        ("right to work in the UK", "GB"),
        ("eligible to work in India", "IN"),
        ("no country here", None),
    ],
)
def test_detect_country(text, code):
    assert detect_country(text) == code


def test_option_coercion():
    options = ["Yes", "No", "Prefer not to say"]
    assert coerce_to_option(True, options, 80) == "Yes"
    assert coerce_to_option(False, options, 80) == "No"
    assert (
        coerce_to_option("60 days, negotiable", ["Immediate", "30 days", "60 days", "90 days"], 80)
        == "60 days"
    )
    assert coerce_to_option("purple", ["Yes", "No"], 80) is None
    assert coerce_to_option(True, [], 80) is None


def test_eeo_detection_and_decline():
    assert is_eeo_question("Gender")
    assert is_eeo_question("Are you a protected veteran?")
    assert is_eeo_question("Anything", section="Voluntary Self-Identification of Disability")
    assert not is_eeo_question("Years of experience")
    assert (
        decline_option(["Male", "Female", "Decline to self-identify"]) == "Decline to self-identify"
    )
    assert decline_option(["Yes", "No", "I don't wish to answer"]) == "I don't wish to answer"
    assert decline_option(["Yes", "No"]) is None


def test_placeholder_issues_flag_example_defaults(tmp_path):
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    path = tmp_path / "answers.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    issues = AnswerBank.load(path).placeholder_issues()
    assert any("email" in i for i in issues)
    assert any("phone" in i for i in issues)
    assert any("current_ctc" in i for i in issues)
    assert any("expected_ctc" in i for i in issues)


def test_placeholder_issues_empty_once_filled(bank):
    bank.data["profile"]["email"] = "candidate@test.invalid"
    bank.data["profile"]["phone"]["number"] = "98765 43210"
    assert bank.placeholder_issues() == []


def test_fixed_pay_target_satisfies_readiness_without_inventing_total_ctc(bank):
    bank.data["profile"]["email"] = "candidate@test.invalid"
    bank.data["profile"]["phone"]["number"] = "1234567890"
    bank.data["compensation"].pop("expected_ctc_lpa")
    bank.data["compensation"]["expected_fixed_lpa"] = {"min": 26, "max": 28}
    assert bank.placeholder_issues() == []


def test_missing_specific_experience_does_not_fall_back_to_total(bank):
    bank.data["experience"]["python"] = None
    bank.data["experience"]["total"] = 3.25
    assert bank.match("How many years of experience with Python?") is None


def test_country_must_be_known_when_applicant_requires_it(bank):
    bank.data["policies"]["require_explicit_country"] = True
    assert bank.match("Will you require visa sponsorship?") is None
    assert bank.match("Will you require visa sponsorship?", country="IN").value is False
    assert bank.match("Will you require visa sponsorship in Canada?").value is True


def test_malformed_private_yaml_reports_location_without_values(tmp_path):
    path = tmp_path / "answers.yaml"
    path.write_text("profile:\n  email: private-sentinel\n   broken: value\n")
    with pytest.raises(AnswerBankError) as error:
        AnswerBank.load(path)
    assert "line" in str(error.value)
    assert "private-sentinel" not in str(error.value)


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), "invalid"])
def test_invalid_compensation_is_a_readiness_issue_not_a_crash(bank, value):
    bank.data["compensation"]["current_ctc_lpa"] = value
    assert any("current_ctc_lpa" in issue for issue in bank.placeholder_issues())


def test_data_directory_override_keeps_runtime_files_together(tmp_path, monkeypatch):
    from jobhunt.config import load_settings

    config = tmp_path / "config"
    config.mkdir()
    (config / "settings.yaml").write_text(
        "paths:\n  data_dir: data\n  db_path: data/jobs.db\n  inbox_dir: data/inbox\n"
        "  applications_dir: data/applications\n"
    )
    target = tmp_path / "isolated"
    monkeypatch.setenv("JOBHUNT_DATA_DIR", str(target))
    monkeypatch.delenv("JOBHUNT_DB_PATH", raising=False)
    settings = load_settings(tmp_path)
    assert settings.db_path == target / "jobs.db"
    assert settings.path("applications_dir") == target / "applications"
    assert settings.path("inbox_dir") == target / "inbox"
    absolute = tmp_path / "data" / "explicit.json"
    assert settings.resolve(absolute) == absolute
    pinned = tmp_path / "pinned.db"
    monkeypatch.setenv("JOBHUNT_DB_PATH", str(pinned))
    assert load_settings(tmp_path).db_path == pinned
    monkeypatch.delenv("JOBHUNT_DB_PATH")
    absolute_db = tmp_path / "data" / "absolute.db"
    (config / "settings.yaml").write_text(
        f"paths:\n  data_dir: data\n  db_path: '{absolute_db.as_posix()}'\n"
    )
    assert load_settings(tmp_path).db_path == absolute_db
