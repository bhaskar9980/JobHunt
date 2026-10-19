# JobHunt

Personal job-hunt assistant: discovers roles from ATS public APIs, alert emails and remote boards,
scores them against `config/profile.yaml`, tracks every job in SQLite, and applies only after
explicit approval. Claude Code skills (`/jobs-scan`, `/jobs-review`, `/jobs-apply`, `/jobs-sync`,
`/jobs-refer`, `/jobs-status`) provide the judgement; `uv run jobhunt <cmd>` does the deterministic work.

No LinkedIn automation of any kind. Applications are never submitted unattended.

## One-time setup

```
uv sync                                   # Python deps (Playwright uses system Chrome, no download)
npm install                               # @playwright/mcp for .mcp.json (assisted browser path)
copy config\answers.example.yaml config\answers.yaml   # then fill in email, phone, CTC, notice
copy config\profile.example.yaml config\profile.yaml   # then fill in name, company, one-liner, links, pay expectation
copy .env.example .env                                 # then set the resume paths
uv run jobhunt init-db
uv run jobhunt facts                      # data/resume_facts.md from the resume .tex
```

`config/profile.yaml`, `config/answers.yaml` and `.env` are gitignored. Real submissions are refused while `answers.yaml`
still holds the example placeholders. The first `/jobs-scan` and `/jobs-sync` must run
interactively so the Gmail connector can authenticate.

Optional daily scan (Task Scheduler; the command is also at the top of `scripts/daily_scan.ps1`):

```
schtasks /Create /F /TN "JobHunt\DailyScan" /SC DAILY /ST 08:30 /RL LIMITED /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"C:\path\to\JobHunt\scripts\daily_scan.ps1\""
```

## Daily flow

Type `/jobs-day` and follow the prompts. It opens or reuses the interactive local dashboard
immediately, then checks setup, syncs the inbox, discovers and scores, shows your action queues,
executes what you approve, and ends with one progress screen. The dashboard shows the actual
workflow stages while the skill runs and refreshes its views automatically. `uv run jobhunt plan`
prints that view on its own; `uv run jobhunt doctor` checks the setup; `uv run jobhunt
backup` copies the database, drafts and evidence to `backups/` (gitignored, keep a copy
elsewhere).

| Step | Skill | CLI underneath |
|---|---|---|
| Discover and score | `/jobs-scan` | `scan`, `ingest-inbox`, `prefilter`, `score-batch`, `dashboard` |
| Approve, prefill, draft letters | `/jobs-review` | `queue`, `decide`, `prefill`, `answers learn`, `tailor check` |
| Submit approved jobs | `/jobs-apply` | `apply`, `apply --dry-run`, `answers sheet`, `mark-applied` |
| Read confirmations, rejections, invites | `/jobs-sync` | `mail last-sync`, `mail ingest`, `mail review`, `mail resolve`, `mail ghost-check` |
| Ask for referrals | `/jobs-refer` | `refer import-connections`, `refer suggest`, `refer draft`, `outreach open`, `outreach mark-sent`, `outreach due` |
| Where things stand | `/jobs-status` | `stats`, `follow-ups`, `list`, `show`, `serve`, `dashboard` |

Handy direct commands:

```
uv run jobhunt list --status shortlisted --min-score 70
uv run jobhunt show <id>
uv run jobhunt set-status <id> interview --note "phone screen Friday"
uv run jobhunt apply <id> --dry-run           # fills the form, screenshots, never submits
uv run jobhunt serve                          # dashboard with working approve/skip buttons
uv run jobhunt dashboard                      # same page as a static file, for your phone
```

`serve` runs on 127.0.0.1 only, opens an authenticated tab, and reuses the matching server on
subsequent calls. The printed URL contains no access token. A tab without access shows only an
empty shell; run `serve` again to open it securely. Private APIs require the per-server token,
which the browser receives in its URL fragment and keeps in tab session storage. Tokens are
not written to request logs or sent as referrers to job sites.

The dashboard is a manual-first tracker. Approve saves a job to your list; it does not prepare
or submit an application. The Approved tab includes every approved job, including referral waits.
The separate preparation count describes optional assistant preparation, not a requirement for
applying manually. Unscored jobs can be approved for manual tracking without bypassing any of the
Python runner's scoring, readiness, duplicate or submission checks.

Open a posting, apply yourself, then choose **I applied** on the job card or detail panel. Confirm
the date, channel and verification note to save the completed application. For referrals, choose
**Referral requested** after sending the message, select a contact or enter a name, and record the
date. Later record **Referral received**, **Declined**, or **Reply only**; a reply alone does not
prove that a referral was completed. Opening an external page never records an application or message.

### Fill for review

