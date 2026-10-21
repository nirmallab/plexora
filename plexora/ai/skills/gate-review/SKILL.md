# Review gates, and diagnose a hard marker

Go over a project's gates with the user: which were set how, which are
uncertain, which contradict each other, and settle them on the user's word --
approve, lock, exclude, re-gate or undo. For one marker that is hard to gate
or looks wrong, find out why -- overlapping populations, a rare population, a
failed stain, saturation, a staining gradient, segmentation, bleed-through --
from the numbers first and the pixels second, and say what would make it
gateable. Read a reference named here (gating/...) with `read_skill`.

## When to use

- "Review the gates", "which gates should I check?", "approve these", "lock
  `CD3`", "undo what the agent did to `CD8`"; after a gating session, to go
  through what it could not settle.
- A session ended a marker in `manual_review_recommended`,
  `technically_failed`, `not_binary` or `accepted_low_confidence` and the user
  asks why; "why is `CD4` so hard?", "is this stain working?", "is this
  bleed-through?".

## When not to use

- Nothing is gated yet, or routine gating: gate-image (it diagnoses as it goes
  and only asks about what is hard).
- Setting one gate by eye with the user: gating/visual-gating.

## Required features

A project with gates (`get_all_gates`); provenance exists for gates an agent
set (older gates show as manual). To diagnose: the marker's column, and its
image channel and a mask for the pictures.

## Decision logic

Review:

1. `get_all_gates`: each gate's value and `provenance` (method, status,
   confidence, state, `edited_since` -- the user changed it after the agent).
2. `gating_qc`: every gated marker's positive fraction, every partner pair's
   contradiction score, and `needs_review`. Start with `needs_review`,
   highest contradiction first. `stale_dependencies` lists gates decided
   against a partner gate that has since changed (a `CD3` gate moved after
   `CD4` was gated beside it): re-gate those, the partner first. `stale_qc`
   lists gates estimated before QC changed: re-gate them (gating/qc-cells).
   `get_marker_hierarchy` (with the session's `session_id`) shows which gates
   failed as evidence and which markers leaned on them: review a marker with
   `stood_on_failed` after the reference it rests on.
3. For a gate the user questions: `render_gating_collage` (`layout: "t2"`) at
   the stored gate, and `bivariate_evidence` against its partner. Where its
   looks started is in the session report (the scored proposal or the Auto
   gate); `score_gate_candidates` shows the candidates it was chosen among,
   against the partners gated now. Gates with method `needs_review` were
   written where the evidence stopped short: review those first. Present
   what you see; the user decides.
4. Act only on the user's decision:
   - approve (`set_gate_status` `approved`) -- agents will not overwrite it;
   - lock (`locked`) -- nobody but the user changes it, and the viewer reverts
     accidental drags;
   - exclude (`excluded`) -- automatic runs skip it;
   - undo an agent write -- `undo_operation` with its `operation_id`
     (`get_gate_provenance` lists them).
5. The classifier's layer, where a session left one (`get_gate`
   `session_run`; `gating_qc` `stale_classifier` when the gate moved since):
   `classifier_state` for its counts, `classifier_review` for the uncertain
   cells it would like a person to call, and `render_cell_gallery` on those
   ids, or on cells it took away from the gate, to show them. The user labels
   cells in the panel and adopts or discards the layer there; `classifier_adopt`
   / `classifier_revert` only on the user's word. Never label cells for them
   (`classifier_annotate`): an agent's word about a cell is not a person's.
6. `gating_report` for a session's review document if the user wants one.

Diagnose one marker:

7. `profile_marker`: the class, the first-tier reasons, the estimators (where
   they agree and disagree), separation (`ashman_d`, `valley_ratio`,
   `overlap_mass`), stability (`boot`), and the flags from the cell table
   (`cell_qc`: size, nuclear bleed, edge, illumination) and the image overview
   (`image_qc`: saturation, empty channel, gradient).
