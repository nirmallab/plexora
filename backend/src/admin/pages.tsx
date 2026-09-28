/** @jsxImportSource hono/jsx */
/**
 * The admin pages. Each takes the JSON a queries.ts function returned, so
 * HTML and JSON views can never disagree about the numbers; anything a page
 * adds (a total, a share, a per-version summary) is arithmetic over that JSON.
 *
 * Every page opens with a row of totals, then compact cards: the answer
 * first, the detail under it, reference material folded.
 */
import type { Child } from 'hono/jsx';

import { addBins, percentile } from '../telemetry/hist';
import type {
  backups,
  budget,
  data,
  errorDetail,
  errors,
  features,
  install,
  performance,
  usage,
} from '../telemetry/queries';
import { VECTORS } from '../telemetry/schema';
import { Bars, Hist, Meter, ShareBar, Sparkline } from '../ui/charts';
import { Badge, Card, Empty, FilterForm, Fold, Mark, Note, SplitList, Stat, Stats, Table, type Tone } from '../ui/components';
import { bytes, day, daysIn, dimLabel, ms, num, pct, share, when } from '../ui/format';
import { Layout } from '../ui/layout';

type R<T extends (...args: never[]) => unknown> = NonNullable<Awaited<ReturnType<T>>>;
type Row = Record<string, any>;

const MS_LABELS = VECTORS.ms_labels as string[];
const sum = (rows: Row[], key: string) => rows.reduce((total, row) => total + Number(row[key] ?? 0), 0);
const binsOf = (row: Row) => Array.from({ length: VECTORS.hist_bins }, (_, i) => Number(row[`b${i}`] ?? 0));

/** "2026-09-01 → 2026-09-30 · 30 days", plus any filter that is set. */
function rangeLede(f: { from: string; to: string; version: string | null; launch_mode: string | null; deployment: string | null }) {
  const set = (['version', 'launch_mode', 'deployment'] as const).filter((k) => f[k]);
  return (
    <>
      {f.from} → {f.to} · {daysIn(f.from, f.to)} days
      {set.map((k) => <> · {k.replace('_', ' ')} <b>{f[k]}</b></>)}
    </>
  );
}

const p5095 = (r: Row) => (r.p50 === null && r.p95 === null ? '–' : <>{ms(r.p50)} <span class="muted">/ {ms(r.p95)}</span></>);

// -- usage ---------------------------------------------------------------------

export function UsagePage(props: { d: R<typeof usage>; who: string }) {
  const d = props.d;
  const days = d.daily.length;
  const sessions = sum(d.daily, 'sessions');
  const errorsTotal = sum(d.daily, 'errors');
  const fresh = sum(d.daily, 'new_installs');
  const installDays = sum(d.daily, 'installs');
  const activeDays = d.daily.filter((r) => r.installs > 0).length;

  // One row per version: how much of the range it carried, and when it was last seen.
  const byVersion = new Map<string, { version: string; installs: number; fresh: number; first: string; last: string; latest: number }>();
  for (const row of d.versions) {
    const v = byVersion.get(row.version) ?? { version: row.version, installs: 0, fresh: 0, first: row.day, last: row.day, latest: 0 };
    v.installs += row.installs;
    v.fresh += row.new_installs;
    if (row.day < v.first) v.first = row.day;
    if (row.day >= v.last) { v.last = row.day; v.latest = row.installs; }
    byVersion.set(row.version, v);
  }
  const versions = [...byVersion.values()].sort((a, b) => b.version.localeCompare(a.version, undefined, { numeric: true }));
  const versionTotal = versions.reduce((t, v) => t + v.installs, 0);
  const peakVersion = Math.max(1, ...versions.map((v) => v.installs));
  const split = (rows: { name: string; installs: number }[]) => rows.map((r) => ({ name: r.name, value: r.installs }));

  return (
    <Layout title="Usage" active="usage" who={props.who} lede={rangeLede(d.filters)}>
      <FilterForm filters={d.filters} />
      <Stats>
        <Stat label="Active today" value={num(d.dau)} sub={`installs on ${d.filters.to}`} />
        <Stat label="Weekly active" value={num(d.wau)} sub="distinct, last 7 days" />
        <Stat label="Monthly active" value={num(d.mau)} sub="distinct, last 30 days" />
        <Stat label="New installs" value={num(fresh)} sub={`in ${days} days`} />
        <Stat label="Sessions" value={num(sessions)} sub={`${(sessions / Math.max(1, days)).toFixed(1)} per day`} />
        <Stat label="Errors" value={num(errorsTotal)} tone={errorsTotal ? 'warn' : undefined}
          sub={sessions ? `${(errorsTotal / sessions).toFixed(2)} per session` : 'no sessions'} />
      </Stats>

      <div class="grid two">
        <Card title="Active installs per day"
          sub={`${(installDays / Math.max(1, days)).toFixed(1)} a day on average · ${activeDays} of ${days} days with any`}>
          <Bars values={d.daily.map((r) => ({ label: r.day, value: r.installs }))} label="Active installs per day" />
        </Card>
        <Card title="Sessions per day" sub={`${num(sessions)} in range`}>
          <Bars values={d.daily.map((r) => ({ label: r.day, value: r.sessions }))} label="Sessions per day" alt />
        </Card>
      </div>

      <div class="grid three">
        <Card title="Launch mode" sub="install-days"><SplitList rows={split(d.launch_mode)} /></Card>
        <Card title="Deployment" sub="install-days"><SplitList rows={split(d.deployment)} /></Card>
        <Card title="Operating system" sub="install-days"><SplitList rows={split(d.os)} /></Card>
      </div>

      <Card title="Versions" sub={`${versions.length} seen in range, newest first`} flush>
        <Table
          columns={[
            { key: 'version', label: 'Version', render: (r: Row) => <span class="mono">{r.version}</span> },
            { key: 'latest', label: `Installs on last day`, num: true, render: (r: Row) => num(r.latest) },
            { key: 'installs', label: 'Install-days', num: true, render: (r: Row) => num(r.installs) },
            { key: 'share', label: 'Share', num: true, render: (r: Row) => share(r.installs, versionTotal) },
            { key: 'bar', label: '', render: (r: Row) => <ShareBar value={r.installs} max={peakVersion} /> },
            { key: 'fresh', label: 'New installs', num: true, render: (r: Row) => num(r.fresh) },
            { key: 'seen', label: 'Seen', render: (r: Row) => <span class="muted small nowrap">{r.first === r.last ? r.first : `${r.first} → ${r.last}`}</span> },
          ]}
          rows={versions as unknown as Row[]}
          empty="No version has reported in this range."
        />
      </Card>
    </Layout>
  );
}

