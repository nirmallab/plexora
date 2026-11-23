# visual-1: the visual artifact pass bench

`plexora ai visual-bench` (`plexora/ai/visual_bench.py`), the gateway bar
`qc.visual_scan` (biocognia-platform `workers/ai/src/ai/catalog.ts`).

## What it runs
The visual pass is the coordinator's own: while its packet is out the model
looks at the whole tissue, zooms, picks channels and writes outlines with
Plexora's tools (`render_artifact_overview`, `inspect_artifact_channels`,
`segment_qc_roi` with the session's id). The bench runs it over Plexora's MCP
server on five synthetic scenes (`bench_qc.register`): two folds, two places
of lost tissue (`dark_region`), and one clean scene. Each session has only the
visual pass on. The scan's detector candidates still come up in the channel
audit, so the score counts only the outlines the visual pass wrote (the visual
unit's `written`), on the map grid as the QC bench scores regions: recall at
IoU >= 0.3 with an accepted class, precision as outlines lying mostly on a
painted artifact.

Runs: `server.sh <root> <port>` (an MCP server on the branch's code, in an
isolated root holding the licence and magic select's `.models`), then
`vis_run.py <root> <port> <model>` (a headless `claude -p` coordinator per
scene, effort high, only the visual-pass tools), then
`plexora ai visual-bench --score <root>/data --sessions <root>/sessions.json`.

## Runs (2026-11-23)

| reader | recall | precision | mean IoU | outlines |
|---|---|---|---|---|
| truth reader (box round each region; the ceiling) | 1.0 | 1.0 | 0.76 | 4 |
| Sonnet 5.5 | 1.0 | 1.0 | 0.63 | 5 |
| Opus 5.5 | 1.0 | 0.8 | 0.63 | 5 |

Neither model outlined anything on the clean scene. Both drew the first fold
twice (redundant, not wrong). Opus's fold_b outline matched the fold but lies
mostly on intact tissue. Transcripts in `runs/<model>/agent_<scene>.txt`.

## Bar (proposed, calibrated under the ceiling)
region_recall >= 0.75 and region_precision >= 0.75, every scene finished; IoU
reported. One miss or one loose outline in five passes.
