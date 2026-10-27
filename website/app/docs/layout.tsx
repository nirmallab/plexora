import type { ReactNode } from 'react';
import { DocsLayout } from 'fumadocs-ui/layouts/docs';
import { baseOptions } from '@/lib/layout.shared';
import { source } from '@/lib/source';

// The guide and the generated Reference are the two `root: true` folders in
// content/docs, so each becomes a tab with a sidebar of its own.
export default function Layout({ children }: { children: ReactNode }) {
  return (
    <DocsLayout tree={source.getPageTree()} {...baseOptions({ docs: true })} sidebar={{ defaultOpenLevel: 0 }}>
      {children}
    </DocsLayout>
  );
}
