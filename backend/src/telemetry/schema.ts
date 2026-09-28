/**
 * The server-side allowlist: a generic interpreter of
 * `vectors/plexora-vectors.json`.
 *
 * The vectors file is generated from the Python client's `schema.py` and is
 * the single source of truth. Nothing here names an event or a field; adding
 * one on the client and regenerating the vectors is the whole change. Each
 * type spec is compiled once per isolate into a closure (regexes built once),
 * so validating a batch is a walk over the payload and nothing more.
 *
 * Rules, all deliberate:
 *   * unknown keys are never copied: a struct, a record's props, a row or a
 *     client block with an unknown key is invalid, not trimmed;
 *   * an invalid event is rejected whole and counted, the rest of the batch
 *     is still accepted; an invalid client block or envelope rejects the batch;
 *   * diagnostics-level fields are *stripped* (not rejected) when the
 *     effective level is anonymous -- that is policy, not validation.
 */
import rawVectors from '../../vectors/plexora-vectors.json';

export type Level = 'anonymous' | 'diagnostics';

export interface TypeSpec {
  t: string;
  m?: Level;
  v?: unknown[];
  re?: string;
  max?: number;
  of?: TypeSpec;
  k?: TypeSpec;
  f?: Record<string, TypeSpec>;
}

export interface KeySpec {
  agg: 'count' | 'hist';
  m: Level;
  dims: Record<string, TypeSpec>;
}

export interface EventSpec {
  kind: 'record' | 'counter';
  priority: number;
  props?: Record<string, TypeSpec>;
  keys?: Record<string, KeySpec>;
}

export interface Vectors {
  schema: number;
  hist_bins: number;
  window_pattern: string;
  caps: { max_events: number; max_rows: number; max_gzip_bytes: number };
  client: Record<string, TypeSpec>;
  reserved_license_fields: string[];
  events: Record<string, EventSpec>;
  ms_labels: string[];
  first_party: string[];
  feature_keys: string[];
  [other: string]: unknown;
}

export const VECTORS = rawVectors as unknown as Vectors;
export const SCHEMA_VERSION = VECTORS.schema;

/** Every type tag the interpreter understands; vectors.test.ts pins this. */
export const SUPPORTED_TAGS = ['enum', 'pattern', 'int', 'bool', 'hist', 'list', 'map', 'struct'] as const;

/** Largest count a counter row or int field may carry when the spec has no max. */
const MAX_COUNT = 1e12;

const INVALID: unique symbol = Symbol('invalid');
type Invalid = typeof INVALID;
type Validator = (value: unknown, level: Level) => unknown | Invalid;

function isObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function isCount(value: unknown, max = MAX_COUNT): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 && value <= max;
}

/** Compiles one type spec into a validator. Throws on an unknown tag. */
export function compile(spec: TypeSpec, histBins = VECTORS.hist_bins): Validator {
  switch (spec.t) {
    case 'enum': {
      const allowed = new Set(spec.v ?? []);
      return (value) => (typeof value === 'string' && allowed.has(value) ? value : INVALID);
    }
    case 'pattern': {
      // Anchored here, whatever the vectors say: a pattern that matched a
      // prefix would let any string through behind a valid-looking head.
      const re = new RegExp(`^(?:${spec.re ?? ''})$`);
      const max = spec.max ?? 64;
      return (value) =>
        typeof value === 'string' && value.length >= 1 && value.length <= max && re.test(value)
          ? value
          : INVALID;
    }
    case 'int': {
      const max = spec.max ?? MAX_COUNT;
      // typeof excludes booleans, which JSON.parse never turns into numbers.
      return (value) => (isCount(value, max) ? value : INVALID);
    }
    case 'bool':
      return (value) => (typeof value === 'boolean' ? value : INVALID);
    case 'hist':
      return (value) =>
        Array.isArray(value) && value.length === histBins && value.every((bin) => isCount(bin))
          ? value.slice()
          : INVALID;
    case 'list': {
      if (!spec.of) throw new Error('list spec without "of"');
      const item = compile(spec.of, histBins);
      const max = spec.max ?? 64;
      return (value, level) => {
        if (!Array.isArray(value) || value.length > max) return INVALID;
        const out: unknown[] = [];
        for (const entry of value) {
          const clean = item(entry, level);
          if (clean === INVALID) return INVALID;
          out.push(clean);
        }
        return out;
      };
    }
    case 'map': {
      if (!spec.k || !spec.v || typeof spec.v !== 'object' || Array.isArray(spec.v)) {
        throw new Error('map spec without "k"/"v"');
      }
      const key = compile(spec.k, histBins);
      const val = compile(spec.v as unknown as TypeSpec, histBins);
      const max = spec.max ?? 64;
      return (value, level) => {
        if (!isObject(value)) return INVALID;
        const entries = Object.entries(value);
        if (entries.length > max) return INVALID;
        const out: Record<string, unknown> = {};
        for (const [name, entry] of entries) {
          if (key(name, level) === INVALID) return INVALID;
          const clean = val(entry, level);
          if (clean === INVALID) return INVALID;
          out[name] = clean;
        }
        return out;
      };
    }
    case 'struct':
      return compileFields(spec.f ?? {}, histBins);
    default:
      throw new Error(`unsupported type tag ${JSON.stringify(spec.t)}`);
  }
}

