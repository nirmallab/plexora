/**
 * How hard a model thinks: one scale for the admin, each model's own on the wire.
 *
 * Models expose effort differently. Claude 5.x thinks by default and takes
 * `output_config.effort`. Claude 4.6-4.8 thinks only when asked
 * (`thinking: {type: "adaptive"}`, then effort). Haiku 4.5 and the 4.5
 * generation reject effort and take a token budget
 * (`thinking: {type: "enabled", budget_tokens}`). OpenAI and every model
 * behind OpenRouter take `reasoning: {effort}`, OpenRouter mapping it onto the
 * model. Many open-weight models take nothing.
 *
 * A route asks for a level on the canonical scale, `auto` (its task's
 * default level, tasks.yaml) or `default` (send nothing: the model's own
 * default). `resolveEffort` turns that into what one model accepts: the
 * level itself, the nearest level it has (ties go to the lower one), or
 * nothing at all. Each route of a chain is resolved on its own, so a fallback
 * model is never sent a level it would reject.
 *
 * A model's profile comes from, in order: what an admin set on the model
 * (`ai_catalog.effort_json`), what its provider's list said when it was
 * imported (stored there too), the built-in PROFILES below; failing those, a
 * model catalogued as reasoning gets low/medium/high as `reasoning.effort` on
 * the OpenAI wires (GENERIC), and any other model is sent nothing. PROFILES is the record of the major models: update
 * it when one ships, and keep `verified` honest.
 */
import type { Wire } from './providers';

export const LEVELS = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'] as const;
export type EffortLevel = (typeof LEVELS)[number];
/** What a route asks for: a level, its task's default, or the model's own default. */
export type EffortSpec = EffortLevel | 'auto' | 'default';
export const SPECS_ALLOWED: readonly string[] = [...LEVELS, 'auto', 'default'];

/**
 * How a model takes effort on the Anthropic wire (the OpenAI wires always
 * take `reasoning.effort`):
 *   effort     thinks by default; `output_config.effort` alone (Claude 5.x)
 *   adaptive   thinks only when asked: `thinking: {type: "adaptive"}` + effort;
 *              `none` sends neither (Claude 4.6-4.8)
 *   budget     `thinking: {type: "enabled", budget_tokens}`, never effort; a
 *              level whose budget is 0 sends nothing (Claude 4.5 and Haiku 4.5)
 *   reasoning  an OpenAI-style model: `reasoning.effort` on the OpenAI wires,
 *              nothing on the Anthropic wire
 *   none       takes no effort anywhere
 */
export type EffortWire = 'effort' | 'adaptive' | 'budget' | 'reasoning' | 'none';
export const EFFORT_WIRES: readonly EffortWire[] = ['effort', 'adaptive', 'budget', 'reasoning', 'none'];

export interface EffortProfile {
  /** The levels the model accepts, low to high; empty when it takes none. */
  levels: EffortLevel[];
  /** What it does when nothing is sent, when known. */
  default: EffortLevel | null;
  wire: EffortWire;
  /** `budget` models: thinking tokens per level (0: thinking off). */
  budgets?: Partial<Record<EffortLevel, number>>;
}

export interface BuiltinProfile extends EffortProfile {
  /** Matched against the approved-model id and the provider's model id, normalised (`normalise`). */
  match: RegExp;
  label: string;
  /** When the entry was last checked, and against what. */
  verified: string;
  source: string;
}

const ANTHROPIC_DOCS = 'https://docs.anthropic.com/en/docs/build-with-claude/effort';
const OPENAI_DOCS = 'https://platform.openai.com/docs/guides/reasoning';
const OPENROUTER_DOCS = 'https://openrouter.ai/docs/use-cases/reasoning-tokens';
const FULL: EffortLevel[] = ['low', 'medium', 'high', 'xhigh', 'max'];
/** Claude 4.5 generation and Haiku 4.5: `low` is no thinking, so a cheap task stays cheap. */
const BUDGETS: Partial<Record<EffortLevel, number>> = { none: 0, low: 0, medium: 4096, high: 10000, max: 24000 };

