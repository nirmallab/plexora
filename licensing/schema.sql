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

-- ---------------------------------------------------------------------------
-- Plexora AI (schema v2): the gateway's money and its tracking.
--
-- The gateway (src/ai/, routes /v1/ai/*) lives in this Worker so every model
-- call is tied to the account, licence, seat and environment that made it.
-- Money is INTEGER micro-USD (1,000,000 = $1.00; 1 credit = 1 cent = 10,000).
-- NOTHING a model saw or said is stored: no prompt, image, answer, marker,
-- image name or path. One metadata row per call, the balance, and a ledger.
-- Re-applying this file is safe (IF NOT EXISTS); an existing database picks
-- the tables up with `npm run db:init`.

-- Per-account AI settings. No row = mode 'credits' with the knob defaults.
-- mode 'dev' is internal testing: the account may use /v1/ai/dev/*, which is
-- metered at provider cost (no markup) and may name a model.
CREATE TABLE IF NOT EXISTS ai_accounts (
  account_id TEXT PRIMARY KEY REFERENCES accounts(id),
  mode TEXT NOT NULL DEFAULT 'credits' CHECK (mode IN ('credits', 'dev', 'disabled')),
  markup_bps INTEGER CHECK (markup_bps IS NULL OR markup_bps >= 10000),   -- NULL = AI_MARKUP_BPS
  allowance_micro INTEGER CHECK (allowance_micro IS NULL OR allowance_micro >= 0),  -- monthly, NULL = seats x knob
  notes TEXT,
  updated_at INTEGER NOT NULL,
  updated_by TEXT
);

-- The balance. Reservations move `held_micro` with conditional UPDATEs, so two
-- racing calls cannot both spend the last credit. `prepaid_micro` may go
-- slightly negative: a provider bills a call we already let through, and one
-- call's overrun is bounded by AI_MAX_RESERVE_MICRO.
CREATE TABLE IF NOT EXISTS ai_balances (
  account_id TEXT PRIMARY KEY REFERENCES accounts(id),
  prepaid_micro INTEGER NOT NULL DEFAULT 0,
  allowance_micro INTEGER NOT NULL DEFAULT 0 CHECK (allowance_micro >= 0),
  allowance_period TEXT,                                -- 'YYYY-MM' the allowance belongs to
  held_micro INTEGER NOT NULL DEFAULT 0 CHECK (held_micro >= 0),
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_holds (
  id TEXT PRIMARY KEY,                                  -- hold_...
  account_id TEXT NOT NULL,
  request_id TEXT,
  run_id TEXT,
  micro INTEGER NOT NULL CHECK (micro >= 0),
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ai_holds_account ON ai_holds(account_id, expires_at);

-- Every movement of credit, signed (+ into a bucket, - out of it). Σ legs per
-- bucket = the balance. (journal_id, bucket) is unique, so a retried grant or
-- settlement cannot post twice.
CREATE TABLE IF NOT EXISTS ai_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  journal_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  at INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('purchase', 'grant', 'allowance_grant', 'allowance_expiry', 'settle',
    'refund', 'adjustment')),
  bucket TEXT NOT NULL CHECK (bucket IN ('prepaid', 'allowance')),
  amount_micro INTEGER NOT NULL,
  ref_type TEXT,
  ref_id TEXT,
  user_id TEXT,
  actor TEXT,
  note TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ai_ledger_journal ON ai_ledger(journal_id, bucket);
CREATE INDEX IF NOT EXISTS ai_ledger_account ON ai_ledger(account_id, at);

-- A declared bounded session ("quoted, capped, metered"): the quote is held at
-- the start, each call accrues its metered price, and the account is charged
-- min(accrued, quote). Calls beyond the envelope are refused.
CREATE TABLE IF NOT EXISTS ai_runs (
  id TEXT PRIMARY KEY,                                  -- run_...
  account_id TEXT NOT NULL,
  user_id TEXT,
  seat_id TEXT,
  billing TEXT NOT NULL CHECK (billing IN ('credits', 'dev')),
  session_id TEXT,
  feature TEXT NOT NULL,
  unit TEXT NOT NULL,
  units INTEGER NOT NULL CHECK (units >= 1),
  quote_micro INTEGER NOT NULL,
  hold_micro INTEGER NOT NULL DEFAULT 0,                -- what is still reserved for it
  accrued_micro INTEGER NOT NULL DEFAULT 0,             -- Σ metered price of its calls
  charged_micro INTEGER NOT NULL DEFAULT 0,             -- what the account has paid
  last_charge_micro INTEGER NOT NULL DEFAULT 0,         -- the latest call's share (settlement scratch)
  cost_micro INTEGER NOT NULL DEFAULT 0,                -- Σ provider cost
  envelope_calls INTEGER NOT NULL,
  calls INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK (status IN ('open', 'finished', 'expired')),
  started_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  closed_at INTEGER
);
CREATE INDEX IF NOT EXISTS ai_runs_account ON ai_runs(account_id, started_at);
CREATE INDEX IF NOT EXISTS ai_runs_open ON ai_runs(status, expires_at);

-- One immutable row per model call, from the PROVIDER's usage (never the
-- client's). Unit prices are copied in so history survives price changes.
CREATE TABLE IF NOT EXISTS ai_requests (
  id TEXT PRIMARY KEY,                                  -- req_...
  account_id TEXT NOT NULL,
  user_id TEXT,
  license_id TEXT,
  seat_id TEXT,
  environment_id TEXT,
  token_jti TEXT,
  billing TEXT NOT NULL CHECK (billing IN ('credits', 'dev', 'shadow')),   -- shadow: Plexora's cost, never billed
  run_id TEXT,
  session_id TEXT,
  feature TEXT,
  agent TEXT,
  workflow TEXT,
  attempt INTEGER NOT NULL DEFAULT 1,
  app_version TEXT,
  capability TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  route_id TEXT,                                        -- builtin:<capability>, an ai_routes id, or dev:<model>
  attempts INTEGER NOT NULL DEFAULT 1,                  -- upstream tries, across retries and failover
  failover INTEGER NOT NULL DEFAULT 0,                  -- 1 when a lower-ranked route served it
  resolved_model TEXT,                                  -- what the provider says answered (aggregators)
  reported_cost_micro INTEGER,                          -- the provider's own cost figure, when it sends one
  shadow_of TEXT,                                       -- the served request a shadow call duplicated
  shadow_agree INTEGER,                                 -- 1 or 0: same decision as the served answer, NULL: not comparable
  provider_request_id TEXT,
  status TEXT NOT NULL CHECK (status IN ('ok', 'error', 'incomplete')),
  failure_class TEXT,
  http_status INTEGER,
  stop_reason TEXT,
  usage_source TEXT NOT NULL CHECK (usage_source IN ('provider', 'partial', 'none')),
  input_uncached INTEGER NOT NULL DEFAULT 0,
  cache_read INTEGER NOT NULL DEFAULT 0,
  cache_write_5m INTEGER NOT NULL DEFAULT 0,
  cache_write_1h INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  image_count INTEGER NOT NULL DEFAULT 0,
  p_in INTEGER NOT NULL, p_cache_read INTEGER NOT NULL, p_cache_write_5m INTEGER NOT NULL,
  p_cache_write_1h INTEGER NOT NULL, p_out INTEGER NOT NULL,  -- provider cost per 1M tokens, micro-USD
  markup_bps INTEGER NOT NULL,                          -- 10000 on the dev route
  hold_micro INTEGER NOT NULL DEFAULT 0,
  cost_micro INTEGER NOT NULL DEFAULT 0,                -- what the provider charges us
  price_micro INTEGER NOT NULL DEFAULT 0,               -- metered price (cost x markup)
  charged_micro INTEGER NOT NULL DEFAULT 0,             -- what this call took from the balance
  request_bytes INTEGER NOT NULL DEFAULT 0,
  started_at_ms INTEGER NOT NULL,
  first_byte_ms INTEGER,
  finished_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ai_requests_account ON ai_requests(account_id, started_at_ms);
CREATE INDEX IF NOT EXISTS ai_requests_run ON ai_requests(run_id);
CREATE INDEX IF NOT EXISTS ai_requests_session ON ai_requests(session_id);

-- Idempotency keys, per account, for AI_IDEMPOTENCY_TTL_S. A repeated key is
-- never sent upstream twice.
CREATE TABLE IF NOT EXISTS ai_idempotency (
  account_id TEXT NOT NULL,
  key_hash TEXT NOT NULL,
  request_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('open', 'settled', 'failed')),
  price_micro INTEGER,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (account_id, key_hash)
) WITHOUT ROWID;

-- Routing (src/ai/routing.ts). Models other than Anthropic's built-in list
-- are catalogued here with their unit costs (micro-USD per 1M tokens) and the
-- source of the price; `fee_bps` is an aggregator's fee on top (OpenRouter 550).
CREATE TABLE IF NOT EXISTS ai_models (
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  in_micro INTEGER NOT NULL CHECK (in_micro >= 0),
  cache_read_micro INTEGER NOT NULL CHECK (cache_read_micro >= 0),
  cache_write_5m_micro INTEGER NOT NULL CHECK (cache_write_5m_micro >= 0),
  cache_write_1h_micro INTEGER NOT NULL CHECK (cache_write_1h_micro >= 0),
  out_micro INTEGER NOT NULL CHECK (out_micro >= 0),
  fee_bps INTEGER NOT NULL DEFAULT 0 CHECK (fee_bps >= 0),
  enabled INTEGER NOT NULL DEFAULT 1,
  source_url TEXT,
  note TEXT,
  updated_at INTEGER NOT NULL,
  updated_by TEXT,
  PRIMARY KEY (provider, model)
) WITHOUT ROWID;

-- The published route table, per (feature, capability); feature '*' is the
-- default. role 'serve' rows are tried by rank; role 'shadow' rows duplicate
-- shadow_pct % of sessions to a candidate at Plexora's cost (never billed,
-- never applied) so it gathers real-traffic agreement before promotion. A
-- serve row that is not a direct provider's default needs evaluation_id: a
-- passing ai_route_evaluations row on the module's current bench version.
CREATE TABLE IF NOT EXISTS ai_routes (
  id TEXT PRIMARY KEY,                                  -- rt_...
  feature TEXT NOT NULL DEFAULT '*',
  capability TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'serve' CHECK (role IN ('serve', 'shadow')),
  rank INTEGER NOT NULL CHECK (rank >= 0),
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  effort TEXT CHECK (effort IS NULL OR effort IN ('low', 'medium', 'high')),
  max_tokens_cap INTEGER NOT NULL CHECK (max_tokens_cap > 0),
  failover TEXT NOT NULL DEFAULT 'outage' CHECK (failover IN ('outage', 'error', 'never')),
  evaluation_id INTEGER,
  shadow_pct INTEGER NOT NULL DEFAULT 0 CHECK (shadow_pct BETWEEN 0 AND 100),
  enabled INTEGER NOT NULL DEFAULT 1,
  note TEXT,
  updated_at INTEGER NOT NULL,
  updated_by TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ai_routes_rank ON ai_routes(feature, capability, role, rank);

-- Routing-bench results (`plexora ai bench route`). `passed` is computed by
-- the gateway from the metrics against catalog.BENCH, never taken on trust.
CREATE TABLE IF NOT EXISTS ai_route_evaluations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  feature TEXT NOT NULL,
  capability TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  bench_version TEXT NOT NULL,
  plexora_version TEXT,
  dataset_ids_json TEXT NOT NULL DEFAULT '[]',
  metrics_json TEXT NOT NULL,
  passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
  misses_json TEXT NOT NULL DEFAULT '[]',
  report_url TEXT,
  evaluated_at INTEGER NOT NULL,
  evaluated_by TEXT
);
CREATE INDEX IF NOT EXISTS ai_route_evaluations_route
  ON ai_route_evaluations(feature, capability, provider, model, evaluated_at);

-- Circuit breakers, per provider:model (or a whole provider, for the admin
-- kill switch: forced = 'open').
CREATE TABLE IF NOT EXISTS ai_circuits (
  route_key TEXT PRIMARY KEY,
  window_start INTEGER NOT NULL,
  ok INTEGER NOT NULL DEFAULT 0,
  fail INTEGER NOT NULL DEFAULT 0,
  open_until INTEGER NOT NULL DEFAULT 0,
  forced TEXT CHECK (forced IS NULL OR forced = 'open'),
  reason TEXT,
  updated_at INTEGER NOT NULL
) WITHOUT ROWID;

-- The route that last served a session, so its prompt cache stays warm.
CREATE TABLE IF NOT EXISTS ai_sticky (
  account_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  route_id TEXT NOT NULL,
  at INTEGER NOT NULL,
  PRIMARY KEY (account_id, session_id)
) WITHOUT ROWID;

INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (1, unixepoch());
INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (2, unixepoch());
