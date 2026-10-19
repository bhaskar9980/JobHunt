"""Filler contract shared by every ATS, plus answer resolution from the bank to a form schema."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from jobhunt.apply.answers import (
    AnswerBank,
    coerce_to_option,
    decline_option,
    is_eeo_question,
)
from jobhunt.apply.schema import FieldKind, FormField, FormSchema
from jobhunt.clock import now_iso

ANSWER_SOURCE_SYSTEM = "system"
ANSWER_SOURCE_EEO = "eeo_decline"
ANSWER_SOURCE_COVER = "cover_letter"
ANSWER_SOURCE_USER = "user"
ANSWERS_SCHEMA_VERSION = 1

_COVER_LETTER = re.compile(
    r"cover letter|why (?:do you want|are you interested|us|this role|join)", re.I
)
_RESUME = re.compile(r"resume|cv\b|curriculum", re.I)
_SENSITIVE = re.compile(r"\b(sex|accommodations?)\b", re.I)
_RACE = re.compile(r"(?:what is your |please select your )?(?:race(?:\s*/\s*ethnicity)?|ethnicity)")
_DISABILITY = re.compile(
    r"disability(?: status)?|do you (?:currently )?have a disability|"
    r"are you (?:currently )?disabled"
)
_DISABILITY_COMPOUND = re.compile(
    r"\b(history|historical|past|ever|never|previous\w*|had|record|accommodations?)\b", re.I
)
_EEO_SECTIONS = {
    "",
    "eeo",
    "demographics",
    "equal employment opportunity",
    "voluntary self-identification",
    "self-identification",
    "voluntary self-identification of disability",
}
_CONSENT = re.compile(
    r"\b(consent|agree\w*|accept|privacy|gdpr|marketing|promotional|terms|certif\w*|"
    r"authori[sz]e|background check|subscribe|permission|personal data|data processing)\b",
    re.I,
)
_APPLICATION_CONSENT = re.compile(
    r"(?:i (?:consent|agree) to (?:the )?processing (?:of )?my personal data|"
    r"do you consent to (?:the )?processing (?:of )?your personal data) "
    r"for (?:this (?:job )?application|(?:my|your) (?:job )?application|recruitment(?: purposes)?)"
)
_MARKETING_CONSENT = re.compile(
    r"(?:i (?:consent|agree) to receive|would you like to receive|"
    r"do you consent to (?:receive|receiving)) (?:marketing|promotional) emails"
)
_CONSENT_SECTIONS = {"", "consent", "applicant consent", "application", "application consent"}
_NUMBER = re.compile(r"-?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


class LoginRequired(RuntimeError):
    """The form is behind a login wall; the runner will wait for the human, then retry once."""


class FillerError(RuntimeError):
    """A filler could not complete a step (selector missing, validation failed)."""


class SubmissionNotStarted(FillerError):
    pass


@dataclass(slots=True)
class Answer:
    field_key: str
    label: str
    value: Any
    source: str
    option: str | None = None
    skipped: bool = False
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Answer:
        return cls(
            field_key=str(data["field_key"]),
            label=str(data.get("label", "")),
            value=data.get("value"),
            source=str(data.get("source", ANSWER_SOURCE_USER)),
            option=data.get("option"),
            skipped=bool(data.get("skipped", False)),
            reason=data.get("reason"),
        )


@dataclass(slots=True)
class FileInputs:
    resume_pdf: Path
    cover_letter_pdf: Path | None = None


@dataclass(slots=True)
class FillReport:
    filled: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Confirmation:
    url: str
    text: str
    at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AtsFiller(Protocol):
    KEY: str

    def matches(self, url: str) -> bool: ...

    def open(self, page: Any, url: str) -> None: ...

    def already_applied(self, page: Any) -> bool: ...

    def extract_schema(self, page: Any) -> FormSchema: ...

    def fill(
        self,
        page: Any,
        schema: FormSchema,
        answers: Mapping[str, Answer],
        profile: Mapping[str, Any],
        files: FileInputs,
    ) -> FillReport: ...

    def advance(self, page: Any) -> bool: ...

    """Move to the next page of a multi-page form. False when this was the last page."""

    def submit(self, page: Any) -> None: ...

    def confirm(self, page: Any, timeout_seconds: float) -> Confirmation | None: ...


# --------------------------------------------------------------------------- answers.json


@dataclass(slots=True)
class AnswerSet:
    job_id: int
    answers: dict[str, Answer] = field(default_factory=dict)
    unmatched: list[FormField] = field(default_factory=list)
    generated_at: str = field(default_factory=now_iso)

    @property
    def unmatched_required(self) -> list[FormField]:
        return [f for f in self.unmatched if f.required]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": ANSWERS_SCHEMA_VERSION,
            "job_id": self.job_id,
            "generated_at": self.generated_at,
            "answers": {k: a.to_dict() for k, a in self.answers.items()},
            "unmatched": [f.to_dict() for f in self.unmatched],
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, default=str), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> AnswerSet:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            job_id=int(data["job_id"]),
            answers={k: Answer.from_dict(v) for k, v in data.get("answers", {}).items()},
            unmatched=[FormField.from_dict(f) for f in data.get("unmatched", [])],
            generated_at=str(data.get("generated_at", now_iso())),
        )


def resolve_answers(
    schema: FormSchema,
    bank: AnswerBank,
    *,
    job_id: int,
    country: str | None = None,
    cover_letter: str | None = None,
) -> AnswerSet:
    """Map every custom field to an answer from the bank; collect what needs a human."""
    result = AnswerSet(job_id=job_id)
    for form_field in schema.fields:
        if form_field.system:
            continue
        answer = _resolve_field(form_field, bank, country, cover_letter)
        if answer is None:
            result.unmatched.append(form_field)
            continue
        result.answers[form_field.key] = answer
    return result


def _resolve_field(
    form_field: FormField, bank: AnswerBank, country: str | None, cover_letter: str | None
) -> Answer | None:
    if form_field.kind == FieldKind.FILE:
        return _file_answer(form_field, cover_letter)
    if is_eeo_question(form_field.label, form_field.section) or _SENSITIVE.search(
        f"{form_field.label} {form_field.section or ''}"
    ):
        return _eeo_answer(form_field, bank)
    if _CONSENT.search(f"{form_field.label} {form_field.section or ''}"):
        return _consent_answer(form_field, bank)
    if (
        cover_letter
        and _COVER_LETTER.search(form_field.label)
        and form_field.kind
        in (
            FieldKind.TEXTAREA,
            FieldKind.TEXT,
        )
    ):
        return Answer(form_field.key, form_field.label, cover_letter, ANSWER_SOURCE_COVER)
    match = bank.match(form_field.label, country=country)
    if match is None:
        return None if form_field.required else _skip(form_field, "optional, no answer in bank")
    return _with_option(form_field, match.value, match.source, bank.option_threshold)


def _explicit_text(text: str) -> str:
    return " ".join(text.casefold().split())


def _eeo_answer(form_field: FormField, bank: AnswerBank) -> Answer | None:
    if bank.get("policies.eeo") == "provided":
        provided = _provided_eeo_answer(form_field, bank)
        if provided is not None:
            return provided
    option = decline_option(form_field.options)
    if option or not form_field.required:
        return Answer(
            form_field.key,
            form_field.label,
            option,
            ANSWER_SOURCE_EEO,
            option=option,
            skipped=option is None,
            reason=None if option else "no decline option",
        )
    return None


def _provided_eeo_answer(form_field: FormField, bank: AnswerBank) -> Answer | None:
    if form_field.kind not in (FieldKind.SELECT, FieldKind.RADIO, FieldKind.MULTISELECT):
        return None
    label = _explicit_text(form_field.label).rstrip(" .?:*")
    section = _explicit_text(form_field.section or "")
    if _RACE.fullmatch(label):
        if section not in _EEO_SECTIONS and not _RACE.fullmatch(section):
            return None
        race = bank.get("demographics.race")
        if isinstance(race, str) and race.strip():
            return _explicit_answer(form_field, race, "demographics.race", {_explicit_text(race)})
    if _DISABILITY.fullmatch(label):
        if (
            section not in _EEO_SECTIONS and not _DISABILITY.fullmatch(section)
        ) or _DISABILITY_COMPOUND.search(" ".join(form_field.options)):
            return None
        disability = bank.get("demographics.disability")
        if isinstance(disability, bool):
            options = (
                {"yes", "i have a disability", "yes, i have a disability"}
                if disability
                else {
                    "no",
                    "i do not have a disability",
                    "no, i do not have a disability",
                    "i don't have a disability",
                    "no, i don't have a disability",
                }
            )
            return _explicit_answer(form_field, disability, "demographics.disability", options)
    return None


def _consent_answer(form_field: FormField, bank: AnswerBank) -> Answer | None:
    label = _explicit_text(form_field.label).rstrip(" .?:*")
    section = _explicit_text(form_field.section or "")
    if _APPLICATION_CONSENT.fullmatch(label):
        source = "policies.gdpr_consent"
        sections = {"privacy", "data privacy", "data protection", "gdpr"}
    elif _MARKETING_CONSENT.fullmatch(label):
        source = "policies.marketing_consent"
        sections = {"marketing", "marketing consent", "promotional emails"}
    else:
        return None
    if section not in _CONSENT_SECTIONS | sections:
        return None
    value = bank.get(source)
    if not isinstance(value, bool):
        return None
    options = (
        {"yes", "i agree", "i consent"} if value else {"no", "i do not agree", "i do not consent"}
    )
    return _explicit_answer(form_field, value, source, options)


def _explicit_answer(
    form_field: FormField, value: Any, source: str, options: set[str]
) -> Answer | None:
    if form_field.kind == FieldKind.CHECKBOX and isinstance(value, bool) and not form_field.options:
        return Answer(form_field.key, form_field.label, value, source)
    if form_field.kind in (FieldKind.SELECT, FieldKind.RADIO, FieldKind.MULTISELECT):
        for option in form_field.options:
            if _explicit_text(option) in options:
                return Answer(form_field.key, form_field.label, value, source, option=option)
    return None


def _file_answer(form_field: FormField, cover_letter: str | None) -> Answer | None:
    if _RESUME.search(form_field.label):
        return Answer(form_field.key, form_field.label, "resume", ANSWER_SOURCE_SYSTEM)
    if _COVER_LETTER.search(form_field.label):
        if cover_letter:
            return Answer(form_field.key, form_field.label, "cover_letter", ANSWER_SOURCE_COVER)
        return None if form_field.required else _skip(form_field, "no cover letter drafted")
    return None if form_field.required else _skip(form_field, "unknown file field")


def _finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return False
    try:
        return bool(_NUMBER.fullmatch(str(value))) and math.isfinite(float(value))
    except (ValueError, OverflowError):
        return False


def _with_option(form_field: FormField, value: Any, source: str, threshold: int) -> Answer | None:
    if form_field.kind == FieldKind.NUMBER and not _finite_number(value):
        return None
    if (
        form_field.kind in (FieldKind.SELECT, FieldKind.RADIO, FieldKind.MULTISELECT)
        and form_field.options
    ):
        option = coerce_to_option(value, form_field.options, threshold)
        if option is None:
            return None
        return Answer(form_field.key, form_field.label, value, source, option=option)
    if form_field.kind == FieldKind.CHECKBOX and not isinstance(value, bool):
        return None
    return Answer(form_field.key, form_field.label, value, source)


def _skip(form_field: FormField, reason: str) -> Answer:
    return Answer(
        form_field.key, form_field.label, None, ANSWER_SOURCE_SYSTEM, skipped=True, reason=reason
    )
