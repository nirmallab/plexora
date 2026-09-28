/** @jsxImportSource hono/jsx */
/**
 * Inline SVG charts: no library, no external asset, no style attributes (the
 * CSP pins one stylesheet by hash). Geometry is SVG attributes; colours are
 * classes, which follow the theme tokens. Labels are HTML under the chart, so
 * they stay legible however wide the card is.
 */
import type { Child } from 'hono/jsx';

/**
 * One bar per value. `ends` puts the first and last labels under the chart
 * (a time series); `bands` puts every label under its own bar (a histogram).
 */
export function Bars(props: { values: { label: string; value: number }[]; label: string; alt?: boolean;
  tall?: boolean; bands?: boolean; format?: (n: number) => string }) {
  const width = 10;
  const height = 64;
  const peak = Math.max(1, ...props.values.map((v) => v.value));
  const format = props.format ?? ((n: number) => String(n));
  const count = Math.max(1, props.values.length);
  return (
    <>
      <svg class={props.tall ? 'bars tall' : 'bars'} viewBox={`0 0 ${count * width} ${height}`} preserveAspectRatio="none"
        role="img" aria-label={props.label}>
        <line x1="0" x2={String(count * width)} y1={String(height / 2)} y2={String(height / 2)} />
        {props.values.map((v, i) => {
          const h = Math.max(1.5, Math.round((v.value / peak) * (height - 2)));
          const cls = [props.alt ? 'alt' : '', v.value > 0 ? 'on' : ''].filter(Boolean).join(' ');
          return (
            <rect x={String(i * width + 1)} y={String(height - h)} width={String(width - 2)} height={String(h)} rx="1"
              class={cls || undefined}>
              <title>{`${v.label}: ${format(v.value)}`}</title>
            </rect>
          );
        })}
      </svg>
      {props.values.length === 0 ? null : props.bands ? (
        <div class="axis bands">{props.values.map((v) => <span title={v.label}>{v.label}</span>)}</div>
      ) : (
        <div class="axis">
          <span>{props.values[0]!.label}</span>
          <span>peak {format(Math.max(0, ...props.values.map((v) => v.value)))}</span>
          <span>{props.values[props.values.length - 1]!.label}</span>
        </div>
      )}
    </>
  );
}

/** A 9-bin histogram for a table cell; the bins' labels are in each title. */
export function Hist(props: { bins: number[]; labels: string[] }) {
  const peak = Math.max(1, ...props.bins);
  const total = props.bins.reduce((a, b) => a + b, 0);
  return (
    <svg class="hist" viewBox={`0 0 ${props.bins.length * 10} 18`} preserveAspectRatio="none" role="img"
      aria-label={`distribution: ${props.bins.join(', ')}`}>
      {props.bins.map((n, i) => {
        const h = n > 0 ? Math.max(2, Math.round((n / peak) * 18)) : 1.5;
        return (
          <rect x={String(i * 10 + 1)} y={String(18 - h)} width="8" height={String(h)} class={n > 0 ? undefined : 'z'}>
            <title>{`${props.labels[i] ?? i}: ${n}${total ? ` (${Math.round((n / total) * 100)}%)` : ''}`}</title>
          </rect>
        );
      })}
    </svg>
  );
}

export function ShareBar(props: { value: number; max: number }) {
  const w = props.max > 0 ? Math.max(0, Math.min(100, (props.value / props.max) * 100)) : 0;
  return (
    <svg class="share" viewBox="0 0 100 5" preserveAspectRatio="none" aria-hidden="true">
      <rect class="track" x="0" y="0" width="100" height="5" rx="2.5" />
      <rect x="0" y="0" width={w.toFixed(2)} height="5" rx="2.5" />
    </svg>
  );
}

/** A used/share meter with threshold ticks (fractions of the share). */
export function Meter(props: { used: number; share: number; ticks: number[]; tone?: 'warn' | 'bad'; label?: Child }) {
  const width = 300;
  const ratio = props.share > 0 ? Math.min(1, props.used / props.share) : 0;
  return (
    <svg class="meter" viewBox={`0 0 ${width} 12`} preserveAspectRatio="none" role="img"
      aria-label={`${Math.round(ratio * 100)}% of share`}>
      <rect class="track" x="0.5" y="2.5" width={String(width - 1)} height="7" rx="3.5" />
      <rect class={props.tone ? `fill ${props.tone}` : 'fill'} x="0.5" y="2.5"
        width={String(Math.max(ratio > 0 ? 2 : 0, ratio * (width - 1)))} height="7" rx="3.5" />
      {props.ticks.filter((t) => t > 0 && t < 1).map((t) => (
        <line class="tick" x1={String(t * width)} x2={String(t * width)} y1="0" y2="12" />
      ))}
    </svg>
  );
}

/**
 * A ratio per day, drawn against the share: full height is 100 % of it (or the
 * highest day, when a day went over), and the budget thresholds are faint
 * lines, so a quiet fortnight looks quiet instead of being stretched to fill
 * the card.
 */
export function Sparkline(props: { values: number[]; labels: string[]; ticks?: number[] }) {
  const width = 10;
  const height = 64;
  const ceiling = Math.max(1, ...props.values);
  // Fourteen slots whatever the data, so two days of history do not fill the card.
  const count = Math.max(14, props.values.length);
  return (
    <>
      <svg class="bars tall" viewBox={`0 0 ${count * width} ${height}`} preserveAspectRatio="none" role="img"
        aria-label="daily ratio of the share">
        {(props.ticks ?? []).map((t) => {
          const y = height - (t / ceiling) * (height - 2);
          return <line x1="0" x2={String(count * width)} y1={y.toFixed(1)} y2={y.toFixed(1)} />;
        })}
        {props.values.map((v, i) => {
          const h = Math.max(v > 0 ? 1.5 : 1, (v / ceiling) * (height - 2));
          return (
            <rect x={String(i * width + 1)} y={(height - h).toFixed(1)} width={String(width - 2)} height={h.toFixed(1)}
              rx="1" class={v > 0 ? 'on' : undefined}>
              <title>{`${props.labels[i] ?? ''} ${(v * 100).toFixed(1)}% of the share`.trim()}</title>
            </rect>
          );
        })}
      </svg>
      {props.values.length ? (
        <div class="axis"><span>{props.labels[0]}</span><span>dashed: 50 / 70 / 85 / 95 %</span>
          <span>{props.labels[props.labels.length - 1]}</span></div>
      ) : null}
    </>
  );
}
