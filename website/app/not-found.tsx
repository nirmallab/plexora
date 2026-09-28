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
          It may have moved when the documentation was reorganised. Search with the button in the top bar, or start
          from the documentation home.
        </p>
        <Link href="/docs" className="rounded-lg bg-fd-primary px-4 py-2 text-sm font-semibold text-fd-primary-foreground">
          Documentation home
        </Link>
      </main>
    </HomeLayout>
  );
}
