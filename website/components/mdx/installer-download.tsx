'use client';

import { CircleCheck, Download } from 'lucide-react';
import { useEffect, useState } from 'react';
import { type Download as DownloadInfo, downloads, formatBytes, startDownloadParam } from '@/lib/site';

/** The top of an install page. A download button elsewhere on the site sends
 * the reader here with ?download=1: the installer starts downloading and this
 * says so, with the steps below it. Reached any other way, it is a plain
 * download button. */
export function InstallerDownload({ os }: { os: DownloadInfo['os'] }) {
  const d = downloads.find((x) => x.os === os);
  const [started, setStarted] = useState(false);

  useEffect(() => {
    if (!d) return;
    const url = new URL(window.location.href);
    if (!url.searchParams.has(startDownloadParam)) return;
    // Drop the flag first, so a reload or Back does not download it again
    // (and React's second effect run in development finds nothing to do).
    url.searchParams.delete(startDownloadParam);
    window.history.replaceState(window.history.state, '', url);
    setStarted(true);
    // GitHub serves release assets as attachments: the browser saves the
    // file and this page stays put.
    window.location.assign(d.url);
  }, [d]);

  if (!d) return null;
  const file = `${d.name} · ${formatBytes(d.size)}`;
  return (
    <div className="px-card not-prose my-6 flex flex-wrap items-center gap-x-5 gap-y-3 p-5" role="status">
      {started ? (
        <>
          <CircleCheck className="size-6 shrink-0 text-fd-primary" aria-hidden="true" />
          <div className="min-w-0 flex-1">
            <p className="font-semibold text-fd-foreground">Your download has started</p>
            <p className="text-sm text-fd-muted-foreground [overflow-wrap:anywhere]">{file}</p>
          </div>
          <a className="px-inline-link text-sm" href={d.url}>
            Download again
          </a>
        </>
      ) : (
        <>
          <a className="px-button px-button-primary" href={d.url} download>
            <Download className="size-4" aria-hidden="true" />
            {d.label}
          </a>
          <p className="w-full text-sm text-fd-muted-foreground [overflow-wrap:anywhere] sm:w-auto sm:min-w-0 sm:flex-1">
            v{d.version} · {file}
          </p>
        </>
      )}
    </div>
  );
}
