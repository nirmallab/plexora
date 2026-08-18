# Plexora Project Guide

Working guide for the `plexora` repository: what it is, how the pieces fit,
where the sharp edges are, and how to validate a change. Written for a coding
agent arriving cold.

## What Plexora Is

A viewer and analysis tool for large multiplexed microscopy images (OME-TIFF
whole-slide data, tens of thousands of pixels square, many fluorescence
channels). Python Flask backend served by Waitress; OpenSeadragon frontend with
a WebGL2 colorize pass. Runs as a local desktop web app, inside a Jupyter
notebook, or behind `jupyter-server-proxy`.

The user picks a subset of channels, assigns each a colour and contrast range,
and pans/zooms an additively-blended composite. Optional layers: a segmentation
mask with cell outlines, cell centroids, and marker-threshold gating.

Entry points:

- Desktop: `python run.py` → `http://localhost:8000/`
- Notebook: `from plexora.jupyter import PlexoraViewer`
- Remote Jupyter/JupyterHub: `PlexoraViewer(..., proxy=True)`
- CLI sidecar: `plexora-server` (`plexora/server_cli.py`)
- Frontend build: `cd plexora/client && npm run start`

## Repository Map

**Top level**

| Path | Purpose |
|---|---|
| `run.py` | Legacy/local desktop entry point. Keep working. |
| `plexora/server_cli.py` | Notebook sidecar CLI (`plexora-server`). Waitress, `threads=8`. |
| `plexora/__init__.py` | Flask app factory; `data_path`, SQLite path, base URL, notebook flag, plugin installation. |
| `plexora/jupyter.py`, `plexora/proxy.py` | Notebook display API, subprocess lifecycle, proxy entry point. |
| `plexora/datasource.py` | Programmatic datasource registration (`register_datasource`, `register_image_datasource`). |
| `pyproject.toml`, `MANIFEST.in` | Packaging. Both must include frontend assets, shaders, and `client/src/js/**/*.js`. |

**Server** (`plexora/server/`)

- `models/data_model.py` — the high-risk file. Datasource loading, zarr/OME-TIFF
  access, tile extraction and encoding, GMM/contrast statistics, segmentation,
  spatial queries. Holds mutable module-level globals (`source`, `config`,
  `channels`, `seg`, `zarray`, `metadata`, `_loaded_source`).
- `models/adapters/` — input-format layer. `base.py` defines `NormalizedDatasource`;
  `csv_adapter.py` / `anndata_adapter.py` implement `load_table()`;
  `get_adapter(data_type)` is the factory.
- `models/database_model.py` — SQLite `ChannelList`, per-datasource UI state.
  Plugin state and result tables go through `plexora.api.store` instead, which
  namespaces them `plugin_<plugin>_<name>`.
- `models/centroid_tiles.py` — prebuilt binary centroid records (`id/x/y`), gzipped.
  Unrelated to pixel tiles.
- `routes/` — `data_routes` (tiles, channel stats, cells), `page_routes` (viewer
  pages, `/client/<path>` static), `project_routes`, `import_routes`,
  `datasource_config_routes`, `quick_view_routes`, `browse_routes`, `tool_routes`,
  `system_routes`.
- `plugins.py` — plugin discovery and installation. Finds descriptors via the
  `plexora.plugins` entry point group and by scanning `plexora/plugins/`, then
  mounts each under `/plugins/<name>/`. **Discovery imports nothing it was not
  asked for**: names come from directory entries and entry-point metadata, so a
  core-only build never pays for an addon's dependencies. A plugin's package
  name must therefore match its declared `PLUGIN.name`.

**Public plugin API** (`plexora/api/`) — the only surface a plugin may use.
A third-party pip package and a bundled one get exactly the same thing.

- `dataset.py` — `dataset(name)` returns a `Dataset`: `image` (always present,
  the floor of the contract), optional `segmentation` and `table`, and a
  `DatasetSchema` mapping roles (`cell_id`, `x`, `y`, `celltype`, `image_id`) to
  column names. Plugins read roles, never literal column names.
- `store.py` — `PluginStore`: `get_state`/`put_state` for plugin-private state,
  `get_table`/`put_table` (Parquet) for derived measurements, annotations and
  classifications written back to the app.
- `plugin.py` — the `Plugin` descriptor a plugin exposes as module-level
  `PLUGIN`, plus `Requires`, which lets core hide a tool whose needs the
  datasource cannot meet.