// -- data ----------------------------------------------------------------------

export function DataPage(props: { d: R<typeof data>; who: string }) {
  const d = props.d;
  const rows = d.modalities as Row[];
  const opened = sum(rows, 'n');
  const distributed = sum(rows, 'distributed');
  const byModality = new Map<string, number>();
  for (const r of rows) byModality.set(r.modality ?? '(none)', (byModality.get(r.modality ?? '(none)') ?? 0) + Number(r.n));
  const modalities = [...byModality.entries()].map(([name, value]) => ({ name, value })).sort((a, b) => b.value - a.value);
  const peak = Math.max(1, ...rows.map((r) => Number(r.n)));
  const scale = Object.entries(d.scale);
  return (
    <Layout title="Data" active="data" who={props.who} lede={rangeLede(d.filters)}>
      <FilterForm filters={d.filters} fields={['version']} />
      <Stats>
        <Stat label="Projects opened" value={num(opened)} sub={`${(opened / Math.max(1, daysIn(d.filters.from, d.filters.to))).toFixed(1)} per day`} />
        <Stat label="Top modality" value={modalities[0]?.name ?? '–'} text sub={modalities[0] ? `${share(modalities[0].value, opened)} of opens` : 'nothing opened'} />
        <Stat label="Kinds of project" value={num(rows.length)} sub="modality × image × run × table" />
        <Stat label="Distributed" value={num(distributed)} sub={`${share(distributed, opened)} of opens`} />
      </Stats>

      <div class="grid two">
        <Card title="By modality" sub="projects opened"><SplitList rows={modalities} /></Card>
        <Card title="Kinds of project" sub="what was opened, most first" flush>
          <Table
            capped
            columns={[
              { key: 'modality', label: 'Modality' },
              { key: 'image_kind', label: 'Image' },
              { key: 'run_format', label: 'Run' },
              { key: 'table_kind', label: 'Table' },
              { key: 'n', label: 'Opened', num: true, render: (r: Row) => num(r.n) },
              { key: 'bar', label: '', render: (r: Row) => <ShareBar value={Number(r.n)} max={peak} /> },
              { key: 'installs', label: 'Install-days', num: true, render: (r: Row) => num(r.installs) },
            ]}
            rows={rows}
            empty="No project was opened in this range."
          />
        </Card>
      </div>

      <h3>Scale of what was opened</h3>
      {scale.length === 0 ? <Card><Empty>No scale measurements in this range.</Empty></Card> : (
        <div class="grid three">
          {scale.map(([dim, bands]) => (
            <Card title={dimLabel(dim)} sub={`${num(bands.reduce((t, b) => t + b.n, 0))} projects`}>
              <Bars values={bands.map((b) => ({ label: b.band, value: b.n }))} label={`${dimLabel(dim)} bands`} bands />
            </Card>
          ))}
        </div>
      )}
    </Layout>
  );
}

// -- features ------------------------------------------------------------------

