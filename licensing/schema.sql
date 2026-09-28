-- Plexora licence service, D1 schema v1.
--
-- Identity chain: account -> licence -> seat -> environment -> certificate.
-- FREE HAS NO ROWS ANYWHERE: a Free user never talks to this service, so
-- `licenses.plan` can only be 'paid' and a trial is a paid licence with
-- is_trial = 1.
--
-- Nothing scientific is stored: no project, path, dataset, marker or image.
-- Environments are known by the name their owner chose and a peppered hash of
-- a random secret; never a hostname, MAC or serial.
--
-- Caps that matter for security (environments per seat, seats per licence, one
-- trial per mailbox) are enforced by D1 itself -- conditional INSERTs and
-- UNIQUE indexes -- never by a read-then-write in the Worker.
--
-- Apply with `npm run db:init` (remote) or `npm run db:init:local`.

CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
  id TEXT PRIMARY KEY,                                  -- acc_...
  kind TEXT NOT NULL CHECK (kind IN ('individual', 'organization')),
  name TEXT NOT NULL,
  use_class_default TEXT NOT NULL DEFAULT 'academic'
    CHECK (use_class_default IN ('academic', 'commercial', 'nonprofit', 'government')),
  -- Provider-agnostic: whichever billing provider is chosen later fills these.
  billing_provider TEXT,
  billing_customer_ref TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,                                  -- usr_...
  email TEXT NOT NULL,
  email_canonical TEXT NOT NULL UNIQUE,
  name TEXT,
  created_at INTEGER NOT NULL,
  last_login_at INTEGER
);

CREATE TABLE IF NOT EXISTS account_members (
  id TEXT PRIMARY KEY,                                  -- mem_...
  account_id TEXT NOT NULL REFERENCES accounts(id),
  user_id TEXT NOT NULL REFERENCES users(id),
  role TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'member')),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'removed')),
  created_at INTEGER NOT NULL,
  removed_at INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS account_members_one_active
  ON account_members(account_id, user_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS account_members_user ON account_members(user_id, status);

CREATE TABLE IF NOT EXISTS licenses (
  id TEXT PRIMARY KEY,                                  -- lic_...
  account_id TEXT NOT NULL REFERENCES accounts(id),
  plan TEXT NOT NULL CHECK (plan IN ('paid')),
  is_trial INTEGER NOT NULL DEFAULT 0 CHECK (is_trial IN (0, 1)),
  status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'suspended', 'expired', 'revoked')),
  use_class TEXT NOT NULL
    CHECK (use_class IN ('academic', 'commercial', 'nonprofit', 'government')),
  -- Explicit: an empty list unlocks nothing. Never "missing means unlimited".
  entitlements_json TEXT NOT NULL DEFAULT '["ai"]',
  seats INTEGER NOT NULL CHECK (seats >= 1),
  envs_per_seat INTEGER NOT NULL DEFAULT 2 CHECK (envs_per_seat >= 1),
  grace_days INTEGER NOT NULL DEFAULT 14 CHECK (grace_days >= 0),
  offline_allowed INTEGER NOT NULL DEFAULT 1 CHECK (offline_allowed IN (0, 1)),
  offline_max_days INTEGER NOT NULL DEFAULT 180 CHECK (offline_max_days >= 1),
  starts_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  renewal_state TEXT NOT NULL DEFAULT 'manual'
    CHECK (renewal_state IN ('manual', 'auto', 'cancelled', 'none')),
  notes TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  revoked_at INTEGER,
  revoke_reason TEXT
);
CREATE INDEX IF NOT EXISTS licenses_account ON licenses(account_id);
CREATE INDEX IF NOT EXISTS licenses_expiry ON licenses(status, expires_at);

CREATE TABLE IF NOT EXISTS seat_assignments (
  id TEXT PRIMARY KEY,                                  -- sa_...
  license_id TEXT NOT NULL REFERENCES licenses(id),
  account_id TEXT NOT NULL REFERENCES accounts(id),
  user_id TEXT REFERENCES users(id),                    -- NULL until assigned
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'released', 'revoked')),
  seat_key_hash TEXT NOT NULL UNIQUE,
  seat_key_hint TEXT NOT NULL,                          -- the last four characters
  seat_key_vault TEXT,                                  -- AES-GCM, only with KEY_VAULT_KEY
  -- The seams for a seat that differs from its licence. NULL = inherit.
  entitlements_override_json TEXT,
  envs_per_seat INTEGER,
  -- Environment removals are rate-limited per seat: no removal before this.
  cooldown_until INTEGER NOT NULL DEFAULT 0,
  releases INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  released_at INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS seat_one_active_per_user
  ON seat_assignments(account_id, user_id) WHERE status = 'active' AND user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS seat_license ON seat_assignments(license_id, status);

-- An environment is a registration: a laptop, a workstation, or ONE WHOLE
-- CLUSTER -- never a node, a job or a container. `env_binding_hash` is
-- HMAC(FP_PEPPER, sha256(client secret)); the client secret never arrives here.
CREATE TABLE IF NOT EXISTS environments (
  id TEXT PRIMARY KEY,                                  -- env_...
  seat_id TEXT NOT NULL REFERENCES seat_assignments(id),
  license_id TEXT NOT NULL REFERENCES licenses(id),
  kind TEXT NOT NULL CHECK (kind IN ('desktop', 'cluster', 'container-host')),
  display_name TEXT NOT NULL,
  env_binding_hash TEXT NOT NULL,
  delegation_pubkey TEXT,
  registration_kind TEXT NOT NULL CHECK (registration_kind IN ('online', 'offline')),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'released', 'revoked')),
  platform TEXT,
  scheduler_hint TEXT,
  app_version TEXT,
  created_at INTEGER NOT NULL,
  -- Written at most once a REFRESH_WRITE_INTERVAL_DAYS by a conditional UPDATE,
  -- so a refresh that finds nothing to change costs no write at all.
  last_refresh_at INTEGER,
  released_at INTEGER,
  release_reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS environments_one_active_binding
  ON environments(seat_id, env_binding_hash) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS environments_seat ON environments(seat_id, status);
