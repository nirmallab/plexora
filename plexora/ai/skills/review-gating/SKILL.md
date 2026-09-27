# Review gates

Go over a project's gates with the user: which were set how, which are
uncertain, which contradict each other, and settle them -- approve, lock,
exclude, re-gate or undo.

## When to use

- "Review the gates", "which gates should I check?", "approve these", "lock
  `CD3`", "undo what the agent did to `CD8`".
- After a gating session, to go through what it could not settle.

## When not to use

- Nothing is gated yet: gate-image.
- One marker needs a deep look at why it is hard: diagnose-marker.

## Required features

A project with gates (`get_all_gates`); provenance exists for gates an agent
set (older gates show as manual).

## Decision logic

1. `get_all_gates`: each gate's value and `provenance` (method, status,
   confidence, state, `edited_since` -- the user changed it after the agent).
2. `gating_qc`: every gated marker's positive fraction, every partner pair's
   contradiction score, and `needs_review`. Start with `needs_review`,
   highest contradiction first.
3. For a gate the user questions: `render_gating_collage` (`layout: "t2"`) at
   the stored gate, and `bivariate_evidence` against its partner. Present what
   you see; the user decides.
4. Act only on the user's decision:
   - approve (`set_gate_status` `approved`) -- agents will not overwrite it;
   - lock (`locked`) -- nobody but the user changes it, and the viewer reverts
     accidental drags;
   - exclude (`excluded`) -- automatic runs skip it;
   - undo an agent write -- `undo_operation` with its `operation_id`
     (`get_gate_provenance` lists them).
5. `gating_report` for a session's review document if the user wants one.

## Tools

`get_all_gates`, `gating_qc`, `get_gate_provenance`, `render_gating_collage`,
`bivariate_evidence`, `set_gate_status`, `undo_operation`, `gating_report`.

## Evidence

Contradiction scores and quadrant counts from `gating_qc`; the collage and its
artifact id for any gate discussed.

## Uncertainty

- A contradiction can be biology (a `CD4`-positive, `CD3`-negative macrophage)
  rather than a wrong
  gate; the orphan's `adjacent_orphan_share` says whether it is spill from a
  neighbour. Say which, and do not re-gate to remove biology.
- Confidence words (`high`, `moderate`, `low`, ...) come from the session's rule
  table (`gating_session_status` `vocabulary` lists them); report them as given.

## Mutation policy

- `set_gate_status` changes only the gate's status (reversible; the receipt's
  undo hint restores the previous status).
- `undo_operation` refuses when the gates changed since; never force it.
- Never change a value the user approved or locked.

## Provenance

Cite each gate's method and session, and the operation ids of anything undone
or re-statused.

## Done when

Every gate the user wanted reviewed has a decision (approved, locked, excluded,
re-gated or left) and the user has the list.

## Failure modes

- `conflict` on `undo_operation`: later writes depend on it; undo those first
  or re-gate instead.
- A gate with no provenance: it was set by hand or before provenance existed;
  treat it as the user's.
