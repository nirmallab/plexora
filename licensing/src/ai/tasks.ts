/**
 * The AI tasks Plexora's client names on a call, by module: a copy of
 * plexora/ai/tasks.yaml (tools/ai_tasks_sync.py writes tasks.json; a pytest
 * fails when it is stale).
 *
 * A task id is `module.task` (`gating.threshold_evaluation`). Assignments
 * (ai_task_routes) are made to a task, to a whole module (`gating.*`) or to
 * everything (`*`); a call resolves the most specific level that has one
 * (levelsFor). A task that is not in the registry -- a newer client, a future
 * module -- still resolves, at its module's level and then `*`.
 */
import registry from './tasks.json';
import type { Capability } from './catalog';
import type { EffortLevel } from './effort';

export interface Requirements {
  vision: boolean;
  reasoning: boolean;
}

export interface TaskSpec {
  id: string;
  module: string;
  name: string;
  label: string;
  blurb: string;
  capability: Capability;
  max_tokens: number;
  /** The level a route set to `auto` asks for (effort.ts fits it to the model). */
  effort: EffortLevel;
  requires: Requirements;
  kinds: string[];
}

export interface ModuleSpec {
  id: string;
  label: string;
  tasks: TaskSpec[];
}

type Raw = { modules: Record<string, { label: string; tasks: Record<string, Omit<TaskSpec, 'id' | 'module' | 'name'>> }> };

export const MODULES: ModuleSpec[] = Object.entries((registry as unknown as Raw).modules).map(([module, spec]) => ({
  id: module,
  label: spec.label,
  tasks: Object.entries(spec.tasks).map(([name, t]) => ({ ...t, id: `${module}.${name}`, module, name })),
}));

export const TASKS: Record<string, TaskSpec> = Object.fromEntries(
  MODULES.flatMap((m) => m.tasks.map((t) => [t.id, t])));

/** A task id as the client sends it. */
export const WIRE_TASK = /^[a-z][a-z0-9_]{0,31}\.[a-z][a-z0-9_]{0,31}$/;
/** What an assignment may be made to: a task, `module.*`, or `*`. */
export const PATTERN = /^(\*|[a-z][a-z0-9_]{0,31}\.(\*|[a-z][a-z0-9_]{0,31}))$/;

export const moduleOf = (task: string): string => task.split('.')[0]!;

export function moduleLabel(module: string): string {
  return MODULES.find((m) => m.id === module)?.label ?? module;
}

/** "Gating › Threshold evaluation", "QC default", "All tasks". */
export function patternLabel(pattern: string): string {
  if (pattern === '*') return 'All tasks';
  const [module, name] = pattern.split('.');
  if (name === '*') return `${moduleLabel(module!)} default`;
  return `${moduleLabel(module!)} › ${TASKS[pattern]?.label ?? name}`;
}

/** What serving a call needs: the registry's task, else what its capability class implies. */
export function requirementsFor(task: string | null, capability: Capability | null): Requirements {
  const spec = task ? TASKS[task] : undefined;
  if (spec) return { ...spec.requires };
  return { vision: !!capability?.startsWith('vision_'), reasoning: capability === 'vision_judgement' ||
    capability === 'text_reasoning' };
}

/** The assignment levels a call looks at, most specific first. Without a task, its feature names the module. */
export function levelsFor(task: string | null, feature: string | null): string[] {
  if (task) return [task, `${moduleOf(task)}.*`, '*'];
  if (feature) return [`${feature}.*`, '*'];
  return ['*'];
}

/** The level of a pattern, for the admin's "inherits" view. */
export function levelOf(pattern: string): 'task' | 'module' | 'global' {
  if (pattern === '*') return 'global';
  return pattern.endsWith('.*') ? 'module' : 'task';
}

/** The pattern a task inherits from when it has no assignment of its own. */
export function parentOf(pattern: string): string | null {
  if (pattern === '*') return null;
  if (pattern.endsWith('.*')) return '*';
  return `${moduleOf(pattern)}.*`;
}


/**
 * The modules an entitlement list unlocks, for a call that names its task.
 * `ai` is everything; `ai:gating` (or `ai:gating:<anything>`) is the gating
 * module, and so on. A module the registry does not know is unlocked only by `ai`.
 */
export function modulesFor(entitlements: string[]): Set<string> | 'all' {
  if (entitlements.includes('ai')) return 'all';
  const modules = new Set<string>();
  for (const e of entitlements) {
    const match = /^ai:([a-z][a-z0-9_]*)/.exec(e);
    if (match) modules.add(match[1]!);
  }
  return modules;
}
