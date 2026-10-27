'use client';

import { useId } from 'react';
import { siteName } from '@/lib/site';

/** Plexora's app icon: two translucent bars crossing, cyan through indigo to
 * magenta. Same geometry as biocognia-platform's PlexoraMark. Each copy gets
 * its own gradient ids: the layout renders the logo twice (top bar and
 * sidebar), and a copy whose gradients live in the hidden one draws nothing. */
export function PlexoraMark({ size = 20 }: { size?: number }) {
  const id = useId().replace(/:/g, '');
  const a = `pxa-${id}`;
  const b = `pxb-${id}`;
  return (
    <svg width={size} height={size} viewBox="0 -5 79 79" aria-hidden="true" focusable="false">
      <defs>
        <linearGradient id={a} gradientUnits="userSpaceOnUse" x1="2" y1="0" x2="77" y2="69">
          <stop offset="0" stopColor="#34c8f2" />
          <stop offset=".25" stopColor="#2fa9e8" />
          <stop offset=".55" stopColor="#4350ab" />
          <stop offset="1" stopColor="#c2589f" />
        </linearGradient>
        <linearGradient id={b} gradientUnits="userSpaceOnUse" x1="77" y1="0" x2="2" y2="69">
          <stop offset="0" stopColor="#8f4fa6" />
          <stop offset=".5" stopColor="#4350ab" />
          <stop offset="1" stopColor="#3a6fd6" />
        </linearGradient>
      </defs>
      <path
        fill={`url(#${b})`}
        opacity=".92"
        d="M77.5 6.15Q77.5 0 71.35 0Q65.2 0 60.16 3.53L8.87 39.44Q1.5 44.6 1.5 53.6L1.5 62.9Q1.5 69 7.6 69Q13.7 69 18.7 65.5L70.13 29.51Q77.5 24.35 77.5 15.35Z"
      />
      <path
        fill={`url(#${a})`}
        opacity=".86"
        d="M1.5 6.15Q1.5 0 7.65 0Q13.8 0 18.84 3.53L70.13 39.44Q77.5 44.6 77.5 53.6L77.5 62.9Q77.5 69 71.4 69Q65.3 69 60.3 65.5L8.87 29.51Q1.5 24.35 1.5 15.35Z"
      />
    </svg>
  );
}

export function Logo() {
  return (
    <span className="inline-flex items-center gap-2 font-semibold">
      <PlexoraMark size={22} />
      {siteName}
    </span>
  );
}