/**
 * A struct: every present key must be known, every field is optional, and a
 * field whose minimum mode is diagnostics is dropped at the anonymous level.
 * Nested fields without an `m` inherit their parent's, which the caller has
 * already applied.
 */
function compileFields(fields: Record<string, TypeSpec>, histBins: number): Validator {
  const compiled = new Map<string, { check: Validator; diagnostics: boolean }>();
  for (const [name, spec] of Object.entries(fields)) {
    compiled.set(name, { check: compile(spec, histBins), diagnostics: spec.m === 'diagnostics' });
  }
  return (value, level) => {
    if (!isObject(value)) return INVALID;
    const out: Record<string, unknown> = {};
    for (const [name, entry] of Object.entries(value)) {
      const field = compiled.get(name);
      if (!field) return INVALID;
      const clean = field.check(entry, level);
      if (clean === INVALID) return INVALID;
      if (field.diagnostics && level === 'anonymous') continue;
      out[name] = clean;
    }
    return out;
  };
}

// ---------------------------------------------------------------------------
// Compiled vectors
// ---------------------------------------------------------------------------

interface CompiledKey {
  agg: 'count' | 'hist';
  diagnostics: boolean;
  dims: Map<string, { check: Validator; diagnostics: boolean }>;
}

interface CompiledEvent {
  kind: 'record' | 'counter';
  priority: number;
  props?: Validator;
  keys?: Map<string, CompiledKey>;
}

interface Compiled {
  vectors: Vectors;
  window: RegExp;
  client: Validator;
  events: Map<string, CompiledEvent>;
  hist: Validator;
}

let compiled: Compiled | null = null;

/** Compiles a vectors document. Exported so tests can compile a variant. */
export function compileVectors(vectors: Vectors): Compiled {
  const events = new Map<string, CompiledEvent>();
  for (const [type, spec] of Object.entries(vectors.events)) {
    if (spec.kind === 'record') {
      events.set(type, {
        kind: 'record',
        priority: spec.priority,
        props: compileFields(spec.props ?? {}, vectors.hist_bins),
      });
    } else if (spec.kind === 'counter') {
      const keys = new Map<string, CompiledKey>();
      for (const [key, keySpec] of Object.entries(spec.keys ?? {})) {
        const dims = new Map<string, { check: Validator; diagnostics: boolean }>();
        for (const [dim, dimSpec] of Object.entries(keySpec.dims ?? {})) {
          dims.set(dim, {
            check: compile(dimSpec, vectors.hist_bins),
            diagnostics: dimSpec.m === 'diagnostics',
          });
        }
        if (keySpec.agg !== 'count' && keySpec.agg !== 'hist') {
          throw new Error(`unsupported agg ${JSON.stringify(keySpec.agg)} on ${type}.${key}`);
        }
        keys.set(key, { agg: keySpec.agg, diagnostics: keySpec.m === 'diagnostics', dims });
      }
      events.set(type, { kind: 'counter', priority: spec.priority, keys });
    } else {
      throw new Error(`unsupported event kind ${JSON.stringify(spec.kind)} on ${type}`);
    }
  }
  return {
    vectors,
    window: new RegExp(`^(?:${vectors.window_pattern})$`),
    client: compileFields(vectors.client, vectors.hist_bins),
    events,
    hist: compile({ t: 'hist' }, vectors.hist_bins),
  };
}