**Plugins** (`plexora/plugins/<name>/`) — each is one self-contained directory
holding its own `server/`, `static/`, `templates/<name>/` and `tests/`. Its
Blueprint carries its own `template_folder` and `static_folder`, so core never
needs to know where a plugin's files live. `gating` is the bundled example.

`PLEXORA_PLUGINS` controls which are active: unset means every plugin found,
`""` means a deliberate core-only build, `"a,b"` means exactly those. Any number
can be active at once; only the cell layer is exclusive (see `claimCellLayer`).

**Client** (`plexora/client/src/js/`)

- `views/imageViewer.js` — the big one (~2.5k lines). Owns the OpenSeadragon
  viewer, the WebGL colorize pipeline, tile decode, overlays, export.
- `views/viewerManager.js` — tile source definition: `getTileUrl`, `getTileKey`,
  `toTileLevels`, and one `addTiledImage` per active channel.
- `services/glRenderer.js` — the WebGL2 core. Shader compile, quad buffer,
  default draw path.
- `workers/tileDecoder.js` — off-main-thread WebP tile decode.
- `services/appStatus.js` — `window.PlexoraStatus`, the app-wide status
  indicator. See its own section below.
- `src/shaders/{vert,frag}.glsl` — the colorize/composite shaders.
- `pluginRegistry.js` — `window.Plexora.registerPlugin`, the client half of the
  plugin contract.
- `services/datasetContext.js` — client mirror of the server dataset contract,
  handed to each plugin as `ctx.dataset`.
- Other views: channel list, colour picker, open-project page, import/config
  forms. (The gating sidebar lives in the plugin, not here.)

Note: `imageViewer.js` is loaded as a **plain `<script>`** from `base.html`, not
bundled by webpack. Only `vendor.js`, `viewerManager.js` and `glRenderer.js` go
through webpack into `client/dist`. So `imageViewer.js` has no module system —
top-level `class` declarations are globals, and `node --check` is a valid syntax
gate for it.

## The Rendering Pipeline

This is the part most worth understanding before touching anything visual.

**One TiledImage per active channel.** `viewerManager.js` calls `addTiledImage`
per channel with `compositeOperation: "lighter"`, so channels blend additively
via canvas compositing. Segmentation is a further layer with `tileFormat: 32`.

**Tile request.** `getTileUrl` →
`/generated/data/<datasource>/<channel>/<level>/<x>_<y>.png` (`?q=hd` for the
16-bit path). Server side: `data_routes.generate_png` → `_get_tile_png_bytes`
(1500-entry LRU keyed on `load_generation`) → `data_model.encode_tile` →
`generate_zarr_png` slices the zarr pyramid level → quantize → encode.

**Tile decode.** `tile-loaded` reads the raw bytes off `e.tileRequest.response`,
and both channel paths decode in the worker pool. The default 8-bit WebP path
uses `createImageBitmap` + a canvas readback; the HD 16-bit path parses the PNG
directly and inflates with the browser's native `DecompressionStream`, never
touching UPNG.js. Only segmentation tiles still decode inline via UPNG (RGBA8,
and a single layer rather than one per channel). Result lands on
`e.tile._array` as a `Uint8Array`.

The HD PNG is written with **stored (uncompressed) deflate** on purpose — see
the measured facts below. The worker's PNG parser only handles what
`fast_png.py` emits (non-interlaced, filter type 0) and returns `unsupported`
otherwise so the caller falls back to UPNG.

**Colorize.** `tile-drawing` runs per tile. It uploads the tile to a texture and
runs a one-channel fragment shader that multiplies the scalar by the channel
colour, then blits the WebGL canvas into the tile's own 2D canvas. OSD then
composites that canvas onto the sketch canvas, and the sketch onto the display.

**The critical optimization.** OSD re-raises `tile-drawing` for every visible
tile of every channel on *every frame*, and the pixels are almost always
identical to the previous frame's. `e.rendered` is the tile's own persistent 2D
context (OSD resolves it via `DrawerBase.getDataToDraw()` → the tile cache) and
OSD blits it immediately after the handler returns — so if it already holds the
right pixels, the handler returns early. A signature is stored **on
`e.rendered`**, not on the tile, so it travels with the canvas that actually
holds the pixels:

```
`${tile.cacheKey}|${tileFmt}|${floatColor}|${range}|${modes.edge},${modes.or}`
```

