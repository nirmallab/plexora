import { createMDX } from 'fumadocs-mdx/next';

const withMDX = createMDX();

// The site is GitHub Pages with the custom domain plexoraapp.com, served from
// the root. The docs workflow sets NEXT_PUBLIC_BASE_PATH from
// actions/configure-pages, which is "" with the custom domain and would be
// "/plexora" without it. Do not set assetPrefix: basePath already prefixes
// every asset Next emits.
const basePath = process.env.NEXT_PUBLIC_BASE_PATH ?? '';

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
