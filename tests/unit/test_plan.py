"""build_plan groups jobs into ready / referral / needs-input and reports health."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobhunt import plan
from jobhunt.config import Settings
from jobhunt.models import Status
from jobhunt.repo import outreach as outreach_repo
from jobhunt.repo.contacts import ContactRecord, upsert_contact
from jobhunt.repo.jobs import apply_score, set_job_status, set_prefill_state, set_snooze, upsert_job

CONFIG_DIR = Path(__file__).parent.parent.parent / "config"


def test_eligibility_review_is_separate_from_shortlist(
    conn, norm, priority, make_posting, settings
):
    from jobhunt.config import load_profile, load_skill_aliases
    from jobhunt.dashboard import collect
    from jobhunt.match import apply_prefilter

    job_id = upsert_job(
        conn, make_posting(location="Remote", is_remote=True), norm, priority
    ).job_id
    apply_prefilter(conn, load_profile(settings), load_skill_aliases(settings))
    result = plan.build_plan(conn, settings)
    assert [i["job_id"] for i in result["needs_input"]["eligibility_review"]] == [job_id]
    assert result["needs_input"]["to_review"] == []
    # It is not a match yet, but it must still be visible: Today's verification queue and the
    # All jobs table have to draw on the same rows or their counts contradict each other.
    assert [r["id"] for r in collect(conn, settings)["rows"]] == [job_id]
    assert "Eligibility verification (1)" in plan.render_markdown(result)


def test_pending_overseas_hydration_is_not_displayed_as_a_match(
    conn, norm, priority, make_posting, settings
):
    from jobhunt.dashboard import collect

    upsert_job(
        conn,
        make_posting(location="London, UK", description_md=None, needs_hydration=True),
        norm,
        priority,
    )
    assert collect(conn, settings)["rows"] == []


def test_source_health_uses_latest_run_and_does_not_count_unfinished(conn, settings):
    from jobhunt.cli_ops import _data_checks
    from jobhunt.clock import now, now_iso

    stamp = now_iso()
    with conn:
        conn.execute(
            "INSERT INTO scan_runs(source, company, started_at, finished_at, error) "
            "VALUES ('greenhouse', 'Example', ?, ?, 'old failure')",
            (stamp, stamp),
        )
        conn.execute(
            "INSERT INTO scan_runs(source, company, started_at, finished_at) "
            "VALUES ('greenhouse', 'Example', ?, ?)",
            (stamp, stamp),
        )
        conn.execute(
            "INSERT INTO scan_runs(source, company, started_at) VALUES ('lever', 'Pending', ?)",
            (stamp,),
        )
    health = plan._health(conn, settings, now())
    assert health["sources_ok"] == 1
    assert health["sources_failed"] == []
    assert len(health["sources_running"]) == 1
    assert next(c for c in _data_checks(conn) if c.name == "sources failing").ok


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    (tmp_path / "data" / "mail_inbox").mkdir(parents=True)
    return Settings(
        root=tmp_path,
        config_dir=CONFIG_DIR,
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        db_path=tmp_path / "data" / "jobs.db",
        raw={
            "paths": {
                "applications_dir": "data/applications",
                "mail_inbox_dir": "data/mail_inbox",
            },
            "follow_up": {"days_after_apply": 10},
        },
    )


def _seed(conn, norm, priority, make_posting, n: int, status: Status, score: int = 80) -> int:
    posting = make_posting(
        source_job_id=str(n), url=f"https://boards.greenhouse.io/acme/jobs/{n}", company=f"Co{n}"
    )
    job_id = upsert_job(conn, posting, norm, priority).job_id
    apply_score(conn, job_id, score, {"reasons": ["fit"]})
    set_job_status(conn, job_id, status, force=True)
    return job_id


def test_plan_groups_jobs(conn, norm, priority, make_posting, settings):
    ready = _seed(conn, norm, priority, make_posting, 1, Status.APPROVED)
    set_prefill_state(conn, ready, "ready")
    unanswered = _seed(conn, norm, priority, make_posting, 2, Status.APPROVED)
    set_prefill_state(conn, unanswered, "schema")
    app_dir = settings.path("applications_dir") / str(unanswered)
    app_dir.mkdir(parents=True)
    (app_dir / "answers.json").write_text(
        json.dumps({"unmatched": [{"label": "Why us?", "required": True}]}), encoding="utf-8"
    )
    blocked = _seed(conn, norm, priority, make_posting, 3, Status.APPROVED)
    set_prefill_state(conn, blocked, "blocked")
    to_review = _seed(conn, norm, priority, make_posting, 4, Status.SHORTLISTED)
    referral = _seed(conn, norm, priority, make_posting, 5, Status.APPROVED)
    set_prefill_state(conn, referral, "ready")
    pending = _seed(conn, norm, priority, make_posting, 6, Status.APPROVED)
    set_snooze(conn, pending, "2999-01-01T00:00:00+00:00")

    routes = {
        referral: {"route": "referral_first", "route_reason": "ask Priya", "referral_count": 1},
        pending: {"route": "referral_pending", "route_reason": "waiting", "referral_count": 1},
    }

    def enrich(row):
        return dict(routes.get(row["id"], {"route": "apply", "route_reason": ""}))

    result = plan.build_plan(conn, settings, enrich)

    assert [i["job_id"] for i in result["ready_to_apply"]] == [ready]
    assert [i["job_id"] for i in result["referral_recommended"]] == [referral]
    assert [i["job_id"] for i in result["awaiting_referral"]] == [pending]
    needs = result["needs_input"]
    assert [i["job_id"] for i in needs["to_review"]] == [to_review]
    assert needs["unanswered_questions"][0]["job_id"] == unanswered
    assert needs["unanswered_questions"][0]["questions"] == ["Why us?"]
    assert [i["job_id"] for i in needs["blocked"]] == [blocked]
    assert needs["mail_review"] == []
    assert result["progress"]["in_process"] == {"screening": 0, "interview": 0, "offer": 0}


def test_approved_unprepared_jobs_have_a_preparation_group(
    conn, norm, priority, make_posting, settings
):
    job_id = _seed(conn, norm, priority, make_posting, 1, Status.APPROVED)
    result = plan.build_plan(conn, settings)
    assert [i["job_id"] for i in result["needs_input"]["to_prepare"]] == [job_id]
    assert result["ready_to_apply"] == []
    assert "## Needs preparation (1)" in plan.render_markdown(result)


@pytest.mark.parametrize("state", ["none", "schema", "ready", "blocked"])
@pytest.mark.parametrize("route", [None, "apply", "referral_first", "referral_deadline"])
@pytest.mark.parametrize("status", [Status.APPROVED, Status.SHORTLISTED])
def test_snoozed_jobs_are_never_actionable(
    conn, norm, priority, make_posting, settings, state, route, status
):
    job_id = _seed(conn, norm, priority, make_posting, 1, status)
    set_prefill_state(conn, job_id, state)
    set_snooze(conn, job_id, "2999-01-01T00:00:00+00:00")
    enrich = (lambda row: {"route": route}) if route else None
    result = plan.build_plan(conn, settings, enrich)
    groups = [result["ready_to_apply"], result["referral_recommended"]]
    groups.extend(result["needs_input"].values())
    assert all(not group for group in groups)


@pytest.mark.parametrize("state", ["none", "schema", "ready", "blocked"])
@pytest.mark.parametrize("status", [Status.APPROVED, Status.SHORTLISTED])
@pytest.mark.parametrize("route", ["referral_first", "referral_pending", "referral_deadline"])
def test_referral_groups_do_not_duplicate_review_or_application_work(
    conn, norm, priority, make_posting, settings, state, status, route
):
    job_id = _seed(conn, norm, priority, make_posting, 1, status)
    set_prefill_state(conn, job_id, state)
    result = plan.build_plan(conn, settings, lambda row: {"route": route})
    groups = [result["ready_to_apply"], result["referral_recommended"], result["awaiting_referral"]]
    groups.extend(result["needs_input"].values())
    assert sum(item["job_id"] == job_id for group in groups for item in group) == 1


def test_existing_referral_wait_is_visible_without_suggestions(
    conn, norm, priority, make_posting, settings
):
    job_id = _seed(conn, norm, priority, make_posting, 1, Status.APPROVED)
    set_prefill_state(conn, job_id, "ready")
    set_snooze(conn, job_id, "2999-01-01T00:00:00+00:00")
    with conn:
        contact_id, _ = upsert_contact(conn, ContactRecord(name="Priya", company_norm="co1"))
        outreach_id = outreach_repo.create(
            conn, contact_id=contact_id, job_id=job_id, channel="linkedin", kind="referral"
        )
        outreach_repo.mark_sent(conn, outreach_id, "2026-01-01T00:00:00+00:00", 7)
    result = plan.build_plan(conn, settings)
    assert [i["job_id"] for i in result["awaiting_referral"]] == [job_id]
    assert result["awaiting_referral"][0]["wait_until"] == "2999-01-01T00:00:00+00:00"
    assert result["ready_to_apply"] == []
    assert result["referral_recommended"] == []


def test_plan_health_reports_failed_sources_and_stale_scan(conn, settings):
    conn.execute(
        "INSERT INTO scan_runs (source, company, started_at, finished_at, error) "
        "VALUES ('greenhouse', 'Acme', '2026-01-01T00:00:00+00:00', '2026-01-01T00:01:00+00:00', "
        "'boom')"
    )
    conn.execute(
        "INSERT INTO scan_runs (source, company, started_at, finished_at) "
        "VALUES ('hn', NULL, '2026-01-01T00:00:00+00:00', '2026-01-01T00:01:00+00:00')"
    )
    conn.commit()
    (settings.data_dir / plan.LAST_SCAN_FILE).write_text(
        json.dumps({"ok": False, "failed": ["claude_scan"]}), encoding="utf-8"
    )
    health = plan.build_plan(conn, settings)["health"]
    assert health["scan_stale"] is True
    assert health["sources_ok"] == 1
    assert health["sources_failed"] == [
        {"source": "greenhouse", "company": "Acme", "error": "boom"}
    ]
    assert health["last_mail_sync_at"] is None
    assert health["last_scheduled_run"]["failed"] == ["claude_scan"]


def test_render_markdown_mentions_groups_and_failures(conn, settings):
    conn.execute(
        "INSERT INTO scan_runs (source, company, started_at, error) "
        "VALUES ('lever', 'CRED', '2026-01-01T00:00:00+00:00', 'HTTP 404')"
    )
    conn.commit()
    text = plan.render_markdown(plan.build_plan(conn, settings))
    assert "# Today" in text
    assert "FAILED lever CRED: HTTP 404" in text
    assert "## Ready to apply (0)" in text
    assert "## Referral recommended (0)" in text
    assert "## Progress" in text
