"""Per-ATS fillers. Each module exposes a `FILLER` object implementing fillers.base.AtsFiller."""

from __future__ import annotations

import importlib

from jobhunt.apply.fillers.base import AtsFiller

_MODULES = {
    "greenhouse": "jobhunt.apply.fillers.greenhouse",
    "lever": "jobhunt.apply.fillers.lever",
    "ashby": "jobhunt.apply.fillers.ashby",
    "workday": "jobhunt.apply.fillers.workday",
}


def filler_for(ats: str | None) -> AtsFiller | None:
    path = _MODULES.get((ats or "").lower())
    if path is None:
        return None
    module = importlib.import_module(path)
    return module.FILLER


def supported() -> list[str]:
    return sorted(_MODULES)
