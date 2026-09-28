import { asset } from '@/lib/site';

/** A screenshot from public/screenshots, prefixed with basePath. */
export function Screenshot({ src, alt, caption, width }: { src: string; alt: string; caption?: string; width?: number }) {
  return (
    <figure className="my-6">
      <img
        src={asset(src)}
        alt={alt}
        loading="lazy"
        className="rounded-xl border border-fd-border"
        style={width ? { maxWidth: width, width: '100%' } : undefined}
      />
      {caption ? <figcaption className="mt-2 text-center text-sm text-fd-muted-foreground">{caption}</figcaption> : null}
    </figure>
  );
}
