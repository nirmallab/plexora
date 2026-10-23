# Gating on the cells QC passed

For skills gate-image and gate-review, gating/dataset and gating/visual-gating.

When QC has been run on the image, every estimate is made on the cells QC
passed, and those cells are not called: a cell in a QC region (or flagged
unreliable for a marker) is neither positive nor negative for it -- grey in
the viewer, left out of counts, and written as `obsm` beside the gates
(`write_gates_to_source`). The gate's value is unchanged.

- The session option `qc` is `strict` (the default: cells QC called exclude
  or warn, and per marker the cells QC flagged that marker unreliable in),
  `exclude` (warn calls kept) or `off` -- only when the user asks, or QC is
  wrong; say which.
- The start's `qc_exclusion` says per image how many cells were left out
  (`n_not_called` per marker) and, per marker, QC's own line about it
  (`digest`) and its word on the channel (`channel_verdict`: `usable`,
  `failed`, `unknown`). Report them with the gates.
- Above {{qc_exclusion.heavy_percent}}% left out it carries a `warning` and
  confidence is capped: tell the user.
- No QC on the image: offer skill qc-image first when the tissue looks
  damaged; do not block on it.
- QC changed after a gate was decided (QC run for the first time, a region
  drawn or moved, strictness changed): `gating_qc` lists the marker under
  `stale_qc`. It was fitted on cells QC now fails, or without cells it now
  passes: re-gate it.
- A dataset session has a `qc_exclusion` block per image: report each image's
  left-out count with its gates, and name an image QC was never run on -- its
  gates stand on every cell, the others' do not.
- The one-marker tools (the profile, collages, galleries, fields) draw from
  the QC-passed cells too (`qc_exclusion` in each result); a field's left-out
  cells are drawn grey and counted apart (`qc_left_out`). Pass `qc: "off"`
  only when the user asks.
- Diagnosing a marker: a problem seen with `qc: "off"` that vanishes once QC
  failures are set aside was an artifact QC already caught; one that shows
  only with them set aside is the remaining tissue's. Compare both before
  blaming the marker.
