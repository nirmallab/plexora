# bench3d — the 3-D viewer benchmark

Reproducible before/after numbers for the 3-D viewer: plane steps, scrubs, the
plane/section synchronisation, channel toggles, the 3-D view, contrast, zoom,
network and memory. Results and targets: `docs/internal/3d/PERF.md`.

```bash
PY=<the plexora conda env's python>
$PY tools/bench3d/fixtures.py make small twoplane flat        # seconds
$PY tools/bench3d/fixtures.py make medium large mediumz flatstack   # minutes
$PY tools/bench3d/fixtures.py register --ims <file.ims> --meso <stack.zarr> --prescan
$PY tools/bench3d/run.py --label S1 --datasets small,twoplane,ims,meso,flat
$PY tools/bench3d/report.py docs/internal/3d/perf/<before>.json docs/internal/3d/perf/<after>.json
$PY tools/bench3d/parity2d.py <before>.json <after>.json
```

- Everything generated lives under `$PLEXORA_BENCH_HOME` (default
  `~/plexora-bench`): fixtures, the scratch data root and HOME, run files,
  screenshots. Never inside a synced folder.
- Chrome runs with the GPU (`--use-angle=metal`); a software renderer is refused.
  `--headed` gives the display's own refresh (120 Hz on a ProMotion panel);
  every number that is "within a frame" is measured against the run's own F.
- The page is observed from outside (`probes.js`, `pagekit.js`, injected before
  the app): the same observers run against every build, so a "before" build with
  no marks of its own is measured exactly like an "after".
- To measure an older commit without stopping work: `git worktree add --detach
  ~/plexora-bench/tree-<sha> <sha>` and `run.py --tree ~/plexora-bench/tree-<sha>`.
  When the change is in what an import derives (pyramid copies, recorded levels), give each
  tree its own data root, registered by that tree: `fixtures.py register --tree <tree>
  --data <root>`, then `run.py --tree <tree> --data <root>`.
- Besides timings, a run records what entering 3-D read (`enter_levelMB`, the level route's
  server read phase `enter_serverReadMs`, refusals and failures `enter_errors`), whether every
  channel is drawn from one level (`volume_levelSpread`), and each dataset's derived files on
  disk (`bricks_diskMB`, `pyramid_diskMB`).
- `legacy/` holds the scripts the numbers in TESTING.md were first taken with.

Scenarios added for the 3-D controls branch (plan sections 2.5, 3.6, 4.0); each degrades
on a build without the hooks it reads (no marks: the request log stands in; no 3-D ghost:
no ghost metrics; no `texmiss` mark: 0). `_L0` is the same measure zoomed in to one image
pixel per screen pixel (the finest level); unsuffixed is the home view. `jump` and `held`
load a fresh page (`?z=` the starting plane, HTTP cache cleared) for every repetition, so
OSD's in-memory tiles never carry one pass into the next; the server's caches stay warm.

- `jump` (depth >= 10): from rest, +50 planes, to the farther end, back; then the same +50
  issued 100 ms into a cold single step (`jumpBusy_*`; `jumpBusy_inFlight` says whether the
  step was still loading when the jump went -- on a local source it usually is not).
  `*_commit` the plane on screen; `*_full` fully loaded AND drawing the level the plane drew
  at rest (`waitFull`: `waitPlane`'s own `full` can end at the swap, while the new items
  cover at a coarser cached level); `*_wastedTiles` tile requests (CDP log, by the `view=`
  plane, wall time) for planes other than the origin and destination between the request
  and the full load; `*_originTiles` the plane being left, same window; `*_waitMs` until the
  destination's revision started (`view:requested` mark; else its first tile request).
- `held` (depth >= 10): the real `]` key held at 8 (`held_*`) and 15 (`held15_*`) steps/s
  over 40 planes, released one interval after the last repeat (a page-side step loop if the
  page does not take the key: `extra.input`). `_lag` planes requested minus planes on screen
  at the keyup; `_settle` keyup to the final plane on screen and loaded at the rest level;
  `_standbyAdopted` the app's `standbyAdopted` counter over the hold; `_painted` planes shown.
- `volume_flash`: a luminance trace of the 3-D view (createImageBitmap, the 3-D ghost
  blended at its opacity where there is one). After a rotation drag: `v3d_restJump` the
  largest frame-to-frame change of the centre's mean from release to rest + 600 ms
  (`v3d_settleJump` from rest only), `v3d_flashFrames` 1-2-frame excursions beyond both
  neighbours by > 6, `v3d_blackFrames` frames under a tenth of the settled mean. After 8
  wheel notches in: `v3d_fillFlash`, `v3d_fillBlack` the same. A forced window handover
  (`volumeWindows.adopt`, qmax x1.1 as exact) with the view redrawn every frame (as motion
  or a contrast drag redraws it; a still view left alone keeps its last picture):
  `v3d_handoverFlash` frames outside the before/after band by max(2, 5 %),
  `v3d_handoverBlack` under a tenth of after -- means over the cells the settled picture
  lights up -- and `v3d_texmiss` marks. `v3d_ghostMs` each 3-D ghost capture's duration.
  `extra.series` keeps the first repetition's means per stage.
- `panes_resize`: sections on, the XZ divider dragged up 200 px in 20 real mouse steps from
  the default size: pane loads (shows that started a load: a new `generation`) during the
  drag (`panes_showsDuring`) and in the 2 s after (`panes_showsAfter`),
  `--plx-section-xz-h` never shrinking during the drag (`panes_monotonic`), how far it grew
  (`panes_grownPx`), the label stretched (`panes_labelStretch`; text in
  `extra.panes_label`), frame-time P95 during the drag (`resize_frameP95`); before/after
  screenshots under `$PLEXORA_BENCH_HOME/shots/`. The size is reset to the default after.
