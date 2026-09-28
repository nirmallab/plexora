/** @jsxImportSource hono/jsx */
/**
 * The admin pages. Each takes the JSON a queries.ts function returned, so
 * HTML and JSON views can never disagree about the numbers.
 */
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
import { Bars, Meter, Sparkline } from '../ui/charts';
import { Badge, FilterForm, Stat, Table } from '../ui/components';
import { bytes, ms, num, pct, when } from '../ui/format';
import { Layout } from '../ui/layout';

type R<T extends (...args: never[]) => unknown> = NonNullable<Awaited<ReturnType<T>>>;
type Row = Record<string, any>;

export function UsagePage(props: { d: R<typeof usage>; who: string }) {
  const d = props.d;
  const split = (rows: { name: string; installs: number }[]) => (
    <Table columns={[{ key: 'name', label: 'Value' }, { key: 'installs', label: 'Install-days', num: true, render: (r: Row) => num(r.installs) }]} rows={rows} />
  );
  return (
    <Layout title="Usage" active="usage" who={props.who}>
      <FilterForm filters={d.filters} />
      <div class="grid">
        <Stat label={`DAU (${d.filters.to})`} value={num(d.dau)} />
        <Stat label="WAU" value={num(d.wau)} />
        <Stat label="MAU" value={num(d.mau)} />
        <Stat label="New installs in range" value={num(d.daily.reduce((s, r) => s + r.new_installs, 0))} />
      </div>
      <h2>Active installs per day</h2>
      <div class="card">
        <Bars values={d.daily.map((r) => ({ label: r.day, value: r.installs }))} />
      </div>
      <h2>Sessions per day</h2>
      <div class="card">
        <Bars values={d.daily.map((r) => ({ label: r.day, value: r.sessions }))} alt />
      </div>
      <h2>Versions</h2>
      <Table
        columns={[
          { key: 'day', label: 'Day' },
          { key: 'version', label: 'Version' },
          { key: 'installs', label: 'Installs', num: true },
          { key: 'new_installs', label: 'New', num: true },
        ]}
        rows={d.versions.slice(-60) as Row[]}
      />
      <div class="grid">
        <div>
          <h2>Launch mode</h2>
          {split(d.launch_mode)}
        </div>
        <div>
          <h2>Deployment</h2>
          {split(d.deployment)}
        </div>
        <div>
          <h2>OS</h2>
          {split(d.os)}
        </div>
      </div>
    </Layout>
  );
}

export function DataPage(props: { d: R<typeof data>; who: string }) {
  const d = props.d;
  return (
    <Layout title="Data" active="data" who={props.who}>
      <FilterForm filters={d.filters} />
      <h2>Modality × image kind × run format × table kind</h2>
      <Table
        columns={[
          { key: 'modality', label: 'Modality' },
          { key: 'image_kind', label: 'Image' },
          { key: 'run_format', label: 'Run format' },
          { key: 'table_kind', label: 'Table' },
          { key: 'n', label: 'Opened', num: true, render: (r: Row) => num(r.n) },
          { key: 'installs', label: 'Install-days', num: true, render: (r: Row) => num(r.installs) },
          { key: 'distributed', label: 'Distributed', num: true, render: (r: Row) => num(r.distributed) },
        ]}
        rows={d.modalities as Row[]}
      />
      <h2>Scale</h2>
      <div class="grid">
        {Object.entries(d.scale).map(([dim, bands]) => (
          <div class="card">
            <strong>{dim}</strong>
            <Bars values={bands.map((b) => ({ label: b.band, value: b.n }))} height={80} width={300} />
          </div>
        ))}
      </div>
    </Layout>
  );
}

