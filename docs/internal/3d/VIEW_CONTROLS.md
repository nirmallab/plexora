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
  - `surfaces`, `surfacesPresent`, `surfaceStudio`, `surfaceMaking` (Plexora 3D), `surfaceRow`
    (a surface to show or one to make), `prefs`, `grid`;
  - `cut` (the 3-D view can be cut: services/inspectState.js is on the page), `volumeDrawn`
    (the voxels are drawn, not "surfaces only").
- **State flags (`FLAGS`)** describe what the user chose:
  - `mode.plane`, `mode.slab`, `mode.volume`;
  - `following`, `fixed` (a projection that follows the plane, or does not);
  - `rotating`, `sectionsOn`, `allPlanes`, `percentile`, `gridOn`;
  - the 3-D view's cut (docs/internal/3d/SLICE_CLIP.md), true only in 3-D:
    - `cut.full`, `cut.on` (a tool is cutting), `cut.cutaway`, `cut.crop`;
    - `axis.x`, `axis.y`, `axis.z`.
- **Typos fail at load.** An unknown capability, flag, mode or duplicate id throws at
  `register`.
- **No fixed point.** `excludes` and `depends` may name flags, or entries that have no
  `excludes` or `depends` of their own. Resolving is two passes, and `register` enforces
  this rule.
- **`config(ctx)`** says what a control *does* in a mode, beside whether it shows. Play's
  `kind` is `planes`, `band` (slide a fixed projection's range), `frames` or `rotate`. The
  rail's `{plane, band, bandMove}` says whether a press picks a plane, whether the band
  ends resize and whether the band moves. The cut panel's `cut.axes` says `{flip, single}`: both
  true for a Cut, which is along one axis (a radio) and flips the side it keeps.

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
| Area (`tool.area`: a square to place, opened in 3-D; V) | ✓ | ✓ | ✓ | – | depth, not colour, WebGL2 |
| Sections | ✓ | ✓ | ✓ | – | the panes exist, not colour |
| Cut, Crop (open the cut panel) | – | – | – | ✓ | depth, cut |
| Play | planes | slides the range | planes | **turns the stack** | depth or frames (single plane: frames) |
| Rail | picks a plane | range moves/resizes | range resizes | – (the cut is the panel's and the handles') | depth |
| Band | – | ✓ | ✓ | – | depth |
| Thumb, hover mark | ✓ | ✓ | ✓ | – | depth |
| Cog | ✓ | ✓ | ✓ | ✓ | depth or frames |
| Frame steps | T | T | T | T | frames |
| Keys `]` `[`, Shift+wheel | plane | range | plane | **the cut along Z** (`viewState.step`) | depth |
| Popover: Show (one plane, or an operation) | ✓ | ✓ | ✓ | – | not colour |
| Popover: Percentile | – | when pct | when pct | – | |
| Popover: Planes z0–z1, Follow | – | ✓ | ✓ | – | |
| Popover: Render (MIP / Additive) | – | – | – | ✓ | WebGL2 |
| Popover: Area (`area.size`: the square's side, µm or px) | ✓ | ✓ | ✓ | – | depth, not colour, WebGL2, prefs |
| Popover: Motion | ✓ | ✓ | ✓ | ✓ | depth or frames, prefs |
| Popover: Turn (axis and direction), Speed | – | – | – | ✓ | prefs |
| Popover: Grid (on, where, labels); Lines (spacing, strength) while on | – | – | – | ✓ | the 3-D view's grid |
| Popover: Surfaces (show once there is one; New, Import with Plexora 3D), Light | – | – | – | ✓ | `surfaceRow`; Light: a surface |
| Thresholding's View Plane/Slab row (`gating.view`) | ✓ | ✓ | ✓ | – | registered by the plugin |

In 3-D the strip has no rail (2026-10-13) and is only as tall as its buttons: the mode
buttons, Cut and Crop, Play and the cog. `]`/`[` and Shift+wheel still move a cut along Z.
There is no readout in any mode: every capture says what is drawn, and over a plane or a
projection so does the rail's `aria-valuetext`.

The popover is one line a setting: a label in a gutter as wide as the longest label, then
the controls. A hairline parts the groups, whose names are their `aria-label`s, not
headings, and what a control does is its tooltip. A control that means nothing in the state
on screen is hidden (the grid's Where and labels while it is off), not greyed. There are no
view presets: the gizmo's axes, a double-click and `0` already turn the view.

The Depth card (View > Depth, views/depthTools.js) holds what is set less often and nowhere
else: the plane or frame typed exactly (the plane not in 3-D), Add as a layer, the z step,
the 3-D copy, Cells in 3-D and the sections' default sizes (not in 3-D). It repeats nothing
the strip or the popover has, and the popover does not link to it.

The cut panel (views/volumeDock.js) is the `dock` surface. It opens to the strip's left from
Cut or Crop and stays out until its › or ×, also while `O` hides the cut. Its rows are the
tool's (`cutTool`: the tool cutting, or else the one last chosen):

| Control | Cut | Crop | Needs |
|---|---|---|---|
| Axes X / Y / Z | ONE (a radio), X at first | any (toggles; the last stays on), X Y Z at first | cut |
| Flip, beside the axis cut | ✓ | – | |
| Solid cut faces | ✓ | ✓ | volumeDrawn |
| Handles shown (eye) | ✓ | ✓ | prefs |
| Reset, Undo (Cmd/Ctrl+Z; Shift redoes) | ✓ | ✓ | |
| Crop to area (`cut.toArea`: the area becomes the crop) | – | while narrower than the area (`crop.narrower`) | |
| Voxels: smooth or blocks | ✓ | ✓ | volumeDrawn |
| Handles' strength | ✓ | ✓ | prefs |
| Save area; the saved areas once there is one (`area.save`, `area.list`) | ✓ | ✓ | `savedAreas` |
| `O` Show/Hide (the cut hidden or shown, the panel left out) | ✓ | ✓ | |

While the cut is hidden the panel's axes and actions are dimmed (`is-off`) and the `O` key cap
is lit; using any of them shows the cut again with the change.

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
