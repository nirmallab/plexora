# Plexora, AI-native: what to build next

*For the people building Plexora. It covers what the AI foundation (MCP server,
capability registry, visual evidence, skills, viewer control) does today, what
it deliberately left out, and what should be built next so that agents like
Claude Code, Codex and Cursor can do real scientific work through Plexora,
with evidence a scientist can check.*

---

## 1. Where things stand

An external agent now reaches Plexora through `plexora mcp serve` (stdio,
registered with `plexora ai setup claude|codex|cursor`, or streamable HTTP
with scoped tokens for an agent on another machine). There are two surfaces
over one capability registry:

| Surface             | What an agent can do                                                                                                                                                                                                                                                              | Where                                                                  |
|---------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|------------------------------------------------------------------------|
| Headless data plane | list and inspect projects and datasets, resource status, channels, markers, distributions; gating (get, auto, summary, set, adjust, write to source); ROIs (list, get, create, update, delete, count cells); scene view (assets, coordinate systems, entity sets, feature spaces); cohort gating as a job; jobs (get, list, wait with progress, cancel); undo; session report | `plexora/agent`, `plugins/*/capabilities.py`                           |
| Visual evidence     | `render_region` (channels, windows, mask outlines or fill, gate highlight, cell ids, scale bar, manifest); gate-field sampling; three-panel gate validation; single-cell galleries and `explain_cell`; other layers composited; content-addressed artifact store                                                                                     | `agent/render.py`, `gate_sampling.py`, `gate_panel.py`, `artifacts.py` |
| Live viewer control | list viewers, get state, open a project or tool, set channels (session-only unless `persist`), navigate (box, point, µm field, cell, ROI), layers, cell mode, capture, show evidence; change events so an open viewer redraws after an agent writes                               | `/agent/v1`, `agent/viewer.py`, `services/agentBridge.js`              |
| Skills              | `dataset-triage`, `visual-inspection`, `marker-qc`, `visual-gating`, each validated against the live tool names                                                                                                                                                                   | `plexora/ai/skills`                                                    |

These guarantees hold today and anything new must keep them:

- **Agent reads never swap the viewer's project.** Handles are provider-backed.
  `tests/test_agent_architecture.py` enforces this.
- **Every write is receipted and audited.** Each write gets an `operation_id`,
  before and after state, an `undo_hint`, and a line in `.agent/audit.jsonl`.
- **Source files are written only on explicit request.** That requires the
  server flag and `confirm: true` on the call.
- **Renders are deterministic.** The same spec gives the same bytes and the
  same manifest.
- **There is no model-provider code in Plexora.**

---

## 2. Gaps found while building it

These were small, concrete issues found during implementation. All four have
shipped.

1. **The brightest cell is never positive.** *Shipped.* The rule is now
   `low < value <= high` everywhere a gate is evaluated: `apply_range_mask`,
   centroid tiles, gating's summary, the browser's `evaluateGateMask`, and
   every agent count (`agent/gate_rule.py`). The agent compares in float32, as
   the viewer's float32 columns do. A float64 comparison would drop a column
   maximum that rounds up.
2. **Masks on a data node are not drawn.** *Shipped.* The node has
   `POST /seg/<id>/region`, which is padded like `padded_label_region`, and
   both segmentation providers have `read_region`, as does `SegHandle`, which
   is the public seam. A node-hosted mask renders byte-identical to the local
   file. An unreachable node still falls back to centroids, with status
   `unavailable`.
3. **Layers are not composited.** *Shipped, scoped.* `server/utils/layer_composite.py`
   draws image layers placed by translation and uniform scale (through
   `SourceImage`, so every format and node reads as usual), points layers as
   dots, and label layers in the reference grid. Rotated, sheared or
   anisotropic layers, shapes layers and binned layers are listed in
   `not_rendered` with the reason. `RenderInput.layers` chooses the layers
   (default `"visible"`). Figure Builder's export can call the same
   `composite` for its panels' layers; it does not yet.
