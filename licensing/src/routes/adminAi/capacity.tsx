/** @jsxImportSource hono/jsx */
/**
 * The Overview's Capacity card: each limit that would make users wait, graded
 * ok / watch / act by src/ai/capacity.ts, with what lifts it. Meters are
 * native <meter> elements because the admin CSP refuses inline styles.
 */
import type { Assessment, Level, Signal } from '../../ai/capacity';
import { Badge, Card, Note, Table } from '../../ui/components';
import { API, BASE } from './shared';

const LEVEL_TONE: Record<Level, 'ok' | 'warn' | 'bad' | 'plain'> = { ok: 'ok', watch: 'warn', act: 'bad', unknown: 'plain' };
const LEVEL_LABEL: Record<Level, string> = { ok: 'ok', watch: 'watch', act: 'act now', unknown: 'unknown' };

function amount(value: number | null, unit: string): string {
  if (value === null) return '—';
  if (unit === 'bytes') {
    return value >= 1e9 ? `${(value / 1e9).toFixed(1)} GB` : value >= 1e6 ? `${(value / 1e6).toFixed(1)} MB`
      : `${Math.round(value / 1e3)} kB`;
  }
  if (unit === 'ms') return `${value} ms`;
  const n = value >= 1e9 ? `${(value / 1e9).toFixed(1)}B` : value >= 1e6 ? `${(value / 1e6).toFixed(1)}M`
    : Math.round(value).toLocaleString('en-US');
  return n;
}

function Meter(props: { s: Signal }) {
  const { value, limit } = props.s;
  if (value === null || !limit) return <span class="muted">—</span>;
  const pct = Math.min(100, (value / limit) * 100);
  return (
    <div class="meter-row">
      <meter class="capacity" min={0} max={100} low={50} high={80} optimum={0} value={pct.toFixed(1)}
        aria-label={props.s.title} title={`${pct.toFixed(1)}% of the limit`} />
      <span class="small nowrap">{pct < 1 && pct > 0 ? '<1' : Math.round(pct)}%</span>
    </div>
  );
}

export function CapacityCard(props: { a: Assessment }) {
  const { a } = props;
  const sub = <>Busiest day and minute of the last 7 days against Workers {a.plan === 'paid' ? 'Paid' : 'Free'};
    watch at 50%, act at 80% (<a href={`${BASE}/settings`}>Settings</a> › Capacity).</>;
  return (
    <Card id="capacity" tight title={<>{a.headline} <Badge tone={LEVEL_TONE[a.level]}>{LEVEL_LABEL[a.level]}</Badge></>}
      sub={sub}>
      {a.move_to_paid ? (
        <Note warn>A Workers Free limit is above 80%. At 100% Cloudflare stops serving every Worker on the account,
          licences included, until 00:00 UTC. Upgrade at dash.cloudflare.com, Workers &amp; Pages, Plans, then switch
          "Cloudflare account is on Workers Paid" on in <a href={`${BASE}/settings`}>Settings</a>.</Note>
      ) : null}
      {a.analytics.configured ? null : (
        <Note>Cloudflare figures are estimates from AI calls alone. Set <span class="mono">CF_ANALYTICS_TOKEN</span> (a
          token with only Account Analytics: Read) to measure the whole account, the telemetry Worker and CPU time
          included.</Note>
      )}
      {a.analytics.errors.length ? (
        <Note warn>Cloudflare analytics answered with errors, so those rows are estimated:
          {' '}{a.analytics.errors.join(' · ')}</Note>
      ) : null}
      <Table head={['Limit', 'Now', 'Of', 'Use', 'What to do']} right={[1, 2]} class="dense tight">
        {a.signals.map((s) => (
          <tr>
            <td>{s.title}<div class="sub">{s.group} · {s.source}</div></td>
            <td class="right nowrap">{amount(s.value, s.unit)}</td>
            <td class="right nowrap">{s.limit === null ? '—' : amount(s.limit, s.unit)}</td>
            <td><Meter s={s} /></td>
            <td><Badge tone={LEVEL_TONE[s.level]}>{LEVEL_LABEL[s.level]}</Badge>
              <div class="sub">{s.action}</div><div class="sub small muted">{s.detail}</div></td>
          </tr>
        ))}
      </Table>
      <p class="hint">As JSON: <span class="mono">GET {API}/capacity</span>.</p>
    </Card>
  );
}
