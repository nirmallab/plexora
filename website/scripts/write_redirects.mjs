#!/usr/bin/env node
// Writes a small page at every old URL in redirects.json that sends the
// reader on to where the content lives now. GitHub Pages has no server-side
// redirects, so each stub is a static index.html: noindex, a canonical link,
// a meta refresh, a script that keeps the #fragment, and a visible link.
//
// "pages" maps one old path to one new one; "prefixes" mirrors every page
// that now exists under the new prefix back under the old one. A stub never
// replaces a real page, and every internal target must exist in out/.
//
//   node scripts/write_redirects.mjs [outDir]
import { existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const outDir = resolve(process.argv[2] ?? join(here, '..', 'out'));
const basePath = process.env.NEXT_PUBLIC_BASE_PATH ?? '/plexora';
const { pages, prefixes } = JSON.parse(readFileSync(join(here, '..', 'redirects.json'), 'utf8'));

if (!existsSync(outDir)) {
  console.error(`write_redirects: ${outDir} does not exist; run next build first.`);
  process.exit(2);
}

const esc = (s) => s.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
const fileFor = (path) => join(outDir, path.replace(/^\/+/, ''), 'index.html');
const external = (url) => /^https?:\/\//.test(url);

function stub(to) {
  const href = external(to) ? to : `${basePath}${to}`;
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Moved</title>
<meta name="robots" content="noindex">
<link rel="canonical" href="${esc(href)}">
<meta http-equiv="refresh" content="0; url=${esc(href)}">
<script>location.replace(${JSON.stringify(href)} + (${external(to)} ? '' : location.hash));</script>
</head>
<body>
<p>This page has moved to <a href="${esc(href)}">${esc(href)}</a>.</p>
</body>
</html>
`;
}

const problems = [];
const written = [];

function write(from, to) {
  const file = fileFor(from);
  if (existsSync(file) && !readFileSync(file, 'utf8').includes('<title>Moved</title>')) {
    problems.push(`${from} is a real page; remove it from redirects.json`);
    return;
  }
  if (!external(to) && !existsSync(fileFor(to))) {
    problems.push(`${from} -> ${to}: the target is not in the build`);
    return;
  }
  mkdirSync(dirname(file), { recursive: true });
  writeFileSync(file, stub(to));
  written.push(from);
}

for (const [from, to] of Object.entries(pages)) write(from, to);

function walk(dir, acc = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p, acc);
    else if (name === 'index.html') acc.push(p);
  }
  return acc;
}

for (const [oldPrefix, newPrefix] of Object.entries(prefixes)) {
  const root = join(outDir, newPrefix.replace(/^\/+/, ''));
  if (!existsSync(root)) {
    problems.push(`prefix ${oldPrefix} -> ${newPrefix}: nothing was built under ${newPrefix}`);
    continue;
  }
  for (const file of walk(root)) {
    const rest = relative(root, dirname(file)).split('\\').join('/');
    const suffix = rest ? `${rest}/` : '';
    write(`${oldPrefix}${suffix}`, `${newPrefix}${suffix}`);
  }
}

if (problems.length) {
  console.error(problems.map((p) => `write_redirects: ${p}`).join('\n'));
  process.exit(1);
}
console.log(`write_redirects: ${written.length} redirect page(s).`);
