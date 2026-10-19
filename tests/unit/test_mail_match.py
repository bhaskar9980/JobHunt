"""Matcher: subject templates, sender domain, body mentions, ambiguity and title tie-breaks."""

from __future__ import annotations

import pytest
from mail_support import COMPANIES, RULES, load_email, seed

from jobhunt.mail.match import extract_subject_hints, match_application
from jobhunt.mail.models import MailMessage
from jobhunt.models import Status


def _msg(subject: str, body: str, sender: str, domain: str | None = None) -> MailMessage:
    data = {
        "schema_version": 1,
        "thread_id": "t",
        "gmail_id": "m",
        "from": sender,
        "date": "2026-09-05T10:00:00Z",
        "subject": subject,
        "body_text": body,
    }
    if domain:
        data["from_domain"] = domain
    return MailMessage.from_dict(data)


@pytest.mark.parametrize(
    ("subject", "company", "title"),
    [
        ("Thank you for applying to Postman", "postman", None),
        (
            "Figma - Software Engineer interview availability",
            "figma",
            "software engineer interview availability",
        ),
        ("Coinbase: Online Assessment Invitation", "coinbase", "online assessment invitation"),
        ("Backend role at Ramp - quick chat?", "ramp", "backend role"),
        ("Your application for Backend Engineer at Postman", "postman", "backend engineer"),
        ("Your offer from Linear", "linear", None),
        ("30 new jobs for software engineer in Chennai", None, None),
    ],
)
def test_extract_subject_hints(subject, company, title):
    hints = extract_subject_hints(subject, RULES)
    assert (hints.companies[0] if hints.companies else None) == company
    assert hints.title == title


def test_late_reply_can_match_ghosted_application(conn, norm, priority, make_posting):
    job = seed(
        conn,
        norm,
        priority,
        make_posting,
        company="Postman",
        title="Software Engineer II",
        status=Status.GHOSTED,
    )
    msg = _msg(
        "Your application for Software Engineer II at Postman",
        "Interview invitation",
        "recruiting@postman.com",
    )
    assert match_application(conn, msg, RULES, COMPANIES, norm).job_id == job


def test_explicit_other_role_does_not_match_only_company_application(
    conn, norm, priority, make_posting
):
    seed(conn, norm, priority, make_posting, company="Postman", title="Software Engineer II")
    msg = _msg(
        "Your application for Sales Director at Postman",
        "Unfortunately, not selected",
        "recruiting@postman.com",
    )
    assert match_application(conn, msg, RULES, COMPANIES, norm).job_id is None


def test_no_open_applications(conn, norm):
    result = match_application(conn, load_email("confirmation_greenhouse"), RULES, COMPANIES, norm)
    assert result.job_id is None
    assert result.reason == "no open applications"


def test_match_by_subject_template(conn, norm, priority, make_posting):
    job = seed(conn, norm, priority, make_posting, company="Postman", title="Software Engineer II")
    seed(conn, norm, priority, make_posting, company="Stripe", title="Backend Engineer", sid="2")
    result = match_application(conn, load_email("confirmation_greenhouse"), RULES, COMPANIES, norm)
    assert result.job_id == job
    assert result.confidence >= 0.85
    assert len(result.candidates) == 1


def test_match_by_sender_domain(conn, norm, priority, make_posting):
    stripe = seed(conn, norm, priority, make_posting, company="Stripe", title="Backend Engineer")
    msg = _msg("An update", "Thanks for your patience.", "Recruiting <recruiting@stripe.com>")
    result = match_application(conn, msg, RULES, COMPANIES, norm)
    assert result.job_id == stripe


def test_ats_domain_is_not_treated_as_company(conn, norm, priority, make_posting):
    seed(conn, norm, priority, make_posting, company="Greenhouse", title="Software Engineer")
    msg = _msg("Hello", "Nothing to see here.", "no-reply@greenhouse.io")
    assert match_application(conn, msg, RULES, COMPANIES, norm).job_id is None


def test_match_by_company_mention_in_body(conn, norm, priority, make_posting):
    coinbase = seed(
        conn, norm, priority, make_posting, company="Coinbase", title="Software Engineer, Backend"
    )
    seed(
        conn, norm, priority, make_posting, company="Postman", title="Software Engineer II", sid="2"
    )
    msg = _msg(
        "Online Assessment Invitation",
        "Please complete the coding challenge for the backend role at Coinbase.",
        "no-reply@hackerrankforwork.com",
    )
    result = match_application(conn, msg, RULES, COMPANIES, norm)
    assert result.job_id == coinbase
    assert result.reason == "company mentioned in body"


def test_alias_maps_to_canonical_company(conn, norm, priority, make_posting):
    cursor = seed(conn, norm, priority, make_posting, company="Cursor", title="Software Engineer")
    msg = _msg("Thank you for applying to Anysphere", "We got it.", "jobs@anysphere.inc")
    assert match_application(conn, msg, RULES, COMPANIES, norm).job_id == cursor


def test_two_open_applications_same_company_is_ambiguous(conn, norm, priority, make_posting):
    a = seed(conn, norm, priority, make_posting, company="Postman", title="Software Engineer II")
    b = seed(
        conn,
        norm,
        priority,
        make_posting,
        company="Postman",
        title="Backend Engineer, Platform",
        sid="2",
    )
    msg = _msg(
        "Thank you for applying to Postman", "We will be in touch.", "no-reply@greenhouse.io"
    )
    result = match_application(conn, msg, RULES, COMPANIES, norm)
    assert result.job_id is None
    assert result.ambiguous
    assert {c.job_id for c in result.candidates} == {a, b}


def test_title_in_subject_disambiguates(conn, norm, priority, make_posting):
    seed(conn, norm, priority, make_posting, company="Postman", title="Software Engineer II")
    b = seed(
        conn,
        norm,
        priority,
        make_posting,
        company="Postman",
        title="Backend Engineer, Platform",
        sid="2",
    )
    msg = _msg(
        "Your application for Backend Engineer at Postman", "Thanks.", "no-reply@greenhouse.io"
    )
    result = match_application(conn, msg, RULES, COMPANIES, norm)
    assert result.job_id == b
    assert result.reason.endswith("+ title")


def test_title_in_body_disambiguates(conn, norm, priority, make_posting):
    seed(conn, norm, priority, make_posting, company="Postman", title="Software Engineer II")
    b = seed(
        conn,
        norm,
        priority,
        make_posting,
        company="Postman",
        title="Backend Engineer, Platform",
        sid="2",
    )
    msg = _msg(
        "Thank you for applying to Postman",
        "We received your application for the Backend Engineer, Platform role.",
        "no-reply@greenhouse.io",
    )
    assert match_application(conn, msg, RULES, COMPANIES, norm).job_id == b


def test_closed_applications_are_ignored(conn, norm, priority, make_posting):
    seed(
        conn,
        norm,
        priority,
        make_posting,
        company="Postman",
        title="Software Engineer II",
        status=Status.REJECTED,
    )
    result = match_application(conn, load_email("confirmation_greenhouse"), RULES, COMPANIES, norm)
    assert result.job_id is None