For an approved job on a supported direct Greenhouse, Lever or Ashby URL, open its details and
choose **Fill for review**. This reuses the existing Python fillers and saved answer bank; it does
not require a separate CLI prefill or a Claude session. Score, profile, duplicate, referral-wait
and unresolved-submission guards still apply, and placeholder answers are refused before opening
any form. Workday and the other assisted/manual providers remain unavailable for this button.

The worker opens a visible Chrome window using the existing dedicated browser profile. If login
or a CAPTCHA is needed, handle it yourself, then choose **Continue filling** in the dashboard.
Known fields and the resume are filled; unknown questions stay blank and are listed for you to
complete. This mode fills the current page only and never clicks Next or Submit. Review/edit the
form and click Submit on the employer's site yourself. Employers may autosave drafts and uploads
before that final click, so filling is permission to share those details with the selected ATS.

The browser stays open for review. Only one fill session can use the profile at once, including
while you review it. Closing the detail drawer does not stop the session; reopen that job to see
its status and controls. **Close fill window** requires confirmation and may discard unsaved
employer-side edits. Cancellation is checked between field operations. Closing a session never
marks a job applied; after a verified submission use **I applied**. Keep the server running during
review; session controls are local to that process and do not automatically resume after a restart.

The private endpoints are `POST /api/fill-review`, `GET /api/fill-status`, and
`POST /api/fill-control`. Start requests are idempotent by request ID, and controls are tied to the
current run ID. The old dashboard `POST /api/apply` submit endpoint has been removed. The explicit
CLI `apply` command is unchanged. Review schemas and resolved answers are saved separately under
`data/applications/<job_id>/review/<run_id>/`, without overwriting prepared CLI application inputs.
Restart the existing dashboard process and reopen it with `uv run jobhunt serve` to load the new
button; an old process still serves its old code.

**Active applications** includes applied, screening, interview and offer stages. **Follow-ups due**
includes application and referral/outreach reminders and stays zero until a recorded action's due
date arrives. The default application interval is in `follow_up.days_after_apply`; referral dates
follow `referrals.yaml`. Run health shows the due items, morning progress and email-review notices.

Pipeline cards support desktop drag-and-drop for valid transitions. Dropping a card opens a
confirmation or recording form; it never submits an application. On mobile or with a keyboard,
open the job and use its actions/status controls. Rejected, withdrawn and ghosted records remain
visible in the tracker and pipeline. Saved decisions, applications, referral records and notes
survive closing the server; unsaved text is not a durable record.

Claude is optional: copy preparation/referral prompts from the detail panel into your Claude Code
terminal. The handoff dialog displays the complete prompt and copy feedback, with selectable text
when clipboard permission is denied. No button launches Claude, submits a form or sends a message.

Refresh interval, page size and port are in `config/settings.yaml: dashboard`. The skill uses
`uv run jobhunt dashboard-progress <stage> --state running|done|failed|skipped` to report progress;
`--new-run` resets the stages at the start. Local server metadata lives under `data/` and remains gitignored. Workflow progress, the completed
mail-sync checkpoint, and the matching-profile fingerprint are stored transactionally in SQLite.
Schema v3 adds only the `runtime_state` table; existing jobs, applications and evidence remain.
A dashboard running an older code build or settings snapshot is not reused. `dashboard` still
writes a self-contained read-only snapshot.

Source or profile changes invalidate machine-generated matching decisions, without undoing human
skips or erasing application history. Approved jobs require fresh preparation after relevant
changes. Auto-unlisting is deliberately limited to the unbounded Greenhouse and Ashby board feeds;
filtered or capped ATS searches are not treated as proof that a job closed. Workday, Eightfold and
SuccessFactors requisition IDs are tenant/employer-scoped to prevent cross-company collisions.
Existing records with the same posting URL are reused rather than duplicated.

Only a completely fetched and successfully ingested Gmail pass advances the sync checkpoint:
`uv run jobhunt mail sync-complete --through "<start timestamp>"`. The skills handle this step.
`mail last-sync --json --days N` provides an explicit search window. Alert ingestion and application
mail use separate, message-level deduplication records; older records remain recognized.

Assisted/manual submission uses `uv run jobhunt reserve-application <id>` after the user's final
yes and immediately before submitting. Python `apply` reserves automatically. Both paths enforce
daily limits including unresolved attempts; never call Python `apply` after reserving manually.
Reconcile an uncertain attempt before retrying. Changed questions or saved answers require another
review, and failed preparation cannot leave an old answer sheet marked ready.

`JOBHUNT_DATA_DIR` now redirects data-relative runtime paths and the default database together.
An explicit `JOBHUNT_DB_PATH` still takes precedence. Existing files are not moved automatically.

## Sources

