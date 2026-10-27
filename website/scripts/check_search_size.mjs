#!/usr/bin/env node
// Fails the build when the exported search index grows past a budget. Every
// reader downloads it on their first search, so it stays small: generated
// reference pages are indexed by title, description and headings only.
//
//   node scripts/check_search_size.mjs [outDir]
import { existsSync, statSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const LIMIT = 2 * 1024 * 1024;
const here = dirname(fileURLToPath(import.meta.url));
const outDir = resolve(process.argv[2] ?? join(here, '..', 'out'));
const index = join(outDir, 'api', 'search');

if (!existsSync(index)) {
  console.error(`check_search_size: ${index} does not exist; run npm run build first.`);
  process.exit(2);
}
const size = statSync(index).size;
const mb = (n) => `${(n / 1024 / 1024).toFixed(2)} MB`;
if (size > LIMIT) {
  console.error(`check_search_size: the search index is ${mb(size)}, over the ${mb(LIMIT)} budget.`);
  process.exit(1);
}
console.log(`check_search_size: ${mb(size)} (budget ${mb(LIMIT)}).`);
