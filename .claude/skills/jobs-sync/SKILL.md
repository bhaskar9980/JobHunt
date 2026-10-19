---
name: jobs-sync
description: Inbox sync. Pulls application confirmations, rejections, interview and assessment invites, offers and recruiter outreach from Gmail into data/mail_inbox as JSON, lets the deterministic classifier move application statuses, asks about anything ambiguous, runs the ghost check and offers follow-up drafts (never sends).
allowed-tools: Bash(uv run jobhunt *) Read Write Glob AskUserQuestion mcp__claude_ai_Gmail__search_threads mcp__claude_ai_Gmail__get_thread mcp__claude_ai_Gmail__create_draft
argument-hint: "[--days N] [--skip-gmail]"
---

# /jobs-sync

Run from the project root. Arguments: `$ARGUMENTS` may contain `--days N`
(override the lookback) and/or `--skip-gmail` (only ingest files already in `data/mail_inbox/`).

Never invent a field, a status or a job id. Python decides every status change; you only fetch
emails and relay questions. Nothing here sends an email.

## 1. Work out the lookback and the queries

```
uv run jobhunt mail last-sync --json
```

If `--days N` was supplied, append `--days N` to this command. Use its rendered `queries` and
`search_after` boundary verbatim. Save the returned `now` as this pass's start timestamp.
`last_sync` records the last fully completed Gmail fetch, not ingestion or manual resolution.
If it is null, the default lookback applies. Follow every page of search results; if retrieval
is incomplete or a tool fails, report that and do not advance the checkpoint.

## 2. Pull messages into data/mail_inbox (skip when `--skip-gmail`)

One JSON file per *message*, not per thread: a thread keeps growing (the confirmation arrives
first, then interview rounds, assessments or recruiter replies arrive days later). Even another
message of the same kind is new evidence; only the same Gmail message id is a replay. Never skip
a thread or kind because it was seen before.

1. For each `kind` in `queries`, call `mcp__claude_ai_Gmail__search_threads` with that query.
   A thread found by two queries is handled once, with the first `kind` as `query_tag`.
2. Read every thread returned with `mcp__claude_ai_Gmail__get_thread` (pass `messageFormat: PLAIN_TEXT`).
3. For every message in the thread whose date is on or after `search_after`, check with `Glob`
   whether `data/mail_inbox/<thread_id>_<message_id>.json` or
   `data/mail_inbox/processed/<thread_id>_<message_id>.json` already exists; skip those.
4. `Write` one file per remaining message to `data/mail_inbox/<thread_id>_<message_id>.json`
   using that message's own `from`, `date`, `subject` and body:

```json
{
  "schema_version": 1,
  "thread_id": "18f3a2b4c5d6e7f8",
  "gmail_id": "18f3c9d1e2f3a4b5",
  "from": "Postman Recruiting <no-reply@greenhouse.io>",
  "from_name": "Postman Recruiting",
  "from_domain": "greenhouse.io",
  "to": "candidate@example.com",
  "date": "2026-09-08T04:12:00Z",
  "subject": "Re: Thank you for applying to Postman",
  "snippet": "first 300 characters of the body",
  "body_text": "plain-text body, at most body_max_chars (4000) characters, links kept inline",
  "links": ["https://..."],
  "query_tag": "rejection"
}
```

Rules: `gmail_id` is the message id (never the thread id); `thread_id` is the thread. `body_text`
is plain text (strip HTML), truncated at 4000 characters. `date` is ISO-8601 UTC. `from_domain`
is the domain of the sender address. Do not summarise or paraphrase the body.

## 3. Ingest

```
uv run jobhunt mail ingest
```

Each line `job N: from -> to (kind)` is a status the classifier moved. Note `failed` lines.
Only when every search page and message was fetched and written successfully, and ingestion has
no failures, run `uv run jobhunt mail sync-complete --through "<now captured in step 1>"`.
Do not checkpoint on `--skip-gmail`, a partial fetch, a write failure, or a failed ingestion.
Human-review items may remain queued: their message content has already been retained.

## 4. Resolve what needs a human

```
uv run jobhunt mail review --json
```

For each item ask one `AskUserQuestion`: header "Mail", question
"<subject> from <from>: what is this?", options built as one per candidate
("<kind> for job <id>: <company> / <title>") plus "Ignore". If the classifier's `kind` looks wrong
from the subject or snippet, offer the corrected kind in the option label. Then:

```
uv run jobhunt mail resolve <gmail_id> --job <id> --kind <kind>
uv run jobhunt mail resolve <gmail_id> --ignore
```

`gmail_id` is the message id shown by `mail review` (two messages of one thread can both be
queued; the thread id only works when it is unambiguous).

Valid kinds: confirmation, rejection, interview, assessment, offer, recruiter_outreach.

## 5. Ghost check

```
uv run jobhunt mail ghost-check
```

With `--skip-gmail`, offer follow-up text in chat only: do not call Gmail or record that a Gmail
draft was created. Otherwise, for every job it moved to ghosted, `uv run jobhunt show <id>` and ask with `AskUserQuestion`
whether to draft a follow-up. Only when the answer is yes: `Read` `config/templates/followup_ghost.md`,
fill `{company}`, `{role}`, `{applied_date}`, `{candidate_name}` and `{resume_link}` (the last two from
`config/profile.yaml`: `candidate.name`, `candidate.links.portfolio`; and `{first_name}` if a recruiter name
is known, otherwise drop the greeting name), and call `mcp__claude_ai_Gmail__create_draft` addressed to the
recruiter or ATS reply-to address from the confirmation thread. Show the draft text first. Never
send it; a draft is the end of this step. Record it:

```
uv run jobhunt note <id> "follow-up draft created in Gmail"
```

## 6. Report (10 lines or fewer)

```
uv run jobhunt stats
```

Messages fetched per kind; statuses moved (job, from -> to); items resolved or ignored; ghosted
jobs and drafts created; anything that failed. Suggest `/jobs-status` for the full funnel.
