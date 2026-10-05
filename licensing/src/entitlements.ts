/**
 * The entitlement roots an administrator toggles, and the tiers they make.
 *
 * Mirrors the roots of plexora/licensing/manifest.py: `ai` (Plexora's own AI
 * harness, billed through the gateway) and the `mcp` add-on (Paid tools from an
 * outside agent such as Claude Code or Codex, with that agent's own model). A
 * root added there and sold is added here, and the admin pages, the portal and
 * the `unlocks=` filter follow. Unknown roots on a certificate cost nothing:
 * the client and the gateway ignore what they do not know.
 */

export interface Root { id: string; label: string; summary: string }

export const KNOWN_ROOTS: Root[] = [
  { id: 'ai', label: 'Plexora AI', summary: "Plexora's own AI: chat, guided gating and QC, on models billed here." },
  { id: 'mcp', label: 'External MCP access',
    summary: 'Paid tools from an outside agent (Claude Code, Codex, Cursor) over MCP, with its own model.' },
];

/** The tiers the issue page offers. A licence's grants are what is stored; these only fill the field. */
export const PRESETS: Array<{ id: string; label: string; entitlements: string[] }> = [
  { id: 'app', label: 'Plexora application only', entitlements: [] },
  { id: 'ai', label: 'Plexora AI harness', entitlements: ['ai'] },
  { id: 'mcp', label: 'MCP access', entitlements: ['mcp'] },
  { id: 'ai_mcp', label: 'AI harness + MCP', entitlements: ['ai', 'mcp'] },
];

/** Whether `grant` covers `required`: equal, or an ancestor of it (`ai` covers `ai:gating`). */
export function covers(grant: string, required: string): boolean {
  return grant === required || required.startsWith(`${grant}:`);
}

/** Whether every grant in `list` is covered by one in `within`: a seat can be narrowed, never widened. */
export function narrows(list: string[], within: string[]): boolean {
  return list.every((grant) => within.some((outer) => covers(outer, grant)));
}

/** One row per known root and whether `grants` include all of it. */
export function unlocks(grants: string[]): Array<Root & { granted: boolean }> {
  return KNOWN_ROOTS.map((root) => ({ ...root, granted: grants.some((g) => covers(g, root.id)) }));
}

/** A partial grant below a root (`ai:gating`), which the toggles carry through untouched. */
export function narrowGrants(grants: string[]): string[] {
  return grants.filter((g) => !KNOWN_ROOTS.some((root) => root.id === g));
}