8. `score_gate_candidates`: where the onset, the noise ceiling and each gated
   partner's curve put the gate, and how each candidate trades purity against
   the positives it keeps (`components`). A partner curve far from the Auto
   gate says the mixture split something other than background from signal
   (another lineage's spill, a dim tail); `robust` false says a partner only
   follows its own gate; `unused_partners` names partners the vocabulary knows
   but nobody has gated yet. A marker the panel calls continuous is diagnosed
   for where its expression starts, not for a valley it will never have.
   `get_marker_hierarchy` with the marker says where it sits in the panel's
   tree and which references its evidence would lean on or avoid; pass the
   sample's `tissue` and `disease` when the user named them, so what the
   marker also marks there (`biology_for_marker`) frames the pictures.
   `get_panel_context` gives its role, compartment and partners.
9. From the flags and the candidates, the one question worth a picture:
   - separation / class -> `render_gating_collage` `layout: "strata"` (every
     band, plus spatially inconsistent cells);
   - a partner relation -> `bivariate_evidence` (a density plot comes back only
     when the contradiction is anomalous), then `layout: "quadrants"`;
   - gradient / edge / saturation -> `layout: "overview"`;
   - one strange cell -> `explain_cell`; its neighbourhood -> `render_region`.
   `sample_gating_cells` gives cell ids for `render_cell_gallery` when a band
   needs a closer look.
10. Say plainly what is wrong, which evidence shows it, and the remedy:
    re-stain, re-segment, a user-set gate, start from a scored candidate,
    exclude from phenotyping, or a gate conditional on a subset partner (in a
    gating session, a look's `within` answer) when the stain is real only
    inside that partner's cells. A membrane or cytoplasmic marker that
    follows the nucleus is the nucleus-based mask, not bleed-through: the
    profile records the DNA correlation (`cell_qc` `nuclear`) without the
    `nuclear_bleed` flag.

## Tools

{{tools}}

## Evidence

Contradiction scores and quadrant counts from `gating_qc`; the profile's
numbers and flags; each picture's artifact id and what it shows.

## Uncertainty

- A contradiction can be biology (a `CD4`-positive, `CD3`-negative macrophage)
  rather than a wrong gate; the orphan's `adjacent_orphan_share` says whether
  it is spill from a neighbour. Say which, and do not re-gate to remove
  biology.
- A gate with method `ai_conditional` was fitted among a partner's positives
  only ("positive only within `CD45`-positive cells"): it is the plain gate at
  that threshold, so on its own it also calls some partner-negative cells, and
  its provenance `condition` says how many. Say both when you report it; a
  phenotype built on it should also require the partner.
- Overlapping populations have no clean threshold; say the error either side
  of a gate rather than pretending there is none. A continuous marker still
  has an onset above background: that is its gate.
- A nuclear counterstain or an autofluorescence channel is not a marker to
  diagnose: automatic gating never gates one.
- Autofluorescent cells are bright in every channel: check two unrelated
  markers before calling positives real.
- The profile and pictures leave out QC failures where QC was run
  (gating/qc-cells says how to tell an artifact QC caught from the marker's
  own problem).
- Confidence words (`high`, `moderate`, `low`, ...) come from the session's
  rule table (`gating_session_status` `vocabulary` lists them); report them as
  given.

## Mutation policy

- `set_gate_status` changes only the gate's status (reversible; the receipt's
  undo hint restores the previous status).
- `undo_operation` refuses when the gates changed since; never force it.
- Never change a value the user approved or locked.
- A classifier layer is adopted, reverted or discarded only on the user's word.
- Diagnosing is read-only: a gate change follows from the user's decision,
  through gate-image or gating/visual-gating.

## Provenance

Cite each gate's method and session, the operation ids of anything undone or
re-statused, and for a diagnosis the profile's key numbers and the artifact
ids behind it.

## Done when

Every gate the user wanted reviewed has a decision (approved, locked,
excluded, re-gated or left) and the user has the list; a diagnosed marker has
its cause, the evidence and a remedy.

## Failure modes

- `conflict` on `undo_operation`: later writes depend on it; undo those first
  or re-gate instead.
- A gate with no provenance: it was set by hand or before provenance existed;
  treat it as the user's.
- `precondition_missing` for a column with no image channel: diagnose from the
  numbers alone and say the pixels could not be checked.
