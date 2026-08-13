# Plexora Project Guide

Use this guide when working on the `plexora` repository. It captures the project shape, high-risk boundaries, and validation workflows so a new coding agent can become useful quickly.

## What This Project Is

Plexora is an OpenSeadragon-based cellular image viewing and analysis tool. It has a Python Flask/Waitress backend and a JavaScript/Webpack frontend.

The core application serves multiresolution microscopy image tiles, segmentation/label tiles, feature CSV-backed cell data, marker-threshold/GMM gating analysis, and viewer pages. It now also supports Jupyter notebooks through an iframe-backed sidecar Flask server, exposed directly on localhost or through `jupyter-server-proxy`.

Important user-facing modes:

- Desktop/local web app: `python run.py`, then open `http://localhost:8000/`.
- Notebook app: `from plexora.jupyter import PlexoraViewer`.
- Remote Jupyter/JupyterHub: use `PlexoraViewer(..., proxy=True)` with `jupyter-server-proxy`.
- PyPI/package install: `pip install "plexora[jupyter]"` should work without conda, while conda/uv remain useful for development.
- Frontend development: edit `plexora/client/src`, then run `npm run start` to regenerate bundled assets in `plexora/client/dist`.

## Repository Map

Top-level files:

- `pyproject.toml`: Python package metadata, dependencies, extras, console script, Jupyter server proxy entry point, package data.
- `requirements.yml`: Conda bootstrap environment for local development. Conda owns the interpreter; pip/uv installs the package.
- `requirements-dev.lock.txt`: uv-generated Python dependency lock for dev/Jupyter extras.
- `run.py`: legacy/local desktop server entry point. Keep this working.
- `Dockerfile`: Docker runtime, currently Python 3.13.
- `MANIFEST.in` and `[tool.setuptools.package-data]`: packaging inclusion for frontend assets/templates/shaders.
- `README.md`: user-facing usage notes.
- `tests/baseline_orion2.py`: main local smoke test using the `orion2` datasource when available.

Python package:

- `plexora/__init__.py`: creates the Flask app, configures `data_path`, SQLite path, package paths, base URL, notebook-mode flag, and imports routes/models. Does **not** set any iframe-specific headers (an `X-Frame-Options: SAMEORIGIN` header here used to block direct/local notebook iframe embedding entirely -- since fixed by removing it; see "Notebook Proxy Env-Var Propagation").
- `plexora/server_cli.py`: notebook-friendly sidecar CLI, exposed as `plexora-server`.
- `plexora/jupyter.py`: notebook display API and subprocess lifecycle for sidecar servers.
- `plexora/proxy.py`: `jupyter-server-proxy` launcher entry point.
- `plexora/datasource.py`: programmatic datasource registration for notebooks and scripts.
- `plexora/server/models/data_model.py`: core tile, metadata, CSV, zarr/OME-TIFF, segmentation, GMM, and spatial-query behavior. Treat this as high-risk.
- `plexora/server/models/adapters/`: input-format adapter layer (CSV/AnnData) -- see "Multi-Modal Datasource Support" below.
  - `base.py`: `NormalizedDatasource` dataclass (the common shape every adapter resolves to).
  - `csv_adapter.py` / `anndata_adapter.py`: format-specific `load_table()` implementations.
  - `inspection.py`: `inspect_anndata()` -- read-only introspection backing the standalone AnnData config UI.
  - `__init__.py`: `get_adapter(data_type)` factory (`_ADAPTERS = {"csv": CsvAdapter, "anndata": AnnDataAdapter}`).
- `plexora/server/models/database_model.py`: SQLite models (`ChannelList`, `GatingList` -- persisted per-datasource channel/gating UI state, not just image/feature data).
- `plexora/server/models/centroid_tiles.py`: centroid manifest/tile cache. Dispatches through `get_adapter()` (`_load_table()` helper), not a raw `pl.read_csv()` -- must stay format-agnostic (was AnnData-broken once, see below).
- `plexora/server/routes/page_routes.py`: viewer/upload/page routes.
- `plexora/server/routes/data_routes.py`: JSON/data/tile/query/download routes.
- `plexora/server/routes/import_routes.py`: CSV upload/import flow routes (positional/header-matching wizard, `channelMatch.js`-driven).
- `plexora/server/routes/datasource_config_routes.py`: AnnData import flow routes (`import_anndata`/`save_datasource_config`) -- a separate, non-positional wizard driven by `inspect_anndata()`'s JSON payload.
- `plexora/server/utils/*`: conversion, pyramid, normalization, and image utility code.

Frontend:

- `plexora/client/package.json`: Webpack 5 frontend dependencies and scripts.
- `plexora/client/webpack.config.js`: JS/CSS/shader bundling config.
- `plexora/client/src/js/main.js`: app initialization.
- `plexora/client/src/js/services/dataLayer.js`: client API layer for server data/metadata/tile configuration.
- `plexora/client/src/js/views/imageViewer.js`: OpenSeadragon viewer, tile loading, cache behavior, overlays, channel rendering.
- `plexora/client/src/js/services/glRenderer.js`: owned WebGL2 tile-colorize/threshold engine (`GLRenderer` class — shader compile/link, texture upload). Ported from the `viawebgl` project's OpenSeadragon-independent core; this repo no longer depends on that package. See "OpenSeadragon Integration" below.
- `plexora/client/src/js/views/csvGatingList.js`: gating marker list + sliders. Its `columns` are the *feature-table* columns (e.g. `adata.var_names`, or CSV headers) -- never the image channel list. See "Multi-Modal Datasource Support" below for why that distinction matters.
- `plexora/client/src/js/views/viewerSidebar.js`: the unified sidebar (image channel slots + gate marker picker/slider/distribution plot), DB-backed via `ChannelList`/`GatingList` (autosaved, restored on load). Duplicates some marker-list derivation independently of `csvGatingList.js` (`getGateMarkerNames()`) -- historically a source of bugs that were fixed in one file and not the other; check both when touching gating-marker logic.
- `plexora/client/src/js/views/channelList.js`: image *channel* list/rendering picker (opacity, color, threshold) -- driven by `config.imageData`, a different vocabulary than the gating marker list above for AnnData datasources.
- `plexora/client/templates/datasource_config.html` + `plexora/client/src/js/views/datasourceConfig.js`: standalone AnnData "configure datasource" page (coordinates/features/subset/transform), separate from the CSV `channelMatch.js` wizard.
- `plexora/client/templates/*.html`: Flask templates. `base.html` is especially important for base URL and frontend asset loading.
- `plexora/client/external/openseadragon-bin-2.4.0/`: only `canvas-overlay-hd.js` (lasso/centroid canvas overlay), `openseadragon-scalebar.js` (scale bar + "download current view" export), and the toolbar icon image set remain here — both are third-party, unmaintained, single-file plugins loaded as plain `<script>` tags in `base.html`, not npm packages. The folder name is stale (real OpenSeadragon is now `client/package.json`'s `openseadragon` npm dependency, not a vendored 2.4.0 build) but was kept as-is rather than renamed. Do not put a new copy of the OpenSeadragon core JS file back in this folder.
- `plexora/client/dist/vendor_bundle.js`: built frontend bundle that must be included in packages.

Generated/local-only directories:

- `build/`, `dist/`, `plexora.egg-info/`, `plexora-<version>/`, `__pycache__/`, `.pytest_cache/`, `node_modules/`, and `plexora/data/` are generated or local data. Do not commit them unless explicitly asked and justified.

## Core Architecture

The Flask app is created at import time in `plexora/__init__.py`.

Data root selection:

- `PLEXORA_DATA_PATH` wins when set.
- Frozen/PyInstaller apps use a `data` directory next to the executable.
- Default development mode uses `plexora/data`.

The selected data root contains:

- `config.json`: datasource definitions.
- datasource directories and generated pyramids/tiles as needed. Each datasource's own directory also holds that datasource's gating/channel-list SQLite database, `<name>/<name>.db` -- see "Per-Datasource SQLite Databases" below.
- `db.sqlite3` (legacy, may or may not be present): the old *shared* SQLite database that used to hold every datasource's `ChannelList`/`GatingList` rows before the per-datasource-file migration. It is never written to anymore and is read at most once per datasource (best-effort, on that datasource's first access after upgrading) to carry old saved state forward into the new per-datasource file. Don't delete it until every datasource that had saved gating/channel state in it has actually been opened at least once post-upgrade (so the migration has actually run for each) -- deleting it early just means any not-yet-migrated datasource loses its old saved state instead of inheriting it.

