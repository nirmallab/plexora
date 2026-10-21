# Benchmark campaigns

One folder per campaign of live gating sessions, written by
`tools/bench_gates/run_arms.py` (decision gate 1 in `../CANDIDATES_PLAN.md`).
The folder is tracked: a campaign's records are the evidence a report's numbers
come from, so `tools/bench_gates/score_gap.py` and
`tools/bench_gates/calibrate.py` can be run on them again by anyone.

    <campaign>/
        manifest.json               the plan (arms and their session options,
                                    images, runs, seeds, markers), the commit,
                                    start and end, and per session its status,
                                    seed, reader models, cost and record
        <arm>/<image>__r<run>.json  the session record the engine stored
        incomplete/...              records of sessions that did not finish
        score_gap.json              the scores, when the driver was given --truth

## Every file under 1 MB

A session record over 1,000,000 bytes is stored trimmed: encoded payloads go
first, then each unit keeps only what the scorers and the calibration read
(project, marker, state, final, high, start, lattice, hypotheses with their
observations and labels, packets, and a few fields saying how the marker was
decided). A trimmed record carries `_trimmed` (what was dropped, from what
size), and the manifest lists it under `records.trimmed`. One still over the
limit is flagged `over_limit` in the manifest: do not commit it; say in the
report where it is kept instead.

## What does not go here

Pictures, packets, crops, proxy captures, transcripts and trace databases:
they stay in the bench's data root, or under `../runs/`, which is ignored. So
do labelled cell sets (`../runs/2026-10-09-labels` and the like), which are
kept out of git on purpose. The report that reads a campaign goes beside
`../CANDIDATES_PLAN.md` and names the campaign folder.
