"""Contacts: people the user knows, imported from the LinkedIn Connections export or added by hand.

Callers manage transactions.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass


class ContactNotFound(LookupError):
    pass


@dataclass(slots=True)
class ContactRecord:
    """Everything we know about one person. None means "leave whatever is stored"."""

    name: str
    linkedin_url: str | None = None
    company: str | None = None
    company_norm: str | None = None
    title: str | None = None
    email: str | None = None
    connected_on: str | None = None
    source: str | None = None
    relationship: str | None = None
    notes: str | None = None


def upsert_contact(conn: sqlite3.Connection, record: ContactRecord) -> tuple[int, bool]:
    """Insert or refresh a contact -> (id, created). Match on LinkedIn URL, then name+company."""
    existing = _find(conn, record)
    if existing is None:
        cursor = conn.execute(
            """
            INSERT INTO contacts (name, company, company_norm, title, linkedin_url, email,
                                  relationship, connected_on, source, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.name,
                record.company,
                record.company_norm,
                record.title,
                record.linkedin_url,
                record.email,
                record.relationship,
                record.connected_on,
                record.source,
                record.notes,
            ),
        )
        return int(cursor.lastrowid or 0), True
    conn.execute(
        """
        UPDATE contacts
        SET name = ?, company = COALESCE(?, company), company_norm = COALESCE(?, company_norm),
            title = COALESCE(?, title), linkedin_url = COALESCE(?, linkedin_url),
            email = COALESCE(?, email), relationship = COALESCE(?, relationship),
            connected_on = COALESCE(?, connected_on), source = COALESCE(?, source),
            notes = COALESCE(?, notes)
        WHERE id = ?
        """,
        (
            record.name,
            record.company,
            record.company_norm,
            record.title,
            record.linkedin_url,
            record.email,
            record.relationship,
            record.connected_on,
            record.source,
            record.notes,
            existing,
        ),
    )
    return int(existing), False


def _find(conn: sqlite3.Connection, record: ContactRecord) -> int | None:
    if record.linkedin_url:
        row = conn.execute(
            "SELECT id FROM contacts WHERE linkedin_url = ?", (record.linkedin_url,)
        ).fetchone()
        if row is not None:
            return int(row["id"])
    row = conn.execute(
        """
        SELECT id FROM contacts
        WHERE lower(name) = lower(?) AND COALESCE(company_norm, '') = COALESCE(?, '')
        """,
        (record.name, record.company_norm),
    ).fetchone()
    return int(row["id"]) if row is not None else None


def get_contact(conn: sqlite3.Connection, contact_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    if row is None:
        raise ContactNotFound(f"contact {contact_id} not found")
    return row


def contacts_for_company(
    conn: sqlite3.Connection, company_norms: Iterable[str]
) -> list[sqlite3.Row]:
    """Contacts whose normalised company is any of the given keys (canonical plus aliases)."""
    keys = sorted({k for k in company_norms if k})
    if not keys:
        return []
    placeholders = ",".join("?" for _ in keys)
    return conn.execute(
        f"SELECT * FROM contacts WHERE company_norm IN ({placeholders}) ORDER BY name", keys
    ).fetchall()


def search_contacts(conn: sqlite3.Connection, text: str | None = None) -> list[sqlite3.Row]:
    if not text:
        return conn.execute("SELECT * FROM contacts ORDER BY company_norm, name").fetchall()
    like = f"%{text.lower()}%"
    return conn.execute(
        """
        SELECT * FROM contacts
        WHERE lower(name) LIKE ? OR lower(COALESCE(company, '')) LIKE ?
           OR lower(COALESCE(company_norm, '')) LIKE ? OR lower(COALESCE(title, '')) LIKE ?
        ORDER BY company_norm, name
        """,
        (like, like, like, like),
    ).fetchall()


def touch_contacted(conn: sqlite3.Connection, contact_id: int, at: str) -> None:
    conn.execute("UPDATE contacts SET last_contacted_at = ? WHERE id = ?", (at, contact_id))


def contact_to_dict(row: sqlite3.Row) -> dict[str, object]:
    return dict(row)
