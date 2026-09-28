/**
 * The fold: batches → rollups, entirely in SQL.
 *
 * Every statement is `INSERT … SELECT … FROM batches b, json_each(b.events_json) e
 * [, json_each(e.value,'$.rows') r] … GROUP BY … ON CONFLICT DO UPDATE SET col =
 * col + excluded.col`. SQLite does the arithmetic while the Worker waits on
 * I/O, which is what keeps a slice inside the 10 ms CPU limit.
 *
 * Idempotent: the first slice of a day deletes that day's rollup rows, so a
 * day can be re-folded any number of times. Sliced: each slice covers a
 * contiguous install_id range of about FOLD_SLICE_BATCHES batch rows, and its
 * statements and the maintenance_log cursor commit in one db.batch(), so a
 * failure leaves slice and cursor together or not at all. `installs` counts
 * (COUNT(DISTINCT install_id)) are summed across slices, which is correct only
 * because slices partition by install: a slice boundary is always an install
 * boundary.
 *
 * All SQL text is built from constants in this file and the vectors (never
 * from client data); values are bound.
 */
import type { Meter } from './budget';
import { VECTORS } from './schema';

/** Tables the first slice of a day clears. error_fingerprints is a registry and is never cleared. */
export const ROLLUP_TABLES = [
  'daily_installs',
  'daily_usage',
  'version_usage',
  'plugin_usage',
  'feature_usage',
  'function_usage',
  'capability_usage',
  'capability_transitions',
  'modality_usage',
  'data_scale_usage',
  'performance_rollups',
  'error_daily',
] as const;

/** dataset.opened band props folded into data_scale_usage as (dim, band). */
export const SCALE_DIMS = [
  'width',
  'height',
  'pixels',
  'channels_band',
  'rows',
  'markers',
  'points',
  'transcripts_genes',
  'transcripts_points',
  'hd_bins',
  'hd_genes',
];

/**
 * The per-event subset of row dims that forms `dims_key` (at most four, in
 * this order). Dims outside the subset are summed over.
 */
export const PERF_DIMS: Record<string, string[]> = {
  'render.summary': ['path', 'label_renderer', 'browser', 'gpu'],
  'server.summary': ['route', 'family', 'kind', 'source'],
  'node.summary': [],
  'capability.summary': ['name', 'status'],
  'tool.summary': ['tool'],
};

/** Record props with this dim subset, for band-valued (ms_labels) props. */
const RECORD_PERF_DIMS = ['kind', 'modality'];

/** Record outcomes that count as success for the performance ok/err split. */
const RECORD_OK_OUTCOMES = ['ready', 'connected', 'registered'];

const BINS = Array.from({ length: VECTORS.hist_bins }, (_, i) => i);
const BIN_COLS = BINS.map((i) => `b${i}`).join(', ');
const BIN_ADD = BINS.map((i) => `b${i} = b${i} + excluded.b${i}`).join(', ');

const SLICE = 'b.day = ?1 AND b.install_id > ?2 AND b.install_id <= ?3';
const ROWS = 'FROM batches b, json_each(b.events_json) e, json_each(e.value, \'$.rows\') r';
const RECORDS = 'FROM batches b, json_each(b.events_json) e';
const TYPE = (type: string) => `json_extract(e.value, '$.type') = '${type}'`;
const K = "json_extract(r.value, '$.k')";
const N = "json_extract(r.value, '$.n')";
const DIM = (name: string) => `json_extract(r.value, '$.d.${name}')`;
const PROP = (name: string) => `json_extract(e.value, '$.props.${name}')`;
const VERSION = "COALESCE(b.version, '')";

/** SUM of the row count where the key matches (and an optional extra condition). */
const countOf = (key: string, extra = '') => `SUM(CASE WHEN ${K} = '${key}'${extra ? ` AND ${extra}` : ''} THEN ${N} ELSE 0 END)`;
/** The nine histogram bin sums over rows with key(s) `keys`. */
const binsOf = (keys: string[]) =>
  BINS.map(
    (i) => `SUM(CASE WHEN ${K} IN (${keys.map((k) => `'${k}'`).join(', ')}) THEN COALESCE(json_extract(r.value, '$.h[${i}]'), 0) ELSE 0 END)`,
  ).join(', ');

/** Canonical `k=v;k=v` built in SQL from validated values only. */
function dimsKey(dims: string[], value: (dim: string) => string): string {
  if (dims.length === 0) return "''";
  return `ltrim(${dims.map((dim) => `COALESCE(';${dim}=' || ${value(dim)}, '')`).join(' || ')}, ';')`;
}

