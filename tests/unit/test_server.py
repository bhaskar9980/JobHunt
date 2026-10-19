"""The localhost dashboard API: buttons must really change the database, and only with the token."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest

from jobhunt import server
from jobhunt.config import Settings
from jobhunt.db import connect, init_db
from jobhunt.models import JobPosting, Status
from jobhunt.normalize import Normalizer
from jobhunt.repo.contacts import ContactRecord, upsert_contact
from jobhunt.repo.events import events_for_job
from jobhunt.repo.jobs import apply_score, current_status, get_job, set_job_status, upsert_job

CONFIG_DIR = Path(__file__).parent.parent.parent / "config"
TOKEN = "test-token-abc"
NORM = Normalizer(company_suffixes=frozenset({"inc"}), location_aliases={})


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
                "dashboard_path": "data/dashboard.html",
            },
            "follow_up": {"days_after_apply": 10},
            "dashboard": {"title": "JobHunt"},
        },
    )


@pytest.fixture
def job_id(settings: Settings) -> int:
    conn = connect(settings.db_path)
    init_db(conn)
    posting = JobPosting(
        source="greenhouse",
        source_job_id="77",
        company="Acme, Inc.",
        title="Software Engineer II",
        url="https://boards.greenhouse.io/acme/jobs/77",
        location="Bengaluru, India",
        description_md="Build .NET services",
    )
    with conn:
        new_id = upsert_job(conn, posting, NORM, {"default": 10}).job_id
        apply_score(conn, new_id, 82, {"reasons": ["stack fit"], "red_flags": []})
        set_job_status(conn, new_id, Status.SHORTLISTED, force=True)
    conn.close()
    return new_id


@pytest.fixture
def client(settings: Settings, job_id: int) -> Iterator[httpx.Client]:
    srv, url = server.start(settings, port=0, token=TOKEN)
    base = f"http://{urlparse(url).netloc}"
    try:
        with httpx.Client(base_url=base, timeout=10) as http:
            yield http
    finally:
        srv.shutdown()
        srv.server_close()


def test_page_is_a_private_data_free_shell_and_api_requires_token(client):
    assert client.get("/").status_code == 200
    assert client.get("/api/state").status_code == 403

    page = client.get("/", params={"t": TOKEN})

    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert TOKEN not in page.text
    assert 'id="decide"' in page.text and 'id="detail"' in page.text
    assert page.headers["referrer-policy"] == "no-referrer"


def test_state_lists_the_job_awaiting_a_decision(client, job_id):
    payload = client.get("/api/state", headers={server.TOKEN_HEADER: TOKEN}).json()

    assert [i["job_id"] for i in payload["plan"]["needs_input"]["to_review"]] == [job_id]
    assert [r["id"] for r in payload["rows"]] == [job_id]
    assert any(k["label"] == "Shortlisted" and k["value"] == 1 for k in payload["kpis"])


@pytest.mark.parametrize(
    ("action", "expected"),
    [("approve", Status.APPROVED), ("skip", Status.DISMISSED)],
)
def test_buttons_change_the_database(client, settings, job_id, action, expected):
    result = client.post(
        "/api/decide",
        json={"job_id": job_id, "action": action},
        headers={server.TOKEN_HEADER: TOKEN},
    )

    assert result.status_code == 200, result.text
    assert result.json() == {"job_id": job_id, "action": action, "status": expected.value}
    conn = connect(settings.db_path)
    assert current_status(conn, job_id) == expected
    conn.close()


def test_snooze_is_not_a_dashboard_decision(client, settings, job_id):
    result = client.post(
        "/api/decide",
        json={"job_id": job_id, "action": "snooze", "until": "2027-03-04"},
        headers={server.TOKEN_HEADER: TOKEN},
    )

    assert result.status_code == 400
    conn = connect(settings.db_path)
    assert current_status(conn, job_id) == Status.SHORTLISTED
    assert get_job(conn, job_id)["snooze_until"] is None
    conn.close()


def test_decisions_are_rejected_without_the_token(client, settings, job_id):
    result = client.post("/api/decide", json={"job_id": job_id, "action": "approve"})
    assert result.status_code == 403
    conn = connect(settings.db_path)
    assert current_status(conn, job_id) == Status.SHORTLISTED
    conn.close()


@pytest.mark.parametrize(
    ("payload", "status"),
    [
        ({"job_id": 999, "action": "approve"}, 404),
        ({"job_id": 1, "action": "delete"}, 400),
        ({"action": "approve"}, 400),
        ({"job_id": 1, "action": "snooze", "until": "next week"}, 400),
    ],
)
def test_bad_requests_answer_with_json_not_a_crash(client, payload, status):
    result = client.post("/api/decide", json=payload, headers={server.TOKEN_HEADER: TOKEN})
    assert result.status_code == status
    assert "error" in result.json()


def test_notes_are_recorded(client, settings, job_id):
    result = client.post(
        "/api/note",
        json={"job_id": job_id, "note": "asked Priya for a referral"},
        headers={server.TOKEN_HEADER: TOKEN},
    )
    assert result.status_code == 200
    conn = connect(settings.db_path)
    notes = [e["note"] for e in events_for_job(conn, job_id) if e["event_type"] == "note"]
    conn.close()
    assert "asked Priya for a referral" in notes


def test_referrals_endpoint_ranks_contacts_at_that_company(client, settings, job_id):
    conn = connect(settings.db_path)
    with conn:
        upsert_contact(
            conn,
            ContactRecord(
                name="Priya Natarajan",
                company="Acme",
                company_norm="acme",
                title="Software Engineer",
                linkedin_url="https://www.linkedin.com/in/priya",
            ),
        )
    conn.close()

    payload = client.get(
        "/api/referrals", params={"job_id": job_id}, headers={server.TOKEN_HEADER: TOKEN}
    ).json()

    assert [c["name"] for c in payload["contacts"]] == ["Priya Natarajan"]
    assert payload["contacts"][0]["linkedin_url"] == "https://www.linkedin.com/in/priya"
    assert payload["contacts"][0]["score"] > 0


def test_unknown_routes_and_bodies_are_handled(client):
    assert client.get("/nope", headers={server.TOKEN_HEADER: TOKEN}).status_code == 404
    assert (
        client.post("/api/nope", json={}, headers={server.TOKEN_HEADER: TOKEN}).status_code == 404
    )
    broken = client.post(
        "/api/decide",
        content=b"not json",
        headers={server.TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
    )
    assert broken.status_code == 400


def test_live_state_refreshes_every_dashboard_section(client):
    state = client.get("/api/state", headers={server.TOKEN_HEADER: TOKEN})
    assert state.status_code == 200
    assert {"rows", "kanban", "follow_ups", "plan", "workflow"} <= state.json().keys()


def test_job_drawer_returns_history_and_valid_actions(client, job_id):
    client.post(
        "/api/note",
        json={"job_id": job_id, "note": "Review this role"},
        headers={server.TOKEN_HEADER: TOKEN},
    )
    response = client.get(
        "/api/job", params={"job_id": job_id}, headers={server.TOKEN_HEADER: TOKEN}
    )
    assert response.status_code == 200
    job = response.json()
    assert job["description_md"] == "Build .NET services"
    assert job["allowed_decisions"] == ["approve", "skip"]
    assert any(e["note"] == "Review this role" for e in job["events"])


def test_stale_review_does_not_overwrite_newer_decision(client, settings, job_id):
    conn = connect(settings.db_path)
    set_job_status(conn, job_id, Status.APPROVED)
    conn.close()
    response = client.post(
        "/api/decide",
        json={"job_id": job_id, "action": "skip", "expected_status": "shortlisted"},
        headers={server.TOKEN_HEADER: TOKEN},
    )
    assert response.status_code == 409
    conn = connect(settings.db_path)
    assert current_status(conn, job_id) == Status.APPROVED
    conn.close()


def test_batch_reports_each_result_without_claiming_full_success(client, settings, job_id):
    response = client.post(
        "/api/decisions",
        json={
            "action": "approve",
            "jobs": [
                {"job_id": job_id, "expected_status": "shortlisted"},
                {"job_id": 999, "expected_status": "shortlisted"},
            ],
        },
        headers={server.TOKEN_HEADER: TOKEN},
    )
    assert response.status_code == 200
    results = response.json()["results"]
    assert results[0]["ok"] is True
    assert results[1]["ok"] is False
    assert results[1]["code"] == 404


def test_status_update_never_submits_or_bypasses_transitions(client, settings, job_id):
    response = client.post(
        "/api/status",
        json={"job_id": job_id, "status": "applied", "note": "not an actual submission"},
        headers={server.TOKEN_HEADER: TOKEN},
    )
    assert response.status_code == 400
    conn = connect(settings.db_path)
    assert current_status(conn, job_id) == Status.SHORTLISTED
    set_job_status(conn, job_id, Status.APPLIED, force=True)
    conn.close()
    response = client.post(
        "/api/status",
        json={
            "job_id": job_id,
            "status": "interview",
            "note": "Invitation received",
            "expected_status": "applied",
        },
        headers={server.TOKEN_HEADER: TOKEN},
    )
    assert response.status_code == 200


def test_cross_origin_writes_are_rejected_even_with_token(client, job_id):
    response = client.post(
        "/api/decide",
        json={"job_id": job_id, "action": "approve"},
        headers={server.TOKEN_HEADER: TOKEN, "Origin": "https://untrusted.invalid"},
    )
    assert response.status_code == 403


def test_authenticated_health_and_server_reuse(settings, job_id):
    srv, url = server.start(settings, port=0, token=TOKEN)
    try:
        assert server.find_running(settings) == url
        opened = []
        assert server.serve(settings, open_browser=False, announce=opened.append) == url
        assert len(opened) == 1
        assert TOKEN not in opened[0]
    finally:
        srv.shutdown()
        srv.server_close()
    assert server.find_running(settings) is None


def test_public_shell_contains_no_private_state(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "Acme" not in page.text
    assert TOKEN not in page.text
    assert client.get("/api/state").status_code == 403


@pytest.mark.parametrize("bad_id", [True, 1.5, -1, "1.5"])
def test_invalid_identifiers_are_rejected(client, bad_id):
    response = client.post(
        "/api/decide",
        json={"job_id": bad_id, "action": "approve"},
        headers={server.TOKEN_HEADER: TOKEN},
    )
    assert response.status_code == 400


def test_workflow_updates_are_visible_and_new_runs_reset_stages(client, settings):
    server.dashboard.update_workflow(settings, "health", "running", new_run=True)
    server.dashboard.update_workflow(settings, "health", "done")
    server.dashboard.update_workflow(settings, "sync", "failed", message="Connector unavailable")
    state = client.get("/api/state", headers={server.TOKEN_HEADER: TOKEN}).json()["workflow"]
    assert state["steps"]["health"]["state"] == "done"
    assert state["steps"]["sync"]["state"] == "failed"
    first_id = state["run_id"]
    server.dashboard.update_workflow(settings, "health", "running", new_run=True)
    state = client.get("/api/state", headers={server.TOKEN_HEADER: TOKEN}).json()["workflow"]
    assert state["run_id"] != first_id
    assert "sync" not in state["steps"]


@pytest.fixture
def dashboard_page(client):
    from filler_support import launch_browser

    browser = launch_browser()
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    base = str(client.base_url)
    page.route(
        "**/*",
        lambda route: route.continue_() if route.request.url.startswith(base) else route.abort(),
    )
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(base + "#t=" + TOKEN)
    page.wait_for_selector("#connection.online")
    try:
        yield page
        assert not errors
    finally:
        page.close()


def test_browser_review_approval_updates_queue_and_pipeline(dashboard_page, settings, job_id):
    from playwright.sync_api import expect

    page = dashboard_page
    expect(page.locator(".job-card")).to_have_count(1)
    page.get_by_role("button", name="Approve", exact=True).click()
    expect(page.locator("#confirm-message")).to_contain_text("does not submit")
    page.get_by_role("button", name="Approve job", exact=True).click()
    expect(page.locator("#decide")).to_contain_text("A clear queue")
    page.get_by_role("tab", name="Approved", exact=False).click()
    expect(page.locator("#decide")).to_contain_text("Acme")
    page.locator('.nav [data-view="pipeline"]').click()
    expect(
        page.locator(".board-column").filter(has=page.locator("h3", has_text="approved"))
    ).to_contain_text("Acme")
    conn = connect(settings.db_path)
    assert current_status(conn, job_id) == Status.APPROVED
    assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0
    conn.close()


def test_browser_notes_and_filters_survive_refresh(dashboard_page):
    from playwright.sync_api import expect

    page = dashboard_page
    page.locator('.nav [data-view="jobs"]').click()
    page.get_by_role("searchbox", name="Search jobs").fill("Acme")
    page.get_by_role("button", name="Software Engineer II", exact=True).click()
    expect(page.locator("#detail-main")).to_contain_text("Build .NET services")
    page.locator("#note-text").fill("A draft that must survive refresh")
    page.wait_for_timeout(6000)
    expect(page.locator("#note-text")).to_have_value("A draft that must survive refresh")
    page.get_by_role("button", name="Save note", exact=True).click()
    expect(page.locator("#detail-main")).to_contain_text("A draft that must survive refresh")
    page.get_by_role("button", name="Close job details").click()
    expect(page.locator("#q")).to_have_value("Acme")
    page.reload()
    page.wait_for_selector("#connection.online")
    expect(page.locator("#q")).to_have_value("Acme")


def test_browser_batch_selection_offers_only_approve_and_skip(dashboard_page, settings, job_id):
    from playwright.sync_api import expect

    page = dashboard_page
    page.locator("#select-page").check()
    expect(page.locator("#selected-count")).to_have_text("1 selected")
    expect(page.locator("#batch [data-batch]")).to_have_count(2)
    expect(page.locator('[data-batch="snooze"]')).to_have_count(0)
    page.locator('[data-batch="skip"]').click()
    page.get_by_role("button", name="Skip job", exact=True).click()
    expect(page.locator("#batch")).to_be_hidden()
    conn = connect(settings.db_path)
    assert current_status(conn, job_id) == Status.DISMISSED
    assert get_job(conn, job_id)["snooze_until"] is None
    conn.close()


def test_browser_filled_buttons_keep_readable_text_on_hover(dashboard_page):
    from playwright.sync_api import expect

    page = dashboard_page
    approve = page.get_by_role("button", name="Approve", exact=True)
    expect(approve).to_be_visible()
    approve.hover()
    page.wait_for_timeout(200)
    colours = approve.evaluate(
        "el => { const s = getComputedStyle(el); return [s.color, s.backgroundColor]; }"
    )
    assert colours[0] != colours[1], f"hovered text is invisible: {colours}"


def test_browser_mobile_navigation_and_theme(dashboard_page):
    from playwright.sync_api import expect

    page = dashboard_page
    page.set_viewport_size({"width": 390, "height": 844})
    page.locator('.nav [data-view="jobs"]').click()
    expect(page.locator("#list-title")).to_have_text("All tracked jobs")
    before = page.locator("html").get_attribute("data-theme")
    page.get_by_role("button", name="Toggle dark mode").click()
    assert page.locator("html").get_attribute("data-theme") != before
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_non_ascii_access_token_is_rejected_cleanly(client):
    response = client.get("/api/state", params={"t": "non-ascii-\u00e9"})
    assert response.status_code == 403


def test_request_logs_do_not_contain_access_tokens(client, caplog):
    caplog.set_level("DEBUG", logger="jobhunt.server")
    client.get("/nope", headers={server.TOKEN_HEADER: TOKEN})
    assert all(
        TOKEN not in record.getMessage()
        for record in caplog.records
        if record.name == "jobhunt.server"
    )


def test_no_unexpected_action_endpoint_is_exposed(client, job_id):
    """Applying is the only outward action, and it is not reachable by accident."""
    for path in ("/api/send", "/api/execute", "/api/submit"):
        response = client.post(path, json={"job_id": job_id}, headers={server.TOKEN_HEADER: TOKEN})
        assert response.status_code == 404

    unconfirmed = client.post(
        "/api/fill-review", json={"job_id": job_id}, headers={server.TOKEN_HEADER: TOKEN}
    )

    assert unconfirmed.status_code == 400


def test_browser_cleared_saved_filters_stay_cleared(dashboard_page):
    from playwright.sync_api import expect

    page = dashboard_page
    page.locator("#location-filter").select_option("Bengaluru, India")
    page.locator("#st").select_option("shortlisted")
    page.reload()
    page.wait_for_selector("#connection.online")
    expect(page.locator("#location-filter")).to_have_value("Bengaluru, India")
    page.locator("#location-filter").select_option("")
    page.locator("#st").select_option("")
    page.get_by_role("button", name="Refresh", exact=True).click()
    page.wait_for_timeout(1000)
    expect(page.locator("#location-filter")).to_have_value("")
    expect(page.locator("#st")).to_have_value("")


def test_rejected_small_request_body_is_consumed_before_close():
    import io
    from types import SimpleNamespace
    from unittest.mock import Mock

    body = io.BytesIO(b'{"job_id":1}')
    handler = SimpleNamespace(
        command="POST", headers={"Content-Length": "12"}, rfile=body, connection=Mock()
    )
    server._Handler._discard_body(handler)
    assert body.tell() == 12
    assert handler._body_consumed is True


def test_rejection_does_not_read_unbounded_request_bodies():
    import io
    from types import SimpleNamespace
    from unittest.mock import Mock

    body = io.BytesIO(b"ignored")
    handler = SimpleNamespace(
        command="POST",
        headers={"Content-Length": str(server.MAX_BODY_BYTES + 1)},
        rfile=body,
        connection=Mock(),
    )
    server._Handler._discard_body(handler)
    assert body.tell() == 0


@pytest.mark.parametrize("submission_state", ["pending", "unknown"])
def test_unresolved_submissions_cannot_be_changed_by_review_buttons(
    client, settings, job_id, submission_state
):
    conn = connect(settings.db_path)
    set_job_status(conn, job_id, Status.APPROVED)
    with conn:
        conn.execute(
            "UPDATE jobs SET submission_state = ? WHERE id = ?", (submission_state, job_id)
        )
    conn.close()
    detail = client.get(
        "/api/job", params={"job_id": job_id}, headers={server.TOKEN_HEADER: TOKEN}
    ).json()
    assert detail["allowed_decisions"] == []
    response = client.post(
        "/api/decide",
        json={"job_id": job_id, "action": "skip"},
        headers={server.TOKEN_HEADER: TOKEN},
    )
    assert response.status_code == 409


def test_browser_status_filter_does_not_offer_hidden_archived_stages(dashboard_page, settings):
    from playwright.sync_api import expect

    connection = connect(settings.db_path)
    with connection:
        posting = JobPosting(
            source="greenhouse",
            source_job_id="filtered",
            company="Hidden",
            title="Director",
            url="https://example.invalid/hidden",
        )
        identifier = upsert_job(connection, posting, Normalizer(), {}).job_id
        set_job_status(connection, identifier, Status.FILTERED)
    connection.close()
    dashboard_page.get_by_role("button", name="Refresh", exact=True).click()
    dashboard_page.wait_for_timeout(1000)
    expect(dashboard_page.locator('#st option[value="filtered"]')).to_have_count(0)


def test_server_reuse_rejects_a_stale_build(settings, job_id, monkeypatch):
    running, _ = server.start(settings, port=0, token=TOKEN)
    try:
        monkeypatch.setattr(server, "CODE_REVISION", "changed-build")
        assert server.find_running(settings) is None
    finally:
        running.shutdown()
        running.server_close()


@pytest.mark.parametrize("width", [1440, 390])
def test_browser_run_and_mail_notices_stay_in_health(dashboard_page, settings, width):
    from playwright.sync_api import expect

    from jobhunt.mail.ingest import save_needs_review

    page = dashboard_page
    page.set_viewport_size({"width": width, "height": 1000})
    save_needs_review(
        settings.path("mail_inbox_dir"),
        [{"gmail_id": "review-message", "subject": "Example application update"}],
    )
    server.dashboard.update_workflow(
        settings, "sync", "running", message="Checking test inbox", new_run=True
    )
    for view in ("today", "health", "jobs", "pipeline", "today", "health"):
        page.locator(f'.nav [data-view="{view}"]').click()
        with page.expect_response(lambda response: response.url.endswith("/api/state")):
            page.get_by_role("button", name="Refresh", exact=True).click()
        expect(page.locator("#health-notice")).to_contain_text("1 email decision")
        expect(page.locator("#workflow-message")).to_have_text("Checking test inbox")
        for panel in ("#workflow", "#health-notice"):
            if view == "health":
                expect(page.locator(panel)).to_be_visible()
            else:
                expect(page.locator(panel)).to_be_hidden()
    page.reload()
    page.wait_for_selector("#connection.online")
    expect(page.locator("#workflow")).to_be_visible()
    expect(page.locator("#health-notice")).to_be_visible()


def _record_payload(job_id, **extra):
    from datetime import timedelta

    from jobhunt.clock import now, to_iso

    return {
        "job_id": job_id,
        "confirmed": True,
        "at": to_iso(now() - timedelta(days=12)),
        "note": "Verified confirmation",
        **extra,
    }


@pytest.mark.parametrize("confirmed", [False, None, 1, "true"])
def test_manual_application_requires_explicit_confirmation(client, job_id, confirmed):
    response = client.post(
        "/api/applied",
        headers={server.TOKEN_HEADER: TOKEN},
        json=_record_payload(job_id, confirmed=confirmed, channel="manual"),
    )
    assert response.status_code == 400


def test_manual_application_tracks_unprepared_job_and_preserves_progress(client, settings, job_id):
    payload = _record_payload(job_id, channel="manual", reference="https://example.invalid/receipt")
    response = client.post("/api/applied", headers={server.TOKEN_HEADER: TOKEN}, json=payload)
    assert response.status_code == 200, response.text
    state = client.get("/api/state", headers={server.TOKEN_HEADER: TOKEN}).json()
    assert state["tracking"]["active_applications"] == 1
    assert state["tracking"]["follow_up_due_count"] == 1
    conn = connect(settings.db_path)
    set_job_status(conn, job_id, Status.INTERVIEW)
    replay = client.post("/api/applied", headers={server.TOKEN_HEADER: TOKEN}, json=payload)
    assert replay.status_code == 200
    assert current_status(conn, job_id) == Status.INTERVIEW
    app = conn.execute("SELECT * FROM applications WHERE job_id = ?", (job_id,)).fetchone()
    assert app["submitted_at"] == payload["at"]
    assert len([e for e in events_for_job(conn, job_id) if e["event_type"] == "submitted"]) == 1
    conn.close()


def test_manual_referral_recording_and_outcome_are_idempotent(client, settings, job_id):
    payload = _record_payload(job_id, channel="linkedin", contact_name="Test employee")
    first = client.post("/api/referral", headers={server.TOKEN_HEADER: TOKEN}, json=payload)
    assert first.status_code == 200, first.text
    repeated = client.post("/api/referral", headers={server.TOKEN_HEADER: TOKEN}, json=payload)
    assert repeated.status_code == 200
    assert repeated.json()["outreach_id"] == first.json()["outreach_id"]
    state = client.get("/api/state", headers={server.TOKEN_HEADER: TOKEN}).json()
    assert state["tracking"]["follow_up_due_count"] == 1
    outcome = _record_payload(job_id, outreach_id=first.json()["outreach_id"], outcome="completed")
    for _ in range(2):
        response = client.post(
            "/api/referral-outcome", headers={server.TOKEN_HEADER: TOKEN}, json=outcome
        )
        assert response.status_code == 200, response.text
    replay = client.post("/api/referral", headers={server.TOKEN_HEADER: TOKEN}, json=payload)
    assert replay.status_code == 200
    assert replay.json()["already_recorded"] is True
    conn = connect(settings.db_path)
    assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM outreach").fetchone()[0] == 1
    assert get_job(conn, job_id)["snooze_until"] is None
    assert (
        len([e for e in events_for_job(conn, job_id) if e["event_type"] == "referral_received"])
        == 1
    )
    conn.close()


def test_manual_approval_does_not_bypass_automation_readiness(client, settings, job_id):
    from jobhunt.apply.safety import SafetyGate

    conn = connect(settings.db_path)
    set_job_status(conn, job_id, Status.DISCOVERED, force=True)
    with conn:
        conn.execute("UPDATE jobs SET score = NULL, prefilter_pass = NULL WHERE id = ?", (job_id,))
    response = client.post(
        "/api/decide",
        headers={server.TOKEN_HEADER: TOKEN},
        json={"job_id": job_id, "action": "approve"},
    )
    assert response.status_code == 200, response.text
    assert current_status(conn, job_id) == Status.APPROVED
    assert not SafetyGate(conn).check(get_job(conn, job_id), "greenhouse").ok
    conn.close()


def test_browser_manual_application_and_dragged_progress(dashboard_page, job_id):
    from playwright.sync_api import expect

    page = dashboard_page
    page.get_by_role("button", name="Software Engineer II", exact=True).click()
    page.locator("#detail").get_by_role("button", name="I applied", exact=True).click()
    expect(page.locator("#record-dialog")).to_be_visible()
    page.locator("#record-note").fill("I saw the confirmation page")
    page.locator("#record-confirm").check()
    page.locator("#record-save").click()
    expect(page.locator("#record-dialog")).to_be_hidden()
    expect(page.locator("#detail-subtitle")).to_contain_text("Acme")
    page.get_by_role("button", name="Close job details").click()
    expect(page.locator('[data-metric="active"] .metric-value')).to_have_text("1")
    page.locator('.nav [data-view="pipeline"]').click()
    page.locator(f'.board-card[data-detail="{job_id}"]').drag_to(
        page.locator('.board-column[data-stage="interview"]')
    )
    expect(page.locator("#record-title")).to_contain_text("interview")
    page.locator("#record-note").fill("Interview invitation received")
    page.locator("#record-confirm").check()
    page.locator("#record-save").click()
    expect(
        page.locator(f'.board-column[data-stage="interview"] [data-detail="{job_id}"]')
    ).to_be_visible()
    page.reload()
    page.wait_for_selector("#connection.online")
    expect(
        page.locator(f'.board-column[data-stage="interview"] [data-detail="{job_id}"]')
    ).to_be_visible()


def test_browser_records_manual_referral(dashboard_page):
    from playwright.sync_api import expect

    page = dashboard_page
    page.get_by_role("button", name="Software Engineer II", exact=True).click()
    page.locator("#detail").get_by_role("button", name="Referral requested", exact=True).click()
    page.locator("#record-contact-name").fill("My colleague")
    page.locator("#record-note").fill("I sent the request on LinkedIn")
    page.locator("#record-confirm").check()
    page.locator("#record-save").click()
    expect(page.locator("#detail-main")).to_contain_text("My colleague")
    page.get_by_role("button", name="Referral received", exact=True).click()
    page.locator("#record-note").fill("Employee confirmed the referral was completed")
    page.locator("#record-confirm").check()
    page.locator("#record-save").click()
    expect(page.locator('.referral-record[data-outcome="completed"]')).to_contain_text(
        "Referral received"
    )


@pytest.mark.parametrize("denied", [True, False])
def test_browser_claude_prompt_has_visible_copy_feedback_and_fallback(
    dashboard_page, denied, job_id
):
    from playwright.sync_api import expect

    page = dashboard_page
    page.evaluate(
        """denied => Object.defineProperty(navigator, 'clipboard', {
        configurable: true, value: {writeText: async text => {
            if (denied) throw new Error('Clipboard denied'); window.copiedPrompt = text;
        }}
    })""",
        denied,
    )
    page.get_by_role("button", name="Software Engineer II", exact=True).click()
    page.get_by_role("button", name="Copy preparation prompt", exact=True).click()
    expect(page.locator("#handoff-dialog")).to_be_visible()
    expect(page.locator("#handoff-text")).to_have_value(re.compile(f".*job #{job_id}.*"))
    expect(page.locator("#handoff-status")).to_contain_text("manually" if denied else "Copied")
    if not denied:
        assert f"job #{job_id}" in page.evaluate("window.copiedPrompt")


def test_manual_records_survive_server_restart(settings, job_id):
    for first in (True, False):
        running, url = server.start(settings, port=0, token=TOKEN)
        try:
            with httpx.Client(
                base_url=f"http://{urlparse(url).netloc}",
                timeout=10,
                headers={server.TOKEN_HEADER: TOKEN},
            ) as http:
                if first:
                    response = http.post(
                        "/api/applied", json=_record_payload(job_id, channel="manual")
                    )
                    assert response.status_code == 200
                    response = http.post(
                        "/api/referral",
                        json=_record_payload(
                            job_id, channel="linkedin", contact_name="Persisted contact"
                        ),
                    )
                    assert response.status_code == 200
                state = http.get("/api/state").json()
                assert len(state["tracking"]["referrals"]) == 1
                assert state["tracking"]["active_applications"] == 1
                assert state["funnel"]["applied"] == 1
        finally:
            running.shutdown()
            running.server_close()


@pytest.mark.parametrize(
    "path,extra",
    [
        ("/api/applied", {"channel": "manual"}),
        ("/api/referral", {"channel": "linkedin", "contact_name": "Test"}),
    ],
)
def test_manual_record_rejects_future_dates(client, job_id, path, extra):
    response = client.post(
        path,
        headers={server.TOKEN_HEADER: TOKEN},
        json=_record_payload(job_id, at="2999-01-01T00:00:00Z", **extra),
    )
    assert response.status_code == 400


def test_browser_manual_tracking_on_mobile(dashboard_page):
    from playwright.sync_api import expect

    page = dashboard_page
    page.set_viewport_size({"width": 390, "height": 844})
    page.get_by_role("button", name="Software Engineer II", exact=True).click()
    page.locator("#detail").get_by_role("button", name="I applied", exact=True).click()
    page.locator("#record-note").fill("Confirmed in the mobile browser")
    page.locator("#record-confirm").check()
    page.locator("#record-save").click()
    expect(page.locator("#record-dialog")).to_be_hidden()
    page.get_by_role("button", name="Close job details").click()
    expect(page.locator('[data-metric="active"] .metric-value')).to_have_text("1")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_browser_visual_snapshots(dashboard_page, tmp_path):
    page = dashboard_page
    desktop = tmp_path / "dashboard-desktop.png"
    mobile = tmp_path / "dashboard-mobile.png"
    page.screenshot(path=str(desktop), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    page.screenshot(path=str(mobile), full_page=True)
    print(f"Dashboard snapshots: {desktop} | {mobile}")


def card_actions(card) -> list[str]:
    """The visible decision buttons on a card, in the order they are rendered."""
    return [
        text.strip()
        for text in card.locator(".card-actions .btn").all_inner_texts()
        if text.strip() != "Details"
    ]


def test_browser_a_card_offers_approve_before_applied_never_both(dashboard_page, settings, job_id):
    """Approving and recording an application are sequential, so a card shows one at a time."""
    from playwright.sync_api import expect

    page = dashboard_page
    card = page.locator(".job-card").first
    expect(card.get_by_role("button", name="Approve", exact=True)).to_be_visible()
    expect(card.get_by_role("button", name="I applied")).to_have_count(0)
    assert card_actions(card) == ["Approve", "Skip"]

    card.get_by_role("button", name="Approve", exact=True).click()
    page.get_by_role("button", name="Approve job").click()
    page.locator('.nav [data-tab="ready"], [data-tab="ready"]').first.click()

    approved = page.locator(".job-card").first
    expect(approved.get_by_role("button", name="I applied")).to_be_visible()
    expect(approved.get_by_role("button", name="Approve", exact=True)).to_have_count(0)
    assert card_actions(approved) == ["I applied", "Skip"]


def test_browser_the_detail_drawer_puts_applied_before_skip(dashboard_page, settings, job_id):
    """One order everywhere: the step forward, the record of a step taken, then the way out."""
    page = dashboard_page
    page.locator(".job-card").first.locator("button.details").click()
    page.wait_for_selector("#detail-main .actions .btn")

    labels = [
        text.strip()
        for text in page.locator("#detail-main .actions").first.locator("button").all_inner_texts()
        if text.strip()
    ]
    assert labels == ["Approve", "I applied", "Skip", "Referral requested"]


def test_browser_the_drawer_is_the_only_thing_that_scrolls(dashboard_page):
    """The description had its own scrollbox, so a long posting gave the panel two nested bars."""
    page = dashboard_page
    page.locator(".job-card").first.locator("button.details").click()
    page.wait_for_selector("#detail-main .actions .btn")

    assert page.evaluate("getComputedStyle(document.getElementById('detail')).overflowY") == "auto"
    # A textarea scrolls its own value by default; that is the control, not a second panel.
    nested = page.evaluate(
        """() => [...document.querySelectorAll('#detail *')]
             .filter(el => !['TEXTAREA', 'INPUT', 'SELECT'].includes(el.tagName))
             .filter(el => /auto|scroll/.test(getComputedStyle(el).overflowY))
             .map(el => el.tagName.toLowerCase() + '.' + (el.className || ''))"""
    )
    assert nested == []


@pytest.mark.parametrize("width", [1280, 390])
def test_browser_modal_hides_background_scrollbar_and_restores_scrolling(
    dashboard_page, settings, job_id, width
):
    from playwright.sync_api import expect

    page = dashboard_page
    page.set_viewport_size({"width": width, "height": 720})
    page.locator(".workspace").evaluate("el => el.style.minHeight = '200vh'")
    conn = connect(settings.db_path)
    with conn:
        conn.execute(
            "UPDATE jobs SET description_md = ? WHERE id = ?",
            ("Build .NET services with a long, detailed job description.\n" * 80, job_id),
        )
    conn.close()
    page.evaluate("window.scrollTo(0, 100)")
    position = page.evaluate("window.scrollY")
    assert position > 0
    assert page.evaluate("document.documentElement.scrollHeight > innerHeight")

    page.locator(".job-card").first.locator("button.details").click()
    page.wait_for_selector("#detail-main .actions .btn")
    expect(page.locator("html")).to_have_css("overflow-y", "hidden")
    drawer = page.locator("#detail")
    expect(drawer).to_have_css("overflow-y", "auto")
    assert drawer.evaluate("el => el.scrollHeight > el.clientHeight")
    drawer.evaluate("el => el.scrollTop = 200")
    assert drawer.evaluate("el => el.scrollTop") > 0
    assert page.evaluate("window.scrollY") == position

    page.get_by_role("button", name="Copy preparation prompt").click()
    expect(page.locator("#handoff-dialog")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#handoff-dialog")).not_to_be_visible()
    expect(drawer).to_be_visible()
    expect(page.locator("html")).to_have_css("overflow-y", "hidden")
    page.keyboard.press("Escape")
    expect(drawer).not_to_be_visible()
    expect(page.locator("html")).not_to_have_css("overflow-y", "hidden")
    assert page.evaluate("window.scrollY") == position
    page.evaluate("window.scrollBy(0, 100)")
    assert page.evaluate("window.scrollY") > position


def test_browser_the_queue_tabs_never_show_a_scrollbar(dashboard_page):
    """The tabs slide sideways on a narrow window, but the bar itself was a stray line."""
    from playwright.sync_api import expect

    page = dashboard_page
    page.locator('.nav [data-view="today"]').first.click()
    page.set_viewport_size({"width": 420, "height": 900})
    tabs = page.locator("#queue-tabs")
    expect(tabs).to_be_visible()

    # Narrow enough that the row really does overflow, so the assertion below has something
    # to prove. offsetHeight also counts the bottom border, so take that out of the reckoning.
    assert tabs.evaluate("el => el.scrollWidth - el.clientWidth") > 0
    assert tabs.evaluate("el => getComputedStyle(el).scrollbarWidth") == "none"
    bar = tabs.evaluate(
        """el => {
             const s = getComputedStyle(el);
             const borders = parseFloat(s.borderTopWidth) + parseFloat(s.borderBottomWidth);
             return el.offsetHeight - el.clientHeight - borders;
           }"""
    )
    assert bar == 0


def test_browser_light_mode_is_the_default(dashboard_page):
    """Light is the default even on a machine set to dark, until the toggle says otherwise."""
    page = dashboard_page
    page.emulate_media(color_scheme="dark")
    page.evaluate("localStorage.removeItem('jobhunt-theme')")
    page.reload()
    page.wait_for_selector("#connection.online")

    assert page.locator("html").get_attribute("data-theme") == "light"
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") != "rgb(15, 20, 28)"


def test_browser_has_no_separate_access_panel(dashboard_page):
    """The panel sat above the tab content, so it showed on every view. One notice replaces it."""
    page = dashboard_page
    assert page.locator("#access").count() == 0
    assert page.locator("#connection-error").is_hidden()


def test_browser_a_stale_tab_is_told_how_to_get_back_in(dashboard_page):
    """Restarting the server mints a new token, which strands an open tab. Say so, once."""
    page = dashboard_page
    page.evaluate("sessionStorage.removeItem('jobhunt-access')")
    page.goto(page.url.split("#")[0])
    page.wait_for_selector("#connection-error:not([hidden])")

    assert "uv run jobhunt serve" in page.locator("#connection-error").inner_text()
    assert page.locator("#access").count() == 0


# --------------------------------------------------------------------------- apply button


def approve_and_prepare(settings: Settings, job_id: int) -> None:
    """Put a job in the one state the Apply button is offered for."""
    conn = connect(settings.db_path)
    try:
        with conn:
            conn.execute(
                "UPDATE jobs SET status = 'approved', prefill_state = 'ready', score = 88, "
                "apply_url = 'https://boards.greenhouse.io/acme/jobs/77' WHERE id = ?",
                (job_id,),
            )
    finally:
        conn.close()


def test_fill_readiness_explains_why_a_job_is_not_ready(client, job_id):
    """A discovered job is not applyable, and the drawer needs a reason, not a blank button."""
    readiness = client.get(
        "/api/job", params={"job_id": job_id}, headers={"X-JobHunt-Token": TOKEN}
    ).json()["fill_readiness"]

    assert readiness["ok"] is False
    assert "Approve" in readiness["reason"]


def test_fill_readiness_is_true_once_approved_and_prepared(client, settings, job_id):
    approve_and_prepare(settings, job_id)

    readiness = client.get(
        "/api/job", params={"job_id": job_id}, headers={"X-JobHunt-Token": TOKEN}
    ).json()["fill_readiness"]

    assert readiness["ok"] is True
    assert readiness["mode"] == "python"


def test_an_unsupported_ats_says_to_apply_by_hand(client, settings, job_id):
    conn = connect(settings.db_path)
    try:
        with conn:
            conn.execute(
                "UPDATE jobs SET status = 'approved', prefill_state = 'ready', "
                "ats_type = 'smartrecruiters', "
                "apply_url = 'https://jobs.smartrecruiters.com/Acme/1-engineer' WHERE id = ?",
                (job_id,),
            )
    finally:
        conn.close()

    readiness = client.get(
        "/api/job", params={"job_id": job_id}, headers={"X-JobHunt-Token": TOKEN}
    ).json()["fill_readiness"]

    assert readiness["ok"] is False
    assert "by hand" in readiness["reason"]


def test_apply_refuses_without_an_explicit_confirmation(client, settings, job_id):
    """The button sends confirmed=true. A bare POST must not submit an application."""
    approve_and_prepare(settings, job_id)

    response = client.post(
        "/api/fill-review", json={"job_id": job_id}, headers={"X-JobHunt-Token": TOKEN}
    )

    assert response.status_code == 400


def test_apply_refuses_a_job_that_is_not_ready(client, job_id):
    response = client.post(
        "/api/fill-review",
        json={"job_id": job_id, "confirmed": True, "request_id": "test-fill-click"},
        headers={"X-JobHunt-Token": TOKEN},
    )

    assert response.status_code == 409
    assert "Approve" in response.json()["error"]


@pytest.fixture
def fake_apply(settings: Settings, job_id: int):
    """A dashboard whose Apply button records the call instead of opening a browser."""
    import threading

    from jobhunt.apply_runs import ApplyRuns

    calls: list[int] = []
    ready = threading.Event()
    workers = []

    def run(target: int) -> tuple[int, str]:
        calls.append(target)
        control = runs.control(target)
        control.update("ready_for_review", "Review in the browser; submit yourself", filled=4)
        ready.set()
        control.cancelled.wait()
        return 0, "Fill window closed; no submission recorded"

    def spawn(work):
        worker = threading.Thread(target=work, daemon=True)
        workers.append(worker)
        worker.start()
        assert ready.wait(5)

    runs = ApplyRuns(run, spawn=spawn)
    srv, url = server.start(settings, port=0, token=TOKEN, runs=runs)
    try:
        with httpx.Client(base_url=f"http://{urlparse(url).netloc}", timeout=10) as http:
            yield http, calls
    finally:
        active = runs.active()
        if active:
            runs.command(active.job_id, active.run_id, "close")
        for worker in workers:
            worker.join(5)
            assert not worker.is_alive()
        srv.shutdown()
        srv.server_close()


def test_a_confirmed_apply_runs_and_reports_its_outcome(fake_apply, settings, job_id):
    http, calls = fake_apply
    approve_and_prepare(settings, job_id)

    started = http.post(
        "/api/fill-review",
        json={"job_id": job_id, "confirmed": True, "request_id": "test-fill-click"},
        headers={"X-JobHunt-Token": TOKEN},
    )
    status = http.get(
        "/api/fill-status", params={"job_id": job_id}, headers={"X-JobHunt-Token": TOKEN}
    ).json()

    assert started.status_code == 200
    assert calls == [job_id]
    assert status["run"]["state"] == "ready_for_review"
    assert status["run"]["code"] is None
    assert "submit yourself" in status["run"]["message"]
    conn = connect(settings.db_path)
    assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0
    conn.close()


def test_a_job_with_no_run_reports_nothing(fake_apply, job_id):
    http, _ = fake_apply

    status = http.get(
        "/api/fill-status", params={"job_id": job_id}, headers={"X-JobHunt-Token": TOKEN}
    ).json()

    assert status["run"] is None


def test_apply_needs_the_dashboard_token(fake_apply, settings, job_id):
    http, calls = fake_apply
    approve_and_prepare(settings, job_id)

    response = http.post("/api/fill-review", json={"job_id": job_id, "confirmed": True})

    assert response.status_code == 403
    assert calls == []


@pytest.fixture
def apply_page(fake_apply):
    """A real browser on a dashboard whose Apply button cannot open a second browser."""
    from filler_support import launch_browser

    http, calls = fake_apply
    browser = launch_browser()
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    base = str(http.base_url)
    page.route(
        "**/*",
        lambda route: route.continue_() if route.request.url.startswith(base) else route.abort(),
    )
    page.goto(base + "#t=" + TOKEN)
    page.wait_for_selector("#connection.online")
    try:
        yield page, calls
    finally:
        page.close()


def open_drawer(page, tab: str | None = None):
    """Open the first card's detail drawer, switching tab first when the job moved."""
    if tab:
        page.locator(f'[data-tab="{tab}"]').first.click()
        page.wait_for_selector(".job-card")
    page.locator(".job-card").first.locator("button.details").click()
    page.wait_for_selector("#detail-main .actions .btn")


