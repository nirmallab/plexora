/** A repository file named in the docs: shown as its path, not linked (the
 * site links to no code host). start/end are kept so existing pages compile. */
export function SourceLink({ path, children }: { path: string; start?: number; end?: number; children?: React.ReactNode }) {
  return <code>{children ?? path}</code>;
}