export function FeaturesPage(props: { d: R<typeof features>; who: string }) {
  const d = props.d;
  const plugins = d.plugins as Row[];
  const used = d.features as Row[];
  const functions = d.functions as Row[];
  const caps = d.capabilities as Row[];
  const opens = sum(plugins, 'opens');
  const loadFailed = sum(plugins, 'load_failed');
  const uses = sum(used, 'n');
  const calls = sum(functions, 'n');
  const callErrors = sum(functions, 'err');
  const capCalls = sum(caps, 'ok') + sum(caps, 'err');
  const capErrors = sum(caps, 'err');
  const totalFeatures = used.length + d.unused.length;
  const peakOpens = Math.max(1, ...plugins.map((r) => Number(r.opens)));
  const peakUses = Math.max(1, ...used.map((r) => Number(r.n)));
  const hist = (r: Row) => <Hist bins={binsOf(r)} labels={MS_LABELS} />;
  return (
    <Layout title="Features" active="features" who={props.who} lede={rangeLede(d.filters)}>
      <FilterForm filters={d.filters} fields={['version']} />
      <Stats>
        <Stat label="Tool opens" value={num(opens)} sub={plugins.length === 1 ? '1 tool' : `${plugins.length} tools`} />
        <Stat label="Tool load failures" value={num(loadFailed)} tone={loadFailed ? 'bad' : undefined}
          sub={opens ? `${share(loadFailed, opens)} of opens` : 'no opens'} />
        <Stat label="Feature uses" value={num(uses)} sub={`${used.length} of ${totalFeatures} features used`} />
        <Stat label="Function calls" value={num(calls)} tone={callErrors ? 'warn' : undefined}
          sub={`${num(callErrors)} errors · ${share(callErrors, calls)}`} />
        <Stat label="Agent calls" value={num(capCalls)} tone={capErrors ? 'warn' : undefined}
          sub={`${num(capErrors)} errors · ${share(capErrors, capCalls)}`} />
      </Stats>

      <div class="grid two start">
        <Card title="Tools and plugins" sub="opens, most first" flush>
          <Table
            columns={[
              { key: 'plugin', label: 'Tool' },
              { key: 'opens', label: 'Opens', num: true, render: (r: Row) => num(r.opens) },
              { key: 'bar', label: '', render: (r: Row) => <ShareBar value={Number(r.opens)} max={peakOpens} /> },
              { key: 'load_failed', label: 'Failed', num: true,
                render: (r: Row) => (r.load_failed ? <span class="bad-text">{num(r.load_failed)}</span> : '0') },
              { key: 'installs', label: 'Install-days', num: true, render: (r: Row) => num(r.installs) },
              { key: 'p', label: 'Load p50 / p95', num: true, render: p5095 },
              { key: 'h', label: 'Load time', render: hist },
            ]}
            rows={plugins}
            empty="No tool was opened in this range."
          />
        </Card>
        <Card title="Features" sub="uses, most first" flush
          foot={d.unused.length ? (
            <div class="chips"><span>Unused:</span>{d.unused.map((k) => <Badge>{k}</Badge>)}</div>
          ) : 'Every feature was used at least once.'}>
          <Table
            capped
            columns={[
              { key: 'feature', label: 'Feature', render: (r: Row) => <span class="mono">{r.feature}</span> },
              { key: 'plugin', label: 'Tool', render: (r: Row) => <span class="muted">{r.plugin}</span> },
              { key: 'n', label: 'Uses', num: true, render: (r: Row) => num(r.n) },
              { key: 'bar', label: '', render: (r: Row) => <ShareBar value={Number(r.n)} max={peakUses} /> },
              { key: 'installs', label: 'Install-days', num: true, render: (r: Row) => num(r.installs) },
            ]}
            rows={used}
            empty="No feature was used in this range."
          />
        </Card>
      </div>

      <Card title="Functions" sub="Python API, CLI and UI calls, most first (top 200)" flush>
        <Table
          capped
          columns={[
            { key: 'fn', label: 'Function', render: (r: Row) => <span class="mono">{r.fn}</span> },
            { key: 'source', label: 'Source', render: (r: Row) => <Badge>{r.source}</Badge> },
            { key: 'n', label: 'Calls', num: true, render: (r: Row) => num(r.n) },
            { key: 'err', label: 'Errors', num: true,
              render: (r: Row) => (r.err ? <span class="bad-text">{num(r.err)} · {share(r.err, r.n)}</span> : '0') },
            { key: 'installs', label: 'Install-days', num: true, render: (r: Row) => num(r.installs) },
            { key: 'p', label: 'p50 / p95', num: true, render: p5095 },
            { key: 'h', label: 'Duration', render: hist },
          ]}
          rows={functions}
          empty="No function call in this range."
        />
      </Card>

      <div class="grid two start">
        <Card title="Agent capabilities" sub="calls, most first" flush>
          <Table
            capped
            columns={[
              { key: 'capability', label: 'Capability', render: (r: Row) => (
                <><span class="mono">{r.capability}</span>{r.owner && r.owner !== 'core' ? <div class="sub">{r.owner}</div> : null}</>
              ) },
              { key: 'ok', label: 'OK', num: true, render: (r: Row) => num(r.ok) },
              { key: 'err', label: 'Errors', num: true, render: (r: Row) => (r.err ? <span class="bad-text">{num(r.err)}</span> : '0') },
              { key: 'refused', label: 'Refused', num: true, render: (r: Row) => num(r.refused) },
              { key: 'nested', label: 'Nested', num: true, render: (r: Row) => num(r.nested) },
              { key: 'p', label: 'p50 / p95', num: true, render: p5095 },
            ]}
            rows={caps}
            empty="No agent call in this range."
          />
        </Card>
        <Card title="What agents do next" sub="top 30 capability → capability" flush>
          <Table
            capped
            columns={[
              { key: 'from_cap', label: 'After', render: (r: Row) => <span class="mono">{r.from_cap}</span> },
              { key: 'to_cap', label: 'Then', render: (r: Row) => <span class="mono">{r.to_cap}</span> },
              { key: 'n', label: 'Times', num: true, render: (r: Row) => num(r.n) },
            ]}
            rows={d.transitions as Row[]}
            empty="No transitions in this range."
          />
        </Card>
      </div>
    </Layout>
  );
}

// -- performance ---------------------------------------------------------------