function vectorsCompiled(): Compiled {
  compiled ??= compileVectors(VECTORS);
  return compiled;
}

// ---------------------------------------------------------------------------
// Batches
// ---------------------------------------------------------------------------

export interface CleanClient {
  install_id: string;
  session_id?: string;
  plexora_version?: string;
  python?: string;
  os?: string;
  arch?: string;
  launch_mode?: string;
  deployment?: string;
  scheduler?: string;
  install_kind?: string;
  mode?: Level;
  [field: string]: unknown;
}

export interface CleanRow {
  k: string;
  d: Record<string, string | number | boolean>;
  n: number;
  h?: number[];
  s?: number;
  mx?: number;
}

export interface CleanEvent {
  type: string;
  window: string;
  props?: Record<string, unknown>;
  rows?: CleanRow[];
}

export interface CleanBatch {
  schema: number;
  batch_id: string;
  client: CleanClient;
  events: CleanEvent[];
  /** Events that failed validation (unknown type included). */
  rejected: number;
  /** Events removed by the level or priority policy, never invalid ones. */
  dropped: number;
}

export type CleanResult = { ok: true; batch: CleanBatch } | { ok: false; error: string };

export interface CleanOptions {
  /** The server ceiling; the client's own `mode` can only lower it. */
  levelMax?: Level;
  /** Events whose priority number is above this are dropped (budget states). */
  maxPriority?: number;
  maxEvents?: number;
}

const BATCH_KEYS = new Set(['schema', 'batch_id', 'client', 'events']);
const RECORD_KEYS = new Set(['type', 'window', 'props']);
const COUNTER_KEYS = new Set(['type', 'window', 'rows']);
const ROW_KEYS = new Set(['k', 'd', 'n', 'h', 's', 'mx']);

/**
 * The client block alone. Used by /register (which sends it without
 * install_id and session_id) as well as by cleanBatch.
 */
export function cleanClient(raw: unknown): CleanClient | Omit<CleanClient, 'install_id'> | null {
  const clean = vectorsCompiled().client(raw, 'diagnostics');
  return clean === INVALID ? null : (clean as CleanClient);
}

export function priorityOf(type: string): number {
  return vectorsCompiled().events.get(type)?.priority ?? 99;
}

export function cleanBatch(raw: unknown, options: CleanOptions = {}): CleanResult {
  const c = vectorsCompiled();
  if (!isObject(raw)) return { ok: false, error: 'Expected a JSON object.' };
  for (const key of Object.keys(raw)) {
    if (!BATCH_KEYS.has(key)) return { ok: false, error: `Unknown batch key ${key}.` };
  }
  if (raw.schema !== c.vectors.schema) {
    return { ok: false, error: `Expected schema ${c.vectors.schema}.` };
  }
  if (typeof raw.batch_id !== 'string' || !/^[0-9a-f]{32}$/.test(raw.batch_id)) {
    return { ok: false, error: 'batch_id must be 32 lowercase hex characters.' };
  }
  const client = c.client(raw.client, 'diagnostics');
  if (client === INVALID || typeof (client as CleanClient).install_id !== 'string') {
    return { ok: false, error: 'The client block does not match the schema.' };
  }
  const cleanedClient = client as CleanClient;
  const maxEvents = Math.min(options.maxEvents ?? Infinity, c.vectors.caps.max_events);
  if (!Array.isArray(raw.events) || raw.events.length > maxEvents) {
    return { ok: false, error: `events must be an array of at most ${maxEvents}.` };
  }

  // The client's own mode can only lower the server's ceiling: an anonymous
  // client that sent a diagnostics field by mistake has it stripped here, and
  // a block that does not say is treated as anonymous.
  const level: Level =
    options.levelMax !== 'anonymous' && cleanedClient.mode === 'diagnostics' ? 'diagnostics' : 'anonymous';
  const maxPriority = options.maxPriority ?? Infinity;

  const events: CleanEvent[] = [];
  let rejected = 0;
  let dropped = 0;
  for (const entry of raw.events) {
    const event = cleanEvent(c, entry, level);
    if (event === INVALID) {
      rejected += 1;
    } else if (event === null || priorityOf(event.type) > maxPriority) {
      dropped += 1;
    } else {
      events.push(event);
    }
  }
  return {
    ok: true,
    batch: {
      schema: c.vectors.schema,
      batch_id: raw.batch_id,
      client: cleanedClient,
      events,
      rejected,
      dropped,
    },
  };
}

