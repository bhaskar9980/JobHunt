from __future__ import annotations

import calendar
import hashlib
import ipaddress
import json
import re
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from jobhunt.clock import now, now_iso, to_iso
from jobhunt.config import Settings, build_normalizer, load_companies, load_sources, source_priority
from jobhunt.db import read_state, transaction, write_state
from jobhunt.models import JobPosting
from jobhunt.normalize import canonical_url
from jobhunt.repo import jobs as jobs_repo

STATE_KEY = "firecrawl_mcp"
RECEIPT_DIR = "firecrawl"
RESULT_BLOCK_SIZE = 10
MAX_SEARCH_RESULTS = 100
MAX_RESET_DAY = 31
GH_HOSTS = frozenset(
    {"boards.greenhouse.io", "job-boards.greenhouse.io", "job-boards.eu.greenhouse.io"}
)
ATS_HOSTS = {
    "lever": "jobs.lever.co",
    "ashby": "jobs.ashbyhq.com",
    "smartrecruiters": "jobs.smartrecruiters.com",
}


def _config(settings: Settings) -> dict[str, Any]:
    cfg = load_sources(settings).get("firecrawl") or {}
    if cfg.get("enabled"):
        for key in (
            "monthly_credit_limit",
            "daily_credit_limit",
            "searches_per_day",
            "search_cache_hours",
            "search_limit",
            "search_credits_per_ten",
            "scrape_credits",
        ):
            if type(cfg.get(key)) is not int or cfg[key] <= 0:
                raise ValueError(f"firecrawl.{key} must be a positive integer")
        if cfg["search_limit"] > MAX_SEARCH_RESULTS:
            raise ValueError("firecrawl.search_limit exceeds the MCP limit")
        if not 1 <= int(cfg.get("budget_reset_day", 1)) <= MAX_RESET_DAY:
            raise ValueError("invalid firecrawl.budget_reset_day")
    return cfg


def _load(conn: sqlite3.Connection) -> dict[str, Any]:
    state = json.loads(read_state(conn, STATE_KEY) or '{"requests": {}, "leads": {}}')
    if not isinstance(state.get("requests"), dict) or not isinstance(state.get("leads"), dict):
        raise ValueError("invalid Firecrawl ledger; do not reset it to bypass the budget")
    return state


def _save(conn: sqlite3.Connection, state: dict[str, Any]) -> None:
    write_state(conn, STATE_KEY, json.dumps(state, sort_keys=True), now_iso())


def _period(cfg: dict[str, Any]) -> str:
    reference = now()
    reset_day = int(cfg.get("budget_reset_day", 1))
    day = min(reset_day, calendar.monthrange(reference.year, reference.month)[1])
    if reference.day < day:
        reference = reference.replace(day=1) - timedelta(days=1)
        day = min(reset_day, calendar.monthrange(reference.year, reference.month)[1])
    return reference.replace(day=day).date().isoformat()