const AXIS_LABELS: Record<string, string> = {
  none: 'No split', path: 'Tile path', label_renderer: 'Label renderer', browser: 'Browser', gpu: 'GPU',
  route: 'Route', family: 'Endpoint family', kind: 'Kind', source: 'Source', modality: 'Modality', tool: 'Tool',
  status: 'Status',
};

export function PerformancePage(props: { d: R<typeof performance>; who: string }) {
  const d = props.d;
  const metrics = d.metrics as Row[];
  const combine = (name: string) => {
    const rows = metrics.filter((m) => m.metric === name);
    if (rows.length === 0) return null;
    const bins = rows.reduce<number[]>((acc, r) => addBins(acc, r.bins as number[]), new Array(VECTORS.hist_bins).fill(0));
    const max = rows.reduce((m, r) => (r.max === null ? m : Math.max(m ?? 0, r.max)), null as number | null);
    return { n: sum(rows, 'n'), err: sum(rows, 'err'), p50: percentile(bins, 0.5, undefined, max), p95: percentile(bins, 0.95, undefined, max) };
  };
  const tile = combine('render.summary.tile_ms');
  const server = combine('server.summary.tile_total_ms');
  const firstPaint = combine('render.summary.first_paint_ms');
  const worst = [...new Set(metrics.map((m) => m.metric as string))]
    .map((name) => ({ name, ...combine(name)! }))
    .filter((m) => m.n >= 10 && m.err > 0)
    .sort((a, b) => b.err / b.n - a.err / a.n)[0];
  const versions = d.tile_load_by_version as Row[];
  const split = d.axis !== 'none';
  const axis = (
    <div class="field">
      <label for="f-axis">Split by</label>
      <select id="f-axis" name="axis">
        {d.axes.map((a) => <option value={a} selected={a === d.axis ? true : undefined}>{AXIS_LABELS[a] ?? a}</option>)}
      </select>
    </div>
  );
  return (
    <Layout title="Performance" active="performance" who={props.who} lede={rangeLede(d.filters)}>
      <FilterForm filters={d.filters} fields={['version']} extra={axis} keep={{ axis: d.axis === 'none' ? '' : d.axis }} />
      <Stats>
        <Stat label="Tile load, p50" value={ms(tile?.p50)} sub={tile ? `p95 ${ms(tile.p95)}` : 'no tiles timed'} />
        <Stat label="Tiles timed" value={num(tile?.n ?? 0)} sub="in the browser" />
        <Stat label="Server tile, p50" value={ms(server?.p50)} sub={server ? `p95 ${ms(server.p95)}` : 'no server timings'} />
        <Stat label="First paint, p50" value={ms(firstPaint?.p50)} sub={firstPaint ? `p95 ${ms(firstPaint.p95)}` : 'none recorded'} />
        <Stat label="Highest failure rate" value={worst ? pct(worst.err / worst.n) : '–'} tone={worst ? 'warn' : undefined}
          sub={worst ? worst.name : 'no failures'} />
      </Stats>

      <Card title="Metrics" sub={split ? `split by ${AXIS_LABELS[d.axis] ?? d.axis}` : `${new Set(metrics.map((m) => m.metric)).size} metrics`} flush>
        <Table
          capped
          columns={[
            { key: 'metric', label: 'Metric', render: (r: Row) => (r._cont ? <span class="muted">″</span> : <span class="mono">{r.metric}</span>) },
            ...(split ? [{ key: 'value', label: AXIS_LABELS[d.axis] ?? d.axis, render: (r: Row) => r.value || '–' }] : []),
            { key: 'n', label: 'Samples', num: true, render: (r: Row) => num(r.n) },
            { key: 'p50', label: 'p50', num: true, render: (r: Row) => ms(r.p50) },
            { key: 'p95', label: 'p95', num: true, render: (r: Row) => ms(r.p95) },
            { key: 'h', label: MS_LABELS.length ? `${MS_LABELS[0]} … ${MS_LABELS[MS_LABELS.length - 1]} ms` : '', render: (r: Row) => <Hist bins={r.bins} labels={MS_LABELS} /> },
            { key: 'failure_rate', label: 'Failures', num: true,
              render: (r: Row) => (r.err ? <span class="bad-text">{pct(r.failure_rate)}</span> : <span class="muted">0</span>) },
          ]}
          rows={metrics.map((m, i) => ({ ...m, _cont: split && i > 0 && metrics[i - 1]!.metric === m.metric }))}
          empty="No timings in this range."
        />
      </Card>

      <Card title="Tile load by version" sub="browser tile_ms; the change is against the version before" flush>
        <Table
          columns={[
            { key: 'version', label: 'Version', render: (r: Row) => <span class="mono">{r.version}</span> },
            { key: 'n', label: 'Tiles', num: true, render: (r: Row) => num((r.bins as number[]).reduce((a, b) => a + b, 0)) },
            { key: 'p50', label: 'p50', num: true, render: (r: Row) => ms(r.p50) },
            { key: 'p95', label: 'p95', num: true, render: (r: Row) => ms(r.p95) },
            { key: 'delta', label: 'p50 change', num: true, render: (r: Row) => {
              const i = versions.indexOf(r);
              const before = i > 0 ? versions[i - 1] : null;
              if (!before || before.p50 === null || r.p50 === null || !before.p50) return <span class="muted">–</span>;
              const change = (r.p50 - before.p50) / before.p50;
              const cls = change > 0.05 ? 'bad-text' : change < -0.05 ? 'ok-text' : 'muted';
              return <span class={cls}>{change > 0 ? '+' : ''}{(change * 100).toFixed(0)}%</span>;
            } },
            { key: 'h', label: 'Distribution', render: (r: Row) => <Hist bins={r.bins} labels={MS_LABELS} /> },
          ]}
          rows={versions}
          empty="No tile timings in this range."
        />
      </Card>
    </Layout>
  );
}

