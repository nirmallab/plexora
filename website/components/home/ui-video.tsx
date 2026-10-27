import { MediaVideo } from '@/components/mdx/media-video';
import { asset } from '@/lib/site';

/** A clip shown inside Plexora's own interface: a real screenshot of the
 * viewer (sidebar, menu bar) with the video laid over its image area.
 * `canvas` is that area in the screenshot's pixels (2000x1250). */
export function UiVideo({
  frame,
  canvas,
  src,
  poster,
  label,
}: {
  frame: string;
  canvas: { x: number; y: number; w: number; h: number };
  src: string;
  poster?: string;
  label: string;
}) {
  const pct = (v: number, of: number) => `${(v / of) * 100}%`;
  return (
    <div className="px-ui-video">
      <img src={asset(frame)} alt="" width={2000} height={1250} loading="lazy" decoding="async" />
      <div
        className="px-ui-video-canvas"
        style={{ left: pct(canvas.x, 2000), top: pct(canvas.y, 1250), width: pct(canvas.w, 2000), height: pct(canvas.h, 1250) }}
      >
        <MediaVideo src={src} poster={poster} label={label} autoplay eager={false} />
      </div>
    </div>
  );
}
