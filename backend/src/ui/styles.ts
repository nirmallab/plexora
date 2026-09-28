/**
 * The one stylesheet, inlined and pinned by CSP hash (so no style="" attribute
 * works anywhere: every look is a class here, and chart geometry is SVG
 * attributes).
 *
 * The same tokens and components as the licence admin (licensing/src/ui/
 * styles.ts), in Plexora's palette from DESIGN.md: Signal Cyan on near-black
 * in dark mode, Desk Signal blue on paper in light mode. Tighter spacing than
 * the licence pages, because these are dense tables of numbers. The page
 * follows the system; a `light` or `dark` class on <html>, set by the theme
 * toggle, overrides it. System fonts only: nothing is fetched.
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
  --shell: 1320px;
  --gutter: clamp(16px, 2.4vw, 32px);
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
  font: 14px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", "Noto Sans", "Liberation Sans", Arial, sans-serif;
  -webkit-font-smoothing: antialiased;
  color: var(--ink);
  background:
    radial-gradient(64rem 34rem at 8% -16rem, color-mix(in srgb, var(--accent) 9%, transparent), transparent 70%),
    var(--canvas);
}

code, pre, .mono { font-family: var(--mono); }
code, .mono { font-size: 0.92em; overflow-wrap: anywhere; }
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
  min-height: 56px;
  padding: 0 var(--gutter);
  display: flex;
  align-items: center;
  gap: 6px 20px;
  flex-wrap: wrap;
}
.brand { display: flex; align-items: center; gap: 9px; color: var(--ink); text-decoration: none; min-height: 44px; }
.brand:hover { color: var(--ink); }
.mark { width: 24px; height: 24px; flex: none; color: var(--ink); }
.mark .ring { fill: none; stroke: currentColor; stroke-width: 2; }
.mark .cell { fill: var(--accent); }
.mark .cell.dim { fill: currentColor; opacity: 0.7; }
.mark .cell.soft { opacity: 0.8; }
.wordmark { font-size: 1.2rem; font-weight: 760; line-height: 1.1; }
.brand .area { margin-left: 2px; font-size: 11px; font-weight: 700; color: var(--ink-muted);
  text-transform: uppercase; letter-spacing: .09em; }
.top nav { margin-left: auto; display: flex; align-items: center; gap: 2px; flex-wrap: wrap; }
.top nav a, .top nav button.link {
  display: inline-flex;
  align-items: center;
  min-height: 34px;
  padding: 5px 11px;
  border-radius: 8px;
  font-size: 13.5px;
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
  text-decoration: none;
}
.top nav .who { font-size: 12.5px; color: var(--ink-muted); padding: 4px 4px 4px 12px; margin-left: 6px;
  border-left: 1px solid var(--line); }
.top nav .theme-toggle { padding: 5px 9px; font-size: 16px; line-height: 1; }

/* -- the page --------------------------------------------------------------- */

main {
  flex: 1;
  width: 100%;
  max-width: var(--shell);
  margin: 0 auto;
  padding: 18px var(--gutter) 40px;
}
main.narrow { max-width: 520px; }
main.center { display: grid; align-content: center; padding-top: clamp(48px, 10vh, 110px); }

.page-head { display: flex; flex-wrap: wrap; align-items: flex-end; gap: 8px 16px; margin-bottom: 12px; }
.page-head .grow { flex: 1; min-width: 240px; }
.page-head .actions { margin-left: auto; }
h1 { font-size: clamp(1.3rem, 2.4vw, 1.5rem); line-height: 1.2; font-weight: 740; margin: 0 0 3px; }
h2 { font-size: 14.5px; margin: 0; font-weight: 700; }
h3 { font-size: 11px; margin: 0 0 6px; text-transform: uppercase; letter-spacing: .06em; color: var(--ink-muted); font-weight: 700; }
p { margin: 0 0 10px; }
.lede { color: var(--ink-muted); margin: 0; font-size: 13.5px; }
.muted { color: var(--ink-muted); }
.small { font-size: 13px; }
.tiny-text { font-size: 12px; }
.right { text-align: right; }
.nowrap { white-space: nowrap; }
.ok-text { color: var(--ok); }
.warn-text { color: var(--warn); }
.bad-text { color: var(--bad); }

/* -- cards ------------------------------------------------------------------ */

