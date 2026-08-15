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
| `plexora/__init__.py` | Flask app factory; `data_path`, SQLite path, base URL, notebook flag, active-module registration. |
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
- `models/database_model.py` — SQLite `ChannelList` / `GatingList`, per-datasource
  UI state.
- `models/centroid_tiles.py` — prebuilt binary centroid records (`id/x/y`), gzipped.
  Unrelated to pixel tiles.
- `routes/` — `data_routes` (tiles, channel stats, cells), `page_routes` (viewer
  pages, `/client/<path>` static), `project_routes`, `import_routes`,
  `datasource_config_routes`, `quick_view_routes`, `browse_routes`, `tool_routes`,
  `system_routes`.
- `modules/` — optional feature modules (`gating`, `hello`) mounted as Blueprints
  by `register_active_module()`, keyed off `PLEXORA_ACTIVE_MODULE` (default
  `gating`). A tool is only shown when `?tool=` matches the installed module.

**Client** (`plexora/client/src/js/`)

- `views/imageViewer.js` — the big one (~2.5k lines). Owns the OpenSeadragon
  viewer, the WebGL colorize pipeline, tile decode, overlays, export.
- `views/viewerManager.js` — tile source definition: `getTileUrl`, `getTileKey`,
  `toTileLevels`, and one `addTiledImage` per active channel.
- `services/glRenderer.js` — WebGL2 core ported from viaWebGL. Shader compile,
  quad buffer, default draw path.
- `workers/tileDecoder.js` — off-main-thread WebP tile decode.
- `src/shaders/{vert,frag}.glsl` — the colorize/composite shaders.
- Other views: channel list, gating sidebar, colour picker, open-project page,
  import/config forms.

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

**Tile decode.** `tile-loaded` reads the raw bytes off `e.tileRequest.response`.
The default 8-bit WebP path goes to the worker pool; HD 16-bit and segmentation
decode inline via UPNG.js (a global script, unreachable from a worker). Result
lands on `e.tile._array` as a `Uint8Array`.

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
