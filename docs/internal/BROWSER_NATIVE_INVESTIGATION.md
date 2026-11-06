# Browser-native Plexora: investigation, recommendation and proof-of-concept plan

Investigation, 2026-11-05. For Plexora engineers who know the codebase and will
build, review or run the proofs of concept for a browser-native deployment
mode.

## Status

- **Proofs of concept:** built in `tools/poc_browser_node/`. Nothing under
  `plexora/` changed. Their records are in `docs/internal/bench/browser_node/`
  (section 8).
- **First round, 2026-11-05:** P0, P1, P2, P2b, P3 and P5 on one Windows
  machine. Verdicts: P0, P1, P2 and P5 GO; P2b and P3 PARTIAL; no STOP.
- **Second round, the same evening:** P2b and P3 reworked (both now GO), and P4,
  P6, P7, P8 and the headed Local Network Access check run. P4 is PARTIAL (h5ad
  is correct but slower than h5py); every other POC is GO; no STOP. The O2
  records followed on 2026-11-06: the protocol (GO) and the open and tour
  times against the desktop route on the same node (GO: open 0.50×,
  navigation 1.03×). ERIS, GCP and Safari remain. See
  [POC results](#poc-results) and
  [the campaign report](bench/browser_node/BROWSER_NODE_POC_2026-11-05.md).
- **P9, 2026-11-06: the AI layer in the browser.** The gating session's
  Python ran unchanged in Pyodide in a worker beside the viewer.
  - **Met:** the same packets, answers, states and gates as CPython, at
    0.9–1.2× its time, with no main-thread long tasks added. Eight small
    substitutions stand in for what Pyodide lacks.
  - **Missed:** startup. Ready 6.2–7.1 s after a visit; a first visit over
    50 Mbps takes 13.8 s at best (bars: 3 s and 10 s).
  - **Verdict:** PARTIAL, not STOP, so the small download beside the browser
    is not triggered. See [P9.md](bench/browser_node/P9.md).
- **Gate:** no product change until the ladder reports GO on P1, P3 and P5. All
  three are GO. The first product commit is the Phase 0 seams (section 7). One
  product fix came out of the POCs already: `frag.glsl`'s 16-bit rebuild
  (question 11).

## How to read this document

Every claim carries one of four evidence tags:

| Tag | Meaning |
|---|---|
| **Measured** | a number already measured and recorded: in this repository (a benchmark report, `SKILL.md`, a test) or in the manuscript's use-case benchmarks |
| **Code** | read from the source at the commit above; file:line citations point at it |
| **Platform** | browser or library documentation, as of November 2026 |
| **Predicted** | an estimate made here, which a named proof of concept (POC) confirms or refutes |

The POC records themselves use the benchmark convention of
[REMOTE_VS_NODE_2026-10-26.md](bench/REMOTE_VS_NODE_2026-10-26.md)
(Measured / Design / Proposed).

Short paths: `views/`, `services/` and `workers/` are under
`plexora/client/src/js/`; `shaders/` is under `plexora/client/src/`;
`templates/` is under `plexora/client/`; `providers/` is
`plexora/server/providers/`; `node/api.py` and `node/app.py` are under
`plexora/server/`; `server/...` is under `plexora/`. A bare client file name
such as `glInit.js` lives in one of those folders; `main.js` sits at
`plexora/client/src/js/` itself.

Terms:

- A **node** is `plexora node serve`: Plexora with the viewer off, serving
  `/node/v1`.
- The **primary** is the process that holds the project (config, SQLite, ROIs,
  gates) and serves the viewer. Today it is always a Python server.
- A **local node** reads files on the user's own machine. In the mode proposed
  here it can be the browser.

## Context

The question: can Plexora gain one more deployment mode, served as static files
(for example from plexoraapp.com), in which the browser itself is the local
node? In that mode the browser reads local files, renders and caches. No Python
server runs on the user's machine, and no central server renders anyone's
pixels. Remote data nodes (O2, ERIS, GCP and later sites) stay first-class
peers.

**This mode is additive.** The desktop app, the CLI, Jupyter and the Python
server all stay. Remote data nodes stay first-class. Nothing here replaces a
mode that exists today. Where the browser mode cannot do something, the answer
is one of the modes that already exist.

This document is an investigation, a recommendation and a falsifiable POC plan.
It contains no implementation. The findings come from reading the server,
client and data layers, the repository's measured-performance notes, and
current browser-platform documentation.

## The short answer

- **Feasible for the core viewer, and closer than it looks.** Colour, contrast,
  gate preview, label outlines, ROI (region of interest) hit-tests, colour-by
  tables and 3-D ray casting already run in the browser. The browser already
  fetches tiles straight from a remote node over CORS (Cross-Origin Resource
  Sharing). The server's dominant per-tile cost is quantize plus WebP encode.
  That cost disappears when the browser decodes the TIFF tile itself and hands
  the raw uint16 array to the existing shader.
- **Not a rewrite, but not free.** Three seams must be built:
  1. The client assumes a same-origin Python server. That assumption is spread
     over ~150 classic scripts and Jinja-rendered pages.
  2. A browser implementation of the local-node interface (the three provider
     Protocols), over geotiff.js, zarrita, h5wasm and typed arrays. Its store
     for derived files is OPFS (Origin Private File System).
  3. Small, backwards-compatible additions to the node protocol, so that a
     hosted page can be a node's primary: origin grant, token pairing, https
     termination.
- **The hard limits are networking and memory, not rendering.**
  - Chrome 142+ puts a Local Network Access (LNA) permission prompt in front of
    a hosted https page that reaches a tunnelled node at 127.0.0.1.
  - Safari 18.0–18.3 blocks that request outright.
  - Nodes speak plain HTTP.
  - A browser cannot open an SSH tunnel.
  - Wasm32 (32-bit WebAssembly) has a 4 GB ceiling and OPFS has quotas. Both
    bound local tables and conversions.
- **Recommendation: a browser-first hybrid with a shared computation core.** It
  is built behind seams that the Python modes also use. The node protocol is the
  common abstraction between Python-backed and browser-backed local nodes.
- **What "fully browser-native" covers.** "Fully" means no Python anywhere on
  the user's machine. That is realistic for local pyramidal OME-TIFF, a prepared
  mask and CSV/Parquet/h5ad tables up to a few million cells. It is realistic
  for remote nodes reachable over https. It is not realistic in v1 for DICOM
  WSI (whole-slide imaging), MRXS, LZ4-compressed Imaris, AI gating and QC
  (quality control), Cellpose, surfaces, Visium HD stores, or writes into the
  user's source files.
- **Measured since (2026-11-05, first POC round).** The core holds up:
  - the product's rendering modules run unmodified from a static page;
  - a 66.7 GB local OME-TIFF opens in about 0.1 s, every tile is byte-identical
    to the HD path, and tours reach full 2.2–2.6× sooner than from a local
    Python node;
  - label masks, a 1.26 M-cell table and a whole-slide flat mask are handled
    in the tab, with every answer identical to Python's;
  - a static page uses an existing node with the same bytes the desktop
    receives.

  The first round's miss, a first-ever CSV open at 5–10× polars, is gone in the
  second: plain workers parse the CSV, without DuckDB, in under 3× polars.
  Flat masks and flat images are made into pyramids in the tab in 0.1–0.9×
  Python's time, identical to Python's. On the same image and tour as the
  product's own page, the browser node reaches full 2.4–3.3× sooner at every
  step that needs new tiles, and ties on the rest. h5ad reads are correct but
  2–13× slower than h5py. Two planned node-protocol
  additions turn out to be unnecessary (section 6.5).

## Decisions (2026-11-05)

- **Browser floor: Chromium first.** Chrome and Edge are the timing targets.
  Firefox and Safari must be functional; they are checked for function, not
  timed.
- **Remote connectivity costs BioCognia nothing per user.** No relay and no SSH
  bridge runs on BioCognia infrastructure. Such a service bills per request and
  per CPU-millisecond, so its cost grows with users. A relay would also pass
  users' tiles through a third party.
- **Connector paths.** The Connect button on plexoraapp.com is served by four
  paths, all on one pairing protocol (section 6.5):
  1. the minimal connector, or the desktop app when it is installed;
  2. the node serving the app, through Open OnDemand (OOD);
  3. a lab gateway;
  4. direct to the tunnel.

## 1. Current-state architecture and dependency map

Code unless noted.

### 1.1 Process model

- The app is Flask + Jinja, built once per process
  (`plexora/__init__.py:146,620`). Waitress serves it with 8–32 threads
  (`plexora/_resources.py:42-50`).
- There is no FastAPI and no WebSocket. There is one SSE (server-sent events)
  route, for AI chat events, and one long-poll, for the agent bridge. Background
  work runs on daemon threads.
- **One loaded project per process.** It is held in module globals
  (`plexora/server/models/data_model.py:44-87`) under `load_lock`.
- Entry points:
  - the `plexora` CLI (`plexora/cli.py:3311-3624`);
  - `--desktop`: the Tauri shell spawns a bundled CPython and opens a WebView on
    the server's URL (`desktop/src-tauri/src/server.rs:169-180`,
    `windows.rs:70-75`). The shell has no frontend of its own;
  - the `plexora-server` notebook sidecar;
  - `run.py` for Docker.
- Auth: a launch token is swapped for an HttpOnly, SameSite=Lax cookie
  (`plexora/__init__.py:235-286`). The main server sends **no CORS headers** and
  refuses cross-site writes (`plexora/_guard.py:107-156`). A page on another
  origin cannot use today's local server as a backend.

### 1.2 Client

- **Rendering.** An OpenSeadragon 6 (OSD) canvas drawer. One offscreen WebGL2
  context colours one tile of one channel per draw
  (`plexora/client/src/js/views/tileColorize.js:72-81`, `glInit.js:115-161`,
  `shaders/frag.glsl:477-531`). Channels composite with the canvas `lighter`
  operation as cover/paint pairs (`viewerManager.js:94-108`). WebGL2 is a hard
  floor. WebGPU is unused.
- **Tile wire format.**
  - HD, the default for local projects: single-channel 16-bit PNG with stored
    deflate. It is decoded in 2–4 workers with `DecompressionStream`
    (`workers/tileDecoder.js:39-86`).
  - 8-bit WebP when the image or the server is remote.
  - Label tiles: RGBA8 PNG carrying uint32 ids, decoded on the main thread with
    UPNG (`tileDecode.js:113-133`).
- **Already in the browser:**
  - per-channel colour, contrast and asinh;
  - the GPU label layer with per-id gate and colour tables (`views/labelGpu.js`,
    `label.frag.glsl`; capped at 2^24 ids);
  - gate preview from cached float32 columns (`services/numericData.js:1-58`,
    `imageViewer.js` `evaluateGateLocally`);
  - ROI point-in-polygon and category counts (Cell Explorer);
  - colour-by lookup tables and the mini-map composite;
  - transcript points in WebGL2;
  - video encoding via WebCodecs;
  - 3-D ray casting (`volumeRenderer.js`; 6 channels, 1 GB GPU budget).
- **Still on the server:** nearest-cell pick (`/get_nearest_cell`); histograms
  and stats (`/get_database_description`); GMM (Gaussian mixture model)
  auto-contrast and auto-gate; centroid tiles; ROI-to-cells mapping; every QC
  analysis; magic select (ONNX on the server); Figure Builder panel renders and
  PDF; 3-D levels, bricks and meshes.
