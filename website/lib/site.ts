// The one place that knows where the site lives and where it links out to.
// Components, the search client and the source links all read from here, so
// moving to plexoraapp.com is NEXT_PUBLIC_BASE_PATH="" plus public/CNAME.
import versionInfo from '@/generated/version.json';

export const siteName = 'Plexora';
export const tagline = 'See multiplexed imaging and spatial transcriptomics data at full resolution, from a laptop, a notebook or a cluster.';

/** Next.js basePath ("/plexora" on GitHub Pages, "" on a custom domain). */
export const basePath = process.env.NEXT_PUBLIC_BASE_PATH ?? '/plexora';

export const siteUrl = process.env.NEXT_PUBLIC_SITE_URL ?? 'https://nirmallab.github.io/plexora';

export const repo = 'nirmallab/plexora';
export const repoUrl = `https://github.com/${repo}`;
export const issuesUrl = `${repoUrl}/issues`;
export const pypiUrl = 'https://pypi.org/project/plexora/';

/** Where the exported search index is fetched from. Must carry basePath:
 * Fumadocs' default reads import.meta.env.BASE_URL, which only Vite sets. */
export const searchApiPath = `${basePath}/api/search`;

/** Version of the Python package these pages were generated from. */
export const version: string = versionInfo.version;
/** Git ref source links point at (a commit on deploys, a tag or main otherwise). */
export const sourceRef: string = versionInfo.source_ref;

/** Link to lines of a repository file at the ref these docs were built from. */
export function sourceUrl(path: string, start?: number, end?: number): string {
  const anchor = start ? `#L${start}${end && end !== start ? `-L${end}` : ''}` : '';
  return `${repoUrl}/blob/${sourceRef}/${path}${anchor}`;
}

/** "Edit this page" link for a content file (path relative to content/docs). */
export function editUrl(contentPath: string): string {
  return `${repoUrl}/edit/main/website/content/docs/${contentPath}`;
}

/** Prefix a public/ asset path with basePath (next/image and <img> do not). */
export function asset(path: string): string {
  return `${basePath}${path.startsWith('/') ? path : `/${path}`}`;
}
