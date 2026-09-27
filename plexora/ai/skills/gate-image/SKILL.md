# Gate an image automatically

Gate every marker of one image, end to end. Plexora does everything that can be
decided by code: display calibration, technical QC, the mixture fit, candidate
thresholds, whole-image checks, the writes and their receipts. You are asked
only what code cannot settle, one small decision packet at a time: whether the
staining is real, which way a gate is wrong, which of a few proposed thresholds
separates the cells. **You never type a threshold.**

The packet is the authority on its own question: what you may answer is its
`allowed` list and its `answer_schema`, how to read its pictures is its
`evidence.how_to_read`, and every number you need is in its `evidence`. This
skill says how to judge; it does not restate what the packet already says.

## When to use

- "Gate this image", "threshold all markers", "set up gates for this project",
  "call positives for every marker".
- A single marker, when the user wants it done rather than walked through:
  `gating_session_start` with `markers` holding just that marker.

## When not to use

- Several images of one cohort: use gate-dataset (one session, gates carried
  from a reference image).
- Reviewing gates that already exist: review-gating.
- Understanding why one marker is hard: diagnose-marker.
- The user wants to set the number themselves by eye: visual-gating.

## Required features

A cell table with markers and cell-id/x/y roles (`inspect_project` says). An
image channel per marker for the looks (a column without one is gated from its
numbers alone, at capped confidence). A segmentation mask makes the looks much
better (outlines).

## Decision logic

1. `inspect_project`, then `get_panel_context`. If markers are `unresolved`,
   fill ONLY what you know with `set_panel_context` (`source: "ai"`): role,
   compartment, lineage, binary, partners that are markers of this panel. Never
   invent partners; skip what you do not know. The session also asks this as
   its first packet (`panel_context`) when needed.
2. `gating_session_start` with `scope: "project"`, the project, and
   `mode: "apply"` (gates are written as they are decided, each undoable) unless
   the user asked to review first (`mode: "propose"`). Pass `mirror: true` when
   the user has the project open in a viewer and wants to watch; the start
   result's `mirror` says at once whether a tab can be driven, and why not.
3. `gating_next` with the `session_id`. Its `state` is `decision` (one
   `packet`: a question, compact numbers, a picture or two, the
   `answer_schema`), `bulk_running` (call again), or `decided` (finish, below).