- **Delivery.** `dist/vendor_bundle.js` (webpack, one entry) plus ~150 classic
  `<script>` tags with `?v=` tags in `templates/base.html` and `index.html`.
  They depend on globals and load order. Pages, tool panels and
  `window.flaskVariables` are rendered per request:
  - `toolLoader.js:868-935` fetches Jinja fragments for tool panels;
  - `template_data` (`plexora/server/routes/page_routes.py:170-234`) supplies
    the datasource list, plugin nav, tool scripts, styles and panels, the
    licence `{plan,state,paid,entitlements}`, `client_node` and
    `server_is_remote`. The viewer route adds `image_kind` and
    `image_streamed`;
  - every API call is a relative same-origin URL built by `plexoraUrl()`
    (`services/passVariablesToFrontend.js:5-54`).
- **Files.** The browser never reads a local file, except a chat image
  attachment. Paths go to Python (`/list_dir`, `/browse_path`). Dropped files up
  to 64 MB are uploaded (`views/importSample.js:987-1063`). The desktop shell
  hands over paths, not bytes. The client uses no File System Access API, no
  OPFS, no IndexedDB, no service worker, no Wasm and no SharedArrayBuffer.
- **The cell table in the browser.** `[id,x,y]` as raw uint32 for every row
  (`/get_all_cells/integer`). Float32 columns on demand: 8 cached, ~5 MB each at
  1.26 M cells. QC not-called bytes. Histograms as JSON. No Arrow or Parquet.

### 1.3 Node protocol: the asset this design builds on

- A node is Plexora with the viewer off: a separate Flask app mounting
  `/node/v1` only (`plexora/server/node/app.py:88-118`). It holds no project
  state.
- Routes (`node/api.py`):
  - handshake `/hello` (api_version 1; capabilities are the registered table
    operations; resources) and `/health`;
  - image: `geometry`, `ome_metadata`, `tile/<c>/<l>/<t>`, `level`, `block`,
    `overview`, `stats`, `gmm`, `quantization`, `region`;
  - mask: `tile`, `region`, `block`;
  - table: `load`, `inspect`, `describe`, `all_cells`, `geometry`, `columns`,
    `metadata_column`, `rows`, `op/<op>`, `stream/<op>`;
  - `/batch`, the dynamic-node file routes, and `/volume/*`.
- **Auth and CORS.** The token travels as `X-Plexora-Node-Token` or `?t=`.
  **CORS is already implemented**, for an exact-origin allowlist fixed at
  startup (`--allow-origin`, `api.py:105-148`). It exposes the custom headers
  and sets a preflight max-age of 86400 s.
- **Rendering happens on the node:** read, quantize, encode
  (`api.py:1632-1713`). The primary forwards the bytes unchanged
  (`data_model.py:3800-3816`). The node caches tiles in an LRU, persists
  quantization windows and `.npy` overviews, warms itself on start, and uses a
  readers-writer lock ([REMOTE_DATA_PERFORMANCE.md](REMOTE_DATA_PERFORMANCE.md),
  section "Implemented").
- **The browser already goes direct for image and mask tiles.**
  `/resource_routing` hands it the node address and token.
  `services/resourceRouting.js` probes `/health` (1.5 s timeout) and remembers
  the verdict in sessionStorage. Tables, 3-D and files still go through the
  primary.
- **Wire formats a browser client would need:**
  - `providers/wire.py` frames: uint32 length, JSON header, raw array bytes;
  - Arrow IPC for the compact id/x/y copy (`providers/node.py:166-268`);
  - the batch body (`server/tile_batch.py:21-25`);
  - gzip.
- **What stays on the primary by design** (`providers/base.py:11-29`):
  `config.json`; the per-project SQLite with plugin tables
  (`plexora/api/store.py`); ROIs; gates; figures; the read spec pushed with
  `POST /table/<id>/load`; the compact id/x/y copy; the spatial index.
- **Registered table operations.** The operations that must run where the file
  is are few and named (`providers/operations.py`): `roi.map_to_cells`,
  `roi.save_anndata`, `roi.save_spatialdata`, `gating.save_gates`,
  `gating.load_gates`, `gating.gmm`, `qc.write_cells`, `table.correction`, plus
  autogate's.
- **Connectivity.**
  - Nodes are plain HTTP: Waitress, no TLS, no WebSocket (`tile_batch.py:6-9`).
  - They are reached through `ssh -L` tunnels that `plexora/connect.py` builds
    with the system `ssh`. Duo and password prompts are relayed through
    `SSH_ASKPASS`.
  - The token reaches the primary only through the SSH stdout announce line.
  - Open OnDemand exposes a node at `https://<portal>/rnode/<host>/<port>/`
    behind the portal's session cookie (`browser_endpoint`,
    `server/models/nodes.py:19-26`).

### 1.4 Data pathways

| Input | Today | Reader |
|---|---|---|
| Pyramidal OME-TIFF / TIFF / QPTIFF | read in place (tifffile `aszarr`; `providers/local.py:542-587`) | tifffile + imagecodecs (zlib, lzw, zstd, webp, jpeg, jpeg2k, lerc) |
| Flat TIFF, longest side > 4096 px | converted once to a SubIFD (sub-Image File Directory) pyramid beside the source, zlib level 1, 1024 tiles (`server/utils/image_pyramid.py`) | tifffile |
| OME-Zarr / NGFF / SpatialData images | read in place; coarse levels derived into `image_pyramid.zarr` only if the coarsest level > 2048 px (`server/utils/ome_zarr.py`) | zarr v3 + numcodecs |
| SVS / NDPI / SCN / BIF | read in place as TIFF, virtual halving chain (`server/utils/brightfield.py`) | tifffile |
| MRXS | OpenSlide only (`[wsi]` extra) | OpenSlide (C) |
| DICOM WSI | wsidicom reassembles instances (`server/utils/dicom_wsi.py`) | wsidicom |
| https / s3 / gs / az / sftp | the primary fetches byte ranges through an on-disk chunk cache and renders locally (`server/utils/remote_store.py`) | fsspec + tifffile/zarr |
| Imaris .ims | h5py + hdf5plugin, chunk index built once | h5py |
| PNG / JPEG | converted to a tiled RGB TIFF | tifffile |
| Masks | converted to `<stem>.labels.pyramid.ome.tiff`, nearest-neighbour levels, up to 8 GB read whole (`server/utils/segmentation_pyramid.py`); polygon masks (Xenium parquet, Space Ranger GeoJSON) rasterised with OpenCV (`boundary_mask.py`) | tifffile, cv2 |
| CSV / TSV / Parquet | read whole with polars, copied into the project folder (`adapters/flat_table.py`, `import_routes.py:945-961`) | polars |
| .h5ad | obs/var read; X and layers in ~64 MB row blocks; wide tables (> 1024 features) read per column (`adapters/anndata_adapter.py`) | h5py, anndata |
| SpatialData | `tables/<name>` via zarr; ≤ 2 GB dense when remote | zarr |

Queries are polars + numpy:

- describe is per-column stats plus 50-bin histograms;
- range masks are `low < v <= high`;
- nearest cell is a scipy cKDTree, pickled to `derived/spatial_index_v1`;
- centroid tiles are on disk, with stride decimation;
- ROI mapping is a shapely STRtree;
- auto gate and auto contrast are an sklearn 3-component GMM on ≤ 100 k
  samples.

**There is no DuckDB, no dask and no matplotlib** in `plexora/`.

### 1.5 Native dependency map

What a browser build must replace or drop:

| Class | Members | In a browser build |
|---|---|---|
| Pure numpy/polars | tile read + LUT (lookup table), describe and histograms, range masks, centroid tiles, bin and transcript tiles, Cell Explorer values, DNA retention, projections, quantization windows | port to typed arrays or Wasm |
| Compiled, but small to port | cKDTree (nearest), STRtree (ROI), 3-component GMM + normal crossover (auto gate and contrast), label pyramid subsampling, polygon rasterisation | port (section 3) |
| Heavy native | QC scan, blur, artifacts and registration (scipy.ndimage, skimage, cv2, numba); the autogate engine (scipy, sklearn, numba, the LLM gateway, PIL, reportlab); the classifier (sklearn); surfaces (skimage marching cubes, numba surface nets); tracking; Cellpose and SAM2 (torch); DICOM (wsidicom); MRXS (OpenSlide); ffmpeg | keep in Python |
| Already has a browser equivalent | magic select with MobileSAM (onnxruntime-web); vector PDF (jsPDF is already a client dependency); video (WebCodecs, mediabunny) | use it |

### 1.6 What crosses the network today

Code; `DEPLOYMENT.md:1522-1534`.

- **Table node:** ids and coordinates once (~20 bytes a cell), then whole
  columns as a tool asks.
- **Image node:** encoded tiles, plus a few hundred floats of statistics per
  channel.
- **Work that must not move:** the ROI spatial join, annotation writes, gate
  writes into `uns`, per-channel mixture fits, CSV export.

## 2. Feasibility matrix

| Verdict | Meaning |
|---|---|
| **B** | browser-native: no Python, no conversion beyond caching |
| **C** | conversion required: in the browser, cached in OPFS; or prepared beforehand in the CLI or desktop app |
| **P** | local Python fallback: an optional local node |
| **N** | remote-node processing |
| **X** | infeasible in a browser today |

Phase is when the roadmap in section 7 delivers it.

### 2.1 Image and mask formats (local files)

| Format | Verdict | How | Phase |
|---|---|---|---|
| Pyramidal OME-TIFF: deflate, LZW, zstd, packbits, raw | **B** | geotiff.js `fromBlob` plus the MIT-licensed OpenSeadragon `GeoTIFFTileSource` (SubIFD layout hint), feeding raw uint16 to the existing colorize shader | 1 |
| Pyramidal TIFF with JPEG tiles (SVS/NDPI brightfield) | **B** | geotiff.js JPEG decoder; the existing brightfield path (`tileFormat 24`) | 2 |
| JPEG 2000 tiles | **X** → P/N | geotiff.js has no JPEG 2000 decoder | none |
| Flat TIFF > 4096 px | **C** (in the browser) | the pyramid is built by band workers into OPFS with `image_pyramid.reduce2`'s arithmetic. **Measured (P7):** 2 channels × 52923 × 45381 (9.6 GB raw) in 0.1–0.9× Python's own time, depending on layout, identical tile for tile; level 0 is copied only from a stripped file | 2 |
| OME-Zarr directory (local) | **B** (Chromium) / **C** (Firefox, Safari) | zarrita.js + numcodecs over a `FileSystemDirectoryHandle`. Firefox and Safari get a `webkitdirectory` file list, which is slow for 100 k chunk files | 2 |
| OME-Zarr / TIFF / DICOM at https, s3 or gs | **B only if the bucket sends CORS**, else **N** or a gateway | geotiff.js / zarrita over HTTP range requests. Python today needs no CORS, so this is a regression to document (Platform) | 2 |
| DICOM WSI (local) | **X** in v1 → P/N | no local-file DICOM WSI assembler exists in JS; it would be new work (dcmjs parsing + frame decoding) | 4+ |
| MRXS | **X** → P/N | OpenSlide only | none |
| Imaris .ims (HDF5), gzip | **C** / B | h5wasm over WORKERFS; 3-D needs the brick store (section 2.3) | 3 |
| Imaris .ims, LZ4 | **P/N** (was **X**) | h5wasm-plugins 0.3.0 now ships an LZ4 filter (Platform; not tried on an .ims). Its lzf filter aborts on variable-length strings (Measured, P4), so each filter needs its own test before it is relied on | 4 |
| PNG / JPEG quick view | **B** | canvas | 1 |
| Xenium `morphology_focus/` | **B** | per-channel TIFFs | 2 |
| Prepared label pyramid (`*.labels.pyramid.ome.tiff`, OME-Zarr labels) | **B** | geotiff.js / zarrita → uint32 straight into `label.frag.glsl`. No 24-bit wire limit; the GPU cap of 2^24 ids stays | 1 |
| Flat label mask | **C** (in the browser) | nearest-neighbour levels built by band workers into OPFS. **Measured (P2b):** a whole-slide 9.6 GB-raw mask in about half Python's time, identical to Python's pyramid; no size threshold was reached | 2 |
| Polygon masks (Xenium parquet, GeoJSON) | **C** | rasterise on OffscreenCanvas or WebGL in a worker (Predicted: feasible at 838 k polygons; to measure) | 3 |
| 3-D label volumes | **C** / N | | 4 |

