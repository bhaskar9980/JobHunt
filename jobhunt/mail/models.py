"""Dataclasses shared by the inbox-sync modules, plus the mail_inbox JSON contract."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = 1
# One file per Gmail *message*; a thread keeps growing (confirmation, then rejection or
# interview invite), so the message id is the unit of ingestion.
INBOX_FILE_TEMPLATE = "{thread_id}_{gmail_id}.json"

KIND_CONFIRMATION = "confirmation"
KIND_REJECTION = "rejection"
KIND_INTERVIEW = "interview"
KIND_ASSESSMENT = "assessment"
KIND_OFFER = "offer"
KIND_RECRUITER = "recruiter_outreach"
KIND_UNCLASSIFIED = "unclassified"
KINDS: tuple[str, ...] = (
    KIND_CONFIRMATION,
    KIND_REJECTION,
    KIND_INTERVIEW,
    KIND_ASSESSMENT,
    KIND_OFFER,
    KIND_RECRUITER,
)

_ADDRESS = re.compile(r"<?([\w.+-]+@([\w-]+(?:\.[\w-]+)+))>?")


class MailFileError(ValueError):
    """Raised when a mail_inbox JSON file does not follow the contract."""


def parse_address(value: str) -> tuple[str | None, str | None]:
    """Return (email, domain) from a From header such as 'Ada <ada@acme.com>'."""
    match = _ADDRESS.search(value or "")
    if not match:
        return None, None
    return match.group(1).lower(), match.group(2).lower()


def inbox_file_name(thread_id: str, gmail_id: str) -> str:
    return INBOX_FILE_TEMPLATE.format(thread_id=thread_id, gmail_id=gmail_id)


def display_name(value: str) -> str:
    """'Ada Lovelace <ada@acme.com>' -> 'Ada Lovelace'; bare addresses give the local part."""
    head = (value or "").split("<", 1)[0].strip().strip('"')
    if head and "@" not in head:
        return head
    email, _ = parse_address(value)
    return email.split("@", 1)[0].replace(".", " ").title() if email else ""


@dataclass(slots=True)
class MailMessage:
    thread_id: str
    gmail_id: str
    sender: str
    from_name: str
    from_domain: str
    to: str
    date: str
    subject: str
    snippet: str
    body_text: str
    links: list[str] = field(default_factory=list)
    query_tag: str | None = None

    @property
    def text(self) -> str:
        return f"{self.subject}\n{self.body_text}".lower()

    @property
    def sender_email(self) -> str | None:
        return parse_address(self.sender)[0]

    @property
    def file_name(self) -> str:
        return inbox_file_name(self.thread_id, self.gmail_id)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MailMessage:
        if not isinstance(data, Mapping):
            raise MailFileError("top level must be an object")
        if data.get("schema_version") != SCHEMA_VERSION:
            raise MailFileError(f"schema_version must be {SCHEMA_VERSION}")
        thread_id = str(data.get("thread_id") or "").strip()
        if not thread_id:
            raise MailFileError("thread_id is required")
        gmail_id = str(data.get("gmail_id") or "").strip()
        if not gmail_id:
            raise MailFileError("gmail_id (the Gmail message id) is required")
        sender = str(data.get("from") or "")
        _, derived_domain = parse_address(sender)
        return cls(
            thread_id=thread_id,
            gmail_id=gmail_id,
            sender=sender,
            from_name=str(data.get("from_name") or display_name(sender)),
            from_domain=str(data.get("from_domain") or derived_domain or "").lower(),
            to=str(data.get("to") or ""),
            date=str(data.get("date") or ""),
            subject=str(data.get("subject") or ""),
            snippet=str(data.get("snippet") or ""),
            body_text=str(data.get("body_text") or ""),
            links=[str(link) for link in data.get("links") or []],
            query_tag=data.get("query_tag"),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["from"] = data.pop("sender")
        data["schema_version"] = SCHEMA_VERSION
        return data


@dataclass(slots=True)
class Classification:
    kind: str
    confidence: float
    evidence: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Candidate:
    job_id: int
    company: str
    title: str
    status: str
    score: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class MatchResult:
    job_id: int | None
    confidence: float
    candidates: list[Candidate] = field(default_factory=list)
    reason: str = ""

    @property
    def ambiguous(self) -> bool:
        return self.job_id is None and len(self.candidates) > 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "confidence": self.confidence,
            "reason": self.reason,
            "candidates": [c.to_dict() for c in self.candidates],
        }


@dataclass(slots=True)
class MailOutcome:
    thread_id: str
    kind: str
    job_id: int | None
    from_status: str | None = None
    to_status: str | None = None
    duplicate: bool = False
    note: str = ""

    @property
    def changed(self) -> bool:
        return self.to_status is not None and self.to_status != self.from_status
