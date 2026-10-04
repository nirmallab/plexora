# Answer a QC session's packets

You answer the decision packets of a QC session someone else started, for the
tasks your brief names, then hand back. Plexora scores and traces everything
code can; you judge only what the pictures show.

## When to use

- Your brief names a `session_id`, this skill and your `tasks`: you are a
  worker of a QC session whose user mapped QC tasks to models.

## When not to use

- Starting, finishing or reporting a session: qc-image (the coordinator).
- A session with no models file: the coordinator answers QC itself (qc-image).

## Required features

A session that exists (`qc_session_status` finds it).

## Decision logic

1. Read skill qc-image with `read_skill` for its step four only: how to judge
   each packet kind. Do nothing else that skill says.
2. `qc_next` with the `session_id`, and the `tasks` and `reader` from your
   brief on every call. Answer each packet with `qc_answer`
   `{session_id, packet_id, answer: {kind, ...}, tasks, reader, model}` --
   `model` is the model you run on, as your client names it. Its `next` is
   the next packet.
3. Stop, without answering the packet in hand, when you answered the number
   of packets in your brief (default {{QC_ENGINE.packets_per_worker}}), or the
   state is `decided`, `waiting_for_user`, `paused`, `stopped` or
   `other_tasks` (what is ready is another worker's). `bulk_running` or
   `busy`: call `qc_next` again.

## Tools

`read_skill`, `qc_session_status`, `qc_next`, `qc_answer`.

## Evidence

Numbers are in `evidence`, never on the images; judge the tiles, and cite the
`artifact_id` of the pictures a judgment rested on.

## Uncertainty

Prefer `need_more_evidence` or `cannot_tell` to a guess: an unclear region
goes to manual review, never to an exclusion.

## Mutation policy

Your answers decide the regions and cell calls the session writes, each
receipted and undoable. Do not start, finish, reset or roll back the session,
and do not ask the user anything: hand `waiting_for_user` back.

## Provenance

The session records every answer against its packet, with the `model` you
named; your lines are the coordinator's record.

## Done when

You stopped (step three). Reply with one line per packet you answered:
`packet_id | kind | verdict | why (artifact ids)`, then one line:
`units_done/units_total | session state | requests, if any`. Stopped on
`other_tasks`: add `other_tasks <needs.task>` as the last line. Nothing else.

## Failure modes

- `conflict` on an answer: that packet is no longer outstanding; call
  `qc_next` for the current one.
- `invalid_input`: fix the answer against its schema; a second unreadable
  answer sends the candidate to review.
- A picture missing: `qc_next` with `rerender: true` draws it again.
