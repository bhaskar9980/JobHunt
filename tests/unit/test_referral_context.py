"""Outreach drafts take the sender's own details from the gitignored profile, not tracked files."""

from jobhunt.cli_refer import _draft_context
from jobhunt.referral.rules import ReferralConfig

JOB = {"company": "Postman", "title": "Software Engineer II", "url": "https://jobs.example/1"}
CONTACT = {"name": "Priya Natarajan"}
CANDIDATE = {
    "name": "Alex Example",
    "current_company": "Example Corp",
    "one_liner": "I build backend services.",
    "links": {"portfolio": "https://alex.example/"},
}


def test_signature_company_link_and_pitch_come_from_the_profile():
    ctx = _draft_context(JOB, CONTACT, ReferralConfig(), "a hook", None, CANDIDATE)
    assert ctx["first_name"] == "Priya"
    assert ctx["signature"] == "Alex"
    assert ctx["current_company"] == "Example Corp"
    assert ctx["resume_link"] == "https://alex.example/"
    assert ctx["one_liner"] == "I build backend services."


def test_referrals_yaml_and_cli_flag_still_override_the_profile():
    cfg = ReferralConfig(resume_link="https://cfg.example/", default_one_liner="cfg pitch")
    ctx = _draft_context(JOB, CONTACT, cfg, "a hook", "flag pitch", CANDIDATE)
    assert ctx["resume_link"] == "https://cfg.example/"
    assert ctx["one_liner"] == "flag pitch"


def test_missing_profile_leaves_sender_fields_blank():
    ctx = _draft_context(JOB, CONTACT, ReferralConfig(), "a hook", None, None)
    assert ctx["signature"] == ""
    assert ctx["current_company"] == ""
    assert ctx["resume_link"] == ""
    assert ctx["one_liner"] == ""
