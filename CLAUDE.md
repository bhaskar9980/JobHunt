# JobHunt

A personal job-application assistant. Deterministic work (fetch, dedupe,
store, prefilter, fill known forms) lives in the `jobhunt` Python package. Judgement (scoring a
job, reading an alert email, drafting outreach) is done by Claude inside a skill that calls the
CLI. Run everything from the project root with `uv run jobhunt <cmd>`.

## Daily rules

These four rules define what a correct daily run looks like. They hold no matter which model
runs the loop, so they are enforced by tests and by `uv run jobhunt doctor`, not by memory.
If a change makes one of them false, the change is wrong.

### 1. A run is additive, never lossy

The same posting appears on a board again tomorrow. `repo.jobs.upsert_job` matches an incoming
posting to an existing row on `(source, source_job_id)`, then canonical URL, then fingerprint of
company, title and location. A repeat listing refreshes content and never creates a second row.
`_refresh` deliberately does not touch `status`, so a re-listing cannot rewrite a decision.

Only three things may reduce the visible count, and each is a decision or a fact, never drift:

- The user skipped or applied to the job.
- A full-board feed stopped listing it, so `pipeline._retire_unlisted` retired it. Bounded
  searches never retire anything, because a search that returns 20 of 200 jobs cannot prove a
  job closed.
- The job moved to a post-application stage and now lives on the kanban instead.

A count that drops for any other reason is a bug. Investigate it rather than re-running the scan.

Retirement is the one reducer that can reverse itself, because a board listing a job again
proves it is open. `repo.jobs._revive_if_relisted` returns such a job to `discovered` and clears
its dismissal, so the next prefilter and scoring pass re-files it. It fires only for a dismissal
whose recorded cause was unlisting, never for a decision the user made.

### 2. A decision is permanent

A skipped job is `dismissed` and must never come back. `jobs.dismissed_reason` records why a job
was dismissed, and only `'unlisted'` is reversible; `'skipped'` never is. Three paths could
revive a job and all three respect that: `repo.jobs.prefilter_candidates` and
`repo.jobs.mark_unlisted` exclude `dismissed` by status, and `repo.jobs._revive_if_relisted`
checks the reason before acting. Never widen any of them, and when the cause of a dismissal
cannot be established, record `'skipped'` so the job stays where it is.

### 3. Nothing visible is unscored

Every job the dashboard shows must carry a score, including jobs parked for eligibility
verification. `doctor` reports this as `everything scored` and fails when it is false. So a run
is not finished until `score-batch --export` reports 0 jobs. Export, score, import, and repeat
until the export is empty. Hydrate first when jobs lack a description, because a job with no
description cannot be scored honestly.

### 4. Eligibility is decided every run, from the posting text only

Jobs the prefilter cannot judge are parked as `filtered` with a reason containing
`eligibility needs verification`. Every parked job gets a verdict on every run. The rules live in
`config/scoring_rubric.md` and are repeated per job in the export. `not_eligible` requires an
explicit restriction the employer stated. A foreign location alone is never one. Never infer
eligibility from an employer's size or reputation, and never change the private
work-authorisation answers in `config/answers.yaml`.

## Safety rules

- Nothing is submitted or sent without an explicit human click in the session.
- Never scrape LinkedIn, never automate Easy Apply or LinkedIn messaging. LinkedIn is read only
  through the job-alert emails it sends, and outreach is drafted for the user to send by hand.
- `data/dashboard-server.json` holds a private per-session dashboard token. Never repeat it in a
  reply, and strip `#t=` or `#token=` from anything shown to the user.
- Secrets come from environment variables. `.env` and `data/` are gitignored.
- Never commit or push without asking first.

## Working on the code

- Tests before implementation. `uv run pytest tests/unit -q`, then `uv run ruff check jobhunt tests`.
- Never use a real external service in a test.
- `jobhunt/match.py` imports `jobhunt.repo.jobs`, so `repo/jobs.py` must not import `match`.
  Shared constants live in `jobhunt/models.py`; shared reason helpers in `jobhunt/prefilter_reasons.py`.
- The running `serve` process holds the template at import time, so restart it to see a
  dashboard change. A restart mints a new token and strands open tabs, so restart once and
  reopen the tab with `uv run jobhunt serve`.
- The invariants above are covered by `tests/unit/test_daily_invariants.py`. Read it before
  changing status handling, dedupe or the dashboard's row query.
