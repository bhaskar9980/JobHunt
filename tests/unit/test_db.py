import sqlite3

import pytest

from jobhunt.db import SCHEMA_VERSION, SchemaError, init_db, migrate, schema_version, table_names


def test_init_creates_schema_and_sets_version(conn):
    assert schema_version(conn) == SCHEMA_VERSION
    names = table_names(conn)
    for table in (
        "jobs",
        "job_sources",
        "applications",
        "events",
        "contacts",
        "outreach",
        "email_ingest",
        "scan_runs",
        "runtime_state",
        "v_pipeline",
    ):
        assert table in names


def test_init_is_idempotent(conn):
    assert init_db(conn) == SCHEMA_VERSION
    assert schema_version(conn) == SCHEMA_VERSION


def test_foreign_keys_cascade_from_jobs(conn, norm, priority, make_posting):
    from jobhunt.repo.jobs import upsert_job

    job_id = upsert_job(conn, make_posting(), norm, priority).job_id
    assert conn.execute("SELECT COUNT(*) FROM job_sources").fetchone()[0] == 1
    with conn:
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    assert conn.execute("SELECT COUNT(*) FROM job_sources").fetchone()[0] == 0


def test_migrate_rejects_future_versions(conn):
    with pytest.raises(SchemaError):
        migrate(conn, SCHEMA_VERSION + 5)


def test_status_check_constraint_rejects_unknown(conn):
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError), conn:
        conn.execute(
            """
            INSERT INTO jobs (company, company_norm, title, title_norm, location_bucket, url,
                              url_canonical, primary_source, discovered_at, updated_at,
                              fingerprint, status)
            VALUES ('a','a','b','b','unknown','u','u','x','t','t','f','bogus')
            """
        )


def test_submission_and_referral_state_columns_exist(conn):
    job_columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    outreach_columns = {row["name"] for row in conn.execute("PRAGMA table_info(outreach)")}
    assert {"submission_state", "submission_started_at"} <= job_columns
    assert "referral_outcome" in outreach_columns


def test_version_one_migration_preserves_data_and_blocks_legacy_unknowns():
    from jobhunt.db import connect

    legacy = connect(":memory:")
    legacy.executescript(
        """
        CREATE TABLE jobs (id INTEGER PRIMARY KEY, prefill_state TEXT);
        CREATE TABLE applications (job_id INTEGER);
        CREATE TABLE events (id INTEGER PRIMARY KEY, job_id INTEGER, event_type TEXT, at TEXT);
        CREATE TABLE outreach (id INTEGER PRIMARY KEY, response TEXT);
        INSERT INTO jobs VALUES (1, 'ready'), (2, 'ready'), (3, 'ready');
        INSERT INTO applications VALUES (2);
        INSERT INTO events VALUES (1, 1, 'submission_unknown', '2026-09-09T08:00:00+00:00');
        INSERT INTO events VALUES (2, 1, 'prefilled', '2026-09-09T09:00:00+00:00');
        INSERT INTO outreach VALUES (1, 'I have not referred you yet');
        PRAGMA user_version = 1;
        """
    )
    try:
        assert init_db(legacy) == SCHEMA_VERSION
        rows = legacy.execute("SELECT * FROM jobs ORDER BY id").fetchall()
        assert rows[0]["submission_state"] == "unknown"
        assert rows[0]["prefill_state"] == "blocked"
        assert rows[1]["submission_state"] == "confirmed"
        assert rows[2]["submission_state"] == "none"
        outreach = legacy.execute("SELECT * FROM outreach").fetchone()
        assert outreach["response"] == "I have not referred you yet"
        assert outreach["referral_outcome"] == "pending"
        assert init_db(legacy) == SCHEMA_VERSION
    finally:
        legacy.close()


def test_failed_migration_rolls_back_schema_changes():
    from jobhunt.db import connect

    legacy = connect(":memory:")
    legacy.executescript("CREATE TABLE jobs (id INTEGER PRIMARY KEY); PRAGMA user_version = 1;")
    try:
        with pytest.raises(sqlite3.OperationalError):
            init_db(legacy)
        assert schema_version(legacy) == 1
        assert [row["name"] for row in legacy.execute("PRAGMA table_info(jobs)")] == ["id"]
    finally:
        legacy.close()
