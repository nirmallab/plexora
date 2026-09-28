#!/usr/bin/env node
// Checks every internal link, asset and #anchor in the static export.
//
// Pages are served from a sub-path (/plexora/ on GitHub Pages), which generic
// link checkers cannot mount, so this walks out/**/*.html itself: strip the
// basePath from each href/src, resolve against out/, and require a file (or
// dir/index.html) and, for #fragments, an element with that id. External
// links are only checked for being well-formed URLs.
//
//   node scripts/check_links.mjs [outDir]
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const outDir = resolve(process.argv[2] ?? join(here, '..', 'out'));
const basePath = process.env.NEXT_PUBLIC_BASE_PATH ?? '/plexora';

if (!existsSync(outDir)) {
  console.error(`check_links: ${outDir} does not exist; run \`npm run build\` first.`);
  process.exit(2);
}

function walk(dir, acc = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p, acc);
    else if (name.endsWith('.html')) acc.push(p);
  }
  return acc;
}

const idCache = new Map();
function idsOf(file) {
  if (!idCache.has(file)) {
    const html = readFileSync(file, 'utf8');
    const ids = new Set();
    for (const m of html.matchAll(/\sid="([^"]+)"/g)) ids.add(decodeEntities(m[1]));
    idCache.set(file, ids);
  }
  return idCache.get(file);
}

function decodeEntities(s) {
  return s
    .replace(/&amp;/g, '&')
    .replace(/&quot;/g, '"')
    .replace(/&#x27;/g, "'")
    .replace(/&#39;/g, "'")
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>');
}

/** Map a site URL path (basePath stripped) to a file in out/, or null. */
function target(pathname) {
  const clean = decodeURIComponent(pathname).replace(/^\/+/, '');
  const candidates = [join(outDir, clean), join(outDir, clean, 'index.html'), join(outDir, `${clean}.html`)];
  for (const c of candidates) {
    if (existsSync(c) && statSync(c).isFile()) return c;
  }
  return null;
}

const errors = [];
const files = walk(outDir);
let checked = 0;

for (const file of files) {
  const html = readFileSync(file, 'utf8');
  const pageRel = '/' + relative(outDir, file).replace(/index\.html$/, '').replace(/\\/g, '/');
  for (const m of html.matchAll(/\s(href|src)="([^"]*)"/g)) {
    const raw = decodeEntities(m[2]);
    if (!raw || raw.startsWith('data:') || raw.startsWith('mailto:') || raw.startsWith('javascript:')) continue;
    checked++;
    if (/^[a-z][a-z0-9+.-]*:/i.test(raw)) {
      try {
        new URL(raw);
      } catch {
        errors.push(`${pageRel}: malformed external URL ${raw}`);
      }
      continue;
    }
    if (raw.startsWith('//')) continue;

    let url;
    try {
      url = new URL(raw, `https://site.invalid${basePath}${pageRel}`);
    } catch {
      errors.push(`${pageRel}: unparseable link ${raw}`);
      continue;
    }
    let path = url.pathname;
    if (basePath && !(path === basePath || path.startsWith(`${basePath}/`))) {
      errors.push(`${pageRel}: link escapes basePath ${basePath}: ${raw}`);
      continue;
    }
    path = path.slice(basePath.length) || '/';
    const hit = path === '/' && !url.hash ? join(outDir, 'index.html') : target(path);
    if (!hit) {
      errors.push(`${pageRel}: broken link ${raw}`);
      continue;
    }
    if (url.hash && url.hash.length > 1 && hit.endsWith('.html')) {
      const id = decodeURIComponent(url.hash.slice(1));
      if (!idsOf(hit).has(id)) errors.push(`${pageRel}: missing anchor ${raw}`);
    }
  }
}

if (errors.length) {
  const unique = [...new Set(errors)].sort();
  console.error(unique.join('\n'));
  console.error(`\ncheck_links: ${unique.length} problem(s) in ${files.length} pages (${checked} links checked).`);
  process.exit(1);
}
console.log(`check_links: ${files.length} pages, ${checked} links, all resolve.`);
