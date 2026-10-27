#!/usr/bin/env node
// Resolves the latest desktop release into generated/release.json, which the
// home page and the Installation page read for their download buttons.
//
// The asset names come from scripts/release.py (`Plexora-<ver>-macos-arm64.dmg`
// and so on); the buttons use the URLs GitHub returns, never a guessed one.
// Offline, or rate-limited, the committed file is kept with a warning so a
// local build still works. `--check` validates the committed file only.
//
//   node scripts/resolve_release.mjs [--check]
import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const outPath = join(here, '..', 'generated', 'release.json');
const repo = 'nirmallab/plexora';

/** Download key -> the suffix scripts/release.py gives that asset. */
const ASSETS = {
  macos_arm64: '-macos-arm64.dmg',
  windows_x64: '-windows-x64-setup.exe',
  linux_x64_deb: '-linux-x64.deb',
};

function validate(release) {
  const problems = [];
  if (!/^v\d+\.\d+\.\d+/.test(release?.tag ?? '')) problems.push(`tag ${JSON.stringify(release?.tag)} is not vX.Y.Z`);
  for (const [key, suffix] of Object.entries(ASSETS)) {
    const a = release?.assets?.[key];
    if (!a) problems.push(`no ${key} asset (*${suffix})`);
    else if (!a.url?.startsWith(`https://github.com/${repo}/releases/download/`) || !a.name?.endsWith(suffix) || !(a.size > 0))
      problems.push(`${key} asset is malformed: ${JSON.stringify(a)}`);
  }
  return problems;
}

function fail(problems) {
  for (const p of problems) console.error(`resolve_release: ${p}`);
  process.exit(1);
}

if (process.argv.includes('--check')) {
  if (!existsSync(outPath)) fail([`${outPath} does not exist; run npm run resolve:release`]);
  const problems = validate(JSON.parse(readFileSync(outPath, 'utf8')));
  if (problems.length) fail(problems);
  console.log('resolve_release: generated/release.json is well-formed.');
  process.exit(0);
}

const headers = { Accept: 'application/vnd.github+json', 'User-Agent': 'plexora-docs' };
if (process.env.GITHUB_TOKEN) headers.Authorization = `Bearer ${process.env.GITHUB_TOKEN}`;

let data;
try {
  const res = await fetch(`https://api.github.com/repos/${repo}/releases/latest`, { headers });
  if (!res.ok) throw new Error(`GitHub answered ${res.status} ${res.statusText}`);
  data = await res.json();
} catch (err) {
  // CI must not deploy stale buttons silently; a laptop build may.
  if (process.env.CI) fail([`could not read the latest release: ${err.message}`]);
  if (!existsSync(outPath)) fail([`could not read the latest release (${err.message}) and there is no committed file`]);
  console.warn(`resolve_release: could not read the latest release (${err.message}); keeping the committed file.`);
  process.exit(0);
}

const release = {
  tag: data.tag_name,
  version: String(data.tag_name).replace(/^v/, ''),
  published_at: data.published_at,
  url: data.html_url,
  assets: {},
};
for (const [key, suffix] of Object.entries(ASSETS)) {
  const a = (data.assets ?? []).find((x) => x.name.endsWith(suffix));
  if (a) release.assets[key] = { name: a.name, url: a.browser_download_url, size: a.size };
}
const problems = validate(release);
if (problems.length) fail(problems);

const text = `${JSON.stringify(release, null, 2)}\n`;
const before = existsSync(outPath) ? readFileSync(outPath, 'utf8') : '';
if (text !== before) writeFileSync(outPath, text);
console.log(`resolve_release: ${release.tag}${text === before ? ' (unchanged)' : ''}.`);
