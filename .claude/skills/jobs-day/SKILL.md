---
name: jobs-day
description: The one-command morning workflow. Syncs the inbox first, then discovers and scores fresh jobs, then shows three action groups (ready to apply, referral recommended, needs your input), executes what the user approves, and ends with one progress screen. Runs /jobs-sync, /jobs-scan, /jobs-review, /jobs-apply and /jobs-refer in the right order so nothing has to be coordinated by hand.
allowed-tools: Bash(uv run jobhunt *) Read Write Glob AskUserQuestion mcp__claude_ai_Gmail__search_threads mcp__claude_ai_Gmail__get_thread mcp__claude_ai_Gmail__create_draft mcp__playwright__browser_navigate mcp__playwright__browser_snapshot mcp__playwright__browser_click mcp__playwright__browser_type mcp__playwright__browser_fill_form mcp__playwright__browser_select_option mcp__playwright__browser_file_upload mcp__playwright__browser_take_screenshot mcp__playwright__browser_wait_for mcp__playwright__browser_close mcp__firecrawl__firecrawl_scrape mcp__firecrawl__firecrawl_search
argument-hint: "[--skip-gmail] [--skip-firecrawl] [--no-apply] [--no-referrals]"
---

# /jobs-day

Run from the project root. One pass through the whole loop. Read the other
skills' SKILL.md files when you reach their step and follow them; this file only fixes the order,
the grouping and the stopping rules. `$ARGUMENTS` may contain `--skip-gmail` (skip email access),
`--skip-firecrawl` (skip paid MCP discovery), `--no-apply` (no application submissions)
and `--no-referrals` (skip referral outreach).
Direct job-source discovery still uses the network when Gmail is skipped.

Rules that apply to every step: never invent a job, URL, answer or score; stop and ask instead
of guessing; nothing is submitted or sent without an explicit yes in this session; LinkedIn is
never automated.

## 0. Open the workspace immediately

Before health checks, Gmail or scanning, start the dashboard with Bash `run_in_background`:

```
uv run jobhunt serve
```

It opens an authenticated local browser tab automatically. If the matching server is already
running, the command reuses it and exits successfully; that is not an error. Give the user the
clean localhost URL printed in the task output. Do not read or expose the private server-token
file. If an unrelated process occupies the default port, retry with `uv run jobhunt serve --port 0`;
do not stop that process. If browser opening fails, ask him to run `uv run jobhunt serve` locally.
Leave the server running for the session, including when a later setup check fails.

The page refreshes all views automatically. Approve saves the user's interest; it does not prepare
or submit a form. The dashboard also supports manual application/referral records and confirmed
pipeline moves. Re-read the plan after browser actions; do not repeat records the user already saved.
Claude preparation is optional. Unscored manual approvals still do not satisfy automatic-submission
checks. Clipboard buttons show a prompt to paste into this chat; they do not execute or send it.

Publish the actual stage outcome with `dashboard-progress`: `running` before work, then `done`,
`failed` or `skipped`. Never mark a failed or unperformed step done. Keep messages brief and free
of private answers, mail bodies and tokens. Progress updates only change the dashboard, not the
underlying workflow. Start each invocation with:

```
uv run jobhunt dashboard-progress health --state running --new-run --message "Checking local setup"
uv run jobhunt doctor
```

If anything is `FAIL` or the check errors, record `health --state failed --message "Setup needs attention"`,
report the error and stop. Otherwise record `health --state done`. `WARN` lines are reported at the end.

## 1. Sync existing applications first

With `--skip-gmail`, skip all Gmail tools and checkpoint updates, but still ingest existing local
message files and offer their queued review items using `/jobs-sync` sections 1–4 with `--skip-gmail`. Record
`sync --state skipped --message "Gmail skipped; local inbox checked"`. Otherwise record
`sync --state running --message "Checking application updates"` using
`uv run jobhunt dashboard-progress`, then follow `.claude/skills/jobs-sync/SKILL.md` sections 1 to 4
(search, write message files, `mail ingest`, present `mail review` items with AskUserQuestion,
`mail resolve`). Run `uv run jobhunt mail ghost-check` too. Record `sync --state done` only when
these steps succeed, or `sync --state failed` with a brief explanation. Do not draft follow-up
emails yet; they go in step 5.

## 2. Find and rank fresh jobs

Record `scan --state running --message "Finding and scoring matching roles"` using the progress
command. Follow `.claude/skills/jobs-scan/SKILL.md` sections 1 to 4 (`scan`, alert emails,
metered Firecrawl MCP, `ingest-inbox`, `prefilter`, score every batch), forwarding `--skip-gmail`
and `--skip-firecrawl` if supplied. The scheduler covers native sources, not the MCP step. Record
`scan --state failed` if any source or scoring step failed, otherwise `scan --state done`.
Continue with available results and report partial failures; do not hide them as an empty queue.

Two things are not optional in this step, every run:

- **Nothing is left unscored.** Keep exporting and importing until `score-batch --export` reports
  0 jobs. If jobs are held back because they have no description, hydrate them first (rerun
  `scan`), then score them. Report any job you could not score, and why.
- **Every parked job gets an eligibility verdict.** The export marks them `eligibility_review`;
  each result must carry `eligibility` of `eligible`, `not_eligible` or `unclear`, judged only
  from the posting text. An `eligible` verdict returns the job to the pipeline. This clears the
  "Verification & preparation" queue instead of letting it grow.

