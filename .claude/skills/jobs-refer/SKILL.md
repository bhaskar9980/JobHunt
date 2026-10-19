---
name: jobs-refer
description: For approved or shortlisted jobs, rank the people the user already knows at that company, draft a referral message from a template, open the profile in his own browser and record when he has sent it. Nothing is sent automatically. Second-degree fallback builds a connection note and a people-search URL he opens himself.
disable-model-invocation: true
allowed-tools: Bash(uv run jobhunt *) Read Write Glob AskUserQuestion mcp__firecrawl__firecrawl_scrape mcp__firecrawl__firecrawl_search
argument-hint: "[job_id ...]"
---

# /jobs-refer

Run from the project root. `$ARGUMENTS` may list job ids; otherwise work
through every approved and shortlisted job.

## The LinkedIn position (read this first, every run)

LinkedIn is never scraped. No automated connection requests or messages are ever sent
(LinkedIn User Agreement section 8.2, items 2 and 13). Contacts come only from the official
Connections.csv data export, drafts are written to local files, and the user clicks send himself.
Never open linkedin.com with Firecrawl or any fetch tool. The only LinkedIn URLs this skill
produces are profile links and a people-search link that `outreach open` / the user opens in his
own browser.

## 0. Contacts present?

```
uv run jobhunt refer contacts --json
```

If empty, stop and ask the user to export Connections.csv (LinkedIn: Settings > Data privacy >
Get a copy of your data > Connections) and run
`uv run jobhunt refer import-connections <path>`. Then continue.

## 1. Jobs to work on

```
uv run jobhunt list --status approved --status shortlisted --json
```

Filter to `$ARGUMENTS` when given. Skip jobs with a future `snooze_until`; existing referral
waits are progress, not a reason to send another request. For each remaining job, in score order:

## 2. Who to ask

```
uv run jobhunt refer suggest <job_id> --json
```

- **Suggestions found**: show a table (contact id, name, title, score, reasons). Use
  `AskUserQuestion` with `multiSelect: true` to pick up to two contacts (options are the
  contacts; the question text names the job). Skip the job if the user picks none.
- **No suggestions**: go to step 5 (second degree).

## 3. Hook and draft

For each chosen contact propose one `hook`: a single concrete sentence about them or the
company drawn from `uv run jobhunt show <job_id>` (the description), their title, or the company
itself. No flattery, no claims you cannot see in the data. Show the hook and let the user accept
or rewrite it (`AskUserQuestion`, options: "Use it", "Rewrite" with his text via Other).

Pick the template: `referral_excolleague` when the contact's company history overlaps Platform
Science / Trimble or the user says they worked together, otherwise `referral_friend`.

```
uv run jobhunt refer draft <job_id> <contact_id> --template <name> --hook "<hook>"
```

The command prints length and tone issues (over the character cap, unfilled placeholder,
em dash, American spelling) and exits 1 without writing when it finds any. Fix the hook and
rerun; use `--force` only if the user explicitly says so. On success it prints the outreach id,
the file path and the full text. Show the text.

## 4. Open, send, record

```
uv run jobhunt outreach open <outreach_id>
```

That opens the profile in the user's default browser and prints the draft path. He pastes and
sends. Then `AskUserQuestion`: "Sent to <name>?" with options "Sent", "Skipped for now",
"Edited and sent". For Sent / Edited and sent:

```
uv run jobhunt outreach mark-sent <outreach_id>
```

This schedules the follow-up (7 days, then 14, then stop; see `config/referrals.yaml`).

## 5. Second degree (no contacts at the company)

1. Firecrawl only the job page and the company's own site (team, about, careers, engineering
   blog). Never linkedin.com, never a people-data site. Look for a recruiter or hiring-manager
   name and title that is stated on those pages.
2. Print the people-search link for the user to open himself:
   `uv run jobhunt refer people-search <job_id> --role recruiter --role "engineering manager"`
3. If a name was found and the user wants to reach out, he sends the connection request by hand.
   Render the note for him: `Read` `config/templates/outreach/connection_note_recruiter.md`,
   fill `{first_name}`, `{role}`, `{company}` and `{resume_link}` (from `config/profile.yaml`, `candidate.links.portfolio`),
   keep it under 300 characters, and show it. Do not create a contact row for someone the user is
   not yet connected to; once they accept and appear in his next Connections.csv export, rerun
   this skill and use `refer draft` as normal.

## 6. Follow-ups and wrap-up

```
uv run jobhunt outreach due
```

For each due row, offer a `followup_1` draft (`refer draft ... --template followup_1`), then the
same open / sent / mark-sent loop. When a reply has arrived, record it:
`uv run jobhunt outreach mark-replied <outreach_id> --response "<one line>"`.
This records the reply but leaves the referral outcome pending. Add `--referred` only when
the user confirms the referral is completed, or `--declined` for an explicit refusal; never use
both. Do not infer completion from reply text or a promise to refer later. The same flags on a
follow-up update its associated referral request. Completion unparks the job; refusal unparks
it only when no other request remains pending. An explicit outcome on a standalone message
is also recorded; only that explicit confirmation, never its text, establishes the outcome.

Report in under ten lines: jobs covered, drafts written, messages the user sent, follow-ups due,
jobs with nobody to ask.