Anything that changes what should be drawn must be in that signature or the
viewer will show stale pixels.

## Status Indicator (`PlexoraStatus`)

One indicator for the whole app, far right of the navbar on every page that
extends `base.html`. Built by `services/appStatus.js`, styled in `main.css`.

```js
const task = PlexoraStatus.begin("Auto-contrast");
task.done();                       // or task.fail("reason"), task.relabel("...")
await PlexoraStatus.track("Saving", promise);
```

Three states, using the `--accent-success` / `--accent-warning` /
`--accent-danger` tokens: green "Live", orange plus a 1–2 word label with a
morphing glyph and a shimmer sweep, red plus a short reason. Tasks are
refcounted so overlapping features compose; the most recently begun label wins.
Debounced 150 ms before showing and held 400 ms, so warm sub-150 ms work never
flashes.

**Add new indication by calling `begin()`, not by inventing another affordance.**
Three inputs are already wired automatically:

- **`window.fetch` is wrapped once** in `appStatus.js` — every one of the ~40
  call sites (26 in `dataLayer.js`, each with its own swallowing try/catch)
  reports transport failures with no per-site change. It deliberately does *not*
  mark requests busy: a generic label is worse than the specific ones features
  supply.
- **`GET /health`** (`system_routes.py`, returns 204, does no work) polled every
  5 s while the tab is visible; two consecutive failures → red. Required because
  an idle page issues no other requests, so nothing else notices a dead server.
- **Tiles still loading**, tracked **per TiledImage** in `watchViewer()`. Not via
  the Viewer's aggregate `fully-loaded-change`: that only recomputes when some
  TiledImage raises its own event, and a newly added image doesn't raise one on
  the way in, so the viewer's cached `_fullyLoaded` stays `true` through the
  whole load and matches again at the end. Verified: **zero** viewer-level events
  across a channel toggle. Per-image tracking via `world`'s `add-item` /
  `remove-item` is the working hook.

## Performance: Measured Facts

These were established by profiling, not inspection. Several contradict the
obvious guess — read before optimizing.

**Server, 42 tiles (7 channels × 6), real whole-slide data:**

| | Before | After |
|---|---|---|
| First viewport | 63.8 s | 1.9 s |
| Re-pan over same tiles | 65.0 s | 0.005 s |

The dominant bug: `load_datasource()` ran on **every tile request** for
image-only projects. Its early return required `datasource is not None`, but
`has_feature_data=False` legitimately sets that to `None`, so it could never
short-circuit; and `generate_zarr_png` treated `seg is None` as "not loaded".
Every tile reopened the OME-TIFF, re-parsed the OME-XML, wiped the derived
caches and bumped `load_generation` — which, being part of the tile cache key,
pinned that cache at a permanent 0% hit rate.

Loadedness is now tracked by an explicit `_loaded_source` global, set last inside
`load_datasource()`. **Never add a guard that infers loadedness from a global
that can legitimately be `None`.** Use `ensure_loaded()`, and call it *before*
sampling `load_generation` for a cache key — loading is what bumps it.

**Client, mid-zoom pan, median / p90 / frames over 16 ms of 98:**

| channels | before | after |
|---|---|---|
| 5 | 8.3 / 9.3 / — | 8.3 / 8.5 / 4 |
| 7 | 8.4 / 25.1 / 18 | 8.3 / 9.3 / 3 |
| 11 | 8.4 / 50.1 / 45 | 8.3 / 8.9 / 6 |
| 15 | 33.4 / 90.9 / 73 | 8.3 / 9.3 / 7 |

Two changes got there, and the profile pointed at a different culprit at each
channel count:

- **At 7 channels**, 82% of wall time was one call: the WebGL-canvas → 2D-canvas
  blit, ~103 times per frame at 2.29 ms each. Fixed by the signature cache above
  (median 258 → 8.4 ms).
- **At 15 channels**, ~60% was tile decode (`handleTileLoaded` 24.6%,
  `getImageData` 23.5%, plus Blob/bitmap/GC). Fixed by the worker pool. It scales
  with tiles streaming in, i.e. with channel count — at 7 channels the same work
  was ~2%, which is why it did not show up first.