def test_browser_the_drawer_says_why_a_job_cannot_be_applied_to(apply_page):
    page, _ = apply_page
    open_drawer(page)

    text = page.locator("#detail-main").inner_text()

    # The drawer stylesheet uppercases its headings, so compare case-insensitively.
    assert "fill for review" in text.lower()
    assert "Approve the job first." in text
    assert page.locator("#detail-main button[data-apply]").count() == 0


def test_browser_one_click_fills_for_review_without_a_submit_button(apply_page, settings, job_id):
    """One click fills a draft; the only submit control belongs to the employer's site."""
    from playwright.sync_api import expect

    page, calls = apply_page
    approve_and_prepare(settings, job_id)
    page.reload()
    page.wait_for_selector("#connection.online")
    open_drawer(page, tab="ready")
    page.get_by_role("button", name="Fill for review", exact=True).click()
    expect(page.locator("#fill-panel")).to_contain_text("submit yourself")
    assert calls == [job_id]
    assert page.locator("button[data-apply]").count() == 0
    assert "Yes, submit" not in page.locator("#detail-main").inner_text()
    page.get_by_role("button", name="Close job details").click()
    open_drawer(page, tab="ready")
    expect(page.locator("#fill-panel")).to_contain_text("submit yourself")
    assert calls == [job_id]
    conn = connect(settings.db_path)
    assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0
    conn.close()


