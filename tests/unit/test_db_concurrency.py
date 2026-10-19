"""A second writer waits its turn instead of failing, so the dashboard survives a background run."""

from __future__ import annotations

import threading

import pytest

from jobhunt.db import BUSY_TIMEOUT_SECONDS, connect, init_db, transaction

HOLD_SECONDS = 0.4


def test_a_connection_waits_for_a_busy_database(tmp_path):
    """Without a busy timeout the second writer fails at once and the write is lost."""
    path = tmp_path / "jobs.db"
    setup = connect(path)
    init_db(setup)
    setup.close()
    holding = threading.Event()
    release = threading.Event()

    def hold():
        writer = connect(path)
        try:
            with transaction(writer):
                writer.execute(
                    "INSERT INTO runtime_state (key, value, updated_at) VALUES ('a', '1', 'now')"
                )
                holding.set()
                release.wait(HOLD_SECONDS)
        finally:
            writer.close()

    thread = threading.Thread(target=hold)
    thread.start()
    holding.wait(HOLD_SECONDS * 5)
    other = connect(path)
    try:
        with transaction(other):
            other.execute(
                "INSERT INTO runtime_state (key, value, updated_at) VALUES ('b', '2', 'now')"
            )
        keys = [row["key"] for row in other.execute("SELECT key FROM runtime_state ORDER BY key")]
    finally:
        release.set()
        thread.join()
        other.close()

    assert keys == ["a", "b"]


def test_the_busy_timeout_is_set_on_every_connection(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    try:
        timeout_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    finally:
        conn.close()
    assert timeout_ms == int(BUSY_TIMEOUT_SECONDS * 1000)


def test_the_busy_timeout_is_finite_so_a_deadlock_still_surfaces():
    assert 0 < BUSY_TIMEOUT_SECONDS <= 60


@pytest.mark.parametrize("path", [":memory:"])
def test_an_in_memory_database_still_connects(path):
    conn = connect(path)
    try:
        init_db(conn)
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        conn.close()
