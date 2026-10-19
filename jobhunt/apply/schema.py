"""Form schema extracted from an application page, persisted as form_schema.json."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class FieldKind(StrEnum):
    TEXT = "text"
    TEXTAREA = "textarea"
    EMAIL = "email"
    PHONE = "phone"
    URL = "url"
    NUMBER = "number"
    SELECT = "select"
    MULTISELECT = "multiselect"
    RADIO = "radio"
    CHECKBOX = "checkbox"
    FILE = "file"
    DATE = "date"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class FormField:
    key: str
    label: str
    kind: FieldKind = FieldKind.TEXT
    required: bool = False
    options: list[str] = field(default_factory=list)
    selector: str | None = None
    section: str | None = None
    system: bool = False  # name/email/phone/resume style fields the filler handles itself

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["kind"] = self.kind.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FormField:
        return cls(
            key=str(data["key"]),
            label=str(data.get("label", "")),
            kind=FieldKind(data.get("kind", FieldKind.TEXT.value)),
            required=bool(data.get("required", False)),
            options=[str(o) for o in data.get("options", [])],
            selector=data.get("selector"),
            section=data.get("section"),
            system=bool(data.get("system", False)),
        )


@dataclass(slots=True)
class FormSchema:
    ats: str
    url: str
    fields: list[FormField] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"ats": self.ats, "url": self.url, "fields": [f.to_dict() for f in self.fields]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FormSchema:
        return cls(
            ats=str(data["ats"]),
            url=str(data["url"]),
            fields=[FormField.from_dict(f) for f in data.get("fields", [])],
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> FormSchema:
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def custom_fields(self) -> list[FormField]:
        return [f for f in self.fields if not f.system]

    def keys(self) -> set[str]:
        return {f.key for f in self.fields}

    def changed_fields(self, other: FormSchema) -> list[FormField]:
        known = {f.key: f for f in self.fields}
        attributes = ("label", "kind", "required", "options", "section", "system")
        return [
            f
            for f in other.fields
            if f.key not in known
            or any(getattr(f, attr) != getattr(known[f.key], attr) for attr in attributes)
        ]

    def diff_new(self, other: FormSchema) -> list[FormField]:
        """Fields present in `other` that were not in this schema (dynamic follow-ups)."""
        known = self.keys()
        return [f for f in other.fields if f.key not in known]
