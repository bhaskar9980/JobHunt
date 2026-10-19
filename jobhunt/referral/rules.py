"""config/referrals.yaml: ranking weights, token lists, follow-up schedule."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jobhunt.config import REFERRALS_FILE, load_yaml

DEFAULT_WEIGHTS: dict[str, int] = {
    "same_team_or_role": 40,
    "engineer": 30,
    "recruiter": 25,
    "manager": 20,
    "recent_connection": 10,
    "prior_reply": 15,
    "cooldown_penalty": -100,
}
DEFAULT_CONNECTED_WITHIN_YEARS = 2
DEFAULT_COOLDOWN_DAYS = 60
DEFAULT_MAX_SUGGESTIONS = 5
DEFAULT_FOLLOWUP_DAYS = (7, 14)
DEFAULT_ROUTE_ENABLED = True
DEFAULT_ROUTE_MIN_CONTACT_SCORE = 50
DEFAULT_ROUTE_WAIT_DAYS = 5
DEFAULT_ROUTE_POSTED_WITHIN_DAYS = 21
DAYS_PER_YEAR = 365


@dataclass(slots=True)
class ReferralConfig:
    weights: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    connected_within_years: int = DEFAULT_CONNECTED_WITHIN_YEARS
    cooldown_days: int = DEFAULT_COOLDOWN_DAYS
    max_suggestions: int = DEFAULT_MAX_SUGGESTIONS
    followup_days: list[int] = field(default_factory=lambda: list(DEFAULT_FOLLOWUP_DAYS))
    role_tokens: list[str] = field(default_factory=list)
    engineer_tokens: list[str] = field(default_factory=list)
    recruiter_tokens: list[str] = field(default_factory=list)
    manager_tokens: list[str] = field(default_factory=list)
    resume_link: str = ""
    default_one_liner: str = ""
    route_enabled: bool = DEFAULT_ROUTE_ENABLED
    route_min_contact_score: int = DEFAULT_ROUTE_MIN_CONTACT_SCORE
    route_wait_days: int = DEFAULT_ROUTE_WAIT_DAYS
    route_posted_within_days: int = DEFAULT_ROUTE_POSTED_WITHIN_DAYS

    def weight(self, key: str) -> int:
        return int(self.weights.get(key, DEFAULT_WEIGHTS.get(key, 0)))

    @property
    def connected_within_days(self) -> int:
        return self.connected_within_years * DAYS_PER_YEAR

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReferralConfig:
        routing = data.get("routing") or {}
        weights = dict(DEFAULT_WEIGHTS)
        weights.update({str(k): int(v) for k, v in (data.get("weights") or {}).items()})
        return cls(
            weights=weights,
            connected_within_years=int(
                data.get("connected_within_years", DEFAULT_CONNECTED_WITHIN_YEARS)
            ),
            cooldown_days=int(data.get("cooldown_days", DEFAULT_COOLDOWN_DAYS)),
            max_suggestions=int(data.get("max_suggestions", DEFAULT_MAX_SUGGESTIONS)),
            followup_days=[int(d) for d in data.get("followup_days", DEFAULT_FOLLOWUP_DAYS)],
            role_tokens=_lower_list(data.get("role_tokens")),
            engineer_tokens=_lower_list(data.get("engineer_tokens")),
            recruiter_tokens=_lower_list(data.get("recruiter_tokens")),
            manager_tokens=_lower_list(data.get("manager_tokens")),
            resume_link=str(data.get("resume_link") or ""),
            default_one_liner=str(data.get("default_one_liner") or ""),
            route_enabled=bool(routing.get("enabled", DEFAULT_ROUTE_ENABLED)),
            route_min_contact_score=int(
                routing.get("min_contact_score", DEFAULT_ROUTE_MIN_CONTACT_SCORE)
            ),
            route_wait_days=int(routing.get("wait_days", DEFAULT_ROUTE_WAIT_DAYS)),
            route_posted_within_days=int(
                routing.get("posted_within_days", DEFAULT_ROUTE_POSTED_WITHIN_DAYS)
            ),
        )

    @classmethod
    def load(cls, config_dir: Path) -> ReferralConfig:
        return cls.from_dict(load_yaml(config_dir / REFERRALS_FILE))


def _lower_list(values: Any) -> list[str]:
    return [str(v).lower() for v in (values or [])]
