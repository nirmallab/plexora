# Gate an image automatically

Gate every marker of one image, end to end. Plexora does everything code can
decide: display calibration, technical QC, the mixture fit, candidate
thresholds, whole-image checks, the writes and their receipts. The agent is
asked only what code cannot settle, one small decision packet at a time.
**Nobody types a threshold.**

This skill is the coordinator's: plan, start, hand the packets out, finish,
report. Answering packets is skill gate-packets. The session's
`reading_guide` says how to read every picture and holds every answer schema;
neither skill restates it. A start that delegates does not send it (workers
fetch their own); `gating_session_status` without `known_guide` does.

## When to use

- "Gate this image", "threshold all markers", "call positives for every
  marker".
- One marker the user wants done rather than walked through:
  `gating_session_start` with `markers` holding just that marker.

## When not to use

- Several images of one cohort: gate-dataset.
- Reviewing existing gates: review-gating. Why one marker is hard:
  diagnose-marker. Setting the number by eye: visual-gating.

## Required features

A cell table with markers and cell-id/x/y roles (`inspect_project` says). An
image channel per marker for the looks (a column without one is gated from its
numbers alone, at capped confidence). A segmentation mask makes the looks much
better.

## QC-passed cells only

When QC has been run on the image, every estimate is made on the cells QC
passed; the gate still applies to every cell. The session option `qc` is
`strict` (the default: cells QC called exclude or warn, and per marker the
cells QC flagged that marker unreliable in), `exclude` (warn calls kept) or
`off` (only when the user asks, or QC is wrong -- say which). The start's
`qc_exclusion` says per image how many cells were left out; report it with the
gates. Above {{qc_exclusion.heavy_percent}}% left out it carries a `warning`
and confidence is capped: tell the user. No QC on the image: offer qc-image
first when the tissue looks damaged; do not block on it. QC changed after a
gate was decided: `gating_qc` lists the marker under `stale_qc`.

## Which channels are gated

- **Never a structural channel**: a nuclear counterstain or an
  autofluorescence or blank channel, recognised by name pattern, is listed in
  `skipped_markers` and never gated, not even when named in `markers`. Do not
  report them as failures.
- **Always a continuously expressed marker** (`binary` false: `HLA-ABC`,
  `B2M`, `PD-L1`): gated where expression rises out of background, never
  skipped.

## Where a look starts: scored candidates

Before a marker's first look the session scores candidate thresholds on the
numbers and the biology (`evidence.scored`) and starts from the proposal,
`score`: the Auto mixture gate `gmm`; `onset`, where background stops
explaining the cells (local false-discovery rate below
{{scoring.onset_percent}}%); `ceiling`, the background peak plus
{{scoring.ceiling_sd}} core sds; `bio:<partner>`, where a subset or
co-expressed partner's share has done most of its rise (`robust` false when it
only follows the partner's own gate); `anti:<partner>` for an exclusive one;
`sens:<partner>`, the {{scoring.sens_percent}}th percentile among a subset
marker's positives. The strictest robust partner curve leads. The numbers
choose where to look; the cells decide. Partners are evidence, so fill the
panel context with care: give each unresolved marker the lineage-specific
partners you know (`HLA-DR` for a dendritic or macrophage marker, `CD163` for
`CD68`, `SOX10` for a melanocytic marker) and the exclusive ones; a wrong
partner misleads the start, so skip what you do not know.

## The sample's biology and the evidence graph

Plan before the first look: what the user said about the sample, the panel,
the marker tree, then the numbers and the looks.

- **Tissue and disease.** When the user names the sample's biology, pass it to
  `gating_session_start` as `biology` (`tissue`, `disease`, `notes`, in their
  words). Its relations are laid over the panel, and each look carries
  `evidence.biology`. It is a prior: it never moves a gate and never makes a
  population exist. Without it the session takes it from metadata or infers
  it from the panel (`source`): say an inferred context as inferred, ask the
  user to confirm it, never report it as the diagnosis.
- **The marker tree.** `get_marker_hierarchy` shows each marker's `stage`,
  `parents` and `children`. Gating follows it, broad markers first; it guides
  and never blocks. Describe unplaced markers in the panel context.
- **Reliable references only.** Each gate earns a grade as evidence; a
  failed one is never used, and a low one only when nothing better exists. A
  parent whose own distribution is weak waits for its children
  (`defer_parents`). A marker that leaned on a reference which later failed
  carries `stood_on_failed`: say so in the report.

## Decision logic

1. `inspect_project`, `get_panel_context`, `get_marker_hierarchy`. Fill only
   what you know of `unresolved` markers with `set_panel_context`
   (`source: "ai"`); never invent partners.
