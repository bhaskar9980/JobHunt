"""Load config/mail_rules.yaml and expose typed accessors with defaults."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from jobhunt.config import MAIL_RULES_FILE, load_yaml

DEFAULT_AUTO_THRESHOLD = 0.80
DEFAULT_REVIEW_THRESHOLD = 0.50
DEFAULT_BODY_MAX_CHARS = 4000
DEFAULT_LOOKBACK_DAYS = 7
DEFAULT_MAX_LOOKBACK_DAYS = 30
DEFAULT_SUBJECT_BONUS = 0.15
DEFAULT_ATS_BOOST = 0.20
DEFAULT_TIE_MARGIN = 0.10
NEWER_THAN_PLACEHOLDER = "{newer_than}"


def load_mail_rules(config_dir: Path) -> dict[str, Any]:
    return load_yaml(config_dir / MAIL_RULES_FILE)


def thresholds(rules: Mapping[str, Any]) -> tuple[float, float]:
    """(auto, review) confidence thresholds."""
    block = rules.get("thresholds") or {}
    return (
        float(block.get("auto", DEFAULT_AUTO_THRESHOLD)),
        float(block.get("review", DEFAULT_REVIEW_THRESHOLD)),
    )


def body_max_chars(rules: Mapping[str, Any]) -> int:
    return int(rules.get("body_max_chars", DEFAULT_BODY_MAX_CHARS))


def default_lookback_days(rules: Mapping[str, Any]) -> int:
    return int(rules.get("default_lookback_days", DEFAULT_LOOKBACK_DAYS))


def max_lookback_days(rules: Mapping[str, Any]) -> int:
    return int(rules.get("max_lookback_days", DEFAULT_MAX_LOOKBACK_DAYS))


def ats_domains(rules: Mapping[str, Any]) -> frozenset[str]:
    return frozenset(str(d).lower() for d in rules.get("ats_domains") or [])


def is_ats_domain(domain: str | None, rules: Mapping[str, Any]) -> bool:
    if not domain:
        return False
    domain = domain.lower()
    return any(domain == d or domain.endswith("." + d) for d in ats_domains(rules))


def render_queries(rules: Mapping[str, Any], days: int) -> dict[str, str]:
    """Gmail search strings with the lookback filled in and whitespace collapsed."""
    rendered: dict[str, str] = {}
    for kind, query in (rules.get("queries") or {}).items():
        text = " ".join(str(query).split())
        rendered[str(kind)] = text.replace(NEWER_THAN_PLACEHOLDER, str(days))
    return rendered
