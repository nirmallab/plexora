-- Plexora telemetry D1 schema.
--
-- Two rules hold for every table, and a test enforces both:
--   * WITHOUT ROWID, with a day-first or id primary key, so the key *is* the
--     storage order and range scans on it are the only access path needed;
--   * no secondary indexes. D1 bills one written row per index touched by a
--     write, and the free-tier ceiling that binds first is rows written.
-- The only queries that cannot range on a leading key are admin-only and are
-- bounded to a window in SQL.
--
-- Safe to re-run: every statement is IF NOT EXISTS.

-- One row per installation. Identity columns are nulled (erased_at set) after
-- INSTALL_INACTIVE_ERASE_DAYS of silence or on an admin erase.
CREATE TABLE IF NOT EXISTS installs (
  install_id      TEXT PRIMARY KEY,
  first_seen      INTEGER NOT NULL,
  last_seen       INTEGER NOT NULL,
  last_active_day TEXT,
  version         TEXT,
  python          TEXT,
  os              TEXT,
  arch            TEXT,
  launch_mode     TEXT,
  deployment      TEXT,
  scheduler       TEXT,
  install_kind    TEXT,
  mode            TEXT,
  country         TEXT,
  days_active     INTEGER NOT NULL DEFAULT 0,
  erased_at       INTEGER,
  -- Reserved for a future licensing channel; always NULL in v1.
  machine_id_hash TEXT,
  license_id      TEXT,
  license_tier    TEXT
) WITHOUT ROWID;