.card {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  box-shadow: var(--shadow-low);
  padding: 12px 14px;
  margin: 0 0 12px;
  min-width: 0;
}
.card > header { display: flex; align-items: baseline; gap: 4px 12px; flex-wrap: wrap; margin-bottom: 8px; }
.card > header .grow { flex: 1; min-width: 160px; display: flex; align-items: baseline; gap: 4px 10px; flex-wrap: wrap; }
.card > header .sub { color: var(--ink-muted); font-size: 12.5px; }
.card > header .actions { margin-left: auto; }
.card.feature {
  border-color: color-mix(in srgb, var(--line) 70%, var(--accent));
  background:
    radial-gradient(circle at 10% -30%, color-mix(in srgb, var(--accent) 10%, transparent), transparent 42%),
    var(--surface);
}
.card.flush { padding: 0; }
.card.flush > header { padding: 12px 14px 0; }
.card.flush .scroll { padding: 0 14px 4px; margin: 0; }
.card.flush > .empty, .card.flush > .card-foot { padding-left: 14px; padding-right: 14px; }
.card-foot { border-top: 1px solid var(--line); padding: 8px 0 10px; margin-top: 2px; font-size: 12.5px; color: var(--ink-muted); }

.grid { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); margin-bottom: 12px; }
.grid.two { grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); }
.grid.three { grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); }
.grid > .card { margin: 0; }
.grid.start { align-items: start; }

details.section { border-top: 1px solid var(--line); padding-top: 10px; margin-top: 10px; }
details.section > summary { cursor: pointer; font-size: 13.5px; font-weight: 600; list-style: none; }
details.section > summary::-webkit-details-marker { display: none; }
details.section > summary::before { content: "▸ "; color: var(--ink-muted); }
details.section[open] > summary::before { content: "▾ "; }
details.section > summary:hover { color: var(--accent); }
details.section > *:not(summary) { margin-top: 10px; }
details.card > summary { cursor: pointer; list-style: none; font-weight: 700; font-size: 14.5px; }
details.card > summary::-webkit-details-marker { display: none; }
details.card > summary::before { content: "▸ "; color: var(--ink-muted); }
details.card[open] > summary::before { content: "▾ "; }
details.card > summary .sub { font-weight: 400; font-size: 12.5px; color: var(--ink-muted); margin-left: 6px; }
details.card[open] > summary { margin-bottom: 10px; }

/* -- numbers ---------------------------------------------------------------- */

