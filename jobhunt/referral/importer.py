"""Import LinkedIn's official Connections.csv export into the contacts table.

The file starts with a "Notes:" preamble (a few lines about missing emails), a blank line, then
the header row. We locate the header by content so the preamble length does not matter.
"""

from __future__ import annotations

import csv
import io
import logging
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from jobhunt.normalize import Normalizer
from jobhunt.repo.contacts import ContactRecord, upsert_contact

log = logging.getLogger(__name__)

SOURCE_LINKEDIN_EXPORT = "linkedin_export"
HEADER_COLUMNS = (
    "First Name",
    "Last Name",
    "URL",
    "Email Address",
    "Company",
    "Position",
    "Connected On",
)
_HEADER_MARKERS = ("first name", "last name", "url")
_CONNECTED_ON_FORMATS = ("%d %b %Y", "%d %B %Y", "%Y-%m-%d", "%m/%d/%Y")


class ConnectionsFormatError(ValueError):
    """The file does not look like a LinkedIn Connections export."""


@dataclass(slots=True)
class ImportSummary:
    rows: int = 0
    created: int = 0
    updated: int = 0
    skipped_no_name: int = 0


def read_connections(csv_path: Path) -> list[dict[str, str]]:
    """Rows from the export as dicts keyed by the header names, preamble skipped."""
    text = csv_path.read_text(encoding="utf-8-sig", errors="replace")
    lines = text.splitlines()
    header_index = _find_header(lines)
    reader = csv.DictReader(io.StringIO("\n".join(lines[header_index:])))
    return [
        {str(k).strip(): (v or "").strip() for k, v in row.items() if k is not None}
        for row in reader
        if any((v or "").strip() for v in row.values())
    ]


def _find_header(lines: list[str]) -> int:
    for index, line in enumerate(lines):
        lowered = line.lower()
        if all(marker in lowered for marker in _HEADER_MARKERS):
            return index
    raise ConnectionsFormatError("no 'First Name,Last Name,URL,...' header row found")


def parse_connected_on(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    for fmt in _CONNECTED_ON_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()  # noqa: DTZ007 - date only
        except ValueError:
            continue
    log.debug("unparsed Connected On value %r", text)
    return None


def canonical_company(
    name: str | None, norm: Normalizer, aliases: Mapping[str, list[str]]
) -> str | None:
    """Normalise a LinkedIn company string and fold known aliases onto the canonical employer."""
    if not name or not name.strip():
        return None
    company_norm = norm.company(name)
    reverse = alias_index(norm, aliases)
    return reverse.get(company_norm, company_norm)


def alias_index(norm: Normalizer, aliases: Mapping[str, list[str]]) -> dict[str, str]:
    """alias_norm -> canonical_norm, including the canonical name mapping to itself."""
    index: dict[str, str] = {}
    for canonical, names in aliases.items():
        canonical_norm = norm.company(canonical)
        index[canonical_norm] = canonical_norm
        for alias in names:
            index[norm.company(alias)] = canonical_norm
    return index


def to_record(
    row: Mapping[str, str], norm: Normalizer, aliases: Mapping[str, list[str]]
) -> ContactRecord | None:
    name = f"{row.get('First Name', '')} {row.get('Last Name', '')}".strip()
    if not name:
        return None
    company = row.get("Company") or None
    return ContactRecord(
        name=name,
        linkedin_url=row.get("URL") or None,
        company=company,
        company_norm=canonical_company(company, norm, aliases),
        title=row.get("Position") or None,
        email=row.get("Email Address") or None,
        connected_on=parse_connected_on(row.get("Connected On")),
        source=SOURCE_LINKEDIN_EXPORT,
    )


def import_connections(
    conn: sqlite3.Connection,
    csv_path: Path,
    norm: Normalizer,
    aliases: Mapping[str, list[str]],
) -> ImportSummary:
    summary = ImportSummary()
    rows = read_connections(csv_path)
    with conn:
        for row in rows:
            summary.rows += 1
            record = to_record(row, norm, aliases)
            if record is None:
                summary.skipped_no_name += 1
                continue
            _, created = upsert_contact(conn, record)
            if created:
                summary.created += 1
            else:
                summary.updated += 1
    log.info(
        "imported %s: %d rows, %d new, %d updated, %d skipped",
        csv_path.name,
        summary.rows,
        summary.created,
        summary.updated,
        summary.skipped_no_name,
    )
    return summary
