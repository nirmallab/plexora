import type { ReactNode } from 'react';
import { DocsLayout } from 'fumadocs-ui/layouts/docs';
import { baseOptions } from '@/lib/layout.shared';
import { startUrl } from '@/lib/site';
import { source } from '@/lib/source';

export default function Layout({ children }: { children: ReactNode }) {
  return (
    <DocsLayout
      tree={source.getPageTree()}
      {...baseOptions()}
      sidebar={{
        footer: (
          <a href={startUrl} className="text-sm text-fd-muted-foreground hover:text-fd-foreground">
            Sign in or sign up
          </a>
        ),
      }}
    >
      {children}
    </DocsLayout>
  );
}
