# Plexora AI · Automatic QC promo (2026-10-04)

The second video in the series and the first with a voice-over: 106 s,
1080p + 4K, Kokoro `af_heart` at 0.95, synthesised interface sounds.
It replays a real delegated QC session on lsp11385 (`qs_20261004T184838_c6518e`).

- `harvest.py` -- reads the copied session record (`session/`) and the live
  root's job file into `story.json` (every number on screen). Run with the
  repo on `PYTHONPATH`.
- `storyboard.js` -- the timeline: agent-card replay (`emit`/`issued`/`closed`),
  per-frame region fades and hover holds in `tracks`, the piecewise HUD clock
  (`clock.segments`), and the `voice` / `sfx` cues the soundtrack reads.
- `vo_script.json` -- the 13 narration lines.
- `stubs.mjs` -- AI gateway placeholders (2,480 credits, run `air_demo`) and the
  evidence sheets served read-only from `$PLEXORA_LIVE_ROOT`.

Copy the kit into a work dir first (SKILL.md section 2), then these files
over it. The user asked for this one to be slower than the gating promo.