Tile and metadata flow:

- The browser loads a datasource page such as `/orion2`.
- The frontend requests `/config`, metadata, channel names, OME metadata, and `/generated/data/<datasource>/<channel>/<level>/<x>_<y>.png` tiles.
- Python routes delegate most tile/metadata behavior to `server/models/data_model.py`.
- Segmentation is represented as an image channel in `config.json` plus `segmentation` metadata. The first `imageData` entry often points to the label/area channel.

Notebook flow:

- `PlexoraViewer` starts `python -m plexora.server_cli` in a subprocess bound to `127.0.0.1`, with `cwd` pinned to the repo root (`Path(__file__).resolve().parent.parent` in `jupyter.py`) so package resolution can't be shadowed by a same-named directory in whatever cwd the caller happens to have (see "Known Sharp Edges").
- Direct local notebooks use iframe URLs like `http://127.0.0.1:<port>/<datasource>`.
- Remote/JupyterHub notebooks use proxy URLs like `<jupyter_base>/proxy/<port>/<datasource>`.
- `PLEXORA_BASE_URL` makes Flask templates and frontend requests base-url aware (every frontend AJAX call goes through `plexoraUrl()` in `passVariablesToFrontend.js`, which reads `window.PLEXORA_BASE_URL`).
- `PLEXORA_NOTEBOOK_MODE=1` is read by `__init__.py` but currently has no header-setting behavior attached to it (see "Notebook Proxy Env-Var Propagation" below for why an earlier `X-Frame-Options: SAMEORIGIN` header tied to this flag was removed).
- `PlexoraViewer._default_data_dir()` resolves to the package's own `data/` folder (`Path(__file__).resolve().parent / "data"` in `jupyter.py`) when no `data_dir`/`PLEXORA_DATA_PATH` is given -- deliberately *not* cwd-relative, since a notebook kernel's cwd can be anywhere (unlike `run.py`'s desktop-app convention of "run from the repo root", which is genuinely cwd-relative by design).

### Notebook Proxy Env-Var Propagation (fixed; understand before touching `jupyter.py`/`proxy.py`/`server_cli.py`)

`plexora/__init__.py` snapshots `PLEXORA_BASE_URL`/`PLEXORA_DATA_PATH`/`PLEXORA_NOTEBOOK_MODE` from `os.environ` **once, at package-import time**. Both `python -m plexora.server_cli` and the `plexora-server` console script unavoidably import the parent `plexora` package (running that snapshot code) *before* `server_cli.py`'s own `main()` is even reachable -- so `main()`'s `os.environ["PLEXORA_BASE_URL"] = args.base_url` (etc.) always ran too late and was silently a no-op. This broke both JupyterHub integration paths: `PlexoraViewer(proxy=True)` and the `jupyter-server-proxy` entry point (`proxy.py`) would spawn a server that ignored the base URL and data directory they were configured with.

Fix (confirmed via live testing, not just reasoning about import order):
- `jupyter.py`'s `_start_server` passes `env=` directly to `subprocess.Popen`, setting the three vars as real OS-level env vars *before* the child process starts -- this sidesteps the snapshot entirely, since the child's first-ever `import plexora` then sees the correct values.
- `proxy.py`'s `setup_plexora()` returns an `"environment"` dict in addition to the CLI flags in `"command"` -- `jupyter_server_proxy`'s own `SuperviseAndProxyHandler.ensure_process()` already does `server_env = os.environ.copy(); server_env.update(get_env()); create_subprocess_exec(..., env=server_env)` internally, so this is the same real-env-vars-at-spawn-time mechanism, verified directly against the installed `jupyter_server_proxy` package rather than assumed.
- `server_cli.py` additionally does a post-import `app.config["PLEXORA_BASE_URL"] = _clean_base_url(args.base_url)` override (mirroring the pre-existing `PLEXORA_NOTEBOOK_MODE` override on the line above it), so a bare standalone `plexora-server --base-url X` invocation (no parent process to inject env for) also works. **Watch out**: `plexora/jupyter.py` defines its own, differently-behaved `_clean_base_url` (forces a trailing slash) -- always import the `__init__.py` one (strips it) for this override.
- `PLEXORA_DATA_PATH` has **no equivalent post-import fix** for bare standalone CLI usage: `data_path` drives `SQLALCHEMY_DATABASE_URI`/`config_json_path`, both baked in at Flask-SQLAlchemy init time before `main()` runs -- reassigning `app.config[...]` afterward can't retroactively repoint an already-constructed SQLAlchemy engine. This is an accepted residual gap -- only affects a user typing `plexora-server --data-dir X` directly in a shell, not `PlexoraViewer`/`jupyter-server-proxy`, which both fix it via the real-env-at-spawn-time mechanism above.

To verify a change in this area without a live JupyterHub: reproduce `jupyter_server_proxy`'s actual entry-point loader locally (`from jupyter_server_proxy.config import get_entrypoint_server_processes`), and separately point `PlexoraViewer(proxy=True, ...)` at a scratch data dir with an obviously-distinct `config.json` marker key to prove `data_dir` isn't landing on the right value by coincidence.

Datasource registration:

- Use `register_datasource(...)` in `plexora/datasource.py`.
- It writes/updates `config.json` under the selected `data_dir`.
- It uses `data_model.convertOmeTiff(...)` for image and segmentation metadata.
- `copy=False` stores absolute paths and is preferred for large files on remote servers.

## Multi-Modal Datasource Support (CSV / AnnData)

CSV and AnnData (`.h5ad`) are both first-class input formats, unified through an adapter layer (`server/models/adapters/`) so the ~15 gating/query functions in `data_model.py` read one common shape regardless of source format. SpatialData was designed but deferred -- not implemented.

**Schema**: `config.json[name]['data_type']` is `"csv"` when absent (zero migration for existing entries) or `"anndata"`. `load_config()`/`load_datasource()`/`centroid_tiles.py` all dispatch through `get_adapter(data_type)` rather than assuming CSV. For AnnData, `featureData[0]['dataSource']` records how the resolved `xCoordinate`/`yCoordinate`/`idField` were derived (`format`, `path`, `coordinates`, `features`, `obs_id_field`, `subset`, `apply_log_transform`) -- provenance only, no gating code reads into it directly; the flat `xCoordinate`/`yCoordinate`/`idField`/`isTransformed` keys are the single contract every downstream function reads, for either format.

**Registration**: `register_anndata_datasource(...)` in `datasource.py` mirrors `register_datasource()`'s CSV shape but resolves coordinates/features/subset via `AnnDataAdapter`. Explicit kwargs always win; nothing is silently guessed (see `isTransformed`/`apply_log_transform` below for why that matters). `derive_anndata_channel_names(image_path, features_path, n_channels)` picks image channel display names in this order: (1) `adata.var_names` if length matches the image's channel count -- checked first because gating always keys off var_names, so this maximizes auto-match; (2) embedded OME-XML channel metadata; (3) `adata.uns['all_markers']`; (4) generic `"Channel N"`. Real exemplar data has shown `var_names` (gene symbols, e.g. `PECAM1`) and `all_markers`/OME-XML (antibody names, e.g. `CD31`) can both exist for the *same* marker and never string-match each other -- expect this, don't treat it as a bug to "fix" by renaming one to match the other.

**Gating markers are never the image channel list.** This is the single most-recurring bug pattern in this codebase: `csvGatingList.js`'s `columns` and `viewerSidebar.js`'s `getGateMarkerNames()` must both be derived from `get_datasource_description()`'s output filtered to entries that actually have a `.histogram` (i.e. real feature-table/`var_names` columns), never from `dataLayer.getChannelNames()` (the image channel list from `config.imageData`). The two lists are independent vocabularies for AnnData -- different lengths, different naming conventions -- and every gating query (`get_gated_cells`, `get_gating_gmm`, `_gate_filter_columns`) already keys directly off the feature-table column name, completely independent of whether a matching image channel exists. **Selecting a gate marker with no matching image channel must still work** -- gating updates the segmentation-outline overlay/centroids exactly the same either way; only the image-channel section of the sidebar has nothing to auto-open in that case, and that's fine, expected, not an error to fix or a case to block. If a gating marker's name happens to exactly match an image channel's display name, UI code may auto-open that channel as a convenience (`viewerSidebar.js`'s `setGateMarker` mirrors into channel slot 1 only when the marker's full name is a real key in `imageChannels`, the global fullname->tile-index map built in `main.js`; `csvGatingList.js`'s `addEventsLinked()` only auto-clicks a channel-list row on an exact name match) -- but this is always opportunistic string matching with a graceful no-op fallback, **never** required/forced, and **never** positional/index matching (image channel count and marker count are frequently different sizes entirely).

**Cautionary tale (fixed, but watch for the same shape of bug recurring):** `setGateMarker`'s image-channel-match check once read `this.columns.includes(name)` -- `this.columns` is the *gate marker* list, and `name` is always drawn from that same list, so the check was a silent no-op that evaluated true for every single marker selection, matching image channel or not. The effect: selecting *any* gate marker force-hijacked channel slot 1 via `setSlotMarker` regardless of whether real image data existed for it; `activateChannel` then no-ops on a real `imageChannels[fullName] === undefined` miss, leaving that slot marked enabled/visible but rendering nothing -- so the image layer appeared to silently stop working the moment a marker without a matching channel was picked. The bug reads as "gating forces a required image channel match," which is backwards from the intended design (match is opportunistic, mismatch is a fully supported no-op) -- when touching this code, verify the check is actually testing image-channel membership (`imageChannels[fullName] !== undefined`), not marker-list membership.

**Segmentation is optional; `imageData[0]` is only the "Area" placeholder when it was actually registered.** `register_anndata_datasource()`/`register_datasource()` only prepend the `{"name": "Area", "fullname": "Area", ...}` entry to `imageData` when a segmentation file was given -- without one, `imageData[0]` is just the first real image channel (e.g. `DNA`), with a real `src` and no special meaning. Any code that slices `imageData[1:]` or subtracts a hardcoded `1` from an `imageData` index to reach the matching zarr channel is assuming segmentation always exists, and will silently drop/misindex the first real channel the moment it doesn't. This has recurred **server-side** as well as client-side -- `data_model.get_channel_names()` used to slice `imageData[1:]`, dropping the first real channel (e.g. `DNA` never appeared in the channel list at all, and `viewerSidebar.js`'s `initChannelSlots()`/`applySavedChannels()` both build off that list); `data_model.get_channel_gmm()` used to compute `image_channelIdx` as `(imageData index) - 1`, which under-indexes into `zarray` (zarray only ever holds the real channels -- Area is never part of the physical image file) by one for every channel, and goes negative for channel 0. The fix in both cases is the same: filter `imageData` by `fullname != 'Area'` and use position *within that filtered list*, never a raw `imageData` index or a slice/offset that assumes Area is always there. `get_datasource_description()`'s `image_layer` loop already does this correctly (its own counter, incremented only for non-`'Area'` entries) -- match that pattern, don't reintroduce the offset.

**The WebGL colorize pipeline's init is wired to segmentation loading -- don't let it stay that way.** `imageViewer.js`'s `initGL` (compiles the tile-colorize shader, sizes the GL canvas to the real tile dimensions, and attaches the real `tile-loaded`/`tile-drawing` handlers that actually paint channel pixels) only runs on OpenSeadragon's `"open"` event, and that event is *manually* raised (`viewer.raiseEvent("open", e.item)`, per a "Open Event is Necessary for ViaWebGl to init" comment) rather than firing on its own from `addTiledImage`. Historically the only place that ever raised it was `viewerManager.js`'s `load_label_image()` success callback -- fine when every datasource had segmentation, since that function always ran. Once segmentation became optional and `ensureSegmentationReady()` started short-circuiting on `this.noLabel` (the correct fix for the centroid-fallback behavior -- see above), `load_label_image()` stopped running at all for segmentation-less datasources, and `initGL` never fired with it: channel tiles still fetched real bytes successfully (`tile-loaded` on the OSD viewer, network layer fine) but were never GL-rendered -- the GL canvas stayed at its default `300x150` size, uninitialized, and every tile painted transparent onto a pre-filled black background, so the image layer looked completely blank while everything else (centroids, channel list, saved channel state) looked correct. Fixed by also raising `"open"` from `viewerManager.js`'s `channel_add()` success callback (idempotent -- `initGL` is safe to run more than once, per its own comment). The general lesson: don't gate a piece of one-time global initialization behind a code path that a different, unrelated feature (segmentation-optional support) is allowed to skip -- verify with `sd.viaGL.gl.canvas.width` (should match `config.tileWidth`, not the browser's default `300`) if image channels ever silently stop rendering again despite tiles loading fine.