### 2.2 Tables

| Format / query | Verdict | How | Phase |
|---|---|---|---|
| CSV / TSV | **C** (once) | parsed by plain workers (V8's `Number()`, correctly rounded) into a raw float64 column store in OPFS. **Measured (P3):** 1.26 M × 29 drawable in under 3× polars' read, every column bit-identical to DuckDB's parse; DuckDB-Wasm remains the fallback for quoted fields | 1 |
| Parquet / Arrow | **B** | hyparquet (JavaScript, column reads, GeoParquet). DuckDB-Wasm downloads its Parquet reader from `extensions.duckdb.org` on first use (Measured, P4), so it is not the default | 1 |
| .h5ad (obs, var, obsm, dense X, CSR/CSC, gzip/lzf/zstd/blosc) | **B** for reads, **C** for speed | h5wasm in a worker over WORKERFS. **Measured (P4):** every read identical to h5py, at 2–13× its time; lzf-compressed strings unsupported. One pass copies X into the column store | 2 |
| AnnData-zarr, SpatialData `tables/<name>` | **B** | zarrita (zarr v3, sharding, zstd). **Measured (P4):** spatialdata 0.8's store read identical to zarr-python | 2 |
| SpatialData shapes and points (parquet, WKB geometry) | **B** | hyparquet; WKB bytes or GeoJSON. **Measured (P4)** identical to pyarrow | 3 |
| describe + 50-bin histograms | **B** | typed-array kernels in a worker, or DuckDB SQL | 1 |
| Range gate, gate count | **B** (already) | `numericData.js` + `labelGpu.js` | 1 |
| Nearest cell / hover pick | **B** | kdbush over the `[id,x,y]` already in memory; also a client-side label-tile lookup | 1 |
| Centroid tiles | **B** | built from the in-memory `[id,x,y]` | 1 |
| ROI → cells mapping | **B** | flatbush + point-in-polygon in a worker, with the same rule as `roi/server/mapping.py` | 2 |
| Auto gate / auto contrast GMM | **B** | expectation-maximisation (EM) in JS on ≤ 100 k samples. Parity test against sklearn with a tolerance; the fit is already non-deterministic across sessions (Measured) | 2 |
| Writes into the user's source file (`uns` gates, obs columns, SpatialData shapes) | **N** / P, or **export** | never in place from the browser. Write a sidecar (`gates.json`, `annotations.parquet`), or hand the operation to a node that holds the file | 2 |

CSR/CSC are compressed sparse row and column layouts.

### 2.3 Features and plugins

| Feature | Verdict | Notes | Phase |
|---|---|---|---|
| Viewer: channels, contrast, colour, HD, mini-map, layers | **B** | unchanged shader and OSD pipeline | 1 |
| Segmentation overlay, gate preview, colour-by | **B** | already on the GPU | 1 |
| Gating (manual thresholds, store, CSV export) | **B** | store → IndexedDB; export via the save picker or a download | 1 |
| Cell Explorer | **B** | pure data | 1 |
| ROI (draw, edit, map, GeoJSON) | **B** | exports to the source file → N/P | 2 |
| Figure Builder | **B** | re-render with the same shader offscreen (canvas limits ~16384²); jsPDF for PDF; store → IndexedDB | 3 |
| Video Studio | **B** | the WebCodecs path exists; ffmpeg fallback → ffmpeg.wasm or N | 3 |
| Magic select (MobileSAM) | **B** | onnxruntime-web, with the WebGPU execution provider where present | 3 |
| QC checks (blur, artifacts, registration, segmentation QC, DNA retention) | **P/N** | heavy scipy/cv2/numba. DNA retention and blur are portable later | 4 |
| AI gating, AI QC, chat (Pro) | **N** + gateway | the engine is Python + spatialagents + the gateway. It runs on a node or in local Python; the browser hosts the packet UI | 4 |
| 3-D volume view | **C** / N | levels for small stacks in the browser; building the brick store is heavy → N or desktop-prepared | 4 |
| Transcripts, Visium HD (Pro) | **C** / N | the one-time stores (Visium HD ~6 min, 8 GB on disk, Measured) are plausible in DuckDB-Wasm + OPFS but heavy; N first | 4 |
| Surfaces, 3-D objects, tracking, Cellpose | **N** / P | | none |
| Import wizard, dataset builder (folder scans) | **B** for picked local folders; N for node folders | | 2 |
| Remote data nodes (image, mask, table) | **N**, first-class | direct routing for everything, not just tiles; needs the protocol additions of section 6.5 | 1–2 |
| SSH tunnel creation, `plexora connect`, GCP VM rental | **X** in a page → helper | the browser cannot spawn `ssh`. The connector or the desktop app makes the tunnel (a connectivity helper, not a compute server), or an https gateway (OOD, a lab gateway) replaces it. No BioCognia-run relay (section 6.5) | 1 (connector, OOD), 3 (lab gateway) |
| MCP (Model Context Protocol) / external agents | P/N | they attach to a node or to local Python. Viewer control needs a relay the browser can poll, not a BioCognia-run one (Decisions) | 4 |
| Licensing (Pro gates) | service | account.biocognia.com device certificate. Gates move from `guards.check_capability` to signed entitlements the page verifies. A node still enforces its own Pro operations (HMAC proof) | 1 |
| Telemetry | service | the page posts directly to analytics.plexoraapp.com; today it posts to the local server | 1 |

### 2.4 Browser support matrix (Platform)

| Capability | Chromium | Firefox | Safari |
|---|---|---|---|
| `showOpenFilePicker` and directory handles, persisted across sessions | yes | no (`<input type=file>` only) | no |
| OPFS quota | ~60 % of disk. **Measured (Chrome 156):** `estimate().quota` reports usage + 10 GiB, not the limit; 14.2 GB wrote without error | min(10 %, 10 GiB); 50 % if `persist()` is granted | ~60 %; **script storage purged after 7 days without interaction** |
| Wasm Memory64 (> 4 GB) | 133+ | 134+ | no |
| WebGPU | 113+ | 141+ (Windows), 145–147 (macOS) | 26 |
| COEP (Cross-Origin-Embedder-Policy) `credentialless`: threads without breaking cross-origin tiles | 113+ | 128+ | no |
| https page → `http://127.0.0.1` node | LNA prompt (142+); `targetAddressSpace:"loopback"` required | allowed | blocked in 18.0–18.3, allowed from 18.4 |

## 3. Technology evaluation

Platform: library and browser documentation, November 2026.

| Need | Chosen | Rejected or held back |
|---|---|---|
| TIFF / OME-TIFF tiles | geotiff.js + GeoTIFFTileSource | Viv/deck.gl; tifffile under Pyodide |
| Zarr: OME-Zarr, SpatialData, AnnData-zarr | zarrita.js + numcodecs.js | none |
| HDF5: .h5ad, gzip Imaris | h5wasm (+ h5wasm-plugins) | tsfive; Pyodide + h5py |
| CSV ingest | plain workers + V8's `Number()` (**Measured**, P3) | DuckDB-Wasm, kept as the fallback for quoted CSVs |
| Parquet reads | hyparquet (**Measured**, P4) | DuckDB-Wasm, loaded on demand for SQL only |
| In-browser column format | raw float64 columns in OPFS (**Measured**, P3) | Parquet (8 s to encode at 1.26 M × 29); parquet-wasm |
| Flat TIFF → pyramid | band workers + `CompressionStream` (**Measured**, P2b, P7) | one worker; a JS deflate at level 1 (no faster than the native one) |
| Nearest cell, ROI mapping | kdbush, flatbush | none |
| Auto gate / contrast | a JS 3-component EM GMM | none |
| Magic select | onnxruntime-web, WebGPU execution provider | none |
| Python in the browser | none as the core | Pyodide as the architecture |
| Renderer | WebGL2, unchanged | WebGPU as a dependency |
| Parallelism | Workers + message passing | cross-origin isolation in v1 |
| Hosting | Cloudflare Pages on plexoraapp.com | none |

**geotiff.js** provides `fromBlob`, lazy IFD (Image File Directory) parsing, a
worker `Pool`, and decoders for raw, packbits, LZW, deflate, JPEG, LERC and
zstd. WebP goes through the browser. It has no JPEG 2000.
**GeoTIFFTileSource** (MIT) is an OpenSeadragon TileSource over geotiff.js. It
takes a local `File` and selects SubIFD pyramids through a layout hint. It is
the shortest path to P1. The risk: it serves RGBA canvases, and Plexora needs
the raw array, so the tile source is adapted to return typed arrays into
`tile._array`. **Chosen** over Viv/deck.gl, because a second renderer beside
OSD would split the pipeline. Also chosen over porting tifffile to Pyodide.

**zarrita.js + numcodecs.js** read zarr v2 and v3, with sharding, and Wasm
codecs for blosc, zstd, gzip, zlib and lz4. Stores exist for fetch, Map and
Zip. A Blob or file-handle store is a small adapter. **Chosen** for OME-Zarr,
SpatialData and AnnData-zarr.

**h5wasm** is libhdf5 compiled to Wasm. WORKERFS mounts a `File` for random
access without loading it, and slicing happens inside libhdf5.
`h5wasm-plugins` adds blosc, bzip2, lzf and zstd, and since 0.3.0 LZ4. It
runs in a worker; there is an IIFE build for Firefox workers. **Chosen** for
.h5ad and gzip Imaris. Rejected: tsfive (read-only pure TypeScript, less
mature) and Pyodide + h5py (below). **Measured (P4):** every read identical to
h5py, at 2–13× h5py's time; its lzf filter aborts on variable-length strings.

**DuckDB-Wasm** does CSV/Parquet/Arrow ingest and SQL. It has
`registerFileHandle` on a `File` and OPFS-backed databases since 1.32. It has
the wasm32 4 GB ceiling and is single-threaded unless the page is
cross-origin isolated. It was chosen for ingest. **Measured since, it is not
the right default:**

- one instance parses the 1.26 M-row table in 2.6 s, and splitting the file
  over several instances only trades their start-up (about 0.4 s each, a few
  hundred MB of heap each) for parse time (P3);
- plain workers calling V8's `Number()`, which is correctly rounded, parse the
  same file several times faster, bit-identical to DuckDB's parse in all 29
  columns (P3b);
- its Parquet reader is an extension it downloads from `extensions.duckdb.org`
  on first use (P4): a third-party request from a page that should work
  offline, unless Plexora serves the extension itself.

It stays as the fallback for CSVs with quoted fields and for ad-hoc SQL, loaded
on demand. **hyparquet** (JavaScript, no dependencies) reads Parquet and
GeoParquet with no network, identical to pyarrow (P4).

**Apache Arrow JS.** The node already speaks Arrow IPC for the compact table
copy, so Arrow is how node columns arrive. In the browser node itself a column
is a raw float64 file in OPFS (P3); no parquet-wasm is needed.

**kdbush and flatbush** handle nearest-cell and ROI mapping. A JS 3-component
GMM fitted by EM replaces sklearn for auto gate and auto contrast.

**onnxruntime-web**, with its WebGPU execution provider, runs MobileSAM magic
select. ONNX Runtime's own numbers show a ~19× encoder speed-up on WebGPU over
Wasm.

**Pyodide is rejected as the core.**

- scikit-image and pandas exist for it.
- polars was present in the 0.27 package lists but is not a stable Pyodide
  package.
- numba has no Wasm build (no LLVM JIT).
- scipy and sklearn load tens of MB.
- The Python server's value is its tifffile/zarr/h5py I/O over the filesystem.
  Pyodide cannot do that over a File handle without copying.

Pyodide stays a possible later vehicle for porting one specific numpy-only
routine. It is not the architecture.

**WebGL2 stays the renderer.** WebGPU compute is an optimisation for
histograms, GMM sampling and label pyramids, once it is on every supported
browser. It is not a dependency.

**Cross-origin isolation is not taken in v1.** Isolation means COOP
(Cross-Origin-Opener-Policy) plus COEP. It would unlock SharedArrayBuffer, and
with it multithreaded DuckDB and ffmpeg.wasm. The costs:

- every node response would need a CORP (Cross-Origin-Resource-Policy)
  header. **Measured since (P5 d): not so.** The viewer's reads of a node are
  CORS reads, and they load under `require-corp` without CORP; only a no-cors
  `<img>` is blocked;
- Safari lacks `credentialless`;
- an isolated page cannot be embedded in a notebook iframe.

Parallelism comes from Workers and message passing instead.

**Hosting.** Static files on Cloudflare Pages behind the existing
plexoraapp.com zone, where the telemetry Worker already lives. A service worker
handles offline/PWA (progressive web app) use and asset versioning, for assets
only; section 6.2 rejects one as the server. `_headers` sets the CSP (Content
Security Policy).

## 4. Network and hosting constraints

Platform + Code.

### 4.1 Local Network Access in Chrome 142+

- A public https page's `fetch` to 127.0.0.1, or to an RFC 1918 private
  address, prompts the user once per origin.
- The fetch must declare `targetAddressSpace: "loopback"` (or `"local"`) to be
  exempt from mixed-content blocking.
- No server header or preflight is required.
- An iframe needs `allow="local-network-access"`.
- Enterprise policy can pre-grant the permission.
- Later Chrome versions split the loopback permission from the local-network
  one.

Consequence: a tunnelled node at `http://127.0.0.1:<port>` is usable from
plexoraapp.com after one prompt. `resourceRouting.js`'s probe must pass the
option. It must treat a denied permission as a dead end with no proxy to fall
back on, and ask the user.

**Measured on Chrome 156 (P5 b).** The page was marked public with
`--ip-address-space-overrides`, and the node was on loopback.
- Without the permission, every request is refused with "Permission was
  denied for this request to access the `loopback` address space".
- The permission that matters is `loopback-network`, separate from
  `local-network`.
- Once it is granted, plain fetches and `targetAddressSpace: "loopback"` both
  succeed. That holds over https and over http, with **no** header from the
  server.
- `targetAddressSpace: "local"` for a loopback address is refused.

**Measured headed (P5LNAH).** In a visible Chrome 156 window, with nothing
granted, the fetch is not refused: it is held while a bubble anchored to the
address bar asks "127.0.0.1:PORT wants to — Access other apps and services on
this device — Allow / Block" (screenshot in the P5 report). Headless Chrome
denies the same request at once, which is why the first round saw only
refusals. Granting the permission over CDP did not release the held request;
new requests then succeeded. The pairing page must therefore start its probe
only after telling the user what the prompt will say, and retry after it is
answered.

### 4.2 Safari

Safari blocked https → `http://127.0.0.1` outright in 18.0–18.3 (WebKit bug
279249) and restored it in 18.4. WebKit maintainers flagged it as a risk, so it
may tighten again. Safari therefore needs an https node endpoint, or the "node
serves the app" layout (4.3 a).

### 4.3 Nodes speak plain HTTP

Three answers were considered. All are compatible with api_version 1.

**(a) The node serves the static app itself.** The wheel already ships the
client. The browser is then same-origin with the node, through the tunnel or
the OOD `/rnode/` proxy: no CORS, no LNA prompt, no mixed content. This is the
zero-infrastructure path for HPC (high-performance computing) clusters.

**(b) An https gateway the site runs.** OOD, or a reverse proxy on a login
node, with the hosted page allowed as an origin.

**(c) A BioCognia-run connectivity relay. Rejected (2026-11-05), for cost and
privacy.** The design was an outbound connection from the node, an address
such as `https://<id>.relay.plexoraapp.com` for the browser, and end-to-end
token auth. It would give lightweight connectivity, but:

- a relay bills per request and per CPU-millisecond, so its cost grows with
  users, and remote connectivity must cost BioCognia nothing per user;
- encoded tiles would transit a third party, unless the relay were a TCP
  pass-through with the node terminating TLS.

So (a) and (b) are the https answers. With the connector and direct-to-tunnel
paths they make up the four paths of section 6.5.

### 4.4 The browser cannot open an SSH tunnel

`plexora connect` does srun, two-hop `ssh -J`, Duo via askpass, node launch and
the token announce. That job stays in a native helper: the CLI, the desktop app
or the minimal connector. In the hosted mode the helper is a connectivity tool,
not a compute server. A node must also learn to accept the hosted origin and to
hand its token to a browser without SSH stdout (section 6.5: pairing).

### 4.5 Buckets need CORS

The browser can read `s3://` and `gs://` images directly only if the bucket
sends CORS headers. The Python path does not need them. Checked 2026-11-05 with
an `Origin: https://plexoraapp.com` header:

- `s3://lin-2023-orion-crc` answers `Access-Control-Allow-Origin: *`, with
  `Range` allowed on preflight. The Orion fixture is readable from a hosted
  page.
- IDC's (Imaging Data Commons) `gs://idc-open-data` object endpoint answers
  range reads with **no** CORS header; only its JSON listing API sends one. The
  manuscript's use case 3 DICOM slide is therefore not readable from a hosted
  page. It falls back to a node or the Python helper.

Expect both outcomes among users' buckets.

### 4.6 Jupyter embedding

The hosted page embeds in Jupyter as an iframe on the hub's origin. Notebook
kernels already act as data nodes (`plexora/memory.py`), reachable only through
the hub proxy. The kernel-node path is therefore "node serves the app".

## 5. Performance and scalability assessment

### 5.1 Measured (repository)

- **Client frame cost** is already independent of channel count up to 15: pan
  median 8.3 ms (`SKILL.md`, "Performance: Measured Facts").
- **Server cost per 1024² tile:** zarr read 7.9 ms + LUT 1.1 ms + WebP encode
  ~21 ms. The HD path encodes in 1.5 ms (stored deflate); the browser decodes in
  a worker, 8.4 ms median during pan. All tile I/O is globally serialised by
  zarr/tifffile locks. Caching, not threads, is the lever.
- **Auto-level GMM:** 0.2–1.9 s per channel, 17 s for 19 channels,
  non-deterministic.
- **Remote node versus viewer-on-cluster.** Node + local viewer wins: tile
  repeat 1.0 ms proxied against 82 ms; per-channel metadata 0.5 ms against
  76 ms; viewport fill 0.6 s against 1.33 s. The reason is that the caches sit
  on the browser's side of the tunnel
  ([REMOTE_DATA_PERFORMANCE.md](REMOTE_DATA_PERFORMANCE.md)).
- **Node versus SFTP streaming** on a 640 GB Imaris file: node open 1.81 s
  cold, steps 1–3 s, 0.55 GB per cold tour. SFTP was 10–54× slower on cold
  first-visit steps and moved 15 GB per tour
  ([REMOTE_VS_NODE_2026-10-26.md](bench/REMOTE_VS_NODE_2026-10-26.md)).
- **Live gate drag** with the GPU label layer: 1088 → 25 ms per update
  (manuscript use case 2).
- **One-time builds:** Visium HD bin store ~6 min, 8 GB on disk; Orion mask
  pyramid ~60 s. The gs:// DICOM slide opens in 29 s cold (metadata) and 2.6 s
  warm (manuscript use case 3).

### 5.2 Predicted (to be measured by the POCs)

- **Local pyramidal OME-TIFF in the browser.** A 1024² deflate tile decodes in
  roughly 5–15 ms in a worker (pako-class inflate). That is the same order as
  today's HD path (8.4 ms), and it skips the server's 21 ms encode. Warm pan
  and zoom should match the local-Python build; cold tiles should beat it.
  Risk: first open must parse the IFDs of an 80-channel × 8-level file, ~700
  IFDs with SubIFDs. geotiff.js parses lazily, but the cold header walk is the
  number to measure. tifffile takes 6–25 s on a cold remote Orion header and
  1 s locally (Measured).
- **Quantization windows.** Today's 8-bit path needs a full-resolution maximum
  per channel (`data_model.py:3224-3261`). The browser uses the 16-bit path. It
  takes percentile windows from a coarse level at once and refines them in the
  background. No full-plane scan sits on the critical path.
- **Tables.** 1.26 M cells × 90 float32 columns is 450 MB on disk as Parquet;
  one column is ~5 MB. Converting a 0.8 GB CSV to Parquet in DuckDB-Wasm is
  Predicted at 10–60 s single-threaded, and must stay under the 4 GB working
  set. 5 M cells is today's "slow to gate" label
  (`import_proposal.py:1304`); it is the extreme fixture for P3.
- **Label pyramid build in the browser.** Nearest-neighbour subsampling is I/O
  bound. A 2 GB uint32 mask is Predicted at 1–3 min in a worker streaming to
  OPFS (34050×5797 is 0.8 GB). A whole-slide 47296×64246 mask (12 GB) is beyond
  a browser tab and must be prepared outside it.
- **Memory.** The page's own typed arrays are bounded by the renderer process,
  not by Wasm's 4 GB. But Safari and Firefox tabs are routinely killed above
  2–4 GB. The working set must stay streaming: tiles through OSD's LRU (already
  bounded), columns 8 deep, OPFS for everything derived.
