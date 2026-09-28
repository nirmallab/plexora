/** @jsxImportSource hono/jsx */
import type { Child } from 'hono/jsx';
import { raw } from 'hono/html';

import { CLIENT_JS } from './client';
import { STYLES } from './styles';

/**
 * Every portal and admin page. The stylesheet and script are inlined
 * byte-for-byte as the constants whose hashes the CSP pins, so nothing loads
 * from anywhere else.
 */
export function Layout(props: {
  title: string;
  product: 'portal' | 'admin';
  nav?: [string, string][];
  active?: string;
  who?: string | null;
  logout?: string;
  children: Child;
}) {
  const brand = props.product === 'admin' ? 'Plexora licensing · admin' : 'Plexora licence portal';
  return (
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <meta name="robots" content="noindex" />
        <title>{`${props.title} · Plexora`}</title>
        <style>{raw(STYLES)}</style>
      </head>
      <body>
        <header class="top">
          <span class="brand">{brand}</span>
          {props.nav ? (
            <nav>
              {props.nav.map(([href, label]) => (
                <a href={href} class={props.active === href ? 'on' : ''}>{label}</a>
              ))}
            </nav>
          ) : null}
          {props.who ? (
            <span class="who muted">
              {props.who}{' '}
              <button type="button" data-action={props.logout ?? '/portal/logout'} data-next={
                props.product === 'admin' ? '/admin/login' : '/portal/login'}>Sign out</button>
            </span>
          ) : null}
        </header>
        <main>
          <h1>{props.title}</h1>
          <p id="flash" role="status"></p>
          <div id="reveal" aria-live="polite"></div>
          {props.children}
        </main>
        <script>{raw(CLIENT_JS)}</script>
      </body>
    </html>
  );
}

export function date(seconds: number | null | undefined): string {
  if (!seconds) return '—';
  return new Date(seconds * 1000).toISOString().slice(0, 10);
}

export function StatusBadge(props: { status: string }) {
  const tone = ['active', 'ok', 'paid_active'].includes(props.status) ? 'ok'
    : ['suspended', 'released', 'expired'].includes(props.status) ? 'warn' : 'bad';
  return <span class={`badge ${tone}`}>{props.status}</span>;
}
