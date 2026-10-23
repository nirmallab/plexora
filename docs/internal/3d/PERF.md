# 3-D viewer performance: what was slow, what changed, what it measures now

Branch `feature/volumetric-3d` (worktree `../plexora-3d`). Plan:
`~/.claude/plans/pasted-content-id-7217-deep-lively-orbit.md` (stages S0–S7). Nothing here is
on `main`; it merges after the user's own testing (`TESTING.md`).

The complaint (2026-10-10): even stacks under 10 GB felt slow; the plane view and the XZ/YZ
panes updated at different moments; switching a channel on in 3-D took seconds; a Z step showed
a plane at the wrong brightness before it corrected. Every number below is measured on this
machine (Apple M2 Pro, Chrome with the GPU, 120 Hz) by `tools/bench3d/`, against the commit
before any of this work (`e7f386bc`, "before").

## How it is measured

`tools/bench3d/` (README there) starts a scratch server on a data root under `~/plexora-bench`,
drives Chrome over the DevTools protocol with the GPU on (`--use-angle=metal`; a software
renderer is refused), and observes the page from outside with scripts injected before the app
(`probes.js`, `pagekit.js`) — the same observers for every build, so a build with no marks of
its own is measured exactly like one with them. Each run records the renderer and the idle
frame period F; targets "within a frame" are in F. Results are JSON under `perf/`
(`<sha>-<label>.json`); `report.py` diffs two runs against `targets.json`; `parity2d.py`
checks that a planar project's tile URLs and settled picture are unchanged.

Datasets: `small` (3 ch × 24 × 1024², OME-TIFF), `twoplane` (one channel, plane 0 at 25 % and
plane 1 at 75 % of one window — the flash test), `ims` (the real Imaris stack, 6 ch × 190 ×
1538×763, gzip chunks 64 planes deep), `meso` (the real light-sheet OME-Zarr, 1937 × 2048²,
9.4 GB), `flat` (a planar project, for parity), `medium` (3 × 200 × 2048² sharded OME-Zarr,
never scanned before the run) and `large` (declared 4 × 4000 × 16384², 8.6 TB, of which a
128-plane core is written: registration and every path must stay independent of the declared
size).

Two measurement traps found and fixed in the harness, both of which had produced convincing
wrong numbers:

- **Reading the canvas back every frame demotes it off the GPU.** The first luminance sampler
  called `getImageData` on OSD's canvas per frame; Chrome then moves that canvas to software
  rendering and the scrub's frame-time P95 doubled (a "25 ms" scrub was the sampler). Luminance
  is now read through `createImageBitmap` downscales, and Z-step timings run on a page of their
  own, apart from the flash sampler.
- **The section pane's z-line moves its pane's scroll, not a line element** — the z-line metric
  was empty until the probe signature included the pane's view top.

## What was wrong (code-traced, then measured)

1. **No shared clock between surfaces.** The plane, the two panes, the 3-D view, 3-D label
   outlines and the contrast each followed a Z change on their own schedule: the panes' z-line
   drew the requested plane 67–93 ms before the picture showed it; panes refreshed only after
   OSD's `animation-finish` + 180 ms (1.4 s behind a pan); an overtaken pane handover still
   faded up and fired its 6 s deadline; contrast drags never reached the panes or the 3-D view.
2. **The brightness flash was the fade, not the data.** The fade held a copy of OSD's canvas,
   which is transparent wherever a channel is dim (alpha is coverage; the ground is CSS). Mid
   fade the next plane showed through under the old one and the sum overshot both: 7–10 bright
   frames per step.
3. **Shaders recompiled on every item.** Every `open` (one per channel item, two per channel)
   re-fetched the GLSL with `no-store` and recompiled: 34–122 compiles per page, 8 per step.
4. **Every tile decoded twice**, by OSD into a canvas and by Plexora's worker into a texture,
   with the buffer copied to the worker; nothing cancelled a superseded fetch.
5. **3-D textures were deleted when a channel was switched off**, and the level route was
   `no-store`: switching a channel back on, re-entering 3-D or reloading downloaded and uploaded
   the whole level again.