**`viewerManager.js` and `services/glRenderer.js` are bundled into `client/dist/vendor_bundle.js` via webpack (`export class ...` syntax), not loaded as plain `<script>` tags.** Editing their source has **no effect** until you rebuild (`cd plexora/client && npm run start`, or `npm run watch` while iterating) -- unlike most other `views`/`services` files (`viewerSidebar.js`, `dataLayer.js`, `csvGatingList.js`, etc.), which are loaded directly by `base.html` and only need a `?v=` cache-bust bump. If a fix to either file doesn't seem to take effect in the browser even after a hard refresh and server restart, check whether it actually landed in the rebuilt bundle before assuming the fix is wrong.

**`idField` must be numeric-castable.** `get_all_cells()` unconditionally does `.astype(np.uint32)` on `[idField, X, Y]`. AnnData defaults `idField` to the adapter's own positional `id` column (0..n-1), not `adata.obs_names` (usually a non-numeric string). If real segmentation-mask label values are needed (so gated cell IDs correctly filter the segmentation outline overlay, which decodes per-pixel label IDs from the mask tiles), pass `obs_id_field` pointing at the real numeric label column (e.g. `"CellID"` from an mcmicro-style pipeline) -- the AnnData import UI's "Cell ID" field defaults to a column named `CellID` when present for exactly this reason.