## 3. Review the live plan

The dashboard is already open. Record `review --state running --message "Waiting for your review"`,
then read the same data as text:

```
uv run jobhunt plan --json
```

Show the groups in the terminal too, so both views agree. If he says he clicked something,
re-read `plan --json` rather than assuming what changed. In this order, counts in the headings:

1. **Ready to apply**: `ready_to_apply` (approved, prefilled, route is apply, referral received
   or referral deadline passed). Show company, title, score, route reason.
2. **Referral recommended**: `referral_recommended` (a strong contact exists and the posting is
   fresh). Show the top contact and the reason. These are not applied to today unless the user
   says "apply anyway".
3. **Needs your input**: `needs_input.to_review` (shortlisted, not yet decided: run the
   `/jobs-review` step 2 approve/skip/snooze questions for them), `to_prepare` (already approved
   but not prepared: follow `/jobs-review` sections 3 to 5 for these job IDs, including `prefill`,
   without reloading the default shortlisted queue),
   `unanswered_questions` (answer with `answers learn`), `blocked` (login wall or an unconfirmed
   submission: explain what to check), `referral_deadline` (offer to apply directly now; review
   shortlisted jobs and prepare approved ones before submission), `mail_review` (already handled
   in step 1; list anything left), and `eligibility_review` (not shortlisted: verify India-eligible
   remote work or both visa sponsorship and relocation assistance on the official posting).
   Never turn unclear overseas eligibility into approval or change private work-authorization answers.
   Rebuild the plan after review or preparation.

Each job has one group. `awaiting_referral` is shown as one line each under Progress, not as
an action. Future-snoozed jobs are never actionable, including with `plan --no-referrals`;
that option skips new suggestions but still shows existing referral waits and outcomes.

When he finishes reviewing, re-read the plan and record `review --state done`. If he defers the
review, record `review --state skipped` and do not assume that any jobs were approved.

## 4. Execute what the user approves

Use `uv run jobhunt dashboard-progress applications --state running` before the application
block. Record `skipped` instead when `--no-apply` is supplied or nothing was selected. When the
block ends, record `done` only for verified results, otherwise `failed` with the blocked reason.
Use the same running/outcome pattern for `referrals`; skip all referral work with `--no-referrals`
or when no outreach was selected. A completed outreach step means requests were handled, not
that a referral has been received.

- **Applications**: one AskUserQuestion listing the ready jobs (multiSelect), then follow
  `.claude/skills/jobs-apply/SKILL.md` for the selected ones. Stop on exit 10, 11, 13, 16 and 20
  exactly as that skill says; never retry an unconfirmed submission. Skip this block on
  `--no-apply`.
- **Referrals**: for each job the user picks from the referral group, follow
  `.claude/skills/jobs-refer/SKILL.md`: `refer suggest`, choose the contact, approve the hook,
  `refer draft`, `outreach open`, ask "Sent?", `outreach mark-sent`. Marking a referral request
  sent parks the job (`snooze_until` = today + `routing.wait_days` from `config/referrals.yaml`)
  and it reappears under "Referral deadline passed" (or "Ready to apply" if already prepared)
  if no referral is completed by then. Record confirmed completion with
  `uv run jobhunt outreach mark-replied <id> --referred`; the job is unparked and appears under
  preparation or "Ready to apply" as appropriate. Record refusal with `--declined` instead.
  These flags are mutually exclusive. Ordinary replies, including an offer to refer later,
  remain pending; response text never proves completion. A refusal unparks the job only when
  no other referral request remains pending. Follow-up replies use the same flags.

## 5. Follow-ups

For `progress.follow_ups_due` and jobs ghosted in step 1, offer one Gmail draft each from
`config/templates/followup_ghost.md` (create the draft only after a yes; never send).
With `--skip-gmail`, offer text in chat only; do not call a Gmail tool.

## 6. One progress screen

```
uv run jobhunt dashboard-progress finish --state running --message "Preparing the run summary"
uv run jobhunt plan
uv run jobhunt dashboard-progress finish --state done --message "Run finished; review any flagged stages"
```

The dashboard from step 0 is already live and refreshes itself, so do not start a second one.
Earlier failed stages remain flagged even after the summary is complete. Only run
`uv run jobhunt dashboard` when he wants a read-only static snapshot; do not publish private
tracker data unless explicitly requested.

Before the summary, verify the daily rules in `CLAUDE.md` rather than assuming them:

```
uv run jobhunt doctor
uv run jobhunt score-batch --export data/scoring/check.json
```

`doctor` must report `everything scored` as ok, and the export must report 0 jobs. If either
fails, the scoring pass is unfinished: hydrate, export, score and import again until both pass,
then delete the check file. Report the counts for today and for all jobs, and if the all-jobs
count fell since the last run, say which of the three permitted causes explains it (a decision
the user made, a retired unlisted job, or a move to a post-application stage). An unexplained
drop is a bug to report, not something to fix by re-scanning.

Report in this shape, 15 lines or fewer: sources ok/failed and mail sync time; emails processed
and status changes made; jobs discovered, scored, shortlisted; applications submitted today with
their confirmation evidence (`confirmation_ref` or "awaiting confirmation email"); referral
requests sent, awaiting reply, and referrals received; interviews and assessments coming up;
follow-up drafts created (not sent); the `doctor` warnings. Suggest the next `/jobs-day`.
