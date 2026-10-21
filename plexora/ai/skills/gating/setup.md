# Set a gating session up

For skill gate-image, and gating/dataset: what to settle before and at
`gating_session_start`. Set-up is the coordinator's; a worker never gets it.

## The panel context

`get_panel_context` gives each marker's role, compartment, lineage and
partners, and lists as `unresolved` the markers the vocabulary does not know.
Partners are evidence: where a look starts leans on them (gating/decisions).
Give each unresolved marker, with `set_panel_context` (`source: "ai"`), the
lineage-specific partners you know (`HLA-DR` for a dendritic or macrophage
marker, `CD163` for `CD68`, `SOX10` for a melanocytic marker) and the
exclusive ones. A wrong partner misleads the start, so skip what you do not
know; never invent one. `source: "user"` is for what the user said.

What a reader answers in a `panel_context` packet is used by this session
and listed in the status as `unconfirmed_context`; the vocabulary learns it
only once the user confirms it (`set_panel_context`, `source: "user"`).

## The sample's biology

When the user names the sample's biology, pass it to `gating_session_start`
as `biology` (`tissue`, `disease`, `notes`, in their words). Its relations are
laid over the panel (status `biology`). It is a prior: it never moves a gate
and never makes a population exist. Without it the session takes it from
metadata or infers it from the panel (`source`): say an inferred context is
inferred, ask the user to confirm it, never report it as the diagnosis.

## The marker tree

`get_marker_hierarchy` shows each marker's `stage`, `parents` and `children`.
Gating follows it, broad markers first; it guides and never blocks. Describe
the markers it cannot place in the panel context.

## Refusals and set-up packets

- A start refused with `detail.setup` names a column the project lacks (a
  cell id, the coordinates, an image id): tell the user which, and that the
  viewer's set-up form (opening Thresholding) or Edit project sets it. Start
  again once they have, or start with `setup: "wait"` when they are about to
  answer the form in the viewer: the session then waits in `needs_setup`,
  with no packet, until they have.
- `unconfirmed_setup` in the start lists columns the predictor picked and
  nobody confirmed: tell the user.
- Set-up packets come before any marker's, and are yours to answer:
  - `expression_setup` (`needs_setup`): which matrix to gate, and whether its
    values are logged as they are read. Put the options to the user in plain
    words and answer with their choice (`inspect_expression_sources`,
    `set_expression_source`); never guess.
  - `pixel_setup`: the snapshots' ring against the nuclei. Answer
    `estimate_confirmed`, `adjusted` with your `microns_per_pixel`, or
    `user_stated` with the user's value; only that one is written to the
    project (as `set_pixel_size` would).
  - `panel_context`: as above, only what you know.
  - `display_setup`: how bright each marker is drawn, once per image (the
    choice outlives the session): per row, the columns where the background
    reads as a faint floor (`background_ok`) and where the row's `pattern`
    shows with detail (`structure_ok`); code picks. `display_check: "rule"`
    at the start skips it, `"off"` drops it; for brightness alone,
    `contrast_session_start`.

## Options worth knowing

- `qc`: which QC calls leave cells out (gating/qc-cells).
- `reuse_answers`: answers are kept under a hash of each packet (per `agent`)
  and reused by an identical later packet (status `replayed`). Pass false to
  be asked afresh.
- `engine: "candidates"`: blind cell panels instead of rows around a gate
  (gating/decisions); experimental, only when the user asks.

## Tools

{{tools}}
