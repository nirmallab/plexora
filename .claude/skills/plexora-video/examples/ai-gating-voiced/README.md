# Plexora AI · Auto-Thresholding, voiced (v4, 2026-10-04)

The kit port of `../ai-gating-promo` (v3, silent, 72 s), re-timed to a
voice-over: 103 s, 1080p + 4K, Kokoro `af_heart` at 0.95, 78 s of narration.
It replays the same real session (`gs_20261004T041222_d3e818`), re-read from
its `decisions.jsonl`: the slider starts at Plexora's scored proposal and
moves only where the AI moved it (v3 started CD45 at the GMM 7.50 and
"refined" it, which the record does not support).

- `storyboard.js` -- `doMarker`/`offUnless`/window tweens, gate slides and the
  launcher entrance in `tracks`, agent-card replay as `call` cues.
- `vo_script.json` -- the 14 lines; marker names spelled for the voice
  ("C D forty-five", "SOX ten", "Ki sixty-seven").
- `stubs.mjs` -- gateway placeholders; evidence sheets read-only from `$PLEXORA_LIVE_ROOT`.

Draft at DSF 1: 499 s. Final DSF 3 + 4K: 2642 s.