def _budget(state: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    period = _period(cfg)
    requests = list(state["requests"].values())
    current = [r for r in requests if period in {r["period"], r.get("resolved_period")}]
    allocated = sum(r["credits"] for r in current)
    daily = sum(
        r["credits"]
        for r in requests
        if now().date().isoformat() in {r["day"], r.get("resolved_day")}
    )
    limit = int(cfg.get("monthly_credit_limit", 0))
    return {
        "enabled": bool(cfg.get("enabled")),
        "period_start_utc": period,
        "credit_limit": limit,
        "allocated_credits": allocated,
        "reported_credits": sum(r.get("reported_credits", 0) for r in current),
        "remaining_local_credits": max(0, limit - allocated),
        "allocated_today": daily,
        "pending_requests": [r["id"] for r in requests if r["status"] == "pending"],
        "account_balance_known": False,
        "account_wide_limit": False,
        "lead_counts": {
            status: sum(lead["status"] == status for lead in state["leads"].values())
            for status in {lead["status"] for lead in state["leads"].values()}
        },
    }


def budget_status(conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    return _budget(_load(conn), _config(settings))


def _safe_url(value: str, cfg: dict[str, Any]) -> str:
    try:
        url = urlsplit(value)
        host = url.hostname or ""
        if (
            url.scheme not in {"https", "http"}
            or "." not in host
            or url.username
            or url.password
            or url.port not in {None, 80, 443}
            or host.endswith((".localhost", ".local", ".internal"))
        ):
            return ""
        if any(
            host == domain or host.endswith("." + domain)
            for domain in cfg.get("blocked_domains", [])
        ):
            return ""
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return value
        return ""
    except ValueError:
        return ""


def _url_key(url: str, settings: Settings) -> str:
    value = canonical_url(url, settings.section("normalize").get("keep_query", []))
    parsed = urlsplit(value)
    if parsed.hostname in GH_HOSTS:
        value = parsed._replace(netloc="boards.greenhouse.io").geturl()
    return value


def _company_for(url: str, settings: Settings, cfg: dict[str, Any]) -> str | None:
    parsed = urlsplit(url)
    host, path = parsed.hostname, parsed.path.lower()
    for entry in cfg.get("official_sites", []):
        prefix = urlsplit(entry["url_prefix"])
        if host == prefix.hostname and path.startswith(prefix.path.lower()):
            return str(entry["company"])
    for company in load_companies(settings):
        if not company.enabled:
            continue
        token = (company.token or "").lower()
        if (
            company.ats == "greenhouse" and host in GH_HOSTS and path.startswith(f"/{token}/jobs/")
        ) or (host == ATS_HOSTS.get(company.ats) and path.startswith(f"/{token}/")):
            return company.name
        if (
            company.ats == "workday"
            and host == f"{company.tenant}.{company.wd}.myworkdayjobs.com"
            and f"/{company.site}/job/".lower() in path
        ):
            return company.name
        if company.host == host and re.search(r"job|career|position", path):
            return company.name
        if re.search(r"job|career|vacanc|opportun", path) and any(
            host == domain or (host or "").endswith("." + domain) for domain in company.domains
        ):
            return company.name
    return None


def _known_urls(conn: sqlite3.Connection, settings: Settings) -> set[str]:
    return {
        _url_key(row["url"], settings)
        for row in conn.execute("SELECT url FROM jobs UNION SELECT url FROM job_sources")
    }


def _next_scrape(conn, settings, cfg, state) -> dict[str, Any] | None:
    known = _known_urls(conn, settings)
    for key, lead in state["leads"].items():
        if lead["status"] not in {"discovered", "unverified_source"}:
            continue
        if key in known:
            lead["status"] = "known"
            continue
        company = _company_for(lead["url"], settings, cfg)
        if not company or not _safe_url(lead["url"], cfg):
            lead["status"] = "unverified_source"
            continue
        return {
            "tool": "firecrawl_scrape",
            "company": company,
            "lead_key": key,
            "credits": cfg["scrape_credits"],
            "arguments": {
                "url": lead["url"],
                "formats": ["markdown"],
                "maxAge": 0,
                "proxy": "basic",
                "parsers": [],
                "onlyMainContent": True,
            },
        }
    return None


def _next_search(cfg, state) -> dict[str, Any] | None:
    searches = [r for r in state["requests"].values() if r["tool"] == "firecrawl_search"]
    if sum(r["day"] == now().date().isoformat() for r in searches) >= cfg["searches_per_day"]:
        return None
    cutoff = to_iso(now() - timedelta(hours=cfg["search_cache_hours"]))
    recent = {r["arguments"]["query"] for r in searches if r["at"] >= cutoff}
    query = next((q for q in cfg.get("queries", []) if q not in recent), None)
    if not query:
        return None
    blocks = (cfg["search_limit"] + RESULT_BLOCK_SIZE - 1) // RESULT_BLOCK_SIZE
    return {
        "tool": "firecrawl_search",
        "credits": blocks * cfg["search_credits_per_ten"],
        "arguments": {
            "query": query,
            "limit": cfg["search_limit"],
            "sources": [{"type": "web"}],
            "excludeDomains": cfg.get("blocked_domains", []),
        },
    }


def plan_request(conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    cfg = _config(settings)
    with transaction(conn):
        state = _load(conn)
        budget = _budget(state, cfg)
        if not budget["enabled"] or budget["pending_requests"]:
            return {
                "request": None,
                "budget": budget,
                "reason": "disabled or unresolved MCP request",
            }
        request = _next_scrape(conn, settings, cfg, state) or _next_search(cfg, state)
        if not request:
            _save(conn, state)
            return {
                "request": None,
                "budget": _budget(state, cfg),
                "reason": "no uncached verified leads or due searches",
            }
        available = min(
            budget["remaining_local_credits"], cfg["daily_credit_limit"] - budget["allocated_today"]
        )
        if request["credits"] > available:
            return {"request": None, "budget": budget, "reason": "local credit budget reached"}
        request.update(
            id=uuid4().hex,
            at=now_iso(),
            day=now().date().isoformat(),
            period=budget["period_start_utc"],
            status="pending",
        )
        directory = settings.data_dir / RECEIPT_DIR
        directory.mkdir(parents=True, exist_ok=True)
        request["receipt_path"] = str(directory / f"{request['id']}.json")
        state["requests"][request["id"]] = request
        _save(conn, state)
        return {
            "request": request,
            "budget": _budget(state, cfg),
            "instruction": "Call once; ingest the receipt or mark failed before planning again.",
        }


def fail_request(conn: sqlite3.Connection, settings: Settings, request_id: str) -> dict[str, Any]:
    with transaction(conn):
        state = _load(conn)
        request = state["requests"].get(request_id)
        if not request:
            raise ValueError("unknown Firecrawl request")
        if request["status"] == "pending":
            request.update(
                status="failed",
                resolved_period=_period(_config(settings)),
                resolved_day=now().date().isoformat(),
            )
            if request.get("lead_key"):
                state["leads"][request["lead_key"]]["status"] = "needs_review"
            _save(conn, state)
        return {"status": request["status"], "credits_retained": request["credits"]}


def _envelope(receipt: dict[str, Any]) -> dict[str, Any]:
    response = receipt.get("response") if isinstance(receipt, dict) else None
    if not isinstance(response, dict):
        raise ValueError("receipt needs the unmodified MCP JSON response")
    if isinstance(response.get("content"), list):
        content = response["content"]
        if (
            not content
            or not isinstance(content[0], dict)
            or not isinstance(content[0].get("text"), str)
        ):
            raise ValueError("MCP text response is missing")
        response = json.loads(content[0]["text"])
    if not isinstance(response, dict):
        raise ValueError("unexpected MCP response shape")
    if response.get("success") is False or response.get("isError"):
        raise ValueError("MCP reported a failure; mark the request failed")
    return response


def _response(receipt: dict[str, Any]) -> dict[str, Any]:
    response = _envelope(receipt)
    data = response.get("data", response)
    if not isinstance(data, dict):
        raise ValueError("unexpected MCP response shape")
    return data


def _search_results(data, receipt, request, cfg, settings, state) -> list[int]:
    results = data.get("web")
    if not isinstance(results, list) or len(results) > request["arguments"]["limit"]:
        raise ValueError("invalid web search results")
    if receipt.get("jobs"):
        raise ValueError("search snippets cannot be imported as verified jobs")
    for item in results:
        url = _safe_url(str(item.get("url") or ""), cfg)
        if url:
            state["leads"].setdefault(
                _url_key(url, settings),
                {
                    "url": url,
                    "title": str(item.get("title") or ""),
                    "status": "discovered",
                    "search_request_id": request["id"],
                },
            )
    return []


def _flat(text: str) -> str:
    return " ".join(text.split()).casefold()


def _verified_posting(data, facts, request, cfg, settings) -> JobPosting:
    body = str(data.get("markdown") or "")
    metadata = data.get("metadata") or {}
    url = str(metadata.get("url") or metadata.get("sourceURL") or request["arguments"]["url"])
    if metadata.get("statusCode") != 200 or not _safe_url(url, cfg):
        raise ValueError("posting was not verified with a successful live fetch")
    if (
        _url_key(url, settings) != request["lead_key"]
        or _company_for(url, settings, cfg) != request["company"]
    ):
        raise ValueError("posting redirected; verify the new official URL separately")
    if any(_flat(phrase) in _flat(body) for phrase in cfg.get("closed_phrases", [])):
        raise ValueError("posting appears closed; do not import it")
    if facts.get("is_job_posting") is not True or facts.get("is_open") is not True:
        raise ValueError("explicit verification of an open individual posting is required")
    if facts.get("company") != request["company"]:
        raise ValueError("company does not match the verified source")
    for key in ("title", "description_md"):
        value = facts.get(key)
        if not isinstance(value, str) or not value.strip() or _flat(value) not in _flat(body):
            raise ValueError(
                f"{key} must be copied from the live posting, not invented or paraphrased"
            )
    if len(facts["description_md"]) < int(cfg.get("min_description_chars", 1)):
        raise ValueError("full job description required")
    location = facts.get("location")
    if location and _flat(str(location)) not in _flat(body):
        raise ValueError("location must appear in the live posting")
    if type(facts.get("is_remote", False)) is not bool:
        raise ValueError("is_remote must be a boolean")
    return JobPosting(
        source="firecrawl",
        source_job_id=hashlib.sha256(request["lead_key"].encode()).hexdigest(),
        company=request["company"],
        title=facts["title"],
        url=url,
        location=location,
        is_remote=facts.get("is_remote", False),
        description_md=body,
        raw={
            "request_id": request["id"],
            "verified_at": now_iso(),
            "eligibility_not_assumed": True,
        },
    )


def _receipt(request: dict[str, Any], response_file: Path | None) -> dict[str, Any]:
    path = Path(request["receipt_path"])
    if response_file is not None:
        if path.exists():
            raise ValueError("receipt already exists; inspect it instead of overwriting evidence")
        raw = json.loads(response_file.read_text(encoding="utf-8"))
        receipt = {"response": raw, "jobs": []}
        _envelope(receipt)
        path.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
    return json.loads(path.read_text(encoding="utf-8"))


def _settle(request, receipt, cfg, job_ids) -> None:
    reported = _envelope(receipt).get("creditsUsed")
    if type(reported) is int and reported >= 0:
        request["reported_credits"] = reported
        request["credits"] = max(request["credits"], reported)
    request.update(
        status="completed",
        completed_at=now_iso(),
        job_ids=job_ids,
        resolved_period=_period(cfg),
        resolved_day=now().date().isoformat(),
    )


def complete_request(
    conn: sqlite3.Connection,
    settings: Settings,
    request_id: str,
    *,
    response_file: Path | None = None,
) -> dict[str, Any]:
    cfg = _config(settings)
    with transaction(conn):
        state = _load(conn)
        request = state["requests"].get(request_id)
        if not request:
            raise ValueError("unknown Firecrawl request")
        if request["status"] != "pending":
            return {"already_processed": True, "status": request["status"]}
        receipt = _receipt(request, response_file)
        data = _response(receipt)
        if request["tool"] == "firecrawl_search":
            job_ids = _search_results(data, receipt, request, cfg, settings, state)
        else:
            facts = receipt.get("jobs") or []
            if not isinstance(facts, list) or len(facts) > 1:
                raise ValueError("a scrape receipt may describe at most one individual job")
            postings = [_verified_posting(data, fact, request, cfg, settings) for fact in facts]
            job_ids = [
                jobs_repo.upsert_job(
                    conn, p, build_normalizer(settings), source_priority(settings)
                ).job_id
                for p in postings
            ]
            state["leads"][request["lead_key"]].update(
                status="imported" if job_ids else "needs_review", scrape_request_id=request_id
            )
        _settle(request, receipt, cfg, job_ids)
        _save(conn, state)
        return {"job_ids": job_ids, "budget": _budget(state, cfg)}


def review_leads(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        lead
        for lead in _load(conn)["leads"].values()
        if lead["status"] in {"discovered", "unverified_source", "needs_review"}
    ]
