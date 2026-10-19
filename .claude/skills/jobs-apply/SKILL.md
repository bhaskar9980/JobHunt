---
name: jobs-apply
description: Submit the applications the user approved in /jobs-review. Python fillers handle Greenhouse, Lever and Ashby after a final confirmation; Workday, Naukri and Instahyre are driven step by step in the Playwright MCP browser with the user watching; LinkedIn-only jobs are handed to him manually. Never submits anything unapproved or unconfirmed.
disable-model-invocation: true
allowed-tools: Bash(uv run jobhunt *) Read AskUserQuestion mcp__playwright__browser_navigate mcp__playwright__browser_snapshot mcp__playwright__browser_click mcp__playwright__browser_type mcp__playwright__browser_fill_form mcp__playwright__browser_select_option mcp__playwright__browser_press_key mcp__playwright__browser_wait_for mcp__playwright__browser_file_upload mcp__playwright__browser_take_screenshot mcp__playwright__browser_tabs mcp__playwright__browser_close
argument-hint: "[--dry-run] [job_id ...]"
---

# /jobs-apply

Run from the project root. `$ARGUMENTS` may contain `--dry-run` (fill and
screenshot, never submit) and/or specific job ids.

## 1. What is ready

```
uv run jobhunt queue --status approved --limit 50
```

Split the list by `ats`: `greenhouse` / `lever` / `ashby` with `prefill_state` = `ready` go to
step 2; `workday`, `smartrecruiters`, `naukri`, `instahyre`, `cutshort`, `wellfound` go to step 3;
`linkedin` or unknown go to step 4. Anything with `prefill_state` != `ready` is sent back to
`/jobs-review`.

## 2. Python fillers (Greenhouse, Lever, Ashby)

Ask once with `AskUserQuestion`: "Submit N applications now (daily remaining X)? Companies: ...".
Options: "Submit all", "Dry run first", "Pick a subset", "Not now". Only proceed on an explicit
yes. Then for each job:

```
uv run jobhunt apply <job_id> [--dry-run]
```

Act on the exit code:

| code | meaning | what to do |
|---|---|---|
| 0 | applied (or dry run done) | report `confirmation.json` / `prefilled.png` path |
| 10 | CAPTCHA | the headed browser is waiting; ask the user to solve it, rerun once |
| 11 | login wall | ask him to log in in that window, rerun once |
| 12 | already-applied indicator | ask the user to verify that the indicator is for this job. If confirmed, record it with `mark-applied`; do not dismiss an existing application or submit again. |
| 13 | new/changed question or changed saved answer | the refreshed schema and answer set are saved. Review them with the user; learn genuinely missing answers via `/jobs-review` step 4. If answers are already complete, renew submission approval before retrying; do not invent another question. |
| 14 | validation error, nothing was submitted | read `data/applications/<id>/run.log`; a retry is safe because 14 means the form never left the browser; retry once, then use the assisted path (step 3) |
| 15 | safety gate | report the reason (cap, score, duplicate, unconfirmed submission); never override |
| 16 | submitted but NOT confirmed | **never rerun `apply`**. Ask the user to open Gmail or the ATS in his own browser and verify the outcome (a screenshot may be available at `data/applications/<id>/confirmation.png`). If it went through: `uv run jobhunt mark-applied <id> --via ats_form`. Only after he explicitly confirms it did not: `uv run jobhunt reconcile-submission <id> --not-submitted --note "<what he checked>"`, then `uv run jobhunt prefill <id>` and ask for submission approval again. Re-running prefill or learning answers alone never clears this block. |
| 20 | not a python ATS | step 3 or 4 |

Never run `apply` more than twice for the same job in one session, and never after exit 16 without the check above.

## 3. Assisted path (Workday, SmartRecruiters, Naukri, Instahyre)

Read `ats-notes.md` in this skill folder first. Then:

1. `uv run jobhunt answers sheet <job_id>` (prints profile + answers; this is your source of truth).
2. `mcp__playwright__browser_navigate` to the apply URL. If a login is required, tell the user and
   wait; never type passwords.
3. Page by page: `browser_snapshot`, then `browser_fill_form` / `browser_select_option` /
   `browser_file_upload` (resume path from `config/settings.yaml: paths.resume_pdf`) using only
   values from the sheet. Unknown question: `AskUserQuestion`, then
   `uv run jobhunt answers learn --job <id> --question "..." --answer "..."`.
4. On the review page: `browser_take_screenshot` to `data/applications/<id>/prefilled.png`.
5. With `--dry-run`, stop here: never reserve or click Submit. Otherwise ask with `AskUserQuestion`:
   "Submit this application to <company>?" Only after yes, run `uv run jobhunt reserve-application <id>`.
   This checks the same limits, duplicate protection, readiness and unresolved-attempt rules as the
   Python runner. Stop on any nonzero exit; do not click Submit. After a successful reservation,
   make the single supervised submit click. Do not call Python `apply` for a reserved attempt.
6. After verified confirmation: screenshot to `confirmation.png`, then
   `uv run jobhunt mark-applied <job_id> --ref "<confirmation text or URL>" --screenshot data/applications/<id>/confirmation.png --via assisted`.
   If the click, navigation or confirmation is uncertain, leave the reservation pending and follow
   the exit-16 verification procedure. Only verified non-submission permits reconciliation and a retry.

Respect the Workday daily cap in `config/settings.yaml`. One application at a time.

## 4. Manual (LinkedIn or no supported URL)

LinkedIn is never automated. Print the job, its URL and the answer sheet. With `--dry-run`, this
is a preview only: do not ask him to submit or record an application. Otherwise, when he explicitly
chooses to proceed, run `uv run jobhunt reserve-application <id>` before handing off the submission.
Stop on a failed gate. When he confirms it was submitted, record it with
`uv run jobhunt mark-applied <job_id> --via manual`; if he did not submit, use explicit reconciliation.

## 5. Report

`uv run jobhunt list --status applied --since 24h` and a summary: submitted (company, title,
confirmation), blocked (code and reason), handed to the user. Suggest `/jobs-sync` in a few days.
