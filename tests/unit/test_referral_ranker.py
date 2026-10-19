"""Referral ranking: weights, cooldown, alias company match, ordering, cap."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jobhunt.referral.ranker import company_keys, score_contact, suggest
from jobhunt.referral.rules import ReferralConfig
from jobhunt.repo import outreach as outreach_repo
from jobhunt.repo.contacts import ContactRecord, upsert_contact
from jobhunt.repo.jobs import get_job, upsert_job

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
ALIASES = {"platform science": ["trimble transport"]}
CFG = ReferralConfig.from_dict(
    {
        "role_tokens": ["backend", ".net", "platform", "software"],
        "engineer_tokens": ["engineer", "developer"],
        "recruiter_tokens": ["recruiter", "talent"],
        "manager_tokens": ["manager", "lead", "head"],
        "max_suggestions": 3,
    }
)
ROLE, ENGINEER, RECRUITER, MANAGER, RECENT, REPLY, COOLDOWN = 40, 30, 25, 20, 10, 15, -100


def _contact(**overrides):
    base = {"name": "X", "title": None, "connected_on": None}
    base.update(overrides)
    return base


def _score(**overrides):
    kwargs = {"prior_reply": False, "last_asked": None}
    kwargs.update({k: overrides.pop(k) for k in ("prior_reply", "last_asked") if k in overrides})
    return score_contact(
        _contact(**overrides), "Software Engineer II, Backend", CFG, now=NOW, **kwargs
    )


def test_same_role_engineer_scores_role_plus_engineer():
    score, reasons = _score(title="Backend Software Engineer")
    assert score == ROLE + ENGINEER
    assert any("same team or role" in r for r in reasons) and "engineer" in reasons


def test_recruiter_and_manager_weights():
    assert _score(title="Technical Recruiter")[0] == RECRUITER
    assert _score(title="Engineering Manager")[0] == MANAGER
    assert _score(title="Head of Talent")[0] == RECRUITER + MANAGER


def test_recent_connection_and_prior_reply_bonuses():
    recent = (NOW - timedelta(days=100)).date().isoformat()
    old = (NOW - timedelta(days=3 * 365)).date().isoformat()
    assert _score(title="Engineer", connected_on=recent)[0] == ENGINEER + RECENT
    assert _score(title="Engineer", connected_on=old)[0] == ENGINEER
    assert _score(title="Engineer", prior_reply=True)[0] == ENGINEER + REPLY


def test_cooldown_penalty_kills_the_score():
    asked = (NOW - timedelta(days=10)).isoformat()
    score, reasons = _score(title="Backend Engineer", last_asked=asked)
    assert score == ROLE + ENGINEER + COOLDOWN
    assert any("asked within" in r for r in reasons)
    long_ago = (NOW - timedelta(days=90)).isoformat()
    assert _score(title="Backend Engineer", last_asked=long_ago)[0] == ROLE + ENGINEER


def test_company_keys_include_aliases_both_ways(norm):
    assert company_keys("platform science", norm, ALIASES) == {
        "platform science",
        "trimble transport",
    }
    assert company_keys("trimble transport", norm, ALIASES) == {
        "platform science",
        "trimble transport",
    }
    assert company_keys("stripe", norm, ALIASES) == {"stripe"}


@pytest.fixture
def job(conn, norm, priority, make_posting):
    posting = make_posting(company="Platform Science", title="Software Engineer II, Backend")
    return get_job(conn, upsert_job(conn, posting, norm, priority).job_id)


def _seed(conn, name, title, company_norm, connected_on=None):
    record = ContactRecord(
        name=name,
        title=title,
        company=company_norm.title(),
        company_norm=company_norm,
        linkedin_url=f"https://www.linkedin.com/in/{name.lower().replace(' ', '-')}",
        connected_on=connected_on,
    )
    with conn:
        contact_id, _ = upsert_contact(conn, record)
    return contact_id


def test_suggest_ranks_and_caps_and_uses_aliases(conn, norm, job):
    _seed(conn, "Rohit Sharma", "Software Engineer II", "trimble transport")
    _seed(conn, "Arjun Mehta", "Technical Recruiter", "platform science")
    _seed(conn, "Kavya Iyer", "Engineering Manager", "platform science")
    _seed(conn, "Neha Verma", "Product Designer", "platform science")
    _seed(conn, "Someone Else", "Backend Engineer", "stripe")
    ranked = suggest(conn, job, CFG, norm, ALIASES, now=NOW)
    assert [s.name for s in ranked] == ["Rohit Sharma", "Arjun Mehta", "Kavya Iyer"]
    assert ranked[0].score == ROLE + ENGINEER
    assert ranked[0].linkedin_url.endswith("/rohit-sharma")
    assert all(s.to_dict()["contact_id"] for s in ranked)


def test_suggest_applies_cooldown_from_outreach_history(conn, norm, job):
    hot = _seed(conn, "Rohit Sharma", "Backend Engineer", "platform science")
    _seed(conn, "Arjun Mehta", "Technical Recruiter", "platform science")
    with conn:
        outreach_id = outreach_repo.create(
            conn, contact_id=hot, job_id=job["id"], channel="linkedin", kind="referral"
        )
        outreach_repo.mark_sent(conn, outreach_id, (NOW - timedelta(days=5)).isoformat(), 7)
    ranked = suggest(conn, job, CFG, norm, ALIASES, now=NOW)
    assert [s.name for s in ranked] == ["Arjun Mehta"]


def test_suggest_empty_when_nobody_at_company(conn, norm, job):
    _seed(conn, "Someone Else", "Backend Engineer", "stripe")
    assert suggest(conn, job, CFG, norm, ALIASES, now=NOW) == []
