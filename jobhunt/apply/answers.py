"""Answer bank: maps screening questions to stored answers, fuzzy-matches, and learns new ones."""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from rapidfuzz import fuzz, process

from jobhunt.config import ConfigError, load_yaml

log = logging.getLogger(__name__)

DEFAULT_FUZZY_THRESHOLD = 88
DEFAULT_OPTION_THRESHOLD = 80
PERCENT = 100.0
PLACEHOLDER_EMAIL_DOMAIN = "@example.com"
LEARNED_KEY = "learned"
QUESTION_MAP_KEY = "question_map"
WORK_AUTH_KEY = "work_authorisation"
DEFAULT_COUNTRY = "default"

_NOISE = re.compile(r"\*|\(required\)|\(optional\)|\brequired\b|\boptional\b", re.I)
_NON_WORD = re.compile(r"[^\w\s#+.]+")
_SPACES = re.compile(r"\s+")
_EEO = re.compile(
    r"\b(gender|race|ethnic|ethnicity|veteran|disability|disabled|sexual orientation|pronouns?|"
    r"self[- ]identif|hispanic|latino)\b",
    re.I,
)
_DECLINE = re.compile(
    r"decline|don'?t wish|do not wish|prefer not|rather not|not to (?:say|answer)", re.I
)
_COUNTRY_HINTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(united states|usa|u\.s\.a?|us\b|america)", re.I), "US"),
    (re.compile(r"\b(united kingdom|uk|u\.k\.|britain|england)\b", re.I), "GB"),
    (re.compile(r"\bindia\b", re.I), "IN"),
    (re.compile(r"\bcanada\b", re.I), "CA"),
    (re.compile(r"\b(germany|deutschland)\b", re.I), "DE"),
    (re.compile(r"\b(netherlands|holland)\b", re.I), "NL"),
    (re.compile(r"\baustralia\b", re.I), "AU"),
    (re.compile(r"\bsingapore\b", re.I), "SG"),
    (re.compile(r"\b(ireland|eire)\b", re.I), "IE"),
)


class AnswerBankError(ConfigError):
    """Raised when config/answers.yaml is missing or malformed."""


@dataclass(slots=True)
class Match:
    value: Any
    source: str  # learned | rule | fuzzy
    rule: str | None = None
    confidence: float = 1.0


def normalise_question(text: str) -> str:
    lowered = _NOISE.sub(" ", (text or "").lower())
    lowered = _NON_WORD.sub(" ", lowered)
    return _SPACES.sub(" ", lowered).strip()


def detect_country(text: str) -> str | None:
    for pattern, code in _COUNTRY_HINTS:
        if pattern.search(text or ""):
            return code
    return None


def is_eeo_question(label: str, section: str | None = None) -> bool:
    return bool(_EEO.search(label or "")) or bool(section and _EEO.search(section))


def decline_option(options: list[str]) -> str | None:
    for option in options:
        if _DECLINE.search(option):
            return option
    return None


def coerce_to_option(value: Any, options: list[str], threshold: int) -> str | None:
    if not options:
        return None
    if isinstance(value, bool):
        target = "yes" if value else "no"
        for option in options:
            if option.strip().lower().startswith(target):
                return option
        return None
    best = process.extractOne(str(value), options, scorer=fuzz.WRatio)
    if best and best[1] >= threshold:
        return str(best[0])
    return None


