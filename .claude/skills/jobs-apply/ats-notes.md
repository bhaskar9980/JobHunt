# ATS notes for the assisted path

Use these when driving a form through the Playwright MCP. Values always come from
`uv run jobhunt answers sheet <job_id>`; never invent an answer.

## Workday (`*.myworkdayjobs.com`)

- 5 to 7 pages: Start (Apply / Apply Manually / Autofill with Resume), account sign-in or creation,
  My Information, My Experience, Application Questions, Voluntary Disclosures, Self Identify, Review.
- The user signs in or creates the account himself in the headed window. Never type a password.
- Stable `data-automation-id` hooks: `adventureButton` (Apply), `applyManually`, `legalNameSection_firstName`,
  `legalNameSection_lastName`, `addressSection_addressLine1`, `addressSection_city`,
  `addressSection_countryRegion`, `addressSection_postalCode`, `phone-device-type`, `phone-number`,
  `file-upload-input-ref` (resume), `linkedinQuestion`, `bottom-navigation-next-button`,
  `gender`, `veteranStatus`, `agreementCheckbox`. Page markers: `contactInformationPage`,
  `myExperiencePage`, `voluntaryDisclosuresPage`, `selfIdentificationPage`.
- Prefer "Apply Manually" over "Autofill with Resume": autofill invents dates and titles.
- Voluntary disclosures / self-identification: pick the "decline" or "do not wish to answer" option.
- Sessions time out after inactivity; if the page bounces to the start, tell the user and restart.
- Cap: `apply.workday_daily_cap` in `config/settings.yaml` (default 3).

## SmartRecruiters (`jobs.smartrecruiters.com`)

**Checked live on 10 Sep 2026: a deterministic filler is not possible today.** Clicking
"I'm interested" now redirects to a `oneclick-ui` single-page app that sits behind bot detection.
The challenge page never resolves under automation, headless or headed, with the persistent
profile, over a full minute of waiting. The document stays at about 2 KB with zero form fields.
So SmartRecruiters is assisted-only: open it in the user's own browser with the answer sheet.

The notes below describe the older form and are kept only in case the tenant serves it again:

- Single page. Resume upload first (it pre-fills name/email; verify them against the sheet).
- Screening questions appear below the profile block; select-style answers are native `<select>`s.
- Submit button text "Submit application". Confirmation text "Thank you for applying".

## Eightfold (`apply.careers.microsoft.com`)

Checked live on 10 Sep 2026. The apply URL redirects straight to `/careers/apply?pid=...`, which
renders only a sign-in wall: "Sign in using Microsoft / LinkedIn / Google / Facebook" and
"Create an account". No application form exists before authentication.

The runner already waits up to `apply.login_wait_seconds` for a human to sign in, and the session
persists in the Playwright profile, so a filler is possible in principle. Writing its config needs
one signed-in look at the real form, which has not happened yet. Assisted until then.

## SuccessFactors (`careers.wipro.com` and other tenants)

Checked live on 10 Sep 2026. "Apply now" opens a "Start" panel whose only field is
"Enter email to start application process", so the flow begins with an email gate and then
account creation. Same position as Eightfold: possible after a signed-in look, assisted until then.

## Naukri (`naukri.com`)

- Login required (the user logs in). The Apply button is on the job page; a questionnaire modal may
  follow. Answers from the sheet; expected CTC in lakhs, notice period in days.
- One at a time. Naukri's terms restrict scripted use, so nothing here runs unattended.

## Instahyre / Cutshort / Wellfound

- Login required. Applying is a single click plus an optional note. The note may reuse the first
  paragraph of `cover_letter.md` when one exists.
- Their terms forbid scraping; this path only touches the pages the user would open himself.

## LinkedIn

- Never automated, never opened in the MCP browser (it is blocked by `--blocked-origins`).
- Hand the job to the user with the answer sheet; record with `mark-applied --via manual`.

## Greenhouse / Lever / Ashby (python fillers)

These three normally run through `uv run jobhunt apply <job_id>` with no browser driving by you.
Selectors live in `config/ats/{greenhouse,lever,ashby}.yaml`; the filler code only knows the keys.
When the runner exits 14 (validation error) or 13 (unmatched questions), open
`data/applications/<job_id>/run.log` and `answers.json`, then take over in the MCP browser:

- **Exit 13**: `answers.json` lists `unmatched` fields. Answer them once with
  `uv run jobhunt answers learn --job <id> --question "<label>" --answer "<value>"`, then
  `uv run jobhunt prefill <id>` and retry `apply`. Do not type free-text answers into the form
  yourself; the bank must learn them so the next application gets the same answer.
- **Exit 14, Greenhouse**: usually a react-select combobox that did not take the typed value
  (Country, work authorisation, sponsorship) or a "Cover Letter" file field with no PDF. Open the
  apply URL, check each `select` field listed in `report.errors`, pick the option by hand, and use
  "Enter manually" for the cover letter, pasting `cover_letter.md`. Resume goes through the
  hidden `#resume` file input via `browser_file_upload`.
- **Exit 14, Lever**: the location autocomplete (`#location-input`) needs a suggestion click; type
  the city slowly and pick the first hit. Custom `cards[...]` radios and checkboxes are plain inputs.
  Never click `#btn-submit` yourself before the user confirms in AskUserQuestion; hCaptcha may
  appear after the click and must be solved by him.
- **Exit 14, Ashby**: Yes/No questions are two buttons (`button[data-option=yes|no]`); a required
  question is marked only by a `_required_*` class on its label, so re-check required fields
  visually. The resume input is `#_systemfield_resume`. Submit text is "Submit Application".
- After a manual submission on any of these, record it with
  `uv run jobhunt mark-applied <job_id> --via assisted`.
- If `already applied` text appears (exit 12) the ATS remembers the email address; do not create a
  second account or re-apply under another address.

## Indeed, foundit, Hirist, ZipRecruiter, Welcome to the Jungle (assisted)

- All five sit behind a login and their terms restrict scripted use, so they run in the headed
  MCP browser, one at a time, with the user signing in himself and clicking the final Apply.
- Indeed: "Apply now" opens Indeed Apply (a multi-step modal) or redirects to the employer's
  own ATS. If it redirects to Greenhouse, Lever or Ashby, stop and run the Python filler on that
  URL instead (`uv run jobhunt prefill <id>` after `set-status`/`note` with the new apply URL).
- foundit and Hirist: one-click apply with the stored profile; check the questionnaire modal
  against the answer sheet (expected CTC in lakhs, notice period in days).
- ZipRecruiter: "1-Click Apply" uses the ZipRecruiter profile; otherwise it forwards to the
  employer ATS, same rule as Indeed.
- Welcome to the Jungle: nearly always forwards to the company's ATS. Detect the final URL and
  route accordingly.

## Tal "jobs in the wild" (manual)

- Each posting is a LinkedIn or X post by the hiring manager. There is no form: open the original
  post in the user's own browser (never the MCP browser, linkedin.com is blocked there), and use
  `/jobs-refer` to draft the message to the poster. Record with `mark-applied --via manual`.

## Late confirmations (exit 16)

- Lever sometimes lands on `/thanks` only after a second or two of spinner; Greenhouse's new
  boards show "Thank you for applying" inline without changing the URL. Both are covered by
  `confirm_timeout_seconds` (45s). If exit 16 still happens, the confirmation email (usually within
  minutes) is the ground truth: `/jobs-sync` will move the job to applied on its own, or run
  `mark-applied` by hand. Do not resubmit.