| Source | How it is read | Config |
|---|---|---|
| Greenhouse, Lever, Ashby, SmartRecruiters, Workday | Public ATS JSON APIs, per company | `companies.yaml` |
| HN Who is hiring, Remotive, We Work Remotely | Algolia API, JSON API, RSS | `sources.yaml: aggregators` |
| jobfound.org | Public RSS feed, then the job page for the real apply link | `sources.yaml: aggregators.jobfound` |
| Tal "jobs in the wild" (Bangalore) | Public listing page, then the post page | `sources.yaml: aggregators.tal_jobs_in_the_wild` |
| LinkedIn, Naukri, Indeed, foundit, Hirist, ZipRecruiter, Welcome to the Jungle, Instahyre, Cutshort, Wellfound | Your job-alert emails, read by `/jobs-scan` through the Gmail connector | `sources.yaml: gmail_alerts` |

The email boards have no public API and forbid scraping, so set up a job alert on each site
(daily, your target titles and cities) and the scan reads the alerts. Indeed retired its RSS
feeds in 2026 and ZipRecruiter's API needs a partner key, so both are email-only too.

### Worldwide .NET/C# coverage and eligibility

The watchlist has 169 company entries, including 34 public APIs verified during the 2026-09-10
expansion. A working company feed does not mean every indexed vacancy is still open or eligible.
Workday and Eightfold use deduplicated C#, .NET and ASP.NET query variants without India-only
search constraints. Direct feeds run first; existing canonical job URLs are not paid-scraped again.
APIs and search engines still have pagination, indexing and freshness limits: coverage is not exhaustive.

Existing India preferences remain. Additional overseas matches need either explicit remote eligibility
from India/worldwide or **both** visa sponsorship and relocation assistance. Unknown/contradictory
eligibility is excluded from the normal job list and shortlist and appears under **Verification &
preparation** / `plan.needs_input.eligibility_review`. Full descriptions are fetched before deciding
when possible. Rules in `locations.yaml: international_eligibility` are conservative text checks,
not legal eligibility verification; unfamiliar wording may need manual review. Existing applications
and approvals remain in the tracker. Private work-authorisation answers are unchanged.

### Firecrawl MCP supplement

`/jobs-scan` and `/jobs-day` use the existing Firecrawl MCP connection; no separate Python API key
is required. A standalone Task Scheduler/Python scan cannot invoke the assistant's MCP tools.
`--skip-firecrawl` skips this supplement. A fully offline `/jobs-scan` also needs `--skip-scan --skip-gmail`.

```
uv run jobhunt firecrawl status
uv run jobhunt firecrawl plan
uv run jobhunt firecrawl complete <request_id>
uv run jobhunt firecrawl complete <request_id> --response-file <raw-mcp-json>
uv run jobhunt firecrawl fail <request_id>
uv run jobhunt firecrawl leads
```

Planning transactionally reserves one request before the skill calls it. A pending request is never
reissued automatically; complete its receipt or mark it failed. Repeated completions are idempotent,
and failures retain their estimated allocation. Search snippets create leads only. A live, verified,
individual employer posting with verbatim evidence is required before the normal dedupe pipeline
receives a job. Receipts stay under `data/firecrawl/`; the ledger and URL cache live in SQLite.
Unrecognised employer sources remain in a separate verification queue until their official site is
verified. LinkedIn and the email-only boards are never scraped by this supplement.

`config/sources.yaml: firecrawl` sets the 1,000-credit local monthly allocation, 30-credit daily cap,
UTC reset day (default 1), query cache and rotating searches. Standard estimated costs are 2 credits
per ten search results and 1 for a basic page scrape; premium extraction, PDFs, stealth proxies,
crawls and agent jobs are not planned. These are **local allocation limits**, not provider billing
or account-balance guarantees. Other projects/manual MCP calls are not tracked. Verify your billing
reset day and provider pricing, and use Firecrawl account/key spending controls and disable paid
overages if an account-wide financial limit is required.

## Layout

- `config/` profile, watchlist (`companies.yaml`), sources, answer bank, ATS selectors (`ats/`),
  mail rules, referral weights, outreach templates. Secrets never live here.
- `jobhunt/` the Python package: `sources/` fetchers, `repo/` SQLite access, `match.py` prefilter,
  `scoring.py` LLM batch export/import, `apply/` Playwright fillers and safety gate, `mail/`
  classifier and status transitions, `referral/` importer, ranker and drafts.
- `data/` (gitignored) `jobs.db`, `inbox/`, `mail_inbox/`, `applications/<id>/`, `outreach/<id>/`,
  `dashboard.html`, `resume_facts.md`.
- `.claude/skills/` the six skills; `.claude/settings.json` allow/ask/deny lists; `.mcp.json`
  Playwright MCP with linkedin.com blocked.

## Tests

```
uv run pytest tests/unit -q --tb=short
uv run ruff check jobhunt tests scripts && uv run ruff format --check jobhunt tests scripts
```

Everything runs offline: HTTP is mocked with respx, forms load from saved HTML fixtures, Gmail and
the browser are never touched in tests.
