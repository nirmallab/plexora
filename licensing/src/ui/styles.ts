/**
 * The one stylesheet for the portal and admin pages, inlined and pinned by CSP
 * hash. Colours are tokens on :root, redefined for dark mode.
 */
export const STYLES = `
:root {
  --bg: #f7f7f5; --surface: #ffffff; --ink: #1c1d1f; --muted: #62666d; --line: #e3e3df;
  --accent: #2f6fdf; --accent-soft: #dbe6fb; --ok: #1f8a4c; --warn: #b7791f; --bad: #c53030;
  --radius: 8px; --gap: 16px;
  font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
  color-scheme: light dark;
}
@media (prefers-color-scheme: dark) {
  :root { --bg: #141518; --surface: #1c1e22; --ink: #e8e8e6; --muted: #9a9ea6; --line: #2d3036;
    --accent: #6d9cf0; --accent-soft: #23324f; --ok: #4cc27f; --warn: #e0a84a; --bad: #f07171; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); }
header.top { display: flex; flex-wrap: wrap; gap: 4px 16px; align-items: center; padding: 12px 16px;
  background: var(--surface); border-bottom: 1px solid var(--line); }
header.top .brand { font-weight: 650; margin-right: 8px; }
header.top .who { margin-left: auto; }
nav { display: flex; flex-wrap: wrap; gap: 2px; }
nav a { color: var(--muted); text-decoration: none; padding: 4px 8px; border-radius: 6px; }
nav a.on, nav a:hover { color: var(--ink); background: var(--accent-soft); }
main { padding: 16px; max-width: 1100px; margin: 0 auto; }
h1 { font-size: 20px; margin: 4px 0 12px; }
h2 { font-size: 15px; margin: 24px 0 8px; }
p { margin: 6px 0; }
a { color: var(--accent); }
.grid { display: grid; gap: var(--gap); grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); }
.card { background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius); padding: 12px 14px; }
.stat .v { font-size: 20px; font-weight: 650; font-variant-numeric: tabular-nums; }
.stat .k { color: var(--muted); font-size: 12px; }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; background: var(--surface); font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { color: var(--muted); font-weight: 550; font-size: 12px; white-space: nowrap; }
td.actions { white-space: nowrap; }
.muted { color: var(--muted); }
.small { font-size: 12px; }
.badge { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 12px; background: var(--accent-soft); }
.badge.ok { color: var(--ok); } .badge.warn { color: var(--warn); } .badge.bad { color: var(--bad); }
.notice { border-left: 3px solid var(--warn); padding: 8px 12px; background: var(--surface); margin: 12px 0; }
form.stack { display: grid; gap: 10px; max-width: 520px; }
form.inline { display: flex; flex-wrap: wrap; gap: 8px; align-items: end; }
label { display: grid; gap: 2px; font-size: 12px; color: var(--muted); }
input, select, textarea, button { font: inherit; color: var(--ink); background: var(--surface);
  border: 1px solid var(--line); border-radius: 6px; padding: 6px 9px; min-height: 32px; }
button { cursor: pointer; } button.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
button.danger { color: var(--bad); }
code, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; overflow-wrap: anywhere; }
#flash { min-height: 1.4em; color: var(--muted); }
#reveal:empty { display: none; }
#reveal { margin: 12px 0; padding: 12px; border: 1px dashed var(--accent); border-radius: var(--radius);
  background: var(--surface); }
.login { max-width: 400px; margin: 10vh auto; }
.login form { display: grid; gap: 10px; }
@media (max-width: 600px) { main { padding: 12px 16px; } th, td { padding: 5px 6px; } }
`;
