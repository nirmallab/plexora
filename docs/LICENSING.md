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
Paid certificate carries `["ai"]`, plus `mcp` when it includes external MCP
access (see [External MCP access](#external-mcp-access)).

| Entitlement | Capabilities |
|---|---|
| `ai:gating:session` | `gating_session_start`, `gating_session_bulk`, `gating_next`, `gating_answer`, `gating_session_status`, `gating_session_finish`, `gating_report`, `get_panel_context`, `set_panel_context`, `sample_gate_validation_regions`, `render_gate_validation`, `session_report` |
| `ai:gating:analytics` | `compare_gates_across_images`, `profile_marker`, `calibrate_display`, `sample_gating_cells`, `render_gating_collage`, `bivariate_evidence`, `gating_qc`, `score_gate_candidates`, `get_marker_hierarchy` |
| `ai:evidence` | `viewer_show_evidence` |
| `ai:qc:session` | `qc_session_start`, `qc_session_bulk`, `qc_next`, `qc_answer`, `qc_session_status`, `qc_session_finish`, `qc_report` |
| `ai:qc:analytics` | `profile_image_qc`, `render_qc_overview`, `refine_qc_roi`, `sample_qc_examples` |

Everything else is Free, including all of manual gating (`set_gate`,
`adjust_gate`, `apply_gate_to_dataset`, `suggest_auto_gate`, export,
provenance), all of manual quality control and everything that reads or
changes what QC made (`get_qc_results`, `list_qc_results`,
`activate_qc_result`, `set_qc_strictness`, `approve_qc_roi`, `dismiss_qc_finding`, `refresh_qc`,
`set_qc_cycles`, `export_qc`, `write_qc_to_source`, `reset_qc`, `restore_qc`),
the image checks and their writers (`run_blur_check`, `set_blur_check`,
`write_blur_regions`, `compute_registration_mismatch`,
`write_registration_regions`, `run_segmentation_qc`,
`write_segmentation_flags`),
every read, every viewer primitive (`viewer_show_shapes` included), ROIs,
jobs, artifacts and the MCP discovery tools. `tests/test_licensing_enforcement.py` pins this table:
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
`server_info` carries `license: {plan, state, entitlements, mcp, hint?}`.

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
- **The `gating-packet` and `qc-packet` MCP resources**, the ones that read a
  session store directly, check `ai:gating:session` / `ai:qc:session` themselves,
  and `mcp` after it (they exist only over MCP).

Deliberately not guarded: the `agent_session/<id>/control` route (the user's
own stop/pause control over a session), raw `/agent/v1` viewer commands (Free
infrastructure), and every read of what Paid features produced. A job admitted
on a valid licence finishes even if the licence lapses meanwhile, and gates,
provenance, reports, artifacts and exports stay readable, exportable and
editable after expiry.

## External MCP access

An outside coding agent (Claude Code, Codex, Cursor) reaches Plexora through
`plexora mcp serve` and pays for its own model; Plexora's AI harness (the chat
bar, gating and QC runs) runs on models billed through the licence service.
The two are licensed separately by one add-on, `mcp` (`manifest.ADD_ONS`):

**A Paid capability called over MCP needs its own entitlement and `mcp`.
Free capabilities never look at the licence, on any path.**

`plexora/mcp/server.py Runtime.invoke`, the one function every external tool
call and resource read passes through, sets `registry.CALL_ORIGIN` to `"mcp"`;
`registry._invoke` runs `guards.check_origin` right after `check_capability`.
The in-app harness and the HTTP agent API never set an origin, so they are
unaffected. The refusal is the usual `license_required` with
`detail.entitlement == "mcp"` and a hint that never claims the refused tool
"keeps working". `server_info.license.mcp` says whether Paid tools answer on
this connection before an agent tries one, and `validate_scope` asked over MCP
lists them under `license_required`. When more of Plexora becomes Paid, those
capabilities are MCP-gated by the same rule with no further code.

Tiers are grant sets, named on the licence Worker's issue page, never in code:

| Tier | `entitlements` |
|---|---|
| Plexora application only | `[]` |
| Plexora AI harness (the Paid default) | `["ai"]` |
| MCP access | `["mcp"]` |
| AI harness + MCP | `["ai", "mcp"]` |

An `["mcp"]`-only licence runs every Free tool and none of the `ai:*` ones
(over MCP or in the app), and `/v1/ai/token` answers `ai_not_entitled`.
Licences issued before the add-on keep `["ai"]`: every Free tool still works
over MCP; the AI tools need an administrator to add `mcp`.

**Granting and revoking.** An administrator sets grants on a licence (with
"apply to every active licence of this organisation") or overrides them per
seat on `/admin/licenses/:id`; an organisation's owners and admins can narrow
a seat in the portal (never beyond what the licence carries). Nothing on the
user's machine changes: `plexora mcp serve` refreshes its certificate at start
(3 s at most, failing open to the cached certificate) and every
`PLEXORA_MCP_LICENSE_RECHECK_S` seconds after (default 900, at least 60), on
a `plexora-mcp-license` thread, and `/v1/refresh` answers `revoked` or a
re-issued certificate when the grants changed. A revocation or grant change
therefore reaches a running MCP server within 15 minutes; the desktop app
keeps its weekly heartbeat (both share `license.json`, so an MCP recheck
keeps the app fresh too). Free machines, offline licence files and
`PLEXORA_LICENSE_OFFLINE` never start the recheck. The recheck sends
`client: "mcp"`, which the Worker records as `environments.last_mcp_at` (at
most hourly) and an `environment.mcp_seen` event (at most daily), shown as
"MCP seen" on the licence page whether or not the seat holds `mcp`.

**What it resists.** Knowing the endpoint or the command, a stale
`.mcp.json` or Codex config, a hand-edited `license.json` (the signature), a
certificate from before a revocation or grant change (15 minutes online; with
no network, the certificate's own expiry, at most 90 days), clock rollback
(the high-water mark) and another machine's certificate (the binding). It does
not resist editing the installed Python package: that is true of every Paid
check in Plexora, and the only hard boundary is the AI gateway, which an
outside agent using its own model never touches. Executing Paid tools on the
server, or a signed build, would be the next step.

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
