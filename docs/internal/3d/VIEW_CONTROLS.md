# Depth controls: which control is offered in which mode

The depth strip (`views/depthStrip.js`), its cog popover (`views/depthSettings.js`) and any
plugin control that only means something in some modes ask one registry,
`services/viewControls.js` (`window.PlexoraViewControls`), what to show. Nothing decides
visibility by hand: a control that does nothing in a mode is **hidden** there, never left on
screen disabled.

Before the registry (2026-10-12), around 40 mode checks across six files each decided for
themselves, and several were wrong. In 3-D the plane rail, its hover mark, `]`/`[`,
Shift+wheel and Play all changed a plane that 3-D freezes. The clip-range band was drawn
there but could not be dragged. The readout menu still offered "One plane" when the mode
buttons already did.

## The entry

```js
PlexoraViewControls.register({
    id: "rotate.speed",            // unique; a plugin's begin "<plugin>."
    surfaces: ["popover"],         // "strip", "popover", or a plugin's own surface name
    modes: ["volume"],             // "plane" | "slab" | "volume", or "*"
    needs: ["webgl2", "prefs"],    // capabilities: every one must hold
    when: [],                      // state flags that must hold
    excludes: [],                  // state flags, or entry ids, that hide it
    depends: [],                   // entry ids that must be visible too
    config: (ctx) => ({ ... }),    // what it does in this context
    group: "rotate", order: 10,    // where it sits
    doc: "How fast Play turns the stack.",
});
```

- **Capabilities (`CAPS`)** describe the image and the page:
  - `depth` (two or more planes), `frames`, `depthOrFrames`;
  - `colourless` (not an RGB stack), `webgl2` (the 3-D view can be drawn), `sections` (the
    panes exist), `zStep`, `labels3d`;
  - `surfaces`, `surfacesPresent`, `surfaceStudio`, `prefs`, `card`, `grid`;
  - `cut` (the 3-D view can be cut: services/inspectState.js is on the page), `volumeDrawn`
    (the voxels are drawn, not "surfaces only").
- **State flags (`FLAGS`)** describe what the user chose:
  - `mode.plane`, `mode.slab`, `mode.volume`;
  - `following`, `fixed` (a projection that follows the plane, or does not);
  - `rotating`, `sectionsOn`, `allPlanes`, `percentile`, `gridOn`;
  - the 3-D view's cut (docs/internal/3d/SLICE_CLIP.md), true only in 3-D:
    - `cut.full`, `cut.on`, `cut.cutaway`, `cut.section`, `cut.slab`, `cut.crop`;
    - `cut.planes` (section or slab), `cut.box` (crop or cutaway), `cut.combines` (a slab
      that is not a single plane);
    - `axis.x`, `axis.y`, `axis.z`;
    - `cut.noPlane`, `cut.noBand`: nothing on the rail to pick, or no range on it.
- **Typos fail at load.** An unknown capability, flag, mode or duplicate id throws at
  `register`.
- **No fixed point.** `excludes` and `depends` may name flags, or entries that have no
  `excludes` or `depends` of their own. Resolving is two passes, and `register` enforces
  this rule.