4. **`validate_scope` matches words.** *Shipped.* Matching is tiered: names
   and tags, then purpose words, then the biological task
   (`agent/tasks.py`). A domain-phrased request gets `can_recommend` with what
   to establish first, such as which marker. A task Plexora does not do (for
   example neighbourhood enrichment) gets `outside_domain` with the reason.

---

## 3. P0 (shipped)

*Everything in §3 has shipped.*

### 3.1 Jobs for long work (`execution="job"`), shipped
Synchronous calls block the agent and cannot be cancelled. Whole-slide
renders, cohort-wide gating, and mask conversions need to run as jobs.
`agent/jobs.py` holds the store; `job_get`, `job_list`, `job_wait` (which
streams progress) and `job_cancel` are the tools. The first real consumer is
`apply_gate_to_dataset` (same threshold, one receipt per image, reported per
image).
- `Capability.execution="job"` returns `{job_id}` at once. `plexora://job/{id}`
  (already reserved) reports status, progress and result.
- MCP progress notifications while a job runs. `cancel_job(job_id)`.
- Jobs persist under `.agent/jobs/` so a restarted MCP process can report them.
- Reuse `layer_jobs` threading. Never run a job inside a Waitress worker.

### 3.2 Undo and a session report, shipped
Receipts already carry `undo_hint`; nothing consumes them yet.
- `undo_operation(operation_id)` replays the hint through `invoke`, is itself
  receipted, and refuses when the state has moved on (revision mismatch).
- `session_report(since=...)` produces a Markdown or HTML report of what the
  agent did: each operation, the artifacts it rested on (embedded), and the
  before/after numbers. This is the provenance a methods section needs.
- `plexora ai audit` gives a CLI view of `.agent/audit.jsonl`.

As shipped, a destructive undo hint (deleting the region the agent drew) may
run without `--allow-destructive`, but only when it exactly reverses a
receipted operation whose revision still matches. Source-file writes are
never relaxed.

### 3.3 Cell-level evidence, shipped
Gating is judged cell by cell, but the agent mostly sees fields.
- `render_cell_gallery(project, cell_ids | {marker, class}, n, crop_um)` returns
  a grid of per-cell crops with outlines, id labels and the marker value
  under each, sorted by expression. Borderline galleries (the 24 cells nearest
  the gate) are the single most useful gating view.
- `explain_cell(project, cell_id)` returns every marker value with its
  percentile, the region it is in, its neighbours, and a crop.


### 3.4 Streamable HTTP transport, with tokens, shipped
stdio is right for a local client. It is wrong for an agent that is not on the
machine with the data: an HPC login node, a cloud Codex, a teammate's IDE.
- `plexora mcp serve --transport http` behind the existing auth token (or
  per-client tokens issued by `plexora ai token create --scope read`).
- Same Waitress-safe rule: run it in its own process (ASGI), not inside the
  viewer's worker pool.
- Scoped tokens map onto `Policy` (read-only, no raw pixels, and so on).

As shipped, HTTP requires a token even on loopback (`--no-auth` is allowed
only there). Tokens only narrow the server's policy, and every audit line
records the token as its `principal`. `docs/AI_AGENTS_REMOTE.md` gives the
HPC recipe.

---

## 4. P1: cohort, hand-off, grounding

### 4.1 Cohort and dataset capabilities
- `apply_gate_to_dataset(dataset, marker, rule)`: the same-threshold rule
  shipped with §3.1. Still to do: per-image auto gates
  (`rule="auto_per_image"`), with a report of their spread.
- `dataset_qc(dataset, markers)`: per-image distribution summaries, outlier
  images, missing markers. Returns a table and a montage.
- Report the experimental unit and the number of images with every
  cross-image number. Cells are not replicates. This rule belongs in the
  capability output, not only in a skill.

### 4.2 Hand-off to analysis servers (SCIMAP Pro and others)
Plexora is where cells are seen and gated. Downstream statistics live in
other MCP servers (this lab already runs `scimappro`).
- `export_for_analysis(project | dataset, include=[gates, rois, phenotypes])`
  writes an AnnData with the gates in `uns`, ROI and phenotype columns in
  `obs`, and a provenance block (operation ids, artifact ids). It is a
  `source_file_write`-class capability writing a *new* file, so it can be
  allowed more liberally than editing the user's file.