@dataclass(slots=True)
class AnswerBank:
    data: dict[str, Any]
    path: Path | None = None

    @classmethod
    def load(cls, path: Path) -> AnswerBank:
        if not path.exists():
            raise AnswerBankError(
                f"{path} not found. Copy config/answers.example.yaml to config/answers.yaml "
                "and fill it in."
            )
        try:
            data = load_yaml(path)
        except ConfigError as exc:
            raise AnswerBankError(str(exc)) from None
        data.setdefault(LEARNED_KEY, {})
        return cls(data=data, path=path)

    # ------------------------------------------------------------------ lookups

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def placeholder_issues(self) -> list[str]:
        """Values still at their answers.example.yaml defaults; never submit with these."""
        issues: list[str] = []
        email = str(self.get("profile.email") or "")
        if not email or email.endswith(PLACEHOLDER_EMAIL_DOMAIN):
            issues.append("profile.email is a placeholder")
        digits = re.sub(r"\D", "", str(self.get("profile.phone.number") or ""))
        if not digits or set(digits) == {"0"}:
            issues.append("profile.phone.number is a placeholder")
        if not _positive_amount(self.get("compensation.current_ctc_lpa")):
            issues.append("compensation.current_ctc_lpa must be a positive finite amount")
        expected_key = (
            "expected_fixed_lpa"
            if self.get("compensation.expected_fixed_lpa") is not None
            else "expected_ctc_lpa"
        )
        expected = self.get(f"compensation.{expected_key}")
        if not isinstance(expected, dict) or not _positive_amount(expected.get("min")):
            issues.append(f"compensation.{expected_key}.min must be a positive finite amount")
        return issues

    @property
    def fuzzy_threshold(self) -> int:
        return int(self.get("matching.fuzzy_threshold", DEFAULT_FUZZY_THRESHOLD))

    @property
    def option_threshold(self) -> int:
        return int(self.get("matching.option_threshold", DEFAULT_OPTION_THRESHOLD))

    def match(self, question: str, *, country: str | None = None) -> Match | None:
        norm = normalise_question(question)
        if not norm:
            return None
        learned = self.data.get(LEARNED_KEY) or {}
        if norm in learned:
            return Match(learned[norm].get("answer"), "learned")
        for index, rule in enumerate(self.data.get(QUESTION_MAP_KEY) or []):
            if _rule_hits(rule, norm):
                value = self._rule_value(rule, question, country)
                return (
                    Match(value, "rule", rule=f"question_map[{index}]")
                    if value is not None
                    else None
                )
        return self._fuzzy(norm, learned)

    def _fuzzy(self, norm: str, learned: dict[str, Any]) -> Match | None:
        if not learned:
            return None
        best = process.extractOne(norm, list(learned.keys()), scorer=fuzz.token_set_ratio)
        if best and best[1] >= self.fuzzy_threshold:
            return Match(learned[best[0]].get("answer"), "fuzzy", confidence=best[1] / PERCENT)
        return None

    def _rule_value(self, rule: dict[str, Any], question: str, country: str | None) -> Any:
        if "value" in rule:
            return self.render(self.get(str(rule["value"])))
        if "per_country" in rule:
            code = detect_country(question) or country
            if code is None and self.get("policies.require_explicit_country", False):
                return None
            code = code or DEFAULT_COUNTRY
            table = self.get(WORK_AUTH_KEY) or {}
            entry = table.get(code) or table.get(DEFAULT_COUNTRY) or {}
            return entry.get(str(rule["per_country"]))
        return None

    def render(self, value: Any) -> Any:
        """Turn structured values (compensation ranges) into form-ready text."""
        if isinstance(value, dict) and {"min", "max"} <= set(value):
            style = str(self.get("compensation.answer_style", "range"))
            currency = str(self.get("compensation.currency", "INR"))
            if style == "decline":
                return "Open to discussion based on the role"
            if style == "min":
                return f"{value['min']} LPA {currency}"
            return f"{value['min']}-{value['max']} LPA {currency}"
        return value

    # ------------------------------------------------------------------ learning

    def learn(self, question: str, answer: Any, job_id: int | None = None) -> str:
        norm = normalise_question(question)
        self.data.setdefault(LEARNED_KEY, {})[norm] = {
            "answer": answer,
            "question": question.strip(),
            "job_id": job_id,
            "added_on": date.today().isoformat(),
        }
        log.info("learned answer for %r", norm)
        return norm

    def save(self) -> None:
        if self.path is None:
            raise AnswerBankError("answer bank has no path to save to")
        self.path.write_text(
            yaml.safe_dump(self.data, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )


def _positive_amount(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        number = float(value)
        return math.isfinite(number) and number > 0
    except (TypeError, ValueError, OverflowError):
        return False


def _rule_hits(rule: dict[str, Any], norm: str) -> bool:
    keywords = rule.get("any") or []
    if any(str(k).lower() in norm for k in keywords):
        return True
    pattern = rule.get("regex")
    return bool(pattern) and re.search(str(pattern), norm, re.I) is not None
