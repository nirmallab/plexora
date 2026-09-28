import type { BaseLayoutProps } from 'fumadocs-ui/layouts/shared';
import { repoUrl, siteName } from './site';

export function Logo() {
  return (
    <span className="inline-flex items-center gap-2 font-semibold">
      <svg width="20" height="20" viewBox="0 0 24 24" aria-hidden="true">
        <circle cx="12" cy="12" r="10" fill="none" stroke="currentColor" strokeWidth="2" />
        <circle cx="9" cy="10" r="2.2" fill="var(--color-fd-primary)" />
        <circle cx="15" cy="9" r="1.6" fill="currentColor" opacity="0.7" />
        <circle cx="13.5" cy="15" r="2" fill="var(--color-fd-primary)" opacity="0.8" />
      </svg>
      {siteName}
    </span>
  );
}

export function baseOptions(): BaseLayoutProps {
  return {
    nav: {
      title: <Logo />,
    },
    githubUrl: repoUrl,
    links: [
      { text: 'Documentation', url: '/docs', active: 'nested-url' },
      { text: 'Python API', url: '/docs/python-api', active: 'nested-url' },
      { text: 'Build a plugin', url: '/docs/plugin-development', active: 'nested-url' },
    ],
  };
}
