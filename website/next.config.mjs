import { createMDX } from 'fumadocs-mdx/next';

const withMDX = createMDX();

// GitHub Pages serves a project site from /plexora/. The docs workflow sets
// NEXT_PUBLIC_BASE_PATH from actions/configure-pages, which becomes "" once a
// custom domain (plexoraapp.com) is attached, so no code changes then. Do not
// set assetPrefix: basePath already prefixes every asset Next emits.
const basePath = process.env.NEXT_PUBLIC_BASE_PATH ?? '/plexora';

/** @type {import('next').NextConfig} */
const config = {
  output: 'export',
  trailingSlash: true,
  basePath,
  env: { NEXT_PUBLIC_BASE_PATH: basePath },
  images: { unoptimized: true },
  reactStrictMode: true,
};

export default withMDX(config);