-- One row per accepted upload. events_json feeds the json_each folds (SQLite
-- does the work, not Worker CPU); events_gz is the same JSONL line as one gzip
-- member, so the daily archive is a byte concatenation with no compression in
-- the cron. events_gz is base64 TEXT, not a BLOB: D1 hands BLOBs back as
-- arrays of numbers, and turning a 1 MiB export segment of those into bytes
-- would spend the cron's whole CPU budget; atob on text is native. PK order makes dedupe point lookups, rate limits PK range counts
-- and fold slices contiguous install ranges.
CREATE TABLE IF NOT EXISTS batches (
  day          TEXT NOT NULL,
  install_id   TEXT NOT NULL,
  id           TEXT NOT NULL,
  received     INTEGER NOT NULL,
  session_id   TEXT,
  version      TEXT,
  python       TEXT,
  os           TEXT,
  arch         TEXT,
  launch_mode  TEXT,
  deployment   TEXT,
  scheduler    TEXT,
  install_kind TEXT,
  mode         TEXT,
  country      TEXT,
  events       INTEGER NOT NULL,
  errors       INTEGER NOT NULL,
  bytes        INTEGER NOT NULL,
  priority_max INTEGER NOT NULL,
  has_session  INTEGER NOT NULL,
  events_json  TEXT NOT NULL,
  events_gz    TEXT NOT NULL,
  PRIMARY KEY (day, install_id, id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS daily_installs (
  day         TEXT NOT NULL,
  install_id  TEXT NOT NULL,
  version     TEXT,
  launch_mode TEXT,
  deployment  TEXT,
  os          TEXT,
  country     TEXT,
  mode        TEXT,
  sessions    INTEGER NOT NULL,
  batches     INTEGER NOT NULL,
  events      INTEGER NOT NULL,
  errors      INTEGER NOT NULL,
  PRIMARY KEY (day, install_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS daily_usage (
  day         TEXT NOT NULL,
  version     TEXT NOT NULL,
  launch_mode TEXT NOT NULL,
  deployment  TEXT NOT NULL,
  os          TEXT NOT NULL,
  installs    INTEGER NOT NULL,
  sessions    INTEGER NOT NULL,
  batches     INTEGER NOT NULL,
  events      INTEGER NOT NULL,
  errors      INTEGER NOT NULL,
  PRIMARY KEY (day, version, launch_mode, deployment, os)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS version_usage (
  day          TEXT NOT NULL,
  version      TEXT NOT NULL,
  installs     INTEGER NOT NULL,
  new_installs INTEGER NOT NULL,
  sessions     INTEGER NOT NULL,
  batches      INTEGER NOT NULL,
  errors       INTEGER NOT NULL,
  PRIMARY KEY (day, version)
) WITHOUT ROWID;

-- ← tool.summary (keys open/close/fold/activate/load_failed; load_ms bins).
CREATE TABLE IF NOT EXISTS plugin_usage (
  day         TEXT NOT NULL,
  version     TEXT NOT NULL,
  plugin      TEXT NOT NULL,
  opens       INTEGER NOT NULL,
  closes      INTEGER NOT NULL,
  folds       INTEGER NOT NULL,
  activates   INTEGER NOT NULL,
  load_failed INTEGER NOT NULL,
  installs    INTEGER NOT NULL,
  b0 INTEGER NOT NULL, b1 INTEGER NOT NULL, b2 INTEGER NOT NULL,
  b3 INTEGER NOT NULL, b4 INTEGER NOT NULL, b5 INTEGER NOT NULL,
  b6 INTEGER NOT NULL, b7 INTEGER NOT NULL, b8 INTEGER NOT NULL,
  PRIMARY KEY (day, version, plugin)
) WITHOUT ROWID;

-- ← feature.summary (dims feature, plugin).
CREATE TABLE IF NOT EXISTS feature_usage (
  day      TEXT NOT NULL,
  version  TEXT NOT NULL,
  plugin   TEXT NOT NULL,
  feature  TEXT NOT NULL,
  n        INTEGER NOT NULL,
  installs INTEGER NOT NULL,
  PRIMARY KEY (day, version, plugin, feature)
) WITHOUT ROWID;

-- ← function.summary (source, fn; keys n/err/ms) + capability.summary n as agent.
CREATE TABLE IF NOT EXISTS function_usage (
  day      TEXT NOT NULL,
  version  TEXT NOT NULL,
  source   TEXT NOT NULL,
  fn       TEXT NOT NULL,
  n        INTEGER NOT NULL,
  err      INTEGER NOT NULL,
  installs INTEGER NOT NULL,
  b0 INTEGER NOT NULL, b1 INTEGER NOT NULL, b2 INTEGER NOT NULL,
  b3 INTEGER NOT NULL, b4 INTEGER NOT NULL, b5 INTEGER NOT NULL,
  b6 INTEGER NOT NULL, b7 INTEGER NOT NULL, b8 INTEGER NOT NULL,
  PRIMARY KEY (day, version, source, fn)
) WITHOUT ROWID;

-- ← capability.summary (n by outcome/transport; ms bins).
CREATE TABLE IF NOT EXISTS capability_usage (
  day        TEXT NOT NULL,
  version    TEXT NOT NULL,
  capability TEXT NOT NULL,
  owner      TEXT NOT NULL,
  ok         INTEGER NOT NULL,
  err        INTEGER NOT NULL,
  refused    INTEGER NOT NULL,
  nested     INTEGER NOT NULL,
  installs   INTEGER NOT NULL,
  b0 INTEGER NOT NULL, b1 INTEGER NOT NULL, b2 INTEGER NOT NULL,
  b3 INTEGER NOT NULL, b4 INTEGER NOT NULL, b5 INTEGER NOT NULL,
  b6 INTEGER NOT NULL, b7 INTEGER NOT NULL, b8 INTEGER NOT NULL,
  PRIMARY KEY (day, version, capability, owner)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS capability_transitions (
  day      TEXT NOT NULL,
  version  TEXT NOT NULL,
  from_cap TEXT NOT NULL,
  to_cap   TEXT NOT NULL,
  n        INTEGER NOT NULL,
  PRIMARY KEY (day, version, from_cap, to_cap)
) WITHOUT ROWID;

-- ← dataset.opened props.
CREATE TABLE IF NOT EXISTS modality_usage (
  day         TEXT NOT NULL,
  version     TEXT NOT NULL,
  modality    TEXT NOT NULL,
  image_kind  TEXT NOT NULL,
  run_format  TEXT NOT NULL,
  table_kind  TEXT NOT NULL,
  n           INTEGER NOT NULL,
  installs    INTEGER NOT NULL,
  distributed INTEGER NOT NULL,
  PRIMARY KEY (day, version, modality, image_kind, run_format, table_kind)
) WITHOUT ROWID;

-- A new scale dimension is a new `dim` value, never a new column.
CREATE TABLE IF NOT EXISTS data_scale_usage (
  day      TEXT NOT NULL,
  version  TEXT NOT NULL,
  dim      TEXT NOT NULL,
  band     TEXT NOT NULL,
  n        INTEGER NOT NULL,
  installs INTEGER NOT NULL,
  PRIMARY KEY (day, version, dim, band)
) WITHOUT ROWID;

-- metric = '<event>.<key>'; dims_key = canonical 'k=v;k=v' over a fixed
-- per-metric subset (<= 4) of validated dims, built in SQL.
CREATE TABLE IF NOT EXISTS performance_rollups (
  day      TEXT NOT NULL,
  version  TEXT NOT NULL,
  metric   TEXT NOT NULL,
  dims_key TEXT NOT NULL,
  n        INTEGER NOT NULL,
  ok       INTEGER NOT NULL,
  err      INTEGER NOT NULL,
  sum      REAL,
  max      REAL,
  b0 INTEGER NOT NULL, b1 INTEGER NOT NULL, b2 INTEGER NOT NULL,
  b3 INTEGER NOT NULL, b4 INTEGER NOT NULL, b5 INTEGER NOT NULL,
  b6 INTEGER NOT NULL, b7 INTEGER NOT NULL, b8 INTEGER NOT NULL,
  PRIMARY KEY (day, version, metric, dims_key)
) WITHOUT ROWID;

-- The registry: never cleared by a fold. Counts are summed from error_daily at
-- read time so a re-fold cannot double them.
CREATE TABLE IF NOT EXISTS error_fingerprints (
  fp            TEXT PRIMARY KEY,
  side          TEXT,
  exception     TEXT,
  component     TEXT,
  route         TEXT,
  action        TEXT,
  file          TEXT,
  plugin        TEXT,
  first_seen    TEXT NOT NULL,
  last_seen     TEXT NOT NULL,
  first_version TEXT,
  last_version  TEXT,
  muted_at      INTEGER,
  muted_by      TEXT,
  notes         TEXT
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS error_daily (
  day      TEXT NOT NULL,
  version  TEXT NOT NULL,
  fp       TEXT NOT NULL,
  n        INTEGER NOT NULL,
  installs INTEGER NOT NULL,
  PRIMARY KEY (day, version, fp)
) WITHOUT ROWID;

-- Per-version server config; version '*' is the default for every version.
CREATE TABLE IF NOT EXISTS client_config (
  version           TEXT PRIMARY KEY,
  level_max         TEXT,
  upload_interval_s INTEGER,
  sample            REAL,
  disabled_until    INTEGER NOT NULL DEFAULT 0,
  note              TEXT,
  updated_at        INTEGER NOT NULL,
  updated_by        TEXT
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS register_quota (
  ip_hash TEXT NOT NULL,
  day     TEXT NOT NULL,
  n       INTEGER NOT NULL,
  PRIMARY KEY (ip_hash, day)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS budget_daily (
  day              TEXT PRIMARY KEY,
  requests         INTEGER NOT NULL DEFAULT 0,
  register_requests INTEGER NOT NULL DEFAULT 0,
  admin_requests   INTEGER NOT NULL DEFAULT 0,
  cron_ticks       INTEGER NOT NULL DEFAULT 0,
  batches          INTEGER NOT NULL DEFAULT 0,
  events           INTEGER NOT NULL DEFAULT 0,
  duplicates       INTEGER NOT NULL DEFAULT 0,
  rate_limited     INTEGER NOT NULL DEFAULT 0,
  refused_503      INTEGER NOT NULL DEFAULT 0,
  dropped_events   INTEGER NOT NULL DEFAULT 0,
  d1_rows_read     INTEGER NOT NULL DEFAULT 0,
  d1_rows_written  INTEGER NOT NULL DEFAULT 0,
  bytes_in         INTEGER NOT NULL DEFAULT 0,
  r2_puts          INTEGER NOT NULL DEFAULT 0,
  r2_gets          INTEGER NOT NULL DEFAULT 0,
  r2_bytes         INTEGER NOT NULL DEFAULT 0,
  state            TEXT NOT NULL DEFAULT 'ok',
  state_changed_at INTEGER,
  note             TEXT
) WITHOUT ROWID;

-- One row per (run day, step). `day` is the day the step works on for
-- fold/export/verify, and the run day for the purge/sweep/finalize steps.
CREATE TABLE IF NOT EXISTS maintenance_log (
  day          TEXT NOT NULL,
  step         TEXT NOT NULL,
  step_order   INTEGER NOT NULL,
  state        TEXT NOT NULL,
  cursor       TEXT,
  attempts     INTEGER NOT NULL DEFAULT 0,
  started      INTEGER,
  finished     INTEGER,
  rows_read    INTEGER NOT NULL DEFAULT 0,
  rows_written INTEGER NOT NULL DEFAULT 0,
  object_key   TEXT,
  checksum     TEXT,
  error        TEXT,
  PRIMARY KEY (day, step)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS archives (
  key         TEXT PRIMARY KEY,
  kind        TEXT NOT NULL,
  period      TEXT NOT NULL,
  segments    INTEGER NOT NULL,
  parts_json  TEXT NOT NULL,
  bytes       INTEGER NOT NULL,
  records     INTEGER NOT NULL,
  sha256      TEXT,
  created     INTEGER NOT NULL,
  verified_at INTEGER,
  expires     INTEGER,
  deleted_at  INTEGER
) WITHOUT ROWID;

-- Reserved for the licensing channel; empty in v1.
CREATE TABLE IF NOT EXISTS license_summary (
  day      TEXT NOT NULL,
  version  TEXT NOT NULL,
  tier     TEXT NOT NULL,
  installs INTEGER NOT NULL,
  sessions INTEGER NOT NULL,
  PRIMARY KEY (day, version, tier)
) WITHOUT ROWID;