.stats { display: grid; gap: 10px; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); margin-bottom: 12px; }
.stat {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  box-shadow: var(--shadow-low);
  padding: 9px 13px 10px;
  min-width: 0;
}
.stat .label { font-size: 10.5px; font-weight: 700; color: var(--ink-muted); text-transform: uppercase; letter-spacing: .06em;
  display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.stat .n { font-size: 21px; font-weight: 720; letter-spacing: -.01em; display: block; line-height: 1.25;
  font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.stat .n.text { font-size: 16px; line-height: 1.5; }
.stat .sub { font-size: 12px; color: var(--ink-muted); display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.stat.ok .n { color: var(--ok); }
.stat.warn .n { color: var(--warn); }
.stat.bad .n { color: var(--bad); }
.card .stats { margin-bottom: 0; }
.card .stat { box-shadow: none; background: var(--surface-2); }

svg.bars { display: block; width: 100%; height: 64px; margin: 2px 0 3px; }
svg.bars.tall { height: 88px; }
svg.bars rect { fill: var(--accent-soft); }
svg.bars rect.on { fill: var(--accent); }
svg.bars rect.alt.on { fill: color-mix(in srgb, var(--accent) 55%, var(--ok)); }
svg.bars line { stroke: var(--line); stroke-dasharray: 3 5; vector-effect: non-scaling-stroke; }
.axis { display: flex; justify-content: space-between; gap: 8px; font-size: 11.5px; color: var(--ink-muted);
  font-variant-numeric: tabular-nums; }
.axis.bands { justify-content: space-around; }
.axis.bands span { flex: 1; text-align: center; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* A 9-bin histogram small enough to sit in a table cell. */
svg.hist { display: inline-block; width: 90px; height: 18px; vertical-align: middle; }
svg.hist rect { fill: var(--accent); }
svg.hist rect.z { fill: var(--line); }

/* A share of the largest row, behind its number. */
svg.share { display: block; width: 100%; height: 5px; margin-top: 3px; }
svg.share rect { fill: var(--accent); }
svg.share rect.track { fill: var(--surface-2); }
.split-list { display: grid; gap: 7px; margin: 0; padding: 0; list-style: none; }
.split-list li { min-width: 0; }
.split-list .row-line { display: flex; justify-content: space-between; gap: 10px; font-size: 13px; }
.split-list .row-line .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.split-list .row-line .v { font-variant-numeric: tabular-nums; color: var(--ink-muted); white-space: nowrap; }
.split-list .row-line .v b { color: var(--ink); font-weight: 650; }

svg.meter { display: block; width: 100%; height: 12px; margin: 6px 0 4px; }
svg.meter .track { fill: var(--surface-2); stroke: var(--line); }
svg.meter .fill { fill: var(--accent); }
svg.meter .fill.warn { fill: var(--warn); }
svg.meter .fill.bad { fill: var(--bad); }
svg.meter .tick { stroke: var(--ink-faint); stroke-width: 1; vector-effect: non-scaling-stroke; }
.meter-head { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
.meter-head .pct { font-size: 18px; font-weight: 720; font-variant-numeric: tabular-nums; }

.summary-strip { display: flex; flex-wrap: wrap; gap: 3px 16px; align-items: baseline; font-size: 12.5px; color: var(--ink-muted); }
.summary-strip b { color: var(--ink); font-size: 13.5px; font-weight: 700; font-variant-numeric: tabular-nums; }

/* -- badges ----------------------------------------------------------------- */

.badge {
  display: inline-block;
  font-size: 11.5px;
  font-weight: 600;
  line-height: 1.5;
  padding: 0 8px;
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
.chips { display: flex; flex-wrap: wrap; gap: 5px; }

/* -- tables ----------------------------------------------------------------- */

.scroll { overflow-x: auto; margin: 0 -4px; padding: 0 4px; }
.scroll.capped { max-height: 440px; overflow-y: auto; }
.scroll.capped thead th { position: sticky; top: 0; background: var(--surface); z-index: 1; }
table { width: 100%; border-collapse: collapse; font-size: 13px; font-variant-numeric: tabular-nums; }
th {
  text-align: left;
  font-size: 10.5px;
  text-transform: uppercase;
  letter-spacing: .05em;
  color: var(--ink-muted);
  font-weight: 700;
  padding: 4px 12px 6px 0;
  border-bottom: 1px solid var(--line);
  white-space: nowrap;
}
td { padding: 5px 12px 5px 0; border-bottom: 1px solid var(--line); vertical-align: middle; }
tr:last-child td { border-bottom: 0; }
tbody tr:hover { background: color-mix(in srgb, var(--surface-2) 70%, transparent); }
td.num, th.num { text-align: right; white-space: nowrap; }
td:last-child, th:last-child { padding-right: 0; }
td.actions { white-space: nowrap; text-align: right; }
td.wrap { white-space: normal; }
td .sub { font-size: 12px; color: var(--ink-muted); }
tr.dim td { color: var(--ink-faint); }
tr.failed td { color: var(--bad); }
tr.group td { font-weight: 650; padding-top: 9px; }
td.cont { color: var(--ink-faint); }
table.kv td { white-space: normal; }
table.kv td:first-child { width: 42%; color: var(--ink-muted); }
.kv-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 0 24px; }
.empty { color: var(--ink-muted); font-size: 13px; padding: 12px 0; text-align: center; }

/* -- forms ------------------------------------------------------------------ */

.field { margin: 0 0 12px; min-width: 0; }
.field label, .field .label { display: block; font-size: 11px; font-weight: 700; color: var(--ink-muted);
  text-transform: uppercase; letter-spacing: .05em; margin-bottom: 3px; }
input[type=text], input[type=password], input[type=date], input:not([type]), select {
  width: 100%;
  padding: 6px 9px;
  font: inherit;
  font-size: 13.5px;
  color: var(--ink);
  background: var(--surface-3);
  border: 1px solid var(--line-strong);
  border-radius: 7px;
}
input:focus, select:focus { outline: 2px solid var(--accent-line); outline-offset: 0; border-color: var(--accent); }

/* The filter bar: one row of fields, then the range presets. */
.filters { display: flex; flex-wrap: wrap; align-items: flex-end; gap: 8px 10px; }
.filters .field { margin: 0; flex: 1; min-width: 120px; }
.filters .field.date { flex: 1.2; min-width: 138px; }
.filters .go { display: flex; gap: 6px; align-items: center; }
.filters .presets { display: flex; gap: 2px; align-items: center; margin-left: auto; flex-wrap: wrap; }
.filters .presets a { font-size: 12.5px; font-weight: 650; padding: 4px 9px; border-radius: 7px; text-decoration: none;
  color: var(--ink-muted); }
.filters .presets a:hover, .filters .presets a[aria-current] { background: var(--accent-soft); color: var(--accent); }
.filter-card { padding: 10px 14px; }
.filters input, .filters select, .filters .go button, .filters .go .button { height: 34px; }
.filters .go .button { display: inline-flex; align-items: center; }

/* -- buttons ---------------------------------------------------------------- */

button, .button {
  font: inherit;
  font-size: 13.5px;
  font-weight: 600;
  padding: 6px 14px;
  border-radius: 7px;
  border: 1px solid var(--accent);
  background: var(--accent);
  color: var(--accent-ink);
  cursor: pointer;
  text-decoration: none;
  display: inline-block;
  line-height: 1.4;
  white-space: nowrap;
}
button:hover, .button:hover { background: var(--accent-hover); border-color: var(--accent-hover); color: var(--accent-ink); }
button:disabled { color: var(--ink-faint); background: var(--surface-2); border-color: var(--line); cursor: not-allowed; }
button.ghost, .button.ghost { background: transparent; color: var(--ink); border-color: var(--line-strong); }
button.ghost:hover, .button.ghost:hover { background: var(--surface-2); color: var(--ink); border-color: var(--line-strong); }
button.danger { background: transparent; color: var(--bad); border-color: var(--bad); }
button.danger:hover { background: var(--bad-soft); color: var(--bad); border-color: var(--bad); }
button.tiny, .button.tiny { padding: 2px 9px; font-size: 12.5px; }
button.wide, .button.wide { width: 100%; display: block; text-align: center; }
button.link { background: none; border: 0; padding: 0; color: var(--accent); font-weight: 600; }
button.link:hover { background: none; color: var(--accent-hover); text-decoration: underline; }
.actions { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }

/* -- messages --------------------------------------------------------------- */

.flash {
  position: sticky;
  top: 64px;
  z-index: 30;
  margin: 0 0 12px;
  padding: 8px 12px;
  border-radius: 8px;
  font-size: 13.5px;
  background: var(--ok-soft);
  color: var(--ok);
  border: 1px solid currentColor;
}
.flash.bad { background: var(--bad-soft); color: var(--bad); }

.note {
  background: var(--surface-2);
  border-left: 3px solid var(--accent);
  border-radius: 0 8px 8px 0;
  padding: 7px 12px;
  font-size: 13px;
  margin: 0 0 12px;
}
.note.warn { border-left-color: var(--warn); background: var(--warn-soft); color: var(--ink); }
.note.bad { border-left-color: var(--bad); background: var(--bad-soft); color: var(--ink); }

/* -- signing in ------------------------------------------------------------- */

.auth-card {
  position: relative;
  width: min(100%, 440px);
  margin: 0 auto;
  padding: 52px 34px 26px;
  text-align: center;
  background:
    radial-gradient(circle at 50% 0%, color-mix(in srgb, var(--accent) 9%, transparent), transparent 50%),
    var(--surface);
  border: 1px solid var(--line);
  border-radius: 16px;
  box-shadow: 0 24px 70px color-mix(in srgb, var(--accent) 10%, transparent), var(--shadow-medium);
}
.auth-badge {
  position: absolute;
  left: 50%;
  top: -30px;
  transform: translateX(-50%);
  width: 60px;
  height: 60px;
  display: grid;
  place-items: center;
  border-radius: 17px;
  background: var(--surface);
  border: 1px solid var(--accent-line);
  box-shadow: 0 14px 30px color-mix(in srgb, var(--accent) 16%, transparent);
}
.auth-badge .mark { width: 34px; height: 34px; }
.auth-card h1 { font-size: clamp(1.5rem, 5vw, 1.85rem); margin-bottom: 6px; }
.auth-card .lede { margin: 0 auto 20px; max-width: 360px; }
.auth-card form { text-align: left; }
.auth-card .foot-note { margin: 16px 0 0; font-size: 12.5px; color: var(--ink-muted); }

/* -- the footer ------------------------------------------------------------- */

footer.foot { border-top: 1px solid var(--line); background: color-mix(in srgb, var(--surface) 70%, var(--canvas)); }
footer.foot .inner {
  max-width: var(--shell);
  margin: 0 auto;
  padding: 12px var(--gutter) 16px;
  display: flex;
  flex-wrap: wrap;
  gap: 4px 16px;
  align-items: center;
  font-size: 12.5px;
  color: var(--ink-muted);
}
footer.foot .wordmark { font-size: .95rem; color: var(--ink); }

@media (max-width: 720px) {
  .top .inner { padding-block: 8px; gap: 4px 10px; }
  .brand { width: 100%; }
  .brand .area { margin-left: auto; }
  .top nav { width: 100%; margin-left: 0; }
  .top nav a, .top nav button.link { min-height: 32px; padding: 4px 9px; font-size: 13px; }
  .top nav .who { order: 10; width: 100%; border-left: 0; margin-left: 0; padding-left: 9px; }
  .grid.two { grid-template-columns: 1fr; }
  .filters .presets { margin-left: 0; }
  .auth-card { padding: 50px 20px 22px; }
  .flash { top: 8px; }
}
`;
