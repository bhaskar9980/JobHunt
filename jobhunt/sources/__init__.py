"""Source registry. Company sources are keyed by ATS name; aggregators by their own key."""

from __future__ import annotations

import importlib
from types import ModuleType

COMPANY_SOURCES = (
    "greenhouse",
    "lever",
    "ashby",
    "smartrecruiters",
    "workday",
    "eightfold",
    "successfactors",
)
FULL_INVENTORY_SOURCES = frozenset({"greenhouse", "ashby"})
AGGREGATOR_SOURCES = {
    "hn_hiring": "hn",
    "remotive": "remotive",
    "wwr": "wwr",
    "jobfound": "jobfound",
    "tal_jobs_in_the_wild": "tal",
}

_MODULES = {
    "greenhouse": "jobhunt.sources.greenhouse",
    "lever": "jobhunt.sources.lever",
    "ashby": "jobhunt.sources.ashby",
    "smartrecruiters": "jobhunt.sources.smartrecruiters",
    "workday": "jobhunt.sources.workday",
    "eightfold": "jobhunt.sources.eightfold",
    "successfactors": "jobhunt.sources.successfactors",
    "hn": "jobhunt.sources.hn_hiring",
    "remotive": "jobhunt.sources.remotive",
    "wwr": "jobhunt.sources.wwr",
    "jobfound": "jobhunt.sources.jobfound",
    "tal": "jobhunt.sources.tal",
}


class UnknownSource(KeyError):
    """Raised when no module is registered for a source key."""


def get_module(key: str) -> ModuleType:
    path = _MODULES.get(key.lower())
    if path is None:
        raise UnknownSource(key)
    return importlib.import_module(path)


def known_sources() -> list[str]:
    return sorted(_MODULES)
