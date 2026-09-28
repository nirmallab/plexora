# Licensing

Plexora has two plans. **Free** is everything Plexora does without a licence:
every image, table and modality, every plugin's manual tools, remote, HPC,
Open OnDemand, JupyterHub and notebook viewing, and export. It needs no
account, no network and no activation, and shows no nag. **Paid** unlocks AI
capabilities today and selected advanced features later. A trial is Paid for
30 days (`trial: true` on the certificate), not a third plan.

This page is for people working on Plexora and on plugins. Running the licence
service is in [`licensing/README.md`](../licensing/README.md).

## What is Paid

Entitlements attach to **capabilities and actions**, never to plugins as such
and never to plan names. They are colon paths; a grant covers everything
beneath it (`ai` satisfies `ai:gating:session`; `ai:gating` does not satisfy
`ai:evidence`). The declared set is `plexora/licensing/manifest.py`, and a
Paid certificate carries `["ai"]`.

| Entitlement | Capabilities |
|---|---|
| `ai:gating:session` | `gating_session_start`, `gating_session_bulk`, `gating_next`, `gating_answer`, `gating_session_status`, `gating_session_finish`, `gating_report`, `get_panel_context`, `set_panel_context`, `sample_gate_validation_regions`, `render_gate_validation`, `session_report` |
| `ai:gating:analytics` | `compare_gates_across_images`, `profile_marker`, `calibrate_display`, `sample_gating_cells`, `render_gating_collage`, `bivariate_evidence`, `gating_qc` |
| `ai:evidence` | `viewer_show_evidence` |

Everything else is Free, including all of manual gating (`set_gate`,
`adjust_gate`, `apply_gate_to_dataset`, `suggest_auto_gate`, export,
provenance), every read, every viewer primitive, ROIs, jobs, artifacts and the
MCP discovery tools. `tests/test_licensing_enforcement.py` pins this table:
changing it is a product decision, made there on purpose.

## How it is enforced

One place for agents: `registry._invoke` calls
`licensing.guards.check_capability(capability)` right after the permission
policy and before anything touches a viewer, a project or a job. Every MCP
transport (stdio and HTTP), the agent HTTP API, jobs at submit and nested
invokes go through it. MCP follows "option B": every tool is listed on every
transport, a Paid tool's description says so, and on Free the call answers

```json
{"ok": false, "error": {"code": "license_required",
  "detail": {"entitlement": "ai:gating:session", "plan_required": "paid",
             "state": "free", "plan": "free", "label": "...", "hint": "..."}}}
```

`validate_scope` answers `can_recommend` with `license_required: [...]`, and
`server_info` carries `license: {plan, state, entitlements}`.

The first line of `check_capability` is the Free fast path: a capability with
no entitlement returns before the licence is looked at. Nothing Free reads a
licence file, loads `cryptography` or waits on anything
(`tests/test_licensing_hardening.py` asserts the tile path never asks).

Other enforcement points:

- **Tools.** A Paid plugin's `/panel` answers `{"locked": {...}}` with 403, the
  page opens the Paid modal (`PlexoraPaid.explain`) and mounts nothing, and
  `?tool=` never activates it. It stays listed in the Tools menu with a Paid
  badge.
- **Routes.** `guards.require_entitlement` (a view) and `guard_blueprint` (a
  blueprint, or chosen endpoints) answer 403 with a `license` block.
- **Data nodes.** Table operations that serve Paid capabilities
  (`gating.autogate.*`) need an `X-Plexora-Entitlement-Proof` header: an
  HMAC under the node token, minted by the primary only when its licence
  allowed the work (`licensing/tokens.py`). Nodes hold no licence and never
  call the service. This is a second check behind the registry, not a barrier
  against someone holding the node token.
- **The `gating-packet` MCP resource**, the one gating resource that reads the
  session store directly, checks `ai:gating:session` itself.

Deliberately not guarded: the `agent_session/<id>/control` route (the user's
own stop/pause control over a session), raw `/agent/v1` viewer commands (Free
infrastructure), and every read of what Paid features produced. A job admitted
on a valid licence finishes even if the licence lapses meanwhile, and gates,
provenance, reports, artifacts and exports stay readable, exportable and
editable after expiry.

## For plugin authors

Nothing is required: `Plugin.entitlement` and `Capability.entitlement` default
to `None`, which is Free.

```python
Plugin(name="spatial_stats", label="Spatial Stats",
       entitlement="plugin:spatial_stats")          # the whole plugin is Paid

Plugin(name="mixed", label="Mixed",
       endpoint_entitlements={"run_model": "ai"})   # a Free plugin with one Paid route

Capability(name="mixed.analyze", ..., entitlement="ai")   # a Paid action
Capability(name="spatial_stats.view", ..., entitlement="free")  # opt back out
```

A plugin's entitlement is the default for its capabilities; a capability that
names its own (or `"free"`) keeps it: the capability-level answer wins. Declare
new first-party entitlements in `manifest.py`; `plugin:<name>` paths need no
declaration.

## The licence on a machine

A licence is an Ed25519-signed certificate:

```
PLEXORA1.<kid>.<base64url canonical-JSON payload>.<base64url signature>
```

It is verified against the public keys in `plexora/licensing/keys.py`. The
private keys never leave the licence service, and `aud: "plexora"` makes a
certificate for any other product fail. The payload holds identifiers,
dates, the plan, `use_class` (academic, commercial, and so on: an axis of its
own, never a plan) and grants. It holds no personal data and nothing
scientific. An empty grant list unlocks nothing.

