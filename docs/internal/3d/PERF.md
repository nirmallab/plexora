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