2. `gating_session_start` with `scope: "project"`, the project, `mode:
   "apply"` (each gate written as decided, undoable) unless the user asked to
   review first (`"propose"`), `biology`, and `agent` (your model's name).
   `mirror: true` when the user watches in a viewer. Set-up packets come
   first and are yours to answer:
   - `expression_setup` (`needs_setup`): which matrix to gate. When
     `ask_user_required` is true, put the options to the user in plain words
     and answer with their choice (`inspect_expression_sources`,
     `set_expression_source`); never guess.
   - `pixel_setup`: the snapshots' ring against the nuclei. Answer
     `estimate_confirmed`, `adjusted` with your `microns_per_pixel`, or
     `user_stated` with the user's value (only that one is written,
     `set_pixel_size`).
   - `panel_context`: as step one.
3. Hand out the packet loop. The start (and `gating_session_status`) carries
   `delegate`: the worker's `tier` and `model` (the user's mapping; else
   `pick` says which of your models), its `tools`, `units_per_worker` and a
   short `brief`. Launch a fresh worker with that `brief` as its whole prompt,
   on that model, given only those tools (`agent` names the installed worker
   where your client has agent files). Never paste skill or guide text into
   it. Keep only the lines it returns; relaunch until the state is `decided`.
   A worker that hands back `waiting_for_user`: ask the user, then pass their
   answer with `gating_session_status` `limits` (each marker `continue` or
   `stop`); never answer for them. Without workers, answer the packets
   yourself (`gating_next`, `gating_answer`, skill gate-packets) and start a
   new conversation every {{ENGINE.markers_per_worker}} markers or so; every
   later call re-reads every earlier packet.
4. Put a question in `ask_user` only for what the data and the panel cannot
   settle (expected prevalence, which partner to trust, a stain known to be
   off-target here). Do not ask the user to approve each marker.
5. `gating_session_finish` (`action: "close"`; `"commit"` for a propose-mode
   session once the user agrees), then `gating_qc` and `gating_report`.
   `export_gates` writes CSV files if wanted.
6. Only if the user explicitly asks to save the gates into their AnnData file:
   `write_gates_to_source` with `confirm: true`.

## Tools

`inspect_project`, `inspect_expression_sources`, `set_expression_source`,
`set_pixel_size`, `get_panel_context`, `set_panel_context`,
`get_marker_hierarchy`, `gating_session_start`, `gating_next`,
`gating_answer`, `gating_session_status`, `gating_session_finish`,
`gating_qc`, `gating_report`, `export_gates`, `get_all_gates`, `reset_gates`,
`undo_operation`, `write_gates_to_source`.

## Evidence

Packets carry their numbers in `evidence` and their pictures as artifacts;
cite the artifact ids a gate rested on (the workers' lines carry them). A
packet's `mirror` says whether the open viewer shows the same thing.

## Uncertainty

- Each marker ends in a state the session names (`gating_session_status`
  `vocabulary`); report the words, never a number for a confidence.
- A unit that stops short ends `manual_review_recommended`, its gate written
  at the best the evidence reached and tagged `needs_review`. Every gated
  marker ends with a value; none is accepted because it ran out. Say which
  carry the tag and what would settle each.
- Confidence comes from a fixed rule over the numbers and the answers.
- Reruns are deterministic: answers are kept under a hash of the packet (per
  `agent`) and reused by a later identical packet (`reuse_answers`; status
  counts them as `replayed`). Pass `reuse_answers` false to be asked afresh.
- `reset_gates` puts a project's gates back at full range (locked ones stay);
  `undo_operation` reverses it.

## Mutation policy

- In `apply` mode each decided gate is written to Plexora's gating state as a
  child receipt of the session; `gating_session_finish` with
  `action: "rollback"` undoes them all.
- A gate is never accepted against the last direction a look gave.
- `technically_failed` and `no_positive_population` markers are written at the
  column's maximum, the reason kept beside the gate.
- Locked and approved gates are never written; hand-set gates are kept unless
  `overwrite_manual: true`; a gate the user changes during the session wins.
- The source file is touched only by `write_gates_to_source` on request.

## Provenance

`gating_report` lists per marker the final gate, the GMM proposal, the tier,
the confidence, the flags and the decisions; `get_all_gates` shows each gate's
method. Report the session id and the number of writes.

## Done when

Every marker has a final state; the user has the report path and a sentence per
marker that is not `accepted`; open `questions` are put to the user.

## Failure modes

- `bulk_running` for long: `gating_session_status` `bulk` says whether the pass
  runs (the next `gating_next` resumes it after a restart).
- `paused`: wait, then go on. `stopped`: `gating_session_finish` with `close`
  (keeps the gates) or `rollback`, and stop; never start another unasked.
- `viewer_not_available` while mirroring: the session continues headless.
