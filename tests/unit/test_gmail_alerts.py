import json
import shutil
from pathlib import Path

import pytest
import yaml

from jobhunt.repo.jobs import get_job, sources_for_job, upsert_job
from jobhunt.sources.gmail_alerts import (
    ALLOWED_ALERT_SOURCES,
    ingest_inbox,
    source_job_id_for,
    to_posting,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "inbox"


@pytest.fixture
def inbox(tmp_path: Path) -> Path:
    target = tmp_path / "inbox"
    target.mkdir()
    for name in ("linkedin_ok.json", "naukri_ok.json", "malformed.json"):
        shutil.copy(FIXTURES / name, target / name)
    return target


def test_ingest_creates_jobs_and_moves_files(conn, norm, priority, inbox):
    summary = ingest_inbox(conn, inbox, norm, priority)
    assert summary.files == 3
    assert summary.emails == 2
    assert summary.jobs == 3  # the card without a URL is dropped
    assert summary.created == 3
    assert len(summary.failed) == 1
    assert sorted(p.name for p in (inbox / "processed").iterdir()) == [
        "linkedin_ok.json",
        "naukri_ok.json",
    ]
    assert [p.name for p in (inbox / "failed").iterdir()] == ["malformed.json"]
    assert list(inbox.glob("*.json")) == []
    rows = conn.execute(
        "SELECT gmail_id, source, jobs_found FROM email_ingest ORDER BY gmail_id"
    ).fetchall()
    assert [(r["gmail_id"], r["source"], r["jobs_found"]) for r in rows] == [
        ("alert-message:18f3c2a1b2c3d4e5", "linkedin_email", 2),
        ("alert-message:18f3c2a1deadbeef", "naukri_email", 1),
    ]


def test_ingest_is_idempotent_per_email(conn, norm, priority, inbox):
    ingest_inbox(conn, inbox, norm, priority)
    shutil.copy(FIXTURES / "linkedin_ok.json", inbox / "again.json")
    summary = ingest_inbox(conn, inbox, norm, priority)
    assert summary.skipped_emails == 1
    assert summary.created == 0
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3


def test_linkedin_card_merges_with_ats_posting(conn, norm, priority, inbox, make_posting):
    ats_id = upsert_job(conn, make_posting(), norm, priority).job_id
    ingest_inbox(conn, inbox, norm, priority)
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3
    sources = [s["source"] for s in sources_for_job(conn, ats_id)]
    assert sources == ["greenhouse", "linkedin_email"]
    row = get_job(conn, ats_id)
    assert row["primary_source"] == "greenhouse"
    assert row["description_md"] == "Build .NET microservices with MongoDB and Kafka."


@pytest.mark.parametrize("mail_first", [False, True])
def test_alert_and_status_mail_have_independent_deduplication(conn, norm, priority, mail_first):
    from jobhunt.clock import now_iso
    from jobhunt.mail import ingest as mail
    from jobhunt.sources import gmail_alerts as alerts

    def status_mail():
        mail._record_ingest(conn, "shared-message", "confirmation", True, now_iso())

    def alert_mail():
        alerts._ingest_one(
            conn,
            {"email": {"gmail_id": "shared-message", "alert_source": "linkedin"}, "jobs": []},
            norm,
            priority,
            alerts.IngestSummary(),
        )

    first, second = (status_mail, alert_mail) if mail_first else (alert_mail, status_mail)
    first()
    other = alerts if mail_first else mail
    assert not other._already_ingested(conn, "shared-message")
    second()
    assert alerts._already_ingested(conn, "shared-message")
    assert mail._already_ingested(conn, "shared-message")


def test_malformed_card_is_quarantined_without_crashing(conn, norm, priority, tmp_path):
    payload = {
        "schema_version": 1,
        "email": {"gmail_id": "bad-card", "alert_source": "linkedin"},
        "jobs": ["not a job object"],
    }
    (tmp_path / "bad.json").write_text(json.dumps(payload))
    result = ingest_inbox(conn, tmp_path, norm, priority)
    assert len(result.failed) == 1
    assert result.created == 0


def test_source_ids():
    assert (
        source_job_id_for(
            "linkedin",
            "https://www.linkedin.com/jobs/view/software-engineer-ii-at-acme-4012345678/?trackingId=abc",
        )
        == "4012345678"
    )
    assert (
        source_job_id_for(
            "naukri",
            "https://www.naukri.com/job-listings-dot-net-developer-090926012345?src=jobalert",
        )
        == "090926012345"
    )
    fallback = source_job_id_for("instahyre", "https://www.instahyre.com/job-123/")
    assert len(fallback) == 16
    assert fallback == source_job_id_for(
        "instahyre", "https://www.instahyre.com/job-123?utm_source=x"
    )


def test_to_posting_fields():
    payload = json.loads((FIXTURES / "linkedin_ok.json").read_text(encoding="utf-8"))
    posting = to_posting(payload["jobs"][1], payload["email"])
    assert posting is not None
    assert posting.source == "linkedin_email"
    assert posting.source_job_id == "4098765432"
    assert posting.is_remote is True
    assert posting.salary_text == "INR 25-35 LPA"
    assert posting.raw["posted_text"] == "1 day ago"
    assert to_posting(payload["jobs"][2], payload["email"]) is None


@pytest.mark.parametrize(
    ("source", "url", "expected"),
    [
        ("indeed", "https://in.indeed.com/rc/clk?jk=9f8e7d6c5b4a3210&from=ja", "9f8e7d6c5b4a3210"),
        ("indeed", "https://in.indeed.com/viewjob?jk=9F8E7D6C5B4A3210&tk=1", "9f8e7d6c5b4a3210"),
        (
            "foundit",
            "https://www.foundit.in/job/software-engineer-acme-bengaluru-34567890?utm=x",
            "34567890",
        ),
        (
            "hirist",
            "https://www.hirist.tech/j/acme-dotnet-developer-3-6-yrs-1543210.html?ref=alert",
            "1543210",
        ),
        (
            "hirist",
            "https://www.hirist.tech/one-click-apply?jobId=1673986&jsAuth=abc",
            "1673986",
        ),
        (
            "ziprecruiter",
            "https://www.ziprecruiter.com/c/Acme/Job/Software-Engineer/-in-Austin,TX?jid=ab12cd34ef56",
            "ab12cd34ef56",
        ),
        (
            "wttj",
            "https://app.welcometothejungle.com/jobs/software-engineer-acme-chennai?ref=email",
            "software-engineer-acme-chennai",
        ),
    ],
)
def test_source_job_id_for_new_boards(source, url, expected):
    assert source_job_id_for(source, url) == expected


def test_same_indeed_job_from_two_click_links_dedupes(conn, norm, priority):
    email = {"alert_source": "indeed", "gmail_id": "g1", "subject": "s"}
    first = to_posting(
        {
            "title": "SE",
            "company": "Acme",
            "url": "https://in.indeed.com/rc/clk?jk=abcdef123456&a=1",
        },
        email,
    )
    second = to_posting(
        {
            "title": "SE",
            "company": "Acme",
            "url": "https://in.indeed.com/viewjob?jk=abcdef123456&b=2",
        },
        email,
    )
    assert first is not None and second is not None
    assert first.source_job_id == second.source_job_id == "abcdef123456"
    upsert_job(conn, first, norm, priority)
    upsert_job(conn, second, norm, priority)
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_every_configured_alert_sender_is_an_allowed_source():
    config = yaml.safe_load(
        (FIXTURES.parent.parent.parent / "config" / "sources.yaml").read_text(encoding="utf-8")
    )
    senders = config["gmail_alerts"]["senders"]
    assert set(senders) <= ALLOWED_ALERT_SOURCES
