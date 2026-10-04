/**
 * The one stylesheet for the portal and admin pages, inlined and pinned by CSP
 * hash (so no style="" attribute works anywhere: every look is a class here).
 *
 * The layout and components follow the SCIMAP Pro admin; the colours are
 * Plexora's own, from DESIGN.md. Dark is the application's chrome -- Void,
 * Panel, Inset and Signal Cyan. Light is the Figure Builder "desk" rebinding,
 * where the accent becomes Desk Signal blue. The page follows the system; a
 * `light` or `dark` class on <html>, set by the theme toggle, overrides it.
 * System fonts only, as everywhere in Plexora: nothing is fetched.
 */
export const STYLES = `
:root {
  color-scheme: light dark;
  --canvas: #f4f5f8;
  --canvas-sunk: #eceef3;
  --surface: #ffffff;
  --surface-2: #f6f7fa;
  --surface-3: #ffffff;
  --ink: #16202e;
  --ink-muted: #5b6679;
  --ink-faint: #7b8699;
  --line: rgba(22, 32, 46, 0.12);
  --line-strong: rgba(22, 32, 46, 0.22);
  --accent: #2563eb;
  --accent-hover: #1d4ed8;
  --accent-ink: #ffffff;
  --accent-soft: #e6eefd;
  --accent-line: rgba(37, 99, 235, 0.36);
  --ok: #15803d;
  --ok-soft: #e4f5ea;
  --warn: #a16207;
  --warn-soft: #fbf1d9;
  --bad: #d64545;
  --bad-soft: #fcebeb;
  --shadow-low: 0 1px 2px rgba(22, 32, 46, 0.06), 0 10px 28px -24px rgba(22, 32, 46, 0.28);
  --shadow-medium: 0 2px 5px rgba(22, 32, 46, 0.08), 0 22px 48px -32px rgba(22, 32, 46, 0.32);
  --radius: 10px;
  --shell: 1280px;
  --gutter: clamp(16px, 2.6vw, 40px);
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
}

.dark {
  color-scheme: dark;
  --canvas: #05070a;
  --canvas-sunk: #030406;
  --surface: #0b0f15;
  --surface-2: #121820;
  --surface-3: #121820;
  --ink: #f8fafc;
  --ink-muted: #8ea0b8;
  --ink-faint: #6d7c91;
  --line: rgba(255, 255, 255, 0.12);
  --line-strong: rgba(255, 255, 255, 0.22);
  --accent: #38bdf8;
  --accent-hover: #5cc9fb;
  --accent-ink: #05070a;
  --accent-soft: rgba(56, 189, 248, 0.12);
  --accent-line: rgba(56, 189, 248, 0.36);
  --ok: #34d399;
  --ok-soft: rgba(52, 211, 153, 0.14);
  --warn: #f3b845;
  --warn-soft: rgba(243, 184, 69, 0.14);
  --bad: #f87171;
  --bad-soft: rgba(248, 113, 113, 0.14);
  --shadow-low: 0 1px 2px rgba(0, 0, 0, 0.3), 0 10px 28px -24px rgba(0, 0, 0, 0.7);
  --shadow-medium: 0 2px 5px rgba(0, 0, 0, 0.28), 0 22px 48px -32px rgba(0, 0, 0, 0.82);
}

@media (prefers-color-scheme: dark) {
  :root:not(.light) {
    color-scheme: dark;
    --canvas: #05070a;
    --canvas-sunk: #030406;
    --surface: #0b0f15;
    --surface-2: #121820;
    --surface-3: #121820;
    --ink: #f8fafc;
    --ink-muted: #8ea0b8;
    --ink-faint: #6d7c91;
    --line: rgba(255, 255, 255, 0.12);
    --line-strong: rgba(255, 255, 255, 0.22);
    --accent: #38bdf8;
    --accent-hover: #5cc9fb;
    --accent-ink: #05070a;
    --accent-soft: rgba(56, 189, 248, 0.12);
    --accent-line: rgba(56, 189, 248, 0.36);
    --ok: #34d399;
    --ok-soft: rgba(52, 211, 153, 0.14);
    --warn: #f3b845;
    --warn-soft: rgba(243, 184, 69, 0.14);
    --bad: #f87171;
    --bad-soft: rgba(248, 113, 113, 0.14);
    --shadow-low: 0 1px 2px rgba(0, 0, 0, 0.3), 0 10px 28px -24px rgba(0, 0, 0, 0.7);
    --shadow-medium: 0 2px 5px rgba(0, 0, 0, 0.28), 0 22px 48px -32px rgba(0, 0, 0, 0.82);
  }
}

* { box-sizing: border-box; }
[hidden] { display: none !important; }

body {
  margin: 0;
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  font: 14px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", "Noto Sans", "Liberation Sans", Arial, sans-serif;
  -webkit-font-smoothing: antialiased;
  color: var(--ink);
  background:
    radial-gradient(64rem 34rem at 8% -16rem, color-mix(in srgb, var(--accent) 9%, transparent), transparent 70%),
    var(--canvas);
}

code, pre, .mono { font-family: var(--mono); }
code { font-size: 0.92em; overflow-wrap: anywhere; }
a { color: var(--accent); text-underline-offset: 2px; }
a:hover { color: var(--accent-hover); }

/* -- the top bar ------------------------------------------------------------ */

.top {
  position: sticky;
  top: 0;
  z-index: 40;
  background: linear-gradient(90deg,
    color-mix(in srgb, var(--surface) 92%, var(--accent) 3%),
    color-mix(in srgb, var(--surface) 90%, var(--accent-soft) 10%));
  border-bottom: 1px solid var(--line);
  box-shadow: var(--shadow-low);
  backdrop-filter: blur(18px) saturate(1.15);
}
.top .inner {
  max-width: var(--shell);
  margin: 0 auto;
  min-height: 64px;
  padding: 0 var(--gutter);
  display: flex;
  align-items: center;
  gap: 8px 24px;
  flex-wrap: wrap;
}
.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  color: var(--ink);
  text-decoration: none;
  min-height: 44px;
}
.brand:hover { color: var(--ink); }
.mark { width: 26px; height: 26px; flex: none; color: var(--ink); }
.mark .ring { fill: none; stroke: currentColor; stroke-width: 2; }
.mark .cell { fill: var(--accent); }
.mark .cell.dim { fill: currentColor; opacity: 0.7; }
.mark .cell.soft { opacity: 0.8; }
.wordmark { font-size: 1.3rem; font-weight: 760; line-height: 1.1; letter-spacing: 0; }
.brand .area {
  margin-left: 2px;
  font-size: 11px;
  font-weight: 700;
  color: var(--ink-muted);
  text-transform: uppercase;
  letter-spacing: .09em;
}
.top nav {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 4px;
  flex-wrap: wrap;
}
.top nav a, .top nav button.link {
  display: inline-flex;
  align-items: center;
  min-height: 38px;
  padding: 7px 13px;
  border-radius: 10px;
  font-size: 14px;
  font-weight: 650;
  color: color-mix(in srgb, var(--ink) 74%, var(--ink-muted));
  text-decoration: none;
  border: 0;
  background: none;
  cursor: pointer;
  font-family: inherit;
}
.top nav a:hover, .top nav button.link:hover, .top nav a[aria-current] {
  background: color-mix(in srgb, var(--accent-soft) 70%, var(--surface));
  color: var(--accent);
  filter: none;
}
.top nav .who { font-size: 13px; color: var(--ink-muted); padding: 6px 4px 6px 10px; }
.top nav .theme-toggle { padding: 6px 9px; font-size: 16px; line-height: 1; }

/* -- the page --------------------------------------------------------------- */

main {
  flex: 1;
  width: 100%;
  max-width: var(--shell);
  margin: 0 auto;
  padding: 28px var(--gutter) 64px;
}
main.narrow { max-width: 560px; }
main.center { display: grid; align-content: center; padding-top: clamp(40px, 9vh, 96px); }

.page-head { display: flex; flex-wrap: wrap; align-items: flex-end; gap: 8px 20px; margin-bottom: 18px; }
.page-head .grow { flex: 1; min-width: 260px; }
.page-head .actions { margin-left: auto; }
h1 { font-size: clamp(1.4rem, 2.6vw, 1.65rem); line-height: 1.2; font-weight: 740; margin: 0 0 6px; }
h2 { font-size: 1.02rem; margin: 0 0 4px; font-weight: 700; }
h3 { font-size: 12px; margin: 0 0 8px; text-transform: uppercase; letter-spacing: .06em; color: var(--ink-muted); font-weight: 700; }
p { margin: 0 0 12px; }
.lede { color: color-mix(in srgb, var(--ink) 70%, var(--ink-muted)); margin: 0; font-size: 1.05rem; }
.muted { color: var(--ink-muted); }
.small { font-size: 13px; }
.tiny-text { font-size: 12px; }
.right { text-align: right; }
.nowrap { white-space: nowrap; }
.ok-text { color: var(--ok); }
.bad-text { color: var(--bad); }

/* -- cards and sections ----------------------------------------------------- */

.card {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  box-shadow: var(--shadow-low);
  padding: 18px;
  margin: 0 0 16px;
}
.card > header {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}
.card > header h2 { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.card > header .grow { flex: 1; min-width: 200px; }
.card > header .sub { color: var(--ink-muted); font-size: 13px; margin-top: 2px; }
.card.feature {
  border-color: color-mix(in srgb, var(--line) 70%, var(--accent));
  background:
    radial-gradient(circle at 10% -30%, color-mix(in srgb, var(--accent) 10%, transparent), transparent 42%),
    var(--surface);
}

.section { border-top: 1px solid var(--line); padding-top: 16px; margin-top: 16px; }
.grid { display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); }
.grid > .card { margin: 0; }
.stack-gap { margin-bottom: 16px; }

details.section > summary {
  cursor: pointer;
  font-size: 14px;
  font-weight: 600;
  list-style: none;
}
details.section > summary::-webkit-details-marker { display: none; }
details.section > summary::before { content: "▸ "; color: var(--ink-muted); }
details.section[open] > summary::before { content: "▾ "; }
details.section > summary:hover { color: var(--accent); }
details.section > *:not(summary) { margin-top: 14px; }
.card > header + details.section { border-top: 0; padding-top: 0; margin-top: 0; }

/* -- numbers ---------------------------------------------------------------- */

.stats { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); margin-bottom: 16px; }
.stat {
  background: color-mix(in srgb, var(--surface) 70%, var(--canvas-sunk));
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 14px 16px;
  min-width: 0;
}
.stat .n { font-size: 24px; font-weight: 700; letter-spacing: -.01em; display: block; line-height: 1.25;
  font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.stat .label { font-size: 11px; font-weight: 700; color: var(--ink-muted); text-transform: uppercase; letter-spacing: .06em; }
.stat .sub { font-size: 12px; color: var(--ink-muted); }
.card .stats:last-child { margin-bottom: 0; }

svg.bars { display: block; width: 100%; height: 72px; margin: 6px 0 4px; }
svg.bars rect { fill: var(--accent-soft); }
svg.bars rect.on { fill: var(--accent); }
.axis { display: flex; justify-content: space-between; font-size: 12px; color: var(--ink-muted); }

.summary-strip { display: flex; flex-wrap: wrap; gap: 4px 18px; align-items: baseline; font-size: 13px; color: var(--ink-muted); }
.summary-strip b { color: var(--ink); font-size: 15px; font-weight: 700; }

/* -- badges ----------------------------------------------------------------- */

.badge {
  display: inline-block;
  font-size: 12px;
  font-weight: 600;
  line-height: 1.5;
  padding: 1px 9px;
  border-radius: 999px;
  background: var(--surface-2);
  color: var(--ink-muted);
  border: 1px solid var(--line);
  white-space: nowrap;
  vertical-align: middle;
}
.badge.ok { background: var(--ok-soft); color: var(--ok); border-color: transparent; }
.badge.warn { background: var(--warn-soft); color: var(--warn); border-color: transparent; }
.badge.bad { background: var(--bad-soft); color: var(--bad); border-color: transparent; }
.badge.accent { background: var(--accent-soft); color: var(--accent); border-color: transparent; }

/* -- tables ----------------------------------------------------------------- */

.scroll { overflow-x: auto; margin: 0 -4px; padding: 0 4px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; font-variant-numeric: tabular-nums; }
th {
  text-align: left;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: .05em;
  color: var(--ink-muted);
  font-weight: 700;
  padding: 0 12px 8px 0;
  border-bottom: 1px solid var(--line);
  white-space: nowrap;
}
td { padding: 9px 12px 9px 0; border-bottom: 1px solid var(--line); vertical-align: middle; }
tr:last-child td { border-bottom: 0; }
tbody tr:hover { background: color-mix(in srgb, var(--surface-2) 70%, transparent); }
td.right, th.right { text-align: right; }
td:last-child, th:last-child { padding-right: 0; }
/* A table cell, not the flex row .actions is elsewhere. */
td.actions { display: table-cell; white-space: nowrap; text-align: right; }
td .sub { font-size: 12px; color: var(--ink-muted); }
table.kv td:first-child { width: 190px; color: var(--ink-muted); }
.empty { color: var(--ink-muted); font-size: 14px; padding: 18px 0; text-align: center; }

/* -- forms ------------------------------------------------------------------ */

.field { margin: 0 0 14px; min-width: 0; }
.field label, .field .label { display: block; font-size: 13px; font-weight: 600; margin-bottom: 5px; }
.hint { font-size: 12px; color: var(--ink-muted); margin-top: 5px; }
input[type=text], input[type=email], input[type=number], input[type=password], input[type=date], input:not([type]),
select, textarea {
  width: 100%;
  padding: 8px 11px;
  font: inherit;
  font-size: 14px;
  color: var(--ink);
  background: var(--surface-3);
  border: 1px solid var(--line-strong);
  border-radius: 8px;
}
textarea { resize: vertical; min-height: 76px; }
input:focus, select:focus, textarea:focus {
  outline: 2px solid var(--accent-line);
  outline-offset: 0;
  border-color: var(--accent);
}
input[type=file] { font-size: 13px; color: var(--ink-muted); max-width: 100%; }
.row { display: flex; gap: 12px; flex-wrap: wrap; align-items: flex-end; }
.row > * { flex: 1; min-width: 150px; }
.row > button, .row > .actions { flex: none; min-width: 0; }
.check { display: flex; align-items: flex-start; gap: 8px; font-size: 14px; margin: 0 0 12px; font-weight: 400; }
.check input { margin-top: 4px; }
.form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 14px; }
.form-grid.three { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.form-grid .wide { grid-column: 1 / -1; }
@media (max-width: 620px) { .form-grid, .form-grid.three { grid-template-columns: 1fr; } }
.inline-form { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.inline-form input, .inline-form select { width: auto; min-width: 160px; padding: 5px 9px; font-size: 13px; }
.inline-form button { padding: 5px 12px; font-size: 13px; }
.filters { display: flex; flex-wrap: wrap; align-items: flex-end; gap: 10px; }
.filters .field { margin: 0; flex: 1; min-width: 150px; }
.filters .field.grow { flex: 3; min-width: 220px; }

/* -- buttons ---------------------------------------------------------------- */

button, .button {
  font: inherit;
  font-size: 14px;
  font-weight: 600;
  padding: 8px 16px;
  border-radius: 8px;
  border: 1px solid var(--accent);
  background: var(--accent);
  color: var(--accent-ink);
  cursor: pointer;
  text-decoration: none;
  display: inline-block;
  line-height: 1.4;
}
button:hover, .button:hover { background: var(--accent-hover); border-color: var(--accent-hover); color: var(--accent-ink); }
button:disabled {
  color: var(--ink-faint);
  background: var(--surface-2);
  border-color: var(--line);
  cursor: not-allowed;
}
button.ghost, .button.ghost { background: transparent; color: var(--ink); border-color: var(--line-strong); }
button.ghost:hover, .button.ghost:hover { background: var(--surface-2); color: var(--ink); border-color: var(--line-strong); }
button.danger { background: transparent; color: var(--bad); border-color: var(--bad); }
button.danger:hover { background: var(--bad-soft); color: var(--bad); border-color: var(--bad); }
button.tiny, .button.tiny { padding: 3px 10px; font-size: 13px; }
button.wide { width: 100%; }
button.link { background: none; border: 0; padding: 0; color: var(--accent); font-weight: 600; }
button.link:hover { background: none; color: var(--accent-hover); text-decoration: underline; }
.actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
form > .actions { margin-top: 4px; }
.inline-form > .actions { margin-top: 0; }

/* -- messages --------------------------------------------------------------- */

.flash {
  position: sticky;
  top: 72px;
  z-index: 30;
  margin: 0 0 16px;
  padding: 10px 14px;
  border-radius: 8px;
  font-size: 14px;
  background: var(--ok-soft);
  color: var(--ok);
  border: 1px solid currentColor;
}
.flash.bad { background: var(--bad-soft); color: var(--bad); }

.note {
  background: var(--surface-2);
  border-left: 3px solid var(--accent);
  border-radius: 0 8px 8px 0;
  padding: 9px 13px;
  font-size: 13px;
  margin: 0 0 14px;
}
.note.warn { border-left-color: var(--warn); background: var(--warn-soft); color: var(--ink); }
.meter-row { display: flex; align-items: center; gap: 8px; min-width: 120px; }
meter.capacity { flex: 1; min-width: 80px; height: 8px; border: 0; border-radius: 999px; background: var(--surface-2);
  -webkit-appearance: none; appearance: none; }
meter.capacity::-webkit-meter-bar { background: var(--surface-2); border: 0; border-radius: 999px; height: 8px; }
meter.capacity::-webkit-meter-optimum-value { background: var(--ok); border-radius: 999px; }
meter.capacity::-webkit-meter-suboptimum-value { background: var(--warn); border-radius: 999px; }
meter.capacity::-webkit-meter-even-less-good-value { background: var(--bad); border-radius: 999px; }
meter.capacity:-moz-meter-optimum::-moz-meter-bar { background: var(--ok); border-radius: 999px; }
meter.capacity:-moz-meter-sub-optimum::-moz-meter-bar { background: var(--warn); border-radius: 999px; }
meter.capacity:-moz-meter-sub-sub-optimum::-moz-meter-bar { background: var(--bad); border-radius: 999px; }

.secret {
  background: var(--surface-2);
  border: 1px dashed var(--accent);
  border-radius: 8px;
  padding: 14px;
  margin: 14px 0;
}
.secret > strong { display: block; margin-bottom: 8px; }
.secret .secret-row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; margin: 6px 0; }
.secret .secret-row .k { font-size: 12px; color: var(--ink-muted); min-width: 90px; }
.secret code {
  font-size: 15px;
  word-break: break-all;
  user-select: all;
  padding: 3px 8px;
  border-radius: 6px;
  background: var(--surface);
  border: 1px solid var(--line);
}

.keyline { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.keyline code {
  padding: 3px 8px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--surface-2);
  letter-spacing: .04em;
}

/* A per-row menu, so a table row carries several actions without becoming
   several buttons wide. */
.rowmenu { position: relative; display: inline-block; text-align: left; }
.rowmenu > summary {
  list-style: none;
  cursor: pointer;
  padding: 2px 9px;
  border-radius: 6px;
  color: var(--ink-muted);
  font-weight: 700;
  letter-spacing: .08em;
}
.rowmenu > summary::-webkit-details-marker { display: none; }
.rowmenu > summary:hover, .rowmenu[open] > summary { background: var(--surface-2); color: var(--ink); }
.rowmenu > div {
  position: absolute;
  right: 0;
  z-index: 20;
  display: grid;
  gap: 4px;
  min-width: 190px;
  padding: 6px;
  border: 1px solid var(--line-strong);
  border-radius: var(--radius);
  background: var(--surface);
  box-shadow: var(--shadow-medium);
}
.rowmenu > div button { width: 100%; text-align: left; }

/* -- section tabs, toolbars, facts (the AI admin) ---------------------------- */

.subnav {
  display: flex;
  gap: 2px;
  margin: -6px 0 20px;
  border-bottom: 1px solid var(--line);
  overflow-x: auto;
  scrollbar-width: none;
}
.subnav a {
  padding: 8px 12px 9px;
  font-size: 13px;
  font-weight: 650;
  color: var(--ink-muted);
  text-decoration: none;
  white-space: nowrap;
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
}
.subnav a:hover { color: var(--ink); }
.subnav a[aria-current] { color: var(--ink); border-bottom-color: var(--accent); }

/* The AI admin's workflow: numbered steps with a status dot each, the quiet pages aside. */
.steps {
  display: flex;
  align-items: stretch;
  gap: 2px;
  margin: -6px 0 16px;
  border-bottom: 1px solid var(--line);
  overflow-x: auto;
  scrollbar-width: none;
}
.steps a {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 7px 12px 8px;
  font-size: 13px;
  font-weight: 650;
  color: var(--ink-muted);
  text-decoration: none;
  white-space: nowrap;
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
}
.steps a:hover { color: var(--ink); }
.steps a[aria-current] { color: var(--ink); border-bottom-color: var(--accent); }
.steps .n {
  display: inline-grid;
  place-items: center;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  font-size: 11px;
  font-weight: 700;
  background: var(--surface-2);
  border: 1px solid var(--line-strong);
  color: var(--ink-muted);
}
.steps a[aria-current] .n { background: var(--accent); border-color: var(--accent); color: var(--accent-ink); }
.steps .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--line-strong); }
.steps .dot.ok { background: var(--ok); }
.steps .dot.warn { background: var(--warn); }
.steps .dot.bad { background: var(--bad); }
.steps .aside { margin-left: auto; display: flex; gap: 2px; }
.steps .aside a { font-weight: 600; font-size: 12.5px; color: var(--ink-faint); }
.steps .aside a:hover, .steps .aside a[aria-current] { color: var(--ink); }

/* Icons, and the icon-only buttons that carry most row actions. */
svg.icon { width: 15px; height: 15px; flex: none; fill: none; stroke: currentColor; stroke-width: 2;
  stroke-linecap: round; stroke-linejoin: round; vertical-align: -2px; }
svg.icon.fill { fill: currentColor; }
svg.icon.lead { margin-right: 5px; }
.icon-btn {
  display: inline-grid;
  place-items: center;
  width: 26px;
  height: 26px;
  padding: 0;
  border-radius: 6px;
  border: 1px solid transparent;
  background: transparent;
  color: var(--ink-muted);
  line-height: 1;
  vertical-align: middle;
  text-decoration: none;
}
button.icon-btn, button.icon-btn.danger { background: transparent; border-color: transparent; color: var(--ink-muted); }
.icon-btn:hover, button.icon-btn:hover { background: var(--surface-2); border-color: var(--line); color: var(--ink); }
.icon-btn:focus-visible { outline: 2px solid var(--accent-line); outline-offset: 1px; }
button.icon-btn.danger:hover { background: var(--bad-soft); color: var(--bad); border-color: transparent; }
.icon-btn.accent, button.icon-btn.accent { color: var(--accent); }
.icon-btn.accent:hover, button.icon-btn.accent:hover { background: var(--accent-soft); color: var(--accent);
  border-color: transparent; }
button.icon-btn:disabled { background: transparent; border-color: transparent; color: var(--ink-faint); opacity: .5; }
.icon-btn svg.chev { transition: transform .12s ease; }
.icon-btn[aria-expanded="true"] svg.chev { transform: rotate(180deg); }
span.icon-btn { cursor: default; }
span.icon-btn:hover { background: transparent; border-color: transparent; }
td.icons { white-space: nowrap; text-align: right; width: 1%; }
td.icons > * + * { margin-left: 1px; }

/* A model's abilities as four letters, its provider chain on one line. */
.abil { display: inline-flex; gap: 2px; }
.abil span { display: inline-grid; place-items: center; min-width: 17px; height: 17px; padding: 0 3px; border-radius: 4px;
  font-size: 10.5px; font-weight: 700; background: var(--accent-soft); color: var(--accent); }
.abil span.no { background: transparent; color: var(--ink-faint); text-decoration: line-through; font-weight: 600; }
.chain { font-size: 12.5px; }
.chain .p { font-weight: 650; color: var(--ink); }
.chain .sep { color: var(--ink-faint); margin: 0 4px; }
.chain .off { text-decoration: line-through; color: var(--ink-faint); }
.chain .bad { color: var(--bad); }
.keyhint { font-family: var(--mono); font-size: 12px; color: var(--ink-muted); }

/* A task's model and fallbacks, as three selects that save on change. */
form.chain-form { display: grid; grid-template-columns: minmax(0, 1.25fr) auto minmax(0, 1fr) auto minmax(0, 1fr);
  align-items: center; gap: 4px; margin: 0; }
form.chain-form select { padding: 3px 6px; font-size: 12.5px; border-radius: 6px; min-width: 0; }
form.chain-form select:disabled { opacity: .45; }
form.chain-form .sep { color: var(--ink-faint); font-size: 12px; }
form.chain-form select.blank { color: var(--ink-muted); font-style: italic; }
/* A provider route's failover, saved on change, under its state. */
form.failover-form { margin: 4px 0 0; }
form.failover-form select { padding: 2px 6px; font-size: 12px; border-radius: 6px; max-width: 100%; }
form[aria-busy="true"] select, form[aria-busy="true"] button { opacity: .5; pointer-events: none; }
@media (max-width: 760px) { form.chain-form { grid-template-columns: 1fr; } form.chain-form .sep { display: none; } }

table.dense.tight td { padding: 5px 10px 5px 0; }
table.dense.tight th { padding-bottom: 6px; }
tr.hit > td { background: var(--accent-soft); }
tr.sub > td { background: var(--surface-2); border-left: 2px solid var(--accent-line); padding: 10px 14px; }
tr.sub:hover { background: none; }
tr.sub table { background: var(--surface); border-radius: 6px; }
tr.sub .dl { margin-bottom: 8px; }
tr.sub .inline-form { margin-top: 8px; }
.next-step { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 13px; padding: 8px 12px;
  margin: 0 0 12px; border-radius: 8px; background: var(--accent-soft); color: var(--ink); }
.next-step svg.icon { color: var(--accent); }

/* The Overview's one-line summary: each cell a link to the step it summarises. */
.strip { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 0; margin: 0 0 16px;
  background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius); overflow: hidden; }
.strip a { display: block; padding: 9px 13px; color: var(--ink); text-decoration: none; border-right: 1px solid var(--line);
  min-width: 0; }
.strip a:last-child { border-right: 0; }
.strip a:hover { background: var(--surface-2); }
.strip .k { display: flex; align-items: center; gap: 6px; font-size: 10.5px; font-weight: 700; color: var(--ink-muted);
  text-transform: uppercase; letter-spacing: .06em; }
.strip .v { display: block; font-size: 15px; font-weight: 700; line-height: 1.35; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; }
.strip .s { display: block; font-size: 11.5px; color: var(--ink-muted); overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; }

.section-head { display: flex; align-items: center; gap: 8px 12px; flex-wrap: wrap; margin-bottom: 8px; }
.section-head h3 { margin: 0; flex: 1; }
.section > .section-head + .scroll { margin-top: 0; }
.ai-page > .section:first-child { border-top: 0; margin-top: 0; padding-top: 0; }

.toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 14px; margin: 0 0 12px; font-size: 13px;
  color: var(--ink-muted); }
.toolbar .grow { flex: 1; min-width: 200px; }
.toolbar b { color: var(--ink); font-weight: 650; }

.seg { display: inline-flex; padding: 2px; gap: 2px; border-radius: 8px; background: var(--surface-2);
  border: 1px solid var(--line); }
.seg a { padding: 3px 10px; border-radius: 6px; font-size: 12px; font-weight: 600; color: var(--ink-muted);
  text-decoration: none; }
.seg a:hover { color: var(--ink); }
.seg a[aria-current] { background: var(--surface); color: var(--ink); box-shadow: var(--shadow-low); }

.dl { display: flex; flex-wrap: wrap; gap: 8px 28px; margin: 0 0 4px; }
.dl > div { min-width: 0; }
.dl dt { font-size: 11px; font-weight: 700; color: var(--ink-muted); text-transform: uppercase; letter-spacing: .06em; }
.dl dd { margin: 2px 0 0; font-size: 13px; }

.power { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; font-weight: 650; }
.power::before { content: ""; width: 8px; height: 8px; border-radius: 50%; background: var(--bad); }
.power.on::before { background: var(--ok); }

/* Dense tables: the catalogue, the route chain, task routing. */
table.dense { font-size: 12.5px; }
table.dense td { padding: 7px 12px 7px 0; }
table.dense td .sub { font-size: 11.5px; }
table.dense .mono, table.dense code { font-size: 12px; overflow-wrap: anywhere; }
td.rank { font-size: 11px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; color: var(--ink-muted);
  white-space: nowrap; }
tr.off td:not(:last-child) { opacity: .55; }
.price { white-space: nowrap; font-variant-numeric: tabular-nums; }
.price .unit { color: var(--ink-faint); font-size: 11px; }
.caps { font-size: 12px; color: var(--ink-muted); }
.inherit { color: var(--ink-muted); font-style: italic; }
.inherit::before { content: "↳ "; font-style: normal; }
.set-here { font-weight: 650; }
.dim { color: var(--ink-faint); }

table.tree th.group, table.tree td.group {
  background: var(--surface-2);
  font-size: 12px;
  font-weight: 700;
  color: var(--ink);
  padding-top: 8px;
  padding-bottom: 8px;
}
table.tree td.group:first-child { padding-left: 10px; border-radius: 6px 0 0 6px; }
table.tree td.group:last-child { border-radius: 0 6px 6px 0; padding-right: 10px; }
table.tree { table-layout: fixed; min-width: 900px; }
table.tree th:nth-child(1) { width: 22%; }
table.tree th:nth-child(2) { width: 8%; }
table.tree th:nth-child(3) { width: 44%; }
table.tree th:nth-child(4) { width: 14%; }
table.tree th:nth-child(5) { width: 8%; }
table.tree th:nth-child(6) { width: 4%; }
table.tree td { overflow-wrap: anywhere; }
table.tree td.task { padding-left: 22px; }
tr.editor > td { background: var(--surface-2); border-left: 2px solid var(--accent-line); padding: 14px 16px 6px; }
tr.editor:hover { background: none; }
tr.editor .form-grid { max-width: 880px; }
tr.editor details.section { border-top: 0; padding-top: 0; margin: 0 0 10px; }

ul.problems { list-style: none; margin: 0 0 16px; padding: 0; display: grid; gap: 6px; }
ul.problems li { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; padding: 8px 12px; border-radius: 8px;
  font-size: 13px; background: var(--warn-soft); border-left: 3px solid var(--warn); }
ul.problems li.bad { background: var(--bad-soft); border-left-color: var(--bad); }
ul.problems li .grow { flex: 1; min-width: 220px; }
ul.problems li a { font-size: 12px; font-weight: 600; }
ul.problems li { padding: 5px 6px 5px 10px; gap: 8px; }
ul.problems li > svg.icon { color: var(--warn); }
ul.problems li.bad > svg.icon { color: var(--bad); }
ul.problems.dismissed li { background: var(--surface-2); border-left-color: var(--line-strong); color: var(--ink-muted); }
ul.problems.dismissed li > svg.icon { color: var(--ink-faint); }
.problems-foot { font-size: 12px; color: var(--ink-muted); margin: -10px 0 14px; }
.all-clear { font-size: 13px; color: var(--ok); margin: 0 0 16px; }
.all-clear::before { content: "✓ "; }

.settings-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0 18px; }
@media (max-width: 900px) { .settings-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 620px) { .settings-grid { grid-template-columns: 1fr; } }

.grid.two { grid-template-columns: repeat(auto-fit, minmax(380px, 1fr)); }
.card.tight { padding: 14px 16px; }
.card.tight > header { margin-bottom: 8px; }

/* -- signing in ------------------------------------------------------------- */

.auth-card {
  position: relative;
  width: min(100%, 480px);
  margin: 0 auto;
  padding: 58px 40px 30px;
  text-align: center;
  background:
    radial-gradient(circle at 50% 0%, color-mix(in srgb, var(--accent) 9%, transparent), transparent 50%),
    var(--surface);
  border: 1px solid var(--line);
  border-radius: 16px;
  box-shadow: 0 24px 70px color-mix(in srgb, var(--accent) 10%, transparent), var(--shadow-medium);
}
.auth-card.wide { width: min(100%, 580px); }
.auth-badge {
  position: absolute;
  left: 50%;
  top: -32px;
  transform: translateX(-50%);
  width: 64px;
  height: 64px;
  display: grid;
  place-items: center;
  border-radius: 18px;
  background: var(--surface);
  border: 1px solid var(--accent-line);
  box-shadow: 0 14px 30px color-mix(in srgb, var(--accent) 16%, transparent);
}
.auth-badge .mark { width: 36px; height: 36px; }
.auth-card h1 { font-size: clamp(1.7rem, 5vw, 2.1rem); margin-bottom: 8px; }
.auth-card .lede { margin: 0 auto 22px; max-width: 400px; }
.auth-card form { text-align: left; }
.auth-card .foot-note { margin: 18px 0 0; font-size: 13px; color: var(--ink-muted); }
.auth-card .note { text-align: left; }

/* -- the footer ------------------------------------------------------------- */

footer.foot { border-top: 1px solid var(--line); background: color-mix(in srgb, var(--surface) 70%, var(--canvas)); }
footer.foot .inner {
  max-width: var(--shell);
  margin: 0 auto;
  padding: 18px var(--gutter) 24px;
  display: flex;
  flex-wrap: wrap;
  gap: 6px 18px;
  align-items: center;
  font-size: 13px;
  color: var(--ink-muted);
}
footer.foot .wordmark { font-size: 1rem; color: var(--ink); }

@media (max-width: 680px) {
  .top .inner { padding-block: 10px; gap: 6px 12px; }
  .brand { width: 100%; }
  .brand .area { margin-left: auto; }
  .top nav { width: 100%; margin-left: 0; gap: 2px; }
  .top nav a, .top nav button.link { min-height: 34px; padding: 5px 10px; font-size: 13px; }
  .top nav .who { order: 10; width: 100%; padding-left: 10px; }
  .auth-card { padding: 54px 22px 24px; }
  table.kv td:first-child { width: auto; }
}
`;