- **Remote node from a hosted page.** Identical bytes and latency to today's
  direct routing: tiles 86–107 ms cold over a 65 ms tunnel
  ([REMOTE_DATA_PERFORMANCE.md](REMOTE_DATA_PERFORMANCE.md)), plus one LNA
  prompt. Tables would also go direct. That removes the primary's hop, but also
  its laptop-side caches. The browser must therefore cache the compact table
  copy and the stats in memory or OPFS, to keep the 0.5 ms metadata hits the
  node layout relies on.

**Measured since (2026-11-05).** The records are in
`bench/browser_node/`; the details are in [POC results](#poc-results).
- **OME-TIFF:** a tile takes 21 ms p50 and 31 ms p95 in 4 workers (native
  inflate 4.4 ms). The cold open of a 40-channel, 7-level file takes 73–107 ms:
  geotiff.js parses lazily.
- **Tables:** CSV → Parquet in DuckDB-Wasm took 10.5 s for 0.63 GB, inside
  the predicted range. Parquet encoding was the cost, so the design moved to a
  raw column store: 3.4 s to drawable, reopen in 31 ms.
- **Label pyramids:** a whole-slide flat mask (9.6 GB raw) was built in a tab
  in 58 s with a 1.09 GB peak. It is not "beyond a browser tab".
- **Memory:** a 1.26 M × 29 table peaked at 1.67 GB. The viewer's own heap at
  15 channels is 2.8 GB in both the browser and the node arm (OpenSeadragon's
  tile cache).

**Measured in the second round (2026-11-05, evening).**
- **Tables:** the CSV parsed by plain workers is drawable in about 1 s for
  1.26 M × 29 (under 3× polars), peaking near 1 GB, every column bit-identical
  to DuckDB's parse (P3, P3b).
- **Pyramids:** the whole-slide flat mask builds in about half Python's time
  with band workers, and a 9.6 GB-raw flat image in 0.1–0.9× Python's time
  depending on how it was written, every tile identical (P2b, P7).
- **AnnData:** h5wasm reads are correct and 2–13× slower than h5py (P4).
- **The product page:** on the same image and tour, the browser node reaches
  full 2.4–3.3× sooner than the local Python app at every step that needs new
  tiles, ties on the others, and peaks at about half its memory (P6).

### 5.3 Hard limits and the experiment that turns each into a threshold

| Limit | Value | Experiment |
|---|---|---|
| Wasm32 heap | 4 GB per module instance (DuckDB and h5wasm each) | P3: CSV sizes 0.2 / 0.8 / 3 GB; record the peak and the failure mode |
| OPFS quota | 60 % of disk (Chromium, Safari); 10 GiB (Firefox). Chrome's `estimate()` reports usage + 10 GiB, not this limit (Measured) | P2b, P3, P7: conversion output sizes; writing past the reported quota; Safari's 7-day purge → re-conversion cost |
| Per-tab memory | 2–4 GB in practice | P6: memory under 15 channels + mask + 8 columns; Chrome's `performance.measureUserAgentSpecificMemory` |
| GPU | label tile budget 160 MB, texture LRU 384 MB, volume 1 GB (`labelGpu.js`, `glInit.js`, `volumeRenderer.js`) | unchanged; reuse the bench3d memory probes |
| Ids | 2^24 on the GPU label path | unchanged (CPU fallback) |
| Workers | 2–4 decode workers today | P1: scale 4 / 8 / `hardwareConcurrency` |
| HTTP/1.1 connections | 6 per origin; a batch endpoint exists for nodes | unchanged; a File-backed source has no connection limit |

## 6. Recommended target architecture

### 6.1 One contract, two local nodes

```
                 ┌─────────────────────── static build (same files everywhere) ────────────────────────┐
                 │  plexoraapp.com (CDN)  ·  a node's /app/  ·  lab gateway  ·  desktop shell (later)  │
                 └──────────────────────────────┬──────────────────────────────────────────────────────┘
                                                │ browser
   ┌───────────────────────── PlexoraBackend (services/backend.js) ─────────────────────────┐
   │ fetch shim + route table      ProjectStore (OPFS/IDB)      NodeRegistry (paired nodes) │
   │        │                                                           │                   │
   │  BrowserPrimary (node/primary.js): the viewer-facing subset of data_model.py           │
   │   kd-tree · centroid tiles · gate masks · describe · requirements · guards             │
   │        │ NodeClient interface (node/nodeClient.js = providers/node.py twin)            │
   └────────┼───────────────────────────────┬─────────────────────────────┬─────────────────┘
            │ in-process MessagePort        │ fetch over https / tunnel   │ fetch over loopback
   BrowserNode worker                 Python data node                Python local node
   (geotiff.js, zarrita, h5wasm,      plexora node serve              plexora node serve --dynamic
    DuckDB-Wasm, OPFS caches)         on O2 / ERIS / GCP / lab        (fallback C for unsupported formats)
```

IDB is IndexedDB. CDN is the content delivery network that serves the static
build.

- **The node protocol (`/node/v1`, api_version 1) is the contract.**
  `BrowserNode` implements its operation names and wire shapes in a Worker. A
  Python node is reached by `nodeClient.js`, a twin of `providers/node.py` +
  `providers/http.py` + `providers/wire.py`. The browser primary cannot tell the
  two apart, exactly as `data_model.py` cannot tell `LocalTableProvider` from
  `NodeTableProvider`.
- **The primary logic exists twice.** What the viewer needs from
  `data_model.py` is Python for the desktop, CLI and Jupyter modes, and JS for
  the static build. That duplication is the price of rewriting neither side.
  It is held honest by **conformance vectors**: recorded from the Python node
  against the test fixtures (`tests/node_conformance/`) and replayed against the
  JS node (`tests/js/browser_node_conformance_probe.mjs`). A `Requires` parity
  test sits beside them.
- **Python modes are untouched.** In server mode the backend facade is a
  pass-through, and the Python server is simply the local node.

### 6.2 Client transport: a hybrid facade, no service worker

- `services/backend.js` installs one page-level `fetch` shim before anything
  else. This is the pattern `appStatus.js` already uses
  (`templates/base.html:36-39`). Same-origin requests whose path matches a
  generated route table go to in-page handlers (the browser primary and node).
  Everything else passes through. `fetch(plexoraUrl(path))` stays the universal
  call shape, so the ~300 call sites across core and nine plugins do not change.
- **Label (32-bit) and RGB (24-bit) tiles** move from OpenSeadragon's XHR loader
  onto the existing `views/tileFetch.js` scheduler, whose `_fetch` is already
  injectable. The mini-map overview becomes a fetch. This is the one client
  change with a hard external reason: only `fetch` can pass
  `targetAddressSpace`, which Chrome 142+ requires for loopback nodes.
- **A transferred `{kind:"u16"}` tile skips decode entirely.** It goes straight
  to the two-byte texture upload in `glInit.js` (`loadArray`,
  `glInit.js:115-161`, which uploads u16 tiles as `RG8UI`). Label tiles hand a
  `Uint32Array` to `labelGpu.js` in Phase 2. Phase 1 writes a stored-deflate
  RGBA8 PNG in JS, so `labelTile.js` stays untouched.
- **Rejected: a Service Worker as the server.** It cannot spawn Workers, so
  there is no DuckDB or decode pool. It is killed after idle, so open files and
  tables evaporate. It has iframe and WKWebView caveats, and debugging is
  opaque.
- **Rejected: a full RPC rewrite of every call site.**

### 6.3 Static shell: the same Jinja templates, rendered once at build time

- **`plexora/client/static_build.py`** runs `create_app()` once and renders:
  - every page twice: the full document and the `_fragment.html` layout, as
    `inject_layout` already does for `X-Plexora-Fragment`;
  - every plugin panel fragment;
  - a `manifest.json`: plugins' `describe()`, requirements descriptors, panels,
    asset URLs, view tools, layer sections, the requirement vocabulary;
  - a `routes.json` that classifies every core route as served, unsupported or
    n/a. A test fails on an unclassified route.

  The output goes to `dist/static/`, shipped in the wheel so that a node can
  serve it at `/app/`.
- **Per-request decisions become client-applied.**
  - `data-plexora-when` attributes sit beside the existing Jinja `if`s: four
    sites in `index.html`, and the Tools and View menus.
  - A `licensed_block` macro emits nothing in server mode and a
    `<template data-plexora-licensed>` in static mode.
  - `window.flaskVariables` comes from `PlexoraShell.variables()`. `datasource`
    is taken from the path, synchronously, for `main.js:7`. Two new keys,
    `backend` and `local_kind` (`"browser"` or `"server"`), are added. The rest
    comes from the project store and the manifest before `PlexoraPage.boot()`.
  - `base_url` must be resolved at runtime, not at build time. One build then
    serves plexoraapp.com, a node's `/app/`, and an OOD `/rnode/<host>/<port>/`
    prefix.
- **Tool panels.** The browser primary answers `/<ds>/tools/<tool>/panel` from
  the pre-rendered fragments, with the same `{locked}`, `{needs}` and
  `{fragments, scripts, styles}` payloads. `toolLoader.js` is untouched. The
  one real port is the `Requires` class of `plexora/api/plugin.py`, with
  `plexora/server/models/manifest.py`, held by a parity test.
- **Rejected:** client-side templating (~40 templates, nine plugins, and a
  second rendering path for server mode), and a JS port of Jinja.

### 6.4 Browser node and project state

- **Images.**
  - geotiff.js (`fromBlob` / `fromUrl`, SubIFD levels, OME-XML names and pixel
    size) and zarrita (directory-handle, file-map and fetch stores).
  - Tiles are raw u16, with no quantize and no encode.
  - Stats come from the coarsest adequate level, with a provisional window and
    an exact scan in the background.
  - A JS 3-component EM GMM, held to tolerance vectors.
  - Flat-TIFF pyramids (images and label masks) are built by band workers into
    OPFS, with Python's own arithmetic (`segmentation_pyramid`'s nearest
    neighbour, `image_pyramid.reduce2`). Level 0 is read from the user's own
    file when it is tiled, and copied (deflated) only from a stripped file
    (P2b: 20 MB for a 9.6 GB mask; P7: 3.8 GB for a 9.6 GB-raw image, the size
    of Python's copy). Bytes go straight into `CompressionStream`; a Blob in
    between fails at this scale.
  - Unsupported, and said loudly: DICOM, SVS with JPEG 2000, MRXS, Imaris LZ4
    (until its plugin is tested), 3-D label volumes, `/volume/*`, surfaces.
- **Tables.**
  - CSV and TSV: plain workers parse byte ranges cut at line starts, with V8's
    `Number()`; every numeric column is kept in OPFS as a raw float64 file plus
    a manifest. The design was Parquet, but P3 measured about 8 s of
    single-threaded Parquet encoding for 1.26 M × 29 doubles, whatever the
    codec; and DuckDB-Wasm's own parse was the remaining cost. The raw store
    reopens in tens of ms without starting anything. DuckDB-Wasm parses a CSV
    with quoted fields, loaded only then.
  - Parquet: hyparquet. DuckDB-Wasm would fetch its Parquet reader from
    `extensions.duckdb.org`; if it is ever the reader, Plexora serves that
    extension itself.
  - h5wasm (WORKERFS) and zarrita for h5ad and SpatialData, copied once to the
    same column store (P4: one pass over X costs 1–9 s at 1.26 M × 90, against
    h5py's 0.5–2.3 s).
  - Implemented: the `load_table` document, `describe`, `all_cells`,
    `geometry`, `columns`, `metadata_column`, `rows`, `roi.map_to_cells`,
    `gating.gmm`.
  - Not supported in the browser: writes into source files
    (`roi.save_anndata`, `roi.save_spatialdata`, `qc.write_cells`, h5ad `uns`).
    Instead, a sidecar in the project store plus "Export annotations", or the
    operation goes to a node that holds the file.
- **Project state (`ProjectStore`).**
  - OPFS `projects/<name>/entry.json`: one `config.json` entry, with resources
    marked `provider:"browser"`.
  - `plugins/<plugin>/`: the `PluginStore` twin, holding state bytes and
    Parquet tables.
  - `derived/`, and the rebuildable `cache/{images,masks,tables}/<fingerprint>/`.
  - IndexedDB holds file handles (Chromium) or descriptors (other browsers →
    "Locate file", the same UX as a moved file today), paired nodes (the
    `nodes.json` twin) and the licence.
- **Portable bundle.** `<name>.plexora-project.zip` holds the entry, a sources
  manifest, plugin files and derived data; no pixels and no caches. Python gets
  `plexora project export` and `import`, and the browser the same, so a project
  moves between modes.
- **Plugin coverage in the static build.** cell_explorer, roi, the manual half
  of gating, and the viewer core. figure_builder, qc, video_studio,
  segmentation, transcripts and visium_hd are `unsupported` in v1. Their tools
  are not offered, and the locked-panel sentence names the desktop app and the
  CLI.

### 6.5 Remote nodes and connectivity, at zero marginal cost to BioCognia

Nothing in the data path or the connection path runs on BioCognia
infrastructure. Any relay or SSH bridge there bills per request and per
CPU-millisecond, and would grow with users. The four paths below cost
BioCognia nothing per user and share one pairing protocol.

| Path | User experience | What runs where |
|---|---|---|
| **Connector (helper)** | Connect on plexoraapp.com. The first time, save the server (address, srun, jump host) as in Settings › Remote servers today. Later, click Connect and type the password or Duo answer in the page | a small loopback service on the user's machine does ssh, srun, askpass and the tunnel, exactly as `plexora/connect.py` does. If the desktop app is installed, it answers. Otherwise the page offers a one-line install (`curl … \| sh`, or a small download), not the full app |
| **Node serves the app (OOD / site proxy)** | launch Plexora from the Open OnDemand portal; no install | the node in the SLURM job serves `dist/static` at `/app/` behind `/rnode/<host>/<port>/`. The browser is same-origin with it: no CORS, no prompt. Every cache sits on the browser side, the layout the remote-node benchmarks found fastest |
| **Lab gateway** | one person runs `plexora gateway` on a lab workstation or VM with a hostname and certificate; lab members connect from plexoraapp.com | the connector's logic, plus an https listener and per-member profiles. BioCognia runs nothing |
| **Direct to tunnel** | as the connector path, plus accepting Chrome's local-network prompt once | the page fetches `http://127.0.0.1:<tunnel>` with `targetAddressSpace:"loopback"` |

**The minimal connector.**

- One Rust crate, `plexora-connector`. The Tauri desktop shell uses it, and it
  is also built alone as a single-file binary per platform. There is one
  implementation.
- It shells out to the system `ssh`, as `connect.py` does: `ssh -L`, `-J`,
  `srun`, the `SSH_ASKPASS` relay, the `[plexora-node]` announce parse, and
  `remotes.json`-compatible profiles.
- It exposes `/connector/v1/{hello,profiles,connect,status,answer,disconnect}`
  on loopback, with CORS for the app origin and a per-install token.
- The token reaches the page once, through a pairing URL
  (`https://plexoraapp.com/connect#…`). The fragment is never sent to a server.
- Section 9, question 3 scopes this port. Signing a downloadable binary is the
  same unsolved item the desktop installers have.

**Node protocol additions, all additive under api_version 1.**

- `/hello.features`: `pair`, `origins-env`, `app`.
- ~~The answer to a PNA (Private Network Access) preflight~~ and ~~a
  `Cross-Origin-Resource-Policy` stamp~~: **not needed** (P5).
  - Chrome 156 no longer preflights a public page's request to loopback; it
    asks the user for the `loopback-network` permission, and the server sends
    nothing.
  - The viewer's reads of a node are CORS reads, and those load under COEP
    `require-corp` without CORP.
- More than one origin from every launch path. `127.0.0.1` and `localhost`
  are different origins (P5 f). `plexora node serve
  --allow-origin` is already repeatable (`action="append"`), but `plexora
  connect` and `--node-allow-origin` pass one origin. They must pass several.
  An environment variable, `PLEXORA_NODE_ORIGINS` (today the name of the node's
  config key), lets a launcher add the hosted origin.
- `plexora node serve --pair [ORIGIN]` prints a pairing URL whose fragment
  carries the endpoint and token. `--browser-endpoint` sets the address for a
  gateway.
- `/app/` serves the static build.
- `plexora connect --pair` keeps building tunnels, and registers the node into
  the page instead of into a local Python primary.

The browser as primary needs nothing else new on the node. It already pushes
the read spec and pulls the compact copy as Arrow IPC. Pro proofs for
`gating.autogate.*` are minted in the browser with WebCrypto HMAC under the
node token.

**Rejected:**

- a dynamic origin-grant endpoint: it is unreachable before CORS allows the
  origin;
- node-side TLS in Waitress: a second server plus a self-signed trust dance;
- any BioCognia-run relay or SSH bridge: cost and privacy.

### 6.6 Services: issuance only, no central data processing

- **Licensing.** The device flow runs against account.biocognia.com; the
  platform must allow the app origin, or offer a thin proxy route. The
  certificate is verified **offline in the browser** with WebCrypto (Ed25519
  in Chrome 113+, Safari 17+, Firefox 130+) and cached in IndexedDB.
  `node/guards.js` is the `check_capability` / `locked_payload` twin, at the
  same seams. For local compute this is exactly as strong as the desktop's
  offline check today.
- **AI gating, QC and chat.** Unavailable in the static build until a Python
  node with the engine is paired; the gateway and harness are Python. The
  button is rendered only when `licensed('ai') && PlexoraBackend.supports('ai')`.
- **Telemetry.** The page posts directly to analytics.plexoraapp.com. The
  Worker adds CORS. It already exists and costs nothing per user beyond today.
- **Degradation.** `PlexoraBackend.supports(feature)` drives what is offered.
  Missing capabilities are sentences in the existing locked-panel and
  `resource_status` UX, never blank panels.

## 7. Phased roadmap

| Phase | What changes | Tests that must stay green | New tests | Rollback |
|---|---|---|---|---|
| **POC** (next; section 8) | nothing under `plexora/`; `tools/poc_browser_node/` on the POC branch; records in `docs/internal/bench/browser_node/` | everything | the POC records | delete the branch |
| **0. Seams** (small, reversible) | `services/backend.js` pass-through; `base.html` script insertion + `static_build` branch; `licensed_block` + `data-plexora-when`; `template_data` gains `backend` and `local_kind`; label, RGB and overview tiles onto `tileFetch` with `targetAddressSpace`; node: `features`, multi-origin launch + environment variable (no PNA preflight answer or CORP: P5), `--pair`, `/app/`; `connect.py --pair`; `plexora/client/static_build.py` (not yet deployed); goldens regenerated once with the diff reviewed | all `tests/test_node_*.py`; `tests/js/*_probe.mjs` (`resource_routing`, `tile_fetch`, `tile_url`, `label_tile_lifecycle`, `label_gpu`, `app_router`, `license`, `tool_assets`, `datalayer_globals`); `test_hd_default.py`, `test_plugin_boundary.py`, `test_app_shell.py`, `test_app_router.py`, `test_auth_token.py`, `test_browser_guard.py` | `tests/js/backend_shim_probe.mjs`, `tests/test_static_build.py`, `tests/test_node_pairing.py`, PNA/CORP cases in `test_node_direct_routing.py` | remove the script tag and the macro |
| **1. Thin static client over Python nodes** (no Wasm) | `shell.js`, `node/router.js`, `node/primary.js`, `node/nodeClient.js`, `node/wire.js`, `workers/{kdtree,centroids}.js`, `node/requirements.js` + parity test, `node/guards.js`, `LicenseClient`; the connector crate and its pairing page; the node serves `/app/`; the OOD scenario | as above | `nodeClient` conformance against `tests/node_harness.py`; `static_routes.json` classification; connector pairing round trip | unpublish the static site; `/app/` off by flag |
| **2. Browser node, read-only** | `workers/browserNode.js` (geotiff.js, zarrita, h5wasm, hyparquet; the POC's CSV workers; DuckDB-Wasm on demand for quoted CSVs, its Parquet extension served by Plexora), `pyramid.js` (the POC's band workers), `histogram.js`, `gmm.js`, OPFS caches, flat image and mask conversion, Import with the browser file picker (`local_kind:"browser"` in `dataLocation.js`, `browsePicker.js`, `fileSourceRow.js`), the cell_explorer, roi and manual gating halves | as above | `tests/node_conformance/` vectors + `browser_node_conformance_probe.mjs`; GMM and histogram tolerance vectors; the POC's `reduce` vectors and CSV edge cases | `supports("file_picker")=false` hides local import; Phase 1 remains |
| **3. Write-back, bundles, mixed projects, lab gateway** | project-store sidecars for roi, gating and qc; `plexora project export/import` + browser export/import; mixed projects (table in the browser, image on a node); `plexora gateway` | as above | bundle round trip Python → browser → Python | feature flags |
| **4. Optional** | COOP/COEP isolation for DuckDB threads (nodes need no CORP, P5 d; not Safari); volumes and surfaces if demanded; WebGPU kernels | | | |

**Risk register.**

- Two implementations of primary logic. Only the conformance harness mitigates
  it.
- h5ad in wasm32: correct but 2–13× h5py (P4). Large flat masks and images are
  no longer a risk (P2b, P7).
- Safari storage semantics.
- The Chrome prompt as a new user step. Its wording is copied into the pairing
  page.
- The plugin coverage gap, stated in product copy.
- Golden churn in Phase 0, reviewed line by line.
- The connector port.

## 8. Proof-of-concept plan

Nothing moves into `plexora/client/src` until the ladder says GO. Concretely,
no change under `plexora/` until P1, P3 and P5 report GO.

Every POC produces one JSON record and a short MD under
`docs/internal/bench/browser_node/`, tagged in the convention of
[REMOTE_VS_NODE_2026-10-26.md](bench/REMOTE_VS_NODE_2026-10-26.md). The POCs
reuse existing harnesses rather than building new ones:

- `tools/bench3d/drive.mjs`: GPU Chrome over the Chrome DevTools Protocol,
  bytes by origin, software-renderer refusal;
- `tools/bench_remote/tourpage.js`: the full-resolution completion test;
- `tour.mjs`: the fixed 11-step tour;
- `campaign.py` / `analyze.py`: ABBA blocks (paired runs, arm order
  alternated A-B-B-A), paired bootstrap, Holm adjustment;
- `tests/node_harness.py`: a node in a second process.

The fairness rules of the remote-vs-node campaign apply. Same bytes, checked by
fingerprint. Same pixels: uint16 tile SHA-256 across arms, HD on, channel
ranges pinned so auto-level never changes the picture under test. Same
completion test, same Chrome binary and GPU, a 1440×900 window at device pixel
ratio 1, paired ABBA blocks, censoring at the cap.

### 8.1 Ladder

```
P0 seam audit (no timing)
 ├─ P1 local OME-TIFF → existing colorize pass      (make-or-break for "no local Python")
 │   ├─ P2 local label pyramid → labelGpu; P2b build the pyramid in a worker
 │   └─ P7 OPFS persistence across restart; flat-TIFF pyramidise in-browser
 ├─ P3 local CSV/Parquet in DuckDB-Wasm → selection on the image   (parallel to P1)
 ├─ P4 AnnData / SpatialData capability matrix                       (parallel)
 └─ P5 remote node from a static origin, ssh tunnel to O2, LNA prompt (parallel; release-blocking)
      └─ P6 head-to-head vs local Python, same hardware              (needs P1+P2+P3)
           └─ P8 Firefox / Safari fallbacks
```

- A STOP on **P1** does not kill the mode. It collapses to "the browser as a
  thin client of nodes": local Python started as a node, plus remote nodes.
  That still gives static hosting, but not "no local Python".
- A STOP on **P5** blocks the mode outright.
- A STOP on **P6** limits the mode to a remote-node-client tier.

### 8.2 The POCs

Reading the thresholds:

- **Fixtures.** S is synthetic and small. M is one Orion sample (47–112 GB, 19
  channels) on NVMe, with its 34050×5797 uint32 mask and its 0.2–0.8 GB CSV. L
  is the 67 GB LSP11385 OME-TIFF with a 1.26 M × 90 table. XL is the 303 GB,
  80-channel LSP70267 and the 19.9 M-bin Visium HD set.
- **Cache states.** C0, C1 and C2 are local Python's first-ever open, cold and
  warm states, as in the remote-vs-node campaign. B0, B1 and B2 are the
  browser's: fresh profile with OPFS cleared, OPFS kept, revisit.
- **Timing** is compared Chrome to Chrome on one machine, GPU on. Firefox and
  Safari are functional checks only.

| POC | Hypothesis | GO | STOP | If it stops |
|---|---|---|---|---|
| **P0** seam audit | the product's rendering layer runs unmodified from a static page, and `tourpage.js` applies unchanged. It also settles the byte order `glInit.loadArray` expects for HD tiles (HD PNG is big-endian; geotiff.js returns native-endian) | 100 % identical pixels against the product page, with no edit to the harness | a module cannot load without a change in `plexora/client/src` | the first product task becomes extracting the `Source` seam, before any timing |
| **P1** local OME-TIFF | geotiff.js SubIFD reads from a local `File`, in a worker pool, deliver 1024² uint16 tiles fast enough to hold the measured 8.4 ms HD pan, byte-identical to the Python HD path | tile median ≤ 30 ms and p95 ≤ 80 ms on NVMe; pan median ≤ 12 ms and p95 ≤ 20 ms at 7 channels; every step ≤ 1.5× local-Python C2 and open ≤ 1.0× C1; 200/200 tiles identical; auto-level ≤ 2 s per channel, provisional window ≤ 0.2 s | tile median > 60 ms; pan median > 25 ms at 7 channels; any mismatch not explained by a lossy codec; or M's codec unsupported | local images go through a local Python node (mode C); a single failing codec becomes "prepare first" |
| **P2 / P2b** local mask | a prepared label pyramid draws through `labelGpu.drawTile` with the 25 ms gate drag; a flat uint32 mask pyramidises in a worker into OPFS within 2× the server's ~60 s | M build ≤ 120 s with ≤ 5 long tasks > 50 ms; derived ≤ 2× raw; drag median ≤ 30 ms at 1.26 M ids; reopen from OPFS, first tile ≤ 1 s | build > 5 min; a tab crash at M; or `QuotaExceededError` with ≥ 50 GB free | masks become "prepare first" or go through a local Python node; the read half still draws a ready pyramid |
| **P3** local table | a 0.8 GB CSV loads into DuckDB-Wasm, and the five operations the viewer needs (all_cells, column, describe + 50-bin histograms, nearest cell, ROI → cells) match the Python server exactly, within bounded time | at L: load ≤ 5× polars' read of the same file; reopen ≤ 5 s; describe ≤ 3 s; warm column ≤ 200 ms; nearest ≤ 5 ms; ROI with 100 vertices ≤ 300 ms; identity 100 %; Wasm memory ≤ 2 GB | wasm32 out of memory at L; or an identity failure not explained by float32 rounding | tables go through a local Python node; the in-page gate stays; XL sets the "prepare first" row threshold |
| **P4** AnnData / SpatialData matrix | h5wasm over WORKERFS reads obs, obsm/spatial and dense X row blocks for common writers; zarrita reads a SpatialData table; the unsupported set is small and enumerable | obs + obsm + dense X blocks + per-column reads (≤ 1024 features) succeed within 2× Python, uncompressed and gzip; every "unsupported" has a named conversion | obs or obsm unreadable | h5ad and SpatialData are served by a node, or exported to Parquet by `plexora prepare` |
| **P5** remote node from a static origin | a page at a static origin, with nothing else running, uses an existing `plexora node serve` through `/node/v1/*` end to end; tiles are byte-identical to the desktop route; the tour is within 1.2× the desktop node arm; the permission and CORS flows are workable | over ≥ 4 ABBA blocks: open C1 ≤ 1.2× desktop (1.81 s); navigation median ≤ 1.2×; 100 % of steps full by 10 s; tiles identical; table operations identical; scenarios a–c and e pass | a primary-only capability with no node route and no in-browser substitute; completion < 100 % by 10 s; or the LNA prompt has no documented path | the mode is blocked until the node protocol gains the missing route or header |
| **P6** head-to-head | for a local project on NVMe, cold and warm open, navigation, interaction and memory are within thresholds of local Python on the same machine | browser/Python geometric-mean ratio ≤ 1.3 (bootstrap upper CI ≤ 1.6) on open and navigation; frame p95 ratio 0.9–1.1; heap ≤ 2 GB on 32 GB machines; an 8 GB laptop completes M | ratio > 2 on open or navigation; or any crash at M on a 32 GB machine | ship only a remote-node-client tier; local files stay with the desktop app |
| **P7** OPFS persistence, flat-TIFF pyramid | derived data survives a restart on Chrome, Edge and Firefox; reopen is at least as fast as local-Python C1; a flat TIFF > 4096 px pyramidises in a worker within 2× the server | 3/3 restarts survive; reopen ≤ C1; pyramidise ≤ 2× the server; quota ≥ 2× derived | derived data lost across a normal Chrome restart; or Firefox's 10 GiB below M's derived size | derived data goes beside the source (Chromium write permission) or comes from the desktop app or CLI |
| **P8** Firefox / Safari | the P1 and P3 pages at S work through `<input type=file>` and drag-drop with the same pixels; the differences are enumerated | S identical on both; M opens on Firefox | S fails on both without a fallback | the support statement becomes Chromium-first, stated plainly |

P5's scenarios:

- (a) http static page → http loopback node;
- (b) https static page (mkcert) → http loopback node, with
  `targetAddressSpace:"loopback"` and the Chrome LNA prompt;
- (c) https → https node behind a TLS proxy;
- (d) with and without COOP/COEP, to learn whether the node needs CORP;
- (e) node restart recovery;
- (f) both `--allow-origin` spellings (`127.0.0.1` and `localhost`);
- (g) the connector pairing flow, against a stub loopback connector answering
  `/connector/v1/hello` and `connect`: the page's probe, the pairing URL, and
  the LNA prompt on an https origin;
- (h) the node serving the static build under an `/rnode/<host>/<port>/`-style
  prefix through a local reverse proxy, with `base_url` resolved at runtime.

The desktop reference arm is `campaign.py`'s arm N. The real OOD and O2 runs are
manual records (section 8.4).

### 8.3 Fallback modes A–E

| Mode | Meaning |
|---|---|
| **A** | read in place |
| **B** | prepare in the browser, into OPFS |
| **C** | local Python as a node (`plexora node serve --serve image:x=<path> --allow-origin <origin> --dynamic`), treated exactly as a remote node |
| **D** | a remote node |
| **E** | prepare in the desktop app or CLI: `plexora prepare` writes a pyramidal OME-TIFF, a label pyramid, Parquet and quantization windows. It is proposed; today `plexora node prepare` converts masks only |

| Input | Condition | Mode |
|---|---|---|
| pyramidal TIFF, supported codec | read p95 ≤ `T_read_p95` | A |
| flat TIFF > 4096 px | derived ≤ `T_derived_bytes` and build ≤ `T_build` | B, else E |
| mask pyramid / flat mask | as above; ids > 2^24 | A / B; the CPU path with a warning |
| CSV / Parquet | rows × columns ≤ `T_rows×cols_max` | A (+ a B column-store cache in OPFS), else C |
| h5ad / SpatialData table | P4 matrix says "ok" | A, else C or E |
| Imaris, DICOM, SpatialData images, JPEG 2000, MRXS | always | C or D |
| any file on a share with read p95 > `T_read_p95` | | C (a node beside the file) |
| Firefox or Safari with derived size > quota | | E |

The page states which mode it chose and why, in one sentence.

The `T_*` names are thresholds the POCs turn into numbers:

- `T_read_p95` comes from P1's per-tile read ladder over NVMe, SATA and SMB;
- `T_derived_bytes` comes from P7's write-until-error and
  `navigator.storage.estimate()`;
- `T_rows×cols_max` comes from P3's ladder (1.26 M × 90 → 5 M × 90 → 19.9 M ×
  10) until out of memory, per browser;
- `T_build` is the build-time ceiling that P2b and P7 measure.

### 8.4 Release-blocking acceptance criteria for remote-node compatibility

**Must stay green, unchanged.** Editing one of these is itself a finding:

- `tests/test_node_direct_routing.py`, `test_node_image.py`,
  `test_node_table.py`, `test_tile_batch.py`, `test_node_warm_and_cache.py`,
  `test_unreachable_node.py`, `test_node_disconnect.py`,
  `test_node_reconnect.py`, `test_node_routes.py`, `test_tile_url.py`,
  `test_bench_remote.py`, `test_hd_default.py`;
- the Karma suite;
- `tests/js/*_probe.mjs`.

**New tests.** They live in the POC tree first, and are promoted only if the
mode proceeds:

- node CORS for a non-loopback static origin, plus CORP if adopted;
- a static page + `NodeProcess` → 20 tiles byte-identical, HD and 8-bit;
- table parity through the page's node client: all_cells, describe, columns,
  gating masks, rows, ROI mapping on the node;
- loopback → loopback needs no prompt (automated);
- the https → loopback LNA flow (headed, with screenshots kept);
- node restart recovery;
- the harness drivers parse.

**Manual records, one each:**

- **O2:** a SLURM job, Duo once, the 640 GB Imaris file and NLU290, restart
  recovery, and the `-t` time-limit cliff. Made on 2026-11-06 with NLU337:
  P5R (the protocol, byte-identical) and P5T (open and tour against the
  desktop route), each on one login. On O2 the login node cannot reach a
  compute node's port, so the `bind_node` layout fails there. An ssh from
  the login node into the job's node, with the cluster's own key, works and
  asks for no Duo; it is what O2's `srun --tunnel` does for Jupyter. The
  connector should build O2 tunnels that way (§6.5).
- **ERIS:** portal address rewriting. A static page has no proxy fallback when
  direct routing fails.
- **GCP:** a VM node behind a TLS proxy, a public address space, CORS
  preflights on POSTs, and real-internet round trips.

**Release gate.** All three manual records at GO, and the tests green on CI.
CI runs SwiftShader for correctness only; timing is never a CI assertion.

Results land in `docs/internal/bench/browser_node/` (one JSON record and one MD per POC); the campaign report `BROWSER_NODE_POC_<date>.md` says which thresholds are now numbers.

## 9. Open questions and blockers, ranked

| # | Severity | Question | Resolved by |
|---|---|---|---|
| 1 | Blocker → **resolved** | Can geotiff.js over a local `File` hold the HD pan budget (8.4 ms median), and open an 80-channel, 8-level OME-TIFF (hundreds of IFDs and SubIFDs) in a few seconds cold? Predicted yes for deflate and LZW; the IFD walk is the unknown. **Measured:** a 40-channel, 7-level file opens in 73–107 ms (lazy IFDs); pan frames ≤ 2.2 ms p95 at 7 channels; deflate with predictor 2 decodes byte-identically. The 80-channel file was not run | P0, P1 |
| 2 | Blocker → **resolved on loopback** | Does a static origin reach an existing node end to end (pairing, token, CORS, PNA preflight, batch POST, table routes, restart recovery), and is the Chrome prompt a workable step? Safari 18.0–18.3 cannot do loopback at all. **Measured:** yes, byte-identical; no preflight answer needed; one `loopback-network` permission; and on O2 (2026-11-06), a page from main against a released 0.0.28 node in a SLURM job through one ssh tunnel, every byte identical (HD tile 66 ms p50, 1.38 M cells' all_cells in 2.6 s). Open: ERIS, GCP and Safari | P5 (a–h), P5R (O2) |
| 3 | High | The minimal connector: how much of `plexora/connect.py` (3,430 lines: ssh, `-J`, srun, the askpass relay, the announce parse, GCP `gcloud … --tunnel-through-iap`, remotes.json) the Rust crate must port for the first release; whether Windows OpenSSH's askpass limitation (`DEPLOYMENT.md:492-494`) stays; how a downloadable binary is signed while the desktop installers are not yet | a scoping pass on `connect.py` before Phase 1; the signing item is shared with releases |
| 4 | High → **answered, PARTIAL** | h5ad in wasm32: per-column reads of a 1.26 M × 90 dense `X` through h5wasm, against a one-time copy; CSR column reads; LZ4-filtered files; and the policy that the browser never writes into the user's source file (sidecar + export instead). **Measured (P4):** every read identical to h5py, at 2–13× its time; a column of an uncompressed dense X read alone is ~50× (one tiny read per row), so X is copied once into the column store (1–9 s at 1.26 M × 90); lzf-compressed strings abort the plugin. Open: the write-back wording | P4; a product decision on the write-back wording |
| 5 | High | Two implementations of primary logic: describe, GMM, kd-tree, centroid tiles, `Requires`, launch parsing. Without conformance vectors this design fails its own goal | `tests/node_conformance/`, built in Phase 1 before any Phase 2 code |
| 6 | High → **resolved** | Large flat masks: a strip-wise label-pyramid builder in a worker writing to OPFS, and the threshold above which "prepare first" is the answer. **Measured:** a 9.6 GB-raw whole-slide mask in 58 s, peak 1.09 GB, no jank, identical to Python's; about 6 s per raw GB in one worker. No "prepare first" threshold was reached. **Second round:** band workers build it in about half Python's time, and flat images too (P7) | P2b, P7 |
| 7 | Medium | The node serves the app. `base_url` becomes a runtime value: 186 `base_url` substitutions in core and plugin templates today, 104 of them in `base.html` and 65 in `index.html`. The node's `/` deliberately reveals nothing (`node/app.py:123-136`), so `/app/` changes that posture and must stay token-gated. OOD cookie and prefix behaviour | Phase 0/1; P5 (h); the OOD manual record |
| 8 | Medium | The licence in the browser: does the platform's device flow allow the app origin (CORS), or does it need a proxy route; shipping the Ed25519 public keys; the refusal UX when offline | a check against account.biocognia.com before Phase 1 |
| 9 | Medium → **Firefox answered** | Firefox and Safari: no persisted handles, Firefox's 10 GiB, Safari's 7-day purge and no Memory64; the "re-locate files" and export nudges. **Measured (P8):** Firefox 159 passes every S feature (tiles, pyramids in OPFS, the CSV store, h5ad, SpatialData) with the same picture as Chrome. Playwright's WebKit on Windows passes everything but OPFS, which that port lacks; Safari itself is unmeasured | P8 on a Mac |
| 10 | Medium | Bucket CORS varies: Orion's S3 bucket is open, IDC's Google Cloud Storage object reads are not. The page must detect it and say "use a node or the Python helper" | the Phase 2 import flow; one probe per bucket |
| 11 | Low → **fixed** | An existing bug, independent of this work: `frag.glsl:504` rebuilt the 16-bit value from the two uploaded bytes as `r*255 + g`; it should be 256. **Measured** through the product's renderer: ±1 display level over the full range, up to **11 of 255 levels dark** for a bright channel in a tight window ([0.50, 0.55]), and a saturated pixel in a window at the top of the range near black. Fixed on main (9a2b1912) with `tests/test_hd_u16_shader.py` | done |
| 12 | Low | COOP/COEP later (DuckDB threads, ffmpeg.wasm). **Measured:** CORS reads of a node (fetch, XHR) load under `require-corp` without CORP, so nodes need nothing; `credentialless` is still absent in Safari. Memory64 for tables over 4 GB is Chrome and Firefox only | Phase 4 |
| 13 | Medium → **resolved** | OPFS quota in a fresh Chrome profile on the bench machine read about 10.7 GB. **Measured:** that is Chrome 156's report, not its limit: `estimate().quota` is usage + 10 GiB (exactly 10,737,418,240 when empty), headless or headed, persistent profile or not, and 14.2 GB wrote without error (the report then read 25.0 GB). It is no setting of Plexora's; the site uses the browser's own limit (about 60 % of the disk in Chrome), calls `navigator.storage.persist()`, and treats `QuotaExceededError`, not the estimate, as the signal. Firefox's 10 GiB is real | P7 |
| 14 | Medium → **resolved** | P3's first-ever CSV open was 5–10× polars' read (one-threaded wasm parse): 3.4 s to drawable at 1.26 M cells. **Measured since:** plain workers parsing with V8's `Number()` reach drawable in about 1 s (under 3× polars), bit-identical to DuckDB's parse; no isolation needed | P3 |
| 15 | High → **answered, PARTIAL** | Can the AI layer (agent capabilities, the gating engine, spatialagents, spatialbridge, biocognia) run unchanged in a no-install website, keeping one Python codebase? Or does the website need a small download beside the browser? **Measured (P9):** in Pyodide in a worker, the gating session's packets, answers, states and gates are identical to CPython's, at 0.9–1.2× its time. Eight substitutions (threads, imagecodecs, polars' missing Parquet, `os.link`) total 111 lines. The viewer is unaffected. Startup misses the user's bars: ready 6.2–7.1 s after a visit, and 13.8 s on a first visit over 50 Mbps with the 46 MB trimmed build (17.3 s with the 70 MB build Cloudflare Pages can serve). The proof also lists nine product changes a browser build needs | P9; a decision on startup |

## POC results

Two rounds on 2026-11-05: one Windows machine (i9-12900K, 128 GB, RTX 3080, NVMe), Chrome 156 headless on the GPU (Firefox 159 and WebKit 27.2 for P8), and Python as the reference for every byte: `plexora node serve`, Python's own pyramid builders, h5py, zarr-python and pyarrow, and the product itself for P6. The full report is [BROWSER_NODE_POC_2026-11-05.md](bench/browser_node/BROWSER_NODE_POC_2026-11-05.md); there is one report per POC beside it. The records are the second round's.

| POC | Verdict | Key numbers (Measured) |
|---|---|---|
| P0 | **GO** | Rendering modules unmodified; one page-side shim (`toFloatColor`). Node-fed and browser-fed canvases identical at every view (0 of 648,000 pixels differ). `frag.glsl` bug quantified, then fixed (the probe now reads 0 error) |
| P1 | **GO** | 66.7 GB, 40-channel OME-TIFF: open 0.06 s. Tile p50 20.5 ms, p95 35.5 ms. 200/200 tiles identical to the HD path. Tour to full 2.3–2.9× sooner than a local Python node. Auto-level 0.10–0.21 s per channel. The same reader on a 73 GB S3 file: open 7.0 s, tiles 0.36 s p50 |
| P2 | **GO** | Label tiles identical (200/200). GPU gate drag 18.4 ms per drawn update at 1.26 M ids |
| P2b | **GO** (round 1 PARTIAL) | Whole-slide flat mask (9.6 GB raw) → pyramid by band workers: 9.6 s against Python's 21.8 s; 15.7 s in 4 workers and 0.75 GB; identical; no long tasks |
| P3 | **GO** (round 1 PARTIAL) | 1.26 M × 29 CSV parsed by plain workers: drawable in 0.9 s (2.7× polars; DuckDB-Wasm took 3.4 s), peak 1.09 GB; every column bit-identical to DuckDB's parse; every answer identical to the server's; reopen 40 ms |
| P4 | **PARTIAL** | h5ad (1.26 M × 90; dense and CSR; none, gzip, lzf) and a spatialdata 0.8 store: every read identical to Python's except lzf-compressed strings; h5wasm 2–13× h5py (a lone dense column 56×); one pass copies X into the column store in 1–9 s. DuckDB fetches its Parquet reader from the network; hyparquet does not |
| P5 | **GO** | Static page ↔ node: tiles, batch, all_cells and describe byte-identical to the desktop route. Restart recovery; connector pairing; COOP/COEP; the node-served app under `/rnode/`; Local Network Access with one `loopback-network` permission and no server header, and, headed, the prompt that holds the first fetch. On an O2 node against the desktop route (6 ABBA blocks, cold): open 0.50× (0.63× with both channels drawn), navigation 1.03×, 240/240 steps full within 10 s |
| P6 | **GO** | The product page over a scratch Python server against the browser node, same image, tour, waypoints and completion test, ABBA: every step that needs new tiles 2.4–3.3× faster in the browser; open 0.3× the product's (page load included); renderer peak 0.68 GB against 1.3 GB |
| P7 | **GO** | A 9.6 GB-raw flat image (2 channels) → pyramid by band workers with `image_pyramid.reduce2`'s arithmetic: 27.5 s against Python's 31.2 s from zlib strips, 21.2 s against 178.6 s from an uncompressed file; 200/200 tiles identical at every level; no long tasks; reopened in 14 ms after a relaunch. Chrome's `estimate()` reads usage + 10 GiB: not a limit |
| P8 | **GO** for Firefox | Firefox 159: every S feature, the same picture as Chrome (0.4 of 255 levels, worst grid cell). WebKit on Windows: all but OPFS, which that port lacks; Safari unmeasured |
| P9 | **PARTIAL** (2026-11-06) | Plexora's gating session in Pyodide 314.0.7 beside the viewer, against CPython: S and M sessions identical in packets, answers, final states and gates; 0.87–1.22× CPython's time; no long tasks added to the viewer. Ready (trimmed build: precompiled bytecode, brotli, pyarrow on demand) 7.1 s cold, 6.4 s on return, 13.8 s on a first visit at 50 Mbps / 20 ms (46 MB); as published 16.3, 16.1 and 28.9 s (81.5 MB). 1.26 M cells reach Python through the column store in 2.3 s; polars in wasm cannot parse that CSV |

**What changed in the design** (each section above notes it):
- the browser's table store is raw columns in OPFS, not Parquet, filled by plain workers, not DuckDB (§3, §6.4);
- Parquet is read by hyparquet; DuckDB-Wasm is a fallback loaded on demand (§3);
- flat masks and flat images become pyramids in band workers, with Python's arithmetic (§2.1, §6.4);
- h5ad is copied once into the column store (§2.2, §6.4);
- the node needs no private-network preflight answer and no CORP stamp (§6.5);
- the pairing page announces Chrome's prompt and retries after it (§4.1);
- Chrome's `estimate()` is not a storage limit (§2.4, question 13);
- four Phase 0 seams were found in P0:
  - export the tile-source helpers;
  - move `toFloatColor`;
  - keep `.png` addresses for OpenSeadragon's transparency rule;
  - register handlers before items.

**Still to do:**
- P9's startup: decide whether 6–7 s to AI-ready (14 s on a first visit at 50 Mbps) is acceptable, or measure the next steps against the same bars. Those steps are staged readiness, fetching packages while the core starts, and snapshots that keep loaded libraries ([P9.md](bench/browser_node/P9.md));
- the ERIS and GCP records (O2 done: `P5R-none-2026-11-06-o2.json`, every byte identical; `P5T-none-2026-11-06-o2.json`, open and tour times against the desktop route);
- Safari on a Mac (P8);
- an XL table; the LZ4 plugin on a real .ims.