**HD mode was a separate, worse problem.** With 7 channels panning into fresh
territory, HD sat at **466.6 ms** median while the default path was 8.3 ms — 56x
slower — because the signature cache helps a *stationary* HD view but every
newly-arrived tile still paid for decode. A CPU profile put ~81% of all HD time
in pako's JavaScript inflate (`inflate_fast` alone 71%) and another 13% in
UPNG's unfiltering. Two changes, measured in sequence:

| | median |
|---|---|
| 16-bit PNG at zlib level 6, UPNG on the main thread | 466.6 ms |
| + stored (uncompressed) deflate | 125.0 ms |
| + PNG parsed in the worker with `DecompressionStream` | **8.4 ms** |

Uncompressed costs 1.15x the bytes (2.10 MB vs 1.82 MB per 1024² tile) and drops
server-side encode from 36.9 ms to 1.5 ms. Output verified byte-identical.

An earlier attempt to skip PNG entirely and send raw `uint16` **failed**: OSD
wraps the tile response in a Blob and needs a decodable image to build the
tile's canvas (which is what `e.rendered` is), so a non-image response leaves
the tile permanently unloaded and the canvas black. The payload has to stay a
valid image; the win comes from making it trivially cheap to decode, not from
dropping the container.

**Switching a channel on was slow for a completely different reason.** Not
tiles — those arrive in ~200 ms, and tile latency barely moves under load
(44 → 54 ms). The auto-level `GaussianMixture(3, max_iter=1000, tol=1e-6)` fit
costs 0.2–1.9 s per channel (17.1 s for all 19), and everything waited on it:

| | before | after |
|---|---|---|
| newly enabled channel becomes visible (cold) | 6.6 s | ~0.12 s |
| …reaches its final contrast | 6.6 s | 1.6 s |
| restoring 3 saved channels (cold) | 5.8 s | 1.3 s |

Three distinct causes, all fixed:

1. A new slot starts at `[0, 255]`, the whole byte domain. Against a
   quantization ceiling of the channel's full-plane max that renders the tissue
   near-black, so the channel was *drawn* in 200 ms but *invisible* until the
   fit landed. `get_image_channel_stats` now also returns `vmin_hint`/`vmax_hint`
   (percentiles of the log-intensity distribution it already computes);
   `autoChannel` applies those immediately and the real fit replaces them.
2. `qmin`/`qmax` — needed to convert a stored raw-16-bit range into the byte
   domain — were reachable *only* as two extra fields on the GMM packet. So
   restoring saved channels ran a ~1 s fit per channel purely to read them, even
   though the saved range already *is* the auto-level result. They now ride on
   the stats response; `ViewerSidebar.quantWindow()` is the single accessor.
3. The restore loop `await`ed that fit **per iteration**, so channels restored
   strictly serially — each GMM started within 3 ms of the previous one ending.

The hint percentiles (p50 / p99.5, `_HINT_PERCENTILES`) were chosen by sweeping
7×5 candidates against the real GMM for all 19 channels and scoring the error in
the **byte** domain — worst case 15 byte-levels, mean 6.6, versus ~234 for the
`[0, 255]` default. Do not retune by eye.

**Do not loosen the GMM's `tol` to make it faster.** At `tol=1e-4` the total
drops 17.1 → 5.9 s but 12 of 19 channels shift vmin/vmax by >2% and CD45 moves
155 → 475. The fit is also non-deterministic run to run (SMA varies 554–569 /
2812–2965) because `random_state` is unset — so nothing downstream may assume a
stable auto-level across sessions.

**Things measured and rejected — do not redo these without new evidence:**

- *Single-pass WebGL drawer* to eliminate the `compositeOperation: "lighter"`
  sketch canvas. Forcing `source-over` to isolate the cost made frame times
  **worse** (15 ch median 41.6 → 91.9 ms). The sketch canvas is not the
  bottleneck.
- *Per-frame colorize budget* spreading work across frames. The extra
  `forceRedraw` passes cost more than the spike they spread (11 ch median
  8.4 → 16.6 ms). Built, measured, reverted.
- *Never-blank rendering* (thumbnail underlay, `immediateRender`). Measured blank
  pixel fraction when panning into fresh territory: worst 0.002 on zoom-in,
  exactly 0 on a hard jump. OSD's coarser pyramid levels already cover it.
- *Fixing the GL texture cache, hoisting `gl.getParameter`, removing the
  O(tiles²) `tile-drawn` handler.* All real bugs, all worth keeping, but together
  they moved the median from 283.3 → 291.6 ms — nothing. The evictor fix matters
  for **memory**, not speed: the old one was written against OSD 2.x's
  `_tilesLoaded` shape (`{tile: ...}` records), so it freed nothing while still
  tearing tiles out of OSD's LRU, and the cache grew unbounded.

