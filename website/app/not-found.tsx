import Link from 'next/link';
import { HomeLayout } from 'fumadocs-ui/layouts/home';
import { baseOptions } from '@/lib/layout.shared';

export default function NotFound() {
  return (
    <HomeLayout {...baseOptions()}>
      <main className="mx-auto flex max-w-xl flex-1 flex-col items-start justify-center gap-4 px-4 py-24">
        <p className="text-xs font-bold uppercase tracking-wide text-fd-primary">404</p>
        <h1 className="text-2xl font-bold">This page does not exist</h1>
        <p className="text-fd-muted-foreground">
          The documentation was reorganised in October 2026 and most old addresses lead to their new page, but not
          this one. Search with the box in the top bar (⌘K or Ctrl+K), or start from the documentation home.
        </p>
        <div className="flex flex-wrap gap-3">
          <Link href="/docs" className="px-button px-button-primary">
            Documentation home
          </Link>
          <Link href="/" className="px-button px-button-secondary">
            Download Plexora
          </Link>
        </div>
      </main>
    </HomeLayout>
  );
}
