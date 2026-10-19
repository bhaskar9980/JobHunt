import json
from pathlib import Path

import pytest
import yaml
from freezegun import freeze_time

from jobhunt import web_discovery as web
from jobhunt.config import Settings
from jobhunt.repo.jobs import upsert_job


@pytest.fixture
def settings(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    cfg = {
        "enabled": True,
        "monthly_credit_limit": 1000,
        "daily_credit_limit": 30,
        "searches_per_day": 3,
        "search_cache_hours": 72,
        "budget_reset_day": 1,
        "search_limit": 10,
        "search_credits_per_ten": 2,
        "scrape_credits": 1,
        "queries": ['"C#" "remote worldwide" careers', '".NET" visa relocation careers'],
        "blocked_domains": ["linkedin.com", "indeed.com"],
        "official_sites": [{"company": "Acme, Inc.", "url_prefix": "https://acme.test/jobs/"}],
        "closed_phrases": ["no longer accepting applications", "position has been filled"],
        "min_description_chars": 40,
    }
    (config / "sources.yaml").write_text(yaml.safe_dump({"firecrawl": cfg}), encoding="utf-8")
    (config / "companies.yaml").write_text("companies: []", encoding="utf-8")
    (config / "locations.yaml").write_text("cities: {}", encoding="utf-8")
    return Settings(tmp_path, config, tmp_path / "data", tmp_path / "logs", tmp_path / "jobs.db")


def complete_search(conn, settings, url="https://acme.test/jobs/1"):
    request = web.plan_request(conn, settings)["request"]
    receipt = {
        "response": {
            "success": True,
            "data": {
                "web": [{"url": url, "title": "C# Software Engineer", "description": "remote"}]
            },
        },
        "jobs": [],
    }
    Path(request["receipt_path"]).write_text(json.dumps(receipt), encoding="utf-8")
    web.complete_request(conn, settings, request["id"])
    return request


def test_reservation_precedes_mcp_and_cannot_be_reissued(conn, settings):
    request = web.plan_request(conn, settings)["request"]
    assert request["tool"] == "firecrawl_search"
    assert request["arguments"]["limit"] == 10
    assert "scrapeOptions" not in request["arguments"]
    assert web.budget_status(conn, settings)["allocated_credits"] == 2
    assert web.plan_request(conn, settings)["request"] is None
    assert web.budget_status(conn, settings)["allocated_credits"] == 2


def test_search_is_only_a_lead_and_receipts_are_idempotent(conn, settings):
    request = complete_search(conn, settings)
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    assert web.complete_request(conn, settings, request["id"])["already_processed"]
    assert web.budget_status(conn, settings)["allocated_credits"] == 2
    scrape = web.plan_request(conn, settings)["request"]
    assert scrape["tool"] == "firecrawl_scrape"
    assert scrape["arguments"]["maxAge"] == 0
    assert scrape["arguments"]["proxy"] == "basic"
    assert scrape["arguments"]["formats"] == ["markdown"]


def test_failed_request_stays_charged_and_query_is_cached(conn, settings):
    request = web.plan_request(conn, settings)["request"]
    web.fail_request(conn, settings, request["id"])
    next_request = web.plan_request(conn, settings)["request"]
    assert next_request["arguments"]["query"] != request["arguments"]["query"]
    assert web.budget_status(conn, settings)["allocated_credits"] == 4


@pytest.mark.parametrize("field", ["monthly_credit_limit", "daily_credit_limit"])
def test_budget_stops_before_an_unaffordable_request(conn, settings, field):
    file = settings.config_dir / "sources.yaml"
    data = yaml.safe_load(file.read_text(encoding="utf-8"))
    data["firecrawl"][field] = 1
    file.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert web.plan_request(conn, settings)["request"] is None
    assert web.budget_status(conn, settings)["allocated_credits"] == 0


@pytest.mark.parametrize(
    "url",
    [
        "https://linkedin.com/jobs/1",
        "http://localhost/jobs/1",
        "https://127.0.0.1/jobs/1",
        "file:///private.txt",
        "https://user:password@acme.test/jobs/1",
        "https://unknown.test/jobs/1",
        "https://acme.test.evil.test/jobs/1",
    ],
)
def test_untrusted_and_private_search_urls_are_not_scraped(conn, settings, url):
    complete_search(conn, settings, url)
    request = web.plan_request(conn, settings)["request"]
    assert request["tool"] == "firecrawl_search"


def test_existing_native_job_is_not_scraped(conn, settings, norm, priority, make_posting):
    upsert_job(conn, make_posting(url="https://acme.test/jobs/1"), norm, priority)
    complete_search(conn, settings)
    assert web.plan_request(conn, settings)["request"]["tool"] == "firecrawl_search"


def scrape_receipt(request, **overrides):
    description = "Build C# and .NET backend services with MongoDB, Kafka and automated tests."
    body = f"# Software Engineer\nAcme, Inc.\nRemote - India\n{description}\nApply now"
    receipt = {
        "response": {
            "success": True,
            "data": {
                "markdown": body,
                "metadata": {"statusCode": 200, "url": request["arguments"]["url"]},
            },
        },
        "jobs": [
            {
                "company": "Acme, Inc.",
                "title": "Software Engineer",
                "location": "Remote - India",
                "is_remote": True,
                "description_md": description,
                "is_job_posting": True,
                "is_open": True,
            }
        ],
    }
    receipt.update(overrides)
    return receipt


def test_only_verified_live_facts_enter_existing_dedupe_pipeline(conn, settings):
    complete_search(conn, settings)
    request = web.plan_request(conn, settings)["request"]
    Path(request["receipt_path"]).write_text(json.dumps(scrape_receipt(request)), encoding="utf-8")
    result = web.complete_request(conn, settings, request["id"])
    assert len(result["job_ids"]) == 1
    assert conn.execute("SELECT primary_source FROM jobs").fetchone()[0] == "firecrawl"
    assert web.budget_status(conn, settings)["allocated_credits"] == 3


@pytest.mark.parametrize("problem", ["closed", "invented", "redirect", "status", "not_a_job"])
def test_bad_posting_evidence_never_creates_jobs(conn, settings, problem):
    complete_search(conn, settings)
    request = web.plan_request(conn, settings)["request"]
    receipt = scrape_receipt(request)
    data = receipt["response"]["data"]
    if problem == "closed":
        data["markdown"] += " This position has been filled."
    elif problem == "invented":
        receipt["jobs"][0]["description_md"] = "Invented C# job description with visa sponsorship."
    elif problem == "redirect":
        data["metadata"]["url"] = "https://unknown.test/jobs/1"
    elif problem == "status":
        data["metadata"]["statusCode"] = 404
    else:
        receipt["jobs"][0]["is_job_posting"] = False
    Path(request["receipt_path"]).write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError):
        web.complete_request(conn, settings, request["id"])
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    assert web.budget_status(conn, settings)["allocated_credits"] == 3


def test_budget_rollover_preserves_unresolved_request_and_search_cache(conn, settings):
    with freeze_time("2026-09-30 23:59:00"):
        first = web.plan_request(conn, settings)["request"]
    with freeze_time("2026-10-01 00:01:00"):
        assert web.budget_status(conn, settings)["allocated_credits"] == 0
        assert web.plan_request(conn, settings)["request"] is None
        web.fail_request(conn, settings, first["id"])
        assert web.budget_status(conn, settings)["allocated_credits"] == 2
        second = web.plan_request(conn, settings)["request"]
        assert second["arguments"]["query"] != first["arguments"]["query"]


def test_raw_mcp_artifact_is_preserved_and_provider_usage_recorded(conn, settings, tmp_path):
    request = web.plan_request(conn, settings)["request"]
    raw = {"success": True, "data": {"web": []}, "creditsUsed": 2}
    artifact = tmp_path / "tool-response.json"
    artifact.write_text(json.dumps(raw), encoding="utf-8")
    web.complete_request(conn, settings, request["id"], response_file=artifact)
    saved = json.loads(Path(request["receipt_path"]).read_text(encoding="utf-8"))
    assert saved == {"response": raw, "jobs": []}
    assert web.budget_status(conn, settings)["reported_credits"] == 2
