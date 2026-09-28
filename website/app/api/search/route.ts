import { source } from '@/lib/source';
import { createFromSource } from 'fumadocs-core/search/server';

export const dynamic = 'force-static';
export const revalidate = false;

// Pages may list `aliases` in frontmatter -- the words a reader might use
// instead of the page's own ("mask" for segmentation, "gating" for
// Thresholding). They are indexed with the description so search finds them.
export const { staticGET: GET } = createFromSource(source, {
  language: 'english',
  buildIndex: async (page) => {
    const aliases = page.data.aliases?.length ? ` Also known as: ${page.data.aliases.join(', ')}.` : '';
    return {
      title: page.data.title,
      description: `${page.data.description ?? ''}${aliases}`.trim() || undefined,
      url: page.url,
      id: page.url,
      structuredData: page.data.structuredData,
    };
  },
});
