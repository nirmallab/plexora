#!/usr/bin/env node
// Generates the browser plugin API reference (content/docs/plugin-api/browser)
// from the JSDoc @typedef blocks in the client's JavaScript.
//
//   node scripts/generate_browser_api.mjs          write the pages
//   node scripts/generate_browser_api.mjs --check  fail if they are stale
//
// The Python generator (tools/docs/sync_docs.py) owns every other reference
// page; this one reads JavaScript, so Node owns it. Only typedefs listed in
// ORDER are published, in that order.
import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const repo = resolve(here, '..', '..');
const outDir = resolve(here, '..', 'content', 'docs', 'plugin-api', 'browser');
const SOURCES = ['plexora/client/src/js/pluginRegistry.js', 'plexora/client/src/js/main.js'];
const ORDER = ['PluginDefinition', 'PluginHelp', 'SidebarController', 'PluginContext', 'PluginLayerApi', 'LayerRecord'];

function kebab(name) {
  return name.replace(/([a-z0-9])([A-Z])/g, '$1-$2').replace(/([A-Z])([A-Z][a-z])/g, '$1-$2').toLowerCase();
}

/** Escape MDX-significant characters outside inline code. */
function escape(text) {
  return text
    .split(/(`[^`]*`)/g)
    .map((part, i) => (i % 2 ? part : part.replace(/[{}<]/g, (c) => `\\${c}`)))
    .join('');
}

function linkTypes(text, names, self) {
  return text.replace(/`([A-Za-z]+)`/g, (m, name) =>
    names.has(name) && name !== self ? `[\`${name}\`](/docs/plugin-api/browser/${kebab(name)})` : m,
  );
}

/** Read `{...}` with balanced braces starting at s[i] === '{'. */
function braced(s, i) {
  let depth = 0;
  for (let j = i; j < s.length; j++) {
    if (s[j] === '{') depth++;
    else if (s[j] === '}' && --depth === 0) return [s.slice(i + 1, j), j + 1];
  }
  throw new Error(`unbalanced type expression near: ${s.slice(i, i + 60)}`);
}

function parseBlock(body, file, line) {
  const lines = body.split('\n').map((l) => l.replace(/^\s*\* ?/, ''));
  const text = lines.join('\n');
  const typedef = /@typedef\s+\{Object\}\s+(\w+)/.exec(text);
  if (!typedef) return null;
  const description = text.slice(0, text.search(/^@/m)).trim();
  const properties = [];
  const tagChunks = text.slice(text.search(/^@/m)).split(/^(?=@)/m);
  for (const chunk of tagChunks) {
    if (!chunk.startsWith('@property')) continue;
    let rest = chunk.slice('@property'.length).trimStart();
    let type = '';
    if (rest.startsWith('{')) {
      const [t, end] = braced(rest, 0);
      type = t.trim();
      rest = rest.slice(end).trimStart();
    }
    const m = /^(\[?)([\w.]+)\]?\s*(?:-\s*)?([\s\S]*)$/.exec(rest);
    if (!m) throw new Error(`${file}:${line}: cannot parse @property: ${chunk.slice(0, 80)}`);
    const desc = m[3].replace(/^\s*-\s*/, '').replace(/\s+/g, ' ').trim();
    properties.push({ name: m[2], optional: m[1] === '[', type, description: desc });
  }
  return { name: typedef[1], description, properties, file, line };
}

function collect() {
  const found = new Map();
  for (const rel of SOURCES) {
    const source = readFileSync(join(repo, rel), 'utf8');
    for (const match of source.matchAll(/\/\*\*([\s\S]*?)\*\//g)) {
      const line = source.slice(0, match.index).split('\n').length;
      const block = parseBlock(match[1], rel, line);
      if (block) found.set(block.name, block);
    }
  }
  for (const name of ORDER) {
    if (!found.has(name)) throw new Error(`typedef ${name} not found in ${SOURCES.join(', ')}`);
  }
  return found;
}

function json(value) {
  return `{${JSON.stringify(value)}}`;
}

function render(block, names) {
  const [summary, ...rest] = block.description.split(/\n\s*\n/);
  const oneLine = summary.replace(/\s+/g, ' ').trim();
  const front = [
    '---',
    `title: ${JSON.stringify(block.name)}`,
    `description: ${JSON.stringify(oneLine.replace(/`/g, ''))}`,
    'generated: true',
    `symbol: ${JSON.stringify(`js:${block.name}`)}`,
    'source:',
    `  path: ${JSON.stringify(block.file)}`,
    `  start: ${block.line}`,
    '---',
  ].join('\n');
  const parts = [
    front,
    `{/* GENERATED from the JSDoc @typedef in ${block.file}. Run: npm run generate:browser-api --prefix website */}`,
  ];
  if (rest.length) parts.push(linkTypes(escape(rest.join('\n\n').trim()), names, block.name));
  const rows = block.properties.map((p) => {
    const props = [`name=${json(p.name)}`];
    if (p.type) props.push(`type=${json(p.type)}`);
    if (!p.optional) props.push('required');
    const desc = linkTypes(escape(p.description), names, block.name);
    return desc ? `<Param ${props.join(' ')}>\n\n${desc}\n\n</Param>` : `<Param ${props.join(' ')} />`;
  });
  if (rows.length) parts.push(`## Properties\n\n<ParamTable>\n\n${rows.join('\n\n')}\n\n</ParamTable>`);
  return parts.join('\n\n') + '\n';
}

