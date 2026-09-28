/** @jsxImportSource hono/jsx */
import type { Child } from 'hono/jsx';
import { raw } from 'hono/html';

import { CLIENT_JS } from './client';
import { Badge, Mark, PageHead } from './components';
import { statusTone } from './format';
import { STYLES } from './styles';

export { date } from './format';

/**
 * Every portal and admin page. The stylesheet and script are inlined
 * byte-for-byte as the constants whose hashes the CSP pins, so nothing loads
 * from anywhere else.
 *
 * `title` is the page's <h1> unless `heading` is false (the sign-in pages,
 * which draw their own); `lede` and `actions` sit beside it.
 */
export function Layout(props: {
  title: string;
  product: 'portal' | 'admin';
  nav?: [string, string][];
  active?: string;
  who?: string | null;
  logout?: string;
  lede?: Child;
  actions?: Child;
  heading?: boolean;
  /** A narrow centred column: the sign-in and trial pages. */
  center?: boolean;
  children: Child;
}) {
  const admin = props.product === 'admin';
  const home = admin ? '/admin' : '/portal';
  return (
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <meta name="color-scheme" content="light dark" />
        <meta name="robots" content="noindex, nofollow" />
        <title>{`${props.title} · Plexora`}</title>
        <style>{raw(STYLES)}</style>
      </head>
      <body>
        <header class="top">
          <div class="inner">
            <a class="brand" href={home}>
              <Mark />
              <span class="wordmark">Plexora</span>
              <span class="area">{admin ? 'Admin' : 'Licence portal'}</span>
            </a>
            <nav>
              {(props.nav ?? []).map(([href, label]) => (
                <a href={href} aria-current={props.active === href ? 'page' : undefined}>{label}</a>
              ))}
              {props.who ? <span class="who">{props.who}</span> : null}
              <button type="button" class="link theme-toggle" data-theme-toggle="" aria-label="Switch between light and dark"
                title="Switch between light and dark"><span data-theme-icon="" aria-hidden="true">◐</span></button>
              {props.who ? (
                <button type="button" class="link" data-action={props.logout ?? '/portal/logout'}
                  data-next={admin ? '/admin/login' : '/portal/login'}>Sign out</button>
              ) : null}
            </nav>
          </div>
        </header>
        <main class={props.center ? 'narrow center' : undefined}>
          <div id="flash" class="flash" role="status" hidden></div>
          {props.heading === false ? null : <PageHead title={props.title} lede={props.lede} actions={props.actions} />}
          <div id="reveal" aria-live="polite" hidden></div>
          {props.children}
        </main>
        <footer class="foot">
          <div class="inner">
            <span class="wordmark">Plexora</span>
            <span>Licensing only: no image, dataset or result ever reaches this service.</span>
          </div>
        </footer>
        <script>{raw(CLIENT_JS)}</script>
      </body>
    </html>
  );
}

export function StatusBadge(props: { status: string }) {
  return <Badge tone={statusTone(props.status)}>{props.status}</Badge>;
}