**`isTransformed`/`apply_log_transform`: explicit only, never inferred.** `isTransformed` gates whether the gate slider/auto-gate keep float precision or round to whole numbers (`Math.floor`/`Math.ceil` in `viewerSidebar.js`). Guessing wrong is a silent, hard-to-diagnose failure mode: rounding a real narrow continuous range (e.g. an already-log-transformed `[1.85, 2.23]` gate) to integers (`[1, 3]`) makes the gate match ~100% of cells with no error and no visibly-wrong UI state -- the red gate-threshold lines even render off-canvas (outside the histogram's real x-domain) with zero visual sign anything is wrong. `register_anndata_datasource()`'s `apply_log_transform` param (surfaced as a "Log1p Transform Data" checkbox on the AnnData config page, mirroring the CSV wizard's existing `#transform-data` checkbox) is the only thing allowed to set `isTransformed=True` -- there is deliberately no mean-based or other heuristic. When checked, `AnnDataAdapter.load_table()` applies `np.log1p()` to the resolved feature values on *every* load (not a one-time file mutation like CSV's `data_model.logTransform()`, which rewrites the CSV on disk once) -- this only matters at all for a feature source that is genuinely raw/untransformed; applying it again on top of an already-transformed layer (e.g. a `log` layer) is the user's call, not something the adapter tries to detect and prevent.

**Renaming channels post-import** (fixing an auto-match miss) is done from the *viewer page*, not the import wizard: the channels-upload icon above the channel list posts a single-column CSV (one name per row, in image-channel order; a `n_channels+1`-row file has its first row dropped as a header) to `/upload_channels` -> `datasource.rename_channels()`, which writes `imageData[i].name`/`.fullname` into `config.json` and reloads the datasource (invalidating `_gmm_cache`/`_description_cache`/`_gate_filter_cache`). `channelList.js` shows a dynamic tooltip on that icon only when zero gating markers currently have an image-channel match. An earlier design put a channel-names CSV step directly in the AnnData import wizard -- removed by user request in favor of this one always-available mechanism.

**Gating/channel UI state is DB-backed, not just in-memory.** `viewerSidebar.js` autosaves the full marker/gate/channel-slot state to SQLite (`database_model.GatingList`/`ChannelList`, one row per datasource, pickled) and restores it on every page load (`applySavedGating`/`applySavedChannels`). A schema or naming change (e.g. switching what a gating column is called) does **not** retroactively fix already-saved rows -- old channel names can persist indefinitely and get replayed on load until the row is explicitly overwritten. When a gating-related bug "won't go away" despite a confirmed-correct code fix and a real server restart, check `GET /get_saved_gating_list?datasource=<name>` for stale entries before assuming the fix didn't take. See "Per-Datasource SQLite Databases" below for where that row physically lives on disk.

### Per-Datasource SQLite Databases

`database_model.py` no longer uses Flask-SQLAlchemy or a single shared `data_path/db.sqlite3`. Each datasource gets its own plain-`sqlite3` file, `data_path/<name>/<name>.db`, inside that datasource's own project folder (the same directory `register_datasource`/`register_anndata_datasource` already create). `ChannelList`/`GatingList` are now just marker classes carrying `__tablename__` -- `get(model, datasource)`/`save_list(model, datasource, cells)` are the only two real entry points (the old `create`/`edit`/`get_all`/`get_or_create` helpers were confirmed dead -- no callers outside the file -- and were removed).

- **Lazy, not eager**: a datasource's `.db` file is created the first time `get`/`save_list` is actually called for it (module import no longer does any DB I/O at all -- no datasource is even known at that point). Connections are short-lived (opened, used, closed per call) rather than a cached/pooled engine -- call frequency here is sidebar-autosave/page-load, not a tile hot path, so this avoids needing a thread-safety story on top of SQLite's own file locking (`timeout=10` on connect covers the rare concurrent-write case).
- **Legacy migration**: the first time a given datasource's new file doesn't exist yet, `database_model.py` does a best-effort, read-only lookup against the old shared `data_path/db.sqlite3` (if present) for that datasource's row and copies it forward. The legacy file is never deleted or written to. See the data-root note above for when it's safe to remove.
- **Corruption recovery is reactive, not proactive**: there's no more import-time `PRAGMA integrity_check` (there's nothing to check until a datasource is known). Instead, `_connect()` tries to open+use the file normally and only on `sqlite3.DatabaseError` renames it aside (`<name>.db.corrupt-<timestamp>`) and recreates fresh. On Windows, the failed connection must be `.close()`d *before* the rename or it fails with `PermissionError: [WinError 32]` (a handle still open on the file) -- this bit a first draft of this code and was caught by `tests/test_database_model.py::test_corrupted_db_file_is_recovered`; keep that close-before-rename ordering if you touch `_connect`.
- `Flask-SQLAlchemy` was removed as a dependency (`pyproject.toml`, `requirements.yml`, `requirements-dev.lock.txt`) alongside this, following the same "confirmed genuinely unused, remove it" precedent as the `requests`/`pandas` removals below -- it had no purpose in the app beyond these two models. A stray, unused `from flask_sqlalchemy import SQLAlchemy` import in `data_routes.py` had to be deleted in the same change, since it would otherwise break `import plexora` (imported unconditionally from `__init__.py`) the moment the dependency was actually gone.

Relevant tests: `test_csv_adapter.py`, `test_anndata_adapter.py`, `test_derive_anndata_channel_names.py`, `test_register_anndata_datasource.py`, `test_rename_channels.py`, `test_centroid_tiles.py`, `test_datasource_config_routes.py`, `test_database_model.py` (61 tests total in `tests/` as of this writing, all passing -- run `conda run -n plexora python -m pytest tests/ -q`).


## OpenSeadragon Integration

