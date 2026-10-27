// The one place that knows where the site lives and where it links out to.
// Components, the search client and the source links all read from here, so
// moving to plexoraapp.com is NEXT_PUBLIC_BASE_PATH="" plus public/CNAME.
import releaseInfo from '@/generated/release.json';
import versionInfo from '@/generated/version.json';

export const siteName = 'Plexora';
export const tagline = 'A free viewer for multiplexed tissue images: open an image, adjust channels, gate markers, draw regions and make figures, on your laptop, in a notebook or on a cluster.';

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

/** The latest desktop release (scripts/resolve_release.mjs). The home page
 * shows this version, not version.json's: main runs ahead of releases. */
export const release = releaseInfo;

export type Download = {
  key: keyof typeof releaseInfo.assets;
  os: 'macOS' | 'Windows' | 'Linux';
  label: string;
  requirement: string;
  name: string;
  url: string;
  size: number;
  /** The release this installer comes from: the newest one that has it. */
  version: string;
};

export const downloads: Download[] = [
  { key: 'macos_arm64', os: 'macOS', label: 'Download .dmg', requirement: 'macOS 12 or later, Apple silicon' },
  { key: 'windows_x64', os: 'Windows', label: 'Download .exe', requirement: 'Windows 10 or 11, 64-bit; installs for you only' },
  { key: 'linux_x64_deb', os: 'Linux', label: 'Download .deb', requirement: 'Ubuntu 22.04, Debian 12 or later, 64-bit' },
].map((d) => ({ ...d, ...releaseInfo.assets[d.key as Download['key']] }) as Download);

/** 487551834 -> "488 MB". */
export function formatBytes(bytes: number): string {
  return `${Math.round(bytes / 1e6)} MB`;
}

/** Plexora's demo clip, hosted with BioCognia's site (content-hashed names;
 * the docs workflow checks both still answer). */
export const demoVideo = {
  src: 'https://biocognia.com/static/plexora-3d-8c88a6219f.mp4',
  poster: 'https://biocognia.com/static/plexora-3d-6ba4177447.webp',
  width: 1120,
  height: 700,
};
