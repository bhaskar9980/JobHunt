"""Classifier: every fixture lands in its class at >= 0.80; newsletters stay unclassified."""

from __future__ import annotations

import pytest
from mail_support import RULES, load_email

from jobhunt.mail.classify import classify
from jobhunt.mail.models import (
    KIND_ASSESSMENT,
    KIND_CONFIRMATION,
    KIND_INTERVIEW,
    KIND_OFFER,
    KIND_RECRUITER,
    KIND_REJECTION,
    KIND_UNCLASSIFIED,
    MailFileError,
    MailMessage,
)

AUTO = 0.80


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("confirmation_greenhouse", KIND_CONFIRMATION),
        ("confirmation_lever", KIND_CONFIRMATION),
        ("confirmation_ashby", KIND_CONFIRMATION),
        ("confirmation_workday", KIND_CONFIRMATION),
        ("rejection_unfortunately", KIND_REJECTION),
        ("rejection_other_candidates", KIND_REJECTION),
        ("interview_calendly", KIND_INTERVIEW),
        ("assessment_hackerrank", KIND_ASSESSMENT),
        ("recruiter_cold", KIND_RECRUITER),
        ("offer", KIND_OFFER),
    ],
)
def test_fixture_classifies_with_high_confidence(name, kind):
    result = classify(load_email(name), RULES)
    assert result.kind == kind
    assert result.confidence >= AUTO
    assert result.evidence


def test_newsletter_is_unclassified():
    result = classify(load_email("newsletter_linkedin"), RULES)
    assert result.kind == KIND_UNCLASSIFIED
    assert result.confidence == 0.0
    assert any(e.startswith("-") for e in result.evidence)


def _msg(subject: str, body: str, sender: str = "Ada <ada@acme.com>") -> MailMessage:
    return MailMessage.from_dict(
        {
            "schema_version": 1,
            "thread_id": "t1",
            "gmail_id": "m1",
            "from": sender,
            "date": "2026-09-01T00:00:00Z",
            "subject": subject,
            "body_text": body,
        }
    )


def test_ats_domain_boosts_only_flagged_classes():
    plain = classify(_msg("Hello", "we received your application"), RULES)
    ats = classify(
        _msg("Hello", "we received your application", sender="no-reply@greenhouse.io"), RULES
    )
    assert ats.scores[KIND_CONFIRMATION] > plain.scores[KIND_CONFIRMATION]
    recruiter_plain = classify(_msg("Hi", "came across your profile"), RULES)
    recruiter_ats = classify(
        _msg("Hi", "came across your profile", sender="no-reply@greenhouse.io"), RULES
    )
    assert recruiter_ats.scores[KIND_RECRUITER] == recruiter_plain.scores[KIND_RECRUITER]


def test_subject_phrase_earns_bonus():
    in_body = classify(_msg("Hello", "we have received your application"), RULES)
    in_subject = classify(_msg("We have received your application", "Hello"), RULES)
    assert in_subject.scores[KIND_CONFIRMATION] > in_body.scores[KIND_CONFIRMATION]


def test_precedence_breaks_near_ties():
    rules = {
        "thresholds": {"auto": 0.8, "review": 0.5},
        "tie_margin": 0.1,
        "precedence": ["rejection", "confirmation"],
        "classes": {
            "confirmation": {"phrases": {"application": 0.9}},
            "rejection": {"phrases": {"application": 0.85}},
        },
    }
    result = classify(_msg("application", ""), rules)
    assert result.kind == "rejection"


def test_below_review_threshold_is_unclassified():
    result = classify(_msg("Hello", "will be in touch"), RULES)
    assert result.kind == KIND_UNCLASSIFIED
    assert 0 < result.confidence < 0.5


def test_from_dict_validation():
    with pytest.raises(MailFileError):
        MailMessage.from_dict({"schema_version": 1, "gmail_id": "x"})
    with pytest.raises(MailFileError):
        MailMessage.from_dict({"schema_version": 2, "thread_id": "t"})
    with pytest.raises(MailFileError, match="gmail_id"):
        MailMessage.from_dict({"schema_version": 1, "thread_id": "t", "from": "a@b.co"})
    msg = MailMessage.from_dict(
        {
            "schema_version": 1,
            "thread_id": "t",
            "gmail_id": "t",
            "from": "Ada Lovelace <ada@acme.co.uk>",
        }
    )
    assert msg.gmail_id == "t"
    assert msg.file_name == "t_t.json"
    assert msg.from_domain == "acme.co.uk"
    assert msg.from_name == "Ada Lovelace"
    assert msg.sender_email == "ada@acme.co.uk"
    assert msg.to_dict()["from"] == "Ada Lovelace <ada@acme.co.uk>"


def test_bare_domain_negative_sender_matches_subdomains():
    body = "Thank you for applying. New jobs for you: apply now to 12 software engineer roles."
    alert = _msg(
        "Software Engineer jobs in Chennai", body, sender="Indeed <alert@match.indeed.com>"
    )
    result = classify(alert, RULES)
    assert result.kind == KIND_UNCLASSIFIED
    assert any(
        e == "-sender:indeed.com" or e.endswith("sender:indeed.com") for e in result.evidence
    )
