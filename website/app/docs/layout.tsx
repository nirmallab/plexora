import type { ReactNode } from 'react';
import { DocsLayout } from 'fumadocs-ui/layouts/docs';
import { baseOptions } from '@/lib/layout.shared';
import { startUrl, trialUrl } from '@/lib/site';
import { source } from '@/lib/source';

export default function Layout({ children }: { children: ReactNode }) {
  return (
    <DocsLayout
      tree={source.getPageTree()}
      {...baseOptions()}
      sidebar={{
        footer: (
          <div className="flex flex-wrap gap-x-3 gap-y-1 text-sm text-fd-muted-foreground">
            <a href={startUrl} className="hover:text-fd-foreground">
              Sign in or sign up
            </a>
            <a href={trialUrl} className="hover:text-fd-foreground">
              Start a free trial
            </a>
          </div>
        ),
      }}
    >
      {children}
    </DocsLayout>
  );
}