**Server per-tile cost breakdown** (1024² tile, after the fixes): zarr read
7.9 ms, LUT quantization 1.1 ms, WebP encode ~21 ms. Encode dominates. The
`method=` table is in a comment at the encode site — `method=0` is 21 ms/64410 B
versus `method=6` at 97 ms/58884 B.

**Concurrency ceiling.** `threads=8` buys less than it looks like: zarr 3 funnels
every read through a single global `zarr_io` event-loop thread, and tifffile
takes a per-file re-entrant read lock. All tile I/O is globally serialized; only
decode escapes to a pool. Caching is the lever, not thread count.

## Key Invariants

- **Tile size comes from the zarr chunk shape**, so HTTP tiles map 1:1 to TIFF
  tiles. `data_model.convertOmeTiff` currently hardcodes `chunks = (1, 1024, 1024)`
  for multiscale files instead of reading the real shape — fine for 1024-tiled
  sources, silently wrong (4× or 16× read amplification) for others.
- **The pyramid is real and used.** `_zarr_level(channels, level)` indexes the
  level group. When the source is a bare `zarr.Array` (non-pyramidal), `level` is
  ignored and every tile reads full resolution.
- **`qmin`/`qmax` must come from full-resolution data.** The downsampled `zarray`
  overview is mean-pooled, which dilutes single-pixel peaks and causes whole
  channels to saturate. `get_channel_quantization_window()` is deliberately split
  out of `get_channel_gmm()` so the tile path does not pay for the ~1 s
  GaussianMixture fit it does not need.
- **The black `fillRect` before the GL blit is load-bearing.** The shader emits
  alpha 0.9, so the output composites over whatever is already in the reused tile
  canvas.
- **Polars, not pandas**, in the data layer.
- Tile responses carry `ETag` + `Cache-Control`; the ETag embeds
  `load_generation` so a reload invalidates without rewriting tile URLs.

## Validation

Python environment is the conda env `plexora`:
`/Users/aj/miniconda3/envs/plexora/bin/python`.

```bash
# Test suite
python -m pytest tests/ -q
```

Current healthy state: **83 passed, 4 failed**. Those 4 fail on a clean tree too
— they are pre-existing and unrelated to rendering:
`test_datasource_config_routes.py::test_import_anndata_then_save_then_viewer_page`,
`::test_previewed_channel_names_are_what_gets_saved`,
`test_quick_view_routes.py::test_quick_view_dedupes_name_on_repeat_registration`,
`test_register_image_datasource.py::test_derive_dataset_name_from_path`.

```bash
# Syntax gate for the unbundled viewer
node --check plexora/client/src/js/views/imageViewer.js

# Frontend build
cd plexora/client && npx webpack

# Local server
python -m plexora.server_cli --port 8848 --host 127.0.0.1
```

The baseline smoke test takes a datasource via `PLEXORA_BASELINE_DATASOURCE`
(default `orion2`). On macOS `orion2`'s files live only on the Windows side of
this Dropbox-synced repo, so use a locally-populated datasource instead.

### Validating rendering changes

Syntax checks and unit tests prove nothing about pixels. Playwright is available
under `plexora/client/node_modules` (run with
`NODE_PATH="$PWD/node_modules"` if the script lives elsewhere). The pattern that
works:

1. Launch `chromium` with `channel: 'chrome'`, `args: ['--use-gl=angle',
   '--ignore-gpu-blocklist']`.
2. Load `http://127.0.0.1:8848/<datasource>`, wait, then activate channels via
   `window.__plexora.seaDragonViewer.updateActiveChannels(name, 'add')`.
3. Set a deterministic viewport with `zoomTo` + `panTo` + `applyConstraints`.
4. For **speed**: sample `requestAnimationFrame` intervals while driving
   `viewport.panBy` in a loop; report median/p90, not mean.
5. For **correctness**: hash the `#openseadragon canvas` pixels, then re-run the
   identical script against stashed pre-change code and compare. Cover base,
   pan-away-and-back, colour change, range change, HD toggle.
6. To attribute cost, monkey-patch `CanvasRenderingContext2D.prototype.drawImage`
   and bucket by source argument, or take a CDP `Profiler` trace.

