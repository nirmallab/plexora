#!/usr/bin/env node
// Fast pre-build check of MDX pages: compiles each file with @mdx-js/mdx,
// and reports components the site does not register, links to /docs/ pages
// that do not exist, and screenshots missing from public/.
//
//   node scripts/check_mdx.mjs [files or dirs...]     (default: content/docs)
//
// `next build` catches the same compile errors, but slowly and one at a
// time; this reports every file at once and can run while a build is going.
import { compile } from '@mdx-js/mdx';
import remarkGfm from 'remark-gfm';
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const site = resolve(here, '..');
const content = join(site, 'content', 'docs');

const registry = readFileSync(join(site, 'components', 'mdx.tsx'), 'utf8');
const known = new Set([
  ...[...registry.matchAll(/^\s{4}(\w+),$/gm)].map((m) => m[1]),
  'Card', 'Cards', 'Callout', 'CodeBlockTab', 'CodeBlockTabs', 'CodeBlockTabsList', 'CodeBlockTabsTrigger',
]);

function walk(p, acc = []) {
  const s = statSync(p);
  if (s.isDirectory()) for (const n of readdirSync(p)) walk(join(p, n), acc);
  else if (p.endsWith('.mdx')) acc.push(p);
  return acc;
}

function pageExists(url) {
  const rel = url.replace(/^\/docs\/?/, '').replace(/[#?].*$/, '').replace(/\/$/, '');
  if (!rel) return existsSync(join(content, 'index.mdx'));
  return existsSync(join(content, `${rel}.mdx`)) || existsSync(join(content, rel, 'index.mdx'));
}

const targets = process.argv.slice(2).length ? process.argv.slice(2).map((p) => resolve(p)) : [content];
const files = targets.flatMap((t) => walk(t));
const problems = [];
const warnings = [];
const strictLinks = process.env.CHECK_MDX_STRICT_LINKS === '1';

for (const file of files) {
  const rel = relative(site, file);
  const text = readFileSync(file, 'utf8');
  if (!text.startsWith('---\n')) problems.push(`${rel}: missing frontmatter`);
  else if (!/^title:/m.test(text.split('---', 3)[1])) problems.push(`${rel}: frontmatter has no title`);
  try {
    await compile(text.replace(/^---\n[\s\S]*?\n---\n/, ''), { remarkPlugins: [remarkGfm] });
  } catch (err) {
    problems.push(`${rel}:${err.line ?? '?'}:${err.column ?? '?'} ${err.reason ?? err.message}`);
    continue;
  }
  const body = text.replace(/```[\s\S]*?```/g, '').replace(/`[^`\n]*`/g, '');
  const tags = body.replace(/=\{"(?:[^"\\]|\\.)*"\}/g, '=""').replace(/\\</g, '');
  for (const m of tags.matchAll(/<([A-Z]\w*)[\s/>]/g)) {
    if (!known.has(m[1])) problems.push(`${rel}: unknown component <${m[1]}> (register it in components/mdx.tsx)`);
  }
  for (const m of body.matchAll(/\]\((\/docs[^)\s]*)\)|href=["{]+["']?(\/docs[^"'}\s]*)/g)) {
    const url = m[1] || m[2];
    if (!pageExists(url)) (strictLinks ? problems : warnings).push(`${rel}: link to missing page ${url}`);
  }
  for (const m of body.matchAll(/<Screenshot[^>]*src=["{]+["']?([^"'}\s]+)/g)) {
    if (!existsSync(join(site, 'public', m[1]))) warnings.push(`${rel}: screenshot not captured yet ${m[1]}`);
  }
}

if (warnings.length) console.warn(warnings.join('\n'));
if (problems.length) {
  console.error(problems.join('\n'));
  console.error(`\ncheck_mdx: ${problems.length} problem(s) in ${files.length} file(s).`);
  process.exit(1);
}
console.log(`check_mdx: ${files.length} file(s) compile${warnings.length ? `, ${warnings.length} warning(s)` : ''}.`);
