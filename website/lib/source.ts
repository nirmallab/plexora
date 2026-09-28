import { llms, loader } from 'fumadocs-core/source';
import { lucideIconsPlugin } from 'fumadocs-core/source/plugins/lucide-icons';
import { metaSchema, pageSchema } from 'fumadocs-core/source/schema';
import { defineDocs } from 'fumadocs-mdx/macro';
import { z } from 'zod';

// Frontmatter beyond Fumadocs' own (title, description, icon, full).
// Generated reference pages set `generated` and `source`; `signature` is
// checked against the live object by tests/test_docs_signatures.py.
const docs = defineDocs({
  dir: 'content/docs',
  docs: {
    schema: pageSchema.extend({
      badge: z.string().optional(),
      aliases: z.array(z.string()).optional(),
      related: z.array(z.string()).optional(),
      generated: z.boolean().optional(),
      symbol: z.string().optional(),
      signature: z.string().optional(),
      source: z
        .object({ path: z.string(), start: z.number().optional(), end: z.number().optional() })
        .optional(),
    }),
    postprocess: {
      includeProcessedMarkdown: true,
    },
  },
  meta: {
    schema: metaSchema,
  },
});

// baseUrl is the in-app route prefix only; Next applies basePath underneath.
export const source = loader({
  baseUrl: '/docs',
  source: docs.toFumadocsSource(),
  plugins: [lucideIconsPlugin()],
});

export const docsLlms = llms(source, {
  renderPage: async (page) => `# ${page.data.title} (${page.url})

${await page.data.getText('processed')}`,
});
