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
- Chrome runs with the GPU (`--use-angle=metal` on macOS, `d3d11` elsewhere); a software
  renderer is refused. `--headed` gives the display's own refresh (120 Hz on a ProMotion
  panel); every number that is "within a frame" is measured against the run's own F.
  `PLEXORA_BENCH_CHROME` names the Chrome binary when it is not the platform's default.
- `run.py --scenarios a,b,c` runs them in that order (on the datasets that have them): one
  server serves the whole run, so what ran before warms its caches -- name a cold scenario
  (`zreveal`) before one that visits the same planes (`jump`).
- `run.py --out DIR` writes the run's JSON somewhere other than `docs/internal/3d/perf/`
  (a smoke run: `--out $PLEXORA_BENCH_HOME/results`). The record is written before the
  server is stopped, so a run that cannot stop its server still keeps what it measured.
- `report.py ... --metrics REGEX` keeps the metrics the expression finds, e.g.
  `--metrics "^(step|jump|drag|pan|scrub|zline|panes)_"`.
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
- `clip` (Slice & Clip, docs/internal/3d/SLICE_CLIP.md): in 3-D, an X crop's far face dragged by
  its handle grip with the real mouse, 60 moves (`clip_drag_*`: fps, frame P95/P99, dropped
  frames, `fetches` -- a cut is uniforms, so 0 -- and `turned`, whether the camera moved: 0);
  crops of one and of two axes previewed every frame (`clip_one_*`, `clip_two_*`); a Z section
  drawn alone moved through the stack, until its exact planes are in (`clip_sectionResolved`,
  `clip_sectionLevelFetches`). Skipped on a build without the cut.
- `area_slide` (the area, SLICE_CLIP.md "The area"): an area a quarter of the image across
  placed in 3-D and slid for 1.5 s as Shift+drag does, then released (`area_slide_frameP95`,
  `_dropped`, `_levelFetches`, `_blackFrames` and `_flashFrames` -- the stand-in texture should
  leave no black frame -- `_landMs` until the last area is in, `_ghosts`); then 30 slides in a
  row and the GPU bytes held (`area_slide_gpuBytes`, `_gpuOverBudget`). Skipped on a build
  without the area.

Scenarios for Z navigation as the screen shows it (no black frames, preview first, the
three panes in step). Luminance is always the bitmap path (`createImageBitmap`, never a
read of the plane's canvas); "black" is a frame whose brightest quarter of cells averages
under a tenth of the settled picture's (`pagekit.blackTrace`) -- a statistic a sliding pane
or a plane mid-drag does not move, unlike a mean over the settled picture's lit cells. Where a
build has no loading indicator (`__plexora.paneStatus`) the `*_statusShows` metrics are
simply absent, as are the stale-frame metrics on a build without `view:` marks. Every
case also reports its requests and the server's caches: `<case>_tileRequests`,
`_tileCanceled`, `_sectionRequests` (CDP log), `_serverReadP95` (the tiles' Server-Timing
`read`), `_serverCacheHitShare` (Server-Timing `cache;desc=hit`), and from
`GET /volume/<ds>/perf` before and after: `_chunkCacheHits`, `_chunkCacheMisses`,
`_chunkHitRate`, `_tileCacheHits`, `_tileCacheMisses`.

- `zreveal`: warm single steps from rest on one page (`step_*`), then cold jumps -- HTTP
  cache cleared, a fresh page, +50 planes or as far as the stack allows, each repetition
  4 planes from the last so the server is cold too (`jump_*`): `_previewMs` the first frame
  any cell of the picture moves by 2 levels; `_fullResMs` loaded at the level drawn at rest
  (`waitFull`); `_blackFrames` over [request, full + 400 ms]; `_statusShows` the plane's
  indicator.
- `raildrag` (depth >= 4): the Z rail dragged with the real mouse (CDP, a move every 16 ms
  on a fixed clock) down a leg and back: `slow` 3 planes/s over 9 planes, `fast` 40 planes/s
  over 40 (three sweeps on a stack under 40 planes), the plane alone and with the section
  panes (`_sections`). Every case is two passes on fresh pages: A without luminance --
  `_fps`, `_frameP95`, `_frameP99`, `_dropped` over the drag; `_staleFrames` frames at which
  the plane committed (`view:committed` mark) was 2 or more planes from the revision the app
  had started (`view:requested`), `_staleShare` their share, `_staleMax` the largest such
  lag; `_handLagMax` the hand (every `plexora:view-requested`) against the plane committed;
  `_paintedPlanes`, `_requestedPlanes`; `_settleMs` pointer up to the final plane loaded at
  the rest level (`waitPlane`, `waitFull`: the standby planes prefetched ahead of the hand are
  not waited for -- before 2026-10-13 they were, whenever they reached the world before the plane's last
  tile, and a fast drag's settle read ~0.7-0.9 s by that race) -- and B with it:
  `_blackFrames` from the press to 400 ms after the settle.
- `panesync`: the `sync` pan (a real-mouse drag of 120 px) with the section panes on and
  their canvases traced too (`lumPanes`): `pan_planeChangeMs` press to the plane's first
  change; `pan_{xz,yz}LeadMs` the pane's first change minus the plane's; `pan_{xz,yz}RecutMs`
  pointer up to the pane's new section on screen (negative: during the pan);
  `pan_{xz,yz}BlackFrames`; `pan_{xz,yz}StatusShows`.

### On Windows

Set `PLEXORA_BENCH_HOME` to a local drive with room (`small twoplane flat flatstack medium
large` are ~6.2 GB; ~9 GB once registered: the flatstack import writes a 1.9 GB pyramid
copy beside it and the data root holds ~0.4 GB; not a network share, not a synced folder), e.g.
`$env:PLEXORA_BENCH_HOME = "C:\plexora-bench"`. Chrome is looked for at
`C:\Program Files\Google\Chrome\Application\chrome.exe` (`PLEXORA_BENCH_CHROME` otherwise)
and runs ANGLE on Direct3D 11; headless on a discrete GPU reports the real renderer
(`ANGLE (NVIDIA, ... Direct3D11 ...)`). If `renderer` ever says SwiftShader, run with
`--headed` -- on both sides of a comparison, since `report.py` refuses to compare headed with
headless. The scratch server gets the bench's HOME as its `USERPROFILE`, `LOCALAPPDATA` and
`APPDATA` too: Windows ignores `HOME`, and the settings file (the viewer prefs a run writes)
lives under `%LOCALAPPDATA%\plexora`, so without them a run would rewrite the user's own.
Servers start in a process group of their own and are stopped with `taskkill /PID <pid> /T /F`
(the whole tree); `tasklist | findstr python` after an interrupted run finds a stray one.