CREATE INDEX IF NOT EXISTS environments_idle ON environments(status, last_refresh_at);

-- Opaque bearer credentials (PLXT1_...), stored as SHA-256 only. Their one
-- power is asking this service for a certificate.
CREATE TABLE IF NOT EXISTS license_tokens (
  id TEXT PRIMARY KEY,                                  -- tok_...
  seat_id TEXT NOT NULL REFERENCES seat_assignments(id),
  license_id TEXT NOT NULL REFERENCES licenses(id),
  token_hash TEXT NOT NULL UNIQUE,
  token_hint TEXT NOT NULL,
  scope TEXT NOT NULL CHECK (scope IN ('interactive', 'hpc', 'automation', 'ci')),
  label TEXT NOT NULL,
  created_by TEXT,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  revoked_at INTEGER,
  last_used_at INTEGER,
  use_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS license_tokens_seat ON license_tokens(seat_id);

-- Offline certificates cannot be recalled before they run out, so every one
-- is recorded, capped per licence, and shown to the licence holder.
CREATE TABLE IF NOT EXISTS offline_grants (
  id TEXT PRIMARY KEY,                                  -- off_...
  license_id TEXT NOT NULL REFERENCES licenses(id),
  seat_id TEXT NOT NULL REFERENCES seat_assignments(id),
  environment_id TEXT NOT NULL REFERENCES environments(id),
  cert_id TEXT NOT NULL,
  issued_at INTEGER NOT NULL,
  offline_until INTEGER NOT NULL,
  created_by TEXT
);
CREATE INDEX IF NOT EXISTS offline_grants_license ON offline_grants(license_id, issued_at);

-- One trial per mailbox, enforced by the UNIQUE index -- two racing requests
-- cannot both win it. The fingerprint is peppered and required.
CREATE TABLE IF NOT EXISTS trials (
  id TEXT PRIMARY KEY,                                  -- tri_...
  email_canonical TEXT NOT NULL UNIQUE,
  fingerprint_hash TEXT NOT NULL,
  license_id TEXT,
  ip_hash TEXT,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS trials_fingerprint ON trials(fingerprint_hash);

CREATE TABLE IF NOT EXISTS invitations (
  id TEXT PRIMARY KEY,                                  -- inv_...
  account_id TEXT NOT NULL REFERENCES accounts(id),
  license_id TEXT REFERENCES licenses(id),
  email_canonical TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('admin', 'member')),
  token_hash TEXT NOT NULL UNIQUE,
  invited_by TEXT,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  accepted_at INTEGER,
  revoked_at INTEGER
);
CREATE INDEX IF NOT EXISTS invitations_account ON invitations(account_id);

CREATE TABLE IF NOT EXISTS purchases (
  id TEXT PRIMARY KEY,                                  -- pur_...
  account_id TEXT NOT NULL REFERENCES accounts(id),
  license_id TEXT REFERENCES licenses(id),
  provider TEXT NOT NULL,                               -- 'manual', 'trial', or a billing provider
  provider_ref TEXT,
  kind TEXT NOT NULL CHECK (kind IN ('new', 'renewal', 'trial', 'manual', 'upgrade')),
  amount_cents INTEGER NOT NULL DEFAULT 0,
  currency TEXT,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS webhook_events (
  id TEXT PRIMARY KEY,                                  -- the provider's event id
  provider TEXT NOT NULL,
  received_at INTEGER NOT NULL,
  processed_at INTEGER,
  payload_hash TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,                                  -- SHA-256 of the cookie secret
  user_id TEXT NOT NULL REFERENCES users(id),
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS login_links (
  id TEXT PRIMARY KEY,                                  -- SHA-256 of the link secret
  email_canonical TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  used_at INTEGER
);

CREATE TABLE IF NOT EXISTS rate_limits (
  key TEXT PRIMARY KEY,                                 -- '<bucket>:<subject>:<window start>'
  count INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);

-- Reminder emails already sent, so the daily cron sends each once.
CREATE TABLE IF NOT EXISTS notices (
  license_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  sent_at INTEGER NOT NULL,
  PRIMARY KEY (license_id, kind)
);

-- The audit log. `payload` is dropped after EVENT_PAYLOAD_RETENTION_MONTHS;
-- the row (who, what, when) is kept.
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at INTEGER NOT NULL,
  actor TEXT NOT NULL,                                  -- 'client', 'portal:<usr>', 'admin:<who>', 'cron', 'trial'
  kind TEXT NOT NULL,
  account_id TEXT,
  license_id TEXT,
  seat_id TEXT,
  environment_id TEXT,
  ip_hash TEXT,
  payload TEXT
);
CREATE INDEX IF NOT EXISTS events_at ON events(at);
CREATE INDEX IF NOT EXISTS events_license ON events(license_id, at);
CREATE INDEX IF NOT EXISTS events_kind ON events(kind, at);

-- Misuse signals for a person to review. Computed nightly; NEVER read on a
-- request path, and never the cause of an automatic block.
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  subject TEXT NOT NULL,
  detail TEXT,
  created_at INTEGER NOT NULL,
  acked_at INTEGER,
  acked_by TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS signals_open ON signals(kind, subject) WHERE acked_at IS NULL;

INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (1, unixepoch());
