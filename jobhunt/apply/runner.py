"""Prefill and apply orchestration. Browser and filler are injected so the flow is testable."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError

from jobhunt.apply.answers import AnswerBank, detect_country
from jobhunt.apply.captcha import detect_captcha, looks_like_login, wait_for_human
from jobhunt.apply.detect import AtsRegistry
from jobhunt.apply.fillers import filler_for
from jobhunt.apply.fillers.base import (
    AnswerSet,
    AtsFiller,
    FileInputs,
    FillerError,
    LoginRequired,
    SubmissionNotStarted,
    resolve_answers,
)
from jobhunt.apply.safety import UNCONFIRMED_REASON, SafetyGate
from jobhunt.apply.schema import FormSchema
from jobhunt.apply_runs import (
    NEEDS_INPUT,
    READY_FOR_REVIEW,
    RUNNING,
    WAITING_CAPTCHA,
    WAITING_LOGIN,
    ReviewControl,
)
from jobhunt.clock import now_iso
from jobhunt.config import Settings
from jobhunt.db import transaction
from jobhunt.models import APPLICATION_STAGES, EventType, Status
from jobhunt.repo import applications as applications_repo
from jobhunt.repo import events as events_repo
from jobhunt.repo import jobs as jobs_repo

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_CAPTCHA = 10
EXIT_LOGIN = 11
MAX_PAGES = 12
EXIT_DUPLICATE = 12
EXIT_UNMATCHED = 13
EXIT_VALIDATION = 14
EXIT_SAFETY = 15
EXIT_SUBMISSION_UNKNOWN = 16
EXIT_UNSUPPORTED = 20

FORM_SCHEMA_FILE = "form_schema.json"
ANSWERS_FILE = "answers.json"
COVER_LETTER_FILE = "cover_letter.md"
COVER_LETTER_PDF = "cover_letter.pdf"
PREFILLED_PNG = "prefilled.png"
CONFIRMATION_PNG = "confirmation.png"
CONFIRMATION_JSON = "confirmation.json"
RUN_LOG = "run.log"
STATE_SCHEMA = "schema"
STATE_READY = "ready"
STATE_BLOCKED = "blocked"
SUBMITTED_VIA = "ats_form"
DEFAULT_REVIEW_POLL_MS = 250
REVIEW_CLOSED = (
    "Fill window closed. Use I applied only after submitting and verifying the application."
)

PageFactory = Callable[[], AbstractContextManager[Any]]
ScreenshotFn = Callable[[Any, Path], Path]


@dataclass(slots=True)
class RunResult:
    job_id: int
    code: int
    message: str
    artefacts: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class Runner:
    conn: sqlite3.Connection
    settings: Settings
    bank: AnswerBank
    registry: AtsRegistry
    page_factory: PageFactory
    screenshot: ScreenshotFn
    filler_lookup: Callable[[str | None], AtsFiller | None] = filler_for

    # ------------------------------------------------------------------ paths

    def job_dir(self, job_id: int) -> Path:
        path = self.settings.path("applications_dir") / str(job_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _note(self, job_id: int, text: str) -> None:
        try:
            with (self.job_dir(job_id) / RUN_LOG).open("a", encoding="utf-8") as handle:
                handle.write(f"{now_iso()} {text}\n")
        except OSError as exc:
            log.warning("job %s: run log could not be saved: %s", job_id, exc)
        log.info("job %s: %s", job_id, text)

    def _event(self, job_id: int, event_type: EventType, note: str) -> None:
        with transaction(self.conn):
            app_row = applications_repo.get_for_job(self.conn, job_id)
            events_repo.add_event(
                self.conn,
                job_id,
                event_type,
                note,
                application_id=app_row["id"] if app_row else None,
                source="apply",
            )
        self._note(job_id, f"{event_type}: {note}")

    # ------------------------------------------------------------------ prefill

    def _preparation_block(self, job_id: int) -> RunResult | None:
        if not applications_repo.submission_unresolved(self.conn, job_id):
            return None
        jobs_repo.set_prefill_state(self.conn, job_id, STATE_BLOCKED)
        return RunResult(job_id, EXIT_SAFETY, UNCONFIRMED_REASON)

    def fill_for_review(self, job_id: int, control: ReviewControl) -> RunResult:
        if control.cancelled.is_set():
            return RunResult(job_id, EXIT_OK, REVIEW_CLOSED)
        job = jobs_repo.get_job(self.conn, job_id)
        url = job["apply_url"] or job["url"]
        ats = self.registry.detect(url, fallback=job["ats_type"])
        if not self.registry.review_url_allowed(url, ats):
            return RunResult(
                job_id, EXIT_UNSUPPORTED, "Review filling requires a supported direct ATS URL."
            )
        blocked = self._safety_block(job, ats, dry_run=False, require_ready=False)
        if blocked:
            return blocked
        filler = self.filler_lookup(ats)
        if filler is None or not self._files(job_id).resume_pdf.is_file():
            return RunResult(job_id, EXIT_VALIDATION, "The filler or resume PDF is unavailable.")
        poll_ms = int(self.settings.section("apply").get("review_poll_ms", DEFAULT_REVIEW_POLL_MS))
        if poll_ms <= 0:
            return RunResult(job_id, EXIT_VALIDATION, "review_poll_ms must be positive.")
        return self._review_session(job, filler, control, poll_ms)

    def _review_session(self, job, filler, control, poll_ms) -> RunResult:
        job_id = job["id"]
        try:
            with self.page_factory() as page:
                opened = self._review_open(page, filler, job, control, poll_ms)
                if opened is not None:
                    return opened
                return self._review_form(page, filler, job, control, poll_ms)
        except (FillerError, PlaywrightError, OSError, ValueError) as exc:
            log.warning("review fill for job %s failed (%s)", job_id, type(exc).__name__)
            return RunResult(
                job_id,
                EXIT_VALIDATION,
                "Fill session interrupted. Check the employer site before retrying.",
            )

    def _review_open(self, page, filler, job, control, poll_ms) -> RunResult | None:
        while not control.cancelled.is_set() and not page.is_closed():
            try:
                filler.open(page, job["apply_url"] or job["url"])
            except LoginRequired:
                if control.pause(
                    page,
                    WAITING_LOGIN,
                    "Sign in in the browser, then choose Continue filling.",
                    poll_ms=poll_ms,
                ):
                    continue
                break
            except FillerError:
                if not detect_captcha(page):
                    raise
            if detect_captcha(page):
                if control.pause(
                    page,
                    WAITING_CAPTCHA,
                    "Solve the CAPTCHA yourself, then choose Continue filling.",
                    poll_ms=poll_ms,
                ):
                    continue
                break
            return None
        return RunResult(job["id"], EXIT_OK, REVIEW_CLOSED)

    def _review_form(self, page, filler, job, control, poll_ms) -> RunResult:
        job_id = job["id"]
        if not self.registry.review_url_allowed(page.url, filler.KEY):
            return RunResult(
                job_id,
                EXIT_UNSUPPORTED,
                "The form redirected outside the supported ATS. Fill it manually.",
            )
        current = jobs_repo.get_job(self.conn, job_id)
        blocked = self._safety_block(current, filler.KEY, dry_run=False, require_ready=False)
        if blocked:
            return blocked
        if filler.already_applied(page):
            return RunResult(
                job_id,
                EXIT_DUPLICATE,
                "The site reports an existing application. Verify it before retrying.",
            )
        schema = filler.extract_schema(page)
        answers = self.resolve(job_id, schema)
        directory = self.job_dir(job_id) / "review" / control.run_id
        directory.mkdir(parents=True, exist_ok=True)
        schema.save(directory / FORM_SCHEMA_FILE)
        answers.save(directory / ANSWERS_FILE)
        filled, problems = self._review_fields(page, filler, schema, answers, job_id, control)
        if control.cancelled.is_set() or page.is_closed():
            return RunResult(job_id, EXIT_OK, REVIEW_CLOSED)
        state = NEEDS_INPUT if problems else READY_FOR_REVIEW
        message = (
            "Review and edit the form in the browser. Only you click Submit on the employer site."
        )
        self._event(
            job_id,
            EventType.PREFILLED,
            f"Review-only fill: {filled} fields; {len(problems)} items need review. "
            "No submit click.",
        )
        control.pause(page, state, message, poll_ms=poll_ms, filled=filled, questions=problems)
        return RunResult(job_id, EXIT_OK, REVIEW_CLOSED)

    def _review_fields(
        self, page, filler, schema, answers, job_id, control
    ) -> tuple[int, list[str]]:
        filled = 0
        problems = [item.label for item in answers.unmatched_required]
        for item in schema.fields:
            if control.cancelled.is_set() or page.is_closed():
                break
            if not self.registry.review_url_allowed(page.url, filler.KEY):
                problems.append("The form navigated away; automatic filling stopped.")
                break
            if not item.system and item.key not in answers.answers:
                continue
            control.update(RUNNING, "Filling verified saved details; no submission", filled=filled)
            selected = {item.key: answers.answers[item.key]} if item.key in answers.answers else {}
            try:
                report = filler.fill(
                    page,
                    replace(schema, fields=[item]),
                    selected,
                    self.bank.get("profile") or {},
                    self._files(job_id),
                )
            except (FillerError, PlaywrightError):
                problems.append(f"Check this field manually: {item.label}")
                break
            filled += len(report.filled)
            if report.errors or (item.required and report.skipped):
                problems.append(f"Check this field manually: {item.label}")
        return filled, problems

    def prefill(self, job_id: int) -> RunResult:
        job = jobs_repo.get_job(self.conn, job_id)
        blocked = self._preparation_block(job_id)
        if blocked is not None:
            return blocked
        ats = self.registry.detect(job["apply_url"] or job["url"], fallback=job["ats_type"])
        mode = self.registry.mode_for(ats)
        if mode != "python":
            return self._prefill_without_browser(job, ats, mode)
        filler = self.filler_lookup(ats)
        if filler is None:
            return RunResult(job_id, EXIT_UNSUPPORTED, f"no filler for {ats}")
        return self._prefill_python(job, filler)

    def _prefill_python(self, job: sqlite3.Row, filler: AtsFiller) -> RunResult:
        job_id = int(job["id"])
        with self.page_factory() as page:
            try:
                filler.open(page, job["apply_url"] or job["url"])
            except LoginRequired as exc:
                jobs_repo.set_prefill_state(self.conn, job_id, STATE_BLOCKED)
                self._event(job_id, EventType.LOGIN_REQUIRED, str(exc))
                return RunResult(job_id, EXIT_LOGIN, str(exc))
            except (FillerError, PlaywrightError, OSError) as exc:
                self._event(job_id, EventType.VALIDATION_ERROR, f"open failed: {exc}")
                return RunResult(job_id, EXIT_VALIDATION, f"open failed: {exc}")
            if filler.already_applied(page):
                self._event(job_id, EventType.DUPLICATE_BLOCKED, "form says already applied")
                return RunResult(job_id, EXIT_DUPLICATE, "already applied according to the form")
            schema = filler.extract_schema(page)
        return self._finish_prefill(job, schema)

    def _prefill_without_browser(self, job: sqlite3.Row, ats: str | None, mode: str) -> RunResult:
        """Assisted/manual ATSs: write the answer sheet inputs; the skill drives the browser."""
        schema = FormSchema(ats=ats or "unknown", url=job["apply_url"] or job["url"])
        jobs_repo.set_prefill_state(self.conn, job["id"], STATE_SCHEMA)
        schema.save(self.job_dir(job["id"]) / FORM_SCHEMA_FILE)
        answers = resolve_answers(schema, self.bank, job_id=job["id"])
        answers.save(self.job_dir(job["id"]) / ANSWERS_FILE)
        jobs_repo.set_prefill_state(self.conn, job["id"], STATE_READY)
        self._note(job["id"], f"prefill: {mode} mode for {ats}; answer sheet ready")
        return RunResult(job["id"], EXIT_UNSUPPORTED, f"{ats or 'unknown'} uses {mode} mode")

    def _finish_prefill(self, job: sqlite3.Row, schema: FormSchema) -> RunResult:
        job_id = int(job["id"])
        directory = self.job_dir(job_id)
        jobs_repo.set_prefill_state(self.conn, job_id, STATE_SCHEMA)
        schema.save(directory / FORM_SCHEMA_FILE)
        answers = self.resolve(job_id, schema)
        answers.save(directory / ANSWERS_FILE)
        state = STATE_SCHEMA if answers.unmatched_required else STATE_READY
        jobs_repo.set_prefill_state(self.conn, job_id, state)
        self._event(
            job_id,
            EventType.PREFILLED,
            f"{len(answers.answers)} answered, "
            f"{len(answers.unmatched_required)} required unmatched",
        )
        code = EXIT_UNMATCHED if answers.unmatched_required else EXIT_OK
        return RunResult(
            job_id,
            code,
            f"prefill_state={state}",
            {
                "form_schema": str(directory / FORM_SCHEMA_FILE),
                "answers": str(directory / ANSWERS_FILE),
            },
        )

    def resolve(self, job_id: int, schema: FormSchema) -> AnswerSet:
        cover = self.job_dir(job_id) / COVER_LETTER_FILE
        cover_text = cover.read_text(encoding="utf-8").strip() if cover.exists() else None
        return resolve_answers(
            schema,
            self.bank,
            job_id=job_id,
            country=self._job_country(job_id),
            cover_letter=cover_text,
        )

    def _job_country(self, job_id: int) -> str | None:
        """Most forms ask about authorisation "in the country you are applying to" without naming
        it. The posting names it, so read it from the job rather than leaving the question open.
        None when the location names no country, which keeps the bank's guard in charge."""
        job = jobs_repo.get_job(self.conn, job_id)
        if job is None:
            return None
        return detect_country(job["location"] or "")

    def re_resolve(self, job_id: int) -> RunResult:
        """After the bank learned new answers, recompute answers.json without a browser."""
        blocked = self._preparation_block(job_id)
        if blocked is not None:
            return blocked
        directory = self.job_dir(job_id)
        schema_path = directory / FORM_SCHEMA_FILE
        if not schema_path.exists():
            return RunResult(job_id, EXIT_VALIDATION, "run prefill first: no form_schema.json")
        job = jobs_repo.get_job(self.conn, job_id)
        return self._finish_prefill(job, FormSchema.load(schema_path))

    # ------------------------------------------------------------------ apply

    def apply(self, job_id: int, *, dry_run: bool = False) -> RunResult:
        job = jobs_repo.get_job(self.conn, job_id)
        ats = self.registry.detect(job["apply_url"] or job["url"], fallback=job["ats_type"])
        if self.registry.mode_for(ats) != "python":
            return RunResult(job_id, EXIT_UNSUPPORTED, f"{ats or 'unknown'} is not a python filler")
        blocked = self._safety_block(job, ats, dry_run=dry_run)
        if blocked is not None:
            return blocked
        loaded = self._load_prefill(job_id)
        if loaded is None:
            return RunResult(job_id, EXIT_VALIDATION, "run prefill first")
        schema, answers = loaded
        if answers.unmatched_required:
            return RunResult(job_id, EXIT_UNMATCHED, f"{len(answers.unmatched_required)} unmatched")
        filler = self.filler_lookup(ats)
        if filler is None:
            return RunResult(job_id, EXIT_UNSUPPORTED, f"no filler for {ats}")
        with self.page_factory() as page:
            return self._drive(page, filler, job, schema, answers, dry_run=dry_run)

    def _safety_block(
        self, job: sqlite3.Row, ats: str | None, *, dry_run: bool, require_ready: bool = True
    ) -> RunResult | None:
        """Placeholder answers and the SafetyGate stop a real submission before any browser."""
        job_id = int(job["id"])
        reasons = [] if dry_run else self.bank.placeholder_issues()
        if reasons:
            reason = "answers.yaml still has placeholders: " + "; ".join(reasons)
            self._event(job_id, EventType.SAFETY_BLOCKED, reason)
            return RunResult(job_id, EXIT_SAFETY, reason)
        gate = SafetyGate.from_settings(self.conn, self.settings.section("apply"))
        verdict = gate.check(job, ats, require_ready=require_ready)
        if verdict.ok:
            return None
        self._event(job_id, EventType.SAFETY_BLOCKED, "; ".join(verdict.reasons))
        return RunResult(job_id, EXIT_SAFETY, "; ".join(verdict.reasons))

    def _load_prefill(self, job_id: int) -> tuple[FormSchema, AnswerSet] | None:
        directory = self.job_dir(job_id)
        schema_path, answers_path = directory / FORM_SCHEMA_FILE, directory / ANSWERS_FILE
        if not (schema_path.exists() and answers_path.exists()):
            return None
        return FormSchema.load(schema_path), AnswerSet.load(answers_path)

    def _drive(
        self,
        page: Any,
        filler: AtsFiller,
        job: sqlite3.Row,
        schema: FormSchema,
        answers: AnswerSet,
        *,
        dry_run: bool,
    ) -> RunResult:
        job_id = int(job["id"])
        apply_cfg = self.settings.section("apply")
        opened = self._open(page, filler, job, float(apply_cfg.get("login_wait_seconds", 240)))
        if opened is not None:
            return opened
        if filler.already_applied(page):
            self._event(job_id, EventType.DUPLICATE_BLOCKED, "form says already applied")
            return RunResult(job_id, EXIT_DUPLICATE, "already applied according to the form")
        filled = self._fill_all_pages(page, filler, job, schema, answers)
        if isinstance(filled, RunResult):
            return filled
        shot = self.screenshot(page, self.job_dir(job_id) / PREFILLED_PNG)
        self._event(job_id, EventType.PREFILLED, f"filled {filled} fields; {shot.name}")
        if dry_run:
            self._event(job_id, EventType.DRY_RUN, "stopped before submit")
            return RunResult(
                job_id, EXIT_OK, "dry run: form filled, not submitted", {"prefilled": str(shot)}
            )
        return self._submit(
            page,
            filler,
            job_id,
            float(apply_cfg.get("captcha_wait_seconds", 180)),
            float(apply_cfg.get("confirm_timeout_seconds", 45)),
        )

    def _fill_all_pages(
        self,
        page: Any,
        filler: AtsFiller,
        job: sqlite3.Row,
        schema: FormSchema,
        answers: AnswerSet,
        *,
        max_pages: int = MAX_PAGES,
    ) -> int | RunResult:
        """Fill this page, then any further pages a wizard reveals.

        Prefill can only see the first page, so a later page may ask something new. That is
        reported and the run stops before submitting rather than sending a half-filled form.
        """
        job_id = int(job["id"])
        total = 0
        for index in range(max_pages):
            fresh = filler.extract_schema(page)
            if index == 0:
                refreshed = self._refresh_inputs(job, schema, fresh, answers)
                if refreshed is not None:
                    return refreshed
                page_answers = answers
            else:
                # A later page was never seen at prefill time, so its answers have to come from
                # the bank now. Reusing page one's answer set would leave every new field blank
                # and could never be satisfied by adding answers to the bank.
                page_answers = self.resolve(job_id, fresh)
                if page_answers.unmatched_required:
                    return self._page_needs_answers(job, fresh, page_answers, index)
            report = filler.fill(
                page,
                fresh,
                page_answers.answers,
                self.bank.get("profile") or {},
                self._files(job_id),
            )
            if report.errors:
                self._event(job_id, EventType.VALIDATION_ERROR, "; ".join(report.errors)[:500])
                return RunResult(job_id, EXIT_VALIDATION, "; ".join(report.errors))
            total += len(report.filled)
            if not filler.advance(page):
                return total
        unfinished = f"form still not finished after {max_pages} pages"
        return RunResult(job_id, EXIT_VALIDATION, unfinished)

    def _page_needs_answers(
        self, job: sqlite3.Row, fresh: FormSchema, page_answers: AnswerSet, index: int
    ) -> RunResult:
        """A later wizard page asked something the answer bank has never seen.

        Its schema and answers are saved beside the first page's, never over them: overwriting
        `form_schema.json` would destroy the reviewed prefill and make the next run report the
        first page as changed, which loops forever without ever submitting.
        """
        job_id = int(job["id"])
        page_number = index + 1
        directory = self.job_dir(job_id)
        fresh.save(directory / f"form_schema_page{page_number}.json")
        page_answers.save(directory / f"answers_page{page_number}.json")
        labels = ", ".join(field.label for field in page_answers.unmatched_required)
        reason = f"page {page_number} asks: {labels}"
        jobs_repo.set_prefill_state(self.conn, job_id, STATE_SCHEMA)
        self._event(job_id, EventType.UNMATCHED_QUESTIONS, reason)
        return RunResult(
            job_id,
            EXIT_UNMATCHED,
            reason
            + f"; answer these with `answers learn`, saved in answers_page{page_number}.json",
        )

    def _refresh_inputs(
        self, job: sqlite3.Row, saved: FormSchema, fresh: FormSchema, answers: AnswerSet
    ) -> RunResult | None:
        changed = [
            f
            for f in saved.changed_fields(fresh)
            if not f.system and (f.required or f.key in answers.answers)
        ]
        if changed:
            reason = "form changed: " + ", ".join(f.label for f in changed)
        elif self.resolve(job["id"], fresh).answers != answers.answers:
            reason = "stored answers changed"
        else:
            return None
        self._finish_prefill(job, fresh)
        self._event(job["id"], EventType.UNMATCHED_QUESTIONS, reason)
        return RunResult(job["id"], EXIT_UNMATCHED, reason + "; review refreshed answers")

    def _open(
        self, page: Any, filler: AtsFiller, job: sqlite3.Row, login_wait: float
    ) -> RunResult | None:
        job_id = int(job["id"])
        try:
            self._open_with_login_wait(page, filler, job, login_wait)
        except LoginRequired as exc:
            self._event(job_id, EventType.LOGIN_REQUIRED, str(exc))
            return RunResult(job_id, EXIT_LOGIN, str(exc))
        except (FillerError, PlaywrightError, OSError) as exc:
            self._event(job_id, EventType.VALIDATION_ERROR, f"open failed: {exc}")
            return RunResult(job_id, EXIT_VALIDATION, f"open failed: {exc}")
        return None

    def _open_with_login_wait(
        self, page: Any, filler: AtsFiller, job: sqlite3.Row, login_wait: float
    ) -> None:
        url = job["apply_url"] or job["url"]
        try:
            filler.open(page, url)
        except LoginRequired:
            self._note(job["id"], f"login wall: waiting up to {login_wait:.0f}s for the human")
            if not wait_for_human(page, login_wait, lambda p: not looks_like_login(p)):
                raise
            filler.open(page, url)

    def _submit(
        self, page: Any, filler: AtsFiller, job_id: int, captcha_wait: float, confirm_timeout: float
    ) -> RunResult:
        blocked = self._captcha_blocked(page, job_id, captcha_wait)
        if blocked is not None:
            return blocked
        gate = SafetyGate.from_settings(self.conn, self.settings.section("apply"))
        verdict = gate.reserve(job_id, filler.KEY)
        if not verdict.ok:
            return RunResult(job_id, EXIT_SAFETY, "; ".join(verdict.reasons))
        try:
            filler.submit(page)
        except SubmissionNotStarted as exc:
            # Nothing was sent: the submit control was missing or disabled. Safe to retry.
            with transaction(self.conn):
                applications_repo.resolve_not_submitted(self.conn, job_id, str(exc), ready=True)
            self._event(job_id, EventType.VALIDATION_ERROR, str(exc))
            return RunResult(job_id, EXIT_VALIDATION, str(exc))
        except (FillerError, PlaywrightError, OSError) as exc:
            self._note(job_id, f"submit outcome uncertain: {exc}")
            return self._submission_unknown(page, job_id)
        try:
            confirmation = filler.confirm(page, confirm_timeout)
        except (FillerError, PlaywrightError, OSError) as exc:
            confirmation = None
            self._note(job_id, f"confirm step failed after submit: {exc}")
        if confirmation is None:
            return self._submission_unknown(page, job_id)
        return self._record_success(page, job_id, confirmation)

    def _submission_unknown(self, page: Any, job_id: int) -> RunResult:
        """The submit click happened but no confirmation was seen. Never retry automatically."""
        with transaction(self.conn):
            applications_repo.mark_submission_unknown(self.conn, job_id)
            self._event(
                job_id, EventType.SUBMISSION_UNKNOWN, "submit attempted, no confirmation detected"
            )
        artefacts = self._confirmation_artefacts(page, job_id)
        return RunResult(
            job_id,
            EXIT_SUBMISSION_UNKNOWN,
            "submitted but not confirmed: check Gmail or the site before doing anything else",
            artefacts,
        )

    def _confirmation_artefacts(
        self, page: Any, job_id: int, confirmation: Any = None
    ) -> dict[str, str]:
        artefacts: dict[str, str] = {}
        try:
            directory = self.job_dir(job_id)
            if confirmation is not None:
                path = directory / CONFIRMATION_JSON
                path.write_text(json.dumps(confirmation.to_dict(), indent=2), encoding="utf-8")
                artefacts["confirmation"] = str(path)
            artefacts["screenshot"] = str(self.screenshot(page, directory / CONFIRMATION_PNG))
        except (OSError, PlaywrightError) as exc:
            log.warning("job %s: confirmation evidence could not be saved: %s", job_id, exc)
        return artefacts

    def _captcha_blocked(self, page: Any, job_id: int, captcha_wait: float) -> RunResult | None:
        found = detect_captcha(page)
        if not found:
            return None
        self._note(job_id, f"{found}; waiting up to {captcha_wait:.0f}s for the human")
        if wait_for_human(page, captcha_wait, lambda p: detect_captcha(p) is None):
            return None
        self._event(job_id, EventType.CAPTCHA_BLOCKED, found)
        return RunResult(job_id, EXIT_CAPTCHA, f"captcha not cleared: {found}")

    def _record_success(self, page: Any, job_id: int, confirmation: Any) -> RunResult:
        with transaction(self.conn):
            job = jobs_repo.get_job(self.conn, job_id)
            stamp = job["submission_started_at"] or now_iso()
            if Status(job["pipeline_status"]) not in APPLICATION_STAGES:
                jobs_repo.set_job_status(
                    self.conn, job_id, Status.APPLIED, "submitted via filler", source="apply"
                )
            cover = self.settings.path("applications_dir") / str(job_id) / COVER_LETTER_FILE
            applications_repo.record_submission(
                self.conn,
                job_id,
                submitted_at=stamp,
                submitted_via=SUBMITTED_VIA,
                cover_letter_path=str(cover) if cover.exists() else None,
                confirmation_ref=confirmation.url or confirmation.text[:200],
            )
            self._event(job_id, EventType.SUBMITTED, confirmation.url)
        return RunResult(
            job_id,
            EXIT_OK,
            f"applied: {confirmation.text[:120]}",
            self._confirmation_artefacts(page, job_id, confirmation),
        )

    def _files(self, job_id: int) -> FileInputs:
        directory = self.job_dir(job_id)
        variant = directory / "resume.pdf"
        resume = variant if variant.exists() else self.settings.path("resume_pdf")
        cover_pdf = directory / COVER_LETTER_PDF
        return FileInputs(
            resume_pdf=resume, cover_letter_pdf=cover_pdf if cover_pdf.exists() else None
        )
