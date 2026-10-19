"""Helpers shared by the mail tests. Not a test module (pytest only collects test_*.py)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from jobhunt.config import CompanyConfig
from jobhunt.mail.models import MailMessage
from jobhunt.mail.rules import load_mail_rules
from jobhunt.models import Status
from jobhunt.repo.jobs import set_job_status, upsert_job

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
EMAILS_DIR = ROOT / "tests" / "fixtures" / "emails"
# Later messages of threads whose first message lives in EMAILS_DIR; kept apart so the
# whole-inbox tests keep their counts.
THREADS_DIR = EMAILS_DIR / "threads"
RULES = load_mail_rules(CONFIG_DIR)

COMPANIES = [
    CompanyConfig(name="Postman", ats="greenhouse", token="postman", domains=["postman.com"]),
    CompanyConfig(name="Stripe", ats="greenhouse", token="stripe", domains=["stripe.com"]),
    CompanyConfig(name="Figma", ats="greenhouse", token="figma", domains=["figma.com"]),
    CompanyConfig(name="Coinbase", ats="greenhouse", token="coinbase", domains=["coinbase.com"]),
    CompanyConfig(name="Spotify", ats="lever", token="spotify", domains=["spotify.com"]),
    CompanyConfig(name="Linear", ats="ashby", token="linear", domains=["linear.app"]),
    CompanyConfig(name="Ramp", ats="ashby", token="ramp", domains=["ramp.com"]),
    CompanyConfig(
        name="Cursor", ats="ashby", token="cursor", aliases=["anysphere"], domains=["cursor.com"]
    ),
    CompanyConfig(
        name="Salesforce", ats="workday", tenant="salesforce", domains=["salesforce.com"]
    ),
]


def load_email(name: str) -> MailMessage:
    data = json.loads((EMAILS_DIR / f"{name}.json").read_text(encoding="utf-8"))
    return MailMessage.from_dict(data)


def email_dict(name: str) -> dict:
    return json.loads((EMAILS_DIR / f"{name}.json").read_text(encoding="utf-8"))


def stage(inbox: Path, *names: str) -> list[Path]:
    """Copy fixtures (from emails/ or emails/threads/) into `inbox` under the contract name."""
    staged: list[Path] = []
    for name in names:
        source = EMAILS_DIR / f"{name}.json"
        if not source.exists():
            source = THREADS_DIR / f"{name}.json"
        data = json.loads(source.read_text(encoding="utf-8"))
        target = inbox / f"{data['thread_id']}_{data['gmail_id']}.json"
        target.write_text(json.dumps(data), encoding="utf-8")
        staged.append(target)
    return staged


def seed(
    conn: sqlite3.Connection,
    norm,
    priority,
    make_posting,
    *,
    company: str,
    title: str,
    status: Status = Status.APPLIED,
    sid: str = "1",
    at: str | None = None,
) -> int:
    """Insert one job and force it to `status`; returns the job id."""
    slug = company.lower().replace(" ", "-")
    posting = make_posting(
        company=company,
        title=title,
        source_job_id=f"{slug}-{sid}",
        url=f"https://boards.greenhouse.io/{slug}/jobs/{sid}",
        apply_url=None,
    )
    job_id = upsert_job(conn, posting, norm, priority).job_id
    if status != Status.DISCOVERED:
        set_job_status(conn, job_id, status, force=True, at=at)
    return job_id