It carries two end dates. `expires_at` is the certificate's own, at most 90
days out, and is what stops Paid; it is renewed online without anyone
noticing. `license_expires_at` is the licence's, and is the only date Settings
and `plexora license status` show as "Valid until". A renewal date appears
only when it needs acting on: an unrenewed certificate within 14 days of its
end (the machine has been offline), grace, or an offline licence file that
runs out before the licence does. Refresh replaces a certificate whose
`license_expires_at` no longer matches the licence, so an extension shows up
at the next check.

Resolution (`state.py`) is lazy and fails open to Free. It runs in this order:

1. `PLEXORA_LICENSE_JOB_CERT`: a job certificate, for a container that cannot
   see `$HOME`
2. `PLEXORA_LICENSE_TOKEN`: a licence token, exchanged once and cached
3. `PLEXORA_LICENSE_FILE`: a `.plexora` offline licence file
4. `license.json` in `user_config_dir("plexora")/license`, written by
   `plexora license activate|install` or by Settings > License

The states are `free`, `trial`, `paid_active`, `offline_valid`, `grace`,
`expired`, `revoked` and `invalid`. Paid runs in the four in the middle.
`PLEXORA_LICENSE_OFFLINE=1` forbids every licensing network call. A clock high
water mark (`_now = max(time, hwm)`, refreshed from the service's
`server_time`) makes winding the clock back pointless.

The only network activity is a background refresh of a cached certificate.
It runs only when the last check is more than 7 days old or the certificate
is within 21 days of expiry, never in a subprocess, and never on Free.

**Environment identity** is a random 256-bit secret in `environment.json`
(0600). The service sees only its SHA-256 (the binding) and stores a peppered
hash of that. There is no hardware fingerprint. The trial fingerprint is a
separate thing, sent only when a trial is requested: an HMAC of the OS
machine id, the same value `py-machineid`'s `hashed_id("plexora")` produces.

### HPC

Register a cluster **once**, from a login node:

```sh
plexora license activate PLEX-XXXX-XXXX-XXXX-XXXX --cluster --name "O2"
# or, in automation:  PLEXORA_LICENSE_TOKEN=PLXT1_... plexora license environment register --cluster
```

The certificate and secret live in `$HOME`, so every compute node, SLURM job,
Open OnDemand session, JupyterHub server and bind-mounted container of that
account uses the one registration. They read files and contact nothing. A
laptop plus a cluster is two environments, the default seat allowance. For a
container that cannot see `$HOME`:

```sh
PLEXORA_LICENSE_JOB_CERT=$(plexora license lease --ttl 48h) singularity exec ...
```

A job certificate (`PLEXORAD1`) is signed by the cluster's delegation key.
It is verifiable offline, lasts at most 7 days, carries at most the cluster's
grants, and registers nothing. `plexora license lease --server` asks the
service to sign one instead.

## Privacy

A licence call sends the credential or certificate, the environment binding,
a coarse platform and scheduler family, and the Plexora version. It never
sends a hostname, a username, a MAC address, a path, a project, or anything
about data. Telemetry, when it is on, carries one word, `license_tier`
(free/paid/trial). The reserved licence identity fields stay unset.
Licensing never reads telemetry, and turning telemetry off changes nothing
about a licence.

## Testing

- The suite is Free by default. `_isolated_license` (autouse) gives each test
  its own licence directory, sets `PLEXORA_LICENSE_OFFLINE=1`, and disables
  the heartbeat.
- `pytest.mark.paid` installs a Paid test licence, for suites that exercise AI
  capabilities in depth.
- `license_issuer` gives you a throwaway Ed25519 key, the only one trusted
  during that test. Use `.issue(**claims)`, `.install()` and `.sign()`.
- `license_service` is a stand-in licence service with the network switched
  back on.
- The cross-language contract is `licensing/vectors/plexora-license-vectors.json`
  (`licensing/tools/make_test_vectors.py`). The Worker re-signs every vector
  byte for byte, and `tests/test_licensing_vectors.py` verifies them.
- The browser half is `tests/js/license_probe.mjs`.

## Splitting out `plexora-ai`

Paid AI may later ship as a private package, a plugin under
`plexora.plugins` (LICENSE §4 permits separate plugins). Steps done so far:
gating's shared mixture maths (`server/mixture.py`) and gate provenance
(`server/provenance.py`) now live in gating core. The manual half no longer
imports the automatic half for them, and the old `autogate.*` names are
aliases. The remaining couplings are pinned in
`tests/test_licensing_hardening.py::KNOWN_COUPLINGS`:

1. `gating/capabilities.py` aggregates the AI capabilities. Move that to a
   `Plugin.capabilities_factory` the AI package supplies.
2. `gating/server/routes.py` agent-session control and event routes. These
   move with the engine; the control route stays reachable for sessions
   already on disk.
3. `gating/server/tableops.py` registers autogate's node operations. The AI
   package registers its own on import.
4. `mcp/resources_gating.py`. This needs a `Plugin.mcp_factory` hook so
   plugins contribute MCP resources.
5. `ai/skills.py` and `ai/bench.py` read autogate's schemas. The skills
   manifest should name schemas by plugin.
6. The agent panel's vocabulary in the client should become data-driven.
