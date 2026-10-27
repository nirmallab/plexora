import Link from 'next/link';
import { ArrowRight } from 'lucide-react';
import { asset } from '@/lib/site';

/** A screenshot with a title and one line, linking into the docs. */
export function ExploreTile({ title, body, href, image }: { title: string; body: string; href: string; image: string }) {
  return (
    <Link href={href} className="px-card group flex flex-col overflow-hidden">
      <div className="aspect-[16/10] overflow-hidden border-b border-fd-border bg-[#05070a]">
        <img
          src={asset(image)}
          alt=""
          width={2000}
          height={1250}
          loading="lazy"
          decoding="async"
          className="size-full object-cover object-left-top transition-transform duration-300 group-hover:scale-[1.02] motion-reduce:transition-none motion-reduce:group-hover:scale-100"
        />
      </div>
      <div className="flex flex-1 flex-col gap-1 p-4">
        <h3 className="flex items-center gap-1.5 font-semibold">
          {title}
          <ArrowRight className="size-4 text-fd-muted-foreground transition-transform group-hover:translate-x-0.5" aria-hidden="true" />
        </h3>
        <p className="text-sm text-fd-muted-foreground">{body}</p>
      </div>
    </Link>
  );
}
