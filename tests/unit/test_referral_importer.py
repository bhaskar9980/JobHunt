"""LinkedIn Connections.csv import: preamble skipping, BOM, quoted commas, aliases, upsert."""

from __future__ import annotations

from pathlib import Path

import pytest

from jobhunt.referral.importer import (
    ConnectionsFormatError,
    canonical_company,
    import_connections,
    parse_connected_on,
    read_connections,
)
from jobhunt.repo import contacts as contacts_repo

FIXTURE = Path(__file__).parent.parent / "fixtures" / "referrals" / "Connections.csv"
ALIASES = {"platform science": ["trimble transport", "trimble transportation"]}
EXPECTED_ROWS = 6
EXPECTED_NAMED = 5


def test_read_connections_skips_preamble_and_handles_bom_and_quotes():
    rows = read_connections(FIXTURE)
    assert len(rows) == EXPECTED_ROWS
    assert rows[0]["First Name"] == "Priya"
    assert rows[2]["Company"] == "Stripe, Inc."
    assert set(rows[0]) >= {"First Name", "Last Name", "URL", "Company", "Position", "Connected On"}


def test_missing_header_raises(tmp_path):
    bad = tmp_path / "x.csv"
    bad.write_text("Notes:\nnothing useful\n", encoding="utf-8")
    with pytest.raises(ConnectionsFormatError):
        read_connections(bad)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("08 Mar 2024", "2024-03-08"), ("2 June 2023", "2023-06-02"), ("", None), ("garbage", None)],
)
def test_parse_connected_on(value, expected):
    assert parse_connected_on(value) == expected


def test_canonical_company_folds_aliases(norm):
    assert canonical_company("Trimble Transport", norm, ALIASES) == "platform science"
    assert canonical_company("Stripe, Inc.", norm, ALIASES) == "stripe"
    assert canonical_company("", norm, ALIASES) is None


def test_import_creates_contacts_and_is_idempotent(conn, norm):
    first = import_connections(conn, FIXTURE, norm, ALIASES)
    assert (first.rows, first.created, first.updated, first.skipped_no_name) == (
        EXPECTED_ROWS,
        EXPECTED_NAMED,
        0,
        1,
    )
    second = import_connections(conn, FIXTURE, norm, ALIASES)
    assert (second.created, second.updated) == (0, EXPECTED_NAMED)
    assert conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == EXPECTED_NAMED

    rohit = contacts_repo.search_contacts(conn, "rohit")[0]
    assert rohit["company"] == "Trimble Transport"
    assert rohit["company_norm"] == "platform science"
    assert rohit["connected_on"] == "2023-06-02"
    assert rohit["source"] == "linkedin_export"

    neha = contacts_repo.search_contacts(conn, "neha")[0]
    assert neha["company"] is None and neha["company_norm"] is None


def test_contacts_for_company_uses_normalised_keys(conn, norm):
    import_connections(conn, FIXTURE, norm, ALIASES)
    rows = contacts_repo.contacts_for_company(conn, {"postman"})
    assert sorted(r["name"] for r in rows) == ["Arjun Mehta", "Priya Natarajan"]
    assert contacts_repo.contacts_for_company(conn, set()) == []


def test_upsert_without_url_matches_on_name_and_company(conn):
    record = contacts_repo.ContactRecord(name="Sam Roy", company="Acme", company_norm="acme")
    first_id, created = contacts_repo.upsert_contact(conn, record)
    assert created
    again = contacts_repo.ContactRecord(
        name="Sam Roy", company_norm="acme", title="SRE", linkedin_url="https://l.in/sam"
    )
    second_id, created = contacts_repo.upsert_contact(conn, again)
    assert (second_id, created) == (first_id, False)
    row = contacts_repo.get_contact(conn, first_id)
    assert row["title"] == "SRE" and row["linkedin_url"] == "https://l.in/sam"
    assert row["company"] == "Acme"
