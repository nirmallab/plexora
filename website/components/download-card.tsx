import { Download } from 'lucide-react';
import { type Download as DownloadInfo, formatBytes } from '@/lib/site';

function OsIcon({ os }: { os: DownloadInfo['os'] }) {
  // Simple, recognisable outlines; no vendor logos.
  if (os === 'macOS')
    return (
      <svg viewBox="0 0 24 24" className="size-7" fill="currentColor" aria-hidden="true">
        <path d="M16.37 12.6c.02 2.43 2.13 3.24 2.15 3.25-.02.06-.34 1.15-1.11 2.28-.67.98-1.36 1.95-2.45 1.97-1.08.02-1.42-.64-2.65-.64s-1.61.62-2.63.66c-1.05.04-1.85-1.06-2.52-2.03-1.38-1.99-2.43-5.62-1.02-8.07.7-1.22 1.95-1.99 3.31-2.01 1.03-.02 2.01.7 2.64.7.63 0 1.82-.86 3.06-.73.52.02 1.98.21 2.92 1.59-.08.05-1.74 1.02-1.72 3.03M14.37 6.6c.56-.68.94-1.62.83-2.56-.81.03-1.79.54-2.37 1.21-.52.6-.98 1.56-.85 2.48.9.07 1.83-.46 2.39-1.13" />
      </svg>
    );
  if (os === 'Windows')
    return (
      <svg viewBox="0 0 24 24" className="size-7" fill="currentColor" aria-hidden="true">
        <path d="M3 5.5 10 4.5v7H3zm8-1.15L21 3v8.5H11zM3 12.5h7v7l-7-1zm8 0h10V21l-10-1.4z" />
      </svg>
    );
  return (
    <svg viewBox="0 0 24 24" className="size-7" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
      <rect x="3" y="4" width="18" height="13" rx="2" />
      <path d="M8 21h8M12 17v4M7 9l2.5 2L7 13M11.5 13H15" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/** One installer from the latest release, with the size GitHub reports. */
export function DownloadCard({ download }: { download: DownloadInfo }) {
  return (
    <div className="px-card flex h-full flex-col gap-4 p-5">
      <div className="flex items-center gap-3">
        <span className="text-fd-foreground">
          <OsIcon os={download.os} />
        </span>
        <div>
          <p className="text-lg font-semibold leading-tight text-fd-foreground">{download.os}</p>
          <p className="text-sm text-fd-muted-foreground">{download.requirement}</p>
        </div>
      </div>
      <a className="px-button px-button-primary mt-auto" href={download.url} download>
        <Download className="size-4" aria-hidden="true" />
        {download.label}
      </a>
      <p className="text-xs text-fd-muted-foreground">
        v{download.version} · {formatBytes(download.size)}
      </p>
    </div>
  );
}
