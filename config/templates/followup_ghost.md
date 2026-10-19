---
max_words: 120
placeholders: [first_name, company, role, applied_date, candidate_name, resume_link]
notes: Drop the name from the greeting when first_name is unknown. candidate_name and resume_link come from config/profile.yaml (candidate.name, candidate.links.portfolio). British spelling, no dashes.
---
Subject: Following up on my application for {role}

Hi {first_name},

I applied for the {role} position at {company} on {applied_date} and wanted to check in on where things stand. I remain very interested in the role: the work matches what I do today, building and operating backend services in .NET and Python for a logistics platform.

If it helps, I am happy to share anything further about my experience or to make time for a short call at your convenience.

Thank you for your time, and I look forward to hearing from you.

Kind regards,
{candidate_name}
{resume_link}
