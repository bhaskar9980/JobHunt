"""Shared fixtures: in-memory DB, normalizer, posting factory. No test touches the network."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from jobhunt.db import MEMORY_DB, connect, init_db
from jobhunt.models import JobPosting
from jobhunt.normalize import Normalizer

FIXTURES = Path(__file__).parent / "fixtures"

COMPANY_SUFFIXES = frozenset(
    {"inc", "llc", "ltd", "limited", "pvt", "private", "corp", "co", "technologies", "india"}
)
LOCATION_ALIASES = {
    "chennai": "chennai",
    "bengaluru": "bengaluru",
    "bangalore": "bengaluru",
    "karnataka": "bengaluru",
    "hyderabad": "hyderabad",
    "pune": "pune",
    "london": "london",
    "san francisco": "sf",
    "india": "india",
}
PRIORITY = {
    "default": 10,
    "greenhouse": 30,
    "lever": 30,
    "ashby": 30,
    "hn": 20,
    "linkedin_email": 10,
}


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = connect(MEMORY_DB)
    init_db(connection)
    yield connection
    connection.close()


@pytest.fixture
def norm() -> Normalizer:
    return Normalizer(
        company_suffixes=COMPANY_SUFFIXES,
        company_drop_anywhere=frozenset({"careers", "jobs"}),
        keep_query=frozenset({"gh_jid", "jobid", "id"}),
        location_aliases=LOCATION_ALIASES,
    )


@pytest.fixture
def priority() -> dict[str, int]:
    return dict(PRIORITY)


@pytest.fixture
def make_posting() -> Callable[..., JobPosting]:
    def _make(**overrides: Any) -> JobPosting:
        base: dict[str, Any] = {
            "source": "greenhouse",
            "source_job_id": "1001",
            "company": "Acme, Inc.",
            "title": "Software Engineer II",
            "url": "https://boards.greenhouse.io/acme/jobs/1001?gh_src=abc",
            "location": "Bengaluru, Karnataka, India",
            "is_remote": False,
            "apply_url": "https://boards.greenhouse.io/acme/jobs/1001#app",
            "ats_type": "greenhouse",
            "description_md": "Build .NET microservices with MongoDB and Kafka.",
        }
        base.update(overrides)
        return JobPosting(**base)

    return _make


@pytest.fixture
def load_fixture() -> Callable[[str], Any]:
    def _load(name: str) -> Any:
        path = FIXTURES / name
        text = path.read_text(encoding="utf-8")
        return json.loads(text) if path.suffix == ".json" else text

    return _load
