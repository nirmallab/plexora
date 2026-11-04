# Set a gating session up

For skill gate-image, and gating/dataset: what to settle before and at
`gating_session_start`. Set-up is the coordinator's; a worker never gets it.

## The panel context

`get_panel_context` gives each marker's role, compartment, lineage and
partners, and lists as `unresolved` the markers the vocabulary does not know.
Partners are evidence: where a look starts leans on them (gating/decisions).
The vocabulary is the marker graph SCIMAP Pro reads too, so an unresolved
marker is filled there first, with evidence (skill `biocognia-marker-graph`):
`marker_graph` names what it lacks; research it, write it with
`propose_marker_edges` and check its quotes with `verify_marker_evidence`;
then read `get_panel_context` again. What no evidence places, give this
session with `set_panel_context` (`source: "ai"`): the lineage-specific
partners you know (`HLA-DR` for a dendritic or macrophage marker, `CD163` for
`CD68`, `SOX10` for a melanocytic marker) and the exclusive ones. A wrong
partner misleads the start, so skip what you do not know; never invent one.
`source: "user"` is for what the user said.

What a reader answers in a `panel_context` packet is used by this session
and listed in the status as `unconfirmed_context`; the vocabulary learns it
only once the user confirms it (`set_panel_context`, `source: "user"`).

`partner_review` packets go through every marker, each batch twice. Fill
each marker's `empty` partner slots from the `panel`, reading its
`expression` note against their lineages: `subset` for one lineage,
`subset_any` for any of several (one partner per lineage, every one the note
names that the panel marks), `coexpressed` only when most cells of both are
positive, `exclusive` only for a lineage the note does not name, each with a
`basis`. Then list in `checks` the stated relations that hold less in this
`sample`, and add what its tissue or disease calls for. Say `high` only when
sure; a change to a stated relation also needs a `basis` naming the sample's
tissue or disease. With none named, give the one the panel tells you in
`inferred_context`, if it does. A change is made only when both answers give
it that way; code drops what breaks the rules; otherwise the vocabulary's
partners stand, and an empty slot's partner draws only the look's tile.
For a `state` marker (it lists `carriers_stated`), also give `carriers`: the
panel's lineage markers whose cells carry it, as its `expression` note names
them, most cells first. Those both answers name join the stated ones; of
those gated in the run whose cells carry the marker more often than the
average cell, the two holding the most of its positives set the marker's
start beside the whole population and supply its looks' carrier negatives
and clear positives. Name a lineage only where the marker is common in it:
one where it is rare is dropped on the numbers anyway.
Status `partner_review` lists what was changed and the context used.

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
  - `partner_review`: as above; nothing to change is an empty answer.
  - `display_setup`: how bright each marker is drawn, once per image (the
    choice outlives the session): per row, the columns where the background
    reads as a faint floor (`background_ok`) and where the row's `pattern`
    shows with detail (`structure_ok`); code picks. Engine `v2` leaves it
    out unless `display_check: "vision"` is passed; with `v1`, `"rule"` at
    the start skips it and `"off"` drops it; for brightness alone,
    `contrast_session_start`.

## Options worth knowing

- `qc`: which QC calls leave cells out (gating/qc-cells).
- `basic_qc`: on with `v2`. An image with no QC result gets QC's model-free
  checks before it is gated (`run_basic_qc`, no model calls); the start
  output's `basic_qc` note says so. Pass it on to the user, with the advice
  to run the full QC from the QC panel to review what it left out.
- `reuse_answers`: answers are kept under a hash of each packet (per `agent`)
  and reused by an identical later packet (status `replayed`). Pass false to
  be asked afresh.
- `engine`: `v2` (the default) decides a marker by blind cell panels choosing
  among candidate gates (gating/decisions); `v1` by rows around a gate, only
  when the user asks. `classic` and `candidates` are their first names.

## Tools

{{tools}}
