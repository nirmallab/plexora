# Gate an image automatically

Gate every marker of one image, end to end. Plexora does everything that can be
decided by code: display calibration, technical QC, the mixture fit, candidate
thresholds, whole-image checks, the writes and their receipts. You are asked
only what code cannot settle, one small decision packet at a time: whether the
staining is real, which way a gate is wrong, which of a few proposed thresholds
separates the cells. **You never type a threshold.**

The packet is the authority on its own question: what you may answer is its
`allowed` list and its `answer_schema`, and every number you need is in its
`evidence`. How to read its pictures, and every answer schema, come once per
session: the start (and `gating_session_status`) returns a `reading_guide`;
each packet names the entries it relies on in `evidence.guide` (its
compartment and its plotted relation among them) and its schema in
`answer_schema.see`. Keep the guide for the whole session. It is the same for
every session of a build: pass its `guide_version` back as `known_guide` when
you start another, and it is not sent again. This skill says how to judge;
it does not restate what the guide and the packet already say.

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

## QC-passed cells only

When QC has been run on the image -- a session, or regions drawn by hand in a
QC category -- every estimate is made on the cells QC passed: the mixture fit,
the strata and galleries you are shown, the validation fields and every count
in a packet. Cells in a fold, a blurred field, a misregistered patch or a bad
segmentation would otherwise pull the gate toward themselves. The gate that
comes out still applies to every cell; QC removes nothing.

- What is left out is the session option `qc`: `strict` (the default) leaves
  out cells QC called exclude or warn, and for one marker the cells QC flagged
  that marker unreliable in; `exclude` keeps the warn calls; `off` ignores QC.
  Pass `off` only when the user asks, or when you have reason to think QC
  itself is wrong -- and say which.
- The start result's `qc_exclusion` says per image whether QC applied and how
  many cells were left out (`n_left_out`, `fraction`); packets carry the same
  block in `evidence.qc_exclusion`. Report it with the gates.
- A field is never drawn where more than {{qc_exclusion.field_max_percent}}%
  of the cells were left out; left-out cells in a picture are drawn grey and
  never counted as positive. Do not set `artifact_flags` for an artifact QC has
  already removed: it is not in the evidence.
- Above {{qc_exclusion.heavy_percent}}% left out, `qc_exclusion` carries a
  `warning` and confidence is capped: what remains may not represent the
  image. Tell the user.
- No QC on the image (`applied` false, "no QC result"): gating runs on every
  cell. Offer qc-image first when the tissue looks damaged; do not block on it.