def test_browser_closing_the_fill_window_requires_confirmation(apply_page, settings, job_id):
    from playwright.sync_api import expect

    page, calls = apply_page
    approve_and_prepare(settings, job_id)
    page.reload()
    page.wait_for_selector("#connection.online")
    open_drawer(page, tab="ready")
    page.get_by_role("button", name="Fill for review", exact=True).click()
    page.get_by_role("button", name="Close fill window", exact=True).click()
    page.get_by_role("button", name="Keep open", exact=True).click()
    expect(page.locator("#fill-panel")).to_contain_text("submit yourself")
    page.get_by_role("button", name="Close fill window", exact=True).click()
    page.get_by_role("button", name="Yes, close fill window", exact=True).click()
    expect(page.locator("#fill-panel")).to_contain_text("no submission recorded", timeout=10000)
    assert calls == [job_id]


def test_old_dashboard_submit_route_is_disabled(fake_apply, settings, job_id):
    http, calls = fake_apply
    approve_and_prepare(settings, job_id)
    response = http.post(
        "/api/apply",
        json={"job_id": job_id, "confirmed": True},
        headers={server.TOKEN_HEADER: TOKEN},
    )
    assert response.status_code == 404
    assert calls == []


def test_fill_does_not_require_prepared_answers(client, settings, job_id):
    approve_and_prepare(settings, job_id)
    conn = connect(settings.db_path)
    with conn:
        conn.execute("UPDATE jobs SET prefill_state = 'none' WHERE id = ?", (job_id,))
    conn.close()
    readiness = client.get(
        "/api/job", params={"job_id": job_id}, headers={server.TOKEN_HEADER: TOKEN}
    ).json()["fill_readiness"]
    assert readiness["ok"] is True