// -- errors --------------------------------------------------------------------

const sideTone = (side: string | null | undefined): Tone => (side === 'server' ? 'bad' : side === 'browser' ? 'warn' : 'plain');

export function ErrorsPage(props: { d: R<typeof errors>; who: string }) {
  const d = props.d;
  const rows = d.errors as Row[];
  const live = rows.filter((r) => !r.muted_at);
  const total = sum(live, 'count');
  const bySide = new Map<string, number>();
  for (const r of live) bySide.set(r.side ?? 'unknown', (bySide.get(r.side ?? 'unknown') ?? 0) + Number(r.count));
  const fresh = rows.filter((r) => typeof r.first_seen === 'string' && r.first_seen >= d.filters.from).length;
  const peak = Math.max(1, ...rows.map((r) => Number(r.count)));
  return (
    <Layout title="Errors" active="errors" who={props.who} lede={rangeLede(d.filters)}>
      <FilterForm filters={d.filters} fields={['version']} />
      <Stats>
        <Stat label="Occurrences" value={num(total)} tone={total ? 'warn' : 'ok'} sub="excluding muted" />
        <Stat label="Fingerprints" value={num(live.length)} sub={`${rows.length - live.length} muted`} />
        <Stat label="New in range" value={num(fresh)} tone={fresh ? 'warn' : undefined} sub={`first seen since ${d.filters.from}`} />
        {[...bySide.entries()].sort((a, b) => b[1] - a[1]).slice(0, 3).map(([side, n]) => (
          <Stat label={side} value={num(n)} sub={`${share(n, total)} of occurrences`} />
        ))}
      </Stats>

      <Card title="Fingerprints" sub="most frequent first; muted ones sink to the bottom" flush>
        <Table
          columns={[
            { key: 'fp', label: 'Fingerprint', render: (r: Row) => <a class="mono" href={`/admin/errors/${r.fp}`}>{r.fp}</a> },
            { key: 'side', label: 'Side', render: (r: Row) => <Badge tone={sideTone(r.side)}>{r.side ?? '–'}</Badge> },
            { key: 'exception', label: 'Exception', render: (r: Row) => (
              <><span class="mono">{r.exception ?? '–'}</span>
                {r.component || r.route ? <div class="sub">{[r.component, r.route].filter(Boolean).join(' · ')}</div> : null}</>
            ) },
            { key: 'versions', label: 'Versions', render: (r: Row) => (
              <span class="small">{r.first_version ?? '–'}{r.last_version && r.last_version !== r.first_version ? ` → ${r.last_version}` : ''}</span>
            ) },
            { key: 'installs', label: 'Install-days', num: true, render: (r: Row) => num(r.installs) },
            { key: 'count', label: 'Count', num: true, render: (r: Row) => num(r.count) },
            { key: 'bar', label: '', render: (r: Row) => <ShareBar value={Number(r.count)} max={peak} /> },
            { key: 'mute', label: '', render: (r: Row) => (
              <button type="button" class="ghost tiny" data-action={`/admin/api/errors/${r.fp}/mute`}
                data-body={JSON.stringify({ muted: !r.muted_at })} data-done={r.muted_at ? 'Unmuted.' : 'Muted.'} data-reload="">
                {r.muted_at ? 'Unmute' : 'Mute'}
              </button>
            ) },
          ]}
          rows={rows}
          rowClass={(r) => (r.muted_at ? 'dim' : '')}
          empty="No errors in this range."
        />
      </Card>
    </Layout>
  );
}

