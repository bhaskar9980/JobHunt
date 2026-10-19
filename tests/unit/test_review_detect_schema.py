from datetime import date
from pathlib import Path

import pytest

from jobhunt.apply.detect import AtsRegistry
from jobhunt.apply.schema import FieldKind, FormField, FormSchema
from jobhunt.models import Status
from jobhunt.repo.jobs import apply_score, current_status, get_job, set_job_status, upsert_job
from jobhunt.review import Decision, decide, parse_markdown, queue, render_markdown

CONFIG_DIR = Path(__file__).parent.parent.parent / "config"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://boards.greenhouse.io/acme/jobs/123", "greenhouse"),
        ("https://job-boards.greenhouse.io/acme/jobs/123", "greenhouse"),
        ("https://acme.com/careers?gh_jid=123", "greenhouse"),
        ("https://jobs.lever.co/acme/uuid/apply", "lever"),
        ("https://jobs.ashbyhq.com/acme/uuid/application", "ashby"),
        ("https://acme.wd5.myworkdayjobs.com/en-US/careers/job/x", "workday"),
        ("https://www.linkedin.com/jobs/view/123", "linkedin"),
        ("https://www.naukri.com/job-listings-x-123", "naukri"),
        ("https://example.com/apply", None),
    ],
)
def test_detect(url, expected):
    registry = AtsRegistry.load(CONFIG_DIR)
    assert registry.detect(url) == expected


def test_detect_fallback_and_modes():
    registry = AtsRegistry.load(CONFIG_DIR)
    assert registry.detect("https://example.com", fallback="Lever") == "lever"
    assert registry.mode_for("greenhouse") == "python"
    # Workday has a filler, but its Next and Submit share one automation id, so it stays
    # assisted until the page walk has been proven against a real form.
    assert registry.mode_for("workday") == "assisted"
    # SmartRecruiters serves a bot-detection page instead of its apply form, so it stays assisted.
    assert registry.mode_for("smartrecruiters") == "assisted"
    assert registry.mode_for("linkedin") == "manual"
    assert registry.mode_for(None) == "unsupported"


def test_form_schema_roundtrip_and_diff(tmp_path):
    schema = FormSchema(
        ats="greenhouse",
        url="https://boards.greenhouse.io/acme/jobs/1",
        fields=[
            FormField("first_name", "First Name", FieldKind.TEXT, True, system=True),
            FormField("question_1", "Notice period", FieldKind.SELECT, True, ["30", "60"]),
        ],
    )
    path = tmp_path / "form_schema.json"
    schema.save(path)
    loaded = FormSchema.load(path)
    assert loaded.to_dict() == schema.to_dict()
    assert [f.key for f in loaded.custom_fields()] == ["question_1"]
    later = FormSchema(
        ats="greenhouse",
        url=schema.url,
        fields=[*schema.fields, FormField("question_2", "Why us?", FieldKind.TEXTAREA)],
    )
    assert [f.key for f in schema.diff_new(later)] == ["question_2"]


@pytest.fixture
def shortlisted(conn, norm, priority, make_posting):
    ids = []
    for n, score in enumerate((92, 75, 71)):
        job_id = upsert_job(
            conn,
            make_posting(source_job_id=str(n), url=f"https://boards.greenhouse.io/acme/jobs/{n}"),
            norm,
            priority,
        ).job_id
        apply_score(
            conn,
            job_id,
            score,
            {"reasons": ["stack fit"], "red_flags": ["on-call"] if n == 1 else []},
        )
        set_job_status(conn, job_id, Status.SHORTLISTED, force=True)
        ids.append(job_id)
    return ids


def test_queue_orders_by_score_and_hides_snoozed(conn, shortlisted):
    items = queue(conn, ["shortlisted"], limit=10)
    assert [i["job_id"] for i in items] == shortlisted
    assert items[0]["score"] == 92
    assert items[1]["red_flags"] == ["on-call"]
    decide(conn, Decision(shortlisted[0], "snooze", date(2999, 1, 1)))
    items = queue(conn, ["shortlisted"], limit=10)
    assert [i["job_id"] for i in items] == shortlisted[1:]
    assert queue(conn, ["shortlisted"], limit=1)[0]["job_id"] == shortlisted[1]


def test_decisions_change_status(conn, shortlisted):
    assert decide(conn, Decision(shortlisted[0], "approve")) == Status.APPROVED
    assert decide(conn, Decision(shortlisted[1], "skip")) == Status.DISMISSED
    assert current_status(conn, shortlisted[2]) == Status.SHORTLISTED
    with pytest.raises(ValueError):
        decide(conn, Decision(shortlisted[2], "maybe"))


def test_markdown_roundtrip(conn, shortlisted):
    text = render_markdown(queue(conn, ["shortlisted"], limit=10))
    assert f"#{shortlisted[0]}" in text
    ticked = text.replace(
        f"- [ ] approve | [ ] skip | [ ] snooze:  #{shortlisted[0]}",
        f"- [x] approve | [ ] skip | [ ] snooze:  #{shortlisted[0]}",
    ).replace(
        f"- [ ] approve | [ ] skip | [ ] snooze:  #{shortlisted[1]}",
        f"- [ ] approve | [ ] skip | [x] snooze:2999-01-01  #{shortlisted[1]}",
    )
    decisions = parse_markdown(ticked)
    assert [(d.job_id, d.action) for d in decisions] == [
        (shortlisted[0], "approve"),
        (shortlisted[1], "snooze"),
    ]
    assert decisions[1].until == date(2999, 1, 1)
    for decision in decisions:
        decide(conn, decision)
    assert current_status(conn, shortlisted[0]) == Status.APPROVED
    assert get_job(conn, shortlisted[1])["snooze_until"].startswith("2999-01-01")


def test_queue_enrich_adds_referrals_and_route(conn, shortlisted):
    extras = {
        shortlisted[0]: {
            "referral_count": 2,
            "referral_top": ["Priya", "Arjun"],
            "route": "referral_first",
            "route_reason": "Priya scores 80; ask before applying",
        },
        shortlisted[1]: {"referral_count": 1, "referral_top": ["Arjun"]},
    }
    items = queue(
        conn, ["shortlisted"], limit=10, enrich=lambda row: dict(extras.get(row["id"], {}))
    )
    assert items[0]["referral_count"] == 2 and items[0]["route"] == "referral_first"
    assert items[1]["referral_count"] == 1 and items[1]["route"] == "apply"
    assert items[2]["referral_count"] == 0 and items[2]["route"] == "apply"

    text = render_markdown(items)
    assert "route: referral_first (Priya scores 80; ask before applying)" in text
    assert "contacts: 1 (Arjun)" in text


def test_queue_can_include_snoozed_items(conn, shortlisted):
    decide(conn, Decision(shortlisted[0], "snooze", date(2999, 1, 1)))
    hidden = queue(conn, ["shortlisted"], limit=10)
    shown = queue(conn, ["shortlisted"], limit=10, include_snoozed=True)
    assert shortlisted[0] not in [i["job_id"] for i in hidden]
    parked = next(i for i in shown if i["job_id"] == shortlisted[0])
    assert parked["snoozed"] is True
    assert all(i["snoozed"] is False for i in hidden)