Caution: `git stash push` only isolates a change if HEAD does **not** already
contain it. After a commit lands, that comparison silently becomes a no-op
against itself.

**Pixel hashes are not sufficient for a uniform rename.** Proven by mutation:
pointing the JS at `u_gating_shape` while the shader declared `u_cell_range_shape`
left all 14 captured pixel hashes identical, with no console error and
`gl.getError() == 0`. `getUniformLocation` returns `null` for an unknown name and
`gl.uniform2iv(null, ...)` is a silent no-op, and the colour-coded path that
would have shown it is unreachable from the UI — `u32_rgba_map` returns white
before consulting the range table unless `or_mode`, and `eval_mode` is always
`'and'`. Add a `getUniform` readback of the range-table shape, which tracks
`[5,2]`/`[5,1]`/`[5,0]` as gates change and goes `null` the instant the names
disagree.

Two setup requirements for anything touching the range table:

- **Use a datasource with segmentation.** That code lives under `u_tile_fmt == 32`,
  so a maskless datasource exercises none of it. Nothing in `plexora/data/` or
  `tests/` has one — build it. Any mask size works;
  `segmentation_pyramid.pyramidize_segmentation_mask` writes its own OME metadata
  and stops adding levels once the image fits one tile.
- **Know which mask kind the datasource stores.** `segmentationMode` is
  `"outlines"` (default: boundaries baked into the file) or `"filled"` (labels
  stored whole, boundaries derived client-side). Both are handled in
  `renderLabelTile()` in imageViewer.js — **not** in the shader. That trips
  people up: frag.glsl has a `u_tile_fmt == 32` branch (`u32_rgba_map`) that
  looks like it draws the label layer, but `handleTileLoaded` renders every
  label tile into `tile._renderedContext` and the tile-drawing handler blits
  that canvas, so the GL branch is unreachable for tileFormat 32. Editing the
  shader to change how cells are drawn will appear to do nothing.
  `tests/js/label_outline_probe.mjs` runs the real function against synthetic
  tiles; `frag.glsl`'s `near_cell_edge`/`in_diff` are dead code inherited from
  minerva_analysis and were never called there either.
- **Give gate ranges in data units, not 0–1.** Normalized values match no cell,
  and every gate state then hashes identically.

Hash `page.locator('#openseadragon').screenshot()`, not `canvas.toDataURL()` —
the latter returned stale content for the WebGL layer partway through a long
run. Screenshots reproduced bit-for-bit across runs.

Absolute frame times from headless ANGLE are pessimistic versus a real GPU. Trust
the before/after ratio, not the number.

## Sharp Edges

- `data_model` module globals are mutated under `load_lock`, but
  `generate_zarr_png` reads them without it. A datasource switch mid-pan can race.
- `getTileKey` omits the HD flag while `getTileUrl` appends `?q=hd` — same key,
  different URL. That is why `setHdMode` removes and re-adds every channel
  instead of invalidating. The GL texture cache works around it by including the
  pixel format in its own key.
- The server tile LRU is capped by **count** (1500), not bytes: ~2.7 GB in HD
  mode.
- `maxImageCacheCount` is a **shared** budget — OSD 6 creates one `TileCache` on
  the viewer and hands the same instance to every TiledImage, so it must cover
  visible tiles × channel count.
- `tileDrawingCustom` is declared `async` but has no `await` before its callback.
  It works only because it runs to completion synchronously; adding an `await`
  ahead of the draw would silently make tiles render a frame late or not at all.
- HD mode measurably darkens the image (mean pixel value ~506 → ~276). This is
  pre-existing and unexplained — verified identical before and after the
  performance work. Possibly a real bug in the 16-bit range handling.
- `tests/baseline_orion2.py` depends on datasource files that may not exist on
  the current machine; those tests skip rather than fail.

## Agent Operating Notes

- **Profile before optimizing.** Every intuition about this codebase's hot spots
  was wrong at least once; the numbers above were the only reliable guide, and
  the answer changed with channel count.
- Prefer measurements on the real slide over synthetic data — the costs are
  dominated by tile size and channel count.
- `data_model.py` and `imageViewer.js` are both large and load-bearing. Read the
  surrounding comments; several encode hard-won reasons (the `qmax` full-res
  requirement, the WebP-vs-PNG alpha corruption, the black fill).
- Keep `requires-python = ">=3.12,<3.14"` unless a dependency forces otherwise.
