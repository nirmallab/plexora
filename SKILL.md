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
| `pyproject.toml`, `MANIFEST.in` | Packaging. Both must include frontend assets, shaders, and `client/src/js/**/*.js`. `MANIFEST.in` has no `plugins/*/static` glob, so each bundled plugin needs its own `recursive-include` line or an sdist installs fine and serves the tool with no client. |

**Server** (`plexora/server/`)

- `models/data_model.py` — the high-risk file. Datasource loading, zarr/OME-TIFF
  access, tile extraction and encoding, GMM/contrast statistics, segmentation,
  spatial queries. Holds mutable module-level globals (`source`, `config`,
  `channels`, `seg`, `zarray`, `metadata`, `_loaded_source`).
- `models/project.py` — **the project record**: one typed view of one
  config.json entry (`Project`, `ImageSpec`, `SegmentationSpec`, `DataSpec`,
  `ColumnRoles`, `ColumnGroups`). The only place that knows the on-disk shape;
  everything else asks it questions (`project.roles.x`, `project.has_table`).
  Two invariants: keys it does not model round-trip through `extra`, and every
  change goes through `patch()`, which merges. There is deliberately no API for
  replacing an entry wholesale — that is what used to destroy AnnData projects
  on save. It also owns the **file access**: `read_config()` / `write_config()`
  are the only sanctioned way to touch config.json, and every other module must
  go through them (`config_transaction()` for a read-modify-write spanning
  several calls). Writes go via a temp file and a rename, so a reader never sees
  a half-written file — reading or writing it directly reintroduces the race
  that made an import fail the next page with `JSONDecodeError: Expecting value:
  line 1 column 1`.
- `models/adapters/` — input-format layer. `base.py` defines `NormalizedDatasource`;
  `csv_adapter.py` / `anndata_adapter.py` / `spatialdata_adapter.py` implement
  `load_table()` and take a `DataSpec`; `get_adapter(type)` is the factory and
  `detect_data_type(path)` is what routes a dropped path to one of them.
  `classify.py` is the single marker-vs-metadata predictor (it replaced three
  drifting denylists); `inspection.py` reads a not-yet-registered file and
  proposes a read spec.
- `models/database_model.py` — SQLite `ChannelList`, per-datasource UI state.
  Plugin state and result tables go through `plexora.api.store` instead, which
  namespaces them `plugin_<plugin>_<name>`.
- `models/centroid_tiles.py` — prebuilt binary centroid records (`id/x/y`), gzipped.
  Unrelated to pixel tiles.
