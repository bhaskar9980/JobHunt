# Scoring rubric for /jobs-scan

Score each job 0-100 for fit with the candidate in `config/profile.yaml`. Read the whole
description before scoring. Return an integer score, 2-4 short reasons, any red flags, and a
`resume_variant_hint` of `dotnet-backend`, `python-platform`, or `general`.

## Weights (sum to 100)

| Dimension | Points | What earns full marks |
|---|---|---|
| Stack fit | 35 | Core stack is C#/.NET (ASP.NET Core) backend with MongoDB, Kafka, microservices, Docker, Kubernetes, AWS. Half credit for a language-agnostic role that lists C#, C++ or "any modern language" among its options, or for a Python backend (he has working Python, not depth). Near zero when the primary language is one he has not worked in (Go, Rust, Ruby, PHP, Node, Scala, Java-only), or for frontend-only, mobile, data science or embedded work. |
| Seniority match | 20 | Mid-level / SE II / SDE 2 roles aligned with the candidate's verified experience and target level in the profile. Prefer roles asking for 3-5 years; consider overlapping ranges. Deduct heavily for 6+ years, staff/principal, or fresh-graduate roles. |
| Location and remote | 15 | An existing India target location, remote work explicitly available from India/worldwide, or an overseas role explicitly offering both visa sponsorship and relocation assistance. Country-restricted remote work is not worldwide remote. Unknown or contradictory eligibility belongs in the verification queue, not the shortlist. Employer support is not proof of the candidate's current work authorisation; never alter private answers. |
| Company and role quality | 15 | Product company or strong engineering culture, clear ownership, real scale. Deduct for body-shop/contract postings, vague descriptions, unrealistic skill lists. |
| Compensation signal | 10 | Compare verified fixed annual pay with `profile.comp.expected_fixed_lpa`. Bonus, variable pay and equity are additional and cannot satisfy a fixed-pay target. Neutral 5 when fixed pay is unstated; keep foreign-currency amounts in their original currency and stay neutral without a verified conversion and applicable target. Do not invent a split from total CTC or infer pay from company reputation. A target is not a hard floor unless `floor_inr_lpa` is set. |
| Process friction | 5 | Direct ATS apply, no mandatory cover letter essays, no assessments before a human conversation. |

## Red flags (list them; each also costs points in the relevant dimension)

- "Immediate joiners only" with a notice period under 30 days.
- Requires US citizenship, security clearance, or onsite-only in a country with no sponsorship note.
- Salary far below floor, commission-only, unpaid, or "competitive" plus a very long skill list.
- Posting older than 30 days with no repost signal.
- Title says engineer but duties are support, QA, or sales engineering.

## Eligibility verdict (jobs marked `eligibility_review`)

Some jobs arrive with `"eligibility_review": true` and an `eligibility_question`. The rules
could not tell whether the user may take the role, so it is parked rather than dropped. Score it
like any other job **and** add an `eligibility` field with one of:

- `eligible` - the posting states remote work open to India or worldwide, or states **both** visa
  sponsorship and relocation assistance.
- `not_eligible` - the posting carries an **explicit** restriction that shuts the user out. It must
  say so in its own words. The recognised forms are: remote restricted to named countries or
  states, "must reside in" or "must be located in", sponsorship stated as unavailable, a security
  clearance or citizenship requirement, an export-control or local-military-service requirement,
  or a graduation-year window he falls outside.
- `unclear` - everything else, including the common case of a posting that names a foreign city
  and simply says nothing about visas or sponsorship.

**A foreign location is not by itself a restriction.** Onsite, hybrid or unstated work mode in
another country is `unclear`, not `not_eligible`, unless the posting also states one of the
explicit restrictions above. Sponsorship could still be offered; the posting just did not say.
When two readings are available, `unclear` is the honest default: it records that the posting was
silent rather than asserting a refusal the employer never made.

Judge only from the posting text. Never infer eligibility from the employer's size or reputation,
and never change the private work-authorisation answers. An `eligible` verdict returns the job to
the pipeline; the other two record the decision and leave it filtered.

## Output shape (one object per job)

```json
{"job_id": 12, "score": 82,
 "reasons": [".NET + MongoDB backend at a product company", "SE II band, 3-5 years asked", "Bengaluru hybrid"],
 "red_flags": ["Mentions on-call without compensation detail"],
 "resume_variant_hint": "dotnet-backend"}
```

For a job marked `eligibility_review`, add the verdict:

```json
{"job_id": 44, "score": 71, "reasons": ["..."], "red_flags": [],
 "resume_variant_hint": "dotnet-backend", "eligibility": "not_eligible"}
```

Do not invent facts. If the description is missing, score on title, company and location only and say so in reasons.
