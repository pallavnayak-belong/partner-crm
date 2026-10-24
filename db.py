"""SQLite schema + helpers for the Partner CRM."""
import os
import sys
import sqlite3
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("PARTNER_CRM_DB", os.path.join(HERE, "data", "partner_crm.db"))

STAGES = ["New", "Contacted", "Replied", "Applied", "In discussion", "Onboarded", "Not interested"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS universe (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,              -- 'MFD' | 'SEBI'
  key TEXT NOT NULL UNIQUE,        -- normalised ARN-123 / INA000017523
  name TEXT NOT NULL,
  category TEXT,                   -- SEBI category, or 'Indian Mutual Fund Distributors'
  classification TEXT,             -- Corporate / Individual / Bank
  email TEXT, phone TEXT, contact_person TEXT,
  city TEXT, state TEXT, address TEXT,
  website TEXT, linkedin TEXT, other_social TEXT,
  rank INTEGER, top_500 INTEGER, avg_aum_cr REAL, gross_commission_cr REAL,
  sif_authorized INTEGER, validity TEXT
);
CREATE INDEX IF NOT EXISTS ix_universe_kind ON universe(kind);

CREATE TABLE IF NOT EXISTS partners (
  id INTEGER PRIMARY KEY,
  universe_id INTEGER UNIQUE REFERENCES universe(id),
  name TEXT NOT NULL,
  category TEXT, segment TEXT, country TEXT, city TEXT,
  website TEXT, description TEXT, source TEXT,
  contact_person TEXT, designation TEXT, email TEXT, phone TEXT,
  linkedin TEXT, other_social TEXT,
  priority TEXT, owner TEXT,
  stage TEXT NOT NULL DEFAULT 'New',
  last_contacted TEXT, follow_up TEXT,
  remarks TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_partners_stage ON partners(stage);

CREATE TABLE IF NOT EXISTS activities (
  id INTEGER PRIMARY KEY,
  partner_id INTEGER NOT NULL REFERENCES partners(id),
  ts TEXT NOT NULL,
  type TEXT NOT NULL,              -- added | note | stage | draft | email | reply
  body TEXT
);
CREATE INDEX IF NOT EXISTS ix_activities_partner ON activities(partner_id, ts);

CREATE TABLE IF NOT EXISTS templates (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  subject TEXT NOT NULL,
  plain TEXT NOT NULL,
  html TEXT NOT NULL,
  segment TEXT NOT NULL DEFAULT '',   -- '' = default for everyone; else Corporate/Individual/Bank
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS emails (
  id INTEGER PRIMARY KEY,
  partner_id INTEGER NOT NULL REFERENCES partners(id),
  template_id INTEGER,
  to_addr TEXT, subject TEXT,
  message_id TEXT,
  status TEXT NOT NULL,              -- sent | failed
  error TEXT,
  sent_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_emails_partner ON emails(partner_id);
CREATE INDEX IF NOT EXISTS ix_emails_msgid ON emails(message_id);

CREATE TABLE IF NOT EXISTS sequence_steps (
  step_no INTEGER PRIMARY KEY,        -- 1 = first follow-up after the initial email
  delay_days INTEGER NOT NULL,        -- days after the previous send
  template_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
  id INTEGER PRIMARY KEY,
  external_id TEXT NOT NULL UNIQUE,     -- Youform submission_id, or a hash for CSV rows
  received_at TEXT,
  name TEXT, email TEXT, phone TEXT, company TEXT,
  applying_as TEXT, clients TEXT, products TEXT, profile TEXT,
  status TEXT NOT NULL,                 -- linked | review | new_partner | ignored
  partner_id INTEGER REFERENCES partners(id),
  match_method TEXT, match_confidence TEXT,
  candidates TEXT,                      -- JSON [[partner_id, confidence, method], ...]
  raw TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_applications_status ON applications(status);

CREATE TABLE IF NOT EXISTS referred_customers (
  customer_id TEXT PRIMARY KEY,           -- pseudonymous CUS_ id, no personal data
  partner_id INTEGER NOT NULL REFERENCES partners(id),
  media_source TEXT, campaign TEXT, installed_at TEXT, synced_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_referred_partner ON referred_customers(partner_id);

CREATE TABLE IF NOT EXISTS customer_milestones (
  customer_id TEXT NOT NULL, milestone TEXT NOT NULL, ts TEXT,
  PRIMARY KEY (customer_id, milestone)
);

CREATE TABLE IF NOT EXISTS onboarding (
  partner_id INTEGER PRIMARY KEY REFERENCES partners(id),
  status TEXT NOT NULL DEFAULT 'Collecting',   -- Collecting | In review | On hold | Rejected | Onboarded
  entity_type TEXT, reviewer TEXT, agreement_date TEXT, terms TEXT,
  appsflyer_link TEXT, notes TEXT,
  started_at TEXT NOT NULL, onboarded_at TEXT, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS onboarding_items (
  id INTEGER PRIMARY KEY,
  partner_id INTEGER NOT NULL REFERENCES partners(id),
  key TEXT NOT NULL, label TEXT NOT NULL,
  required INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'pending',      -- pending | received | verified | rejected | na
  note TEXT, verified_at TEXT, sort INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
  UNIQUE(partner_id, key)
);

CREATE TABLE IF NOT EXISTS onboarding_files (
  id INTEGER PRIMARY KEY,
  partner_id INTEGER NOT NULL REFERENCES partners(id),
  item_key TEXT NOT NULL, orig_name TEXT NOT NULL, stored_name TEXT NOT NULL,
  size INTEGER NOT NULL, uploaded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_onb_files ON onboarding_files(partner_id, item_key);

CREATE TABLE IF NOT EXISTS template_attachments (
  id INTEGER PRIMARY KEY,
  template_id INTEGER NOT NULL REFERENCES templates(id),
  orig_name TEXT NOT NULL, stored_name TEXT NOT NULL, size INTEGER NOT NULL, uploaded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tpl_att ON template_attachments(template_id);

-- partner_id 0 holds the standard rate card that new onboardings start from (so no FK here)
CREATE TABLE IF NOT EXISTS commercials (
  partner_id INTEGER PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'Draft',        -- Draft | Proposed | Agreed
  currency TEXT NOT NULL DEFAULT 'INR',
  effective_from TEXT, valid_until TEXT,
  payout_frequency TEXT, payment_days INTEGER, min_payout REAL,
  tax_note TEXT, notes TEXT,
  based_on TEXT,                               -- 'standard' | 'partner:<id>' | NULL
  agreed_by TEXT, agreed_on TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS commercial_lines (
  id INTEGER PRIMARY KEY,
  partner_id INTEGER NOT NULL,
  product TEXT NOT NULL, basis TEXT NOT NULL, rate REAL NOT NULL, note TEXT, sort INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_comm_lines ON commercial_lines(partner_id);

CREATE TABLE IF NOT EXISTS cc_contacts (
  id INTEGER PRIMARY KEY,
  label TEXT NOT NULL,
  email TEXT NOT NULL UNIQUE COLLATE NOCASE,
  auto INTEGER NOT NULL DEFAULT 0     -- pre-selected in every send dialog
);

CREATE TABLE IF NOT EXISTS partner_contacts (       -- additional people at a partner; the primary contact stays on partners
  id INTEGER PRIMARY KEY,
  partner_id INTEGER NOT NULL REFERENCES partners(id),
  name TEXT, designation TEXT, email TEXT, phone TEXT, role TEXT,
  bounced INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_pcontacts ON partner_contacts(partner_id);

CREATE TABLE IF NOT EXISTS tags (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS partner_tags (
  partner_id INTEGER NOT NULL REFERENCES partners(id), tag_id INTEGER NOT NULL REFERENCES tags(id),
  PRIMARY KEY (partner_id, tag_id)
);
CREATE INDEX IF NOT EXISTS ix_partner_tags_tag ON partner_tags(tag_id);

CREATE TABLE IF NOT EXISTS saved_views (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, page TEXT NOT NULL, filters TEXT NOT NULL, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY,
  partner_id INTEGER NOT NULL REFERENCES partners(id),
  title TEXT NOT NULL, due TEXT NOT NULL, assignee TEXT,
  done_at TEXT, created_at TEXT NOT NULL, created_by TEXT
);
CREATE INDEX IF NOT EXISTS ix_tasks_partner ON tasks(partner_id, done_at);
CREATE INDEX IF NOT EXISTS ix_tasks_open ON tasks(done_at, due);

CREATE TABLE IF NOT EXISTS team_members (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS erasure_log (          -- what was deleted, when and why; deliberately holds no personal data
  id INTEGER PRIMARY KEY, partner_id INTEGER, erased_at TEXT NOT NULL, by TEXT, reason TEXT
);

CREATE INDEX IF NOT EXISTS ix_emails_sent ON emails(status, sent_at);
CREATE INDEX IF NOT EXISTS ix_activities_ts ON activities(ts);
CREATE INDEX IF NOT EXISTS ix_partners_email ON partners(email);

CREATE TABLE IF NOT EXISTS backup_runs (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL,   -- local | drive | sheets
  ok INTEGER NOT NULL, detail TEXT
);

CREATE TABLE IF NOT EXISTS replies_seen (
  message_id TEXT PRIMARY KEY,
  partner_id INTEGER, kind TEXT, ts TEXT
);

CREATE TABLE IF NOT EXISTS send_jobs (
  id TEXT PRIMARY KEY, started TEXT NOT NULL, finished TEXT, kind TEXT NOT NULL,      -- bulk | followup | single | test
  by TEXT, template TEXT, total INTEGER NOT NULL DEFAULT 0, sent INTEGER NOT NULL DEFAULT 0, skipped INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'running', stopped_reason TEXT, details TEXT
);
CREATE INDEX IF NOT EXISTS ix_sendjobs_started ON send_jobs(started);

-- Who changed what: every edit to the fields below is recorded with the old value, so it can be undone from the app.
-- crm_actor() is registered on every connection by connect(); it is the signed-in team member, or "system" for background jobs.
CREATE TABLE IF NOT EXISTS change_log (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, partner_id INTEGER NOT NULL,
  field TEXT NOT NULL, old TEXT, new TEXT, actor TEXT, undone INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_changelog_partner ON change_log(partner_id, ts);
CREATE INDEX IF NOT EXISTS ix_changelog_ts ON change_log(ts);
"""

TRIGGERS = """
CREATE UNIQUE INDEX IF NOT EXISTS ux_partner_distributor_code ON partners(distributor_code) WHERE distributor_code IS NOT NULL AND distributor_code<>'';
CREATE TRIGGER IF NOT EXISTS trg_partner_log AFTER UPDATE ON partners
BEGIN
  INSERT INTO change_log(ts,partner_id,field,old,new,actor) SELECT strftime('%Y-%m-%dT%H:%M:%S','now','localtime'),NEW.id,'stage',OLD.stage,NEW.stage,crm_actor() WHERE OLD.stage IS NOT NEW.stage;
  INSERT INTO change_log(ts,partner_id,field,old,new,actor) SELECT strftime('%Y-%m-%dT%H:%M:%S','now','localtime'),NEW.id,'do_not_contact',OLD.do_not_contact,NEW.do_not_contact,crm_actor() WHERE OLD.do_not_contact IS NOT NEW.do_not_contact;
  INSERT INTO change_log(ts,partner_id,field,old,new,actor) SELECT strftime('%Y-%m-%dT%H:%M:%S','now','localtime'),NEW.id,'dnc_reason',OLD.dnc_reason,NEW.dnc_reason,crm_actor() WHERE OLD.dnc_reason IS NOT NEW.dnc_reason;
  INSERT INTO change_log(ts,partner_id,field,old,new,actor) SELECT strftime('%Y-%m-%dT%H:%M:%S','now','localtime'),NEW.id,'lost_reason',OLD.lost_reason,NEW.lost_reason,crm_actor() WHERE OLD.lost_reason IS NOT NEW.lost_reason;
  INSERT INTO change_log(ts,partner_id,field,old,new,actor) SELECT strftime('%Y-%m-%dT%H:%M:%S','now','localtime'),NEW.id,'owner',OLD.owner,NEW.owner,crm_actor() WHERE OLD.owner IS NOT NEW.owner;
  INSERT INTO change_log(ts,partner_id,field,old,new,actor) SELECT strftime('%Y-%m-%dT%H:%M:%S','now','localtime'),NEW.id,'name',OLD.name,NEW.name,crm_actor() WHERE OLD.name IS NOT NEW.name;
  INSERT INTO change_log(ts,partner_id,field,old,new,actor) SELECT strftime('%Y-%m-%dT%H:%M:%S','now','localtime'),NEW.id,'email',OLD.email,NEW.email,crm_actor() WHERE OLD.email IS NOT NEW.email;
  INSERT INTO change_log(ts,partner_id,field,old,new,actor) SELECT strftime('%Y-%m-%dT%H:%M:%S','now','localtime'),NEW.id,'phone',OLD.phone,NEW.phone,crm_actor() WHERE OLD.phone IS NOT NEW.phone;
  INSERT INTO change_log(ts,partner_id,field,old,new,actor) SELECT strftime('%Y-%m-%dT%H:%M:%S','now','localtime'),NEW.id,'contact_person',OLD.contact_person,NEW.contact_person,crm_actor() WHERE OLD.contact_person IS NOT NEW.contact_person;
  INSERT INTO change_log(ts,partner_id,field,old,new,actor) SELECT strftime('%Y-%m-%dT%H:%M:%S','now','localtime'),NEW.id,'designation',OLD.designation,NEW.designation,crm_actor() WHERE OLD.designation IS NOT NEW.designation;
  INSERT INTO change_log(ts,partner_id,field,old,new,actor) SELECT strftime('%Y-%m-%dT%H:%M:%S','now','localtime'),NEW.id,'country',OLD.country,NEW.country,crm_actor() WHERE OLD.country IS NOT NEW.country;
  INSERT INTO change_log(ts,partner_id,field,old,new,actor) SELECT strftime('%Y-%m-%dT%H:%M:%S','now','localtime'),NEW.id,'city',OLD.city,NEW.city,crm_actor() WHERE OLD.city IS NOT NEW.city;
  INSERT INTO change_log(ts,partner_id,field,old,new,actor) SELECT strftime('%Y-%m-%dT%H:%M:%S','now','localtime'),NEW.id,'category',OLD.category,NEW.category,crm_actor() WHERE OLD.category IS NOT NEW.category;
  INSERT INTO change_log(ts,partner_id,field,old,new,actor) SELECT strftime('%Y-%m-%dT%H:%M:%S','now','localtime'),NEW.id,'segment',OLD.segment,NEW.segment,crm_actor() WHERE OLD.segment IS NOT NEW.segment;
  INSERT INTO change_log(ts,partner_id,field,old,new,actor) SELECT strftime('%Y-%m-%dT%H:%M:%S','now','localtime'),NEW.id,'archived',OLD.archived,NEW.archived,crm_actor() WHERE OLD.archived IS NOT NEW.archived;
  INSERT INTO change_log(ts,partner_id,field,old,new,actor) SELECT strftime('%Y-%m-%dT%H:%M:%S','now','localtime'),NEW.id,'archive_reason',OLD.archive_reason,NEW.archive_reason,crm_actor() WHERE OLD.archive_reason IS NOT NEW.archive_reason;
  INSERT INTO change_log(ts,partner_id,field,old,new,actor) SELECT strftime('%Y-%m-%dT%H:%M:%S','now','localtime'),NEW.id,'priority',OLD.priority,NEW.priority,crm_actor() WHERE OLD.priority IS NOT NEW.priority;
  INSERT INTO change_log(ts,partner_id,field,old,new,actor) SELECT strftime('%Y-%m-%dT%H:%M:%S','now','localtime'),NEW.id,'website',OLD.website,NEW.website,crm_actor() WHERE OLD.website IS NOT NEW.website;
END;
CREATE TRIGGER IF NOT EXISTS trg_activity_actor AFTER INSERT ON activities WHEN NEW.actor IS NULL
BEGIN
  UPDATE activities SET actor=crm_actor() WHERE id=NEW.id;
END;
"""


REAL_DB = os.path.join(HERE, "data", "partner_crm.db")


CTX = threading.local()


def current_actor():
    """Who is making this change: the team member of the current web request, or 'system' for background jobs and scripts."""
    return getattr(CTX, "name", "") or "system"


def connect():
    if "unittest" in sys.modules and os.path.realpath(DB_PATH).startswith(os.path.realpath(os.path.join(HERE, "data")) + os.sep) and not os.environ.get("PARTNER_CRM_ALLOW_REAL_DB_IN_TESTS"):
        raise RuntimeError("A test tried to open the REAL CRM database. Tests must use a temporary copy.")   # guard: tests once wrote fake emails/replies into live data
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.create_function("crm_actor", 0, current_actor)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


MIGRATIONS = [
    ("partners", "do_not_contact", "INTEGER NOT NULL DEFAULT 0"),
    ("partners", "dnc_reason", "TEXT"),
    ("emails", "step", "INTEGER NOT NULL DEFAULT 0"),
    ("partners", "partner_code", "TEXT"),
    ("partners", "distributor_code", "TEXT"),
    ("universe", "score", "REAL"),
    ("emails", "cc", "TEXT"),
    ("commercials", "terms_text", "TEXT"),
    ("partners", "archived", "INTEGER NOT NULL DEFAULT 0"),
    ("partners", "archive_reason", "TEXT"),
    ("partners", "lost_reason", "TEXT"),
    ("activities", "actor", "TEXT"),
    ("activities", "edited", "INTEGER NOT NULL DEFAULT 0"),
    ("templates", "region", "TEXT NOT NULL DEFAULT ''"),
    ("backup_runs", "size_kb", "INTEGER"),
]


def init():
    with connect() as c:
        c.executescript(SCHEMA)
        for table, col, decl in MIGRATIONS:
            if col not in [r[1] for r in c.execute(f"PRAGMA table_info({table})")]:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        c.executescript(TRIGGERS)       # after the migrations: the triggers read columns those add
