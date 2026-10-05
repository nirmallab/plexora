/**
 * What a licence or seat unlocks, drawn the same on the admin pages and in the
 * portal: chips for reading, one checkbox per root for editing (client.ts
 * `data-set` gathers them into the `entitlements` array).
 */
import { covers, KNOWN_ROOTS, narrowGrants, unlocks } from '../entitlements';
import { Badge, CheckField } from './components';

/** One chip per root a grant list includes, and any narrower grant as it is written; "nothing" when empty. */
export function GrantChips(props: { grants: string[] }) {
  const roots = unlocks(props.grants).filter((r) => r.granted);
  const narrow = narrowGrants(props.grants).filter((g) => !roots.some((r) => covers(r.id, g)));
  if (!roots.length && !narrow.length) return <span class="muted">application only</span>;
  return (
    <span class="chips">
      {roots.map((r) => <Badge tone="ok">{r.label}</Badge>)}
      {narrow.map((g) => <Badge><span class="mono">{g}</span></Badge>)}
    </span>
  );
}

/**
 * A checkbox per root (only those `within` covers, when given: a portal owner
 * narrows a seat, never widens it), and the narrower grants already present
 * carried through as hidden fields so a toggle never drops `ai:gating`.
 */
export function GrantChecks(props: { name: string; grants: string[]; within?: string[] }) {
  const offered = props.within ? KNOWN_ROOTS.filter((r) => props.within!.some((g) => covers(g, r.id))) : KNOWN_ROOTS;
  // A narrower grant no checked root covers (`ai:gating` on a licence without `ai`).
  const carried = narrowGrants(props.grants).filter((g) => !props.grants.some((o) => o !== g && covers(o, g)));
  return (
    <>
      {offered.map((r) => <CheckField name={props.name} value={r.id} set label={r.label} hint={r.summary}
        checked={props.grants.some((g) => covers(g, r.id))} />)}
      {carried.map((g) => <input type="hidden" name={props.name} value={g} data-set="" />)}
    </>
  );
}