def test_fill_controls_are_confirmed_and_bound_to_the_session(fake_apply, settings, job_id):
    http, calls = fake_apply
    approve_and_prepare(settings, job_id)
    payload = {"job_id": job_id, "confirmed": True, "request_id": "same-fill"}
    headers = {server.TOKEN_HEADER: TOKEN}
    first = http.post("/api/fill-review", json=payload, headers=headers)
    second = http.post("/api/fill-review", json=payload, headers=headers)
    assert first.status_code == second.status_code == 200
    assert calls == [job_id]
    control = {"job_id": job_id, "run_id": "same-fill", "action": "close"}
    assert http.post("/api/fill-control", json=control, headers=headers).status_code == 400
    control.update(confirmed=True, run_id="stale")
    assert http.post("/api/fill-control", json=control, headers=headers).status_code == 409
    control["run_id"] = "same-fill"
    assert http.post("/api/fill-control", json=control, headers=headers).status_code == 200


def test_the_page_carries_an_inline_favicon(client):
    """The tab needs an icon, and the dashboard must never fetch one from the network."""
    page = client.get("/", params={"t": TOKEN}).text

    icon = page.split('rel="icon" href="')[1].split('"')[0]

    # A data URI cannot cause a request. The one http reference inside it is the SVG
    # namespace, which is a declaration browsers never fetch.
    assert icon.startswith("data:image/svg+xml,")
    assert "www.w3.org/2000/svg" in icon

    # It must be the same mark as the header brand, not a second unrelated logo.
    compass = page.split("compass:'")[1].split("'")[0]
    from urllib.parse import unquote

    assert compass in unquote(icon)


def test_browser_the_tab_icon_actually_loads(dashboard_page):
    """A malformed data URI would leave the tab blank, and nothing else would complain."""
    page = dashboard_page
    loaded = page.evaluate(
        """() => new Promise(resolve => {
             const href = document.querySelector('link[rel=icon]').href;
             const img = new Image();
             img.onload = () => resolve(img.width > 0);
             img.onerror = () => resolve(false);
             img.src = href;
           })"""
    )

    assert loaded is True
