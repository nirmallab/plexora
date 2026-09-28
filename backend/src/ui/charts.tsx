/** @jsxImportSource hono/jsx */
/**
 * Inline SVG charts: no library, no external asset, no style attributes (the
 * CSP pins one stylesheet by hash). Colours come from classes, which follow
 * the theme tokens.
 */

export function Bars(props: { values: { label: string; value: number }[]; height?: number; width?: number; alt?: boolean }) {
  // A wide viewBox keeps 10-unit labels near 16 px on a full-width card.
  const width = props.width ?? 1000;
  const height = props.height ?? 120;
  const max = Math.max(1, ...props.values.map((v) => v.value));
  const step = width / Math.max(1, props.values.length);
  const bar = Math.min(40, Math.max(1, step * 0.8));
  return (
    <svg class="chart" viewBox={`0 0 ${width} ${height + 16}`} role="img" aria-label="bar chart">
      <line class="axis" x1="0" x2={width} y1={height} y2={height} />
      {props.values.map((v, i) => {
        const h = (v.value / max) * (height - 4);
        return (
          <rect class={props.alt ? 'bar alt' : 'bar'} x={i * step + (step - bar) / 2} y={height - h} width={bar} height={h}>
            <title>{`${v.label}: ${v.value}`}</title>
          </rect>
        );
      })}
      {props.values.length > 0 ? (
        <>
          <text x="0" y={height + 12}>
            {props.values[0]!.label}
          </text>
          <text x={width} y={height + 12} text-anchor="end">
            {props.values[props.values.length - 1]!.label}
          </text>
        </>
      ) : null}
    </svg>
  );
}

/** A used/share meter with threshold ticks (fractions of the share). */
export function Meter(props: { used: number; share: number; ticks: number[] }) {
  const width = 300;
  const ratio = props.share > 0 ? Math.min(1, props.used / props.share) : 0;
  return (
    <svg class="chart" viewBox={`0 0 ${width} 14`} role="img" aria-label={`${Math.round(ratio * 100)}% of share`}>
      <rect class="track" x="0" y="3" width={width} height="8" rx="4" />
      <rect class="fillbar" x="0" y="3" width={ratio * width} height="8" rx="4" />
      {props.ticks.map((t) => (
        <line class="tick" x1={t * width} x2={t * width} y1="0" y2="14" />
      ))}
    </svg>
  );
}

/**
 * A ratio per day, drawn against the share: full height is 100 % of it, and
 * the budget thresholds are faint lines, so a quiet day looks quiet instead of
 * being stretched to fill the card.
 */
export function Sparkline(props: { values: number[]; labels?: string[]; height?: number; ticks?: number[] }) {
  const width = 1000;
  const height = props.height ?? 60;
  const ceiling = Math.max(1, ...props.values);
  // Fourteen slots whatever the data, so two days of history do not fill the card.
  const step = width / Math.max(14, props.values.length);
  return (
    <svg class="chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="daily ratio of the share">
      {(props.ticks ?? []).map((t) => {
        const y = height - (t / ceiling) * (height - 2);
        return <line class="axis" x1="0" x2={width} y1={y} y2={y} stroke-dasharray="4 8" />;
      })}
      <line class="axis" x1="0" x2={width} y1={height} y2={height} />
      {props.values.map((v, i) => {
        const h = Math.max(v > 0 ? 1 : 0, (v / ceiling) * (height - 2));
        return (
          <rect class="bar" x={i * step + 2} y={height - h} width={Math.max(1, step - 4)} height={h}>
            <title>{`${props.labels?.[i] ?? ''} ${(v * 100).toFixed(1)}% of the share`.trim()}</title>
          </rect>
        );
      })}
    </svg>
  );
}
