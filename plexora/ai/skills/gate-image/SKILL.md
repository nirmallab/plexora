# Gate an image automatically

A gating session gates every marker of an image: Plexora does every step code
can decide and asks the rest as small decision packets. **Nobody types a
threshold**: a reader judges what the pictures show, in the words a packet
allows, never which way a gate should move. You coordinate; skill
gate-packets answers packets. Read each reference named here (gating/...)
with `read_skill`.

## When to use

"Gate this image", "threshold all markers", "call positives". One marker done
for the user: `markers` holding only it.

## When not to use

A cohort: this loop plus gating/dataset. Reviewing gates, or why a marker is
hard: gate-review. A gate set by eye with the user: gating/visual-gating.

## Required features

A cell table with cell-id/x/y roles; image channels and a mask for the looks.

## Decision logic

1. Read gating/setup; `inspect_project`, `get_panel_context`,
   `get_marker_hierarchy`. Give `unresolved` markers (`set_panel_context`)
   only partners you know; never invent one.
2. `gating_session_start`: `scope: "project"`, `mode: "apply"`
   (`"propose"` if the user reviews first), `agent` (your model's name),
   `biology` when the user named the tissue or disease, `mirror: true` when
   they watch a viewer. `engine: "candidates"` only when asked: looks become
   blind numbered cell panels (`cell_panel`) and code picks among candidate
   gates; the loop is the same. Set-up refusals and packets are yours, not a
   worker's (gating/setup); an `expression_setup` goes to the user, never
   guessed.
3. Hand packets out as the start's `delegate` says (`how`). Its `brief` is a
   worker's whole prompt: add no skill or guide text; keep only the lines it
   returns. `waiting_for_user`: ask the user, pass their word as
   `gating_session_status` `limits`; never answer for them. Pass the start's
   `guide_version` as `known_guide` on every `gating_session_status`, or the
   guide is sent again. Without workers, answer packets yourself
   (gate-packets) and start a new conversation
   every {{ENGINE.markers_per_worker}} markers or so: each call re-reads all
   earlier packets.
4. Ask the user (`ask_user`) only what neither numbers nor panel settle:
   expected prevalence, which partner to trust, an off-target stain. Never
   ask approval per marker.
5. `gating_session_finish` (`action: "close"`; `"commit"` for a propose-mode
   session once the user agrees), `gating_qc`, `gating_report`.
6. `write_gates_to_source` (`confirm: true`) only when the user asks for the
   gates in their own file.

## Tools

{{tools}}

## Evidence

Cite the artifact ids each gate rested on.

## Uncertainty

Report states in the session's words (status `vocabulary`), never a number.
A marker that stopped short ends `manual_review_recommended`, tagged
`needs_review`: say what would settle it. QC on the image: gating/qc-cells;
how gates are placed and graded: gating/decisions.

## Mutation policy

Locked, approved and hand-set gates are kept. `rollback`
(`gating_session_finish`) undoes the session, `undo_operation` one write.
Status `unconfirmed_context` (what a reader said about the panel): tell the
user; it is learned once they confirm it (`set_panel_context`,
`source: "user"`).

## Provenance

Session id, number of writes, and per marker gate, method, confidence and
flags (`gating_report`); markers with `stood_on_failed`; `skipped_markers`
(never gated, not failures).

## Done when

Every marker has a final state; the user has the report path, a sentence per
marker not accepted, and the open `questions`.

## Failure modes

`bulk_running` for long: status `bulk` says if it runs. `paused`: wait.
`stopped`: `gating_session_finish` (`close` keeps the gates, `rollback` undoes
them), then stop; never start another unasked.
