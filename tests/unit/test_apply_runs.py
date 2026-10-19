"""The dashboard's Apply button runs off the request thread and reports what happened."""

from __future__ import annotations

import pytest

from jobhunt.apply_runs import DONE, FAILED, RUNNING, AlreadyRunning, ApplyRuns


def immediate(work) -> None:
    """Run the job inline so the tests stay deterministic and never touch a thread."""
    work()


def held():
    """A spawn that captures the work instead of running it, so a run stays in flight."""
    captured = []
    return captured.append, captured


def test_a_finished_run_reports_the_exit_code_and_message():
    runs = ApplyRuns(lambda job_id: (0, "applied"), spawn=immediate)

    runs.start(11)
    state = runs.status(11)

    assert state.state == DONE
    assert state.code == 0
    assert state.message == "applied"
    assert state.finished_at is not None


def test_a_blocked_run_keeps_its_exit_code():
    """A safety block is a real answer, not a crash, so it comes back as a finished run."""
    runs = ApplyRuns(lambda job_id: (15, "daily cap reached"), spawn=immediate)

    runs.start(12)

    assert runs.status(12).code == 15
    assert "daily cap" in runs.status(12).message


def test_a_raising_run_is_reported_not_swallowed():
    def explode(job_id: int):
        raise RuntimeError("chrome would not start")

    runs = ApplyRuns(explode, spawn=immediate)
    runs.start(13)
    state = runs.status(13)

    assert state.state == FAILED
    assert "fill session failed" in state.message
    assert "chrome would not start" not in state.message


def test_only_one_application_runs_at_a_time():
    """One browser profile, one human watching. A second run must be refused, not queued."""
    spawn, pending = held()
    runs = ApplyRuns(lambda job_id: (0, "applied"), spawn=spawn)

    first = runs.start(21)

    assert first.state == RUNNING
    assert runs.active().job_id == 21
    with pytest.raises(AlreadyRunning):
        runs.start(22)

    pending[0]()

    assert runs.active() is None
    assert runs.status(21).state == DONE


def test_a_new_run_is_allowed_once_the_previous_one_ends():
    runs = ApplyRuns(lambda job_id: (0, "applied"), spawn=immediate)
    runs.start(31)

    runs.start(32)

    assert runs.status(31).state == DONE
    assert runs.status(32).state == DONE


def test_an_unknown_job_has_no_status():
    assert ApplyRuns(lambda job_id: (0, ""), spawn=immediate).status(999) is None


def test_review_handoff_keeps_the_browser_slot_and_controls_are_run_scoped():
    spawn, _ = held()
    runs = ApplyRuns(lambda job_id: (0, "review finished"), spawn=spawn)
    first = runs.start(11, request_id="first")
    control = runs.control(11)
    control.update("ready_for_review", "Review in the browser", filled=3, questions=["Why us?"])
    assert runs.active().state == "ready_for_review"
    assert runs.status(11).filled == 3
    assert runs.status(11).questions == ["Why us?"]
    with pytest.raises(AlreadyRunning):
        runs.start(12, request_id="second")
    with pytest.raises(ValueError):
        runs.command(11, "stale", "close")
    runs.command(11, first.run_id, "close")
    assert control.cancelled.is_set()
    assert runs.active().state == "stopping"


def test_fill_request_retry_does_not_start_another_browser():
    calls = []
    runs = ApplyRuns(lambda job_id: (calls.append(job_id) or 0, "finished"), spawn=immediate)
    first = runs.start(11, request_id="same-click")
    second = runs.start(11, request_id="same-click")
    assert second.run_id == first.run_id
    assert calls == [11]
    with pytest.raises(ValueError):
        runs.start(12, request_id="same-click")


def test_resume_is_only_available_before_filling_not_during_manual_review():
    spawn, _ = held()
    runs = ApplyRuns(lambda job_id: (0, ""), spawn=spawn)
    state = runs.start(11)
    control = runs.control(11)
    control.update("waiting_for_login", "Sign in")
    runs.command(11, state.run_id, "resume")
    assert control.resumed.is_set()
    control.update("ready_for_review", "Review")
    with pytest.raises(ValueError):
        runs.command(11, state.run_id, "resume")


def test_progress_snapshot_cannot_change_stored_questions():
    spawn, _ = held()
    runs = ApplyRuns(lambda job_id: (0, ""), spawn=spawn)
    runs.start(11)
    runs.control(11).update("needs_input", "Complete missing fields", questions=["Why us?"])
    runs.status(11).questions.clear()
    assert runs.status(11).questions == ["Why us?"]


@pytest.mark.parametrize("state,resume", [("waiting_for_login", True), ("ready_for_review", False)])
def test_review_wait_pumps_browser_events_and_only_resumes_login(state, resume):
    from types import SimpleNamespace

    from jobhunt.apply_runs import ReviewControl

    ticks = []
    control = ReviewControl("session", lambda *args: None)

    def tick(milliseconds):
        ticks.append(milliseconds)
        control.resumed.set()

    page = SimpleNamespace(is_closed=lambda: len(ticks) > 1, wait_for_timeout=tick)
    assert control.pause(page, state, "Review", poll_ms=1) is resume
    assert ticks


def test_worker_errors_do_not_expose_private_values(caplog):
    def fail(job_id):
        raise RuntimeError("private-answer-value")

    runs = ApplyRuns(fail, spawn=immediate)
    runs.start(11)
    assert "private-answer-value" not in runs.status(11).message
    assert "private-answer-value" not in caplog.text
