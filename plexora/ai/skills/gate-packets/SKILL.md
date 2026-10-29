# Answer a gating session's packets

You answer packets of a session someone else started, for a few markers, then
hand back. You judge what the pictures show, in the words a packet allows:
never a threshold, a value, or which way a gate should move.

## When to use

A brief names a `session_id` and this skill, or gate-image has no workers.

## When not to use

Starting, finishing or reporting a session: gate-image.

## Required features

A session `gating_session_status` finds.

## Decision logic

1. `gating_session_status` (no `known_guide`) sends the `reading_guide` once:
   judge every packet by it. Stop once `progress.units_done` rose by your
   brief's number (default {{ENGINE.markers_per_worker}}); a brief with
   `reader` (plus `tasks` or `parallel`): pass them on every call, count
   packets.
2. `gating_next`; per packet `gating_answer` with `model` (your model). Every
   row, interval or numbered cell gets a word (`cell_panel`: no gate, value or
   direction is shown or asked). Each answer field's description is part
   of the rule.
3. Stop without answering on `decided`, `waiting_for_user`, `paused`,
   `stopped`, `other_tasks`. `bulk_running`, `busy`: `gating_next` again.

## Tools

{{tools}}

## Evidence

Cite the `artifact_id` a judgment rested on.

## Uncertainty

`cannot_tell` or `unsure` beats a guess: a wrong word costs more.

## Mutation policy

Never start, finish, reset or roll back the session, or ask the user.

## Provenance

Your lines are the coordinator's record.

## Done when

Stopped. Reply one line per marker closed meanwhile, from its `outcome`:
`marker | state | gate | confidence | why (artifact ids)`, then
`units_done/units_total | session state | requests`; on `other_tasks`, last
`other_tasks <needs.task>`. Nothing else.

## Failure modes

`conflict`: `gating_next`. `invalid_input`: fix the answer to its schema. An
`as_in` packet or picture you lack: `gating_next` with `rerender: true`.