export function ErrorDetailPage(props: { d: R<typeof errorDetail>; who: string }) {
  const r = props.d.registry as Row;
  const series = props.d.series as Row[];
  const byDay = new Map<string, number>();
  for (const row of series) byDay.set(row.day, (byDay.get(row.day) ?? 0) + Number(row.n));
  const total = sum(series, 'n');
  // Every day of the window, so a gap reads as a gap.
  const days: { label: string; value: number }[] = [];
  const first = series[0]?.day as string | undefined;
  if (first) {
    const last = series[series.length - 1]!.day as string;
    for (let t = Date.parse(`${first}T00:00:00Z`); t <= Date.parse(`${last}T00:00:00Z`); t += 86400000) {
      const key = new Date(t).toISOString().slice(0, 10);
      days.push({ label: key, value: byDay.get(key) ?? 0 });
    }
  }
  const kv: [string, Child][] = [
    ['Side', <Badge tone={sideTone(r.side)}>{r.side ?? '–'}</Badge>],
    ['Exception', <span class="mono">{r.exception ?? '–'}</span>],
    ['Component', r.component ?? '–'],
    ['Route', r.route ? <span class="mono">{r.route}</span> : '–'],
    ['Action', r.action ?? '–'],
    ['File', r.file ? <span class="mono">{r.file}</span> : '–'],
    ['Plugin', r.plugin ?? '–'],
    ['Muted', r.muted_at ? `${when(r.muted_at)} by ${r.muted_by ?? '–'}` : 'no'],
  ];
  return (
    <Layout title={`Error ${r.fp}`} active="errors" who={props.who}
      lede={<>{r.exception ?? 'Unknown exception'}{r.component ? ` in ${r.component}` : ''} · <a href="/admin/errors">all errors</a></>}
      actions={
        <button type="button" class="ghost" data-action={`/admin/api/errors/${r.fp}/mute`}
          data-body={JSON.stringify({ muted: !r.muted_at })} data-done={r.muted_at ? 'Unmuted.' : 'Muted.'} data-reload="">
          {r.muted_at ? 'Unmute' : 'Mute'}
        </button>
      }>
      {r.muted_at ? <Note>Muted: it still counts, but sinks to the bottom of the list.</Note> : null}
      <Stats>
        <Stat label="Last 60 days" value={num(total)} sub={`on ${byDay.size} days`} />
        <Stat label="Install-days" value={num(sum(series, 'installs'))} />
        <Stat label="Introduced" value={r.first_version ?? '–'} text sub={r.first_seen} />
        <Stat label="Last seen" value={r.last_version ?? '–'} text sub={r.last_seen} />
      </Stats>
      <div class="grid two">
        <Card title="Per day" sub="last 60 days">
          {days.length ? <Bars values={days} label="Occurrences per day" /> : <Empty>Not seen in the last 60 days.</Empty>}
        </Card>
        <Card title="Where">
          <div class="scroll"><table class="kv"><tbody>
            {kv.map(([k, v]) => <tr><td>{k}</td><td>{v}</td></tr>)}
          </tbody></table></div>
        </Card>
      </div>
      <Card title="By day and version" flush>
        <Table
          capped
          columns={[
            { key: 'day', label: 'Day' },
            { key: 'version', label: 'Version', render: (x: Row) => <span class="mono">{x.version}</span> },
            { key: 'n', label: 'Count', num: true, render: (x: Row) => num(x.n) },
            { key: 'installs', label: 'Install-days', num: true, render: (x: Row) => num(x.installs) },
          ]}
          rows={[...series].reverse()}
          empty="Not seen in the last 60 days."
        />
      </Card>
    </Layout>
  );
}

// -- budget --------------------------------------------------------------------

/** What each budget state does, in the words of backend/README.md. */
const STATE_EFFECT: Record<string, string> = {
  ok: 'Everything runs normally.',
  warn: 'Everything still runs normally; this is the early warning.',
  aggregate: 'Clients are asked to upload a third as often and half of them sample out.',
  reduce: 'Low-priority events and diagnostics fields are dropped; clients are capped at anonymous.',
  critical: 'Only the highest-priority events are kept; maintenance runs export and purge only.',
  stop: 'Uploads get 503 until UTC midnight. Nothing is written.',
};

const stateTone = (state: string): Tone =>
  state === 'ok' ? 'ok' : state === 'warn' || state === 'aggregate' ? 'warn' : 'bad';