- A `handoff` resource describing the export, so the agent can pass the path
  and provenance to the next server in one step.

### 4.3 Better scope and semantic grounding
- Marker synonyms and canonical names (CD8 / CD8a / CD8A, PanCK / pan-CK /
  KRT), from a shipped vocabulary plus per-project aliases. Use them in
  `validate_scope`, channel resolution and skills.
- Capability descriptions tagged with the biological tasks they serve
  (phenotyping, QC, spatial neighbourhood), so a domain-phrased request
  resolves to capabilities and not to `outside_domain`.
- `suggest_next(project)`: from the manifest and the audit log, return what is
  missing and the next sensible step. The agent should not have to rediscover
  the workflow each time.

### 4.4 Segmentation QC
Several gating "errors" turn out to be segmentation errors.
- `segmentation_qc(project, fields)`: outline/stain overlap, cell-size
  distribution, likely merges (large, multi-nucleated) and splits, with a
  `render_region` preset per issue class.
- Feed the result into visual-gating's `segmentation_problem` assessment,
  so the agent stops adjusting a gate to compensate for a bad mask.

### 4.5 Viewer: pointing, not just steering
- An **ephemeral agent overlay layer**: highlighted cells, a pointer, boxes
  with captions. Session-only, so the user can see what the agent is talking
  about without anything being saved. `focus_cell` currently centres on a
  cell but does not mark it.
- **Side-by-side comparison** (two gates, two markers, two projects) in
  synchronised views.
- **Confirmation through the client**: use MCP elicitation, so a
  source-file write or delete asks the *user* in the agent's own UI, instead
  of trusting `confirm: true` typed by the model.
- Server push, where proxies allow it (SSE with long-poll fallback), to
  bring command latency from about 15 s worst-case to under a second.

### 4.6 Figures from evidence
- `figure_from_artifacts(artifact_ids, layout)` creates a Figure Builder
  figure whose panels are the agent's renders.
  `artifacts.as_capture_scene()` already produces the capture shape.
- Overlays re-rendered at export resolution. This is the same work as §2.3
  (layer compositing) and would unlock both.

---

## 5. P2: platform

- **Stable persisted ids.** Asset, entity-set and feature-space ids live in
  `Project.extra["agentIds"]`, so they survive renames and re-imports (scene
  schema 0.6).
- **Notebook parity.** `plexora.agent` capabilities callable from Python
  (`plexora.agent.call("render_region", ...)`) with the same receipts. A
  notebook user and an agent should leave the same audit trail.
- **More clients.** `plexora ai setup` for VS Code (Copilot agent mode),
  Gemini CLI, Windsurf and Zed; each is a small config-shape adapter in
  `ai/setup.py`.
- **MCP prompts.** Expose the four skills as MCP prompts ("slash commands"),
  so a user can start "Gate a marker" from the client's menu.
- **Resource subscriptions.** `plexora://project/{p}/gates` notifies
  subscribers when gates change (from the viewer or another agent).
- **Policy in configuration.** Per-capability allow and deny lists in settings
  (for example, "never `delete_roi` on shared roots"), and a server-wide
  dry-run mode where every write returns the receipt it *would* have made.
- **Egress controls for sensitive data.** Redact file paths and patient-like
  identifiers from outputs by policy. Audit every `rendered_pixels` egress
  with the artifact id.

---

## 6. How to know it is working

- **Skill evaluations on synthetic truth.** Extend `tests/agent_fixtures.py`
  with known phenotypes, known segmentation errors and staining gradients.
  Run each skill's decision loop against them in CI (no model: a scripted
  stand-in agent, as in `test_agent_vertical_slice.py`). Then run a real model
  as a periodic evaluation that is not part of CI.
- **Evidence coverage.** For every claim in a session report, check there is a
  cited artifact or number.
- **Safety invariants.** These stay as tests, whatever is added: no
  `data_model` import in the agent layer, no read loads the viewer's project,
  no source write without flag and confirmation, and every write has an
  audit line.
