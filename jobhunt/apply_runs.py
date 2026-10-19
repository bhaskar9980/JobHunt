"""Background review-only fill sessions started from the dashboard.

Filling opens a real browser and can pause for a sign-in or a CAPTCHA, so it cannot run inside
an HTTP request. Each run happens on its own thread with its own SQLite connection, and the
dashboard polls for progress. The browser stays open for the human to review and submit.

One run at a time. There is a single browser profile and a single human watching it, so a
second concurrent run would fight the first for the window.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from typing import Any
from uuid import uuid4

from playwright.sync_api import Error as PlaywrightError

from jobhunt.clock import now_iso

log = logging.getLogger(__name__)

RUNNING = "running"
DONE = "done"
FAILED = "failed"
READY_FOR_REVIEW = "ready_for_review"
NEEDS_INPUT = "needs_input"
WAITING_LOGIN = "waiting_for_login"
WAITING_CAPTCHA = "waiting_for_captcha"
STOPPING = "stopping"
RESUMABLE = frozenset({WAITING_LOGIN, WAITING_CAPTCHA})


@dataclass(slots=True)
class RunState:
    """What the dashboard shows while an application is being filled in."""

    job_id: int
    state: str
    message: str
    started_at: str
    code: int | None = None
    finished_at: str | None = None
    run_id: str = field(default_factory=lambda: uuid4().hex)
    filled: int = 0
    questions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ReviewControl:
    run_id: str
    report: Callable[..., None]
    cancelled: threading.Event = field(default_factory=threading.Event)
    resumed: threading.Event = field(default_factory=threading.Event)

    def update(self, state: str, message: str, *, filled: int = 0, questions=None) -> None:
        self.report(state, message, filled, list(questions or []))

    def pause(self, page, state, message, *, poll_ms, filled=0, questions=None) -> bool:
        self.resumed.clear()
        self.update(state, message, filled=filled, questions=questions)
        try:
            while not self.cancelled.is_set() and not page.is_closed():
                if state in RESUMABLE and self.resumed.is_set():
                    return True
                page.wait_for_timeout(poll_ms)
        except PlaywrightError:
            if not page.is_closed():
                raise
        return False


class ApplyRuns:
    """Starts and tracks dashboard-triggered review sessions.

    `run_apply` is injected so tests never open a browser. It takes a job id and returns
    (exit_code, message); its control reports progress while the browser remains open.
    """

    def __init__(
        self,
        run_apply: Callable[[int], tuple[int, str]],
        *,
        spawn: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self._run_apply = run_apply
        self._spawn = spawn or _spawn_thread
        self._lock = threading.Lock()
        self._runs: dict[int, RunState] = {}
        self._history: dict[str, RunState] = {}
        self._controls: dict[str, ReviewControl] = {}
        self._active: int | None = None

    def active(self) -> RunState | None:
        with self._lock:
            state = self._runs.get(self._active)
            return replace(state, questions=list(state.questions)) if state else None

    def status(self, job_id: int) -> RunState | None:
        with self._lock:
            state = self._runs.get(job_id)
            # A copy, because the apply thread keeps writing to the stored one.
            return replace(state, questions=list(state.questions)) if state is not None else None

    def control(self, job_id: int) -> ReviewControl:
        with self._lock:
            return self._controls[self._runs[job_id].run_id]

    def command(self, job_id: int, run_id: str, action: str) -> RunState:
        with self._lock:
            state = self._runs.get(job_id)
            if not state or state.run_id != run_id or self._active != job_id:
                raise ValueError("This fill session is no longer active. Refresh its status.")
            control = self._controls[run_id]
            if action == "close":
                control.cancelled.set()
                state.state, state.message = (
                    STOPPING,
                    "Closing the fill window; application status is unchanged.",
                )
            elif action == "resume" and state.state in RESUMABLE:
                control.resumed.set()
                state.state, state.message = RUNNING, "Continuing after your input"
            else:
                raise ValueError("That action is not available for this fill session.")
            return replace(state, questions=list(state.questions))

    def start(self, job_id: int, *, request_id: str | None = None) -> RunState:
        """Begin a run once, or raise if a different browser session is still open."""
        identifier = request_id or uuid4().hex
        with self._lock:
            previous = self._history.get(identifier)
            if previous:
                if previous.job_id != job_id:
                    raise ValueError("A fill request cannot be reused for a different job.")
                return replace(previous, questions=list(previous.questions))
            if self._active is not None:
                raise AlreadyRunning(f"job {self._active} still has an open fill session")
            state = RunState(
                job_id,
                RUNNING,
                "Opening the form for review, not submission",
                now_iso(),
                run_id=identifier,
            )
            self._runs[job_id] = self._history[identifier] = state
            self._controls[identifier] = ReviewControl(
                identifier, lambda *args: self._progress(identifier, *args)
            )
            self._active = job_id
        try:
            self._spawn(lambda: self._work(job_id))
        except Exception as exc:  # noqa: BLE001 - a thread that never started must not wedge us
            log.error("could not start fill thread for job %s (%s)", job_id, type(exc).__name__)
            self._finish(job_id, None, FAILED, "The fill session could not start.")
            raise
        return self.status(job_id)

    def _progress(
        self, run_id: str, state: str, message: str, filled: int, questions: list[str]
    ) -> None:
        with self._lock:
            record = self._history[run_id]
            if record.finished_at or self._controls[run_id].cancelled.is_set():
                return
            record.state, record.message = state, message
            record.filled, record.questions = filled, list(questions)

    def _finish(self, job_id: int, code: int | None, outcome: str, note: str) -> None:
        with self._lock:
            state = self._runs[job_id]
            state.state, state.code, state.message = outcome, code, note
            state.finished_at = now_iso()
            self._active = None

    def _work(self, job_id: int) -> None:
        try:
            code, message = self._run_apply(job_id)
            outcome, note = (DONE if code == 0 else FAILED), message
        except Exception as exc:  # noqa: BLE001 - thread boundary; the error becomes the result
            log.error("fill session for job %s failed (%s)", job_id, type(exc).__name__)
            code, outcome, note = (
                None,
                FAILED,
                "The fill session failed. Check the browser before trying again.",
            )
        self._finish(job_id, code, outcome, note)


class AlreadyRunning(RuntimeError):
    """A second fill was requested while the previous browser session was still open."""


def _spawn_thread(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name="jobhunt-fill-review", daemon=True).start()