export function FeaturesPage(props: { d: R<typeof features>; who: string }) {
  const d = props.d;
  const p = (r: Row) => `${ms(r.p50)} / ${ms(r.p95)}`;
  return (
    <Layout title="Features" active="features" who={props.who}>
      <FilterForm filters={d.filters} />
      <h2>Plugins</h2>
      <Table
        columns={[
          { key: 'plugin', label: 'Plugin' },
          { key: 'opens', label: 'Opens', num: true },
          { key: 'closes', label: 'Closes', num: true },
          { key: 'load_failed', label: 'Load failures', num: true },
          { key: 'installs', label: 'Install-days', num: true },
          { key: 'p', label: 'Load p50 / p95', num: true, render: p },
        ]}
        rows={d.plugins as Row[]}
      />
      <h2>Features</h2>
      <Table
        columns={[
          { key: 'plugin', label: 'Plugin' },
          { key: 'feature', label: 'Feature' },
          { key: 'n', label: 'Uses', num: true },
          { key: 'installs', label: 'Install-days', num: true },
        ]}
        rows={d.features as Row[]}
      />
      <p class="muted">Unused in this range: {d.unused.length ? d.unused.join(', ') : 'none'}</p>
      <h2>Functions by source</h2>
      <Table
        columns={[
          { key: 'source', label: 'Source' },
          { key: 'fn', label: 'Function' },
          { key: 'n', label: 'Calls', num: true },
          { key: 'err', label: 'Errors', num: true },
          { key: 'p', label: 'p50 / p95', num: true, render: p },
        ]}
        rows={d.functions as Row[]}
      />
      <h2>Agent capabilities</h2>
      <Table
        columns={[
          { key: 'capability', label: 'Capability' },
          { key: 'owner', label: 'Owner' },
          { key: 'ok', label: 'OK', num: true },
          { key: 'err', label: 'Errors', num: true },
          { key: 'refused', label: 'Refused', num: true },
          { key: 'nested', label: 'Nested', num: true },
          { key: 'p', label: 'p50 / p95', num: true, render: p },
        ]}
        rows={d.capabilities as Row[]}
      />
      <h2>Top transitions</h2>
      <Table
        columns={[
          { key: 'from_cap', label: 'From' },
          { key: 'to_cap', label: 'To' },
          { key: 'n', label: 'Count', num: true },
        ]}
        rows={d.transitions as Row[]}
      />
    </Layout>
  );
}

export function PerformancePage(props: { d: R<typeof performance>; who: string }) {
  const d = props.d;
  const axis = (
    <label>
      Split by
      <select name="axis">
        {d.axes.map((a) => (
          <option value={a} selected={a === d.axis}>
            {a}
          </option>
        ))}
      </select>
    </label>
  );
  return (
    <Layout title="Performance" active="performance" who={props.who}>
      <FilterForm filters={d.filters} extra={axis} />
      <Table
        columns={[
          { key: 'metric', label: 'Metric' },
          { key: 'value', label: d.axis === 'none' ? '' : d.axis },
          { key: 'n', label: 'Samples', num: true, render: (r: Row) => num(r.n) },
          { key: 'p50', label: 'p50', num: true, render: (r: Row) => ms(r.p50) },
          { key: 'p95', label: 'p95', num: true, render: (r: Row) => ms(r.p95) },
          { key: 'failure_rate', label: 'Failure rate', num: true, render: (r: Row) => pct(r.failure_rate) },
        ]}
        rows={d.metrics as Row[]}
      />
      <h2>render.summary.tile_ms by version</h2>
      <Table
        columns={[
          { key: 'version', label: 'Version' },
          { key: 'p50', label: 'p50', num: true, render: (r: Row) => ms(r.p50) },
          { key: 'p95', label: 'p95', num: true, render: (r: Row) => ms(r.p95) },
          { key: 'bins', label: d.labels.join(' · '), render: (r: Row) => (r.bins as number[]).join(' · ') },
        ]}
        rows={d.tile_load_by_version as Row[]}
      />
    </Layout>
  );
}

export function ErrorsPage(props: { d: R<typeof errors>; who: string }) {
  const d = props.d;
  return (
    <Layout title="Errors" active="errors" who={props.who}>
      <FilterForm filters={d.filters} />
      <Table
        columns={[
          { key: 'fp', label: 'Fingerprint', render: (r: Row) => <a href={`/admin/errors/${r.fp}`}>{r.fp}</a> },
          { key: 'side', label: 'Side' },
          { key: 'exception', label: 'Exception' },
          { key: 'component', label: 'Component' },
          { key: 'route', label: 'Route' },
          { key: 'first_version', label: 'Introduced' },
          { key: 'last_version', label: 'Last seen in' },
          { key: 'installs', label: 'Install-days', num: true },
          { key: 'count', label: 'Count', num: true, render: (r: Row) => num(r.count) },
          {
            key: 'mute',
            label: '',
            render: (r: Row) => (
              <button type="button" data-action={`/admin/api/errors/${r.fp}/mute`} data-body={JSON.stringify({ muted: !r.muted_at })} data-reload>
                {r.muted_at ? 'Unmute' : 'Mute'}
              </button>
            ),
          },
        ]}
        rows={d.errors as Row[]}
        rowClass={(r) => (r.muted_at ? 'muted' : '')}
      />
    </Layout>
  );
}