function histKeys(type: string): string[] {
  const spec = VECTORS.events[type];
  return Object.entries(spec?.keys ?? {})
    .filter(([, key]) => key.agg === 'hist')
    .map(([name]) => name);
}

/** Record props whose enum is exactly the millisecond band labels. */
export function recordBandMetrics(): { e: string; p: string }[] {
  const labels = JSON.stringify(VECTORS.ms_labels);
  const out: { e: string; p: string }[] = [];
  for (const [type, spec] of Object.entries(VECTORS.events)) {
    if (spec.kind !== 'record') continue;
    for (const [prop, propSpec] of Object.entries(spec.props ?? {})) {
      if (propSpec.t === 'enum' && JSON.stringify(propSpec.v) === labels) out.push({ e: type, p: prop });
    }
  }
  return out;
}

const PERF_UPSERT = `ON CONFLICT (day, version, metric, dims_key) DO UPDATE SET
  n = n + excluded.n, ok = ok + excluded.ok, err = err + excluded.err,
  sum = CASE WHEN sum IS NULL AND excluded.sum IS NULL THEN NULL ELSE COALESCE(sum, 0) + COALESCE(excluded.sum, 0) END,
  max = CASE WHEN max IS NULL THEN excluded.max WHEN excluded.max IS NULL THEN max ELSE MAX(max, excluded.max) END,
  ${BIN_ADD}`;

interface Folded {
  sql: string;
  /** Extra binds after ?1 day, ?2 lo, ?3 hi. */
  extra?: (day: string) => unknown[];
}

function perfCounterStatement(type: string): Folded {
  const keys = histKeys(type);
  const dims = PERF_DIMS[type] ?? [];
  const err = `SUM(CASE WHEN ${DIM('status')} IN ('5xx', 'failed') THEN ${N} ELSE 0 END)`;
  return {
    sql: `INSERT INTO performance_rollups (day, version, metric, dims_key, n, ok, err, sum, max, ${BIN_COLS})
      SELECT ?1, ${VERSION}, '${type}.' || ${K}, ${dimsKey(dims, DIM)},
        SUM(${N}), SUM(${N}) - ${err}, ${err},
        SUM(json_extract(r.value, '$.s')), MAX(json_extract(r.value, '$.mx')), ${binsOf(keys)}
      ${ROWS}
      WHERE ${SLICE} AND ${TYPE(type)} AND ${K} IN (${keys.map((k) => `'${k}'`).join(', ')})
      GROUP BY 2, 3, 4
      ${PERF_UPSERT}`,
  };
}

function perfRecordStatement(): Folded {
  const labels = VECTORS.ms_labels;
  const bins = BINS.map((i) => `SUM(CASE WHEN x.band = ?${6 + i} THEN 1 ELSE 0 END)`).join(', ');
  const ok = `SUM(CASE WHEN x.outcome IS NULL OR x.outcome IN (SELECT value FROM json_each(?5)) THEN 1 ELSE 0 END)`;
  return {
    sql: `INSERT INTO performance_rollups (day, version, metric, dims_key, n, ok, err, sum, max, ${BIN_COLS})
      SELECT ?1, x.version, x.type || '.' || x.prop, ${dimsKey(RECORD_PERF_DIMS, (d) => `x.${d}`)},
        COUNT(*), ${ok}, COUNT(*) - ${ok}, NULL, NULL, ${bins}
      FROM (
        SELECT ${VERSION} AS version,
          json_extract(e.value, '$.type') AS type,
          json_extract(m.value, '$.p') AS prop,
          json_extract(e.value, '$.props.' || json_extract(m.value, '$.p')) AS band,
          ${PROP('outcome')} AS outcome, ${PROP('kind')} AS kind, ${PROP('modality')} AS modality
        ${RECORDS}, json_each(?4) m
        WHERE ${SLICE} AND json_extract(e.value, '$.type') = json_extract(m.value, '$.e')
      ) x
      WHERE x.band IS NOT NULL
      GROUP BY 2, 3, 4
      ${PERF_UPSERT}`,
    extra: () => [JSON.stringify(recordBandMetrics()), JSON.stringify(RECORD_OK_OUTCOMES), ...labels],
  };
}