- **`config(ctx)`** says what a control *does* in a mode, beside whether it shows. Play's
  `kind` is `planes`, `band` (slide a fixed projection's range), `frames` or `rotate`. The
  rail's `{plane, band, bandMove}` says whether a press picks a plane, whether the band
  ends resize and whether the band moves.

## Resolving

- `context(viewState, extras)` builds the context once per paint. `extras` is what the page
  has; main.js's `depthControlExtras()` is the one source shared by the strip, the popover
  and plugins, so they cannot disagree.
- `resolve(ctx, {surface})` returns `[{id, group, config}]` in order.
- `visible(id, ctx)` and `configOf(id, ctx)` answer for a single entry.
- `transition(ctx, mode)` is the view-state patch for a mode button. Entering 3-D from a
  projection that follows the plane opens the whole stack.
- Resolving never calls `viewState.set`, and `tests/js/view_controls_probe.mjs` spies to
  check this. A mode change only repaints. A plane stepper whose kind no longer applies
  stops with `persist: false`, because nobody chose the plane it stopped on.

The strip repaints on view-state changes, `plexora:viewer-prefs-changed`,
`plexora:view-controls-changed` (a control registered or removed), `plexora:volume-rotate`,
`plexora:volume-mounted` and `plexora:surface-loaded`.

## The matrix

P plane, S fixed projection, F projection following the plane, V 3-D, T a time series.
Pinned by `tests/js/view_controls_probe.mjs` and `tests/js/depth_strip_probe.mjs`.

| Control | P | S | F | V | Needs |
|---|---|---|---|---|---|
| Mode buttons plane / projection / 3-D | ✓ | ✓ | ✓ | ✓ | depth, not colour; 3-D also WebGL2 |
| Sections | ✓ | ✓ | ✓ | – | the panes exist, not colour |
| Play | planes | slides the range | planes | **turns the stack** | depth or frames (single plane: frames) |
| Rail | picks a plane | range moves/resizes | range resizes | **the cut along Z** (below); a press outside it does nothing | depth |
| Band | – | ✓ | ✓ | whole stack, a Z crop or cutaway, a combining Z slab | depth |
| Thumb, hover mark | ✓ | ✓ | ✓ | a Z section's or slab's plane | depth |
| Readout (a `role=status` label, not a button) | ✓ | ✓ | ✓ | ✓ | depth |
| Cog | ✓ | ✓ | ✓ | ✓ | depth or frames |
| Frame steps | T | T | T | T | frames |
| Keys `]` `[`, Shift+wheel | plane | range | plane | **the cut along Z** (`viewState.step`) | depth |
| Popover: Show (one plane, or an operation) | ✓ | ✓ | ✓ | – | not colour |
| Popover: Percentile | – | when pct | when pct | – | |
| Popover: Planes z0–z1, Follow | – | ✓ | ✓ | – | |
| Popover: Composite, View | – | – | – | ✓ | WebGL2 |
| Popover: Cut (in words, Whole stack, Cut… opens the dock) | – | – | – | ✓ | cut |
| Popover: Speed, About, Direction (Play's turn) | – | – | – | ✓ | prefs |
| Popover: Grid, its options while on | – | – | – | ✓ | the 3-D view's grid |
| Popover: Surfaces (show, light) / New / Import | – | – | – | ✓ | a surface / Surface Studio / the layers |
| Popover: Depth card… | ✓ | ✓ | ✓ | ✓ | the card |
| Thresholding's View Plane/Slab row (`gating.view`) | ✓ | ✓ | ✓ | – | registered by the plugin |
| Depth card: projection rows (`card.projection`) | ✓ | ✓ | ✓ | – | depth, not colour |

In 3-D the rail is the cut along Z (`rail` config `{plane, band, bandMove, cut}`):

| Cut | Rail |
|---|---|
| whole stack | the band is every plane; an end resized makes a Z crop; the band moves |
| Z crop | its range, resized and moved |
| Z cutaway | its range; only the end that is the cut moves |
| Z section, Z slab | the thumb is the plane, picked and dragged (a combining slab's planes as a band) |
| nothing cut along Z | nothing; a press does nothing |

The 3-D dock (views/volumeDock.js) is the `dock` surface, shown only in 3-D:

| Row | Full | Cutaway | Section | Slab | Crop | Needs |
|---|---|---|---|---|---|---|
| Cut (the mode) | ✓ | ✓ | ✓ | ✓ | ✓ | cut |
| Axes | – | ✓ | ✓ | ✓ | ✓ | |
| X / Y / Z (one per active axis) | – | the cut, kept side | the plane | the plane | the range | |
| Combine | – | – | – | ✓ | – | volumeDrawn |
| Thick | – | – | – | unless one plane | – | |
| Opacity | – | – | ✓ | – | – | |
| Around (the context) | – | – | ✓ | ✓ | – | volumeDrawn |
| Solid cut faces | – | ✓ | – | – | ✓ | volumeDrawn |
| Handles, Reset | – | ✓ | ✓ | ✓ | ✓ | (handles: prefs) |
| Undo, redo | ✓ | ✓ | ✓ | ✓ | ✓ | |
| More: Voxels | ✓ | ✓ | ✓ | ✓ | ✓ | volumeDrawn |
| More: handles' strength | – | ✓ | ✓ | ✓ | ✓ | prefs |
| More: panes follow the sections | – | – | ✓ | ✓ | – | sections |

Where values live is unchanged:

- The projection, the 3-D view's cut (`inspect`) and the composite are per image
  (`/view_state/<ds>`).
- Play's speed, axis and direction, the grid, the cut's handles, Motion and the pane sizes are
  per user (`/viewer_prefs`).
- Whether Play is turning is never stored, so a page never opens mid-motion.

## Adding a control

1. **Declare it.** In core, add an entry to `CORE` in `viewControls.js`. In a plugin, call
   `ctx.view.controls.register(entry, {row})` from `createInstance`. The id is prefixed
   with the plugin's name, and the entry is removed with the plugin.
2. **Need only what it needs.** If a capability you need is missing, add it to `CAPS`
   rather than testing the page in the control.
3. **Draw it where it belongs.**
   - In the popover, either add a builder to `ROWS` in `depthSettings.js`, or pass `row`
     (`(ctx, config, popover) -> {node, update(ctx, config)}`).
   - In the strip, add the node to `DepthStrip.nodes` under the same id.
   - On a plugin surface, ask `ctx.view.controls.visible(id)` when painting, and repaint on
     `ctx.view.controls.onChange` and `ctx.view.onChange`.
4. **Pin it.** Add its row to the matrix above and its cases to `view_controls_probe.mjs`.

Thresholding is the plugin example. Its View row (Plane / Slab, `gatingPlanes.js`)
registers `gating.view` with `surfaces: ["gating"]`, `modes: ["plane", "slab"]` and
`needs: ["depth"]`. It then asks `ctx.view.controls.visible("view")`, and falls back to its
own mode check on a core without the registry.
