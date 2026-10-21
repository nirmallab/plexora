# How a session places, grades and writes gates

For skills gate-image and gate-review: what to know to explain a result or
report it. None of it is asked of a reader.

## Which channels are gated

- **Never a structural channel**: a nuclear counterstain or an
  autofluorescence or blank channel, recognised by name pattern, is listed in
  `skipped_markers` and never gated, not even when named in `markers`. Do not
  report them as failures.
- **Always a continuously expressed marker** (`binary` false: `HLA-ABC`,
  `B2M`, `PD-L1`): gated where expression rises out of background, never
  skipped.
- A marker with no image channel is gated from its numbers alone, at capped
  confidence.

## Where a look starts: scored candidates

Before a marker's first look the session scores candidate thresholds on the
numbers and the biology (see the gate's provenance) and starts from the
proposal, `score`: the Auto mixture gate `gmm`; `onset`, where background
stops explaining the cells (local false-discovery rate below
{{scoring.onset_percent}}%); `ceiling`, the background peak plus
{{scoring.ceiling_sd}} core sds; `bio:<partner>`, where a subset or
co-expressed partner's share has done most of its rise (`robust` false when it
only follows the partner's own gate); `anti:<partner>` for an exclusive one;
`sens:<partner>`, the {{scoring.sens_percent}}th percentile among a subset
marker's positives. The strictest robust partner curve leads. The numbers
choose where to look; the cells decide. `score_gate_candidates` shows the
same scoring for one marker against the partners gated now.

## Reliable references only

Each gate earns a grade as evidence; a failed one is never used, and a low
one only when nothing better exists. A parent whose own distribution is weak
waits for its children (`defer_parents`). A marker that leaned on a reference
which later failed carries `stood_on_failed`: say so in the report.

## The candidates engine

With `engine: "candidates"` the candidate gates are competing hypotheses. Each
look is a `cell_panel`: numbered cells from the disputed intervals, shuffled,
no value, call or gate shown; the reader labels every cell (`own_stain`,
`not_own_stain`, `uncertain`, `artifact`), never a gate or a direction. Code
scores each candidate by the cells it would get wrong, as a probability that
it is the best gate given the reads; the leader is the likeliest candidate
nearest the scored start, and it resolves when the best gate is very likely
within the tolerance of it. Else another panel, aimed at the cells that
could change that, up to `panel_looks`; or review with a stated reason, which
names the gates the reads still leave and how likely each is -- including
when another look could not settle it (`looks_uninformative`). `uncertain`
is never a vote: a marker whose crowded cells cannot be read may resolve on
its isolated cells alone, and says so. The gate written is always one of the candidates and
passes the same whole-image checks; a failed check caps its confidence, it
never moves the gate. A conditional gate (a classic look's `within` answer)
or a marker with no candidates gets the classic looks, whose reading comes
with the packet.

## How a marker ends, and what is written

- In `apply` mode each decided gate is written to Plexora's gating state as a
  child receipt of the session; `gating_session_finish` with
  `action: "rollback"` undoes them all. `reset_gates` puts a project's gates
  back at full range (locked ones stay); `undo_operation` reverses it.
- Every gated marker ends with a value. A unit that stops short ends
  `manual_review_recommended`, written at the best the evidence reached and
  tagged `needs_review`; none is accepted because it ran out of looks.
- A gate is never accepted against the last direction a look gave.
- `technically_failed` and `no_positive_population` markers are written at
  the column's maximum, the reason kept beside the gate.
- Locked and approved gates are never written; hand-set gates are kept unless
  `overwrite_manual: true`; a gate the user changes during the session wins.
- Confidence comes from a fixed rule over the numbers and the answers.
- A classifier layer beside a gate decides only once the user adopts it
  (gate-review).
- `gating_report` lists per marker the final gate, the Auto proposal, the
  tier, the confidence, the flags and the decisions; `get_all_gates` shows
  each gate's method; `export_gates` writes CSV files. Status `models` shows
  which model answered each task when the user's models file splits the work.
- A bulk pass a restart stopped resumes at the next `gating_next`
  (`gating_session_status` `bulk` says whether it runs).
- A packet's `mirror` says whether the open viewer shows the same thing;
  `viewer_not_available` while mirroring: the session goes on headless.
