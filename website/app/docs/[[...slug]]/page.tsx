import type { Metadata } from 'next';
import { notFound } from 'next/navigation';
import { DocsBody, DocsDescription, DocsPage, DocsTitle } from 'fumadocs-ui/layouts/docs/page';
import { createRelativeLink } from 'fumadocs-ui/mdx';
import { getMDXComponents } from '@/components/mdx';
import { editUrl, sourceUrl, version } from '@/lib/site';
import { source } from '@/lib/source';

type Params = { slug?: string[] };

export default async function Page(props: { params: Promise<Params> }) {
  const params = await props.params;
  const page = source.getPage(params.slug);
  if (!page) notFound();

  const MDX = page.data.body;
  const { generated, source: src } = page.data;
  // An on-page contents list only where there is something to jump between.
  const longPage = page.data.toc.length >= 4;

  return (
    <DocsPage
      toc={page.data.toc}
      full={page.data.full}
      breadcrumb={{ includeRoot: false }}
      tableOfContent={{ enabled: longPage, style: 'clerk' }}
      tableOfContentPopover={{ enabled: longPage }}
    >
      <DocsTitle>{page.data.title}</DocsTitle>
      <DocsDescription className="mb-0">{page.data.description}</DocsDescription>
      {generated || src ? (
        <div className="flex flex-row flex-wrap items-center gap-3 text-xs text-fd-muted-foreground">
          {generated ? <span className="px-chip">Generated from Plexora {version}</span> : null}
          {src ? (
            <a className="underline-offset-4 hover:text-fd-foreground hover:underline" href={sourceUrl(src.path, src.start, src.end)}>
              View source
            </a>
          ) : null}
        </div>
      ) : null}
      <DocsBody>
        <MDX components={getMDXComponents({ a: createRelativeLink(source, page) })} />
      </DocsBody>
      {!generated ? (
        <a
          className="mt-4 text-xs text-fd-muted-foreground underline-offset-4 hover:text-fd-foreground hover:underline"
          href={editUrl(page.path)}
        >
          Edit this page on GitHub
        </a>
      ) : null}
    </DocsPage>
  );
}

export function generateStaticParams() {
  return source.generateParams();
}

export async function generateMetadata(props: { params: Promise<Params> }): Promise<Metadata> {
  const params = await props.params;
  const page = source.getPage(params.slug);
  if (!page) notFound();

  return {
    title: page.data.title,
    description: page.data.description,
  };
}
