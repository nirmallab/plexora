import type { ReactNode } from 'react';

/** A desktop-app window around a screenshot or a clip. */
export function AppWindow({ children, title = 'Plexora', className = '' }: { children: ReactNode; title?: string; className?: string }) {
  return (
    <div className={`px-window ${className}`}>
      <div className="px-window-bar" aria-hidden="true">
        <span />
        <span />
        <span />
        <p>{title}</p>
      </div>
      <div className="px-window-body">{children}</div>
    </div>
  );
}