- QC changed after a gate was decided (a region drawn, QC re-run): `gating_qc`
  lists the marker under `stale_qc`. Re-gate it, or tell the user.

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
   The start may answer `needs_setup` with an `expression_setup` packet: which
   expression matrix to gate (and whether to apply `log1p`) could not be settled
   from the values. Its `evidence` lists each matrix with what a sample of it
   looks like and a `recommendation`. When `ask_user_required` is true, put the
   options to the user in plain words and answer with their choice -- never
   guess. A log-like matrix is read with `features_log` false; values are never
   transformed twice. The user may answer in the viewer instead; the session
   then goes on by itself. When the choice was certain the start already made
   it (`expression`, receipted and undoable) and says why.
   The context sheet's tissue fields are sized in microns (`field_um`, a
   session option within the preset's bounds), converted with each image's own
   pixel size. An image that states none gets a `pixel_setup` packet first
   (the start's `pixel` says `pending`): an estimate from the segmented cells'
   size, and three snapshots of nuclei and outlines with a scale bar and a
   ring drawn at it. If the nuclei look the size the ring implies, answer
   `estimate_confirmed`; if they clearly do not, `adjusted` with your
   `microns_per_pixel`; if the user told you the pixel size, `user_stated` with
   their value -- only that one is written to the project (`set_pixel_size`,
   undoable). Ask the user (`ask_user`) when the image is unfamiliar and the
   snapshots cannot settle it. The deterministic pass runs meanwhile.
3. `gating_next` with the `session_id`. Its `state` is `decision` (one
   `packet`: a question, compact numbers, a picture or two, the
   `answer_schema`), `bulk_running` (call again), `waiting_for_user` (below), or
   `decided` (finish, below).
   `waiting_for_user`: a marker reached its allowance of looks (or of rounds of
   candidates) while the evidence still says to keep going. Its `requests` name
   it and why. The viewer asks the user in the agent panel; the rest of the
   session goes on meanwhile. With no viewer open, ask the user yourself, then
   pass their answer: `gating_session_status` with `limits`, each marker
   mapped to `continue` or `stop`. Do not answer for them. `continue` grants another
   allowance, `stop` flags the marker for manual review. The session's
   `on_limit` option (`ask`, `extend` for unattended runs, `stop`) and
   `max_extensions` set this; a command line sets their defaults.
4. Answer with `gating_answer` `{session_id, packet_id, answer: {kind, ...}}`,
   `kind` being the packet's `kind`. The result carries the next packet in
   `next`; keep going until `next.state` is `decided`. How to judge each kind:
   - `t1_strip` (the audit sheet): per marker row, {{strip.cells_each_side}}
     cells just below the gate (left) and {{strip.cells_each_side}} just above
     (right). `ok` if the right ones are stained and the left ones are not;
     `suspicious` otherwise. Do not agonise: `suspicious` only sends the marker
     to a proper look.
   - Read `evidence.partners` first: they come first for a reason. A partner
     gated at good confidence is the strongest evidence a look has -- a
     subset marker's positives outside its partner, or a negative control
     above the gate, often settles the direction before the pictures do.
   - Every look carries two pictures: the cells (the collage) and the
     context sheet -- the same marker at three scales: three fields of the
     tissue (borderline, clearly positive, clearly negative), the whole
     image's stain, the whole image's positive cells, and the marker against
     one partner as a flow plot (else its distribution): the partner that
     contradicts the gate most, or the one you asked for; `sheet.plot.why`
     says which. Read coarse to fine: is the pattern across the tissue right,
     do the fields show the architecture the marker should draw (an
     epithelial marker traces glands, a vascular one vessels), then the cells
     at the gate. A marker with an obvious tissue pattern is judged at field
     scale first.
   - The packet's `how_to_read` says what the marker's compartment means for
     its numbers: a membrane or cytoplasmic stain is under-represented by a
     nucleus-based mask, so a clear ring at the outline outranks a borderline
     value.
   - `t2_confirm`: judge the row nearest the gate first. `plausibility`: is the
     stain in the expected compartment (the packet's `context`), and do the
     cells above the gate carry real, cell-shaped staining? `direction`:
     `too_low` means negative cells are called positive (the gate must go up);
     `too_high` means real positives are missed. `about_right` only when every
     row is called correctly. Use the gate-relative panel (its mid-grey IS the
     gate) when the display window misleads. The packet's `partners` are the
     whole-image numbers against partners gated so far, each with the negative
     control it gives (`control`: the marker's high percentile among cells the
     partner says are negative for it). `no_positives` when the stain worked
     but no cell anywhere is really positive: the whole image is checked next.
   - `within_partner` (with `within` naming one of `evidence.within_allowed`)
     when the stain is real inside a subset partner's positives and noise
     outside them -- a marker shared with a non-immune cell type, an
     off-target smear, another lineage's spill. The gate is refitted among
     that partner's positives and shown again: the collage then holds only the
     partner's positive cells and the sheet counts positives among them.
     Answer that conditional look as usual (`about_right` accepts it). The
     gate written is the plain gate at that threshold, with the condition in
     its provenance; its confidence is at most moderate. Use it only when the
     pictures show real staining inside the partner's cells, not to rescue a
     marker that looks wrong everywhere.
   - Ask for what would settle a look instead of guessing, with `request`.
     It opens a reference look only when your answer is undecided
     (`cannot_tell`, `not_binary` or `unsure`); beside a decisive direction it
     only chooses the plotted partner. Still give your best `direction`: `kind: "bivariate"` naming a
     partner the sheet did not plot (an ambiguous lineage marker usually
     has one), or `kind: "reference_channel"` for the partner's channel
     beside the cells. When the partner is gated the next packet is the
     `t3_biological` look beside it, with the plot you asked for.
   - `t3_biological`: the same, beside a reference channel. A subset or
     co-expressed marker's positives should be reference-bright; an exclusive
     one's reference-dark. Set `coexpression_consistent` /
     `exclusion_consistent`.
   - `t4_candidates`: the candidates are points of the marker's lattice --
     every threshold its evidence supports, fixed before any look (the Auto
     gate, the other estimators, each partner's negative control and
     within-partner fit, fixed steps, the band's edges). Row `i1` is the cells
     between the current gate and the first candidate, the next row between
     the first and the second candidate, and so on, nearest the gate first; the sheet's top row shows each candidate in the
     tissue. Judge EVERY row in `intervals` on its own: `mostly_positive`,
     `mostly_negative` or `mixed`. The server places the gate from the rows --
     it moves past each row that says it should and stops at the first that
     does not -- so judge the cells, not the destination. `chosen_candidate`
     is an optional cross-check; if it disagrees with your rows, the rows win
     and the confidence is capped. A first row that is `mixed` sends the marker
     to review. A look never runs past a partner's negative control
     (`ctrl:`) or a within-partner fit (`within:`): that is where the
     flow-cytometry evidence says the gate belongs.
   - `qc_confirm`: the whole image. Its question names what triggered it.
     `real_signal`, `technical_failure` (flat, saturated, background or artifact
     only), `no_positive_population` (the stain worked; no cell here is
     positive) or `cannot_tell`. After `real_signal` the marker gets another
     look.
   - `regression_confirm`: the whole image with positives marked; `holds` unless
     a region is clearly wrong.
   - Confidence is one of `sure`, `fairly_sure`, `unsure` -- a word, not a
     number, so the same judgment always lands in the same state. Say
     `unsure` rather than guess.
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

`inspect_project`, `inspect_expression_sources`, `set_expression_source`,
`set_pixel_size`, `get_panel_context`, `set_panel_context`,
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
- A unit that stops short of a conclusion -- at a limit, or after a look
  that could not settle it -- ends `manual_review_recommended` with a
  `proposed` gate: the best the evidence reached, recorded, not written. A
  marker is never accepted because it ran out. Say so, and what would settle
  it.
- Confidence comes from a fixed rule over the numbers and your answers; saying
  `sure` cannot lift a marker whose populations overlap.
- Artifact flags lower a marker's confidence; they never change its path.
  Only the plausibility fields send a marker to a technical check.
- Reruns are deterministic: the session keeps every answer under a hash of
  the packet it answered (per `agent`), and a later session that issues the
  identical packet reuses it (`reuse_answers`, on by default;
  `gating_session_status` counts them as `replayed`). Pass your model name as
  `agent` so another model's judgments stay separate; pass `reuse_answers`
  false to be asked afresh.
- If you cannot tell, say `cannot_tell`: the marker goes to a reference view or
  to review. A guessed direction costs the user more than an honest one.
- Each marker may take several looks (its `budget`: first look, beside a
  reference, a conditional look, rounds of candidates); technical and
  whole-image checks do not count. Getting the gate right comes first: a look
  at candidates whose every row says to move goes on from where it stopped,
  and a marker at its allowance is asked about (`waiting_for_user`), never
  accepted on it.
- To start over, `reset_gates` puts a project's gates back at their full range
  and forgets their provenance (locked gates stay; open viewers reload). It is
  undoable with `undo_operation`.

## Mutation policy

- In `apply` mode each decided gate is written to Plexora's own gating state
  (what the sidebar shows) as a child receipt of the session, with an undo hint;
  `gating_session_finish` with `action: "rollback"` undoes them all.
- A gate is never written against your last direction: a marker whose looks ran
  out while one said "too low" is proposed, not written.
- A marker that ends `technically_failed` or `no_positive_population` has its
  gate written at the column's maximum (provenance `failed_marker` or
  `no_positive_population`), so every cell reads negative and the reason is
  kept beside it; it is undone like any other write.
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
- `stopped`: the user stopped the session in the viewer. Call
  `gating_session_finish` -- `close` keeps the gates written so far, `rollback`
  undoes them -- and stop; do not start another session unasked.
- `viewer_not_available` or `capability_unavailable` while mirroring: the
  session continues headless; the error says whether to open a tab or restart
  an old viewer.
