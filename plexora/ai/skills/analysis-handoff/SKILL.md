# Hand a question to the analysis application, and show the answer

Plexora shows the image and does the image-facing work: gates, QC, regions,
selections. Cohort statistics and spatial statistics -- neighbourhoods,
enrichment, interactions, comparisons between groups -- belong to an analysis
application on the same dataset (SCIMAP Pro), reached through the shared
protocol (the bridge). This skill is the hand-off: find out where the work
goes, make sure the other side holds the same table, run it there, and show
the result back on the image.

**The rule.** A task that needs pixels, a person's eye or the viewer stays in
Plexora. A task that needs the cell table across samples, a unit of
replication or a statistic goes to the analysis side. When both can, stay
where the user is -- unless the result has to be checked by eye.

## When to use

- `validate_scope` answered with `route_to`: the task belongs to another
  application, and the answer says which and whether it is `reachable`.
- The user asks for neighbourhood composition, cell-cell interactions,
  enrichment, or a comparison across images or groups.
- A result computed elsewhere should be seen on the tissue: colour the cells
  by it, point at them, outline a region.

## When not to use

- Gating, QC, regions or rendering: those are Plexora's own (gate-image,
  qc-image, visual-inspection).
- `route_to` says `reachable` is false: say what is missing (the hint names
  the install or start command) and stop. Never imitate a statistic with
  Plexora's tools.

## Required features

A project with a cell table on this machine (`inspect_project`: the table's
path and roles). The analysis side reads the same file; nothing is copied.

## Decision logic

1. `validate_scope` with the user's words. With `route_to`, read its
   `provider`, `role` and `reachable`.
2. `bridge_route` with the same words lists the candidates with the reason
   for each; `bridge_capabilities` with that role shows what the other side
   offers (its tool names, what each needs and produces).
3. `inspect_project`, then `find_project_for_table` with the project's table
   path: the hand-off works on a table, and this is how the project and the
   table are tied together. A table Plexora has never shown needs
   `bind_project` once, with the image (and mask).
4. `bridge_handoff` to that provider with the capability, its arguments, a
   `context` naming the image (`image_id`) and `returns` listing what to bring
   back. It waits for the other side's job; `bridge_status` re-attaches to one
   by `job_id` if the connection dropped, and `job_wait` follows Plexora's own.
5. Show the result: `viewer_set_color_by` with the column it wrote (the
   result's label), `viewer_highlight_cells` for a handful of cells,
   `set_selection` with `show` for a larger set the user will come back to.
6. Report what the other side reported -- its experimental unit and its
   number of replicates, verbatim -- before any reading of it.

## Tools

`validate_scope`, `bridge_route`, `bridge_capabilities`, `inspect_project`,
`find_project_for_table`, `bind_project`, `bridge_handoff`, `bridge_status`,
`job_wait`, `viewer_set_color_by`, `viewer_highlight_cells`, `set_selection`.

## Evidence

The other side's result record: what ran, on which images, the experimental
unit and replicate count it states, its warnings. A picture of the tissue
coloured by the result shows where, never whether it is significant.

## Uncertainty

- One image is one replicate. A statistic over the cells of one image
  describes that image; it is not evidence about a condition. Say so whenever
  the other side's replicate count is one.
- A column the analysis side wrote is in the table, and the viewer lists it
  once the table's change has been published; if `viewer_set_color_by` says
  the column is not there, the change has not reached Plexora yet -- it is not
  a missing result.

## Mutation policy

The analysis side writes its result into the shared table under its own
names; Plexora writes nothing to the table here. `bind_project` adds a project
to Plexora's registry (the table is read where it lies). A selection is
Plexora's own state, receipted and undoable.

## Provenance

Cite the other side's execution record and Plexora's `operation_id` for
anything Plexora did (a binding, a selection). Name the provider, the
capability and the table, never a token.

## Done when

The result is reported with its unit and replicate count as the other side
stated them, and shown on the image when the user asked to see it -- or the
user knows exactly what is missing to reach the analysis side.

## Failure modes

- `peer_unavailable`: the analysis application is not running or not
  installed; pass on the hint, which names the command.
- `license_required`: the other side refused for licensing; repeat its hint,
  verbatim, and name which product it came from.
- `precondition_missing` from `bind_project`: it needs the image path once.
- `stale_revision`: the table changed since it was read; the hand-off is
  refused rather than run on old data. Re-read and try once.
- `capability_unavailable`: the other side has no such tool, or the bridge
  package is not installed here (the hint says which).