export function ErrorDetailPage(props: { d: R<typeof errorDetail>; who: string }) {
  const r = props.d.registry as Row;
  const byDay = new Map<string, number>();
  for (const row of props.d.series as Row[]) byDay.set(row.day, (byDay.get(row.day) ?? 0) + row.n);
  return (
    <Layout title={`Error ${r.fp}`} active="errors" who={props.who}>
      <div class="grid">
        <Stat label="Side" value={r.side ?? '–'} />
        <Stat label="Exception" value={r.exception ?? '–'} />
        <Stat label="Component" value={r.component ?? '–'} />
        <Stat label="Introduced" value={`${r.first_version ?? '–'} (${r.first_seen})`} />
        <Stat label="Last seen" value={`${r.last_version ?? '–'} (${r.last_seen})`} />
      </div>
      <p class="muted">
        Route {r.route ?? '–'} · action {r.action ?? '–'} · file {r.file ?? '–'} · plugin {r.plugin ?? '–'}
        {r.muted_at ? ` · muted ${when(r.muted_at)} by ${r.muted_by}` : ''}
      </p>
      <h2>Last 60 days</h2>
      <div class="card">
        <Bars values={[...byDay.entries()].map(([label, value]) => ({ label, value }))} />
      </div>
      <Table
        columns={[
          { key: 'day', label: 'Day' },
          { key: 'version', label: 'Version' },
          { key: 'n', label: 'Count', num: true },
          { key: 'installs', label: 'Installs', num: true },
        ]}
        rows={props.d.series as Row[]}
      />
    </Layout>
  );
}

export function BudgetPage(props: { d: R<typeof budget>; who: string }) {
  const d = props.d;
  const tone = d.state === 'ok' ? 'ok' : d.state === 'warn' || d.state === 'aggregate' ? 'warn' : 'bad';
  const ticks = Object.values(d.thresholds);
  const r2 = d.r2;
  return (
    <Layout title="Budget" active="budget" who={props.who}>
      <p>
        Today ({d.day}): <Badge tone={tone}>{d.state}</Badge> at {pct(d.ratio)} of the configured share.
      </p>
      <div class="grid">
        {d.meters.map((m) => (
          <div class="card">
            <strong>{m.name}</strong>
            <Meter used={m.used} share={m.share} ticks={ticks} />
            <div class="muted">
              {num(m.used)} of {num(m.share)} share · {pct(m.used / m.platform)} of the platform's {num(m.platform)}
            </div>
          </div>
        ))}
        <div class="card">
          <strong>R2 storage</strong>
          <Meter used={r2.bytes} share={r2.hard} ticks={[r2.target / r2.hard, r2.warn / r2.hard, r2.aggressive / r2.hard]} />
          <div class="muted">
            {bytes(r2.bytes)} in {r2.archives} archives · target {bytes(r2.target)} · hard {bytes(r2.hard)}
          </div>
        </div>
      </div>
      <h2>D1 write ratio, 14 days</h2>
      <div class="card">
        <Sparkline values={d.write_ratio_14d.map((r) => r.ratio)}
                   labels={d.write_ratio_14d.map((r) => r.day)}
                   ticks={[0.5, 0.7, 0.85, 0.95]} />
      </div>
      <div class="grid">
        <Stat label="Bytes in today" value={bytes(d.bytes_in)} />
        <Stat label="R2 Class A this month (seen)" value={num(r2.class_a_this_month_seen)} />
        <Stat label="R2 Class B this month (seen)" value={num(r2.class_b_this_month_seen)} />
        <Stat label="Oldest batch day" value={d.oldest_batch_day ?? '–'} />
        <Stat label="Next purge" value={d.next_purge_day ?? '–'} />
        <Stat label="Oldest archive" value={d.oldest_archive_day ?? '–'} />
        <Stat label="Last backup" value={d.last_backup ? `${(d.last_backup as Row).day} ${(d.last_backup as Row).state}` : '–'} />
      </div>
      <h2>Pending and failed steps</h2>
      <Table
        columns={[
          { key: 'day', label: 'Day' },
          { key: 'step', label: 'Step' },
          { key: 'state', label: 'State' },
          { key: 'attempts', label: 'Attempts', num: true },
          { key: 'error', label: 'Error' },
        ]}
        rows={d.open_steps as Row[]}
        rowClass={(r) => (r.state === 'failed' ? 'failed' : '')}
        empty="Nothing pending."
      />
      <p>
        <button type="button" data-action="/admin/api/maintenance/run" data-reload>
          Run one maintenance slice now
        </button>
      </p>
      <h2>Effective knobs</h2>
      <Table columns={[{ key: 'k', label: 'Knob' }, { key: 'v', label: 'Value', num: true }]} rows={Object.entries(d.knobs).map(([k, v]) => ({ k, v }))} />
    </Layout>
  );
}

