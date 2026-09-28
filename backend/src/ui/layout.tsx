/** @jsxImportSource hono/jsx */
import type { Child } from 'hono/jsx';
import { raw } from 'hono/html';

import { CLIENT_JS } from './client';
import { Mark, PageHead } from './components';
import { STYLES } from './styles';

const NAV: [string, string][] = [
  ['usage', 'Usage'],
  ['data', 'Data'],
  ['features', 'Features'],
  ['performance', 'Performance'],
  ['errors', 'Errors'],
  ['budget', 'Budget'],
  ['backups', 'Backups'],
];

/** Who is signed in, as a person reads it. */
function whoLabel(who: string): string {
  return who === 'token' ? 'admin token' : who;
}

/**
 * Every admin page, in the licence admin's shell. The stylesheet and script
 * are inlined byte-for-byte as the constants whose hashes the CSP pins, so
 * nothing loads from anywhere.
 *
 * `title` is the page's <h1> unless `heading` is false (the sign-in page,
 * which draws its own); `lede` and `actions` sit beside it.
 */
export function Layout(props: { title: string; active?: string; who?: string; lede?: Child; actions?: Child;
  heading?: boolean; center?: boolean; children: Child }) {
  return (
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <meta name="color-scheme" content="light dark" />
        <meta name="robots" content="noindex, nofollow" />
        <title>{`${props.title} · Plexora telemetry`}</title>
        <style>{raw(STYLES)}</style>
      </head>
      <body>
        <header class="top">
          <div class="inner">
            <a class="brand" href="/admin">
              <Mark />
              <span class="wordmark">Plexora</span>
              <span class="area">Telemetry</span>
            </a>
            <nav>
              {props.who
                ? NAV.map(([path, label]) => (
                    <a href={`/admin/${path}`} aria-current={props.active === path ? 'page' : undefined}>{label}</a>
                  ))
                : null}
              {props.who ? <span class="who">{whoLabel(props.who)}</span> : null}
              <button type="button" class="link theme-toggle" data-theme-toggle="" aria-label="Switch between light and dark"
                title="Switch between light and dark"><span data-theme-icon="" aria-hidden="true">◐</span></button>
              {props.who ? (
                <button type="button" class="link" data-action="/admin/logout" data-next="/admin/login">Sign out</button>
              ) : null}
            </nav>
          </div>
        </header>
        <main class={props.center ? 'narrow center' : undefined}>
          <div id="flash" class="flash" role="status" hidden></div>
          {props.heading === false ? null : <PageHead title={props.title} lede={props.lede} actions={props.actions} />}
          {props.children}
        </main>
        <footer class="foot">
          <div class="inner">
            <span class="wordmark">Plexora</span>
            <span>Anonymous usage counts only: no file, project, marker or gene name, and no cell data, ever reaches this service.</span>
          </div>
        </footer>
        <script>{raw(CLIENT_JS)}</script>
      </body>
    </html>
  );
}