4. Answer with `gating_answer` `{session_id, packet_id, answer: {kind, ...}}`,
   `kind` being the packet's `kind`. The result carries the next packet in
   `next`; keep going until `next.state` is `decided`. How to judge each kind:
   - `t1_strip` (the audit sheet): per marker row, {{strip.cells_each_side}}
     cells just below the gate (left) and {{strip.cells_each_side}} just above
     (right). `ok` if the right ones are stained and the left ones are not;
     `suspicious` otherwise. Do not agonise: `suspicious` only sends the marker
     to a proper look.
   - `t2_confirm`: judge the row nearest the gate first. `plausibility`: is the
     stain in the expected compartment (the packet's `context`), and do the
     cells above the gate carry real, cell-shaped staining? `direction`:
     `too_low` means negative cells are called positive (the gate must go up);
     `too_high` means real positives are missed. `about_right` only when every
     row is called correctly. Use the gate-relative panel (its mid-grey IS the
     gate) when the display window misleads. The packet's `partners` are the
     whole-image numbers against partners gated so far.
   - Want the marker beside a partner's channel? Say so with `request`
     (`kind: "reference_channel"`, the partner as `marker`) and still give your
     best `direction`: when the partner is gated the next packet is the
     `t3_biological` look beside it.
   - `t3_biological`: the same, beside a reference channel. A subset or
     co-expressed marker's positives should be reference-bright; an exclusive
     one's reference-dark. Set `coexpression_consistent` /
     `exclusion_consistent`.
   - `t4_candidates`: each row is the cells that change call if the gate moves
     across it. Pick the candidate id after which the remaining positives look
     real; `keep` if the current gate was right after all; `none_separates` if
     no row boundary separates stained from unstained cells. On overlapping
     markers the last candidate may be the edge of what the distribution allows
     (the packet's `guard`); nothing further that way is offered.
   - `qc_confirm`: the whole image. Its question names what triggered it.
     `real_signal`, `technical_failure` (flat, saturated, background or artifact
     only) or `cannot_tell`. After `real_signal` the marker gets another look.
   - `regression_confirm`: the whole image with positives marked; `holds` unless
     a region is clearly wrong.
   - Always set `artifact_flags` when segmentation, focus, saturation,
     bleed-through, autofluorescence, folds or edges affect the cells shown.
5. Put a question in `ask_user` only for what the data and the panel cannot
   settle: whether a marker is binary or continuous, the expected prevalence,
   which partner to trust. Do not ask the user to approve each marker.
6. `gating_session_finish` (`action: "close"`; `"commit"` for a propose-mode
   session once the user agrees), then `gating_qc` for the panel-wide
   consistency check, and `gating_report` for the review report (HTML and PDF).
   `export_gates` writes CSV files if the user wants them.
7. Only if the user explicitly asks to save the gates into their AnnData file:
   `write_gates_to_source` with `confirm: true`.

Context: each packet is self-contained. For a large panel, start a new
conversation every {{ENGINE.markers_per_conversation}} markers or so and
continue with `gating_next` on the same `session_id`; nothing depends on what an
earlier conversation saw. `gating_session_status` shows where a session is.

## Tools

`inspect_project`, `get_panel_context`, `set_panel_context`,
`gating_session_start`, `gating_next`, `gating_answer`, `gating_session_status`,
`gating_session_finish`, `gating_qc`, `gating_report`, `export_gates`,
`get_all_gates`, `undo_operation`, `write_gates_to_source`.

## Evidence

Every packet carries its own numbers (`evidence`) and images (`images`, with
artifact ids). Numbers are never on the images: read values from the JSON, and
pixels and positions from the pictures. Cite the artifact ids of the looks a
gate rested on when you report it. A packet's `mirror` says whether the open
viewer is showing the same thing (`status`, and `last_error` when it is not).

## Uncertainty

- Each marker ends in a state the session names (`gating_session_status`: each
  unit's `state`; its `vocabulary` lists them all). Report the words as the
  session gives them; do not turn a confidence into a number.
- A unit that ends `insufficient_information` with a `proposed` gate reached a
  threshold it could not accept -- often because the last look said the gate
  was wrong and no candidate was chosen. The proposal is recorded, not
  written; say so, and what would settle it.
- Confidence comes from a fixed rule over the numbers and your answers; saying
  `confidence: 0.95` cannot lift a marker whose populations overlap.
- If you cannot tell, say `cannot_tell`: the marker goes to a reference view or
  to review. A guessed direction costs the user more than an honest one.
- Each marker may take a few looks (its `budget`: first look, beside a
  reference, candidates); technical and whole-image checks do not count.

## Mutation policy

- In `apply` mode each decided gate is written to Plexora's own gating state
  (what the sidebar shows) as a child receipt of the session, with an undo hint;
  `gating_session_finish` with `action: "rollback"` undoes them all.
- A gate is never written against your last direction: a marker whose looks ran
  out while one said "too low" is proposed, not written.
- Locked and approved gates are never written; gates the user set by hand are
  kept unless `overwrite_manual: true` (and are then a free reference for their
  partners); a gate the user changes in the viewer during the session wins, and
  that marker goes to review.
- The source file is never touched except by `write_gates_to_source` on the
  user's explicit request.

## Provenance

`gating_report` lists, per marker, the final gate, the GMM proposal, the tier
and confidence, the flags and the decisions; `get_all_gates` shows each gate's
method, status and confidence (and a `proposed_low` that was not written).
Report the session id and the number of writes.

## Done when

Every marker has a final state; the user has the report path and a sentence per
marker that is not `accepted` (why, and what would settle it); open questions
(`gating_session_status` `questions`) are put to the user.

## Failure modes

- `bulk_running` for a long time: the deterministic pass profiles a marker in
  seconds; keep calling `gating_next`. `gating_session_status` `bulk` says
  whether the pass is really running (a restarted server resumes it on the next
  `gating_next`).
- `conflict` on `gating_answer`: that packet is no longer outstanding; call
  `gating_next` for the current one.
- `invalid_input` on an answer: fix it against `answer_schema`; a second
  unreadable answer sends the marker to review.
- A packet's pictures look stale or are missing: `gating_next` with
  `rerender: true` draws the same packet again, at no cost.
- `paused`: the user paused the session in the viewer; wait, then call again.
- `viewer_not_available` or `capability_unavailable` while mirroring: the
  session continues headless; the error says whether to open a tab or restart
  an old viewer.
