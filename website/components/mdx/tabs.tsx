import type { ReactNode } from 'react';
import { Tabs } from 'fumadocs-ui/components/tabs';

// Tabs that remember the reader's choice across pages: pick macOS once and
// every PlatformTabs on the site opens on macOS.
export function PlatformTabs({ children, items }: { children: ReactNode; items?: string[] }) {
  return (
    <Tabs groupId="platform" persist items={items ?? ['macOS', 'Windows', 'Linux']}>
      {children}
    </Tabs>
  );
}

/** Local / SSH / SLURM / Open OnDemand variants of one task. */
export function WhereTabs({ children, items }: { children: ReactNode; items?: string[] }) {
  return (
    <Tabs groupId="where" persist items={items ?? ['Local', 'SSH', 'SLURM', 'Open OnDemand']}>
      {children}
    </Tabs>
  );
}