export function BudgetPage(props: { d: R<typeof budget>; who: string }) {
  const d = props.d;
  const ticks = Object.values(d.thresholds);
  const r2 = d.r2;
  const meterTone = (ratio: number) => (ratio >= d.thresholds.reduce ? 'bad' : ratio >= d.thresholds.warn ? 'warn' : undefined);
  const r2Ratio = r2.hard ? r2.bytes / r2.hard : 0;
  const lastBackup = d.last_backup as Row | null;
  const steps = d.open_steps as Row[];
  const failed = steps.filter((s) => s.state === 'failed').length;
  const knobs = Object.entries(d.knobs).sort(([a], [b]) => a.localeCompare(b));
  const third = Math.ceil(knobs.length / 3);
  const knobTable = (rows: [string, unknown][]) => (
    <div class="scroll"><table class="kv"><tbody>
      {rows.map(([k, v]) => <tr><td class="mono small">{k}</td><td class="num">{typeof v === 'number' ? v.toLocaleString('en-US') : String(v)}</td></tr>)}
    </tbody></table></div>
  );
  return (
    <Layout title="Budget" active="budget" who={props.who}
      lede={<>Today, {d.day} UTC · shares of the free tier this Worker may use (SCIMAP Pro shares the account)</>}
      actions={<button type="button" class="ghost" data-action="/admin/api/maintenance/run" data-done="One maintenance slice ran." data-reload="">Run one maintenance slice</button>}>
      <Card feature title={<>State <Badge tone={stateTone(d.state)}>{d.state}</Badge></>}
        sub={`${pct(d.ratio)} of the configured share, the highest of requests, writes and reads`}>
        <p class="small muted">{STATE_EFFECT[d.state] ?? ''} Thresholds: warn {pct(d.thresholds.warn, 0)} · aggregate {pct(d.thresholds.aggregate, 0)} · reduce {pct(d.thresholds.reduce, 0)} · critical {pct(d.thresholds.critical, 0)} · stop {pct(d.thresholds.stop, 0)}.</p>
      </Card>

      <div class="grid three">
        {d.meters.map((m) => {
          const ratio = m.share ? m.used / m.share : 0;
          return (
            <div class="card">
              <div class="meter-head"><h2>{m.name}</h2><span class="pct">{pct(ratio, 0)}</span></div>
              <Meter used={m.used} share={m.share} ticks={ticks} tone={meterTone(ratio)} />
              <div class="small muted">{num(m.used)} of {num(m.share)} · {pct(m.used / m.platform)} of the platform's {num(m.platform)}</div>
            </div>
          );
        })}
        <div class="card">
          <div class="meter-head"><h2>R2 storage</h2><span class="pct">{bytes(r2.bytes)}</span></div>
          <Meter used={r2.bytes} share={r2.hard} ticks={[r2.target / r2.hard, r2.warn / r2.hard, r2.aggressive / r2.hard]}
            tone={r2.bytes >= r2.aggressive ? 'bad' : r2.bytes >= r2.target ? 'warn' : undefined} />
          <div class="small muted">{num(r2.archives)} daily archives · {pct(r2Ratio, 0)} of hard cap {bytes(r2.hard)} · target {bytes(r2.target)}</div>
        </div>
      </div>

      <div class="grid two start">
        <Card title="D1 writes per day" sub="last 14 days, against the share">
          <Sparkline values={d.write_ratio_14d.map((r) => r.ratio)} labels={d.write_ratio_14d.map((r) => r.day)} ticks={[0.5, 0.7, 0.85, 0.95]} />
        </Card>
        <Card title="Retention and backups">
          <div class="scroll"><table class="kv"><tbody>
            <tr><td>Last backup check</td><td>{lastBackup
              ? <>{lastBackup.day} <Badge tone={lastBackup.state === 'done' ? 'ok' : lastBackup.state === 'failed' ? 'bad' : 'warn'}>{lastBackup.state}</Badge></>
              : <span class="muted">none yet</span>}</td></tr>
            <tr><td>Oldest raw batch day</td><td>{d.oldest_batch_day ?? '–'}</td></tr>
            <tr><td>Next purge of raw batches</td><td>{d.next_purge_day ?? '–'}</td></tr>
            <tr><td>Oldest archive</td><td>{d.oldest_archive_day ?? '–'}</td></tr>
            <tr><td>Bytes received today</td><td>{bytes(d.bytes_in)}</td></tr>
            <tr><td>R2 writes / reads this month</td><td>{num(r2.class_a_this_month_seen)} / {num(r2.class_b_this_month_seen)}</td></tr>
          </tbody></table></div>
        </Card>
      </div>

      <Card title="Maintenance steps" sub={steps.length ? `${steps.length} open${failed ? `, ${failed} failed` : ''}` : 'nothing pending'} flush>
        <Table
          columns={[
            { key: 'day', label: 'Day' },
            { key: 'step', label: 'Step', render: (r: Row) => <span class="mono">{r.step}</span> },
            { key: 'state', label: 'State', render: (r: Row) => <Badge tone={r.state === 'failed' ? 'bad' : r.state === 'running' ? 'accent' : 'plain'}>{r.state}</Badge> },
            { key: 'attempts', label: 'Attempts', num: true },
            { key: 'error', label: 'Error', wrap: true, render: (r: Row) => r.error ?? '' },
          ]}
          rows={steps}
          rowClass={(r) => (r.state === 'failed' ? 'failed' : '')}
          empty="Nothing pending: every step of the last 30 days is done."
        />
      </Card>

      <Fold title="Effective knobs" sub={`${knobs.length} values from wrangler.toml [vars]`}>
        <div class="kv-grid">
          {knobTable(knobs.slice(0, third))}
          {knobTable(knobs.slice(third, third * 2))}
          {knobTable(knobs.slice(third * 2))}
        </div>
      </Fold>
    </Layout>
  );
}

// -- backups -------------------------------------------------------------------