6. **The server serialised the reads.** Imaris: one lock per file held across decompression
   (and h5py's global lock besides); bricks: the store lock across zarr reads; no decoded-chunk
   cache, so a plane beside one just read decompressed the same 64-plane chunks again. A
   section tile through 1937 light-sheet planes decompressed about a gigabyte.
7. **Every first tile waited for the whole-stack window scan** (seconds for GBs, open-ended for
   TBs).

## What changed

| Stage | Change | State |
|---|---|---|
| S0 | Benchmark harness, `window.PlexoraViewPerf` marks/counters (`services/perfProbe.js`), Server-Timing phases on the volume routes, `GET /volume/<ds>/perf` | shipped |
| S1 | Opaque ghost: the fade is ground + canvas, a true crossfade (`planeMotion.compose`) | shipped |
| S1 | `GLRenderer.ready()`: the tile program is built once per page | shipped |
| S1 | Zombie cache on channel disable; 3-D contrast drags redraw; uniform locations cached | shipped |
| S2 | `NavigationRevision` (`services/viewState.js`): every surface (`main`, `labels`, `panes`, `volume`, `ranges`) prepares a revision off screen and all commit in one task; a late surface commits under the same revision only; superseded revisions are dropped | shipped |
| S2 | Units pinned per item (`hd`, window class, window stamp): an item still on screen converts the channel's range into its own units | shipped |
| S2 | Panes: generation-guarded handovers, the target cut fetched once the hand is still, sub-pixel cuts keep their section | shipped |
| S3 | `views/tileFetch.js`: one request per address, in order of need (on screen › panes › handover › prefetch; coarse before fine), one worker decode with the buffer transferred, finished as `context2d` (OSD's decode never runs), cancelled with its item | shipped |
| S3 | Standby planes: the next planes in the direction of travel kept built and loading invisibly; a step to one is an opacity flip | shipped |
| S3 | Residency ledger across viewers; coarse-first two-phase commit | not done — with standby planes a warmed step commits in 5–7 ms, so neither had anything left to win at the measured sizes |
| S4 | 3-D textures kept by what their bytes are (channel, frame, format, area, window) in a 1 GB LRU, whatever channels are on; hidden plane frozen in 3-D | shipped |
| S4 | Level payload (`server/volume/serve.py`): LUT quantization (byte-identical), ETag + 304 before a voxel is read, a year's cache when the request names its window, gate + single flight, LRU | shipped |
| S5 | Provisional window first, exact scan in the background (user's choice); every 8-bit tile/section/level names its window (`w=`); `services/volumeWindows.js` polls and hands over | shipped |
| S5 | Decoded-chunk cache (`server/volume/chunk_cache.py`); Imaris raw chunks read under the lock, inflated outside it (libdeflate); bricks pinned rather than locked; section read budget with drawn-up tiles; 304 before work on the tile route | shipped |
| S5 | Brick / batch-brick / manifest routes and per-brick stats | shipped 2026-10-11 with the atlas (below) |
| S5 | A badge saying the brightness is provisional | not done — the change is small and brief on the stacks measured; a question for the user |
| S6 | 3-D detail sub-volume: when the camera rests on part of the stack, that part is fetched one or more levels finer (`z=` plane range + `box=`) and drawn inside its box over the whole level; uploaded a slab per frame | shipped (re-scoped from a GPU brick atlas) |
| S7 | Data node: `/node/v1/image/<rid>/level` built by the same `serve.py` functions with the primary's choices sent explicitly; the primary forwards, caches and revalidates; node tiles honour `w=`; derived (slab/section) pyramids cached per view on the node (LRU 16) instead of rebuilt per tile | shipped |

### Decisions

- **Provisional, then exact.** The user's call. A provisional window is the default plane at a
  fine level plus a sample of the coarsest level's planes (milliseconds), never persisted; the
  exact scan runs on a background worker, requested channels first. Because every request names
  its window, a provisional tile and an exact one never share an address in any cache.
- **S6: a detail sub-volume instead of a brick atlas.** The plan's atlas needs a page table per
  level; at a terabyte stack's fine levels that is tens of millions of entries before a voxel is
  drawn, and the bricks would be fetched through a scheduler that did not exist yet. A detail
  box over the always-resident whole level gives the same "finer where you look" at a fixed GPU
  cost, reuses the level route (one more parameter), and was measurable the same day. The atlas
  remains the route to full resolution everywhere at once (below).
- **No coarse-first entry to 3-D.** Showing a coarser level first and replacing it would re-level
  the automatic contrast mid-view (a brightness jump — the very symptom being fixed).
- **Prefetch never fills the wire.** Standby planes take at most one request at a time and start
  only once the plane on screen has loaded: a cold channel enable had regressed to 2.9 s while
  standby tiles held the server. (2026-10-12: two at a time while nothing else is on the wire,
  and once the plane merely covers the view while the hand is moving; below.)

## Results

Headline numbers (p50 unless stated; full tables below). "Before" is `e7f386bc`.

<!-- headline table: filled from the final run -->

## The atlas and the slow link (2026-10-11)

**Brick atlas.** `server/volume/brick_serve.py` + `/volume/<ds>/bricks/manifest` and
`/volume/<ds>/bricks` (up to 48 bricks of one level, up to 6 channels, one request): bricks of
32 x 64 x 64 voxels with a one-voxel apron (the edge repeated past the stack), planes max-pooled
per level so a voxel is about as deep as wide, each brick's maximum per channel (a brick at or
below the window's floor is sent as empty and stored nowhere), quantized with the window the
base uses, read once per source tile per request (grouped by `LevelInfo.chunks`). Client
`views/volumeAtlas.js`: one atlas texture per channel (384 MB in all), a page table of every
atlas level (RGBA8UI: slot x/y/z + RESIDENT | REFINED | EMPTY), a greedy cut -- refine where a
voxel covers the most screen pixels first, until every voxel is under a pixel or the atlas is
full -- and a shader walk per brick crossed (REFINED before EMPTY: a coarse brick can be empty
where a finer one is not), skipping empty bricks along the ray. The whole level stays the base;
the detail sub-volume (S6) remains for images on a data node, which are not served as bricks.

Measured on this Mac (M2 Pro, Chrome, GPU), the Imaris stack (190 x 763 x 1538, 3 channels, base
level 1, atlas level 0): close up along z, 356 level-0 bricks in 1.2 s; sharpness of a 400 px
crop (variance of the Laplacian) 38 -> 299; a draw at rest 42 ms with the atlas, 9 ms without
(once, when the view settles); moving, 14 ms with it and 5 without -- moving frames take the
base's sample count and still read the bricks; no seams between bricks or against the base. The whole stack at an angle: 652 bricks (the room),
nearest first. The Xenium stack (14 x 27241 x 45450, 8 levels, base level 5): the walk passes
levels 0-4, close up the cut is level 2; 24 bricks a request read in 1.8 s (75 ms a brick)
once bricks were grouped by tile -- 0.9 s a brick before, each decoding 14 whole 1024-px tiles.

**HTTP/2 -- measured, not adopted.** Browsers speak HTTP/2 only over TLS; Plexora serves plain
HTTP on loopback and through tunnels that already encrypt. Chrome with 100 ms of latency and
50 Mbit/s (CDP `Network.emulateNetworkConditions`), the Imaris stack:

| | localhost | 100 ms, 50 Mbit/s |
|---|---|---|
| plane step, the tiles on screen (9) | 80 ms | 250-290 ms |
| 3-D base levels (3 x 32.7 MB, uncompressed) | -- | 18 s |
| atlas close up, uncompressed (159 MB) | 1.2 s | 43 s |
| atlas close up, gzip (78 MB of voxels, 21 MB on the wire) | -- | 3.8 s |

A plane step is a handful of tiles: two waves of six connections, one round trip that
multiplexing could save. The 3-D view is bandwidth-bound, where multiplexing saves nothing and
compression saves five-sixths. So:

- **Tile batching on a slow link** (`views/tileFetch.js`, `server/tile_batch.py`,
  `/generated/batch`, a node's `/node/v1/batch`): each server's link is the least wait for a
  first byte, less `Server-Timing`, over its last 12 tiles; over 60 ms, queued tiles of one tier
  go 16 to a request, each answered by its own GET dispatched in the server. Localhost never
  batches (its least wait is ~1 ms; a page stalled at load is not a slow link). A server without
  the route (an older node) is asked singly from then on. Only a volume's tiles go through the
  scheduler, so a flat image's tiles are unchanged.
- **gzip for voxels on a slow link** (`z=gzip` on `/level` and `/bricks`, level 1, ~195 MB/s):
  asked once two large responses have arrived under 30 MB/s, from the first request when the
  page itself is served from another machine.

## Pyramids in depth (2026-10-11, branch feature/3d-index-pyramids)

Not benchmarked yet: the `tools/bench3d` before/after runs (and the targets they would add)
are still to do. What changed, code-traced:

- **One z-pooling rule** (`model.pool_for`, a power of two): the level route, its hint, the
  bricks and the stored pooled levels pool alike, so one stored array serves all of them.
  The light-sheet stack's level-2 texture goes from 388 pooled planes to 485.
- **Levels that store fewer planes** (Imaris, an OME-Zarr store that halves z) are read once
  per stored plane (`model.read_stored`) and budgeted by stored planes: an Imaris level 1 of a
  190-plane stack is a 95-plane texture, not 190 with each plane twice. An OME-Zarr store's
  coarse levels are indexed by their own planes (they were indexed by the stack's).
- **Pooled levels stored** in the 3-D copy as `"{level}p{pool}"` (`bricks.pooled_levels`),
  built after the coarse levels, for z-chunked stacks too (`worth_pooling`): the level route
  and the atlas read them instead of every plane. Only `read_pooled` reads them.
- **No level fits**: `level=auto` answers 400 with a sentence (the 3-D view shows it) instead
  of reading, quantizing and dropping an oversized level 0.
- **Stacks get pyramids by voxels** (`image_pyramid.STACK_LEVEL_VOXELS`, 64 M a channel): a
  single-level 4096² × 200 stack imports with levels down to one that fits; the 3-D copy no
  longer copies a single-level stack past `FALLBACK_BUDGET`.
- **OME-Zarr reads through the chunk cache** (`chunk_cache.read_through`): a section's next
  tile reuses its neighbour's chunks; a read too large to keep bypasses it.
- **Drawn-up projection tiles are final**: cached and marked (`X-Plexora-Served-Steps`);
  sections stay `no-store`. An agent's render of a deep projection keeps to the same budget and
  says by how much it coarsened (`coarsened_levels`).
- **OME-Zarr stacks get the coarse levels they lack by the same voxel rule**
  (`ngff.needs_volume_extension`), and one whose derivation reads more than 256 MB derives them
  in the background (`data_model._with_volume_extension`, task `volume-levels`, shown on the
  Depth card). Until it is done a plane serves those levels as subsamples of the store's coarsest
  (`plan_extension`), so a zoomed-out tile never fails; when it is done the open project takes
  them up (`adopt_extension`). Its 3-D copy waits for the next load.
- **3-D label volumes go as coarse as the image** (`build_label_volume(min_levels=)`, given the
  image's level count at import and by 3-D segmentation); one built before strides a finer level.

## Controls, panes, no 3-D flash, faster Z (2026-10-12, branch feature/3d-controls)

Plan: `~/.claude/plans/pasted-content-id-84cc-plexora-sprightly-naur.md`. The registry and the
cog popover are described in `VIEW_CONTROLS.md`. Measured with the same harness build on
these trees:

- **pre:** `29de11ad`, main before the branch.
- **post:** `ff5b2b7d`, the branch as first committed.
- **fix:** `76fecffa`, main after the merge plus the two fixes below. Its files say `dirty`
  because `prefetch.py`'s `ahead <= 0` guard was not yet committed. That guard changes
  nothing at the default.

All runs: M2 Pro, Chrome with the GPU (Metal), 120 Hz, 3 reps. Datasets as above, plus `mediumz`
(OME-Zarr chunked 8 planes deep), `flatstack` (plane-major OME-TIFF) and `medium`, all under
`~/plexora-bench/dev/data`.

Two warnings about these numbers:

- **The first pre pass on `small` overlapped a full pytest run.** It was re-run on a quiet
  machine (`29de11adb848-pre-small.json`), and that run is what is quoted. The busy run had
  shown 244 → 0 dropped frames and similar figures, and none of them were real.
- **A single step's commit lands in the first or the second frame after the request**,
  depending on where in the frame the step falls. A median of 5 therefore reads about 6 or
  about 10 ms by chance. 17 steps on `small`: pre 7.6, post 7.9, main 8.2 ms (spread 2–15 ms
  in all three). There is no change.

### Z navigation (pre → fix, p50)

| | small | ims | meso | medium | mediumz | flatstack |
|---|---|---|---|---|---|---|
| plane painted after a scrub step (`scrub_paintLatency`, ms) | 36 → 4.5 | 71 → 6.7 | 82 → 6.5 | 18.5 → 12.7 | 15.5 → 10.9 | 19.1 → 9.5 |
| planes adopted from standby, key held at 8/s | 2 → 23 of 23 | 2 → 40 of 40 | 2 → 40 | 2 → 40 | 2 → 40 | 2 → 40 |
| planes behind the hand at release, 8/s (`held_lag`) | 0 → 0 | 0 → 0 | 0 → 0 | 2 → 0 | 2 → 0 | 0 → 0 |
| settled after release, 8/s (ms) | 2.3 → 1.7 | 2.6 → 1.6 | 3.6 → 9.0 | 315 → 116 | 253 → 25 | 5.3 → 13.8 |
| 50-plane jump, commit (ms) | 42 → 39 | 76 → 55 | 62 → 53 | 124 → 90 | 95 → 83 | 89 → 85 |
| the same jump 100 ms into a cold step, commit (ms) | 13 → 12 | 49 → 48 | 49 → 47 | 122 → 72 | 99 → 79 | 94 → 100 |
| tiles fetched for planes other than the jump's two | 0 → 0 | 0 → 0 | 0 → 0 | 0 → 0 | 0 → 0 | 0 → 0 |
| tile requests during a 20-step scrub | 18 → 22 | 126 → 180 | 65 → 100 | 80 → 174 | 70 → 172 | 100 → 173 |

The last row is the cost. Standby now looks ahead by the hand's pace, up to six planes, so a scrub
asks for more planes than it shows. That is the reason every plane of a held key is already
built when the hand reaches it.

### What the first measurement caught (post, fixed in `76fecffa`)

- **A jump read as a held key.** The standby pace was planes per second over any two requests,
  so a 50-plane jump read as 500 planes/s. Six planes ahead of the destination were then asked
  for 30 ms after it first covered the view, alongside its own sharpening. The jump made
  100 ms into a cold step was hit hardest:
  - ims: commit 49 → 262 ms, full load 65 → 598 ms, 115 tiles fetched for other planes;
  - meso: commit 49 → 195 ms;
  - flatstack: full load 116 → 765 ms.

  Now a jump (more than `JUMP_PLANES`, 2) sets no pace, and neither does the step after it.
  Standby starts once the plane on screen merely covers the view only while the hand is moving
  (`STANDBY_FAST_RATE`). After a step or a jump the plane loads fully first, which was the
  earlier rule ("Prefetch never fills the wire", above).
- **A 3-D coast dropped frames on ims.** In MIP the moving frames now keep the step count, so
  brightness doesn't change when the hand stops. On a 120 Hz screen the ims coast then ran
  at 95 fps and dropped 17 frames, while averaging under the 24 ms that lowers the moving
  resolution. `learnFrame` now also steps down when 2 of 12 moving frames come later than 1.5
  display periods. Result: coast 0 dropped at p50, 120 fps. Resolution doesn't change MIP
  brightness; fewer steps did.

**The server's tile read-ahead, A/B on flatstack** (`fix-dev` against `fix-noahead`, where
`TILE_AHEAD = 0`). With it off:
- jumps differ by about 10% either way;
- the 15/s settle is 18% worse;
- the 8/s settle's p50 drops from 13.8 to 1.8 ms, but its p95 rises to 421 ms.

It is not what slowed anything, so it stays at 2.

### 3-D (pre → post, p50; the fix touched only moving frames)

| | ims | meso | medium | mediumz | flatstack |
|---|---|---|---|---|---|
| bright or dark frames at a window handover (`v3d_handoverFlash`) | 27 → 0 | 40 → 0 | 23 → 0 | 21 → 0 | 34 → 0 |
| black frames at a handover | 0 → 0 | 27 → 0 | 0 → 0 | 0 → 0 | 0 → 0 |
| brightness jump from release to rest (`v3d_settleJump`, of 255) | 3.7 → 0.27 | 3.6 → 0.42 | 8.7 → 0.59 | 2.2 → 0.26 | 2.0 → 0.26 |
| ghost crossfade, capture to release (`v3d_ghostMs` p95) | 1.1 | 1.7 | 1.1 | 1.2 | 1.3 |
| rotate, dropped frames (p50) | 0 → 0 | 0 → 0 | 0 → 0 | 0 → 0 | 0 → 0 |

The panes: a 200 px divider drag is honoured on every dataset. Before, it stopped at the
stack's height: 0 px on `small` and `flatstack`, 140 on `medium`, 152 on ims. No section is
asked for while the drag moves (before: one, on ims, meso and medium), and one is asked for
after it. Planar projects are
unchanged (`parity2d`: identical tile URLs, screenshot difference 0.000).

### Open

- **`detail_dropped`**, frames dropped while a wheel-zoom waits for the detail sub-volume, a
  single sample per run:
  - ims 0 → 27 after the fix (70 before it);
  - medium 0 → 42 (post, not re-run after the fix);
  - meso 121 → 116, already over the target before the branch.
- **`reenter`**, 3-D re-entry with the textures cached: ims 12.8 → 16.9 ms, meso 13.1 → 13.4 ms.
- **`first_tile_read` on `small`'s first tour**: 6.6 → 72 ms. That is the 3 tiles the server
  read itself; the other 23 came from the read-ahead's cache. Not investigated.

Every run is under `perf/` as `<sha>-<label>.json`: pre, pre-dev, pre-small, post, post-dev, fix,
fix-dev, fix-noahead, step15-pre/post/main. Regenerate the tables with
`python tools/bench3d/report.py perf/<before>.json perf/<after>.json`.

## Z navigation: never black, preview first, the panes in step (2026-10-13, Windows)

Plan: `~/.claude/plans/pasted-content-id-bfed-improve-virtual-clarke.md`. User-facing notes in
`TESTING.md` ("Scrubbing", "Loading and failure"). Measured here for the first time on Windows
11 (RTX 3080, Chrome headless on ANGLE D3D11 -- the real GPU, F = 16.7 ms), on the synthetic
fixtures only: `ims`, `meso` and `xenium` are not on this machine.

### What was wrong (code-traced, then seen live)

1. **Black at the deadline.** `planeSwapGroup` handed over after 8 s whether or not a tile had
   loaded, and the commit dropped the old pair in the same call: a plane the server could not
   serve in time, or at all, showed the ground. The panes did the same at 6 s.
2. **A failed tile was coverage.** OSD marks a failed tile `exists=false` and no longer counts
   it as loading, so `covers()` took it for a tile past the edge and `getFullyLoaded()` went
   true over the hole. A plane whose tiles all answered 500 committed EMPTY at ~95 ms and the
   view was black for the nine seconds of retries (seen live; the pre tree does the same).
3. **A drag never previewed.** Pointer events less than 80 ms apart were never a "jump", so the
   revision in flight was kept for up to a second before the newest request started from zero;
   the swap waited the 140 ms grace even while the hand moved; a rail drag on a deep stack
   moves more than two planes per event and so read as a jump (no pace, two standby planes,
   full-load gate). And every preview fetched its FULL-RESOLUTION tiles too: OSD's first update
   of a new item issues ten tiles, so each revision put one coarse and four full-resolution
   16-bit tiles per channel on the wire, and the next plane's coarse tiles queued behind them.
   One cycle cost 126-157 ms (p50) with the server warm; at 40 planes/s that is 16-19 planes
   painted of 41 asked, the picture up to 8 planes behind the hand.
4. **The panes** swapped only on full load, XZ and YZ independently, labelled the new cut before
   it arrived, and a pan during a deferred new-frame handover cancelled it with the old frame's
   tokens. A pane shorter than its stack exposed unloaded rows on a Z scroll.
5. **No feedback** beyond the rail's thumb, which stopped breathing at the coarse commit.

### What changed

| Where | Change |
|---|---|
| `services/channelFade.js` | A tile tagged `retrying` is not coverage; `final` is a hole. `settledLoaded`, `blank`, `failedOut`, `coveringLevel`, `progress` (loaded/needed/failed at the display level). `whenCanDraw(item, done, {grace, onFailed})`: `null` = coverage never counts, `0` = ready as soon as any level covers, `>0` = grace then coverage; never ready while the item is blank; fully-loaded is believed only while no tile is retrying. `COVER_POLL_MS` 40 → 16. |
| `views/tileFetch.js` | Failures tagged (`fail`); `retryFailed(items)`. A scrub hold (`setScrubbing` / `endScrubbing`): while the hand moves, a plane coming in loads only its one-tile level (`SCRUB_COVER_SPAN` 0), the plane on screen does not sharpen (`SCRUB_SHARPEN_SPAN` 0), a standby plane loads its two coarsest levels (`SCRUB_STANDBY_SPAN` 1); held requests stay queued, never batched, still cancelled with their item; a request on the wire is never overtaken. |
| `services/viewState.js` | `scrubbing()` (requests < `SCRUB_GAP_MS` 80 apart, held `SCRUB_HOLD_MS` 100), `endScrub()`; a revision's `scrub` follows the REQUEST it serves; a scrub revision that has not reached coverage gives way after `SCRUB_SUPERSEDE_MS` 250 (a drag's first revision too); a revision whose main is ready is never superseded; a far jump supersedes in the request's own task; others get `SCRUB_SYNC_WAIT_MS` 40 to join a preview. |
| `views/viewerManager.js` | Grace 0 for a scrub revision. `PLANE_SWAP_SLOW_MS` 2000 reports "slow" and never hands over: the old plane stays until the new one covers; a plane with nothing drawable is never shown (status `error`, retry re-asks); a partial failure shows with holes. `plexora:surface-status` events for the loading UI. The 8 s deadline stays for the label plane and the brightfield base. |
| `views/imageViewer.js` | Stacks: `maxTilesPerFrame` 32, loader `jobLimit` 256 -- the scheduler, not OSD's FIFO, orders the queue. |
| `views/planeMotion.js` | A close-spaced far move is travel (pace, up to six ahead); the hold is armed per scrub request and released on `plexora:scrub-ended`; standby waits behind the visible plane's retries. |
| `views/depthStrip.js` | Pointer-up, the step key's keyup and Play's stop end the scrub. The thumb follows the main status. |
| `views/sectionPanes.js` | Coverage handover (`PANE_GRACE_MS` 140, 0 while the view moves or the hand scrubs); `PANE_SLOW_MS` 2000 never hands over; each pane of a pan lands on its own as soon as it covers (an older pan's late result never lands); labels painted at each pane's commit; `pendingT` for the frame race; `PANE_ROW_MARGIN_PX` 96 of rows kept loaded beyond a scrolled pane; `tileRetryMax` 3; status events. |
| `views/paneStatus.js` (new) | One pill per view: breathing dot after 220 ms, a ring that fills with `loaded/needed` once a preview has committed, amber retry on error; 300 ms minimum, 150 ms fade; still under reduced motion. |
| `server/volume/prefetch.py` | The two coarsest levels of a chunked stack are made ahead (`COARSE_TILE_AHEAD` 4) regardless of `z_decode`; a pyramid whose extension levels are pending is left alone. |
| `tools/bench3d` | Runs on Windows (process groups, `taskkill`, UTF-8, the scratch profile's AppData, D3D11). Scenarios `zreveal` (preview / full-resolution / black frames by luminance), `raildrag` (a real mouse drag on the rail, 3 and 40 planes/s, with and without sections: frame times, stale frames from the marks, black frames), `panesync` (pane first-change vs plane, re-cut time, pane black frames, sampled inside the visible run); server cache deltas from `/volume/<ds>/perf`. `waitPlane`/`waitFull` ignore standby planes. |

Decisions: no overlay of the new pair over the old (the layer stack interleaves pairs by
`ITEM_Z`, so two planes add); the `scrub` supersede limit stays at 250 ms -- 120 ms starved a
drag on medium (23 of 29 revisions superseded, the picture 14-37 planes behind) and gained
nothing once previews stopped fetching full resolution.

### Results (pre `aad219d` → this change, p50 unless stated, 3 reps, new harness on both)

Records: `perf/aad219d7c0cb-znav-pre-final.json` and `perf/d7d979226e9e-dirty-znav-post-final.json`
(the earlier `znav-pre` / `znav-post` pair was measured with a harness whose "fully loaded" wait
counted the invisible standby planes, which the pace rules now build 30 ms after a drag ends;
its `settleMs` rows are the standby planes, not the picture). "dirty" because the other work in
the tree that day was not yet committed. 40 planes/s is one plane per 25 ms, faster than any hand.

| | small | medium | flatstack | large |
|---|---|---|---|---|
| black frames, every drag / step / jump / pan (max) | 0 → 0 | 0 → 0 | 0 → 0 | 0 → 0 |
| fast rail drag (40 planes/s, 41 asked): planes painted | 23 → 24 | 14 → 31 | 14 → 32 | – |
| … with the panes on | 23 → 23 | 16 → 30 | 15 → 29 | – |
| … furthest the picture trailed the hand (planes, max) | 3 → 3 | 8 → 3 | 8 → 3 | – |
| … requests behind the hand at the worst moment (`handLagMax`) | 4 → 5 | 14 → 6 | 14 → 5 | – |
| … share of frames ≥ 2 planes behind (`staleShare`) | 0.23 → 0.19 | 0.78 → 0.75 | 0.83 → 0.77 | – |
| … tile requests for the drag | 44 → 44 | 160 → 112 | 166 → 112 | – |
| … frame time p95 (ms) | 16.9 → 16.8 | 16.8 → 16.8 | 16.9 → 16.9 | – |
| … final plane sharp after release (ms) | 21 → 21 | 55 → 52 | 53 → 53 | – |
| slow drag (3 planes/s): stale frames / black frames | 0 / 0 | 0 / 0 | 0 / 0 | – |
| step: first visible change (ms) | 28 → 35 | 50 → 37 | 42 → 37 | 86 → 72 |
| step: full resolution (ms) | 39 → 33 | 59 → 43 | 52 → 36 | 51 → 46 |
| cold 50-plane jump: first visible change (ms) | 119 → 112 | 282 → 218 | 225 → 214 | 256 → 266 |
| cold jump: full resolution (ms) | 125 → 117 | 345 → 254 | 255 → 272 | 319 → 409 |
| pan with the panes on: pane changes before the plane (ms, p95) | 0 → 20 | 18 / 43 → 0 / 0 | 0 / 0 → 0 / 20 | – |
| pan: re-cut landed after release, XZ / YZ (ms) | 67 → 79 | 188 / 259 → 344 / 344 | 102 / 154 → 172 / 172 | – |
| pane black frames (max) | 0 → 0 | 0 → 0 | 0 → 0 | – |
| z-line ahead of the picture (ms, max) | 0 → 0 | 0 → 0 | 0 → 0 | 0 → 0 |

What the table says: at 40 planes/s the picture now follows the hand two or three planes
behind instead of eight, paints twice the planes with a third fewer requests, and the frame
time never moves; a step previews a frame sooner on the large stacks and is sharp 15-30 %
sooner; a cold jump on `medium` previews and sharpens a quarter sooner. `staleShare` stays near
0.75 because one revision is in flight at a time and a cycle is ~53 ms (see Open). Two rows
are not improvements as measured: the pan's re-cut, where a first version revealed XZ and YZ
in one task and so made XZ wait for the slower YZ, and `large`'s cold-jump full resolution.
Both were measured again the same evening, panesync alone, 3 reps, same server for each pair:

- **Per-pane reveal** (the spec: each view lands on its own; the generation guard still keeps
  an older cut from landing over a newer one). Re-cut after release, XZ / YZ p50 (ms): medium
  pre tree 130 / 203 → one task 153 / 153 → per pane 89-126 / 129-187; flatstack 97 / 113 →
  144 / 144 → 95-146 / 128-167 (its two panes cover at about the same time, so independence
  buys little there). Lead over the plane 0 ms in every post run (pre 17-43 on medium).
- **`large`'s cold jump** is run-order noise: 5 reps each, on → off → on with the coarse
  read-ahead, full resolution p50 310 / 281 / 301 ms against the pre tree's 331 under the same
  conditions, each run spanning ~200 ms rep to rep. The read-ahead never fires in this scenario
  anyway: at the home view the browser asks for no tile of the two coarsest levels (only a
  request at those levels queues planes ahead).

`step_previewMs` on `small` (28 → 35, n = 3) is one frame of jitter on a 25-plane TIFF;
`step_flashFrames` fails before and after (the sharpening swell, Open).

### Seen live (Playwright on the fixture server, `medium` with sections)

- A fast rail drag over 60 planes in 1.5 s: 0 black frames, 9-15 distinct planes painted (the
  pre tree 6), the final plane sharp 0.5-1.5 s after release (pre 1.3-2.0 s).
- `]` held two seconds: 0 black frames, 16 planes, both panes' z-line on the committed plane in
  every one of 350 frames.
- A cold 50-plane jump: the old plane unchanged until the commit at 235 ms, first visible change
  at 300-330 ms, full at 385 ms (pre: 769 / 1148 ms).
- A plane whose tiles answer 500: the old plane at full brightness (ratio 1.000) from the jump
  through 12 s; the pill says fetching at 50 ms, slow at 2 s, error with retry at 9.1 s; retry
  loads the plane in 0.4 s. Before: black from 194 ms.
- A planar project is unchanged (`parity2d`: identical tile URLs, screenshot difference 0.000).

### Open

- **The sharpening swell.** While a plane sharpens, OSD blends each finer tile over the coarse
  level (`blendTime` 0.18), and both carry coverage as alpha, so the sum overshoots: +28 % of
  the screen mean for 100-250 ms on a cold jump (`step_flashFrames` fails before and after).
  Replacing rather than adding within an item is a drawer change, not a constant.
- **`staleShare` at 40 planes/s** floors near 0.6-0.7: one revision in flight, a cycle of
  ~53 ms, the hand moves two planes per cycle. Under 50 ms needs the channel items added in one
  frame and coverage read on OSD's update rather than polled.
- **Deep zoom while scrubbing** previews from the one-tile level, which is blurry at full zoom; a
  budget by tile count rather than by level would keep it sharper.
- **A remote node**: `SCRUB_SUPERSEDE_MS` should scale with the link's measured wait.
- `POST /volume/<ds>/prepare` answers 409 on a stack without a bricked copy, on every pan with
  the panes on (pre-existing, logged in the console).
- **The first cold jump of a fresh page is slow on this tree and not on the pre tree**: in
  every `zreveal` run the first rep's full resolution was 802-852 ms on medium (pre 221) and
  385-406 on large (pre 329), its server read p95 292-328 ms (pre 18); the other four reps
  are level with or faster than pre. The coarse read-ahead is off in that scenario, so it is
  something the client asks for around its first page -- not found yet.

## The area: sliding it never blanks the view (2026-10-21, branch feature/area-tool, Windows)

`area_slide` (tools/bench3d README): an area a quarter of the image across placed in 3-D,
slid for 1.5 s as Shift+drag does (`onSlide` every 40 ms), released. `small`, real GPU
(RTX 3080, D3D11), 3 reps, record `perf/e5c3a1a33aa6-dirty-area.json`.

| Metric | Value |
|---|---|
| frame P95 while sliding | 16.8 / 16.8 / 16.8 ms, 0 dropped |
| flash frames (slide and landing) | 0 / 0 / 0 |
| level fetches per slide (2 channels) | 36 / 36 / 30 |
| last area in after release | 142 / 74 / 58 ms |
| crossfades | 2 / 1 / 0 |
| GPU bytes after 30 slides | 503 MB, under the budget |
| `clip` in the same run | drag 60 fps, P95 16.8 ms, 0 fetches, camera never turned |

Live (headless Chrome on the GPU, whole-canvas luminance read after every draw): 51 frames
through a Shift+drag and its landing, luminance 4.0-6.4, none below 20 % of the brightest.
The `u_tex_lo/span` read costs no measurable frame time here, so it shipped (plan step 6).

## Brightness across levels: a finer level never lights up the picture (2026-10-23, Windows)

Reported live (an Imaris stack on a data node, 51 channels, 8432 x 5668 x 256, four HD
channels, MIP): "a camera flash within tiles as the data loads", and "the image gets
progressively brighter as I zoom in". Plan: `~/.claude/plans/when-in-3d-there-nested-neumann.md`.

### What was wrong (measured in a second headless Chrome on the same server, writes stubbed)

- **A coarser level is dimmer.** The file's own pyramid is mean-pooled in x, y and z (level 4
  equals level 3 mean-pooled to 0.17 grey levels), and the brightest voxel along a ray through
  averaged voxels is lower: Hoechst's z-MIP mean 61 at level 4, 79 at level 3. The base is the
  whole stack at level 4; at rest `considerDetail` reads the part seen one or more levels finer,
  drawn inside its box over the base. At one camera and the same 1024 steps the picture went
  55.9 → 72.6 (canvas mean) as a detail landed, dissolved in by the ghost, and back as one was
  dropped: the flash. Each zoom that reached a finer level was another step up. The atlas does
  the same per batch of bricks on local images. Step count, window and opacity never changed.
- **The detail was lost close up.** `visibleRegion` found the part seen with a 16-per-side
  lattice; nearer than a lattice step no lattice point was on screen, it returned null, and the
  closest views were drawn from the coarsest data.
- **The camera could enter a thin stack.** `MIN_DIST` 0.03 is below this stack's half
  thickness (0.032): the eye went inside the slab, the rays started past most of the tissue and
  the picture faded to black at the closest zoom. A far-zoomed plane opened in 3-D did the same.

### What changed

- **One gain per channel, measured once as a finer piece lands** (`brightnessGain`,
  volumeRenderer.js): the mean of its columns' maxima over its footprint against the base's
  over the same footprint and planes, within 0.25-4. The base keeps a summary for this as it
  loads (every few columns of every plane, at most 2^20 samples, a few ms, none at level 0); a
  detail folds its maxima into the slab-per-frame upload it already did. A detail too small to
  match on its own (under 64 base samples) takes what earlier details at the same two levels
  measured. The shader multiplies a detail sample by `u_detail_gain[c]`. The atlas does the same
  per level and channel (`gains` in volumeAtlas.js): `locate` says the level it stopped at, the
  gains are read once per brick the walk enters, and a level not measured yet steps on from the
  coarser ones by the ratio between them.
- **The part seen is also found with a 5 x 5 grid of rays** entering and leaving the clipped
  box as the shader's do, so it stays as tight as the view at any distance.
- **`minDistance()`**: a zoom moves the eye along one line; the floor is where that line leaves
  the box drawn (the cut's), by 1.5 near planes, never under `MIN_DIST` from the pivot. Turned,
  the eye comes as close as just outside the box; a line that misses the box (panned off) has no
  box floor. `frameRegion` opens a far-zoomed plane at the closest outside.

### Decisions

- **The base is the reference, matched on the client.** No server work, no fetch, nothing per
  frame or per navigation step (the stack's one contrast stands); a frame costs one multiply per
  sample. The alternative -- a level route that serves every level at the finest level's
  brightness -- would need the finest data read for every coarse level.
- **Raw means, not display space.** Matching the windowed means instead moved the gains by
  under 0.03 and closed a third of the gap, and would have to be solved again on every contrast
  change. What is left is how the GPU samples (trilinear between voxel centres, a step longer
  than a fine plane, rays across columns when turned), which a per-column statistic cannot see;
  a high window floor (Hoechst's at 0.24 of the texture range) magnifies a 2-3 % gain error
  about five times.

### Results (live, the reporter's dataset, same camera before and after)

| Moment | Before | After |
|---|---|---|
| first detail over the base (oblique camera, canvas mean) | 55.9 → 72.6 (+30 %) | 26.0 → 28.8 (+11 %) |
| one detail to the next at rest (level 3 → 3, 3 → 2) | (stepped like the first) | +1.3 %, −2.0 % |
| additive, detail over the base | 7.9 → 10.3 (+30 %) | 2.96 → 2.88 (−3 %) |
| gains measured (level 3 / level 2 over level 4) | -- | 0.87-0.94 / 0.81-0.85 |
| closest zoom, oblique | eye inside the slab, black | stops at 0.0965, level 2 detail drawn |

(Something in the live session changed between the two runs -- the base alone drew 55.9 then
and 26.0 now at the same camera -- so compare within a column, frames of one run.)

**Frame time** (`volume`, `volume_flash`; `small`, `medium`; 3 reps; RTX 3080, D3D11; before a
detached worktree of `d8ca0545`, after the same plus this change; records kept out of `perf/`):

| Metric (p50) | small before → after | medium before → after |
|---|---|---|
| rotate fps / frame P99 | 60.0 / 16.9 → 60.0 / 17.0 ms | 60.0 / 17.5 → 60.0 / 16.9 ms |
| coast fps, dropped | 60.0, 0 → 60.0, 0 | 60.0, 0 → 60.0, 0 |
| detail fps | 60.0 → 60.0 | 60.0 → 60.0 |
| fill / handover flash, black frames | 0 → 0 | 0 → 0 |
| rest jump (luminance) | 1.73 → 1.42 | 4.31 → 3.61 |

`enter_first` (one sample a run) read 37 → 1054 ms on `small`; run again in the other order it
was 1070 (before) and 836 ms (after): noise, not this change. Compiling the atlas program costs
about 20 ms more (200 → 220 ms, two channels; ANGLE/D3D11), once.

## Beyond 2 TB: what is left

- **Direct routing for levels.** Node tiles can be fetched by the browser straight off the node;
  levels still go through the primary (which caches and revalidates them).
- ~~Pooled coarse brick levels built once~~ (done, above); remote (http) OME-Zarr shard-index
  caching under the chunk cache.
- ~~Predictive prefetch from velocity~~ (done 2026-10-12, above); a residency ledger across
  the main viewer and the panes if a dataset shows tile-cache churn.

## Sources borrowed from

| Platform | Mechanism | Where it landed |
|---|---|---|
| BigDataViewer (Pietzsch et al. 2015) | render from what is resident, refine to optimal; loader queue visible-before-prefetch, coarse-first | scheduler order, standby planes |
| Neuroglancer (`chunk_manager`) | priority tiers, cancellation by controller identity, stale completions discarded | scheduler tiers, revision guards |
| napari NAP-4 | immutable slice requests; newer supersedes pending; current slice shown until the new one applies | `NavigationRevision` |
| Viv (`XR3DLayer`) | one 3-D texture per channel; colour/contrast/visibility as uniforms only | 3-D residency (toggle = uniform) |
| webKnossos | prefetch one or two slices ahead in the direction of travel | standby planes |
| BigVolumeViewer, Usher & Pascucci 2020 | GPU brick cache + page table | future work (above) |

## Full tables
