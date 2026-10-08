# Answer a gating session's packets

You answer the decision packets of a gating session someone else started, for
a few markers, then hand back. Plexora decides everything code can; you judge
only what the pictures show. **You never type a threshold.**

## When to use

- Your brief names a `session_id` and this skill: you are a worker.
- You coordinate a session yourself and have no workers: follow this for the
  packet loop of gate-image.

## When not to use

- Starting, finishing or reporting a session: gate-image (the coordinator).
- One marker by hand: visual-gating.

## Required features

A session that exists (`gating_session_status` finds it).

## Decision logic

1. `gating_session_status` with the `session_id` and no `known_guide`: it
   returns the `reading_guide` once, for this conversation. Note
   `progress.units_done`: you stop when it has risen by the number in your
   brief (default {{ENGINE.markers_per_worker}}). A brief with `tasks` and
   `reader` scopes you to those tasks: pass both on every `gating_next` and
   `gating_answer`, and count packets answered against its quota instead.
   Pass `model` (the model you run on, as your client names it) on every
   `gating_answer`, scoped or not.
2. `gating_next`, then answer each packet with `gating_answer`
   `{session_id, packet_id, answer: {kind, ...}}`; its `next` is the next
   packet. What you may answer is the packet's `allowed` and
   `answer_schema.see`; `evidence.guide` names the guide entries to read.
   A value `{as_in: packet_id}` is unchanged since that packet (`as_in`).
3. Stop, without answering the packet in hand, when `units_done` reached your
   quota, or the state is `decided`, `waiting_for_user`, `paused`, `stopped`
   or `other_tasks` (what is ready is another worker's). `bulk_running` or
   `busy`: call `gating_next` again.

How to judge (the guide says how to read each picture):

- A look asks ONE question per collage row, about the cells around the
  nucleus the four magenta ticks mark (the cell centred between them; an
  outline may disagree -- the ticks win). Answer each row with one word:
  `own_stain` -- stain touching that nucleus with no other nucleus inside it
  (a ring or halo around it, a fill inside it, cytoplasm hugging it on one or
  more sides; a spindle cell's stain runs along its long nucleus);
  `neighbour_only` -- stain in the tile, but around ANOTHER nucleus, or apart
  from this one by a dark gap; `unstained` -- nothing touches the nucleus;
  `mixed` -- the row holds both kinds about evenly; `cannot_tell`. A row is
  what most of its cells are.
- Judge the shape, not the brightness: the morphology panel (left) brightens
  each cell's own stain on purpose; the context panel (right) is one window
  for the whole marker. Plexora turns your words into the gate; you never say
  which way it should go.
- `t1_strip`: per row, {{strip.cells_each_side}} cells just below the gate
  (left) and as many just above (right). `ok` when the right ones are stained
  and the left ones are not, else `suspicious` (it only earns a proper look).
- `t2_confirm` / `t3_biological`: `rows` (every key of `evidence.rows`),
  `pattern` (what the stained cells look like, in the collage or the sheet's
  fields; `none` only when no cell anywhere carries the stain), `within`
  only when the stain is real inside a subset partner's positives and noise
  outside them (one of `allowed.within`). In a `t3_biological` look the
  third panel of each cell is the reference channel.
- `t4_candidates`: the same word for EVERY row of `intervals`, about the
  cells that change call there. A row of zero cells: `cannot_tell`, and say
  so in `notes`.
- `qc_confirm`: `real_signal`, `technical_failure`, `no_positive_population`
  or `cannot_tell`. After a look that saw no stain it may show the sheet you
  just judged (or none, `as_in`): answer from that look.
- `regression_confirm`: `holds` unless a region is clearly wrong; the failing
  `checks` say what to look at.
- Every look: set `artifact_flags` when segmentation, focus, saturation,
  bleed-through, autofluorescence, folds or edges affect the cells shown.
  `confidence` is `sure`, `fairly_sure` or `unsure`; say `unsure` rather than
  guess, and ask with `request` for the evidence that would settle it.

## Tools

`read_skill`, `gating_session_status`, `gating_next`, `gating_answer`.

## Evidence

Numbers are in `evidence`, never on the images; cite the `artifact_id` of the
pictures a judgment rested on.

## Uncertainty

`cannot_tell` sends a marker to a reference look or to review; a guessed word
costs the user more than an honest one. A marker is never accepted because it
ran out of looks.

## Mutation policy

Your answers decide gates the session writes, each receipted and undoable. Do
not start, finish, reset or roll back the session, and do not ask the user
anything: hand `waiting_for_user` back to the coordinator.

## Provenance

The session records every answer against its packet; your lines are the
coordinator's record.

## Done when

You stopped (step three). Reply with one line per marker that closed while you
worked, from each answer's `outcome`:
`marker | state | gate | confidence | why (artifact ids)`, then one line:
`units_done/units_total | session state | requests, if any`. Stopped on
`other_tasks`: add `other_tasks <needs.task>` as the last line. Nothing else.

## Failure modes

- `conflict` on an answer: that packet is no longer outstanding; call
  `gating_next` for the current one.
- `invalid_input`: fix the answer against its schema; a second unreadable
  answer sends the marker to review.
- An `as_in` naming a packet you do not hold, or a picture missing:
  `gating_next` with `rerender: true` serves the packet again in full.