function renderIndex(blocks) {
  const cards = blocks
    .map((b) => {
      const summary = b.description.split(/\n\s*\n/)[0].replace(/\s+/g, ' ').replace(/`/g, '').trim();
      return `<Card title=${json(b.name)} href=${json(`/docs/plugin-api/browser/${kebab(b.name)}`)} description=${json(summary)} />`;
    })
    .join('\n');
  return `---
title: "Browser API"
description: "What a plugin's JavaScript registers with the viewer, and the context every hook receives."
generated: true
---

{/* GENERATED from the JSDoc typedefs in the client's JavaScript. Run: npm run generate:browser-api --prefix website */}

A plugin's browser script calls \`Plexora.registerPlugin(definition)\` when it loads. The viewer activates it when the tool opens and passes every hook a context object. The pages below are generated from the type definitions in the viewer's own source, so they describe exactly what the running version provides.

\`\`\`javascript
Plexora.registerPlugin({
  name: "my_tool",
  createInstance(ctx) {
    return { markers: ctx.dataset.markers };
  },
  createSidebarController(ctx) {
    return {
      setup() {
        ctx.sidebar.textContent = \`\${ctx.sample.name}: \${ctx.columns.length} columns\`;
      },
    };
  },
});
\`\`\`

<Cards>
${cards}
</Cards>
`;
}

const blocks = collect();
const names = new Set(ORDER);
const files = new Map();
const ordered = ORDER.map((n) => blocks.get(n));
files.set('index.mdx', renderIndex(ordered));
files.set('meta.json', JSON.stringify({ title: 'Browser API', pages: ORDER.map(kebab) }, null, 2) + '\n');
for (const block of ordered) files.set(`${kebab(block.name)}.mdx`, render(block, names));

if (process.argv.includes('--check')) {
  const stale = [];
  for (const [name, text] of files) {
    const path = join(outDir, name);
    if (!existsSync(path) || readFileSync(path, 'utf8') !== text) stale.push(name);
  }
  if (existsSync(outDir)) {
    for (const name of readdirSync(outDir)) if (!files.has(name)) stale.push(`${name} (no longer generated)`);
  }
  if (stale.length) {
    console.error(`generate_browser_api: out of date: ${stale.join(', ')}\nRun: npm run generate:browser-api --prefix website`);
    process.exit(1);
  }
  console.log(`generate_browser_api: ${files.size} files up to date.`);
} else {
  if (existsSync(outDir)) rmSync(outDir, { recursive: true });
  mkdirSync(outDir, { recursive: true });
  for (const [name, text] of files) writeFileSync(join(outDir, name), text);
  console.log(`generate_browser_api: wrote ${files.size} files to content/docs/plugin-api/browser.`);
}
