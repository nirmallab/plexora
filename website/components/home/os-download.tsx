'use client';

import Link from 'next/link';
import { Download } from 'lucide-react';
import { useEffect, useState } from 'react';
import { type Download as DownloadInfo, downloadHref } from '@/lib/site';

/** The hero's main button: "Download for macOS" (or Windows, or Linux) when
 * the visitor's system has an installer, otherwise a jump to all three. The
 * server renders the jump; the system is only known in the browser. An
 * installer with an install page opens that page, which starts the download. */
export function OsDownloadButton({ downloads }: { downloads: DownloadInfo[] }) {
  const [match, setMatch] = useState<DownloadInfo | null>(null);

  useEffect(() => {
    const nav = navigator as Navigator & { userAgentData?: { platform?: string } };
    const platform = `${nav.userAgentData?.platform ?? ''} ${nav.platform ?? ''} ${nav.userAgent}`.toLowerCase();
    let os: DownloadInfo['os'] | null = null;
    if (/iphone|ipad|android/.test(platform)) os = null;
    else if (/mac/.test(platform)) os = 'macOS';
    else if (/win/.test(platform)) os = 'Windows';
    else if (/linux|x11/.test(platform)) os = 'Linux';
    setMatch(downloads.find((d) => d.os === os) ?? null);
  }, [downloads]);

  const label = (
    <>
      <Download className="size-4" aria-hidden="true" />
      {match ? `Download for ${match.os}` : 'Download Plexora'}
    </>
  );
  const className = 'px-button px-button-primary px-button-lg';
  if (match?.guide)
    return (
      <Link className={className} href={downloadHref(match)}>
        {label}
      </Link>
    );
  return (
    <a className={className} href={match ? match.url : '#download'} {...(match ? { download: '' } : {})}>
      {label}
    </a>
  );
}
