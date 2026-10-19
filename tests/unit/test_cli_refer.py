"""`jobhunt refer ...` and `jobhunt outreach ...` end to end against a temp root and DB."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jobhunt import cli_refer
from jobhunt.cli_refer import outreach_app, refer_app
from jobhunt.db import connect, init_db
from jobhunt.models import JobPosting
from jobhunt.normalize import Normalizer
from jobhunt.referral.route import recommend
from jobhunt.referral.rules import ReferralConfig
from jobhunt.repo import outreach as outreach_repo
from jobhunt.repo.contacts import ContactRecord, upsert_contact
from jobhunt.repo.jobs import get_job, upsert_job

PROJECT_ROOT = Path(__file__).parent.parent.parent
CONNECTIONS = PROJECT_ROOT / "tests" / "fixtures" / "referrals" / "Connections.csv"
EXPECTED_CONTACTS = 5
runner = CliRunner()


@pytest.fixture
def root(tmp_path, monkeypatch):
    """A throwaway project root: real config/ copied in, data/logs/db all under tmp_path."""
    shutil.copytree(
        PROJECT_ROOT / "config",
        tmp_path / "config",
        ignore=shutil.ignore_patterns("answers.yaml", "profile.yaml"),
    )
    shutil.copy(tmp_path / "config" / "profile.example.yaml", tmp_path / "config" / "profile.yaml")
    monkeypatch.setenv("JOBHUNT_ROOT", str(tmp_path))
    monkeypatch.setenv("JOBHUNT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JOBHUNT_DB_PATH", str(tmp_path / "data" / "jobs.db"))
    (tmp_path / "data").mkdir()
    return tmp_path


@pytest.fixture
def job_id(root):
    conn = connect(root / "data" / "jobs.db")
    init_db(conn)
    posting = JobPosting(
        source="greenhouse",
        source_job_id="77",
        company="Postman",
        title="Software Engineer II, Backend",
        url="https://job-boards.greenhouse.io/postman/jobs/77",
        location="Bengaluru, India",
    )
    norm = Normalizer(company_suffixes=frozenset({"inc"}), location_aliases={})
    with conn:
        result = upsert_job(conn, posting, norm, {"default": 10})
    conn.close()
    return result.job_id


def _ok(result):
    assert result.exit_code == 0, result.output
    return result.output


def test_import_then_contacts_and_suggest(root, job_id):
    out = _ok(runner.invoke(refer_app, ["import-connections", str(CONNECTIONS)]))
    assert f"{EXPECTED_CONTACTS} new contacts" in out
    payload = json.loads(
        _ok(runner.invoke(refer_app, ["contacts", "--company", "postman", "--json"]))
    )
    assert sorted(c["name"] for c in payload) == ["Arjun Mehta", "Priya Natarajan"]

    payload = json.loads(_ok(runner.invoke(refer_app, ["suggest", str(job_id), "--json"])))
    names = [s["name"] for s in payload["suggestions"]]
    assert names == ["Priya Natarajan", "Arjun Mehta"]
    assert payload["suggestions"][0]["score"] > payload["suggestions"][1]["score"]

    human = _ok(runner.invoke(refer_app, ["suggest", str(job_id)]))
    assert "Priya Natarajan" in human and "same team or role" in human


def test_suggest_unknown_job_exits_2(root):
    result = runner.invoke(refer_app, ["suggest", "999"])
    assert result.exit_code == cli_refer.EXIT_INVALID
    assert "not found" in result.output


def test_draft_open_send_reply_flow(root, job_id, monkeypatch):
    _ok(runner.invoke(refer_app, ["import-connections", str(CONNECTIONS)]))
    contacts = json.loads(
        _ok(runner.invoke(refer_app, ["contacts", "--company", "priya", "--json"]))
    )
    contact_id = contacts[0]["id"]

    bad = runner.invoke(
        refer_app,
        [
            "draft",
            str(job_id),
            str(contact_id),
            "--template",
            "referral_friend",
            "--hook",
            "Wow — great!",
        ],
    )
    assert bad.exit_code == cli_refer.EXIT_DRAFT_ISSUES
    assert "banned character" in bad.output
    assert not (root / "data" / "outreach").exists() or not any(
        (root / "data" / "outreach").rglob("*.md")
    )

    out = _ok(
        runner.invoke(
            refer_app,
            [
                "draft",
                str(job_id),
                str(contact_id),
                "--template",
                "referral_friend",
                "--hook",
                "Your team owns the collection runner I use every day.",
            ],
        )
    )
    assert out.startswith("outreach 1: ")
    draft_path = Path(out.splitlines()[0].split(": ", 1)[1])
    assert draft_path.exists() and draft_path.parent.name == str(job_id)
    assert "Hi Priya" in draft_path.read_text(encoding="utf-8")
    assert "https://job-boards.greenhouse.io/postman/jobs/77" in out

    opened: list[str] = []
    monkeypatch.setattr(cli_refer.os, "startfile", opened.append, raising=False)
    out = _ok(runner.invoke(outreach_app, ["open", "1"]))
    assert opened == ["https://www.linkedin.com/in/priya-natarajan-1a2b"]
    assert "paste from" in out

    out = _ok(runner.invoke(outreach_app, ["mark-sent", "1"]))
    assert "next follow-up 20" in out
    rows = json.loads(_ok(runner.invoke(outreach_app, ["list", "--job", str(job_id), "--json"])))
    assert rows[0]["sent_at"] and rows[0]["opened_at"] and rows[0]["followup_due"]
    assert json.loads(_ok(runner.invoke(outreach_app, ["due", "--json"]))) == []

    _ok(runner.invoke(outreach_app, ["mark-replied", "1", "--response", "Done, referred you"]))
    rows = json.loads(_ok(runner.invoke(outreach_app, ["list", "--json"])))
    assert rows[0]["replied_at"] and rows[0]["response"] == "Done, referred you"
    assert rows[0]["followup_due"] is None
    assert rows[0]["referral_outcome"] == "pending"

    conn = connect(root / "data" / "jobs.db")
    kinds = [
        r[0]
        for r in conn.execute(
            "SELECT event_type FROM events WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall()
    ]
    conn.close()
    assert kinds == [
        "outreach_drafted",
        "outreach_opened",
        "outreach_sent",
        "referral_wait",
        "outreach_reply",
    ]


def test_people_search_and_unknown_template(root, job_id):
    out = _ok(runner.invoke(refer_app, ["people-search", str(job_id), "--role", "recruiter"]))
    assert out.strip().startswith(
        "https://www.linkedin.com/search/results/people/?keywords=Postman"
    )
    result = runner.invoke(
        refer_app, ["draft", str(job_id), "1", "--template", "nope", "--hook", "x"]
    )
    assert result.exit_code == cli_refer.EXIT_INVALID
    assert "not found" in result.output or "available" in result.output


def test_referral_request_parks_job_until_reply(root, job_id, monkeypatch):
    _ok(runner.invoke(refer_app, ["import-connections", str(CONNECTIONS)]))
    contact_id = json.loads(
        _ok(runner.invoke(refer_app, ["contacts", "--company", "priya", "--json"]))
    )[0]["id"]
    _ok(
        runner.invoke(
            refer_app,
            [
                "draft",
                str(job_id),
                str(contact_id),
                "--template",
                "referral_friend",
                "--hook",
                "Your team owns the collection runner I use every day.",
            ],
        )
    )

    out = _ok(runner.invoke(outreach_app, ["mark-sent", "1"]))
    assert f"job {job_id} parked until" in out
    conn = connect(root / "data" / "jobs.db")
    snooze = conn.execute("SELECT snooze_until FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
    conn.close()
    assert snooze is not None
    assert snooze[:10] == out.split("parked until ")[1][:10]

    out = _ok(runner.invoke(outreach_app, ["mark-replied", "1", "--referred"]))
    assert f"job {job_id} is back in the apply queue" in out
    conn = connect(root / "data" / "jobs.db")
    snooze = conn.execute("SELECT snooze_until FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
    kinds = [
        r[0]
        for r in conn.execute(
            "SELECT event_type FROM events WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall()
    ]
    rows = json.loads(_ok(runner.invoke(outreach_app, ["list", "--json"])))
    conn.close()
    assert snooze is None
    assert kinds[-3:] == ["referral_wait", "outreach_reply", "referral_received"]
    assert rows[0]["response"] == "referred"
    assert rows[0]["referral_outcome"] == "completed"


@pytest.fixture
def sent_referral(root, job_id):
    conn = connect(root / "data" / "jobs.db")
    with conn:
        contact_id, _ = upsert_contact(conn, ContactRecord(name="Priya", company_norm="postman"))
        outreach_id = outreach_repo.create(
            conn, contact_id=contact_id, job_id=job_id, channel="linkedin", kind="referral"
        )
    conn.close()
    _ok(runner.invoke(outreach_app, ["mark-sent", str(outreach_id)]))
    return outreach_id


def _referral_state(root, job_id):
    conn = connect(root / "data" / "jobs.db")
    job = get_job(conn, job_id)
    rows = outreach_repo.for_job(conn, job_id)
    route = recommend(job, [], rows, ReferralConfig(), now=cli_refer.now())
    events = [
        r[0]
        for r in conn.execute(
            "SELECT event_type FROM events WHERE job_id = ? ORDER BY id", (job_id,)
        )
    ]
    conn.close()
    return job, rows, route, events


@pytest.mark.parametrize("response", ["Not referred", "Referred you", "I can refer you"])
def test_ordinary_reply_keeps_job_parked_and_referral_pending(
    root, job_id, sent_referral, response
):
    _ok(runner.invoke(outreach_app, ["mark-replied", str(sent_referral), "--response", response]))
    job, rows, route, events = _referral_state(root, job_id)
    assert job["snooze_until"] is not None
    assert rows[0]["referral_outcome"] == "pending"
    assert rows[0]["response"] == response
    assert route.route == "referral_pending"
    assert "referral_received" not in events


def test_declined_reply_unparks_job_for_direct_application(root, job_id, sent_referral):
    _ok(
        runner.invoke(
            outreach_app,
            ["mark-replied", str(sent_referral), "--declined", "--response", "Not referred"],
        )
    )
    job, rows, route, events = _referral_state(root, job_id)
    assert job["snooze_until"] is None
    assert rows[0]["referral_outcome"] == "declined"
    assert route.route == "apply"
    assert "referral_received" not in events
    assert events[-1] == "outreach_reply"


def test_referral_outcome_options_are_mutually_exclusive(root, job_id, sent_referral):
    result = runner.invoke(
        outreach_app, ["mark-replied", str(sent_referral), "--referred", "--declined"]
    )
    assert result.exit_code == cli_refer.EXIT_INVALID
    assert "mutually exclusive" in result.output
    job, rows, route, events = _referral_state(root, job_id)
    assert job["snooze_until"] is not None
    assert rows[0]["replied_at"] is None
    assert route.route == "referral_pending"
    assert "outreach_reply" not in events


@pytest.mark.parametrize("outcome", ["referred", "declined"])
def test_followup_outcome_resolves_original_request(root, job_id, sent_referral, outcome):
    conn = connect(root / "data" / "jobs.db")
    with conn:
        referral = outreach_repo.get(conn, sent_referral)
        followup = outreach_repo.create(
            conn,
            contact_id=referral["contact_id"],
            job_id=job_id,
            channel="linkedin",
            kind="followup",
        )
    conn.close()
    _ok(runner.invoke(outreach_app, ["mark-sent", str(followup)]))
    _ok(
        runner.invoke(
            outreach_app,
            ["mark-replied", str(followup), f"--{outcome}", "--response", "Here is the update"],
        )
    )
    job, rows, route, events = _referral_state(root, job_id)
    assert job["snooze_until"] is None
    assert all(
        r["referral_outcome"] == ("completed" if outcome == "referred" else "declined")
        for r in rows
    )
    assert all(r["response"] == "Here is the update" for r in rows)
    assert all(r["replied_at"] is not None for r in rows)
    assert route.route == ("referral_received" if outcome == "referred" else "apply")
    assert events.count("referral_received") == (1 if outcome == "referred" else 0)


def test_repeated_referred_recording_does_not_duplicate_received_event(root, job_id, sent_referral):
    for _ in range(2):
        _ok(runner.invoke(outreach_app, ["mark-replied", str(sent_referral), "--referred"]))
    _, rows, route, events = _referral_state(root, job_id)
    assert rows[0]["referral_outcome"] == "completed"
    assert route.route == "referral_received"
    assert events.count("referral_received") == 1


def test_declining_one_contact_keeps_another_referral_parked(root, job_id, sent_referral):
    conn = connect(root / "data" / "jobs.db")
    with conn:
        contact_id, _ = upsert_contact(conn, ContactRecord(name="Arjun", company_norm="postman"))
        another = outreach_repo.create(
            conn, contact_id=contact_id, job_id=job_id, channel="linkedin", kind="referral"
        )
    conn.close()
    _ok(runner.invoke(outreach_app, ["mark-sent", str(another)]))
    _ok(runner.invoke(outreach_app, ["mark-replied", str(sent_referral), "--declined"]))
    job, _, route, events = _referral_state(root, job_id)
    assert job["snooze_until"] is not None
    assert route.route == "referral_pending"
    assert route.contact_name == "Arjun"
    assert "referral_received" not in events
    _ok(runner.invoke(outreach_app, ["mark-replied", str(another), "--declined"]))
    job, _, route, _ = _referral_state(root, job_id)
    assert job["snooze_until"] is None
    assert route.route == "apply"


@pytest.mark.parametrize("outcome", ["referred", "declined"])
def test_resolved_referral_is_not_reparked_by_mark_sent(root, job_id, sent_referral, outcome):
    _ok(runner.invoke(outreach_app, ["mark-replied", str(sent_referral), f"--{outcome}"]))
    _ok(runner.invoke(outreach_app, ["mark-sent", str(sent_referral)]))
    job, rows, _, events = _referral_state(root, job_id)
    assert job["snooze_until"] is None
    assert rows[0]["followup_due"] is None
    assert events.count("referral_wait") == 1


def test_reply_outcome_parking_and_events_roll_back_together(
    root, job_id, sent_referral, monkeypatch
):
    def fail_event(*args, **kwargs):
        raise RuntimeError("event write failed")

    monkeypatch.setattr(cli_refer.events_repo, "add_event", fail_event)
    result = runner.invoke(outreach_app, ["mark-replied", str(sent_referral), "--referred"])
    assert result.exit_code != 0
    assert isinstance(result.exception, RuntimeError)
    job, rows, route, events = _referral_state(root, job_id)
    assert job["snooze_until"] is not None
    assert rows[0]["referral_outcome"] == "pending"
    assert rows[0]["replied_at"] is None
    assert route.route == "referral_pending"
    assert "outreach_reply" not in events


@pytest.mark.parametrize("kind", ["connection_note", "followup"])
def test_referred_on_another_message_keeps_explicit_outcome_authoritative(
    root, job_id, sent_referral, kind
):
    conn = connect(root / "data" / "jobs.db")
    with conn:
        contact_id, _ = upsert_contact(conn, ContactRecord(name="Arjun", company_norm="postman"))
        outreach_id = outreach_repo.create(
            conn, contact_id=contact_id, job_id=job_id, channel="linkedin", kind=kind
        )
    conn.close()
    _ok(runner.invoke(outreach_app, ["mark-replied", str(outreach_id), "--referred"]))
    job, rows, route, events = _referral_state(root, job_id)
    assert job["snooze_until"] is None
    assert rows[0]["referral_outcome"] == "pending"
    assert rows[1]["referral_outcome"] == "completed"
    assert route.route == "referral_received"
    assert route.contact_name == "Arjun"
    assert events.count("referral_received") == 1


def test_connection_note_does_not_park_the_job(root, job_id):
    _ok(runner.invoke(refer_app, ["import-connections", str(CONNECTIONS)]))
    contact_id = json.loads(
        _ok(runner.invoke(refer_app, ["contacts", "--company", "priya", "--json"]))
    )[0]["id"]
    _ok(
        runner.invoke(
            refer_app,
            [
                "draft",
                str(job_id),
                str(contact_id),
                "--template",
                "connection_note_recruiter",
                "--hook",
                "Saw your post on the collection runner.",
            ],
        )
    )
    out = _ok(runner.invoke(outreach_app, ["mark-sent", "1"]))
    assert "parked" not in out
    conn = connect(root / "data" / "jobs.db")
    snooze = conn.execute("SELECT snooze_until FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
    conn.close()
    assert snooze is None
