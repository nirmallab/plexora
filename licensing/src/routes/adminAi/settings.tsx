/** @jsxImportSource hono/jsx */
/**
 * /admin/ai/settings: the AI knobs an admin may change without a redeploy
 * (src/ai/settings.ts), one form per group, and the one-shot migration from
 * the previous route table while it still serves.
 */
import { hasTaskRouting } from '../../ai/routing';
import { legacyRows } from '../../ai/routing_legacy';
import { describe as describeSettings, GROUPS } from '../../ai/settings';
import { type App, page } from '../../http';
import { Action, Badge, Field, JsonForm, Note, Section, SelectField } from '../../ui/components';
import { aiShell, API, BASE } from './shared';

const BLURBS: Record<string, string> = {
  Routing: 'How models are chosen and priced.',
  Limits: 'How much one person, account or token may ask for.',
  Credit: 'What accounts pay, and how much a call may hold.',
  Reliability: 'Retries and circuit breakers.',
  Capacity: 'What the capacity monitor on the Overview grades against: the Cloudflare plan and the provider\'s written limits.',
  Retention: 'How long records are kept.',
  Access: 'Who may use Plexora AI at all.',
};

export async function settingsPage(c: App) {
  const [settings, legacy, taskRouting] = await Promise.all([describeSettings(c.env), legacyRows(c.env),
    hasTaskRouting(c.env)]);

  return page(c, aiShell(c, `${BASE}/settings`, (
    <>
      {!taskRouting && legacy.routes > 0 ? (
        <Note warn>The previous route table ({legacy.routes} routes, {legacy.models} catalogued models) still serves every
          call. Migrating copies it into approved models and task assignments, with the same models in the same order;
          nothing changes for users. <Action action={`${API}/migrate-legacy`} label="Migrate" small reload
            done="Migrated: task routing now serves." confirm="Copy the route table into approved models and task routing?" /></Note>
      ) : null}
      <p class="hint">Changes apply within seconds, with no redeploy. An empty field uses the value shown in it
        (wrangler.toml, or the code's default). The on/off switch is at the top of every page.</p>
      {GROUPS.map((group) => {
        const rows = settings.filter((x) => x.group === group && x.name !== 'AI_ENABLED');
        if (!rows.length) return null;
        return (
          <Section title={group}>
            <p class="hint">{BLURBS[group]}</p>
            <JsonForm action={`${API}/settings`} method="PUT" submit={`Save ${group.toLowerCase()}`} done="Saved." reload>
              <div class="settings-grid">
                {rows.map((x) => {
                  const source = x.source === 'admin' ? <Badge tone="accent">set here</Badge>
                    : <span class="muted">({x.source})</span>;
                  return x.flag ? (
                    <SelectField label={x.label} name={x.name} id={`s-${x.name}`}
                      value={x.source === 'admin' ? String(x.value) : 'default'} hint={<>{x.help} {source}</>} options={[
                        { value: 'default', label: `Default (${x.fallback ? 'on' : 'off'})` },
                        { value: '1', label: 'On' }, { value: '0', label: 'Off' }]} />
                  ) : (
                    <Field label={`${x.label}${x.unit && !x.label.toLowerCase().includes(x.unit) ? `, ${x.unit}` : ''}`}
                      name={x.name} type="number" step="any" keepEmpty min={x.min} max={x.max} id={`s-${x.name}`}
                      value={x.source === 'admin' ? x.shown : undefined} placeholder={String(x.fallback_shown)}
                      hint={<>{x.help} {source}</>} />
                  );
                })}
              </div>
            </JsonForm>
          </Section>
        );
      })}
      <p class="hint">An account's own daily limits, markup and allowance are set on its licence page.</p>
    </>
  )));
}