export function BackupsPage(props: { d: R<typeof backups>; who: string }) {
  return (
    <Layout title="Backups" active="backups" who={props.who}>
      <Table
        columns={[
          { key: 'kind', label: 'Kind' },
          { key: 'period', label: 'Period' },
          { key: 'segments', label: 'Parts', num: true },
          { key: 'bytes', label: 'Size', num: true, render: (r: Row) => bytes(r.bytes) },
          { key: 'records', label: 'Records', num: true },
          { key: 'created', label: 'Created', render: (r: Row) => when(r.created) },
          {
            key: 'verified_at',
            label: 'Verified',
            render: (r: Row) => (r.deleted_at ? <Badge>deleted</Badge> : r.verified_at ? <Badge tone="ok">verified</Badge> : <Badge tone="bad">unverified</Badge>),
          },
          { key: 'expires', label: 'Expires', render: (r: Row) => when(r.expires) },
          {
            key: 'actions',
            label: '',
            render: (r: Row) =>
              r.deleted_at ? (
                ''
              ) : (
                <span>
                  <a href={`/admin/api/backups/${r.key}`}>download</a>{' '}
                  <button type="button" class="danger" data-method="DELETE" data-action={`/admin/api/backups/${r.key}`} data-confirm="Delete this archive?" data-reload>
                    delete
                  </button>
                </span>
              ),
          },
        ]}
        rows={props.d.archives as Row[]}
        empty="No archives yet."
      />
      <h2>Failures</h2>
      <Table
        columns={[
          { key: 'day', label: 'Day' },
          { key: 'step', label: 'Step' },
          { key: 'error', label: 'Error' },
          { key: 'finished', label: 'When', render: (r: Row) => when(r.finished) },
        ]}
        rows={props.d.failed as Row[]}
        rowClass={() => 'failed'}
        empty="No failures."
      />
    </Layout>
  );
}

export function InstallPage(props: { d: R<typeof install>; who: string }) {
  const i = props.d.install as Row;
  return (
    <Layout title={`Install ${i.install_id}`} who={props.who}>
      <div class="grid">
        <Stat label="Version" value={i.version ?? '–'} />
        <Stat label="Days active" value={num(i.days_active)} />
        <Stat label="First seen" value={when(i.first_seen)} />
        <Stat label="Last seen" value={when(i.last_seen)} />
        <Stat label="Launch / deployment" value={`${i.launch_mode ?? '–'} / ${i.deployment ?? '–'}`} />
        <Stat label="OS / country" value={`${i.os ?? '–'} / ${i.country ?? '–'}`} />
      </div>
      <p>
        <button type="button" class="danger" data-action={`/admin/api/erase/${i.install_id}`} data-confirm="Erase this install's identity and detail rows?" data-reload>
          Erase
        </button>
      </p>
      <h2>Last 90 days</h2>
      <Table
        columns={[
          { key: 'day', label: 'Day' },
          { key: 'version', label: 'Version' },
          { key: 'sessions', label: 'Sessions', num: true },
          { key: 'batches', label: 'Batches', num: true },
          { key: 'events', label: 'Events', num: true },
          { key: 'errors', label: 'Errors', num: true },
        ]}
        rows={props.d.days as Row[]}
      />
    </Layout>
  );
}

export function LoginPage(props: { next: string }) {
  return (
    <Layout title="Sign in">
      <div class="card login">
        <form data-json action="/admin/login" data-next={props.next}>
          <label>
            Admin token
            <input type="password" name="token" autocomplete="current-password" required />
          </label>
          <button class="primary" type="submit">
            Sign in
          </button>
        </form>
      </div>
    </Layout>
  );
}
