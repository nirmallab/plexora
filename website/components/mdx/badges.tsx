import type { ReactNode } from 'react';

export function Badge({ children, tone }: { children: ReactNode; tone?: 'primary' }) {
  return <span className={tone === 'primary' ? 'px-chip px-chip-primary' : 'px-chip'}>{children}</span>;
}

/** Marks a page or section as applying to one data modality. */
export function ModalityBadge({ modality }: { modality: string }) {
  return <span className="px-chip px-chip-primary">{modality}</span>;
}

/** Marks functionality that comes from a bundled plugin (UI name). */
export function PluginBadge({ name }: { name: string }) {
  return <span className="px-chip">Plugin: {name}</span>;
}

/** Marks an option or command that is used by Plexora itself, not by people. */
export function InternalBadge() {
  return <span className="px-chip">internal</span>;
}
