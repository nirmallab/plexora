import { ImageZoom } from 'fumadocs-ui/components/image-zoom';
import { asset } from '@/lib/site';
import { MediaVideo } from './media-video';

/** A box drawn over the media, in the source's own pixels (a 2000×1250
 * screenshot measures 0..2000 across). `label` puts a numbered badge on it. */
export type Highlight = { x: number; y: number; w: number; h: number; label?: string | number };

export type MediaProps = {
  /** image: a still (zooms on click). animation: a GIF or animated WebP.
   * video: an MP4 or WebM that plays muted while on screen. */
  kind?: 'image' | 'animation' | 'video';
  src: string;
  alt: string;
  /** Video only: the still shown before it plays. */
  poster?: string;
  caption?: string;
  width?: number;
  height?: number;
  highlights?: Highlight[];
  zoom?: boolean;
  autoplay?: boolean;
  /** Load at once instead of when scrolled near (the first thing on a page). */
  eager?: boolean;
};

const url = (src: string) => (/^https?:\/\//.test(src) ? src : asset(src));

/** Every screenshot, animation and video on the site. All three kinds sit in
 * the same box (the source's aspect ratio, full column width), so replacing a
 * screenshot with a clip later is a change of `kind`, `src` and `poster`. */
export function Media({
  kind = 'image',
  src,
  alt,
  poster,
  caption,
  width = 2000,
  height = 1250,
  highlights,
  zoom = true,
  autoplay = true,
  eager = false,
}: MediaProps) {
  const loading = eager ? 'eager' : 'lazy';
  let body;
  if (kind === 'video') {
    body = <MediaVideo src={url(src)} poster={poster ? url(poster) : undefined} label={alt} autoplay={autoplay} eager={eager} />;
  } else if (kind === 'image' && zoom) {
    body = <ImageZoom src={url(src)} alt={alt} width={width} height={height} loading={loading} decoding="async" />;
  } else {
    body = <img src={url(src)} alt={alt} width={width} height={height} loading={loading} decoding="async" />;
  }

  return (
    <figure className="px-media not-prose">
      <div className="px-media-frame" style={{ aspectRatio: `${width} / ${height}` }}>
        {body}
        {highlights?.map((h, i) => (
          <span
            key={i}
            className="px-media-mark"
            aria-hidden="true"
            style={{
              left: `${(h.x / width) * 100}%`,
              top: `${(h.y / height) * 100}%`,
              width: `${(h.w / width) * 100}%`,
              height: `${(h.h / height) * 100}%`,
            }}
          >
            {h.label !== undefined ? <span className="px-media-badge">{h.label}</span> : null}
          </span>
        ))}
      </div>
      {caption ? <figcaption>{caption}</figcaption> : null}
    </figure>
  );
}
