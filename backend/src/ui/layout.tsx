/** @jsxImportSource hono/jsx */
import type { Child } from 'hono/jsx';
import { raw } from 'hono/html';

import { CLIENT_JS } from './client';
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

/**
 * Every admin page. The stylesheet and script are inlined byte-for-byte as
 * the constants whose hashes the CSP pins, so nothing loads from anywhere.
 */
export function Layout(props: { title: string; active?: string; who?: string; children: Child }) {
  return (
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <meta name="robots" content="noindex" />
        <title>{`Plexora telemetry · ${props.title}`}</title>
        <style>{raw(STYLES)}</style>
      </head>
      <body>
        <header class="top">
          <span class="brand">Plexora telemetry</span>
          <nav>
            {NAV.map(([path, label]) => (
              <a href={`/admin/${path}`} class={props.active === path ? 'on' : ''}>
                {label}
              </a>
            ))}
          </nav>
          {props.who ? (
            <span class="muted">
              {props.who}{' '}
              <button type="button" data-action="/admin/logout" data-reload>
                Sign out
              </button>
            </span>
          ) : null}
        </header>
        <main>
          <h1>{props.title}</h1>
          <p id="flash" role="status"></p>
          {props.children}
        </main>
        <script>{raw(CLIENT_JS)}</script>
      </body>
    </html>
  );
}
