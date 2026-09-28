import { sourceUrl } from '@/lib/site';

/** Link to lines of a repository file at the ref these docs were built from. */
export function SourceLink({ path, start, end, children }: { path: string; start?: number; end?: number; children?: React.ReactNode }) {
  return <a href={sourceUrl(path, start, end)}>{children ?? path}</a>;
}
