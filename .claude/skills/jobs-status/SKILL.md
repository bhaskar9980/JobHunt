---
name: jobs-status
description: Show the job-hunt funnel (discovered, shortlisted, approved, applied, screening, interview, offer), follow-ups due, recent scan health, and open or publish the dashboard.
allowed-tools: Bash(uv run jobhunt *) Read Artifact
argument-hint: "[--publish]"
---

# /jobs-status

Run from the project root.

1. `uv run jobhunt plan` (health line first: last scan, failed sources, mail sync), then
   `uv run jobhunt stats --json`
2. `uv run jobhunt follow-ups --json`
3. `uv run jobhunt list --status shortlisted --status approved --limit 15`
4. Run `uv run jobhunt serve` with Bash `run_in_background`. It opens or reuses the authenticated
   local dashboard; a successful exit on reuse is normal. Share only the clean URL printed by the
   command, not the private access token. Do not reset progress from an ongoing `/jobs-day` run.
5. If `$ARGUMENTS` contains `--publish`, first run `uv run jobhunt dashboard`, then publish
   `data/dashboard.html` with the Artifact tool
   (favicon 🎯, title "JobHunt Dashboard") so it can be viewed on a phone. Otherwise do not publish.

Distinguish "no matching jobs" from "sources failed": if the plan's health shows failed
sources or a stale scan, say so before any funnel number.

Summarise in at most 10 lines: funnel counts that are non-zero, discovered in the last 24h,
response rate, follow-ups due (company, title, days since last activity), and the next action
(`/jobs-review` if shortlisted > 0, `/jobs-apply` if approved > 0, `/jobs-sync` if applied > 0).
