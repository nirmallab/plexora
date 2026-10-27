# Licensing

Plexora has two plans. **Free** is everything Plexora does without a licence:
2-D and 3-D multiplexed images and their tables (z-stacks, time series,
projections, sections and the volume view included), every basic plugin's
manual tools, remote, HPC, Open OnDemand, JupyterHub and notebook viewing, and
export. It needs no account, no network and no activation, and shows no nag.
**Pro** unlocks Plexora AI, Plexora 3D (analysis of a stack beyond looking at
it), spatial transcriptomics and Video Studio. A trial is Pro for 30 days
(`trial: true` on the certificate), not a third plan. External MCP access is
off by default: it is not part of Pro or its trial and is not sold, and an
administrator turns it on for a licence on the platform's admin licence page.
Plexora is licensed by BioCognia (the Licensor in `LICENSE`).

Licences, seats, devices and AI credits live on the BioCognia platform, which
every BioCognia product (Plexora, SCIMAP Pro) shares. People sign in and buy
at [account.biocognia.com](https://account.biocognia.com); Plexora holds no
account, never sees a password, an email address or payment details, and its
only credentials are a device certificate and a 30-minute AI token. The client
side is the shared `biocognia` package, a hard dependency; this repository
keeps only Plexora's gates, its Free semantics and its words.

**Buying** starts on the pricing section of biocognia.com (`links.PRICING_URL`);
the documentation site covers the free app only. Plexora Pro is a yearly subscription per
seat at one flat price per use class, academic or commercial, and both are
bought self-serve: the total is the seat price times the seats, and a
subscription renews at the price it was bought at. Account type, seats and
devices are separate (platform, 2026-10-25): an Individual account may hold
several seats, the others waiting to be given, and becomes a Team only when its
owner invites someone or asks; a seat covers two devices and more are added to
it in pairs (device packs, per product), never as seats. Buy opens Plans & prices in the portal, where Paddle's checkout
runs. The platform then makes the buyer's seat and emails its licence key,
which stays under Licences in the portal to show and copy again. Settings ›
License (on Free and on a trial), `plexora license status` and a Pro
refusal all point at the page. The platform's catalogue id is `plexora.pro`; a trial runs on
it with `trial: true`.

This page is for people working on Plexora and on plugins. Running the
platform is in the `biocognia-platform` repository.

## What is Pro

Entitlements attach to **capabilities and actions**, never to plugins as such
and never to plan names. They are colon paths; a grant covers everything
beneath it (`ai` satisfies `ai:gating:session`; `ai:gating` does not satisfy
`ai:evidence`). The declared set is `plexora/licensing/manifest.py`. Its four
roots, and the first-party plugins licensed as a whole:

| Root | Shown as | What it unlocks |
|---|---|---|
| `ai` | Plexora AI | Every AI path: gating and QC sessions and their analysis, evidence in the viewer, the chat |
| `volume` | Plexora 3D | Surface Studio (`volume:surfaces`), measuring, tracking and reviewing 3-D cells and regions made from them or carried through planes (`volume:objects`), Cellpose over a stack (`volume:segmentation`), Stack QC and 3-D analysis (`volume:analysis`) |
| `transcriptomics` | Spatial transcriptomics | Xenium (`transcriptomics:xenium`), Visium and Visium HD (`transcriptomics:visium`): import, transcript and bin layers, gene lists |
| `mcp` | External MCP access | Off by default (`manifest.OFF_BY_DEFAULT`): Pro tools from an outside agent |
| `plugin:video_studio` | Video Studio | The Video Studio plugin as a whole (`manifest.PLUGINS`) |

A purchased or trial licence carries `manifest.pro_default()`: every root that
is not off by default, then every first-party plugin (`["ai", "volume",
"transcriptomics", "plugin:video_studio"]`). An administrator turns `mcp` on
for a licence (see [External MCP access](#external-mcp-access)) and can switch
any root or plugin on or off per licence on the platform's admin licence
page. Nothing outside the Pro default is sold.

`manifest.py` is also the one editable source of Plexora's **product
manifest** (`manifest.product_manifest()`): the roots `ai`, `volume`,
`transcriptomics`, `mcp` and `plugin` (for `plugin:<name>`) with their labels
and sentences, the first-party plugins (`manifest.PLUGINS`, today Video
Studio as `plugin:video_studio`; the platform shows a switch for each), the
Free tier,
the AI modules the `ai` root opens (`gating`, `qc`, `chat`, the modules of
`plexora/ai/tasks.yaml`) and the trial length. `tools/bioc_sync.py` uploads it,
with the task registry, to the platform; `--check` fails on drift. Plans and
prices are the platform's to edit, and only there; `--check` also fails when
the `plexora.pro` plan does not carry exactly `pro_default()`.

| Entitlement | Capabilities |
|---|---|
| `ai:gating:session` | `gating_session_start`, `gating_session_bulk`, `gating_next`, `gating_answer`, `gating_session_status`, `gating_session_finish`, `gating_report`, `get_panel_context`, `set_panel_context`, `sample_gate_validation_regions`, `render_gate_validation`, `session_report` |
| `ai:gating:analytics` | `compare_gates_across_images`, `profile_marker`, `calibrate_display`, `sample_gating_cells`, `render_gating_collage`, `bivariate_evidence`, `gating_qc`, `score_gate_candidates`, `get_marker_hierarchy` |
| `ai:evidence` | `viewer_show_evidence` |
| `ai:qc:session` | `qc_session_start`, `qc_session_bulk`, `qc_next`, `qc_answer`, `qc_session_status`, `qc_session_finish`, `qc_report` |
| `ai:qc:analytics` | `profile_image_qc`, `render_qc_overview`, `refine_qc_roi`, `sample_qc_examples` |
| `volume:surfaces` | `surface_create`, `surface_suggest_threshold`, `surface_inspect`, `surface_refine`, `surface_neighbours` |
| `volume:objects` | `measure_objects`, `track_objects`, `render_object_views`, `object_z_profile`, `object_review_start`, `object_review_next`, `object_review_answer`, `object_review_finish`, `carry_roi_through_planes`; `create_roi` / `update_roi` with `cells` or `solid` |
| `volume:segmentation` | `segment_3d` (the segmentation plugin as a whole) |
| `volume:analysis` | `check_volume_quality`, `colocalize_3d`, `find_regions_of_interest_3d`, `run_qc_3d`, `set_qc_plane`, `write_qc_3d_regions` |
| `transcriptomics:xenium` | the transcripts plugin as a whole |
| `transcriptomics:visium` | the Visium HD plugin as a whole |

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
looking at a z-stack (`inspect_volume`, `set_z_spacing`, `sample_z_planes`,
`axis_profile`, `render_sections`, `render_projection`, `pin_projection`,
`viewer_set_volume_mode`), reading what Plexora 3D made (`surface_list`,
`surface_measure`, `surface_objects`, `surface_update`, `surface_export`,
`surface_delete`, `get_qc_3d`, `object_review_status`), jobs, artifacts and the
MCP discovery tools. `tests/test_licensing_enforcement.py` pins this table:
changing it is a product decision, made there on purpose.

## How it is enforced

One place for agents: `registry._invoke` calls
`licensing.guards.check_capability(capability)` right after the permission
policy and before anything touches a viewer, a project or a job. Every MCP
transport (stdio and HTTP), the agent HTTP API, jobs at submit and nested
invokes go through it. MCP follows "option B": every tool is listed on every
transport, a Pro tool's description says so, and on Free the call answers

```json
{"ok": false, "error": {"code": "license_required",
  "detail": {"entitlement": "ai:gating:session", "plan_required": "pro",
             "state": "free", "plan": "free", "label": "...",
             "off_by_default": false, "hint": "..."}}}
```

`off_by_default` (`manifest.off_by_default()`) is true for what the Pro
default does not cover: `mcp`, which an administrator turns on for a licence,
and a third-party plugin (`manifest.third_party()`: a `plugin:<name>` that
`manifest.PLUGINS` does not list), which comes from its maker. Its refusal,
hint, MCP tool description and page modal never call it part of Pro, and offer
no trial and no price, none of which would unlock it.

`validate_scope` answers `can_recommend` with `license_required: [...]`, and
`server_info` carries `license: {plan, state, entitlements, mcp, hint?}`.

The first line of `check_capability` is the Free fast path: a capability with
no entitlement returns before the licence is looked at. Nothing Free reads a
licence file, loads `cryptography` or waits on anything
(`tests/test_licensing_hardening.py` asserts the tile path never asks).

Other enforcement points:

- **Tools.** A Pro plugin the licence does not unlock is not on the page at
  all (below). A typed `/panel` URL answers `{"locked": {...}}` with 403, the
  page mounts nothing, and `?tool=` never activates it.
- **Routes.** `guards.require_entitlement` (a view) and `guard_blueprint` (a
  blueprint, or chosen endpoints) answer 403 with a `license` block.
- **Data nodes.** Table operations that serve Pro capabilities
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
infrastructure), and every read of what Pro features produced. A job admitted
on a valid licence finishes even if the licence lapses meanwhile, and gates,
provenance, reports, artifacts and exports stay readable, exportable and
editable after expiry.

## Not granted means not shown

On a licence that does not carry an entitlement, nothing on the page offers
it: no Tools-menu row, launcher, button, context-menu item, section, "Pro"
badge, greyed control or "needs a licence" message. The server still refuses
a direct call (above), and everything a Pro feature made stays readable. An
upsell is a later, separate decision; `PlexoraPro.explain` and `badge` are
kept for it and nothing calls them today.

- **Plugins.** `plugins.unlocked(plugin)` decides, per request. `tools_for`,
  `ready_tools`, `layer_sections_for` and `nav_items` leave out a locked
  plugin, so it has no menu row, launcher, layer section or scripts.
  `installed()`, `tools()` and `find()` keep it, so its routes still refuse,
  its `/panel` still says `locked`, its capabilities are still discovered
  (and refuse), and its `layer_jobs` builders keep serving the layers it made.
- **Templates.** `licensed(entitlement)` is a template global (a context
  processor, never raises): a mixed panel wraps a Pro control in
  `{% if licensed('volume:objects') %}` so the element is absent from the HTML.
- **The client.** A control built in JavaScript (a context-menu item, the
  magic bar's Through planes, a card action) is not built when
  `PlexoraPro.allows(entitlement)` says no. That is a hint from the page's
  render-time licence summary; the server decides. A 403 that still arrives
  (a licence lapsed while the page was open) is a `console.warn` and a quiet
  close, never a modal.
- **Core's scripts are never gated by licence**: a surface or a carried
  region must stay drawable on Free. A locked plugin's own scripts are not
  emitted (see Import below for what that means for transcripts).
- **Import.** Without `transcriptomics` a Xenium, Visium or Visium HD run is
  not detected at all: no layers, no bundle, no warning, and the import
  screens do not mention the formats. A project imported earlier still opens
  with its image, cells, gates, ROIs and QC; its transcript or bin layer keeps
  its card and saved tiles (core's layer route serves them, so exports and
  agent renders draw them), but the gene section is not mounted and the
  viewer does not draw the layer, since the plugin's own scripts do that.
  Nothing is deleted, and the licence brings it all back.
- **MCP** keeps listing every tool whatever the licence: the list is built
  once per server, the licence can change under it within 15 minutes, and a
  Pro tool's description says it is Pro. A call answers `license_required`.

`tests/test_license_visibility.py`, `tests/test_license_volume.py` and
`tests/test_license_transcriptomics.py` pin this.

## External MCP access

An outside coding agent (Claude Code, Codex, Cursor) reaches Plexora through
`plexora mcp serve` and pays for its own model; Plexora's AI harness (the chat
bar, gating and QC runs) runs on models billed through the BioCognia AI
gateway, from the organisation's one credit wallet.
The two are licensed separately by one grant, `mcp`, which is off by default
(`manifest.OFF_BY_DEFAULT`) and which an administrator turns on for a licence:

**A Pro capability called over MCP needs its own entitlement and `mcp`.
Free capabilities never look at the licence, on any path.**

`plexora/mcp/server.py Runtime.invoke`, the one function every external tool
call and resource read passes through, sets `registry.CALL_ORIGIN` to `"mcp"`;
`registry._invoke` runs `guards.check_origin` right after `check_capability`.
The in-app harness and the HTTP agent API never set an origin, so they are
unaffected. The refusal is the usual `license_required` with
`detail.entitlement == "mcp"` and a hint that never claims the refused tool
"keeps working". `server_info.license.mcp` says whether Pro tools answer on
this connection before an agent tries one, and `validate_scope` asked over MCP
lists them under `license_required`. When more of Plexora becomes Pro, those
capabilities are MCP-gated by the same rule with no further code.

Tiers are grant sets, named by the platform's plans, never in code:

| Tier | `entitlements` |
|---|---|
| Plexora application only | `[]` |
| The Pro default (`pro_default()`) | `["ai", "volume", "transcriptomics", "plugin:video_studio"]` |
| The Pro default + MCP | `["ai", "volume", "transcriptomics", "plugin:video_studio", "mcp"]` |
| AI harness only | `["ai"]` |
| MCP access only | `["mcp"]` |

An `["mcp"]`-only licence runs every Free tool and none of the `ai:*` ones
(over MCP or in the app), and the AI token carries no Plexora module, so the
gateway answers `ai_not_entitled`.

**Granting and revoking.** An administrator turns `mcp` on or off for a
licence on the platform's admin licence page, and sets other grants on a
licence or a seat in the portal or admin. Nothing on the user's machine changes:
`plexora mcp serve` refreshes its certificate at start (3 s at most, failing
open to the cached certificate) and every `PLEXORA_MCP_LICENSE_RECHECK_S`
seconds after (default 900, at least 60), on a `plexora-mcp-license` thread,
and `/v1/refresh` answers `revoked` or a re-issued certificate when the grants
changed. A revocation or grant change therefore reaches a running MCP server
within 15 minutes; the desktop app keeps its weekly heartbeat (both share
`license.json`, so an MCP recheck keeps the app fresh too). Free machines,
offline licence files and `BIOCOGNIA_OFFLINE` never start the recheck. The
recheck sends `client: "mcp"`.

**What it resists.** Knowing the endpoint or the command, a stale
`.mcp.json` or Codex config, a hand-edited `license.json` (the signature), a
certificate from before a revocation or grant change (15 minutes online; with
no network, the certificate's own expiry, at most 90 days), clock rollback
(the high-water mark) and another machine's certificate (the binding). It does
not resist editing the installed Python package: that is true of every Pro
check in Plexora, and the only hard boundary is the AI gateway, which an
outside agent using its own model never touches. Executing Pro tools on the
server, or a signed build, would be the next step.

## For plugin authors

Nothing is required: `Plugin.entitlement` and `Capability.entitlement` default
to `None`, which is Free.

```python
Plugin(name="spatial_stats", label="Spatial Stats",
       entitlement="plugin:spatial_stats")          # the whole plugin, licensed as a whole

Plugin(name="mixed", label="Mixed",
       endpoint_entitlements={"run_model": "ai"})   # a Free plugin with one Pro route

Capability(name="mixed.analyze", ..., entitlement="ai")   # a Pro action
Capability(name="spatial_stats.view", ..., entitlement="free")  # opt back out
```

A plugin's entitlement is the default for its capabilities; a capability that
names its own (or `"free"`) keeps it: the capability-level answer wins. Declare
new first-party entitlements in `manifest.py`. A shipped plugin licensed as a
whole (`plugin:<name>`) is listed in `manifest.PLUGINS`, which puts it in the
Pro default and gives staff a switch for it. A third-party `plugin:<name>`
path needs no declaration: it is not part of Pro, its refusal says it comes
from its maker, and its maker licenses it (LICENSE, section 7).

## The licence on a machine

A licence is a `BIOC1` certificate the platform signs for one product:

```
BIOC1.<kid>.<base64url canonical-JSON payload>.<base64url Ed25519 signature>
```

It is verified offline by the `biocognia` package against the platform's
public keys, which ship in that package; the private keys never leave the
platform, and `aud: "plexora"` makes a certificate for any other product
fail. The payload holds opaque identifiers (`org`, `usr`, `seat`, `lic`,
`env`), dates, the catalogue plan id, `use_class` (academic, commercial, and
so on: an axis of its own, never a plan), `ent` (the grants) and `lim`. It
holds no personal data and nothing scientific. An empty grant list unlocks
nothing.

It carries two end dates. `exp` is the certificate's own, at most 90 days out,
and is what stops Pro; it is renewed online without anyone noticing.
`lic_exp` is the licence's, and is the only date Settings and `plexora
license status` show as "Valid until". A renewal date appears only when it
needs acting on: an unrenewed certificate within 14 days of its end (the
machine has been offline), grace, or an offline licence file that runs out
before the licence does.

**Connecting a device.** Settings › License › **Connect this device** (or
`plexora license activate` with no argument) asks the platform for a short
code, shows it with the link to `account.biocognia.com/activate`, and polls.
The person signs in there, approves, and the platform answers with one
certificate per product they hold a seat for, so connecting from Plexora also
connects SCIMAP Pro on the same machine.

Without a browser, use the seat's **licence key** (`BIOC-XXXX-XXXX-XXXX-XXXX`):
it is emailed to you when you are given the seat (and again if it is replaced),
is under Licences at account.biocognia.com, can be shown there again at any
time, works on each of the seat's devices up to its allowance (two, plus any
devices added to the seat), and keeps
working through renewals. Replacing it there stops the old key; devices already
connected stay connected. Paste it in Settings › License › Licence key or pass
it to `plexora license activate`. A one-time activation code (`BIOC-XXXX-XXXX`)
or a `BIOCT1_` token goes in the same places.

Resolution is lazy and fails open to Free. It runs in this order:

1. `PLEXORA_LICENSE_JOB_CERT`: a job certificate, for a container that cannot
   see `$HOME`
2. `BIOCOGNIA_TOKEN`: a `BIOCT1_` token, exchanged once per product and cached
3. `PLEXORA_LICENSE_FILE`: a `.bioc` offline licence file
4. `<config>/biocognia/plexora/license.json` (`user_config_dir("biocognia")`),
   written by `plexora license activate|install` or by Settings > License

The states are `free`, `trial`, `pro_active` (the library's `active`),
`offline_valid`, `grace`, `expired`, `revoked` and `invalid`. Pro runs in the
four in the middle. `licensing.describe()` says `plan` (`free` or `pro`, never
the catalogue id, which is `plan_id`) and `paid`, a boolean: whether a licence
is held, Pro or a trial. `paid` predates the plan's name and is not a plan; it
keeps its name because every surface and the page read it.
`BIOCOGNIA_OFFLINE=1` forbids every network call from every BioCognia product.
A clock high water mark (`now = max(time, hwm)`, refreshed from the platform's
`server_time`) makes winding the clock back pointless.

The only network activity is a background refresh of a cached certificate.
It runs only when the last check is more than 7 days old or the certificate
is within 21 days of expiry, never in a subprocess, and never on Free.

**Device identity** is a random 256-bit secret in
`<config>/biocognia/environment.json` (0600), shared by every BioCognia
product on the machine. The platform sees only its SHA-256 (the binding) and
stores a peppered hash of that. There is no hardware fingerprint. Because the
identity is a random secret, a wiped config directory is a new device:
deactivate before wiping.

### HPC

Register a cluster **once**, from a login node:

```sh
plexora license activate --cluster --name "O2"
# or, in automation:  BIOCOGNIA_TOKEN=BIOCT1_... plexora license activate --cluster
```

The certificate and secret live in `$HOME`, so every compute node, SLURM job,
Open OnDemand session, JupyterHub server and bind-mounted container of that
account uses the one registration. They read files and contact nothing. For a
container that cannot see `$HOME`:

```sh
PLEXORA_LICENSE_JOB_CERT=$(plexora license lease --ttl 48h) singularity exec ...
```

A job certificate (`BIOCD1`) is signed by the cluster's delegation key. It is
verifiable offline, lasts at most 7 days, carries at most the cluster's
grants, and registers nothing. On a machine without a delegation key, `lease`
asks the platform to sign one instead.

## Privacy

A licence call sends the credential or certificate, the device binding, a
coarse platform and scheduler family, and the Plexora version. It never
sends a hostname, a username, a MAC address, a path, a project, or anything
about data. Telemetry, when it is on, carries one word, `license_tier`
(free/pro/trial). The reserved licence identity fields stay unset.
Licensing never reads telemetry, and turning telemetry off changes nothing
about a licence.

## Testing

- The suite is Free by default. `_isolated_license` (autouse, `conftest.py`)
  gives each test its own `BIOCOGNIA_DIR`, sets `BIOCOGNIA_OFFLINE=1`,
  disables the heartbeat, and points the default platform and gateway
  addresses nowhere, so no test can reach the network.
- `pytest.mark.pro` installs a Pro test licence (`conftest.PRO_TEST_GRANTS`:
  the Pro default, Video Studio included, and `mcp`), for suites that
  exercise Pro capabilities in depth.
- `license_issuer` is `biocognia.testing.Issuer` bound to Plexora: a throwaway
  Ed25519 key, the only one trusted during that test. `.issue(**claims)` takes
  `BIOC1` claim names (`ent`, `iat`, `exp`, `offline_until`, ...) and mints the
  narrowest Pro licence (`ent=["ai"]`); `.install()` writes it as an
  activation would.
- `license_service` is `biocognia.testing.FakeService`, a stand-in platform
  with the network switched back on for loopback.
- The certificate format, its vectors and the client's own behaviour
  (resolution, expiry, refresh, delegation) are tested in the `biocognia`
  package; this suite tests Plexora's gates, routes, CLI wording and pages.
- The browser half is `tests/js/license_probe.mjs`.

## Splitting out `plexora-ai`

Pro AI may later ship as a private package, a plugin under
`plexora.plugins` (LICENSE §7 permits separate plugins). Steps done so far:
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
5. `ai/skills.py` and `ai/bench.py` read autogate's
   schemas. The skills manifest should name schemas by
   plugin; the benchmarks move with the AI package.
6. The agent panel's vocabulary in the client should become data-driven.
