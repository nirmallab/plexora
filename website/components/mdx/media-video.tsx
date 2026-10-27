'use client';

import { useEffect, useRef } from 'react';

/** A muted, looping clip that plays only while at least half of it is on
 * screen, and never by itself when the reader asks for reduced motion. */
export function MediaVideo({
  src,
  poster,
  label,
  autoplay,
  eager,
}: {
  src: string;
  poster?: string;
  label: string;
  autoplay: boolean;
  eager: boolean;
}) {
  const ref = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    const video = ref.current;
    if (!video || !autoplay) return;
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) video.play().catch(() => {});
        else video.pause();
      },
      { threshold: 0.5 },
    );
    observer.observe(video);
    return () => observer.disconnect();
  }, [autoplay]);

  return (
    <video
      ref={ref}
      src={src}
      poster={poster}
      aria-label={label}
      muted
      loop
      playsInline
      controls
      preload={eager ? 'metadata' : 'none'}
    />
  );
}
