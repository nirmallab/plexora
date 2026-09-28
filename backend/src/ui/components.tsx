/** @jsxImportSource hono/jsx */
import type { Child } from 'hono/jsx';

import type { Filters } from '../telemetry/queries';

export interface Column<T> {
  key: string;
  label: string;
  num?: boolean;
  render?: (row: T) => Child;
}

export function Table<T extends Record<string, unknown>>(props: { columns: Column<T>[]; rows: T[]; empty?: string; rowClass?: (row: T) => string }) {
  if (props.rows.length === 0) return <p class="muted">{props.empty ?? 'Nothing in this range.'}</p>;
  return (
    <div class="scroll">
      <table>
        <thead>
          <tr>
            {props.columns.map((column) => (
              <th class={column.num ? 'num' : ''}>{column.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {props.rows.map((row) => (
            <tr class={props.rowClass?.(row) ?? ''}>
              {props.columns.map((column) => (
                <td class={column.num ? 'num' : ''}>{column.render ? column.render(row) : String(row[column.key] ?? '–')}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Stat(props: { label: string; value: Child }) {
  return (
    <div class="card stat">
      <div class="v">{props.value}</div>
      <div class="k">{props.label}</div>
    </div>
  );
}

export function Badge(props: { tone?: 'ok' | 'warn' | 'bad'; children: Child }) {
  return <span class={`badge ${props.tone ?? ''}`}>{props.children}</span>;
}

/** The shared GET filter form; the server validates every field again. */
export function FilterForm(props: { filters: Filters; extra?: Child }) {
  const f = props.filters;
  return (
    <form class="filters" method="get">
      <label>
        From <input type="date" name="from" value={f.from} />
      </label>
      <label>
        To <input type="date" name="to" value={f.to} />
      </label>
      <label>
        Version <input name="version" value={f.version ?? ''} placeholder="any" size={8} />
      </label>
      <label>
        Launch mode <input name="launch_mode" value={f.launch_mode ?? ''} placeholder="any" size={8} />
      </label>
      <label>
        Deployment <input name="deployment" value={f.deployment ?? ''} placeholder="any" size={8} />
      </label>
      {props.extra}
      <button type="submit">Apply</button>
    </form>
  );
}
