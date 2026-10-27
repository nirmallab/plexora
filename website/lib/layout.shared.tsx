import type { BaseLayoutProps } from 'fumadocs-ui/layouts/shared';
import { Logo } from '@/components/brand';
import { repoUrl } from './site';

/** `docs`: inside the docs layout the sidebar's tab switcher already offers
 * Docs and Reference, so only Download stays as a link. */
export function baseOptions({ docs = false }: { docs?: boolean } = {}): BaseLayoutProps {
  return {
    nav: {
      title: <Logo />,
    },
    githubUrl: repoUrl,
    // Dark only, like the app.
    themeSwitch: { enabled: false },
    links: docs
      ? [{ text: 'Download', url: '/#download', active: 'none' }]
      : [
          { text: 'Download', url: '/#download', active: 'none' },
          { text: 'Docs', url: '/docs', active: 'nested-url' },
          { text: 'Plugins', url: '/docs/plugins', active: 'nested-url' },
          { text: 'Reference', url: '/docs/reference', active: 'nested-url' },
        ],
  };
}