export function BackupsPage(props: { d: R<typeof backups>; who: string }) {
  const archives = props.d.archives as Row[];
  const failed = props.d.failed as Row[];
  const live = archives.filter((a) => !a.deleted_at);
  const verified = live.filter((a) => a.verified_at).length;
  const newest = live.filter((a) => a.kind === 'daily').sort((a, b) => String(b.period).localeCompare(String(a.period)))[0];
  return (
    <Layout title="Backups" active="backups" who={props.who}
      lede="Daily and monthly archives in R2. Raw batches are purged only after their day's archive is verified.">
      <Stats>
        <Stat label="Archives" value={num(live.length)} sub={`${archives.length - live.length} deleted`} />
        <Stat label="Stored" value={bytes(sum(live, 'bytes'))} sub={`${num(sum(live, 'records'))} records`} />
        <Stat label="Verified" value={`${verified} / ${live.length}`} tone={verified < live.length ? 'warn' : live.length ? 'ok' : undefined}
          sub={verified < live.length ? `${live.length - verified} waiting` : 'all checked'} />
        <Stat label="Newest daily" value={newest?.period ?? '–'} text sub={newest ? `created ${when(newest.created)}` : 'none yet'} />
        <Stat label="Failures" value={num(failed.length)} tone={failed.length ? 'bad' : 'ok'} sub="last 50 kept" />
      </Stats>

      {failed.length ? (
        <Card title="Failures" sub="a failed day is not purged" flush>
          <Table
            columns={[
              { key: 'day', label: 'Day' },
              { key: 'step', label: 'Step', render: (r: Row) => <span class="mono">{r.step}</span> },
              { key: 'error', label: 'Error', wrap: true },
              { key: 'finished', label: 'When', render: (r: Row) => when(r.finished) },
            ]}
            rows={failed}
            rowClass={() => 'failed'}
          />
        </Card>
      ) : null}

      <Card title="Archives" sub="newest first" flush>
        <Table
          capped
          columns={[
            { key: 'period', label: 'Period', render: (r: Row) => <><b>{r.period}</b> <Badge tone={r.kind === 'monthly' ? 'accent' : 'plain'}>{r.kind}</Badge></> },
            { key: 'status', label: 'Status', render: (r: Row) => (r.deleted_at ? <Badge>deleted</Badge> : r.verified_at ? <Badge tone="ok">verified</Badge> : <Badge tone="warn">unverified</Badge>) },
            { key: 'bytes', label: 'Size', num: true, render: (r: Row) => bytes(r.bytes) },
            { key: 'records', label: 'Records', num: true, render: (r: Row) => num(r.records) },
            { key: 'segments', label: 'Parts', num: true },
            { key: 'sha256', label: 'SHA-256', render: (r: Row) => (r.sha256 ? <span class="mono small muted" title={r.sha256}>{String(r.sha256).slice(0, 12)}…</span> : '–') },
            { key: 'created', label: 'Created', render: (r: Row) => day(r.created) },
            { key: 'expires', label: 'Expires', render: (r: Row) => day(r.expires) },
            { key: 'actions', label: '', render: (r: Row) => (r.deleted_at ? '' : (
              <span class="actions">
                <a class="button ghost tiny" href={`/admin/api/backups/${r.key}`}>Download</a>
                <button type="button" class="danger tiny" data-method="DELETE" data-action={`/admin/api/backups/${r.key}`}
                  data-confirm={`Delete the ${r.kind} archive for ${r.period}? It cannot be recovered.`} data-done="Deleted." data-reload="">Delete</button>
              </span>
            )) },
          ]}
          rows={archives}
          rowClass={(r) => (r.deleted_at ? 'dim' : '')}
          empty="No archives yet. The first is written the night after the first upload."
        />
      </Card>
    </Layout>
  );
}

// -- one install ---------------------------------------------------------------

export function InstallPage(props: { d: R<typeof install>; who: string }) {
  const i = props.d.install as Row;
  const days = props.d.days as Row[];
  return (
    <Layout title="Install" who={props.who} lede={<span class="mono">{i.install_id}</span>}>
      {i.erased_at ? <Note tone="warn">Erased {when(i.erased_at)}: identity columns are cleared.</Note> : null}
      <Stats>
        <Stat label="Version" value={i.version ?? '–'} text />
        <Stat label="Days active" value={num(i.days_active)} />
        <Stat label="First seen" value={day(i.first_seen)} text />
        <Stat label="Last seen" value={day(i.last_seen)} text />
        <Stat label="Launch / deployment" value={`${i.launch_mode ?? '–'} / ${i.deployment ?? '–'}`} text />
        <Stat label="OS / country" value={`${i.os ?? '–'} / ${i.country ?? '–'}`} text />
      </Stats>
      <Card title="Last 90 days" sub={`${days.length} active days · ${num(sum(days, 'sessions'))} sessions · ${num(sum(days, 'errors'))} errors`} flush>
        <Table
          capped
          columns={[
            { key: 'day', label: 'Day' },
            { key: 'version', label: 'Version', render: (r: Row) => <span class="mono">{r.version}</span> },
            { key: 'sessions', label: 'Sessions', num: true },
            { key: 'batches', label: 'Batches', num: true },
            { key: 'events', label: 'Events', num: true },
            { key: 'errors', label: 'Errors', num: true },
          ]}
          rows={[...days].reverse()}
          empty="No activity in the last 90 days."
        />
      </Card>
      <Fold title="Erase this install">
        <Note tone="bad">Clears its identity columns and deletes its detail rows. Rollups keep their counts. This cannot be undone.</Note>
        <button type="button" class="danger" data-action={`/admin/api/erase/${i.install_id}`}
          data-confirm="Erase this install's identity and detail rows?" data-done="Erased." data-reload="">Erase</button>
      </Fold>
    </Layout>
  );
}

// -- signing in ----------------------------------------------------------------

export function LoginPage(props: { next: string; access?: boolean }) {
  return (
    <Layout title="Sign in" heading={false} center>
      <div class="auth-card">
        <div class="auth-badge"><Mark /></div>
        <h1>Telemetry admin</h1>
        {props.access ? (
          <>
            <p class="lede">Sign in with Cloudflare Access. The admin token is the fallback for scripts.</p>
            <p><a class="button wide" href={props.next}>Sign in with Cloudflare Access</a></p>
          </>
        ) : <p class="lede">Cloudflare Access is not configured, so sign in with the admin token.</p>}
        <details class="section" open={props.access ? undefined : true}>
          <summary>Use the admin token</summary>
          <form data-json="" action="/admin/login" data-next={props.next}>
            <div class="field">
              <label for="f-token">Admin token</label>
              <input id="f-token" type="password" name="token" autocomplete="current-password" required />
            </div>
            <button type="submit" class="wide">Sign in</button>
          </form>
        </details>
        <p class="foot-note">Anonymous usage counts only. Nothing here identifies a person or a dataset.</p>
      </div>
    </Layout>
  );
}