/** The major models, most specific pattern first. */
export const PROFILES: BuiltinProfile[] = [
  { match: /claude-(fable|mythos)-5/, label: 'Claude Fable / Mythos 5.x', levels: FULL, default: 'high',
    wire: 'effort', verified: '2026-10-03', source: ANTHROPIC_DOCS },
  { match: /claude-opus-5-5/, label: 'Claude Opus 5.5', levels: FULL, default: 'medium', wire: 'effort',
    verified: '2026-10-03', source: ANTHROPIC_DOCS },
  { match: /claude-opus-5/, label: 'Claude Opus 5', levels: FULL, default: 'high', wire: 'effort',
    verified: '2026-10-03', source: ANTHROPIC_DOCS },
  { match: /claude-sonnet-5/, label: 'Claude Sonnet 5 / 5.5', levels: FULL, default: 'high', wire: 'effort',
    verified: '2026-10-03', source: ANTHROPIC_DOCS },
  { match: /claude-opus-4-(7|8)/, label: 'Claude Opus 4.7 / 4.8', levels: ['none', ...FULL], default: 'none',
    wire: 'adaptive', verified: '2026-10-03', source: ANTHROPIC_DOCS },
  { match: /claude-(opus|sonnet)-4-6/, label: 'Claude Opus / Sonnet 4.6', levels: ['none', 'low', 'medium', 'high', 'max'],
    default: 'none', wire: 'adaptive', verified: '2026-10-03', source: ANTHROPIC_DOCS },
  { match: /claude-(haiku|sonnet|opus)-4/, label: 'Claude 4.5 generation and Haiku 4.5',
    levels: ['none', 'low', 'medium', 'high', 'max'], default: 'none', wire: 'budget', budgets: BUDGETS,
    verified: '2026-10-03', source: ANTHROPIC_DOCS },
  { match: /gpt-6/, label: 'OpenAI GPT-6.x', levels: FULL, default: 'medium', wire: 'reasoning',
    verified: '2026-10-03', source: OPENROUTER_DOCS },
  { match: /gpt-5-[2-9]/, label: 'OpenAI GPT-5.2 to 5.9',
    levels: ['none', 'low', 'medium', 'high', 'xhigh'], default: null, wire: 'reasoning', verified: '2026-10-03',
    source: OPENAI_DOCS },
  { match: /gpt-5-1/, label: 'OpenAI GPT-5.1', levels: ['none', 'low', 'medium', 'high'], default: 'none',
    wire: 'reasoning', verified: '2026-10-03', source: OPENAI_DOCS },
  { match: /gpt-5|(^|-)o[34](-|$)/, label: 'OpenAI GPT-5 and o-series', levels: ['minimal', 'low', 'medium', 'high'],
    default: 'medium', wire: 'reasoning', verified: '2026-10-03', source: OPENAI_DOCS },
  { match: /gemini-3/, label: 'Gemini 3.x', levels: ['low', 'high'], default: 'high', wire: 'reasoning',
    verified: '2026-10-03', source: OPENROUTER_DOCS },
  { match: /gemini-2-5/, label: 'Gemini 2.5', levels: ['low', 'medium', 'high'], default: null, wire: 'reasoning',
    verified: '2026-10-03', source: OPENROUTER_DOCS },
  { match: /grok-[34]/, label: 'Grok', levels: ['low', 'high'], default: null, wire: 'reasoning',
    verified: '2026-10-03', source: OPENROUTER_DOCS },
  // Thinking on or off only, or none at all: a level would be ignored or refused, so none is sent.
  { match: /glm-|deepseek|qwen|kimi|minimax|gemma|llama|mistral|dots/, label: 'Open-weight models (on/off or none)',
    levels: [], default: null, wire: 'none', verified: '2026-10-03', source: OPENROUTER_DOCS },
];

