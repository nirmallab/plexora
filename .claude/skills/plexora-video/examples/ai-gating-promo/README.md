# AI Auto-Thresholding promo (lsp11385, 2026-10-04)

The 72 s promo this skill was distilled from, kept as it shipped (v3). It
predates the kit: `choreo.js` is a single-purpose storyboard plus runtime in
one file, `overlay.css` its styles, `record.mjs` its recorder. Read it for
worked answers to things the kit leaves to the storyboard:

- replaying a real MCP gating session in the agent card (`plexora:agent-state-changed`
  events: started, issued, answered, unit_closed, finished);
- gate slider moves (`SLIDE`, `setGateRange` with `GATING_BRUSH_MOVE` then `SELECTION_CHANGED`);
- per-marker looks (`LOOK`, `WIDE`), `doMarker`, `offUnless`;
- the launcher flow with a real click on `#plexora_ai_button`;
- the HUD with the real session clock, the title card with counting stats.

Numbers in it (thresholds, windows, poses) belong to lsp11385 and that session.
New videos use `../../kit/`.
