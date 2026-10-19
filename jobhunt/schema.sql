-- JobHunt schema v4. Applied by jobhunt.db.init_db when PRAGMA user_version = 0.

CREATE TABLE IF NOT EXISTS jobs (
  id                INTEGER PRIMARY KEY,
  company           TEXT NOT NULL,
  company_norm      TEXT NOT NULL,
  title             TEXT NOT NULL,
  title_norm        TEXT NOT NULL,
  location          TEXT,
  location_bucket   TEXT NOT NULL,
  is_remote         INTEGER NOT NULL DEFAULT 0,
  url               TEXT NOT NULL,
  url_canonical     TEXT NOT NULL,
  apply_url         TEXT,
  ats_type          TEXT,
  primary_source    TEXT NOT NULL,
  description_md    TEXT,
  salary_text       TEXT,
  employment_type   TEXT,
  posted_at         TEXT,
  discovered_at     TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  fingerprint       TEXT NOT NULL,
  status            TEXT NOT NULL DEFAULT 'discovered',
  prefilter_pass    INTEGER,
  prefilter_reasons TEXT,
  skill_overlap     REAL,
  score             INTEGER,
  score_reasons     TEXT,
  scored_at         TEXT,
  needs_hydration   INTEGER NOT NULL DEFAULT 0,
  detail_ref        TEXT,
  snooze_until      TEXT,
  prefill_state     TEXT NOT NULL DEFAULT 'none',
  submission_state  TEXT NOT NULL DEFAULT 'none'
                    CHECK (submission_state IN ('none','pending','unknown','confirmed')),
  submission_started_at TEXT,
  -- Why a dismissed job was dismissed. 'unlisted' reverses when a board relists the job;
  -- 'skipped' is the user's decision and is permanent.
  dismissed_reason  TEXT CHECK (dismissed_reason IN ('unlisted','skipped')),
  CHECK (status IN ('discovered','filtered','scored','shortlisted','approved','dismissed',
                    'applied','screening','interview','offer','rejected','withdrawn','ghosted')),
  CHECK (prefill_state IN ('none','schema','ready','blocked'))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_url_canonical ON jobs(url_canonical);
CREATE INDEX IF NOT EXISTS ix_jobs_fingerprint ON jobs(fingerprint);
CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS ix_jobs_company ON jobs(company_norm);
CREATE INDEX IF NOT EXISTS ix_jobs_discovered ON jobs(discovered_at);
CREATE INDEX IF NOT EXISTS ix_jobs_score ON jobs(score);

CREATE TABLE IF NOT EXISTS job_sources (
  id             INTEGER PRIMARY KEY,
  job_id         INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  source         TEXT NOT NULL,
  source_job_id  TEXT NOT NULL,
  url            TEXT NOT NULL,
  first_seen_at  TEXT NOT NULL,
  last_seen_at   TEXT NOT NULL,
  raw_json       TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_job_sources_src ON job_sources(source, source_job_id);
CREATE INDEX IF NOT EXISTS ix_job_sources_job ON job_sources(job_id);

CREATE TABLE IF NOT EXISTS applications (
  id                    INTEGER PRIMARY KEY,
  job_id                INTEGER NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
  status                TEXT NOT NULL,
  applied_at            TEXT,
  submitted_at          TEXT,
  resume_variant        TEXT,
  cover_letter_path     TEXT,
  submitted_via         TEXT,
  confirmation_ref      TEXT,
  confirmation_email_id TEXT,
  last_activity_at      TEXT NOT NULL,
  next_follow_up_at     TEXT,
  notes                 TEXT,
  CHECK (status IN ('applied','screening','interview','offer','rejected','withdrawn','ghosted'))
);
CREATE INDEX IF NOT EXISTS ix_applications_status ON applications(status);
CREATE INDEX IF NOT EXISTS ix_applications_follow_up ON applications(next_follow_up_at);

CREATE TABLE IF NOT EXISTS events (
  id              INTEGER PRIMARY KEY,
  job_id          INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  application_id  INTEGER REFERENCES applications(id) ON DELETE CASCADE,
  event_type      TEXT NOT NULL,
  at              TEXT NOT NULL,
  note            TEXT,
  from_status     TEXT,
  to_status       TEXT,
  source          TEXT NOT NULL DEFAULT 'cli',
  source_email_id TEXT,
  external_id     TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS ix_events_job ON events(job_id, at);
CREATE INDEX IF NOT EXISTS ix_events_app ON events(application_id, at);

CREATE TABLE IF NOT EXISTS contacts (
  id                 INTEGER PRIMARY KEY,
  name               TEXT NOT NULL,
  company            TEXT,
  company_norm       TEXT,
  title              TEXT,
  linkedin_url       TEXT UNIQUE,
  email              TEXT,
  relationship       TEXT,
  connected_on       TEXT,
  source             TEXT,
  last_contacted_at  TEXT,
  notes              TEXT
);
CREATE INDEX IF NOT EXISTS ix_contacts_company ON contacts(company_norm);

CREATE TABLE IF NOT EXISTS outreach (
  id           INTEGER PRIMARY KEY,
  contact_id   INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
  job_id       INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
  channel      TEXT NOT NULL,
  kind         TEXT NOT NULL,
  draft_path   TEXT,
  hook         TEXT,
  opened_at    TEXT,
  sent_at      TEXT,
  replied_at   TEXT,
  followup_due TEXT,
  response     TEXT,
  referral_outcome TEXT NOT NULL DEFAULT 'pending'
                   CHECK (referral_outcome IN ('pending','declined','completed')),
  CHECK (kind IN ('referral','connection_note','followup','inbound'))
);
CREATE INDEX IF NOT EXISTS ix_outreach_contact ON outreach(contact_id);
CREATE INDEX IF NOT EXISTS ix_outreach_job ON outreach(job_id);

CREATE TABLE IF NOT EXISTS email_ingest (
  gmail_id     TEXT PRIMARY KEY,
  source       TEXT NOT NULL,
  ingested_at  TEXT NOT NULL,
  jobs_found   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS scan_runs (
  id           INTEGER PRIMARY KEY,
  source       TEXT NOT NULL,
  company      TEXT,
  started_at   TEXT NOT NULL,
  finished_at  TEXT,
  fetched      INTEGER,
  new_jobs     INTEGER,
  merged       INTEGER,
  error        TEXT
);

CREATE TABLE IF NOT EXISTS runtime_state (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE VIEW IF NOT EXISTS v_pipeline AS
SELECT j.*,
       COALESCE(a.status, j.status) AS pipeline_status,
       a.id AS application_id,
       a.applied_at,
       a.next_follow_up_at,
       a.last_activity_at
FROM jobs j
LEFT JOIN applications a ON a.job_id = j.id;