/** `anthropic/claude-opus-5.5` -> `claude-opus-5-5`, so one pattern matches every spelling. */
export function normalise(id: string): string {
  return (id.split('/').pop() ?? id).toLowerCase().replace(/[^a-z0-9]+/g, '-');
}

/** A model id without its date suffix (`claude-haiku-4-5-20251001` -> `claude-haiku-4-5`, `gpt-5-2025-08-07`
 * -> `gpt-5`), normalised: how a list's dated snapshot finds its price and its alias. */
export function canonicalModelId(id: string): string {
  return normalise(id).replace(/-(\d{8}|\d{4}-\d{2}-\d{2})$/, '');
}

export function builtinProfile(...ids: Array<string | null | undefined>): BuiltinProfile | null {
  for (const id of ids) {
    if (!id) continue;
    const key = normalise(id);
    const found = PROFILES.find((p) => p.match.test(key));
    if (found) return found;
  }
  return null;
}

export const isLevel = (v: unknown): v is EffortLevel => typeof v === 'string' && (LEVELS as readonly string[]).includes(v);

/** What `ai_catalog.effort_json` holds: an admin's override, or what a provider's list said. */
export interface StoredProfile extends EffortProfile {
  source: 'admin' | 'listing';
}

export function parseStored(json: string | null | undefined): StoredProfile | null {
  if (!json) return null;
  try {
    const v = JSON.parse(json);
    if (!v || typeof v !== 'object' || !Array.isArray(v.levels)) return null;
    const levels = sortLevels(v.levels.filter(isLevel));
    const wire = (EFFORT_WIRES as readonly string[]).includes(v.wire) ? v.wire as EffortWire : 'reasoning';
    return { levels, default: isLevel(v.default) ? v.default : null, wire,
      source: v.source === 'admin' ? 'admin' : 'listing',
      ...(v.budgets && typeof v.budgets === 'object' ? { budgets: v.budgets } : {}) };
  } catch {
    return null;
  }
}

export const sortLevels = (levels: EffortLevel[]) =>
  [...new Set(levels)].sort((a, b) => LEVELS.indexOf(a) - LEVELS.indexOf(b));

export type ProfileSource = 'admin' | 'listing' | 'builtin' | 'generic' | 'unknown';

/** A reasoning model nothing else describes: the three levels every reasoning API takes, OpenAI-style. */
export const GENERIC: EffortProfile = { levels: ['low', 'medium', 'high'], default: null, wire: 'reasoning' };

/** A model's effort profile and where it came from. */
export function profileFor(model: { id: string; effort_json?: string | null; reasoning?: number } | null,
  ...providerModels: Array<string | null | undefined>): { profile: EffortProfile; source: ProfileSource;
  builtin: BuiltinProfile | null } {
  const builtin = builtinProfile(model?.id, ...providerModels);
  const stored = parseStored(model?.effort_json);
  if (stored) {
    // A stored budget model without budgets of its own uses the built-in ones.
    const budgets = stored.budgets ?? (stored.wire === 'budget' ? builtin?.budgets ?? BUDGETS : undefined);
    return { profile: { ...stored, ...(budgets ? { budgets } : {}) }, source: stored.source, builtin };
  }
  if (builtin) return { profile: builtin, source: 'builtin', builtin };
  if (model?.reasoning) return { profile: GENERIC, source: 'generic', builtin: null };
  return { profile: { levels: [], default: null, wire: 'none' }, source: 'unknown', builtin: null };
}

/** The level a route sends a model, or null for nothing; `clamped_from` when it is not the level asked for. */
export interface Resolved {
  level: EffortLevel | null;
  wire: EffortWire;
  asked: EffortLevel | null;
  clamped_from?: EffortLevel;
}