- `routes/` — `data_routes` (tiles, channel stats, cells), `page_routes` (viewer
  pages, `/client/<path>` static), `project_routes` (open/edit/save/delete),
  `import_routes` (`POST /import`, `/inspect_data`, the column screen),
  `quick_view_routes`, `browse_routes`, `tool_routes` (opening a tool and
  collecting what it needs), `system_routes`.
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
  column names. Plugins read roles, never literal column names, and never the
  raw config entry — `TableSource` is the typed view for the rare plugin that
  must open the file itself (gating writes gates into an AnnData's `uns`).
  A role the project has not collected yet is `None`; that is not an error, it
  is what a plugin declares in `Requires` so core can ask for it.
- `store.py` — `PluginStore`: `get_state`/`put_state` for plugin-private state,
  `get_table`/`put_table` (Parquet) for derived measurements, annotations and
  classifications written back to the app.
- `plugin.py` — the `Plugin` descriptor a plugin exposes as module-level
  `PLUGIN`, plus `Requires`, which lets core hide a tool whose needs the
  datasource cannot meet.

**Plugins** (`plexora/plugins/<name>/`) — each is one self-contained directory
holding its own `server/`, `static/`, `templates/<name>/` and `tests/`. Its
Blueprint carries its own `template_folder` and `static_folder`, so core never
needs to know where a plugin's files live. `gating` and `roi` are the bundled
examples.

`PLEXORA_PLUGINS` controls which are active: unset means every plugin found,
`""` means a deliberate core-only build, `"a,b"` means exactly those. Any number
can be active at once, and each plugin that draws cells gets a LAYER of its own
(`ImageViewer.registerCellLayer`) — its own colours, gate, mode and opacity,
composited in the order its sidebar card sits in.

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

## Import and Progressive Requirements

The rule: **import the minimum, then ask for more only when a feature needs it.**

**One import screen.** `upload.html` has a single form — name, image, optional
mask, optional data — and no tab per format. `detect_data_type()` decides which
adapter reads a dropped path, and `/inspect_data` answers the form's questions
in one request as the user types. The only controls that appear conditionally
are the ones the *file* forces: a table picker for a multi-table `.zarr`, an
image picker for a table spanning several images, and an expression-matrix
picker for a file carrying `layers`. None can be guessed — picking for the user
silently loads the wrong cells, or thresholds raw counts as if they were log
values. The layer choice arrives as `"X"` or `"layer:<name>"`, prefixed so a
layer that happens to be called `X` cannot be confused with the main matrix.

**A project starts as an image.** No `dataset` block is the first-class
"image only" state; there is no separate flag that can disagree with it. A CSV
import then goes to one confirmation screen (`/project/<name>/columns`) for the
marker/metadata split, because that is the one thing about a CSV that cannot be
worked out reliably. AnnData and SpatialData skip it — `var` and `obs` already
draw that line.

**Everything else is deferred.** A plugin declares what it needs in `Requires`
(`table`, `segmentation`, `markers`, `features`, column `roles`, plus an
`optional` tier); `missing_from(project)` returns typed `Requirement` descriptors and
`tool_routes` turns them into a form the client renders without knowing which
plugin asked. Answers are stored **on the project**, so a role collected for one
plugin is found already-answered by the next — that reuse is the whole point.

**A guess is not an answer.** The column predictor fills in most of a
conventionally-named table, so a well-named import leaves *nothing* missing —
and a tool would open having silently decided five things. `Requires` therefore
distinguishes three states, and `_needs()` sends three lists:

| list | meaning | field |
| --- | --- | --- |
| `missing` | nothing stored | empty |
| `confirm` | stored, but the predictor put it there | prefilled, shown once |
| `optional` | absent, never blocking | empty |

`Project.confirmed` is what separates the first two: a flat list of requirement
keys the user has actually answered. It is written by the modal, the CSV columns
screen and the edit page — all three are places a human looked at these values —
and the table-scoped part of it is dropped by `forget_table_answers()` when the
data file is replaced. `table` and `segmentation` are exempt from confirmation
(`_GIVEN_KEYS`): a path the user typed was never a guess.

Four properties worth not breaking:

- **Nothing already answered is shown.** A confirmed requirement is absent from
  every list, never rendered as a field the user has to dismiss.
- **An optional field offered and skipped is answered.** `optional_missing_from`
  filters by `confirmed`, so it is offered once, not on every open. A plugin
  that genuinely cannot proceed without one uses `requested_from` instead
  (`GET .../requirements?keys=...`), which ignores `confirmed` on purpose.
  A plugin whose `Requires` is *entirely* optional (ROI) has nothing in
  `missing` or `confirm` and so would never reach the modal at all through
  `COLLECT` — `tool_routes._resolve()` has a fourth outcome, `OFFER`, for
  exactly this: nothing blocks, but `optional_missing_from` is non-empty.
  `tool_panel()` treats it like `COLLECT` (the modal shows); the no-JS
  `<a href>` path in `open_tool()` treats it like `OPEN` on purpose, because
  nothing is blocking and detouring to the edit page there would be wrong.
  Since core's generic subtitle ("Plexora filled these in from the data") is
  false on a form with nothing filled in and nothing required, the plugin
  supplies its own line via `Plugin.intro`, and the modal's secondary button
  becomes "Skip" rather than "Cancel" — Skip saves through the same path as
  Continue (recording the offer as declined), because the caller re-enters on
  a `true` result and a plugin that requires nothing must not become
  permanently unopenable. `satisfy_requirements()` returns `stillOptional`
  alongside `stillMissing`, and the modal's save loop closes only when both are
  empty: attaching a data file makes a role like `cell_id` newly *offerable*,
  not newly *missing*, so `stillMissing` alone would close the form one
  question early.
- **The ask loops.** Naming a data file is what makes "which column holds the
  cell id" answerable, so `missing_from` reports roles and markers *only* once a
  table exists, and the modal re-asks after each save.
- **Compatible-but-not-ready still lists the tool.** Hiding it hides the only
  route to fixing it (`tests/test_plugins.py` pins this).

**The cell layer is a default, not a requirement.** `Project.cell_layer`
resolves to the best the project can draw — the mask when there is one,
centroids otherwise — and the stored value only records a user overriding that
on the edit page. It used to be `cell_layer=True` in `Requires`, asked before a
cell-drawing tool could open; a user who supplied a mask wants the mask, so that
was a dialog with a foregone conclusion. Nothing is drawn over the image on load
— `viewerControls.init()` binds the toggles and stops — and `enableCellLayer()`
turns the resolved one on when a plugin registers its cell layer in `main.js`.
It is asked per layer, not of the control as a whole: with several plugins
loaded, "something is already showing" is true as soon as any of them turned the
mask on. A mask whose pyramid is still converting falls back to centroids;
when the job lands, `main.js`'s `adoptSegmentation()` loads the layer in place
and swaps the drawing over (it used to reload the page, minutes into a session).

**`features` is which numbers, not which columns.** A plugin that reads marker
intensities declares `features=True`, and core asks — once, in the `confirm`
tier — which matrix they come from (`X` or one of `adata.layers`) and whether to
`log1p` them on the way in. Never asked for a CSV: one table of numbers is not a
choice. It is the mirror image of `markers`, which is asked *only* for a CSV.
Both halves rewrite the read spec, so answering either re-reads the datasource —
a threshold set against raw counts is not approximately right on a log-scaled
panel, it is meaningless, and nothing about the values themselves says which
they are.

Client side: `requirementsModal.js` renders the form (core-owned CSS in
`main.css`), `columnClassifier.js` is the two-box drag component shared by the
import step, the modal and the edit page, and `ctx.requirements.require(keys)`
lets a plugin ask mid-session — which is how gating gets an image-id column at
AnnData-save time instead of shipping its own "type a column name" box.

**Editing is generated from the record.** `project_edit.html` renders a section
only when `project.has` says it applies, and `POST /project/<name>` merges. The
image is the one thing that cannot change. The old path did the opposite — it
read every project as a CSV and rebuilt the entry from `{}`, which silently
destroyed AnnData projects; `tests/test_project_edit_routes.py` is the guard.

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
a project with no feature table legitimately sets that to `None`, so it could never
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
- **`config` and the database description are each one shared object.** Both are
  fetched once at boot and handed out by reference — `config` to ImageViewer,
  ChannelList and ViewerControls, and the description (`dd`) to
  `channelList.init(dd)`, `viewerSidebar.init(dd)` and every plugin's
  `init(dd)`. Anything that refreshes them mid-session (`__plexora.refreshDataset`,
  after the requirements modal changes which matrix is read) must **mutate them in
  place**; assigning a new object updates only its own reference and leaves every
  holder on the old one. This is not theoretical: rebinding
  `__plexora.databaseDescription` shipped a Thresholding panel whose slider
  readout was in log units while its histogram axis and slider domain were still
  in raw counts, because the gating panel reads the *sidebar's* reference.
  Merge per column rather than replacing entries — `image_min`/`image_max`/
  `image_histogram` and the quantization window are fetched lazily per channel
  (`ChannelList.ensureChannelStats`) and live in those same entries.
- **Loaded tools are cards, and cards are layers.** `toolLoader.js` gives each
  tool its own mount (`[data-tool-panel="<name>"]`) inside a card
  (`[data-tool-card="<name>"]`) in `#tool_panel_slot`, rather than writing a
  whole slot's `innerHTML` — the earlier version destroyed a second tool's DOM
  and left its controller wired to nodes no longer on the page. Three states,
  kept apart: **loaded** (record, panel and cached data exist, nothing drawn),
  **visible** (contributes a layer; several at once, stacked in card order, top
  card on top), **active** (the shared Cells control, opacity slider, picking
  and gate flows act on it, and its panel is expanded — exactly one, or none).
  Opening a tool makes it all three and stands the previous one down to loaded;
  its card's eye turns it back on and PINS it, and a pinned layer is exempt from
  the stand-down (the default is for the first switch, not a rule that keeps
  dismantling a stack). Cards drag to restack (`window.Sortable`,
  same vendored library as `columnClassifier.js`); the DOM order is reversed on
  the way to `setCellLayerOrder`, which stacks bottom-first.
  Switching tools calls the outgoing controller's `onHide()` before painting,
  and the incoming one's `onShow()` after. A controller that only touches
  widgets inside its own panel can ignore both — collapsing is a class, the DOM
  survives, and it comes back instantly. One that reaches outside its panel
  (viewer-canvas pointer handlers, document keyboard shortcuts) must stand those
  down in `onHide()` and re-arm in `onShow()`, or a hidden panel keeps eating
  input meant for the visible one. `onVisibilityChange(on)` is the separate hook
  for the eye: core switches a *cell* layer off by itself, but a plugin drawing
  its own overlay (ROI) has to be told.
- **One decoded label tile, one canvas per drawn layer.** `handleTileLoaded`
  fills `tile._layerContexts` (name → 2D context) and `tileDrawingCustom` blits
  them in `maskDrawList()` order with each layer's opacity — so restacking and
  opacity are redraws, and only a colour/gate/mode change re-renders, for one
  layer at a time (`rerenderSegmentationTiles(name)`). Hiding a layer drops its
  canvases and keeps its lookup table, which is why loaded-but-off is cheap
  enough to need no cache limit. `tile-unloaded` frees both — it used to free
  only `_array`, leaking a canvas per evicted tile
  (`tests/test_label_tile_lifecycle.py` pins it).
- **Two stacks, not one.** Card order restacks the mask layers among themselves.
  Centroid-mode layers draw on core's `CanvasOverlayHd`, which is above every
  mask tile whatever the cards say, and ROI's own overlay is above that. Which
  centroid POINTS exist is also not per layer: the gate is applied server-side
  when the tiles are fetched, so a visible-but-inactive gating layer colours the
  active layer's point set.
  `PlexoraToolLoader.activeTool()` is how a controller checks this for itself.
- **Changing a client file means bumping its `?v=` tag** in the template that
  loads it (and `plugins/<name>/__init__.py`'s `VERSION` for plugin assets, which
  stamps every URL `asset_urls` builds). Sources are served straight from
  `client/src/`, so a stale tag means the browser keeps running the old file and
  the fix looks like it did nothing. `viewerManager.js` and `glRenderer.js` are
  the exceptions: they are webpacked into `client/dist/vendor_bundle.js`, which
  has to be rebuilt *and* re-tagged.
- **Asset URLs in templates start with `{{ data.base_url }}/client/...`**, never
  `../client/...`. A relative URL resolves against the page's own path, so it
  works only for a page exactly one segment deep at the site root and silently
  404s everywhere else — no server-side error, just a page with no CSS and no
  JS. It broke `/project/<name>/columns` (three segments) outright, and every
  page under the Jupyter proxy, which adds a prefix. `tests/test_page_assets.py`
  fetches each page's assets to keep it fixed.
- **The marker/metadata split is the project's answer, never re-derived.** A
  plugin asks `ctx.dataset.table.markers` (core's
  `datasetContext.js`); the server side is `spec.columns.markers`, which
  `CsvAdapter` reads for `feature_columns`. Deriving it from the column
  statistics — "everything numeric with a histogram that is not id/x/y" — cannot
  tell a stain from a measurement, and a CSV puts both in one header, which is
  the entire reason the import screen asks. Gating did derive its own, so every
  CSV project got a threshold slider for `Area` and `Eccentricity`.
  `tests/test_marker_split.py` pins the rule and drives the real getter.
- **A screen asks only for what it is a checkpoint for.** The CSV import screen
  confirms `IMPORT_ROLES` (`cell_id`, `x`, `y`, `image_id`) — the roles that
  decide how the table is *read*. `celltype` is not among them: nothing in core
  reads it, and a plugin that wants an annotation column declares it
  (`Requires(roles=("celltype",))`) and is asked through the requirements modal
  at the moment it matters. Both halves matter — a role echoed back unasked is
  stored *and* marked confirmed, which retires a question nobody saw.
- **`is_transformed` is honoured by every adapter, and asked for on every
  format.** The log1p switch is a separate question from which matrix to read:
  a CSV has nothing to pick between and is still the format most likely to
  arrive as raw counts. It was skipped for CSV in `plugin.py`'s
  `_never_confirmed` and in the edit page's `has.features`, and `CsvAdapter`
  ignored the flag anyway — so the transform was unreachable, and would have
  been a lie if reached.
- **Anything that fits a distribution to marker values fits it on a log
  scale**, and reads `dataset.table.log_transformed` to know whether it has to
  apply the log itself. Marker intensities are log-normal; a mixture of
  *normals* fitted to raw counts chases the skew instead of the populations,
  and a mixture fitted to values that were logged twice sees a separation that
  has been compressed away. Both `get_channel_gmm` (image) and gating's
  `auto_gate` (feature table) do this, and the result is that the same data
  gates identically whether or not the user ticked log1p.
- **A GMM threshold is a density crossover, not the midpoint of two means**,
  and the fit needs three components rather than two. A marker's background is
  a broad distribution, near-symmetric once logged, so a two-component fit
  splits *it* instead of separating it from the positives — and the midpoint of
  the resulting centres sits inside the negative population. That shipped:
  gating called 27-46% of cells positive on markers whose real fraction was
  3-12%. `plexora/plugins/gating/tests/test_auto_gate.py` measures against
  populations whose true membership is known, and keeps the old estimator
  alongside as the baseline.

## Validation

Python environment is the conda env `plexora`:
`/Users/aj/miniconda3/envs/plexora/bin/python`.

```bash
# Test suite
python -m pytest --ignore=tests/test_spatialdata_adapter.py -q -p no:randomly
```

Current healthy state: **921 passed, 1 failed** on Windows/conda
(2026-08-21, after the plugin-layers work); on macOS expect 3 failures. With
`plexora/plugins` on the path — `testpaths` includes it. Those 3:
`test_quick_view_routes.py::test_quick_view_dedupes_name_on_repeat_registration`
and `test_register_image_datasource.py::test_derive_dataset_name_from_path` (a
Windows path assertion that cannot pass on macOS) fail on a clean tree too and
are unrelated to rendering.
`test_segmentation_mapping.py::test_a_user_supplied_label_pyramid_is_served_without_conversion`
passes alone and within the plugin suites, but fails in a full run — a
`data_model` global leak across test files (see the ROI trap note below), not a
product regression.

`pytest-randomly` is installed, so which tests land next to which varies run to
run unless `-p no:randomly` is passed — pass it for a comparable baseline.

**Two environments, neither complete.** The conda env
(`/Users/aj/miniconda3/envs/plexora/bin/python`) has everything except
`spatialdata`, so `tests/test_spatialdata_adapter.py` and the 11 SpatialData
cases in `plexora/plugins/gating/tests/test_anndata_gates.py` fail to import
there. `.venv/` has `spatialdata` but is currently missing `click` and reports
an empty `pip list` — a partially-synced Dropbox checkout, not a code problem.
Run the suite on conda with `--ignore=tests/test_spatialdata_adapter.py`, or
repair `.venv` to cover SpatialData too.

**`data_model`'s module globals leak across test files.** It keeps the loaded
datasource in globals (`ball_tree, source, config, seg, zarray, channels,
metadata, _loaded_source, datasource`); `_ensure_loaded` compares `source` and
`load_datasource` compares `_loaded_source`. Many test files register a
datasource named `"proj"`, so a test that loads a project and does not reset
these leaves the next file silently served the previous test's table — own all
of them via `monkeypatch` in any fixture that loads real data (see the ROI
plugin's `isolate_data_model`). Separately, a synthetic test image must be
`(2, 256, 256)`: a single-channel write comes back 2D and `data_model` indexes
`shape[2]`, and the pyramid walk needs every dimension >= 200.

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
  label tile — once per drawn layer — into `tile._layerContexts` and the
  tile-drawing handler blits those canvases, so the GL branch is unreachable for
  tileFormat 32. Editing the shader to change how cells are drawn will appear to
  do nothing. `tests/js/label_outline_probe.mjs` runs the real function against
  synthetic tiles, `cell_color_probe.mjs` pins its pixels byte-for-byte with and
  without a colour table, `cell_layer_registry_probe.mjs` the layer registry and
  `label_tile_lifecycle_probe.mjs` what a tile holds and when it lets go;
  `frag.glsl`'s `near_cell_edge`/`in_diff` are dead code inherited from
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