/** The fold statements of one slice, in order. */
export const FOLD_STATEMENTS: Folded[] = [
  {
    sql: `INSERT INTO daily_installs (day, install_id, version, launch_mode, deployment, os, country, mode,
        sessions, batches, events, errors)
      SELECT ?1, b.install_id, MAX(b.version), MAX(b.launch_mode), MAX(b.deployment), MAX(b.os),
        MAX(b.country), MAX(b.mode), SUM(b.has_session), COUNT(*), SUM(b.events), SUM(b.errors)
      FROM batches b WHERE ${SLICE} GROUP BY b.install_id
      ON CONFLICT (day, install_id) DO UPDATE SET sessions = sessions + excluded.sessions,
        batches = batches + excluded.batches, events = events + excluded.events, errors = errors + excluded.errors`,
  },
  {
    sql: `INSERT INTO daily_usage (day, version, launch_mode, deployment, os, installs, sessions, batches, events, errors)
      SELECT ?1, ${VERSION}, COALESCE(b.launch_mode, ''), COALESCE(b.deployment, ''), COALESCE(b.os, ''),
        COUNT(DISTINCT b.install_id), SUM(b.has_session), COUNT(*), SUM(b.events), SUM(b.errors)
      FROM batches b WHERE ${SLICE} GROUP BY 2, 3, 4, 5
      ON CONFLICT (day, version, launch_mode, deployment, os) DO UPDATE SET installs = installs + excluded.installs,
        sessions = sessions + excluded.sessions, batches = batches + excluded.batches,
        events = events + excluded.events, errors = errors + excluded.errors`,
  },
  {
    // new_installs: first seen on this day, per the installs registry.
    sql: `INSERT INTO version_usage (day, version, installs, new_installs, sessions, batches, errors)
      SELECT ?1, ${VERSION}, COUNT(DISTINCT b.install_id),
        COUNT(DISTINCT CASE WHEN i.first_seen >= ?4 AND i.first_seen < ?4 + 86400 THEN b.install_id END),
        SUM(b.has_session), COUNT(*), SUM(b.errors)
      FROM batches b LEFT JOIN installs i ON i.install_id = b.install_id
      WHERE ${SLICE} GROUP BY 2
      ON CONFLICT (day, version) DO UPDATE SET installs = installs + excluded.installs,
        new_installs = new_installs + excluded.new_installs, sessions = sessions + excluded.sessions,
        batches = batches + excluded.batches, errors = errors + excluded.errors`,
    extra: (day) => [Date.parse(`${day}T00:00:00Z`) / 1000],
  },
  {
    sql: `INSERT INTO plugin_usage (day, version, plugin, opens, closes, folds, activates, load_failed, installs, ${BIN_COLS})
      SELECT ?1, ${VERSION}, ${DIM('tool')}, ${countOf('open')}, ${countOf('close')}, ${countOf('fold')},
        ${countOf('activate')}, ${countOf('load_failed')}, COUNT(DISTINCT b.install_id), ${binsOf(['load_ms'])}
      ${ROWS} WHERE ${SLICE} AND ${TYPE('tool.summary')} AND ${DIM('tool')} IS NOT NULL
      GROUP BY 2, 3
      ON CONFLICT (day, version, plugin) DO UPDATE SET opens = opens + excluded.opens,
        closes = closes + excluded.closes, folds = folds + excluded.folds, activates = activates + excluded.activates,
        load_failed = load_failed + excluded.load_failed, installs = installs + excluded.installs, ${BIN_ADD}`,
  },
  {
    sql: `INSERT INTO feature_usage (day, version, plugin, feature, n, installs)
      SELECT ?1, ${VERSION}, COALESCE(${DIM('plugin')}, 'core'), ${DIM('feature')}, SUM(${N}), COUNT(DISTINCT b.install_id)
      ${ROWS} WHERE ${SLICE} AND ${TYPE('feature.summary')} AND ${K} = 'n' AND ${DIM('feature')} IS NOT NULL
      GROUP BY 2, 3, 4
      ON CONFLICT (day, version, plugin, feature) DO UPDATE SET n = n + excluded.n, installs = installs + excluded.installs`,
  },
  {
    sql: `INSERT INTO function_usage (day, version, source, fn, n, err, installs, ${BIN_COLS})
      SELECT ?1, ${VERSION}, ${DIM('source')}, ${DIM('fn')}, ${countOf('n')}, ${countOf('err')},
        COUNT(DISTINCT b.install_id), ${binsOf(['ms'])}
      ${ROWS} WHERE ${SLICE} AND ${TYPE('function.summary')} AND ${DIM('source')} IS NOT NULL AND ${DIM('fn')} IS NOT NULL
      GROUP BY 2, 3, 4
      ON CONFLICT (day, version, source, fn) DO UPDATE SET n = n + excluded.n, err = err + excluded.err,
        installs = installs + excluded.installs, ${BIN_ADD}`,
  },
  {
    // Agent capabilities are functions too, with source = agent.
    sql: `INSERT INTO function_usage (day, version, source, fn, n, err, installs, ${BIN_COLS})
      SELECT ?1, ${VERSION}, 'agent', ${DIM('name')}, ${countOf('n')},
        ${countOf('n', `${DIM('outcome')} NOT IN ('ok', 'permission_required', 'license_required')`)},
        COUNT(DISTINCT b.install_id), ${binsOf(['ms'])}
      ${ROWS} WHERE ${SLICE} AND ${TYPE('capability.summary')} AND ${K} IN ('n', 'ms') AND ${DIM('name')} IS NOT NULL
      GROUP BY 2, 3, 4
      ON CONFLICT (day, version, source, fn) DO UPDATE SET n = n + excluded.n, err = err + excluded.err,
        installs = installs + excluded.installs, ${BIN_ADD}`,
  },
  {
    // permission_required and license_required are refusals, not errors;
    // transport=nested counts as nested.
    sql: `INSERT INTO capability_usage (day, version, capability, owner, ok, err, refused, nested, installs, ${BIN_COLS})
      SELECT ?1, ${VERSION}, ${DIM('name')}, COALESCE(${DIM('owner')}, ''),
        ${countOf('n', `${DIM('outcome')} = 'ok'`)},
        ${countOf('n', `${DIM('outcome')} NOT IN ('ok', 'permission_required', 'license_required')`)},
        ${countOf('n', `${DIM('outcome')} IN ('permission_required', 'license_required')`)},
        ${countOf('n', `${DIM('transport')} = 'nested'`)},
        COUNT(DISTINCT b.install_id), ${binsOf(['ms'])}
      ${ROWS} WHERE ${SLICE} AND ${TYPE('capability.summary')} AND ${DIM('name')} IS NOT NULL
      GROUP BY 2, 3, 4
      ON CONFLICT (day, version, capability, owner) DO UPDATE SET ok = ok + excluded.ok, err = err + excluded.err,
        refused = refused + excluded.refused, nested = nested + excluded.nested,
        installs = installs + excluded.installs, ${BIN_ADD}`,
  },
  {
    sql: `INSERT INTO capability_transitions (day, version, from_cap, to_cap, n)
      SELECT ?1, ${VERSION}, ${DIM('from')}, ${DIM('to')}, SUM(${N})
      ${ROWS} WHERE ${SLICE} AND ${TYPE('capability.transition')} AND ${K} = 'n'
        AND ${DIM('from')} IS NOT NULL AND ${DIM('to')} IS NOT NULL
      GROUP BY 2, 3, 4
      ON CONFLICT (day, version, from_cap, to_cap) DO UPDATE SET n = n + excluded.n`,
  },
  {
    sql: `INSERT INTO modality_usage (day, version, modality, image_kind, run_format, table_kind, n, installs, distributed)
      SELECT ?1, ${VERSION}, COALESCE(${PROP('modality')}, 'none'), COALESCE(${PROP('image_kind')}, 'none'),
        COALESCE(json_extract(e.value, '$.props.bundle_formats[0]'), 'none'), COALESCE(${PROP('table_kind')}, 'none'),
        COUNT(*), COUNT(DISTINCT b.install_id), SUM(CASE WHEN ${PROP('distributed')} THEN 1 ELSE 0 END)
      ${RECORDS} WHERE ${SLICE} AND ${TYPE('dataset.opened')}
      GROUP BY 2, 3, 4, 5, 6
      ON CONFLICT (day, version, modality, image_kind, run_format, table_kind) DO UPDATE SET n = n + excluded.n,
        installs = installs + excluded.installs, distributed = distributed + excluded.distributed`,
  },
  {
    // One row per (dim, band) over a bound list of dims: a new scale dim adds no column.
    sql: `INSERT INTO data_scale_usage (day, version, dim, band, n, installs)
      SELECT ?1, ${VERSION}, dm.value, json_extract(e.value, '$.props.' || dm.value), COUNT(*), COUNT(DISTINCT b.install_id)
      ${RECORDS}, json_each(?4) dm
      WHERE ${SLICE} AND ${TYPE('dataset.opened')} AND json_extract(e.value, '$.props.' || dm.value) IS NOT NULL
      GROUP BY 2, 3, 4
      ON CONFLICT (day, version, dim, band) DO UPDATE SET n = n + excluded.n, installs = installs + excluded.installs`,
    extra: () => [JSON.stringify(SCALE_DIMS)],
  },
  ...Object.keys(PERF_DIMS).map(perfCounterStatement),
  perfRecordStatement(),
  {
    sql: `INSERT INTO error_daily (day, version, fp, n, installs)
      SELECT ?1, ${VERSION}, ${DIM('fp')}, SUM(${N}), COUNT(DISTINCT b.install_id)
      ${ROWS} WHERE ${SLICE} AND ${TYPE('error.fingerprint')} AND ${K} = 'n' AND ${DIM('fp')} IS NOT NULL
      GROUP BY 2, 3
      ON CONFLICT (day, version, fp) DO UPDATE SET n = n + excluded.n, installs = installs + excluded.installs`,
  },
  {
    // The registry keeps the earliest first_seen/first_version and the latest
    // last_seen/last_version; SET expressions all read the pre-update row.
    sql: `INSERT INTO error_fingerprints (fp, side, exception, component, route, action, file, plugin,
        first_seen, last_seen, first_version, last_version)
      SELECT ${DIM('fp')}, MAX(${DIM('where')}), MAX(${DIM('exc_type')}), MAX(${DIM('component')}), MAX(${DIM('route')}),
        MAX(${DIM('action')}), MAX(${DIM('file')}), MAX(${DIM('plugin')}), ?1, ?1, MIN(${VERSION}), MAX(${VERSION})
      ${ROWS} WHERE ${SLICE} AND ${TYPE('error.fingerprint')} AND ${K} = 'n' AND ${DIM('fp')} IS NOT NULL
      GROUP BY 1
      ON CONFLICT (fp) DO UPDATE SET
        first_version = CASE WHEN excluded.first_seen < error_fingerprints.first_seen THEN excluded.first_version ELSE error_fingerprints.first_version END,
        first_seen = MIN(error_fingerprints.first_seen, excluded.first_seen),
        last_version = CASE WHEN excluded.last_seen >= error_fingerprints.last_seen THEN excluded.last_version ELSE error_fingerprints.last_version END,
        last_seen = MAX(error_fingerprints.last_seen, excluded.last_seen),
        side = COALESCE(error_fingerprints.side, excluded.side),
        exception = COALESCE(error_fingerprints.exception, excluded.exception),
        component = COALESCE(error_fingerprints.component, excluded.component),
        route = COALESCE(error_fingerprints.route, excluded.route),
        action = COALESCE(error_fingerprints.action, excluded.action),
        file = COALESCE(error_fingerprints.file, excluded.file),
        plugin = COALESCE(error_fingerprints.plugin, excluded.plugin)`,
  },
  {
    // installs.version/mode from the day's activity; days_active once per day,
    // never again on a re-fold (last_active_day guards it).
    sql: `UPDATE installs SET
        version = COALESCE(d.version, installs.version),
        mode = COALESCE(d.mode, installs.mode),
        days_active = installs.days_active + CASE WHEN installs.last_active_day IS NULL OR installs.last_active_day < d.day THEN 1 ELSE 0 END,
        last_active_day = CASE WHEN installs.last_active_day IS NULL OR installs.last_active_day < d.day THEN d.day ELSE installs.last_active_day END
      FROM daily_installs d
      WHERE d.day = ?1 AND d.install_id > ?2 AND d.install_id <= ?3 AND d.install_id = installs.install_id`,
  },
];

/** Upper bound for the final slice: sorts after every hex id. */
export const LAST = '~';

/**
 * The install_id that ends the slice starting after `cursor`: the install of
 * the FOLD_SLICE_BATCHES-th batch row, or LAST when fewer rows remain.
 */
export async function sliceEnd(meter: Meter, day: string, cursor: string, sliceBatches: number): Promise<string> {
  const row = await meter.first<{ install_id: string }>(
    meter
      .prepare('SELECT install_id FROM batches WHERE day = ?1 AND install_id > ?2 ORDER BY install_id LIMIT 1 OFFSET ?3')
      .bind(day, cursor, Math.max(0, sliceBatches - 1)),
  );
  return row?.install_id ?? LAST;
}

export function clearStatements(meter: Meter, day: string): D1PreparedStatement[] {
  return ROLLUP_TABLES.map((table) => meter.prepare(`DELETE FROM ${table} WHERE day = ?1`).bind(day));
}

export function foldStatements(meter: Meter, day: string, lo: string, hi: string): D1PreparedStatement[] {
  return FOLD_STATEMENTS.map((statement) =>
    meter.prepare(statement.sql).bind(day, lo, hi, ...(statement.extra?.(day) ?? [])),
  );
}