/** The nearest level the model accepts; a tie goes to the lower (cheaper) one. */
export function clamp(level: EffortLevel, levels: EffortLevel[]): EffortLevel | null {
  if (!levels.length) return null;
  if (levels.includes(level)) return level;
  const at = LEVELS.indexOf(level);
  let best: EffortLevel | null = null;
  for (const candidate of levels) {
    const d = Math.abs(LEVELS.indexOf(candidate) - at);
    const bestD = best === null ? Infinity : Math.abs(LEVELS.indexOf(best) - at);
    if (d < bestD || (d === bestD && LEVELS.indexOf(candidate) < LEVELS.indexOf(best!))) best = candidate;
  }
  return best;
}

/** `spec` for one model: `auto` is the task's level, `default` (or nothing) sends nothing. */
export function resolveEffort(spec: EffortSpec | null, taskDefault: EffortLevel | null,
  profile: EffortProfile): Resolved {
  const asked = spec === 'auto' ? taskDefault : spec === null || spec === 'default' ? null : spec;
  if (asked === null) return { level: null, wire: profile.wire, asked: null };
  if (profile.wire === 'none') return { level: null, wire: 'none', asked };
  const level = clamp(asked, profile.levels);
  return { level, wire: profile.wire, asked, ...(level !== null && level !== asked ? { clamped_from: asked } : {}) };
}

/** What the request body gains for a route's resolved effort on a provider's wire. */
export function effortFields(wire: Wire, level: EffortLevel | null, how: EffortWire | undefined,
  budgets: EffortProfile['budgets'] | undefined, maxTokens: number): { output_config?: Record<string, unknown>;
  thinking?: Record<string, unknown>; reasoning?: Record<string, unknown> } {
  if (level === null || how === 'none') return {};
  // A budget model's zero-budget level is "no thinking" on every wire (OpenRouter would map it to a budget).
  if (how === 'budget' && ((budgets ?? BUDGETS)[level] ?? 0) === 0) return {};
  if (wire !== 'anthropic') return { reasoning: { effort: level } };
  switch (how ?? 'effort') {
    case 'effort':
      return level === 'none' ? {} : { output_config: { effort: level } };
    case 'adaptive':
      return level === 'none' ? {} : { thinking: { type: 'adaptive' }, output_config: { effort: level } };
    case 'budget': {
      // The budget must leave room for the answer: at least 1024 thinking tokens and 1024 more of output.
      const wanted = (budgets ?? BUDGETS)[level] ?? 0;
      const budget = Math.min(wanted, maxTokens - 1024);
      return wanted > 0 && budget >= 1024 ? { thinking: { type: 'enabled', budget_tokens: budget } } : {};
    }
    default:
      return {};
  }
}

/** A provider refusal that names the effort or thinking fields: the call is retried once without them. */
export function rejectsEffort(status: number, detail: string): boolean {
  return status === 400 && /effort|thinking|budget_tokens|reasoning/i.test(detail);
}

/** The output cap a request falls back to when a model refuses its own: what every client sent before an
 * answer could take its task's cap (16,000), and below any current model's output limit. */
export const SAFE_MAX_TOKENS = 4096;

/** Whether a provider's 400 refuses the request's output cap (a model whose output limit is below it and not
 * recorded in the catalogue as `max_output`). */
export function rejectsMaxTokens(status: number, detail: string): boolean {
  return status === 400 && /max_tokens|max_completion_tokens|max_output_tokens|output tokens|maximum.{0,40}tokens/i
    .test(detail);
}

/** "high", "xhigh → high", "not sent": what one model will receive, for the admin pages. */
export function describeResolved(r: Resolved, budgets?: EffortProfile['budgets']): string {
  if (r.asked === null) return 'model default';
  if (r.level === null) return 'not sent';
  if (r.wire === 'budget' && ((budgets ?? BUDGETS)[r.level] ?? 0) === 0) return `${r.asked}, no thinking`;
  return r.clamped_from ? `${r.clamped_from} → ${r.level}` : r.level;
}