OpenSeadragon is a real, current npm dependency (`client/package.json`'s `openseadragon`, currently `^6.1.0`) with matching `@types/openseadragon`. It used to come in through a chain of personal GitHub forks (`viawebgl` → a pinned-commit fork of OpenSeadragon reporting as 2.3.1) that existed for one reason: exposing raw AJAX tile bytes so the app could decode true 16-bit pixel data itself instead of losing precision through the browser's built-in 8-bit PNG decode. That fork chain was removed; if you see any reference to `viawebgl`, `thejohnhoffer/openseadragon`, or `window.viaWebGL` in old branches/history, treat it as gone, not current.

- **Raw tile bytes**: `imageViewer.js`'s `handleTileLoaded` reads `e.tileRequest.response` (an `ArrayBuffer`) directly off the `tile-loaded` event — OpenSeadragon 6.x exposes the underlying XHR there as a stable, non-deprecated property, and uses `responseType: "arraybuffer"` for AJAX-loaded tiles, so no fork or custom `OpenSeadragon.converter` registration is needed. It's registered as an `async` function; OpenSeadragon awaits a handler's returned promise (`raiseEventAwaiting`) instead of needing an explicit `getCompletionCallback()`.
- **WebGL colorize/threshold pass**: `client/src/js/services/glRenderer.js` (`GLRenderer` class) is a self-contained, OpenSeadragon-independent WebGL2 engine (shader compile/link, texture upload). `imageViewer.js` drives it directly via `viewer.addHandler('tile-drawing', ...)`, compositing the WebGL output onto the tile's 2D canvas.
- **`drawer: 'canvas'` is required** in the `viewer_config` passed to `OpenSeadragon(...)` in `imageViewer.js`. The per-tile WebGL compositing depends on the `tile-drawing` event's 2D `rendered` canvas context, which is only guaranteed under the canvas drawer — OpenSeadragon 6's newer WebGL Drawer has no documented custom-shader hook as of 6.1. Don't change this to `'webgl'` or `'auto'` without re-verifying that assumption against whatever OpenSeadragon version is current at the time.
- **Vendored plugins**: only `canvas-overlay-hd.js` (lasso/centroid overlay, `OpenSeadragon.CanvasOverlayHd`) and `openseadragon-scalebar.js` (`viewer.scalebar(...)`, `scalebarInstance.getImageWithScalebarAsCanvas()` for the download-view export) remain in `client/external/openseadragon-bin-2.4.0/`. Both are unmaintained third-party single-file plugins with no npm equivalent (confirmed via `npm view` — 404), vendored in-repo rather than pulled from a live fork; both currently work against OpenSeadragon 6.x with zero patches. `openseadragon-svg-overlay.js`, `openseadragonrgb.js`, and `openseadragon-filtering.js` were deleted — confirmed zero references anywhere in the app, and the RGB one was actually crashing page load under 6.x (it patched a `Drawer` internal that no longer exists).
- If a future OpenSeadragon upgrade breaks tile rendering, the debugging order is: (1) confirm `drawer: 'canvas'` is still in effect, (2) confirm `tile-loaded` still exposes `e.tileRequest.response` the same way, (3) check the two vendored plugins against whatever internals changed.
- **Fullscreen**: OSD's own default toolbar "full page" button (`setFullPage()`) reparents just the `#openseadragon` element to `<body>` and resizes only that -- it never touches `#viewer_sidebar` (a DOM sibling inside the `#bodyDiv.viewer-shell` grid), so clicking it only fullscreens the canvas, not the sidebar. `imageViewer.js` intercepts OSD's `pre-full-page` event (`event.preventDefaultAction = true`) and instead toggles the native browser Fullscreen API on `#bodyDiv`, so the whole app shell goes fullscreen together. If OSD's fullscreen behavior needs revisiting, start there rather than re-enabling OSD's own button.

## Data Layer: Polars, Not Pandas

`data_model.py`'s `datasource` global (the feature-CSV-backed cell table) and every other DataFrame in this codebase are Polars, not pandas — pandas was fully migrated away and removed as a dependency. Non-obvious things worth knowing before touching this:

- **`id` column**: manufactured via `datasource.with_row_index("id")` immediately after `pl.read_csv(...)`, before any other transform — mirrors the old pandas code's `df['id'] = df.index` trick (a stable positional identity), since nothing in the app sorts/reindexes/samples `datasource` after load. Unlike the old pandas version (where `id` was appended as the *last* column), Polars' `with_row_index` prepends it as the *first* column — this changes CSV export column order (visible in `/download_gating_csv` output) but not correctness, since every consumer accesses columns by name, not position.
- **NaN vs. null**: Polars distinguishes `null` from float `NaN`; pandas' `pd.to_numeric(col, errors="coerce")` produced `NaN` for unparseable values, and downstream code (`_apply_gate_mask` in `data_model.py`, `_apply_gates` in `centroid_tiles.py`) relies on `np.isnan`/`np.isfinite` semantics. Every place a numeric column is extracted to numpy uses an explicit `.cast(pl.Float32, strict=False).fill_null(float("nan")).to_numpy()` pattern rather than relying on Polars' default null-to-NaN export behavior — keep this pattern for any new extraction rather than a bare `.to_numpy()`.
- **`download_gating_csv` dtype parity**: when writing gate-encoded values into an existing float column, the value literal is explicitly cast to that column's original dtype (`csv.schema[channel]`) before the `pl.when/then/otherwise`. Without this, Polars renders an int literal as `"1"` in the CSV where pandas' implicit upcast used to render `"1.0"` — a real text diff downstream tools might depend on.
- **`download_gating_csv` empty-gates behavior changed on purpose**: the old pandas `.query('')` raised `ValueError` when called with zero gates set. The Polars rewrite treats empty gates as "no filter" (empty `ids`, no crash) — a deliberate behavior fix made during the migration, not an oversight.
- **No `.loc`-style in-place mutation**: `download_gates`/`save_gating_list`/`download_channels`/`save_channel_list` build small per-channel export DataFrames using `pl.when(...).then(...).otherwise(...)` inside `with_columns(...)` instead of pandas' `.loc[mask, col] = value`, since Polars frames are immutable. The lasso-row-append path in `download_gates`/`save_gating_list` is confirmed dead in live usage (lasso was removed; `imageViewer.js` permanently sets `list_lassos = {}`) but was still migrated correctly (build rows, `pl.concat(..., how="diagonal_relaxed")`) rather than left as pandas leftovers.
- Do not reintroduce pandas. If a new feature needs CSV/DataFrame work, use Polars and follow the patterns above.


## Common Tasks And Where To Work

For notebook support:

- Start in `plexora/jupyter.py`, `plexora/server_cli.py`, `plexora/proxy.py`, and `plexora/__init__.py`.
- Then check `client/templates/base.html` and URL construction in frontend services.
- Preserve `python run.py` behavior while changing notebook/server-proxy behavior.

For PyPI packaging:

- Start in `pyproject.toml`, `MANIFEST.in`, and package data under `plexora/client`.
- Use `uv build` as the canonical package build.
- Verify the built wheel from outside the repo so imports come from `site-packages`, not the checkout.
- Ensure templates, `client/dist/vendor_bundle.js`, shaders, CSS, images, and external OpenSeadragon assets are included.

For tile or segmentation bugs:

- Start with browser console URLs and `server/routes/data_routes.py`.
- Then inspect `server/models/data_model.py`, especially OME/zarr level selection, channel names, label image handling, and generated tile paths.
- On the frontend, inspect `client/src/js/services/dataLayer.js` and `client/src/js/views/imageViewer.js`.
- If the bug is specifically in tile decoding/colorizing (wrong colors, blank/black tiles, WebGL errors), see "OpenSeadragon Integration" above — start with `imageViewer.js`'s `handleTileLoaded`/`tileDrawingCustom` handlers and `client/src/js/services/glRenderer.js`.
- Be careful with cache behavior: a symptom that only resolves after hard refresh can be frontend cache ordering, stale bundle, or request timing.

For gating/nearest-cell/query behavior:

- Server side: `server/routes/data_routes.py`, `server/models/data_model.py`, `server/models/database_model.py`.
- Frontend side: `client/src/js/views/csvGatingList.js` and `client/src/js/views/viewerSidebar.js`. Gating is marker-threshold/GMM-based only (slider ranges per channel, auto-gate via `getGatingGMM`/`getChannelGMM`) -- there is no spatial/lasso selection tool. For AnnData datasources specifically, read "Multi-Modal Datasource Support" above first -- gating markers and image channels are different vocabularies and mixing them up is the most common source of "gate does nothing"/`ColumnNotFoundError` bugs here.
- "Nearest cell" in the live app is only the click-to-inspect lookup (`dataLayer.getNearestCell()` -> `GET /get_nearest_cell`), triggered from `imageViewer.js`. There is no separate neighborhood/channel-relationship analysis view — an earlier `lensingFilters/*` module implementing that was found to be dead code (never imported by `main.js`) and was removed; do not recreate features assuming it still exists.
- Lasso/spatial-region selection (freeform polygon drawing on the image, `draw_lasso`/`toggle_lasso`/`delete_lasso`/`get_cells_in_polygon`/`get_cells_in_lassos`) was a real, wired-in feature that has since been **removed by user request** (unused). Saved/exported gate lists may still contain legacy `channel == 'Lasso'` rows from before the removal — `csvGatingList.js` `applyGates()` intentionally skips them rather than erroring. `imageViewer.js`'s `list_lassos` property is kept (always empty) purely so `saveGatingList`/`downloadGatingCSV` keep a stable call signature; don't read that as lasso still being supported.
- Confirm CSV download payloads and query endpoints after changes.

For frontend dependency or UI work:

- Work in `plexora/client`.
- Run `npm install` after dependency changes.
- Run `npm run start` to regenerate `client/dist/vendor_bundle.js`.
- Browser tests are legacy and may fail old behavioral assertions; do not assume `npm test` is fully green without checking current notes. As of this writing the known-stable baseline is 2 passing / 3 failing (`Ensure visual rendering must load a mask`, `Ensure download ranges must download channel ranges`, `Ensure download encodings must download cell encodings`) — verified as pre-existing/unrelated to recent dependency work by running the same suite against both the old and new dependency versions and getting identical results. Treat only *new* failures beyond these three as real regressions.
- `karma-jquery` (the test harness's jQuery-serving plugin) only bundles jQuery up to 3.4.0 and has no 4.x build, so `karma.conf.js`'s `frameworks: [..., 'jquery-3.4.0']` stays pinned to 3.4.0 even though the real app runs jQuery 4.x. This is a test-infra-only gap, not a product bug — don't try to "fix" it by downgrading the app's jQuery.
- Files loaded as plain `<script>` tags in `base.html` (e.g. `imageViewer.js`, `viewerManager.js` is the exception — it's `import`ed into `vendor.js`) are NOT processed by webpack/Babel and cannot use `import`/`export` syntax. Anything they need from an npm package must be exposed as a `window.X` global from `vendor.js` first (see how `GLRenderer`, `ViewerManager`, `OpenSeadragon`, `$`, `d3`, etc. are attached there).

For Python dependency modernization:

- Prefer Python 3.13. Python 3.12 is the fallback target.
- Use the `plexora` conda env for local work.
- Use uv for pip resolution/builds, not hand-edited lock files.
- Keep `requires-python = ">=3.12,<3.14"` unless a real dependency forces a narrower range.

## Validation Commands

Run from the repository root unless noted.

Core Python baseline:

```powershell
conda run -n plexora python -m tests.baseline_orion2
```

The baseline datasource is configurable via `PLEXORA_BASELINE_DATASOURCE` (defaults to `orion2`). **On macOS, `orion2`'s referenced files live only on the Windows machine this repo is Dropbox-synced with, so those tests just skip.** The locally-populated datasource on Mac is `orion_mac` — use it instead:

```bash
PLEXORA_BASELINE_DATASOURCE=orion_mac python -m tests.baseline_orion2
```

All 4 tests pass with `orion_mac` (verified). An earlier version of `tests/baseline_orion2.py` hardcoded the tile-check URL to the `orion2` datasource regardless of `PLEXORA_BASELINE_DATASOURCE`; that's been fixed — the test now builds the URL from the `DATASOURCE` variable like the rest of the suite. If you see all 4 pass, that's the expected/healthy state, not a fluke.

Python import/compile sanity:

```powershell
conda run -n plexora python -m compileall -q plexora tests
```

Local server:

```powershell
conda run -n plexora python run.py
```

Open:

```text
http://localhost:8000/orion2
```

On macOS, use `http://localhost:8000/orion_mac` instead — `orion2`'s image/segmentation files are only present on the Windows side of this Dropbox-synced repo.

Frontend build:

```powershell
cd plexora/client
npm run start
```

Frontend tests:

```powershell
cd plexora/client
npm test
```

Known caveat: after the modernization work, TypeScript and Webpack/Karma bundling worked, but several legacy browser assertions were still failing. Treat those as a separate test-maintenance task unless the current branch has fixed them.

Package build:

```powershell
conda run -n plexora uv build
```

Package install probe:

```powershell
conda create -n plexora_piptest -c conda-forge python=3.13 pip
conda activate plexora_piptest
python -m pip install --upgrade pip
python -m pip install dist/plexora-<version>-py3-none-any.whl
python -c "from plexora.jupyter import PlexoraViewer; print(PlexoraViewer.__name__)"
plexora-server --help
```

When testing wheel imports, run Python from outside the repo. If cwd is the checkout, Python may import the local package instead of the installed wheel.

Notebook smoke:

```python
from plexora.jupyter import PlexoraViewer

PlexoraViewer(datasource="orion2", data_dir="path/to/plexora_data")
```

Remote/JupyterHub smoke:

```python
PlexoraViewer(datasource="orion2", data_dir="path/to/plexora_data", proxy=True)
```

## Important Invariants

- `python run.py` must keep working for existing desktop/Docker users.
- Notebook support should remain iframe-backed and server-proxied, not a pure ipywidget rewrite.
- The sidecar server should bind to `127.0.0.1`; Jupyter proxy provides authenticated browser access.
- Datasource registration should not copy large OME-TIFF/zarr files by default.
- Do not break absolute-path datasets in `config.json`; many remote datasets will live outside the package directory.
- Keep package data complete. A pip-installed wheel must serve templates, built JS, shaders, CSS, images, and OpenSeadragon external files.
- Avoid committing generated local data/build artifacts.
- Treat `server/models/data_model.py`, `client/src/js/views/imageViewer.js`, and `client/src/js/services/glRenderer.js` as high-risk: small changes can affect tile rendering, segmentation visibility, zoom behavior, and analysis queries. Any OpenSeadragon version bump needs the full re-verification described in "OpenSeadragon Integration" above, not just a `package.json` bump.
- If changing URL construction, test both root mode `/` and proxied notebook mode `/proxy/<port>/`.
- If changing segmentation, test both zoomed-out and zoomed-in display, first normal page load, and browser hard-refresh behavior.

## Known Performance Hot Spots

A dedicated pass fixed most of what was originally found here (frontend O(n²)/O(n) main-thread loops, backend caching/warmup, tile-cache eviction), plus a full pandas→Polars migration of the data layer (see "Data Layer: Polars, Not Pandas" above). What's fixed vs. what remains an accepted gap:

Fixed:
- `client/src/js/views/imageViewer.js` `loadBuffers()`: the modulo-`.filter()` deinterleave when activating a new marker/gating channel was O(nNew² × cellCount); now a single linear pass.
- `client/src/js/services/numericData.js` `fetchCells()`: the double full-array `.filter()` deinterleave (ids/centers) is now a single linear pass.
- `server/models/data_model.py` `get_channel_gmm`/`get_gating_gmm`: both are memoized (`_gmm_cache`, keyed by datasource/channel/selection) and pre-warmed in a background thread after each `load_datasource` call, so most real requests hit a warm cache instead of refitting. `get_gating_gmm` additionally caps its fit input at a random 100,000-row subsample (fixed seed, deterministic) when the cell-level column exceeds that — EM cost scales ~linearly with N, and a 2-component 1D mixture's fitted parameters barely move between 100k and millions of samples. `get_channel_gmm` is intentionally NOT capped — it already fits on the pre-block-reduced zarray (~40k points), already under the cap.
- `server/models/data_model.py` `get_datasource_description`: memoized (`_description_cache`), pre-warmed in the same background thread, and `describe()`'s per-column loop is one vectorized numpy pass (`_describe_numeric`) instead of a per-column loop.
- `client/src/js/views/imageViewer.js` `clearTileCache()`: the 30s watchdog no longer force-clears every active channel's entire tile pyramid when the shared 1000-tile budget is hit. `evictLeastRecentlyUsedTiles(perItemBudget)` evicts per-`TiledImage`, oldest-touched-first (via OpenSeadragon's own `tile.lastTouchTime`), so activating many channels doesn't cause periodic full-pyramid flicker across all of them at once. The segmentation-only clear on `ensureSegmentationReady()` (`clearTileCache(true)`) is unrelated and untouched.
- `server/models/data_model.py` `load_ball_tree`: no longer does its own redundant second CSV read — builds the BallTree from the already-loaded `datasource` DataFrame. The BallTree pickle cache is validated against the source CSV's size/mtime, not just file existence.
- `server/models/data_model.py` `get_channel_cells`/`get_gated_cells`/`get_gated_cells_custom`: no longer build/eval a string query over the full cell table per request — vectorized numpy masking via `_gate_filter_columns`/`_apply_gate_mask`, modeled on `centroid_tiles.py`'s `_load_filter_table`/`_apply_gates` pattern.
- `server/models/data_model.py`/`server/routes/data_routes.py` `download_gating_csv`: no more redundant full-frame copies; the CSV response streams in chunks instead of materializing the whole serialized string in memory at once.
- `server/models/data_model.py` `generate_zarr_png` tile serving: encoded PNG bytes are cached (bounded in-process LRU in `data_routes.py`, keyed on `data_model.load_generation` so a datasource reload invalidates it), so panning back over previously-viewed tiles doesn't re-decode/re-encode from zarr every time.

Still an accepted gap (deliberately not fixed):
- `load_datasource`/`load_ball_tree` still hold exactly one datasource's state in bare module globals behind a single `load_lock`. Concurrent requests touching two *different* datasources (or a background cache-warmup thread racing the very first request after a load) can still transiently read inconsistent state — reproduced once during testing (a `get_gated_cell_ids` call briefly returned unfiltered results immediately after server startup, then was consistent on every retry after). Fixing this for real requires caching multiple datasources' state simultaneously (a dict keyed by name, touching ~30 read sites in the highest-risk file) — deliberately scoped out as too large a change to bundle with other work; revisit only as its own dedicated task if concurrent multi-datasource access becomes a real requirement.
- Any handler bound to a high-frequency event (`mousemove`, zoom, brush) that fires a network request needs a real debounce/throttle, not just a `loading` boolean guard. `updateVisibleCentroidTiles`/`scheduleCentroidTileUpdate` (`imageViewer.js`) and `scheduleSegmentationForGate` (`main.js`) already do this correctly — copy that pattern rather than re-deriving it.

All of the above were CPU-bound/caching/algorithmic problems, not I/O-concurrency problems — switching web frameworks would not have fixed any of them without the same caching/memoization/vectorization work, since Python's GIL means synchronous numpy/sklearn/Polars work blocks a worker either way.

## Current Dependency Policy

Python:

- Primary target: Python 3.13.
- Fallback: Python 3.12.
- Package metadata allows `>=3.12,<3.14`.
- Flask stack is modernized to Flask 3.x.
- Scientific stack is modernized around NumPy 2.x and current Polars/scikit/skimage/tifffile/zarr.
- zarr is currently allowed as `>=3`; zarr/OME paths remain high-risk and need baseline tile tests after changes.
- Before adding a new Python dependency, check whether it is already pulled in transitively (`pip show <pkg>` in the `plexora` env shows `Required-by`). Several declared dependencies have no direct `import` anywhere in the codebase but are still required: `imagecodecs` (used internally by `tifffile` for less-common TIFF codecs), `pydantic` (hard dependency of `ome-types`), `jupyter-server-proxy` (discovered via the `[project.entry-points.jupyter_serverproxy_servers]` entry point, not an import), `xmlschema` (imported in `__init__.py` only to force PyInstaller to bundle it). Don't remove these just because grep finds no import.
- `requests` was removed as a genuinely unused dependency (no import anywhere, `pip show requests` had no `Required-by`) from `pyproject.toml`, `requirements.yml`, and `requirements-dev.lock.txt`.
- `pandas` was fully migrated to Polars and removed as a dependency (`pyproject.toml`, `requirements.yml`, `requirements-dev.lock.txt`) — confirmed `pip show pandas`'s `Required-by:` was `plexora` only (not a transitive dependency of anything else) before removal, and the full test suite (`baseline_orion2`, `tests/test_centroid_tiles.py`) plus live endpoint checks were re-run with pandas fully uninstalled from the `plexora` env to confirm. See "Data Layer: Polars, Not Pandas" above for what changed.
- `Flask-SQLAlchemy` was removed the same way (`pyproject.toml`, `requirements.yml`, `requirements-dev.lock.txt`) once `database_model.py` moved to per-datasource plain-`sqlite3` files — see "Per-Datasource SQLite Databases" above. Confirmed genuinely unused beyond that one file via a whole-repo grep for `SQLAlchemy`/`db.session`/`ChannelList`/`GatingList` first.
- `requirements-dev.lock.txt` must be regenerated with `uv`, never hand-edited: `uv pip compile pyproject.toml --extra jupyter --extra dev --prerelease disallow --universal --python-version 3.12 -o requirements-dev.lock.txt`. The `--universal` flag is required — without it, `uv` resolves only for the machine it's run on and silently drops the other platform's markers (e.g. regenerating on macOS previously dropped the Windows-only `pywin32-ctypes`/`pywinpty`/`pefile` entries that `pyinstaller` needs on Windows, since this repo is used from both Windows and macOS via Dropbox sync). The `--python-version 3.12` flag became necessary at some point after this doc was first written: in `pip compile` mode (as opposed to `uv lock` project mode), `uv` 0.8.14 does not reliably read `requires-python` from `pyproject.toml` and instead falls back to trying to resolve for Python `>=3.10` by default, which now fails outright because `tifffile>=2026.7.31` itself requires `>=3.12` — reproduced against an unmodified checkout, so this is environment/tooling drift, not a project regression. Without the flag you'll see `No solution found when resolving dependencies for split (markers: python_full_version == '3.11.*' ...)`. **Known gap as of this writing**: running the real regenerate command currently also pulls in a large, unrelated diff — `anndata` (already a direct dependency in `pyproject.toml`) is missing from the checked-in lock entirely, so a real regeneration also resolves anndata's full transitive tree (`pandas`, `h5py`, `natsort`, etc. — pandas back in transitively, just not as a direct plexora import). The Flask-SQLAlchemy removal above was applied as a targeted hand-edit (removing just the `flask-sqlalchemy`/`sqlalchemy`/`greenlet` lines) instead, specifically to avoid bundling that unrelated fix into an unrelated change. Regenerating the lock for real (picking up `anndata` correctly) is a legitimate follow-up task, just not one to fold silently into something else.

Frontend:

- Webpack 5 is used.
- Bootstrap is 5.3.8 (upgraded from 4.6.2), paired with `@popperjs/core` ^2.x (not the old `popper.js` v1). Bootstrap 5 removed `.form-group`, renamed `.ml-*`/`.mr-*` → `.ms-*`/`.me-*`, and `data-toggle` → `data-bs-toggle`; if you find code still using the old names it's a leftover, not intentional.
- jQuery is 4.0.0 (upgraded from 3.7.x). Bootstrap 4 has a hard runtime guard that throws if it detects jQuery ≥4, which is why the Bootstrap and jQuery upgrades had to land in one commit together — they are not independently revertible.
- Babel toolchain is 8.x (`@babel/core`, `@babel/preset-env`, `@babel/plugin-transform-runtime`, `@babel/plugin-transform-class-properties`, `@babel/preset-typescript`, `@babel/runtime` — keep these in lockstep, they're released together). Babel 8 defaults to browserslist-resolved compile targets and ESM output instead of ES5/CJS; `client/.browserslistrc` pins an explicit modern target (Chrome/Firefox/Edge ≥100, Safari ≥15) instead of riding Babel's shifting default. Babel 8 packages declare `engines: node ^22.18.0 || >=24.11.0` — an older local Node (e.g. 22.14.x) produces `EBADENGINE` warnings on `npm install` but has not caused build/test/runtime failures; don't treat that warning alone as a blocker.
- OpenSeadragon is a real npm dependency at 6.1.0 (see "OpenSeadragon Integration" above) — it used to come from a personal-fork chain (`viawebgl`), which has been fully removed.
- D3 is 7.x, FontAwesome is 7.x.
- Browser-side `node-fetch` was removed in favor of native `fetch`.
- The source is not React. Do not describe or treat it as a React app.

## Known Sharp Edges

- `uv build` can create `plexora.egg-info/` and versioned unpack directories. These are generated.
- Building or installing from the live Dropbox checkout on Windows may hit file-lock issues. A clean temp archive/clone is a better PyPI simulation.
- Running import probes from the repo root can accidentally import the checkout instead of the wheel.
- This checkout is synced via Dropbox between at least one Windows machine and one macOS machine. `plexora/data/config.json` has separate local datasources per machine — `orion2` (Windows-only files) and `orion_mac` (macOS-local files); `orion` also exists. Use `orion_mac` for local testing/viewing when working on macOS. Two more symptoms of the cross-machine sync to expect and not misdiagnose as real changes:
  - Files can get silently flipped from LF to CRLF line endings (or back) with zero content change, making `git status`/`git diff` show huge diffs on files nobody intentionally edited. Before editing or reviewing a file that shows as heavily modified, check with `git diff --ignore-space-at-eol -- <file>`; if that is empty, it is pure line-ending noise. When you do need to edit a CRLF-flipped file, the `Edit` tool's exact-string match can fail against `\r\n` content — fall back to a small Python script that edits the raw bytes and writes them back with `\n`.join(...) to avoid re-flipping the whole file back to LF as a side effect.
  - Executable bits on synced files (notably `plexora/client/node_modules/**/bin/*` after `npm install`) can get stripped, causing `npm run start` to fail with `Permission denied` on `webpack`. `chmod +x` the specific binary; `node_modules` is gitignored so this never touches tracked files.
- `conda run -n plexora ...` can fail with `permission denied` from the `__conda_exe` shell function if the invoking shell's `$CONDA_EXE` env var is stale/unset (seen in non-interactive tool shells). If that happens, call the env's Python directly instead, e.g. `~/miniconda3/envs/plexora/bin/python -m compileall ...`, rather than assuming the environment itself is broken.
- `requirements.yml`'s internal `name:` field says `plexora`, matching the conda env name used for local work (`conda env create -f requirements.yml` creates/targets an env called `plexora`; this also runs `pip install -e .[jupyter,dev]`, since that's baked into the yml's `pip:` section, so no separate install step is needed).
- Missing segmentation tiles may show as browser console messages like `/generated/data/<dataset>/<label-channel>/<level>/<x>_<y>.png`. Confirm whether the tile is truly absent, computed lazily, or blocked by stale frontend cache.
- A segmentation overlay that appears only after hard refresh suggests frontend cache/timing/base-url behavior, not necessarily bad source data.
- `plexora/data/` is local runtime data. It may contain large datasets and should not be swept into commits.
- Any bare directory literally named `plexora` sitting somewhere on `sys.path` (most commonly one accidentally created by running something with `PLEXORA_DATA_PATH` unset from an unexpected cwd, since `__init__.py`'s cwd-relative fallback does `Path("plexora/data").mkdir(...)`) will silently shadow the real installed package as an empty PEP 420 namespace package for any `python -m plexora.<submodule>` invocation whose cwd resolves there first -- surfaces as `ImportError: cannot import name 'app' from 'plexora' (unknown location)`. Diagnose with `python -c "import plexora; print(plexora.__file__)"` from the suspect cwd (`None`/missing `__file__` confirms it); fix is deleting the stray directory (safe if it's untracked/gitignored -- check `git status` first) or avoiding that cwd. `jupyter.py`'s `_start_server` already pins `cwd` to the repo root for its own subprocess specifically to prevent this class of bug from recurring there.
- Existing uncommitted changes may be user work. Do not revert them unless explicitly asked.
- Every script tag loaded via `base.html` (~13 of them, including `csvGatingList.js`/`viewerSidebar.js`/`channelList.js`/`dataLayer.js`) shares one static `?v=<tag>` cache-busting query string, and `datasource_config.html` has its own separate one for `datasourceConfig.js`. Neither is auto-generated from file hashes -- after editing any file loaded this way, bump its `?v=` tag(s) or the browser can keep serving pre-edit JS indefinitely (surviving multiple server restarts, since server restarts don't touch the browser's cache at all). A user reporting "I restarted and it's still broken" after a real, verified-correct fix should prompt checking this before re-diagnosing the original bug.
- A datasource's `.db` file (`data_path / <name> / <name>.db`, holds that datasource's `ChannelList`/`GatingList` row -- see "Per-Datasource SQLite Databases" above) can in principle be silently orphaned the same POSIX-delete-while-open way older shared `db.sqlite3` could: if the file is deleted (e.g. moved to Trash) while a connection is mid-call, that connection keeps reading/writing the deleted inode until it closes, while any *new* call opening the same path gets a fresh, empty file -- the two are now completely disconnected despite sharing a filename. The blast radius is much smaller than the old shared-file design (only one datasource is affected, and connections are short-lived/per-call rather than held open for a process's whole lifetime, so the window where this can happen is brief), but the failure mode is the same if it does happen: reads through the stale handle keep returning old data, writes can start failing with generic errors. Diagnose with `lsof` (macOS/Linux) and look for the db file resolving to `~/.Trash/...` or `(deleted)`; fix is retrying the call (a short-lived connection means the *next* call reopens the real path fresh -- no process restart needed, unlike the old design).

## Git And Release Notes

- Main remote for this repository is `origin` at `https://github.com/nirmallab/plexora.git`.
- Check branch and remote before pushing.
- Check `pyproject.toml` (`version = ...`) and `plexora/client/package.json` (`"version"`) for the current package version before referencing it. They have drifted before (e.g. `pyproject.toml` at `1.0.8` while `package.json` stayed at `1.0.2`) — always re-check both rather than assuming they match or trusting a previously-noted pair of numbers.
- For PyPI readiness, prefer this order:
  1. Run `python -m tests.baseline_orion2`.
  2. Run `npm run start` if frontend changed.
  3. Run `uv build`.
  4. Install the generated wheel into a fresh env and import from outside the repo.
  5. Verify `plexora-server --help`.

## Agent Operating Notes

- Read the relevant server and frontend files before changing behavior; this project has coupled Python/JS paths.
- Keep edits narrow and preserve old usage paths unless explicitly migrating them.
- Use `rg`/`rg --files` for code discovery.
- Use `apply_patch` for hand edits.
- Before committing, inspect `git status --short` and avoid generated artifacts.
- When reporting results, mention which validation commands were actually run and which were not.