/** A clean event, INVALID, or null when the level policy emptied it. */
function cleanEvent(c: Compiled, raw: unknown, level: Level): CleanEvent | Invalid | null {
  if (!isObject(raw) || typeof raw.type !== 'string') return INVALID;
  const spec = c.events.get(raw.type);
  if (!spec) return INVALID;
  if (typeof raw.window !== 'string' || !c.window.test(raw.window)) return INVALID;

  if (spec.kind === 'record') {
    for (const key of Object.keys(raw)) if (!RECORD_KEYS.has(key)) return INVALID;
    const props = spec.props!(raw.props, level);
    if (props === INVALID) return INVALID;
    return { type: raw.type, window: raw.window, props: props as Record<string, unknown> };
  }

  for (const key of Object.keys(raw)) if (!COUNTER_KEYS.has(key)) return INVALID;
  const rows = raw.rows;
  if (!Array.isArray(rows) || rows.length === 0 || rows.length > c.vectors.caps.max_rows) {
    return INVALID;
  }
  const out: CleanRow[] = [];
  for (const row of rows) {
    const clean = cleanRow(c, spec.keys!, row, level);
    if (clean === INVALID) return INVALID;
    if (clean !== null) out.push(clean);
  }
  if (out.length === 0) return null;
  return { type: raw.type, window: raw.window, rows: out };
}

function cleanRow(
  c: Compiled,
  keys: Map<string, CompiledKey>,
  raw: unknown,
  level: Level,
): CleanRow | Invalid | null {
  if (!isObject(raw) || typeof raw.k !== 'string') return INVALID;
  for (const key of Object.keys(raw)) if (!ROW_KEYS.has(key)) return INVALID;
  const key = keys.get(raw.k);
  if (!key) return INVALID;
  if (!isCount(raw.n)) return INVALID;

  const dims: Record<string, string | number | boolean> = {};
  if (raw.d !== undefined) {
    if (!isObject(raw.d)) return INVALID;
    for (const [name, value] of Object.entries(raw.d)) {
      const dim = key.dims.get(name);
      if (!dim) return INVALID;
      const clean = dim.check(value, level);
      if (clean === INVALID) return INVALID;
      if (dim.diagnostics && level === 'anonymous') continue;
      dims[name] = clean as string | number | boolean;
    }
  }

  const row: CleanRow = { k: raw.k, d: dims, n: raw.n };
  const histFields = raw.h !== undefined || raw.s !== undefined || raw.mx !== undefined;
  if (key.agg === 'count') {
    if (histFields) return INVALID;
  } else {
    if (raw.h !== undefined) {
      const h = c.hist(raw.h, level);
      if (h === INVALID) return INVALID;
      row.h = h as number[];
    }
    for (const field of ['s', 'mx'] as const) {
      const value = raw[field];
      if (value === undefined) continue;
      if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return INVALID;
      row[field] = value;
    }
  }
  // Validated first, dropped second: an invalid diagnostics row still rejects.
  if (key.diagnostics && level === 'anonymous') return null;
  return row;
}

/** Sum of error.fingerprint counts, for the `batches.errors` column. */
export function errorCount(events: CleanEvent[]): number {
  let total = 0;
  for (const event of events) {
    if (event.type !== 'error.fingerprint') continue;
    for (const row of event.rows ?? []) total += row.n;
  }
  return total;
}
