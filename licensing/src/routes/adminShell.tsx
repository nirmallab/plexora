/** @jsxImportSource hono/jsx */
/** What every admin page shares: the navigation, the shell, and how a
 * history row reads. */
import type { Child } from 'hono/jsx';

import type { App } from '../http';
import { Layout } from '../ui/layout';

export const NAV: [string, string][] = [
  ['/admin', 'Dashboard'],
  ['/admin/licenses', 'Licences'],
  ['/admin/issue', 'Issue'],
  ['/admin/signals', 'Signals'],
  ['/admin/events', 'Events'],
];

export function shell(c: App, title: string, active: string, body: Child,
  extra: { lede?: Child; actions?: Child } = {}) {
  return (
    <Layout title={title} product="admin" nav={NAV} active={active}
      who={c.get('admin') === 'token' ? 'admin token' : c.get('admin')} logout="/admin/logout"
      lede={extra.lede} actions={extra.actions}>
      {body}
    </Layout>
  ) as unknown as string;
}

/** Who did it, as a person reads it. */
export function actorLabel(actor: string): string {
  if (actor === 'admin:token') return 'admin (token)';
  if (actor.startsWith('admin:')) return actor.slice(6);
  if (actor.startsWith('portal:')) return 'portal';
  return actor;
}

export function payloadText(payload: string | null): string {
  if (!payload) return '';
  return payload.length > 160 ? `${payload.slice(0, 157)}…` : payload;
}
