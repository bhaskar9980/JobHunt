---
name: jobs-review
description: Review shortlisted jobs in small batches, approve/skip/snooze each one, prefill the approved forms, answer any screening question the answer bank could not, and draft cover letters where a form asks for one. Always run before /jobs-apply.
disable-model-invocation: true
allowed-tools: Bash(uv run jobhunt *) Read Write AskUserQuestion
argument-hint: "[batch-size]"
---

# /jobs-review

Run from the project root. Batch size: `$ARGUMENTS` or 8. Never submit anything
here; this skill only records decisions and prepares forms.

## 1. Load the queue

```
uv run jobhunt queue --limit <batch-size>
```

If it is empty, say so and stop. Otherwise print a compact table: id, score, company, title,
location, salary, ATS, one-line "why" (first two reasons), red flags.

## 2. Decide, four at a time

Use `AskUserQuestion` with `multiSelect: true`. Options are the jobs in this group of four, labelled
`"{company}: {title} ({score})"` with the why/red-flags as the description. Selected = approve.
Then ask one follow-up for the unselected ones: skip (dismiss) or snooze a week. If the batch is
larger than 12, instead write the checklist and let the user tick it:

```
uv run jobhunt queue --limit <n> --format md --out data/review_queue.md
```

then `uv run jobhunt decide --from-file data/review_queue.md` once he says it is ticked.

Record each decision immediately:

```
uv run jobhunt decide <job_id> approve
uv run jobhunt decide <job_id> skip
uv run jobhunt decide <job_id> snooze --until 2026-09-20
```

## 3. Prefill every approved job

```
uv run jobhunt prefill <job_id>
```

Exit codes: 0 ready; 13 some required questions have no answer in the bank (see step 4);
11 login wall (tell the user which site to log in to in the browser window that opened, then rerun);
12 the form says he already applied (`uv run jobhunt decide <id> skip`); 20 assisted or manual ATS
(Workday, Naukri, Instahyre, LinkedIn): the answer sheet is prepared, note it for `/jobs-apply`.

## 4. Fill the gaps in the answer bank

`Read data/applications/<job_id>/answers.json`. For every entry in `unmatched`:

- If it is factual (years with X, CTC, notice period, relocation, links): ask the user with
  `AskUserQuestion` (free text via Other) and persist:
  `uv run jobhunt answers learn --job <id> --question "<label>" --answer "<answer>"`
- If it is a short essay ("why us", "proudest project"): draft it yourself from
  `data/resume_facts.md` (run `uv run jobhunt facts` if missing) and the job description
  (`uv run jobhunt show <id> --json`), show the draft, get a yes, then `answers learn` it.
  Rules: plain language, verified numbers only, no em-dashes, British spelling, no claims that
  FleetHours has shipped or has customers.

`answers learn` re-resolves the job; repeat until `prefill_state` is `ready`
(`uv run jobhunt show <id>` prints it).

## 5. Cover letters (only when the form has a cover-letter or "why us" field)

Write `data/applications/<job_id>/cover_letter.md` (120-250 words, same rules as above), then

```
uv run jobhunt tailor check <job_id>
uv run jobhunt tailor pdf <job_id>      # one-page PDF for ATSs that take the letter as a file
uv run jobhunt prefill <job_id>
```

and fix anything the check reports before moving on.

## 6. Report

List the jobs now `approved` + `ready` (these are what `/jobs-apply` will submit), the ones that
need the user's input, and the assisted/manual ones. Suggest `/jobs-apply`.
