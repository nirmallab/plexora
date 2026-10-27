import { DownloadCard } from '@/components/download-card';
import { downloads } from '@/lib/site';

/** The three installers from the latest release, as on the home page. */
export function Downloads() {
  return (
    <div className="not-prose my-6 grid gap-4 md:grid-cols-3">
      {downloads.map((d) => (
        <DownloadCard key={d.key} download={d} />
      ))}
    </div>
  );
}
