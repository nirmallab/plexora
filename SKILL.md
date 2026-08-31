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

- Desktop: `plexora` (`plexora/cli.py`), or `python -m plexora`. Starts
  Waitress, picks a free port if 8000 is taken, opens a browser when the
  environment looks interactive.
- Housekeeping subcommands: `plexora where`, `plexora config show|set`.
  Dispatched on `argv[0]` by `cli.split_command`, NOT by an argparse
  subparsers action — see Key Invariants.
- Remote over SSH: `plexora --remote` on the server prints the tunnel command
  (scheduler-aware: two-hop `ssh -J` inside a SLURM/PBS/LSF job);
  `plexora connect user@host [--srun "…"]` runs locally and automates both
  ends (`plexora/connect.py`). Every connection also starts a data node on
  THIS (the user's own) machine by default -- `--no-local-node` opts out --
  because that is what lets a data-selection field's "Local" option mean the
  laptop the browser is running on. `plexora node serve --dynamic` lets the
  viewer add/remove resources on a node at runtime instead of only serving
  what `--serve` named at startup; `--manifest PATH` records what it ends up
  serving so it comes back the same way next session (a dynamic node with a
  `--node-id` and no `--manifest` defaults to `<data_root>/node-manifests/<id>.json`
  **on its own machine**, since an ssh command line cannot name a data root it
  has never seen).
- The mirror layout, and the one a data field's Remote option opens:
  `connect.NodeSession` keeps Plexora *here* -- with the browser, the project
  and the database -- and starts only a `--dynamic` node on the far side,
  forwarded with one `ssh -L` and registered into this machine's own
  `nodes.json`. No `srun`: it wants the filesystem, not an allocation.
- Notebook: `plexora.view("name")` → `plexora/jupyter.py`. `proxy="auto"` by
  default; `plexora/notebook_env.py` decides between a direct localhost URL, an
  Open OnDemand `/rnode/` mount, a jupyter-server-proxy path, and a Colab
  origin.
- Open OnDemand: `plexora --ood` from a session terminal — binds 0.0.0.0,
  mounts under `/rnode/<node>/<port>`, prints a token-bearing portal URL.
- CLI sidecar: `plexora-server` (`plexora/server_cli.py`) — what the notebook
  and the proxy entry point spawn, not something a user runs.
- Legacy/local desktop: `python run.py`. Still what the Docker image runs.
- Frontend build: `cd plexora/client && npm run start`

## Repository Map

**Top level**

| Path | Purpose |
|---|---|
| `run.py` | Legacy/local desktop entry point. Keep working. |
| `plexora/server_cli.py` | Notebook sidecar CLI (`plexora-server`). Waitress, `threads=8`. |
| `plexora/__init__.py` | Flask app factory; base URL, notebook flag, plugin installation, the `PLEXORA_AUTH_TOKEN` guard (`AUTH_COOKIE`), and the app-wide `ResourceUnavailable` handler (503 + `_say_unavailable_once`). Holds **no** path constants -- see `plexora/paths.py`. |
| `plexora/paths.py` | The one resolver for every path. `data_root()` (env -> settings file -> frozen -> platformdirs), `shared_roots()`, `roots()`, `config_path()`, `project_dir()` (read side), `project_state_dir()` (write side, always the user's root), `derived_root()`, `figures_root()`. Leaf module: imports nothing from `plexora`. **Never snapshot these into a module constant** -- that is exactly what was removed, and it is what made `--data-dir` unreachable after the first `import plexora`. |
| `plexora/cli.py` | The `plexora` command: serve, `where`, `config`, `connect`, `node`, `--remote`, `--ood` (`ood_mount`, `ood_instructions`). Also the **environment detection** a bare `plexora` runs: `should_detect` (gate), `detect_environment` (lazy, never raises), `apply_detection` (verdict -> flags), `detected_base_url`, `hub_instructions`, `colab_instructions`, `--no-detect`. And `connect_kwargs` (flags beat a saved profile), `node_serve_argv`/`_start_side_node` (`--also-serve`). **Imports nothing from the `plexora` package at module level** -- see Key Invariants. Keeps its own copies of `REMOTE_ENV_VARS`, `PORT_PLACEHOLDER` and `DEFAULT_REMOTE_COMMAND`, pinned against the originals by `tests/test_cli.py`. |
| `plexora/connect.py` | Local side of `plexora connect`: builds ssh argv, runs one process (direct) or two (`--srun`: job + tunnel), health-polls through the tunnel. `Session` holds one connection -- `establish()` is separate from `wait()` so the app can own a connection a request does not block on. `_Watched` takes a dict of `matchers` (a viewer that starts a node announces twice on one pipe). `_ssh_options` prepends `KEEPALIVE_OPTIONS` (`ServerAliveInterval=30`, `ServerAliveCountMax=3`, deduped when the caller already set the interval) to every ssh invocation, so a dead tunnel becomes an exit somebody can see instead of a hang. `_wait_for_health` takes `any_answer=True`, used ONLY at the viewer call site: an HTTPError with `code < 500` counts as proof of life, because a token-guarded remote viewer answering 403 through the tunnel is a viewer that is up. Node polls keep the strict reading, where a 403 means a wrong token. Also `reverse_forwards` (`-R`), `parse_node_announce`, `register_node_through` (POST to the far viewer's `/settings/nodes`), `connect_node` (viewer here, data there). **Installing Plexora on the far side** when a profile asks (`install=True`) rides the launch's OWN ssh, chained ahead of it: `install_prefixed()` builds `pip … && echo PLEXORA_INSTALL_DONE && <launch>` -- one command because it is one login, and at a Duo site one buzz of the phone instead of two (it used to be a separate ssh; that was the second buzz). `&&` is the failure story: a failed pip short-circuits the chain and nothing launches from the half-upgraded environment. `_begin_install()` announces and phases; `_await_install()` blocks on the `installed` MATCHER -- keyed on `watched.found`, NOT the event alone, because `_pump` sets every event at EOF to unblock waiters, so a set event only proves the process stopped talking. `install_command_line()` is the one rule: *the environment is whatever gets you to the program, and the program is the last word*, so `conda run -n img plexora` becomes `conda run --no-capture-output -n img pip install --progress-bar off --upgrade plexora` and an env prefix becomes its own `bin/pip` -- which is why no separate conda field exists anywhere. `conda activate` is never used: a non-interactive ssh has sourced no rc file. Its own `INSTALL_TIMEOUT`, and the connection's deadline is taken AFTER the marker, so an install spends none of the node's answer-time budget. Under a scheduler the chain puts pip BEFORE `srun`, so it still runs on the login node: shared filesystem, and the allocation is not there to be spent on pip. Stdlib only, same import rule as `cli.py`. |
| `plexora/askpass.py` | The SSH_ASKPASS helper: posts ssh's prompt back to the local Plexora over loopback (one-time nonce, plus `asking_process()` so the server can tell a second hop from a second attempt), polls for the answer, prints it on stdout. Run as a bare script by a generated wrapper, **never** `python -m plexora.askpass` -- that would build a Flask app to answer a password prompt. Stdlib only. |
| `plexora/_url.py` | The three meanings of "base URL": `clean_prefix` (no trailing slash), `prefix_with_slash`, `join_display` (accepts a full origin). Leaf module. |
| `plexora/notebook_env.py` | Which URL a notebook viewer should use, and what to bind. `resolve_display()` returns a `Resolved(server_base, display, bind_host, kind)`; ladder: explicit base_url -> `proxy=False` -> Colab -> Open OnDemand (`OOD_NODE_RE` matches the discovered prefix) -> jupyter prefix + remote evidence -> direct localhost. `verify_proxy_route()` asks the notebook SERVER whether it really proxies a port. |
| `plexora/jupyter.py`, `plexora/proxy.py` | Notebook display API, subprocess lifecycle, proxy entry point. `_start_server` returns `(port, base_url, token)`; the sidecar cache is keyed on bind host too. |
| `plexora/datasource.py` | Programmatic datasource registration (`register_datasource`, `register_image_datasource`). |
| `plexora/nodes.py` | Programmatic **data node** API: `register_node`, `attach_table`/`attach_image`/`attach_segmentation`, `detach`, `inspect_table`. A node is a Plexora with the viewer off; see `plexora/server/providers/`. Also `client_node()` (the registered node on the browser's own machine, if any), `resource_id_for(path)` (derives an id from the path, never generates one), `share_path`/`resource_status`/`unshare_path` (add/poll/remove a resource on an already-running `--dynamic` node), `browse_on_node` (relay a native dialog to a node's machine) and `list_dir_on_node` (list one of its directories -- the only way to browse a machine with no desktop; copies `path`/`parent`/`crumbs`/`entries`/`truncated` out of the node's answer BY NAME, a whitelist that silently drops any field not listed there, so the picker can never learn to draw something this function was not also taught to pass through), and `open_file_on_node`/`write_file_on_node` -- the one exception to "a node names, never sends": a plugin's Upload/Download button needs the bytes, and the browser asking has no route to the node at all. Both stream (an unread response the caller must consume and release; a write read off the wire as it goes), and a write's already-there refusal comes back as data (`{"exists": True}`, via `http.request`'s `allow_status=(409,)`) rather than an exception. `attach_image`/`attach_segmentation`/`detach("image", ...)` all run `_same_image` first. |
| `pyproject.toml`, `MANIFEST.in` | Packaging. Both must include frontend assets, shaders, and `client/src/js/**/*.js`. `MANIFEST.in` has no `plugins/*/static` glob, so each bundled plugin needs its own `recursive-include` line or an sdist installs fine and serves the tool with no client. Distribution is pip/wheel-only (`python -m build`) -- the old PyInstaller desktop-executable pipeline (`packaging/pyinstaller_entry.py`, `plexora/__pyinstaller/`, `package_win.bat`, `package_mac.sh`, `requirements.yml`) is gone. |

**Server** (`plexora/server/`)

- `models/data_model.py` — the high-risk file. Datasource loading, zarr/OME-TIFF
  access, tile extraction and encoding, GMM/contrast statistics, segmentation,
  spatial queries. Holds mutable module-level globals (`source`, `config`,
  `channels`, `seg`, `zarray`, `metadata`, `_loaded_source`).
  `generate_thumbnail(name)` is the Open Project grid's card and is the one
  image path that deliberately does NOT load the datasource -- a page of
  projects must not be a data load per card. It reads one coarse level of
  channel 0 and stretches it 1--99: `_local_thumbnail_plane` off this disk,
  `_node_thumbnail_plane` off the node (geometry, then `read_region`, both
  inside `http.speculative()` with `_NODE_THUMBNAIL_TIMEOUT`). Deliberately
  not the node's `overview` endpoint even though that is one round trip and
  already encoded: overview bytes are quantized against (0, the full-res max)
  because the viewer applies the contrast slider on top of them, so as a
  finished picture one hot pixel makes the card black -- and computing that
  window costs the node a full-resolution read. `_thumbnail_level` picks the
  coarsest level with both dims >= 200 that is also under
  `_NODE_THUMBNAIL_PIXELS`; nothing affordable means no thumbnail, which is
  the placeholder icon. Anything failing here returns None on purpose: a card,
  not a page.
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
  `import_routes` (`POST /import`, `/inspect_data`, the column screen, and
  `POST /upload_data_file` -- stages a CSV/TSV/TXT the browser sent, 512 MB
  cap, answers with a path on the server), `quick_view_routes`, `browse_routes`
  (`POST /browse_path` -- a native dialog, on this server's machine by default
  or, with a `node` field, relayed to that node's, a 400+`fallback` for either
  a bad node name or one that answered "no" rather than a real relay failure,
  and a 502 reserved for a node that could not be reached at all; `POST
  /list_dir` -- one directory's names/sizes/is_dir/path, the picker that
  stands in when no dialog is possible, also taking `node` to walk that
  machine's filesystem instead, and `show_hidden`; `GET`/`POST /picker_prefs`
  -- the picker's last directory, recents (`RECENT_LIMIT=8`) and pins
  (`PINNED_LIMIT=30`), one record per machine under `path_picker.places.
  <node-or-"">` in settings.json, keyed by node name because "" is this
  server's own filesystem and `/n/scratch/aj` means nothing on the laptop),
  `tool_routes` (opening a tool and collecting what it needs),
  `data_routes` also owns `GET /resource_status` (what could not be read and
  why, plus `profiles` -- which saved connection THIS server could open, which
  is what turns the note into a button) and `POST /reload_datasource`, the only
  thing that re-reads a project: `_ensure_loaded` is keyed on the NAME, so a
  project that opened with its image missing keeps that shape for the life of
  the process and a browser reload changes nothing. `/resource_status` answers
  from TWO sources and needs both: the load-time record (`_resource_errors`),
  and `_nodes_that_have_gone` -- a registry read, no probe -- for a node that
  left the map AFTER the project loaded, which is the same keyed-on-the-name
  rule seen from the other side and the commonest way to hit it (disconnect
  between two looks at one project and the load is skipped, so the load-time
  record is still clean). It calls `ensure_loaded` first, because the viewer
  asks while it is still setting itself up and nothing it has called by then
  loads the project -- without that the route answered out of whichever project
  was loaded BEFORE. That load is wrapped: a project that cannot open at all
  (a moved LOCAL image is deliberately fatal) must still get an answer here,
  since 500 to "what is wrong?" is how a blank page stays unexplained.
  `transfer_routes` (`POST /fetch_file` -- streams one file's bytes back, from
  here or, with a `node` field, forwarded from the far side chunk by chunk;
  `X-Plexora-File-Name` carries what to call it -- and `POST /put_file` --
  a multipart `{file, node, dir, name, overwrite}` write, 409 + `exists: true`
  rather than a silent replace. Deliberately its own module rather than a
  fourth route in `browse_routes.py`, whose header contract is "neither
  returns file bytes" -- weakening that next door would have been the easy
  way to add these), `system_routes`, `settings_routes` (the Settings page; `GET /data_places` --
  every machine a data field could name a file on, each carrying both `node`
  (the name a session THIS process owns opened) and `registered_node` (the
  name the registry holds, via `_registered_node_for`, which is all that is
  left after a restart); and `POST
  /nodes/<name>/resources` / `GET .../status` / `DELETE .../<id>`, which relay
  to a `--dynamic` node's own resource endpoints; see below).
- `utils/dir_listing.py` — `listing(raw, limit=LIST_DIR_LIMIT,
  show_hidden=False)`, one directory as `{path, parent, crumbs, entries,
  truncated}`. Shared, because both machines answer the same question now: the
  viewer about its own filesystem (`/list_dir`) and a node about the far
  side's (`/node/v1/list_dir`). Never opens a file. A path naming a FILE opens
  the folder that holds it, so a field's current value can be handed straight
  back as a place to open at; dotfiles are skipped unless `show_hidden`; a
  `PermissionError` becomes `ListingError("Permission denied: …")` rather than
  a bare crash, because on a cluster that is a fact about the account, not a
  bug. **Sorts the whole directory before cutting at `limit`** -- cutting
  first made the 2000 shown an arbitrary slice of scandir order, which on a
  scratch mount is no order at all -- and stats only the entries kept after
  the cut, since a stat per entry across a hundred thousand of them is a
  listing that takes a minute on NFS. Every entry carries its own `path`, and
  `crumbs` is the breadcrumb trail up to the root: **every path the picker
  navigates to is built server-side**, which is the only correct behaviour
  when the node is a Windows box and the browser is on a Mac.
- `utils/file_transfer.py` — the sibling that answers with bytes instead of
  names: `open_read(raw)` (`(path, size, mimetype, name)` for a file that can
  be sent), `safe_name(name)` (a bare filename or a refusal -- no separator of
  either flavour, no `.`/`..`, because the directory came from a picker that
  walked the real filesystem and the name came from a text box), and
  `write_file` (atomic: bytes land in a temp file beside the target and are
  moved onto it with `os.replace`, so a transfer that dies halfway leaves the
  previous file intact). `TransferError.exists` is set for specifically "there
  is already a file there" -- the one refusal a caller can turn into a
  Replace? question -- so `write_file` never silently overwrites.
  `WRITE_MAX_BYTES = 512 MB`, matching `/upload_data_file`'s cap and
  `providers/http.MAX_BUFFERED_BYTES`. Used from both machines a session has:
  the primary's `/fetch_file`/`/put_file` for its own filesystem, and a
  node's `/node/v1/read_file`/`write_file` for the far side's.
- `utils/channel_file.py` — the reader behind `POST /upload_channels`: a
  CSV/TSV/TXT or `.xlsx`/`.xlsm` into a rectangle of stripped strings, plus
  `autodetect()` (does the file say which names it holds?) and `describe()`
  (what the column picker draws). Openpyxl is imported lazily inside it.
- `utils/native_dialog.py` — the server-side native file/folder picker behind
  every "Browse…" button. `FILTER_NAMES` is the allowlist `browse_routes`
  validates against; there are TWO filter tables (tkinter and AppleScript) and
  a new filter needs an entry in both.
- `models/data_migration.py` — moving one data root's contents into another,
  as a background job. Nothing is ever merged (any name collision refuses the
  whole migration), a failure stops rather than carrying on, and progress is
  counted in top-level entries because a byte total means walking the tree
  before anything visibly starts. Its `can_write()` is the preview-safe
  writability probe: `paths.is_writable()` mkdirs what it is asked about and
  caches the answer, both of which are wrong for a directory a user is only
  considering.
- `plugins.py` — plugin discovery and installation. Finds descriptors via the
  `plexora.plugins` entry point group and by scanning `plexora/plugins/`, then
  mounts each under `/plugins/<name>/`. **Discovery imports nothing it was not
  asked for**: names come from directory entries and entry-point metadata, so a
  core-only build never pays for an addon's dependencies. A plugin's package
  name must therefore match its declared `PLUGIN.name`.

**Data nodes** (`plexora/server/providers/`, `plexora/server/node/`)

A project's three *scientific* resources -- image, segmentation, cell table --
are reached through a **provider**, which is either local (the file is here) or
node-backed (it is on another Plexora process, reached over `/node/v1/`).
Everything else a project owns -- config.json, the per-datasource SQLite, the
plugin store, figures, ROIs -- stays on the primary and is never distributed.
One authoritative database; nodes are data services with no project state.

- `providers/base.py` -- `ResourceLocator`, `Fingerprint`, the typed failures
  (`ResourceUnavailable` is the recoverable one and the only one callers
  degrade around), and `node://<node>/<resource>`, the string written where a
  path would go. **Test `is_node_locator()` before any path fixup**:
  `Path("node://hpc/cells")` is a valid relative path that exists nowhere.
  A `ResourceUnavailable` that reaches the route layer is answered by
  `create_app`'s handler -- **503 with the exception's own sentence and its
  `node`, never a 500**, because the sentence is the whole diagnosis and the
  traceback adds nothing to it. `_say_unavailable_once` prints it once per
  `data_model.load_generation` (a reload being the only thing that can change
  the answer): the viewer asks one tile at a time, so a single screenful of a
  project on a disconnected node used to print dozens of identical stacks.
- `providers/local.py` -- the incumbent reads, unchanged. Also what a NODE
  runs: one implementation, two transports.
- `providers/node.py` -- the primary's side of the wire. `_NodeBacked.node`
  resolves lazily (`resolve_providers` runs inside `load_datasource`'s lock and
  must not read `nodes.json` there) and **re-resolves whenever
  `nodes.address_generation` changes**. Without that a reconnect was invisible
  to an open project: the tunnel returns on a new local port, `nodes.json` is
  rewritten, and reopening the project is a no-op (`load_datasource` returns
  early for a name already loaded), so every tile, stat and GMM was refused
  against the port that had gone -- while `/remote_health`, which resolves
  freshly, called the machine Healthy and `/resource_status` reported nothing,
  because the load that cached the old address had SUCCEEDED. Only a node still
  on the map is picked up: a re-resolve that raises keeps the cached entry, so
  a DISCONNECTED node still reports itself in its own words rather than as a
  project to reopen. **Unreachability is
  raised, never swallowed**, and both places it used to be were silent
  failures: `node_for()` turns the registry's KeyError into a
  `ResourceUnavailable` (a project pointing at a node Disconnect has forgotten
  is the ORDINARY end state, and the KeyError reached the browser as a 500 on
  `/init_database` after the page had rendered); `NodeImageProvider.open()`
  re-raises `ResourceUnavailable` and only then falls back to `metadata = {}`
  (it asks for the optional OME header, so catching every `ResourceError`
  reported a dead machine as a project in perfect health); and
  `NodeSegmentationProvider.open()` now asks
  `/node/v1/resources/<id>/status` -- it has nothing to LOAD, but
  `load_datasource` is asking each provider "can this be read?", and answering
  None without asking made a mask on a machine that had gone look fine while
  every label tile 404'd. `geometry()` and `read_region()` take a `timeout` for
  the thumbnail path.
- `providers/operations.py` -- `@table_operation` / `@table_stream`. The seam
  for work that must run where the table's FILE is, because it reads the file
  and the loaded frame together (the ROI spatial join, every scientific
  write-back, the CSV export). Payload and result must survive `json.dumps`;
  refusals are returned as data, never raised across the wire.
- `providers/wire.py` -- length-prefixed frames for arrays. Numbers go raw,
  text goes as JSON: numpy's object dtype only round-trips through pickle.
- `providers/http.py` -- `request()`'s `raw_body`/`content_type` send a
  file-like object as-is rather than JSON-encoding it, so a write to a node
  streams instead of buffering a whole export in this process first;
  `allow_status` names a status that is an ANSWER rather than a failure (the
  409 a refused overwrite carries, with `exists`), so the caller reads it
  instead of matching a `ResourceError`'s message for a substring. `_check`
  now reads a failed response's body even when the caller asked to stream --
  skipping it left an error surfaced as a truncated sentence with the file
  name cut off. `FILE_NAME_HEADER` (`X-Plexora-File-Name`) is what a
  `/read_file` answer's body cannot carry, because the body IS the file.
- `server/node/` -- the node process. No viewer, no registry, no database.
  `resources.py` keys everything by resource id because a node serves several
  at once, which is exactly why data_model's single-loaded-datasource globals
  are the wrong shape there. A resource has a `state` (`ready`/`preparing`/
  `error`); reads are refused (`node/api._ready`) while a freshly-shared
  segmentation mask is still converting into a servable pyramid, which the
  node now does for itself off the request thread rather than requiring an
  already-converted file. Started with `--dynamic`, `server/node/api.py`
  additionally exposes `POST /node/v1/resources` (start serving a file on the
  node's own machine), `GET .../resources/<id>/status` (poll), `DELETE
  .../resources/<id>` (stop; nothing on disk is touched), `POST
  /node/v1/browse` (open a native dialog on the node's machine), `POST
  /node/v1/list_dir` (one directory on the node's machine, via
  `dir_listing.listing`, with `show_hidden` passed through), and `POST
  /node/v1/read_file`/`write_file` (`file_transfer.open_read`/`write_file`
  against THIS machine's disk; a write's directory and name arrive as query
  parameters, because the body is the payload and parsing a multipart envelope
  would mean buffering the file first). Without
  `--dynamic` all seven 403 by name, because the token holder gains arbitrary
  file reads AND WRITES on that account the moment they work. A node's
  quantization windows persist across jobs: `node/api._quantization` consults
  `<data_root>/node-quantization/<resource id>.json` (fingerprint
  `size:mtime_ns` of the served file, the primary store's identity rule)
  before scanning, so the startup warm-up costs a JSON read on every job
  after the first. Without that store the in-process cache died with every
  `srun` job, and RECONNECTING -- the natural response to a node that
  stopped answering -- restarted the very whole-image scan grind that had
  made it stop answering. **A request thread never runs that scan at all**:
  a miss in both caches answers immediately with a provisional window read
  off the in-memory pooled overview (`_provisional_window`) and queues the
  full-resolution read on the node's single scan thread (`_scan_soon`,
  demanded channels `appendleft`), which banks the result to the store the
  moment it lands. The synchronous version -- even slabbed and gated --
  wedged the node deaf to `/health` on both clusters the day 0.0.10 shipped,
  because a page restoring channels put every waitress worker behind the
  first two plane reads. Anything rendered under a provisional window goes
  out `Cache-Control: no-store` with no ETag (`_image(durable=False)`), and
  the window pair is part of the tile-cache key and the ETag, so the exact
  rendering replaces the guess on the next fetch instead of a year-long
  max-age freezing it; `/image/<id>/quantization` reports `"exact"` so a
  reader can tell. The suite runs scans inline -- a conftest autouse fixture
  sets `node/api._WINDOW_SCANS_INLINE`, and `tests/node_harness.py` exports
  `PLEXORA_WINDOW_SCANS_INLINE=1` to its subprocess nodes -- because nearly
  every assertion is byte-equality that needs the exact window on the first
  answer; the asynchrony's own tests (`test_node_warm_and_cache.py`, "the
  scan thread" section) flip it off and drive `_drain_window_scans()` by
  hand, with `_ensure_window_scanner` stubbed so the real daemon thread
  never starts inside pytest. **No request thread may run a first-time
  initialization either**: `data_model.prime_hot_code()` runs before the
  node's announce line (and in both primary CLIs before waitress) because a
  first `Image.save` (PIL plugin imports: dlopen with the GIL held, wants
  glibc's loader lock) racing a first `GaussianMixture.fit` (threadpoolctl's
  `dl_iterate_phdr`: holds the loader lock, wants the GIL for its ctypes
  callback) deadlocks the entire interpreter -- proven live on O2 with
  py-spy (identical dumps 20 s apart, 2.8% CPU, all 77 threads sleeping),
  and impossible on macOS, which has no `dl_iterate_phdr`; that asymmetry is
  why "local works, remote doesn't" pointed here. Order is tested in
  `tests/test_prime_hot_code.py` (prime < announce < warm < serve).
  `--manifest PATH` persists
  the resulting resource set (kinds, ids, paths -- never a project, a role or
  a read spec) so it is re-served identically at the next startup.
- `server/models/nodes.py` -- `nodes.json` (0600), holding the two addresses a
  node has: how this server reaches it, and how the BROWSER does. They differ
  under an OnDemand portal and under a tunnel. `extra["managed_by"]` marks an
  entry a saved connection rewrites every session, and `extra["role"] ==
  "client"` marks the one node -- there is ever at most one -- running on the
  machine the browser is on; `nodes.client_node()` is the only reader.
  `plexora connect` is the only thing that sets `role`, because it is the only
  thing that can know it. `extra["expires_at"]` (with `Node.expires_at` /
  `Node.time_left`) is when the job serving this node runs out, written here
  because a node OUTLIVES the process that started it -- after a restart the
  tunnel is up, the session that knew about the allocation is gone, and this
  entry is the only thing left that knows there is a clock. `remove()` also records the `(name, endpoint)` it
  retired in the in-memory `_disconnected` set, and `providers/http.py` refuses
  that address before opening a socket: taking a tunnel down does not reach
  into the providers, warm-up threads and in-flight requests still holding it,
  and left alone each spends two connection attempts and a backoff -- plus a
  urllib3 warning apiece -- rediscovering what the disconnect already knew.
  `save()` clears the pair, which is how reconnecting on the port the last
  session used works; `http.hello` is exempt (`allow_disconnected=True`),
  because verifying an address is how it stops being disconnected. The
  endpoint is half the key on purpose: a session that comes back on a
  *different* port must not revive work that still holds the old one.
  `save()` also bumps `address_generation(name)` when the endpoint or the token
  is not what was stored -- **including when nothing was stored**, because
  `remove()` deletes the entry and so the commonest reconnect of all
  (disconnect, then connect again) writes over an absence. Exempting that as
  "a first registration has nothing cached to invalidate" is wrong, and was the
  first version of this: it is exactly when a provider IS holding a retired
  address. It is a comparison rather than "bump on every save" only because
  `record_handshake` rewrites this file after every probe to keep `last_seen`
  current. That counter is what lets a cached node notice a reconnect -- see
  `providers/node.py`.
- `server/models/secret_store.py` -- `write_private_json`: the atomic
  chmod-**before**-rename writer both `nodes.json` and `remotes.json` use. The
  ordering is the whole module; a rename-then-chmod leaves a world-readable
  window on a shared cluster filesystem.
- `server/models/remotes.py` -- `remotes.json`, saved remote servers. Field
  names are `connect.Session`'s parameter names so `as_session_kwargs()` is a
  rename-free hand-off. **No password field exists**, deliberately. `srun` is
  three-valued: `None` (no scheduler), `""` (site defaults), a string.
  `as_node_kwargs()` is the second hand-off, to `connect.NodeSession`: it
  carries everything that describes REACHING the host -- `remote_command`,
  `srun`, `bind_node`, `jump`, `ssh_opts`, `plugins`, `node_name`. **The
  profile is the source of truth and a data node inherits all of it**,
  `srun` included: serving tiles is sustained read I/O, and a site that
  keeps Plexora off its login nodes means it for that too. Only what
  configures a viewer that is not being started stays behind (`datasource`,
  `data_dir`, `forwards`), plus `serve`. `install` crosses over for the same
  reason: **the one field on a profile that makes connecting WRITE to the far
  machine** -- `pip install --upgrade plexora` before anything is launched --
  and it is off by default and written to the file only when switched on.
  There is deliberately **no separate conda-environment field**: the launch
  command already names the environment, and
  `connect.install_command_line()` reads it (see below), so a second box
  would be two answers to one question with the launch and the install free
  to disagree.
- `server/models/remote_sessions.py` -- live connections, one daemon thread
  each. **Two kinds**, `KIND_VIEWER` (Plexora over there, browser tunnelled to
  it) and `KIND_NODE` (Plexora stays here, only the far side's files come
  over). Both can be live for one profile at once, so `_key()` namespaces them
  -- the viewer keeps the bare name it always had. States
  `connecting/authenticating/installing/waiting_for_job/tunneling/
  waiting_for_app/connected/failed/exited`; phases come from
  `Session.on_phase`, not from matching echoed text (the queued-job line is
  only printed five seconds in). `installing` exists only for a profile with
  `install` on, and it is a state rather than a background errand because it
  is minutes long, it writes to the far machine, and it is the step most
  likely to be the one that failed. **A new state has to be added to
  `OPENING_STATES`, `PHRASES`, `_on_phase`'s map AND `remoteState.js`'s
  `OPENING`/`LABELS`** -- one missed and a connection mid-pip reads as
  settled. `redact()` strips
  `token=`/`password=` from every served log line. Secrets live in
  `_Prompt.answer` and are handed over exactly once. **One connection
  authenticates three times** -- the job, the login node again as a jump
  host, then the compute node -- so a repeatable answer is kept in
  `_secrets` for the length of ESTABLISHMENT and replayed, and the person
  types once. Guarded twice: `prompt_secret_kind()` replays only a
  password or a key passphrase, never a one-time code, a `(yes/no)`
  host-key confirmation, or wording it does not recognise; and one ssh
  asking the same thing twice counts as a refusal, so the cached answer is
  dropped and the person is asked. Telling a second hop from a second
  attempt needs `askpass.asking_process()` (the ppid, which is the ssh
  itself because the POSIX wrapper `exec`s), since the two hops to the
  login node ask identically. `_forget_secrets_locked()` closes the window
  on connected, failed and stopped. `node_name` is the
  profile's own `node_name` when it has one, its session name otherwise --
  `status()["node"]` reports it for a `KIND_NODE` session rather than the
  profile name, so a node registered under a different name is still the one
  Settings' `_forget_node` matches on disconnect. **The job's clock** is
  `time_limit` (from `recipes.srun_seconds(remote.srun)`), `job_started_at`
  (stamped by `_start_the_clock_locked` on the transition OFF
  `waiting_for_job`, and again at `connected` for a job that never queued --
  queue time is not allocation time), `expires_at` and `time_left`.
  `expires_at` is None unless the session is LIVE (opening or connected):
  disconnecting stops a session but deliberately keeps its record, and a
  deadline computed from `job_started_at + time_limit` alone went on counting
  down for a connection the user had closed on an allocation cancelled with it.
  `status()` reports `time_limit` and `time_left` as DURATIONS rather than a
  deadline, so a browser whose clock disagrees with this machine's still counts
  down correctly. `_register_node` is the wrapper that carries `expires_at`
  into the registry entry when the node announces. **`_tidy_after_end()` is
  the teardown for a session nothing will ever press Disconnect on** — run
  when establishment fails, or when `wait()` returns without `stop()` having
  been called (a walltime, a dropped network, a crash on the far side). It
  stops sibling watchers (under `srun` the tunnel is a second ssh that the job
  leg exiting does not end), removes the askpass helper dir, and — only for a
  `KIND_NODE` session whose `session.registered` is set — calls the
  route-supplied `unregister(node_name)` so the dead entry leaves
  `nodes.json`; left standing, it kept `/resource_routing` offering a dead
  address and `/resource_status` reporting the project fine while every tile
  timed out. A **deliberate** `stop()` skips the tidy on purpose — the
  disconnect route forgets the node itself, and skips `unregister` while doing
  it (see below). `start()` takes the `unregister=` callable and now calls
  `existing.stop()` before replacing a dead (failed/exited) session, closing a
  `connect._ACTIVE` watcher leak a bare dict overwrite used to leave behind.

`data_model` dispatches on one module-global boolean (`_remote`), set under the
load lock. It is False for every project with no `resources` block -- which is
every project that predates this -- so the single-server path costs one global
read and one branch, and the warm-tile path is untouched.

**Settings** (`/settings`, `settings_routes.py` + `client/templates/settings.html`)

A left rail of sections; `SECTIONS` in the route module is the only list and
the rail is generated from it. Three sections today: the data directory, saved
remote servers, and the data-node address book. Adding one is that tuple plus a
`<section>` in the template plus a prototype in `settingsPage.js`.

**Neither section configures where data lives any more.** Remote servers stores
reusable SSH connection profiles and nothing else — the `serve` / `local_serve`
/ `node_name` boxes are gone, because filling them in meant naming, before
Plexora started, the path of a file you were about to go looking for. Data
nodes is a status board: most entries now appear and disappear on their own
(a data field's Remote option opens one; `plexora connect` opens one on the
laptop), and the manual add is behind a disclosure as the exception it now is.
`_remote_payload(payload, name, existing)` **preserves** the dropped fields
from the stored record, so a profile written by `plexora connect --save` does
not lose them when somebody edits an address in the UI.

`server/models/recipes.py` also owns the walltime: `split_srun`/`join_srun`
are the form's three boxes over one stored string, and
`walltime_seconds`/`srun_seconds` read a `-t` value into seconds for every
countdown. Slurm's `-t` is genuinely ambiguous and the rule is not optional --
a bare number is MINUTES, and it is the day separator that makes the colon
groups hours (`30` and `30:00` are both half an hour; `1-2` is a day and two
hours). Anything unparseable, absent or `UNLIMITED` comes back None, all three
the same way: a countdown must never invent a deadline, because somebody told
they have twenty minutes left on a job with no clock saves and reconnects for
nothing.

`server/models/recipes.py` is the "Add a server" preset catalogue behind
`services/connectionModal.js`'s recipe flow, reached through `GET
/settings/recipes` and `POST /settings/recipes/<id>`. `Recipe` dataclass, the
`RECIPES` tuple, `all_recipes()`, `find()`, `compose()`. Six presets: HMS O2
and MGB-ERIS (`mgb-eris`, ERISTwo — both pinned to observed behaviour), generic
Slurm and generic SSH shapes (assert nothing about any machine, carry no
badge), and AWS/Google Cloud (shaped from published documentation, `site=True,
tested=False`). `unverified
= site and not tested` is what renders the badge — presenting a guess with the
same confidence as a verified fact is how somebody spends an afternoon on a
partition that never existed. Composing happens **server side**, through the
same save the Settings form uses, so a preset can never write a profile the
form could not — in particular there is still nowhere in one to put a
password. `compose()` reads the switches off the RAW answers and the boxes
off the trimmed ones: `str(False or "")` is `""` and `str(True or "")` is
`"True"`, so a boolean through the text pass is true in one direction and
empty in the other.

`/settings/remotes*` drives `remote_sessions`: connect answers **202** and the
page polls, because an srun connection legitimately waits a quarter of an hour
in a queue and a route that waited with it would pin a Waitress worker. They
take `?kind=node` to open a data node instead of a viewer — same profile, same
askpass, same polling — and `disconnect?kind=node` also forgets the node entry,
but only one whose `managed_by` proves this route created it (resolved by the
session's own `node_name`, not the profile name, since a node reports the name
it is actually on the map under). The connect route also passes an
`unregister=_forget_node_entry` callable for a `KIND_NODE` session, which is
the other half of `register`: when a node session ends on its own instead of
through this route, its own `_tidy_after_end()` is the only thing left that
knows to take the entry back off the map. `_forget_node` (called on a
deliberate disconnect) and the session's own teardown now share one
implementation, `_forget_node_entry(node_name)` — same `managed_by` guard,
just resolved from a node name instead of a profile name, since the session
has no request to resolve a profile from. The two `_askpass` routes are authenticated
by the session nonce and carry the app's own auth token, so they need no
exemption from the rule that nothing is exempt. `GET
/settings/remotes/<name>/status` takes `?log=N` (`_log_lines()`, clamped to
`remote_sessions.LOG_LINES`): the list of every profile carries a short log
tail, a surface watching ONE connection — `services/remoteState.js`'s focused
fetch — asks for the whole buffer instead. `/data_places` carries the last
eight lines per profile for the same reason, so a card has a terminal to draw
before anyone has focused it.

`GET /remote_health` is the only health probe in Plexora: for each profile
with a live node session it times one `http.hello` (`HEALTH_TIMEOUT = 4.0`)
and reports `{state: healthy|stale|unreachable|unknown, ms, detail}`, keyed by
PROFILE name and probed by NODE name. `stale` is checked FIRST and contacts
nothing: `data_model.held_node_addresses()` (→ `ProviderSet.held_addresses`,
which reads each provider's cached `_node` and never resolves one) says where
the open project is actually sending its requests, and if that is not the
registry's current endpoint the probe would otherwise report a machine as well
while the viewer failed to read a single tile from it. `remoteGlobe.js` renders
it as "Reconnected", which is neither of the other two words: the machine IS
answering, and the server DOES know what is wrong. **Asked for, never polled** — the
navbar panel calls it once when it opens. Session state is what Plexora
*did*; whether the node answers now is a different claim, and a background
poll of it would be a second opinion that disagreed with the session state at
a moment nobody was watching. A profile with no node open is not contacted at
all.

Since `RemoteSession._tidy_after_end()` unregisters a node that dies on its
own, a walltime death now removes the registry entry rather than leaving a
stale one behind — so `/resource_routing` stops offering that address and
`/resource_status` reports the layer missing outright (this is the case the
reconnect modal fires for), instead of the machine answering "stale" forever.
The browser side of the repair lives in `main.js`: `repairRouting()` (exposed
as `window.__plexora.repairRouting`) calls `PlexoraRouting.refresh()`,
re-applies routing over the `origSrc` every channel stashed at boot, and
rebuilds tile layers only when the resolved routes actually changed. It runs
on the `plexora:remote-nodes-changed` window event — fired by
`remoteState.js`'s `publish()` when a snapshot diff shows a profile's node
half changing being-up, map name or registry name, and by `remoteGlobe.js`'s
`staleNodes()` as a backstop for a reconnect made from another tab — and on a
30-second-throttled `tile-load-failed` handler, for the case nothing else in
the tab was watching.

**One connection concept.** The machine Plexora runs on is Local; anything
reached from it over SSH is Remote. Nothing in the app opens a `KIND_VIEWER`
session any more: Settings' Connect used to run Plexora on the far machine and
tunnel the viewer back, which made the Settings page somewhere the host
Plexora runs on could be redefined from inside the running app. That
capability lives in `plexora connect` on the command line, which reads the
same saved profiles — which is why the form's advanced fields (`datasource`,
`forwards`, `plugins`) are still there. The server-side viewer kind and its
routes are unchanged and still tested; what changed is that no UI creates one.

**Changing it records a preference; it never repoints the running process.**
`data_root()` resolves once per interpreter and data_model is holding an open
image against it, so `paths.reset()` here would fail as a stack trace from
whichever tile read got there first. `/settings/data` therefore reports
`in_use` and `pending` separately, and the page asks for a restart. Two other
rules, each pinned by `tests/test_settings_page.py`: the setting is written
only **after** a migration succeeds (written first, a failed copy leaves the
pointer on an empty directory while the projects sit where the app no longer
looks), and a `PLEXORA_DATA_PATH` in the environment makes the write a **409**
rather than something recorded and silently ignored -- the notebook sidecar and
`plexora --data-dir` both export it.

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
  `initMiniMap()` wires up the mini-map lens alongside `initProjectLabel()`/
  `initLegend()`, and calls into it (`invalidate({refetch:true})` on active-channel
  changes, `invalidate()` on range/colour changes) so the lens stays in sync
  without owning its own state.
- `views/miniMap.js` — the bottom-left circular lens (`class MiniMap`, a global,
  loaded the same way as `imageViewer.js`): expands into a circular overview of
  the whole tissue per active channel, fetched from `/generated/overview/...`.
- `views/viewerManager.js` — tile source definition: `getTileUrl`, `getTileKey`,
  `toTileLevels`, and one `addTiledImage` per active channel.
- `services/glRenderer.js` — the WebGL2 core. Shader compile, quad buffer,
  default draw path.
- `workers/tileDecoder.js` — off-main-thread WebP tile decode.
- `services/appStatus.js` — `window.PlexoraStatus`, the app-wide status
  indicator. See its own section below.
- `services/appRouter.js` — `window.PlexoraRouter`, internal navigation that
  does not throw the viewer away. See "Navigation and the App Shell" below.
- `services/pageBoot.js` — `window.PlexoraPage`, the registry every page
  controller mounts through instead of `DOMContentLoaded`.
- `src/shaders/{vert,frag}.glsl` — the colorize/composite shaders.
- `pluginRegistry.js` — `window.Plexora.registerPlugin`, the client half of the
  plugin contract.
- `services/datasetContext.js` — client mirror of the server dataset contract,
  handed to each plugin as `ctx.dataset`.
- `services/dataLocation.js` — `window.PlexoraDataLocation`, the compact
  **L | R** switch every data-selection field gets (one letter each because it
  sits inside the field's row; the meaning is on the per-button `aria-label`
  and a `data-tooltip` on the group reading "Data Location — (L)ocal |
  (R)emote"). `attach()`
  renders on **every** launch (`available()` is unconditionally true) because
  there is always somewhere else a file could be. What each half means is
  derived, not configured — `plainPath()` is the one predicate everything hangs
  off, and it is true only when the machine holding the file is the machine
  running Plexora:

  | switch | server is here | server is elsewhere |
  |---|---|---|
  | This computer | plain path (today's behaviour) | the `role: "client"` node, or a CSV upload |
  | Remote → the server | (not offered) | plain path |
  | Remote → a saved connection | that profile's node | that profile's node |

  Produces the same shapes every form already took — a path, an uploaded
  file's server-side path, or a `node://<node>/<resource>` locator — so
  nothing downstream of the form learns a new shape. `setVerbatim()` (not
  `setWhere`) is what the node chips call: it picks whichever mode submits the
  box unchanged, which differs by where Plexora runs.

  **Choosing Remote is a 0/1/many flow (`choosePlace()`), not always a list.**
  No machine reachable opens `PlexoraConnectionModal` directly, with an
  `intent` sentence explaining why; exactly one reachable machine is adopted
  silently — nothing to choose from is not a choice; several open
  `PlexoraPlacePicker` as before. "Reachable" means the server itself when
  Plexora runs elsewhere, or any saved connection with a data node already
  open — a saved-but-not-connected profile does not count, since offering it
  would adopt a machine the field cannot yet read. The place chip (the pill
  showing which machine is chosen) always calls `choosePlace(force=true)`,
  which skips the 0/1 shortcut and opens the list regardless — the one-machine
  case is a shortcut, not a one-way door.

  **`attach()` never calls `onChange` at mount.** It used to, and that reached
  the caller's handler before `attach` had returned the handle the handler is
  written against — a TypeError that escaped the loop mounting all three import
  fields, so the form shipped with a switch on the image and nothing on the
  mask or the table. Nothing has changed at mount; there is no event to send.
  The mounting loop in `importFormValidation.js` also try/catches per field, so
  one field can never again cost another.
- `services/remoteState.js` — `window.PlexoraRemotes`, the one owner of "what
  are the remote connections doing?". Four surfaces used to ask that
  independently, with their own timer, their own copy of the state list and
  their own idea of which prompts are secret — so Settings masked a host-key
  fingerprint the machine picker showed in the clear. `subscribe(cb,
  {active, focus}) -> unsubscribe` delivers a merged snapshot: `GET
  /settings/remotes` (viewer halves) joined with `GET /data_places` (node
  halves) into one `entries` row per saved profile, `half(entry, kind)`
  picking the one a caller wants. The node half carries TWO names --
  `node.node` from the session and `node.registered` from the registry. A
  surface asking only "is anything up?" may test either; one MATCHING a name
  (against `/resource_routing`, say) must use both, or it compares the empty a
  session-less node leaves with the empty a local project routes to and calls
  that a match. It also carries the job's clock: `node.timeLeft` /
  `node.timeLimit`, plus `at` (when the snapshot arrived) and
  `remaining(entry)` / `duration(seconds)` / `WARN_SECONDS` (600). `remaining`
  INTERPOLATES against `at` rather than reading `timeLeft` straight, because
  the poll deliberately stops when everything is settled — which is the state
  a four-hour job sits in for four hours, and a countdown that only moved when
  a request came back would sit frozen for all of it. `isOpening(state)`, `label(state)` and
  `isSecret(text)` are the one implementation of each judgement, shared so no
  two surfaces can disagree about them again. `connect`/`disconnect`/
  `answer`/`forget`/`save` all act through the profile name plus a
  `KIND_VIEWER`/`KIND_NODE` kind and refresh the snapshot afterwards. `focus`
  may be an object, an ARRAY of them, or a FUNCTION returning either — the
  Settings page passes a function, because which cards have their log expanded
  changes as the user opens and closes them and re-subscribing on every toggle
  would tear down the subscription in order to preserve what it is preserving.
  **The
  poll (`POLL_MS = 1000`) is scoped**: it runs only while there is at least
  one subscriber AND (a session is in an `OPENING` state, or an `active`
  subscriber exists) — a settled connection watched only by the navbar globe
  costs nothing at all, which is what lets the globe sit on every page for
  free. One in-flight request is shared across every subscriber, so a modal
  open beside the Settings page is one round trip, not two. `publish()` also
  diffs consecutive snapshots (`nodeChanges`) and, when a profile's node half
  changed being-up, map name or registry name, dispatches a `window`
  `CustomEvent("plexora:remote-nodes-changed", {detail: {changed}})` — the
  event `main.js`'s `repairRouting()` listens for, since `main.js` resolved
  tile routing once at boot and is not itself a `PlexoraRemotes` subscriber.
  Only a real transition fires it: the first snapshot has nothing to diff
  against, and a failed poll republishes the same `entries` object.
- `services/connectionModal.js` — `window.PlexoraConnectionModal`, the one
  place a connection is watched from wherever it was started.
  `open({name, kind, intent}) -> Promise<{connected, name, node, kind, label,
  detail}>`. A native `<dialog>` + `showModal()` — top layer, so it is NOT a
  `PopoverPortal` case, unlike `remoteGlobe.js` below. Its progress steps map
  1:1 from the server's own states, with two drawn only for a profile that
  actually does them: the scheduler step for one that waits in a queue, and
  "Installing Plexora" for one with `install` on — labelled with the
  environment's name when the server sent one (`install_env`, derived once by
  `connect.environment_label`; **nothing here parses a launch command**); the log pane is
  `services/logTerminal.js`, fed by the focused connection's `?log=200`
  status; the ssh prompt is shown verbatim, masked only when
  `PlexoraRemotes.isSecret()` says so; a failure is drawn against the step
  that was running, with a retry; and closing the window is offered as a
  choice separate from ending the connection ("Continue in background" vs
  "Stop connecting"), because a queued job is a real fifteen minutes and the
  ssh belongs to the server, not the dialog. Also owns the "Add a server"
  recipe flow (`GET /settings/recipes`, `POST /settings/recipes/<id>`),
  composed and connected without a detour through Settings.
  `open({view: "recipes"})` lands straight on the catalogue — that is what the
  Settings page's "Use preset…" button calls, since the presets
  otherwise shipped reachable only by flipping a data field to Remote with
  nothing saved, which is the one place a first-time user was not looking.
- `services/logTerminal.js` — `window.PlexoraLogTerminal.create({title,
  empty})`, the connection log, shared by the modal and the Settings cards.
  Follows its own output while the reader is at the bottom, stops the moment
  they scroll up, and follows again when they return; `paint(lines)` compares
  before touching the DOM, so an unchanged poll costs nothing. One element per
  line, and a line ssh relayed from the far machine (`  [ssh] …`, as
  `connect._Watched` writes it — the remote command's stdout AND stderr, which
  ssh merges) is marked `is-relayed` so the machine's own words read as output
  rather than as narration. **Keep the element and repaint it**: the Settings
  cards used to be rebuilt on every poll, so the pane was a new element once a
  second and started at the top once a second, which is exactly when there is
  something in it worth reading.
- `services/remoteGlobe.js` — the navbar globe and its connection panel,
  mounted on `#remote_globe` (an empty mount in `base.html`, before
  `#app_status`). A passive `PlexoraRemotes` subscriber until its panel opens,
  which is what keeps it free while everything is settled. Uses
  `PopoverPortal` (it is in `tests/test_popover_portal.py`'s
  `VIEWER_POPUPS`). Mounts **once**: the navbar markup is never swapped by
  `appRouter`, so `PlexoraPage.register` returns `null` for it and a
  module-level `mounted` guard makes a re-run a no-op — the same reason
  `segmentationWait.js` guards its chip. The panel is a **status board with a
  switch on it**: one two-line row per saved profile (name + state; then health
  + latency + a per-row connect/disconnect + a monitor icon saying whether the
  image on screen is being read from that machine), and nothing identifying or
  typeable on it — no address, no username, no ssh option — because it opens
  over the viewer and in every screen-share. Adding a machine is a link to
  `settings#remotes`. Two fetches, both once per panel open and neither polled:
  `resource_routing` (which node the image comes from) and `remote_health`.
  The monitor is matched through `nodeNameOf(entry)` (session name, else
  registry name) and **both sides must be a real name**: a local project routes
  to null and a node that outlived its session had a null session name, so
  `null === null` lit the cluster's monitor while the picture was being read
  off the user's own disk -- and lit the local row saying the opposite in the
  same list. Exactly one monitor in the list is lit. A row inside a scheduled
  job also carries `PlexoraRemotes.remaining()` as a clock, amber in the last
  ten minutes; rows with no walltime carry nothing, because most connections
  have none and an empty slot per row would spend the panel's width saying so.
  `staleNodes(datasource, candidates)` compares `PlexoraRouting.held(datasource)`
  (what THIS page's tiles were actually built from) against a fresh
  `/resource_routing` answer; on a mismatch the row draws "Reconnected" over
  an otherwise-healthy probe AND the panel dispatches
  `plexora:remote-nodes-changed` itself — a browser-side counterpart to the
  server's `stale` health state above (both catch a project still addressed to
  where a node was before it reconnected), and the backstop for a reconnect
  made from another tab, which no poll in this one was awake to see.
- `services/sessionExpiry.js` — `window.PlexoraSessionExpiry`, the dialog that
  interrupts before a scheduled job ends. Loaded on every page after
  `connectionModal.js` (whose dialog its one button opens) and mounted once
  through `PlexoraPage.register` with a module-level `started` guard, the same
  shape as the globe's `mounted`. **Two moments only**: ten minutes out, and at
  zero. Subscribes PASSIVELY and counts down locally off
  `PlexoraRemotes.remaining()`, with a 15 s interval that runs only while
  something has a clock — an active subscription would turn a settled
  four-hour job into a request a second. `told[name]` is cleared when a
  connection's remaining time goes back UP, which is exactly what a reconnect
  does, so a fresh job is warned about again and a running one is not warned
  twice. An open dialog closes itself when its clock goes away -- somebody who
  reads it and goes and disconnects has answered the question. "Start a new session" disconnects the node FIRST (the old entry names
  a port whose tunnel has gone, and it is what `nodes._disconnected` keys on)
  and then opens the connection dialog.
- `services/resourceStatus.js` — `window.PlexoraResourceStatus`, why a layer
  is missing. **A modal when this Plexora can fix it, a banner when it cannot**,
  and `/resource_status`'s `profiles` is what says which: a machine one button
  away is a question with an answer, which is not the shape of a banner.
  Connecting hands off to `connectionModal.js`, then `POST /reload_datasource`
  (the server keys "which project is loaded" on the NAME, so a page reload
  alone finds it still in the shape it opened in) and only then reloads the
  page. Two per-tab memories: `asked` (the modal has been answered, so
  navigating does not re-ask) and `dismissed` (the banner too); both are
  dropped by `forget()` the moment the project opens whole, so connect-work-
  disconnect-reopen in one sitting is asked about again rather than met with
  the silence of an answer given about a situation since fixed and rebroken --
  which is why the route is asked even when the banner was dismissed. Declining or a
  connection that fails BOTH leave the banner — the promise resolves when the
  connection attempt settles, not when the dialog closes, or a cancelled
  connect left a missing layer with nothing on screen about it.
- `services/fileLocation.js` — `window.PlexoraFileLocation`, "which machine?"
  asked of every file button at once, so every plugin's Upload/Download honours
  Local/Remote without its form changing. `dataLocation.js` asks this question
  per FIELD by building the switch in; this asks it at the one place every
  button passes through -- the click -- with one bubble-phase delegate on
  `document` for `input[type="file"]` and `a[download][href]`. Does nothing at
  all when `remoteAvailable()` (synchronous, off a passive `PlexoraRemotes`
  subscription -- the check runs inside a click handler, and a promise cannot
  be awaited before `preventDefault()` without losing the transient user
  activation a file dialog needs) says nowhere else is reachable, so an
  install with one machine is untouched. `deliver(blob, filename)` is the
  documented way in for anything built in the tab rather than clicked --
  `form.submit()` fires no event and a detached anchor never bubbles here --
  and with nowhere else to send it, saves locally without touching the
  network, which matters because one caller is the emergency export offered
  when the server has stopped answering. `data-file-location="local"` on an
  element or an ancestor opts it out, for a core field that already has its
  own switch (`dataLocation.js`, `views/channelNamesUpload.js`) and must not
  ask the same question twice in two shapes. Loaded on every page from
  `base.html`, after `connectionModal.js` (whose "Connect another machine…"
  escape hatch it opens) and mounted once at parse time rather than through
  `PlexoraPage`, because its listener is on `document` and survives a routed
  page swap. `plugins/gating/static/gatingApi.js`'s `downloadGatingCSV` forks
  on `remoteAvailable()` between the streamed hidden-form download (one
  machine) and a fetch + `deliver()` (more than one); `plugins/roi/static
  /roiApi.js`'s `saveBlob` calls `deliver()` the same way, keeping its anchor
  as the offline path; `plugins/gating/static/csvGatingList.js`'s upload arrow
  now uses `elem.click()` rather than a hand-rolled, non-cancelable
  `initEvent`, because a click this layer cannot intercept is a click that can
  never reach a remote machine.
- `services/placePicker.js` — `window.PlexoraPlacePicker`, the modal behind
  Remote when there is more than one machine to choose from. Lists `GET
  /data_places`. Its own password-prompt renderer and state chip are gone —
  pressing Connect on an entry that is not up now opens
  `connectionModal.js` on top of the list, which is still there if the modal
  is cancelled; the connection modal and the Settings cards are the only two
  surfaces left that render a prompt inline. Resolves `{id, kind, label,
  node}`; `node` is the only part a field uses. This is the whole of "choose
  where the data lives when you add it" — nothing is configured in advance.
- `services/pathPicker.js` — `window.PlexoraPathPicker`, a directory-listing
  modal (`POST /list_dir`, optionally `{node}`) that stands in for a native
  dialog on a machine with no desktop (a compute node). Not a file manager —
  no rename, delete or upload — and not a replacement for typing a path.
  `pick({mode, filter, start, title, node, multiple})`; `multiple: true`
  answers `string[]` but no caller wires it yet. DOM is built node-by-node, no
  `innerHTML`, so `tests/js/path_picker_probe.mjs` can run it. Back/Up/Refresh,
  an address bar, an in-folder name filter, a hidden-files toggle, a Type
  column, keyboard nav with listbox ARIA, and a places sidebar
  (Home/Pinned/Recent) backed by `/picker_prefs`. The address bar
  (`.path-picker-address`) is one wide strip holding the crumbs: clicking a
  crumb navigates (its handler `stopPropagation`s), and clicking anywhere else
  in the strip turns the whole thing into a path box with its contents
  selected. It was a pencil glyph at the end of the trail, which nobody found.
  `last_dir` is written on the dialog's `close` — not on a successful pick —
  because Esc and the backdrop close without going through `finish()`, and
  because browsing is the part that costs the effort: cancelling is not an
  instruction to forget. `add_recent` still rides only on a real pick, and a
  close that never moved writes nothing at all. Three rules the rest of the
  file follows: **the client does
  no path arithmetic** -- every path it navigates to came from the server
  (`entry.path`, `crumbs[i].path`, `parent`); **`state.here` is assigned in
  exactly ONE place, from a server answer**, so a failed listing changes
  nothing; and **nothing about remembering places may block browsing** -- a
  failed `/picker_prefs` means no Recent list, not a picker that will not
  open. Esc inside the filter or path box must `preventDefault`+
  `stopPropagation`, or the `<dialog>` reads it first and cancels itself.
  `browsePicker.js` passes `start` (the field's current value, read at click
  time) through as where the listing fallback should open, and relays `node`
  so the fallback lists the SAME machine the native dialog would have opened
  on -- the only branch a cluster field ever takes, since it has no desktop.
- `views/channelNamesUpload.js` — `window.PlexoraChannelNames`, the dialog
  behind the sidebar's channel-rename button. One `<dialog>` with three stages
  (which file → which column → the count did not match); the server decides
  which comes next. `main.js`'s `adoptChannelNames` is what takes the result on
  without a reload. See "Naming an image's channels" below.
- Other views: channel list, colour picker, open-project page, import/config
  forms. (The gating sidebar lives in the plugin, not here.)

Note: `imageViewer.js` and `miniMap.js` are loaded as **plain `<script>`** tags
from `base.html`, not bundled by webpack. Only `vendor.js`, `viewerManager.js`
and `glRenderer.js` go through webpack into `client/dist`. So neither has a
module system — top-level `class` declarations are globals, and `node --check`
is a valid syntax gate for either.

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
mask on. A mask whose pyramid is still converting is **waited for, not
substituted**: `enableCellLayer` sets `seaDragonViewer.cellLayerAwaitingMask`
and turns nothing on, because centroids standing in for a mask are a different
representation of the same cells rather than a rougher one, and the substitution
was silent and could last minutes. `main.js` polls `/get_segmentation_status`
and announces every reading as `plexora:segmentation-progress` / `-ready` /
`-failed` — one loop asking the server, whatever number of panels are showing a
wait (Cell Explorer's `renderMaskWait` is the one that does). When the job
lands, `adoptSegmentation()` loads the layer in place and turns on whatever was
waiting, or swaps a fallback over (it used to reload the page, minutes into a
session). `viewerControls.fallBackToCentroids()` is the way out for a user who
would rather not wait; it marks the centroids as a fallback, so the mask still
replaces them when it arrives. `hasSegmentation()` and `maskPending()` are the
two halves of this: "no mask" and "not yet" are different projects.
`tests/js/cell_mode_control_probe.mjs` pins all of it.

**The conversion is waited for IN the viewer, not on a form.** Saving the edit
page goes straight to the viewer even with a job pending; `segmentationWait.js`
shows it there, as a dismissible modal that hands off to `#segmentation_chip` in
the navbar ("Pyramidizing segmentation mask…", click to reopen). It polls
nothing — it is a listener on the three announcements above, and `main.js` opens
it right after `viewerControls.init()` rather than at its own poll, which runs at
the bottom of `init()`. Two endings, told apart deliberately: **ready** reopens
nothing (the mask going on IS the message, and a modal would cover it), **failed**
reopens once (nothing else on the page would ever mention it). The overlay goes
through `PopoverPortal`, unlike `segmentationProgress.js`'s identical card — the
import pages have no viewer and no way to go fullscreen, this one runs over a
viewer that has both, and the fullscreen `::backdrop` covers siblings whatever
their size. When the job lands
on a viewer drawing nothing, `adoptSegmentation()` turns the mask on:
`viewerControls.userChose` is what separates "none, because None was the only
enabled button for the last four minutes" from "none, because the user clicked
it", and all three surfaces that move the control set it.
`tests/test_segmentation_wait.py` pins the flow and the four files that have to
agree on it.

**The Cells control shows what the project HAS.** Three outcomes per mode, and
`viewerControls.shownModes()` is the one place that decides between them — the
sidebar buttons and the View menu both render from it, so they cannot disagree.
A mode the active *plugin* does not use is hidden. A mode whose resource is
**missing outright** is hidden too, and `#cell_data_cta` — a plain `<a>` to
`/edit_config/<project>`, so `appRouter` swaps the page in — appears reading
"Add Seg Mask", "Add Data" or "Add Seg Mask / Data". A mode whose resource is
**present but cannot do this** stays visible and disabled with the reason on it
(`unusableReason()`): a mask stored as boundaries has nothing to fill, a table
whose x/y roles are unanswered has no positions, and a mask still converting is
about to work. That distinction is the whole design — telling either of the
latter to go and add a file it already has is the wrong instruction. With
nothing left but None the buttons go entirely and the link takes the row. The
options ship `hidden` and `disabled` from `index.html` and are shown by
`refreshAvailability()`, but they stay in the DOM, which is what lets
`adoptSegmentation()` bring Outlines and Filled back mid-session.

**Drawing the mask needs no feature table.** `renderLabelTile` reads cell ids
out of the label pyramid itself, so image + mask + no data is a project that
draws — and one `attach_segmentation` explicitly supports, inserting the "Area"
channel whether or not a table exists. Per-cell rows are what PLUGINS need, and
each is already gated on a table. `NumericData.loadCells()` therefore returns
empty arrays rather than throwing whenever `hasCellTable()` is false (no data
block, or roles nobody has answered); `bindSegmentationBuffers` and
`forceRepaint` already no-op on zero cells. It used to destructure the null
schema, so the first click on Outlines threw before the pyramid was requested
and `selectMode` read the TypeError as a mask that would not load.
`tests/test_mask_without_table.py` pins both halves.

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

**Anything base.html loads needs its CSS in `main.css`, not `import.css`.**
`import.css` is linked only by `upload.html`, `project_edit.html` and
`project_columns.html`; the requirements modal and the channel-names dialog open
over the *viewer*, which links neither. The classifier's `.column-*` rules and
the shared `.field-hint` both started in `import.css`, so the modal drew the
marker/metadata split as two bare `<ul>`s — Sortable was attached and the drag
technically worked, but with no chip to grab and no box-shaped target it read as
a printed list of column names. `tests/test_column_classifier_css.py` pins the
pairing.

**Editing is generated from the record.** `project_edit.html` renders a section
only when `project.has` says it applies, and `POST /project/<name>` merges. The
image is the one thing that cannot change. The old path did the opposite — it
read every project as a CSV and rebuilt the entry from `{}`, which silently
destroyed AnnData projects; `tests/test_project_edit_routes.py` is the guard.

## Naming an Image's Channels

An OME-TIFF routinely arrives with its channels called `Channel_0 … Channel_n`
and the panel that says what they really are in a separate CSV or spreadsheet.
Until that list is in, gating matches markers to channels **by name** and so
matches nothing — which is what the sidebar's `#channels_upload_icon` is for.

**A path, and Upload only where it earns its place.** The dialog's main
control is a path box plus a `Browse…` that opens a native picker **on the
server**, filtered by `native_dialog.py`'s `"channels"` entry. On a desktop
launch Browse and a browser upload do the same thing — both write a path on
the machine running the server — so offering both there is a choice between
two spellings of one act before the user has done anything, and Upload stays
hidden. The moment Plexora runs somewhere else that stops being true: Browse
lists the *server's* filesystem and a marker list on the user's own laptop —
which is exactly where one usually is, having arrived from a collaborator by
email — has no way in at all. So `channelNamesUpload.js` offers `Upload…`
beside Browse exactly when `serverIsElsewhere()`, sending the bytes with
`multipart/form-data`; not offered for a file on a data NODE, since the path
box means the server's filesystem and a node path typed into it names nothing
the server can open. `session.file` and `session.path` are mutually
exclusive — Load clears `file`, choosing a file clears `path` — so the route
never receives both.

**The reading is server-side.** `server/utils/channel_file.py` reads the file;
the client parses nothing, and cannot: a path names a file only the server can
open. `.xlsx`/`.xlsm` go through openpyxl (a core dependency, imported lazily so
a drifted environment refuses Excel with a sentence instead of failing to
start); `.xls` is refused by name with the fix.

**`POST /upload_channels` answers one of three ways** and the dialog
(`views/channelNamesUpload.js`) has a stage for each:

- **applied** — the file said which names it holds without being asked: one
  column, and a length that is either the channel count or one more than it (a
  header row). This is the common case and costs one request and no questions.
- **needs_column** (HTTP 200, not an error) — several columns, so there is no
  such thing as "the" column. The whole description of the file comes back in
  the same response — preview, per-column counts, a header guess — rather than
  in a second inspect call, because a file read twice is a file that can be
  edited in between.
- **mismatch** (HTTP 400) — the names were read and there is the wrong number
  of them. **Nothing is applied.** Half a panel renamed looks named, and every
  wrong name in it would be believed by gating.

A single-column file never reaches the picker: there is nothing to choose, so a
count that fits neither reading goes straight to the mismatch.

The per-column `nonempty` counts in the description are what let the "File
contains column headers" checkbox re-label the select and re-count instantly,
without asking the server again — `nameCount()` mirrors `channel_file.names()`
exactly, and the two are pinned against each other by
`tests/test_channel_names_upload.py` and `tests/js/channel_names_probe.mjs`.

The dialog is a native `<dialog>` + `showModal()`, like `requirementsModal.js`
and unlike `segmentationWait.js`: a modal dialog is promoted to the top layer,
*above* the fullscreen element and its opaque `::backdrop`, so it does not
need `PopoverPortal`. An ordinary positioned element on `<body>` would.

### A rename lands in place — and names are keys

**A rename moves no index.** The image is the same file and `imageData` keeps
its order, so every tile URL, `rangeConnector`, `colorConnector` and
`currentChannels` entry — all keyed by index — is still correct. That is the
whole reason `main.js`'s `adoptChannelNames(names)` can take the new names on
without a reload, and it is the same argument `adoptSegmentation` makes.

What *does* move is every container keyed by **name**, and they have to move
together:

| where | what |
| --- | --- |
| `config.imageData[i]` | `name` / `fullname`, mutated in place |
| `imageChannels`, `imageChannelsIdx`, `columns` | rebuilt in place |
| `dd` | old channel keys deleted, description re-fetched, image-side stats carried across by index |
| `ChannelList` | `columns`, `channelIDs`, `image_channels`, `hasChannelGMM`, `sel`, `sliders`, `selections`, the row label, the swatch datum |
| `ViewerSidebar` | `columns`, `markerRangeOverrides`, each slot's `name`, each marker select's options |
| the DB | `data_model.rename_saved_channels` — the saved channel list holds **names** |

**Mutate, never replace.** `config`, `imageChannels` and `dd` are held by
reference by things that outlive the call — including every plugin's
`ctx.dataset`, which reads all three *live* through getters
(`services/datasetContext.js`). Handing anyone a fresh object leaves them on the
old names. Same rule `refreshDataset` records.

**The saved channel list is the one that used to bite.** It is what
`ViewerSidebar.applySavedChannels` rebuilds slots from on *every* page load, so
a reload did not fix the stale name — it restored it. That slot then asked for
stats under a name the server no longer had, and `next(...)` with no default
raised `StopIteration`. Channel lookups now go through
`data_model.real_channel_index`, which raises `UnknownChannelError`;
`/get_image_channel_stats` and `/get_channel_gmm` turn that into a 404 with a
sentence, and the background warm-up pass skips the channel rather than
abandoning the rest.

`dd` holds two different things under one key — the feature table's stats for a
column of that name, and the image-side stats `ensureChannelStats` fetched
lazily. The image side belongs to the *channel* and is carried across (the
pixels did not change, and re-fetching would blank every open slider); the table
side belongs to the *column* and is re-read, because which marker each channel
now matches is the whole point of the rename.

Tests: `tests/test_channel_rename_state.py` + `tests/js/channel_rename_probe.mjs`
(which drives both `renameChannels` methods through
`Object.create(...prototype)`, including the two-channels-swap-names case).

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

## Navigation and the App Shell

Plexora is server-rendered and multi-page: every destination is its own Flask
document. Walking from a slide to the Figures page and back therefore used to
destroy the OpenSeadragon viewer, its WebGL2 context, every decoded tile and
every piece of session state not written to the server — the **viewport above
all, which nothing persisted at all** — and rebuild it cold on return.
`services/appRouter.js` makes that one class of navigation happen inside the
document that is already open, under one rule:

> The viewer is rebuilt when, and only when, the PROJECT changes.

**How it works.** `base.html` renders `<body data-plexora-datasource="...">` and
an empty `#plexora_page_host` after the content block. A click on an internal
link is intercepted, the destination is fetched with `X-Plexora-Fragment: 1`,
and the response — the page's own `{% block style %}` and `{% block content %}`,
nothing else — is mounted into the page host while `#container` is hidden. The
stylesheets are lifted into `<head>`, the scripts are re-created (markup
inserted as innerHTML never runs), and `history.pushState` keeps the URL honest.

**Server side is one context processor and one line per template.**
`page_routes.inject_layout` picks `_fragment.html` over `base.html` per request,
and every page template says `{% extends layout|default('base.html', true) %}`.
No route knows the router exists, and a request without the header — a
bookmark, a hard reload, JavaScript off, every other test — gets the whole
document exactly as before. `tests/test_app_shell.py` pins that a fragment is
byte-for-byte the same content the full page renders.

**`_fragment.html` also emits `data.active_tool_styles`/`_scripts`**, which on a
full page base.html puts in `<head>`. Not optional: Figure Builder's library and
canvas are whole pages whose controllers live in the plugin's script list, so a
fragment without them arrives as static markup and **looks completely correct** —
heading, tabs, search box, all in the template — while nothing ever loads and no
button does anything. Empty on every core page, so a core-only build pays
nothing and no template names a plugin.

**Three limits, each buying a large amount of safety.** They are why there is no
teardown code in this file to get wrong:

1. **Only a document that booted AS a viewer routes at all.** Landing on
   `/open_project` and clicking a project is a full navigation. There is no
   viewer to preserve yet, and booting one client-side would mean re-entering
   `main.js`, which has document-scoped top-level bindings (`const
   eventHandler`, `const datasource`) and can only run once.
2. **A link to a DIFFERENT project is a full navigation.** The server holds one
   loaded datasource (`data_model._loaded_source`) and `ImageViewer` has no
   destroy path.
3. **The viewer is hidden with `visibility`, never `display`.** OSD's autoResize
   compares its container's `clientWidth`/`clientHeight` every frame;
   `display: none` reports 0×0, resizing the viewport to nothing and taking the
   zoom with it — the exact state this exists to protect. See
   `#container.plexora-view-hidden` in viewer.css.

Anything it cannot do, it declines to do: an unroutable link, a fragment that
will not fetch or parse, all fall through to `window.location`.

**A page controller registers with `PlexoraPage.register(fn)`, not
`DOMContentLoaded`** — that event fires once per document, so a second visit to
a page would never get one. `register` mounts `fn` on the initial load and after
every swap; `fn` keeps its existing `if (!root) return` guard, which is what
makes running every controller on every page safe. It may return a **function**
to tear down anything that outlives the markup (settingsPage's migration poll,
figureWorkspace's window listeners); anything else returned is ignored, since
several of these are one-liners around a `boot()` that answers with its
instance.

**Two things a change here must not break.** A script already in the document is
never re-executed — these are classic scripts and several declare a top-level
`class`, whose re-declaration is a `SyntaxError`, and `columnClassifier`,
`coordinateField` and `segmentationProgress` are all loaded by `base.html` AND
named again by the pages that use them. And a navigation asked for while another
is in flight is **queued, not dropped**: for a `popstate` the browser has already
moved the address bar, so ignoring it leaves the URL describing a page that is
not on screen — which is what holding Back down did before the queue existed.

**Deliberately still full navigations**, both marked at the call site: saving on
the project edit page, and `segmentationProgress`'s redirect. Each has just
changed what the project IS, and a running viewer holds the config, the column
statistics and a loaded datasource from before it. The edit page's save takes
that reload even when the mask pyramid is still converting — it used to hold the
form behind a blocking overlay until the job finished, and now hands the wait to
the viewer (`segmentationWait.js`) instead.

**The viewer leaving and returning is `onHide()`/`onShow()`.** `toolLoader.js`
turns `plexora:viewer-hidden` / `plexora:viewer-shown` into the hook a plugin
already implements, so ROI's pen and document-level keys stand down under a
routed page without any plugin learning a second lifecycle. Figure Builder's
`onShow` is also where it reads the pending-edit note the canvas leaves in
`sessionStorage` — `applyOrDefault` only ever ran at tool boot, which used to be
the only way back into the viewer.

Covered by `tests/js/app_router_probe.mjs` (16 checks, driven from
`tests/test_app_router.py`), `tests/test_app_shell.py`, and the viewer-visibility
half of `tests/js/tool_switch_probe.mjs`.

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

**Mini-map (overview lens).** `data_model.generate_channel_overview` builds a
lossless mode-`L` WebP from the already-resident `zarray` (the downsampled
~200-400px per-channel array `load_datasource` keeps), quantized with the same
`get_channel_quantization_window()` the tile path uses — which is what makes
the lens's contrast match the viewer for free. Measured on a real 298x357
array: lossless is 50408 B / 3.0 ms and byte-exact, versus quality=90 at
23514 B / 3.4 ms with max error 11 grey levels — cheap enough that there is no
reason to take the tile path's lossy tradeoff here, and a narrow contrast
window would multiply that byte error into a visibly wrong lens. Warm
server-side generation measured at 3.3 ms/channel for 19 channels (the first
call per channel additionally pays the one-time full-res `.max()` for the
quantization window, which the tile path pays anyway and caches). Client side,
Playwright against a real datasource (3 channels, chromium/ANGLE) measured pan
median 8.3 ms both with the lens collapsed and expanded — identical to the
documented pan baseline above — with zero `_draw()` calls across 100 pan
frames, zero network requests until the lens is first opened, and no request
on a colour change or a re-expand. `tests/js/mini_map_probe.mjs` +
`tests/test_mini_map.py` cover the client geometry/shader/guard logic (61
probe checks, 19 pytest tests, 15 of them mutation tests) and
`tests/test_channel_overview.py` covers the route and the quantization
contract, including that the Area placeholder does not shift the zarray
channel index.

If the lens opens to a black circle, check the server's age before the code —
see "A Python change needs a restart" under Key Invariants. `MiniMap._updateNote`
now says so on screen: a 404 on every active channel prints "restart the Plexora
server", any other total failure prints a generic message, and a partial failure
(one channel of several) stays silent because that draws a perfectly good map
with one colour missing. Failures are recorded per channel (`_failed`, srcIdx ->
status) rather than in one last-error field, because the fetches drain
concurrently and a scalar is won by whichever request happens to finish last.

## Key Invariants

- **`[tool.setuptools.packages.find]` namespace discovery must stay ON** (the
  default -- do not add `namespaces = false`). `plexora/server` and its
  `models/`, `routes/`, `utils/` subpackages have no `__init__.py`, so turning
  discovery off silently ships a wheel with no server in it while the build
  still looks successful. The `exclude = ["plexora.client*",
  "plexora.plugins.*.tests*"]` list exists because leaving discovery on also
  sweeps up `plexora/client/node_modules/flatted/python/flatted.py` and every
  plugin's `tests/` directory. Check with
  `python -m zipfile -l <whl>` and confirm it lists
  `plexora/server/models/data_model.py`.
- **`cli.py` and `connect.py` must stay importable without the `plexora`
  package.** `tests/test_cli.py` and `tests/test_connect.py` load them straight
  off disk with `spec_from_file_location`, and a PyInstaller onefile build puts
  the package somewhere an importlib file loader cannot reach. Anything from
  the package goes in a lazy import INSIDE a function (`_run_where`,
  `_run_config`, `_run_connect`, `_run_node_connect` are the pattern). This is why `cli.py` keeps
  its own copy of `_clean_base_url` rather than importing `plexora._url`;
  `tests/test_url_helpers.py` pins the two against each other.
- **No token ever goes on a command line.** Everything in a remote command is
  visible in `ps` to every other account on a shared login node. `plexora node
  serve` generates its own token and prints it on stdout (`[plexora-node]`,
  inside the ssh channel); the registration that uses it is POSTed through the
  tunnel to the far viewer's own `/settings/nodes`. Ports on argv are fine and
  are chosen locally up front, because an `-L`/`-R` forward is fixed when the
  connection opens. `remote_sessions.redact()` keeps the same token out of any
  log tail a page can show.
- **A saved remote profile stores no secret.** `remotes.py` has no field for a
  password; credentials reach ssh through `askpass.py` and live in memory for
  the seconds between the user typing one and ssh consuming it -- or, when
  a connection has more hops to authenticate, until it is open or has
  failed (`_forget_secrets_locked`). Pinned by
  `tests/test_remote_connect.py`, including that the answer appears in no
  status payload and that a one-time code is never replayed.
- **Environment detection only ever fills in flags the user did not type.**
  `should_detect` returns False for any of `--ood/--remote/-r/--bind-node/
  --base-url/--host/--login-host`, for `PLEXORA_HOST` in the environment (the
  Docker image sets it and means it), and for `--no-detect`. Every failure
  inside it means "we learned nothing", never a traceback in front of somebody
  who wanted a local viewer.
- **Subcommands are split off `argv[0]`, not by an argparse subparsers
  action.** A subparsers action is itself a positional, so on one parser with
  the optional `datasource` positional it takes first refusal on the only
  argument (`plexora tonsil` → "invalid choice") and, when a subcommand DOES
  match, the trailing positional then resets `datasource` to None afterwards,
  discarding what the subparser just read. `cli.split_command` +
  `cli.build_parser(command)` is the fix; do not merge them back.
- **A browser cannot serve a file by path.** Reading a file in place needs a
  process on that machine, and that process is the data node `plexora connect`
  starts on the user's laptop by default (`--no-local-node` to opt out). Without
  it, choosing "Local" on a data field degrades to a CSV upload plus a sentence
  naming the command to fix it — for an image, a mask or an .h5ad there is
  nothing else a browser alone can do.
- **A resource id is derived from the file's own path, never generated.**
  `nodes.resource_id_for` hashes the path; a project's binding and a node's
  manifest only meet again because both were computed from the same filename,
  with nothing exchanged between sessions to reconcile them — which is what
  lets a project reopen in a later session with no reconfiguration.
- **Where the primary image LIVES can change; which image it IS cannot.**
  `nodes._same_image` runs before `attach_image`, `attach_segmentation` and
  `detach("image", ...)`, and compares width/height/`num_channels`. Every ROI
  outline, figure panel and cell coordinate a project holds is expressed in
  that image's pixel space, and nothing downstream would notice a swap to a
  same-sized-but-different image — it would render, and mean something else.
- **A missing local mask or table degrades to the resource-status banner; a
  missing image still fails loudly.** The image is the floor of the contract
  (see `api/dataset.py`'s `Dataset.image`); a mask or table a node has stopped
  serving is something `data_routes.resource_status` can name and the viewer
  can keep working around.
- **A node manifest holds kinds, ids and paths only — never a project, a role
  or a read spec.** `--manifest PATH` lets a `--dynamic` node come back
  serving the same resources under the same ids; what those resources MEAN is
  recorded only on the primary, in config.json, same as everything else a node
  is not trusted with.
- **A data node's address is not a mount path.** `nodes.json` holds absolute
  endpoints (and optionally a browser-side address that may be portal-relative);
  none of them goes through `clean_prefix`, which is about where THIS app is
  mounted. Likewise `node://<node>/<resource>` is written where a filesystem
  path would go in config.json, so anything that stats, resolves or migrates a
  stored path must test `providers.is_node_locator()` first --
  `Path("node://hpc/cells")` is a perfectly valid relative path that exists
  nowhere, and on Windows it silently becomes `node:\hpc\cells`.
- **A tile URL has exactly one `?`.** The HD flag used to be written as a bare
  `"?q=hd"` in `getTileUrl`, which is a second `?` the moment a tile is fetched
  from a node and carries its token too. Anything added to that query joins
  with `&` through the same list; `tests/js/tile_url_probe.mjs` pins it,
  because a URL with two `?` fetches successfully and returns the wrong thing.
- **Whether the browser can reach a node is the browser's question.** The
  server offers a candidate (`/resource_routing`) and the browser probes the
  node's own health endpoint before using it, falling back to the proxy on
  anything short of a clean answer. A server-side guess would be wrong in
  exactly the deployments this exists for -- a cluster node reachable from a
  laptop through a tunnel and from nowhere else, a portal that rewrites
  addresses. Direct routing additionally requires the node to have been started
  with `--allow-origin <viewer origin>`; without it the probe fails and
  everything silently proxies, which is correct but worth knowing when
  measuring. **Only a remembered `true` verdict is reused** from
  `resourceRouting.js`'s sessionStorage cache — a remembered `false`
  (unreachable) always re-probes. A node mid-restart or a tunnel not yet up is
  a fact about a moment, and caching it pinned the whole tab to the proxy hop
  silently for as long as the tab stayed open, even long after the node came
  back; re-probing costs at most `PROBE_TIMEOUT_MS` once per load.
- **A read that is proportional to the table never crosses a node boundary.**
  The primary keeps a compact copy (the cell id, the coordinates, and the
  columns filling a role) so the spatial index, the centroid layers and the
  hover lookup answer locally; everything else is a column at a time or a
  bounded result. `TableHandle.frame()` therefore REFUSES for a node-backed
  table -- a frame missing every marker but answering `frame["id"]` perfectly
  well is the shape of bug that passes every test. Use `geometry()` when ids
  and positions are what you meant.
- **Work that reads the file and the loaded frame together runs where the file
  is.** Every scientific write-back checks the file's row count against the
  loaded table before touching anything, and that check means nothing across a
  network. Those are `@table_operation`s, not provider reads, and their
  refusals travel as data (`{"ok": false, "reason": …}`) so a "column already
  exists" stays something a user acts on.
- **A background job captures the registry it was started against.** The
  segmentation job outlives a delete, a project switch and a data-directory
  change; resolving `Project.config_path_for` when it FINISHES answers a
  different question by then. `start_segmentation_job` resolves the config path
  up front and passes it down, and declines to reload if the registry moved.
- **A derived label pyramid is located from the mask's path, never the
  project's.** `segmentation_pyramid.resolve_derived_mask` is the single answer
  to "where is it, and where would a new one go" — beside the source by
  default, the project's `derived_root` as fallback, BOTH always searched.
  Beside-the-source is what lets a second project and a data node (which has no
  project at all, so nothing to look under) reuse one conversion; the fallback
  is what keeps a read-only source directory working and what finds every mask
  built before this convention. `paths.mask_output_preference()`
  (`plexora config set mask-output beside|project`) swaps the order for both
  halves together — a preference that moved writes but not lookups would
  disagree with itself the moment a pyramid existed in both places. Callers
  with a recorded `segmentationSourceKey` still use it (`refresh_segmentation_
  mapping`); the ones without — a fresh import, a node — fall back to "ours, of
  this mode, not older than the source".
- **`segmentationMode` missing is read as "outlines", not as "unknown".** Both
  `viewerControls.canDrawFilled()` and `imageViewer.renderLabelTile()` test it
  against `"filled"`, so an absent key greys Filled out ("stored as outlines,
  nothing to fill") and paints a filled label pyramid as solid blobs with
  Outlines selected — wrong picture, no error anywhere. Every path that records
  a mask must therefore record a mode. Locally `refresh_segmentation_mapping`
  backfills it; for a mask on a NODE that refresh is skipped wholesale (nothing
  here to fingerprint or convert), so the mode comes from the node's
  `/hello` `mask_mode`, which is `Resource.mask_mode` — decided once by
  `app._convert_mask_if_needed`, which returns the mode of whatever is left
  being served in every branch. It cannot be re-derived from the file: the two
  branches that skip conversion because the user's own mask is already fine (a
  servable label pyramid; a mask that already looks like outlines) leave no OME
  marker for `generated_mask_kind` to read. `load_config` backfills node-backed
  entries that predate this, and `nodes.attach_segmentation` falls back to
  `DEFAULT_MODE` when an older node reports nothing.
- **A full origin never passes through `clean_prefix`.** `PLEXORA_BASE_URL` and
  `app.config['PLEXORA_BASE_URL']` hold a MOUNT PATH. Colab's proxy is a whole
  origin (`https://….googleusercontent.com`), and prefixing that with "/" gives
  `/https:/…` — a valid-looking path that fails nowhere near the mistake.
  `clean_prefix` raises ValueError on one; origins belong in the DISPLAY url
  via `join_display`.
- **`--plugins ""` is not `--plugins` unset.** Unset means "activate everything
  installed"; `""` is a deliberate core-only build. Anything forwarding the
  setting to a child must omit the flag entirely when it is unset — passing
  `""` is what silently disabled every plugin behind the jupyter-server-proxy
  launcher tile. And **the value cannot cross a process boundary in an
  environment variable on Windows**: `SetEnvironmentVariable(name, "")` deletes
  the variable, so a child launched with `PLEXORA_PLUGINS=""` reads "unset" and
  activates everything — the exact opposite. Pass it in **argv** and let the
  child write it into its own `os.environ` before `import plexora`
  (`server_cli.main()` does exactly this, which is what makes the notebook
  sidecar and the proxy tile correct).
- **No entry point can set `PLEXORA_PLUGINS` in time by itself.** Blueprints
  are registered during the first `import plexora`, and reaching `cli.main` at
  all requires that import — the console script is generated as
  `from plexora.cli import main`, and `python -m plexora` imports the package
  to find `__main__`. Writing the variable inside `main()` therefore lands
  after the decision it is meant to make, and `--plugins` did nothing from
  either command. `cli.maybe_reexec_for_plugins` re-execs once into
  `cli.bootstrap_program(...)` — a `python -c` program that sets the variable
  before importing anything — and only when `--plugins` was passed and the
  environment disagrees. Do not "simplify" it back into `main()`.
- **Open OnDemand is reached through `/rnode/`, never `/node/`.** The portal
  offers both doors (`node_uri` / `rnode_uri` in `ood_portal.yml`): `/node/`
  forwards the request path UNSTRIPPED, which suits Jupyter because Jupyter is
  started with a matching `base_url`, and guarantees a 404 for Plexora, which
  always serves at root and uses its base URL only to generate links.
  `/rnode/` strips the prefix. jupyter-server-proxy is irrelevant on OOD
  either way — it would have to be in the Jupyter SERVER's environment, which
  on a typical site is an admin-controlled module. `notebook_env.OOD_NODE_RE`
  matches the discovered prefix; do not re-run `discover_jupyter_prefix` to
  test it, because that function prints when several servers are running.
- **The auth guard activates on a token, never on a bind address.** It is
  registered unconditionally in `create_app()` (which runs once per
  interpreter, so a conditional registration could never be corrected later)
  and reads `app.config['PLEXORA_AUTH_TOKEN']` per request. The Docker image
  binds 0.0.0.0 deliberately and shares one server deliberately; it sets no
  token and must stay open. Nothing is exempt from the guard, health probes
  included — which is why `jupyter._wait_until_ready` takes a `token` and
  `cli.main` writes the token onto `app.config` as well as the environment
  (create_app already ran by then, exactly as for `PLEXORA_BASE_URL`).
- **`resolve_display` decides the bind host too.** It returns a `Resolved`
  NamedTuple rather than a pair so that the URL and the bind cannot disagree:
  the OOD route is unreachable on loopback, and every other route depends on
  staying there. A separate "what should I bind" helper is the shape to avoid.
- **A Python change needs a full server restart; a client change does not.**
  `server_cli` hands the app to `waitress.serve`, which has no reloader, and
  Flask binds routes at import — while Jinja templates and everything under
  `client/src/` are read from disk per request. So a live process serves the
  NEW frontend against the OLD backend: a newly added route 404s while the
  feature that calls it looks fully deployed. This cost real debugging time on
  the mini-map, whose lens, circle, viewport indicator and drag all worked
  against a server that had never heard of `/generated/overview`. Before
  suspecting the code, compare `ps -eo pid,lstart | grep plexora` against the
  source mtime.
- **Tile size comes from the zarr chunk shape**, so HTTP tiles map 1:1 to TIFF
  tiles. `data_model.convertOmeTiff` currently hardcodes `chunks = (1, 1024, 1024)`
  for multiscale files instead of reading the real shape — fine for 1024-tiled
  sources, silently wrong (4× or 16× read amplification) for others.
- **There is no tile level that reliably holds the whole image.** `convertOmeTiff`
  does not build a pyramid — it reads `maxLevel = len(channels)` from whatever
  wrote the OME-TIFF — while `tileWidth` is hardcoded 1024, so the coarsest
  level is a 1x1 tile grid for some files and 4x4 for others. This is why the
  mini-map (`generate_channel_overview`, `GET /generated/overview/<datasource>/<channel>`)
  has its own route instead of reusing the tile route.
- **The pyramid is real and used.** `_zarr_level(channels, level)` indexes the
  level group. When the source is a bare `zarr.Array` (non-pyramidal), `level` is
  ignored and every tile reads full resolution.
- **`qmin`/`qmax` must come from full-resolution data.** The downsampled `zarray`
  overview is mean-pooled, which dilutes single-pixel peaks and causes whole
  channels to saturate. `get_channel_quantization_window()` is deliberately split
  out of `get_channel_gmm()` so the tile path does not pay for the ~1 s
  GaussianMixture fit it does not need. The full-resolution read itself
  (`quantization_window_of`) is slabbed (`_WINDOW_SCAN_SLAB_BYTES`) and
  bounded to two concurrent scans process-wide (`_WINDOW_SCAN_GATE`): as one
  whole-plane `np.asarray(...).max()` it was gigabytes in a single numpy
  call, and a node whose startup warm-up walks every channel
  (`node/app.warm_resources`) spent minutes answering nothing at all -- not
  even `/node/v1/health` -- right after registering. Observed live against
  two clusters; the globe said "Not answering" over a machine that was
  merely busy. The mini-map honours this by quantizing
  pooled `zarray` pixels against the full-res window rather than deriving the
  ceiling from `zarray` itself, which is the mistake this invariant warns
  against.
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
  and gate flows act on it, and its panel is expanded — exactly one, or none,
  with one sanctioned exception below). Opening a tool makes it all three and
  stands the previous one down to loaded; its card's eye turns it back on and
  PINS it, and a pinned layer is exempt from the stand-down (the default is
  for the first switch, not a rule that keeps dismantling a stack). The other
  exception is `openToolAlongside(toolName, anchorToolName)`, which opens a
  tool WITHOUT standing the anchor down, forming a COEXISTING PAIR: both cards
  stay expanded and both layers stay drawn while the selection moves freely
  between them, and `isCoexisting(name)`/`coexistPartner(name)` let a
  controller ask whether it is one half of one. Cell Explorer's
  `#cell_explorer_open_roi` button is the only caller, because its ROI
  composition card only means anything while the ROI overlay it summarises is
  still drawn underneath — opening a tool from the Tools menu is unaffected.
  Opening a third tool folds both halves and clears the pair; closing or
  removing either half promotes the survivor to sole active tool;
  `tests/js/tool_coexist_probe.mjs` + `tests/test_tool_coexistence.py` pin all
  of that. Cards drag to restack (`window.Sortable`,
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
- **A plugin can announce a hover without knowing who is listening.** ROI
  (`plugins/roi/static/roiTools.js`) tracks pointer hover on the ROI overlay
  with an `OpenSeadragon.MouseTracker` on `viewer.canvas` (rAF-throttled,
  suppressed mid-gesture) and dispatches `plexora:roi-hover` /
  `plexora:roi-unhover` on `window` — a plain DOM CustomEvent, not a plugin API
  call, because ROI has no reason to know Cell Explorer exists. The hover
  detail's `anchorRect`/`viewportRect` are computed once on hover-enter, not
  per pointer move, and are in CLIENT pixels (the canvas bounding rect already
  folded in) so a listener never has to know about OpenSeadragon coordinate
  spaces. A client-pixel anchor is stale the moment the picture moves, so ROI
  re-announces the standing hover on `viewport-change` (`viewportMoved` /
  `reanchorHover`, one dispatch per frame, re-testing what is under the pointer
  because a zoom can carry a shape out from under it). Do NOT make listeners
  close on `viewport-change` instead: no pointer event follows one, so the
  region has to be left and re-entered before anything can be seen again, which
  reads as a hover the tool missed. Cell Explorer's `cellExplorerRoiBridge.js`
  (`plugins/cell_explorer/static/`, listed in `__init__.py`'s `scripts` before
  `cellExplorerSidebarController.js`) is the one listener today: it renders a
  floating composition card, fetching every cell centre once per session via
  core's `viewer.numericData.loadCells()` and using them UNSCALED — raw
  full-resolution image pixels — because that is the space ROI geometry is
  stored in. That fetch is warmed when the user asks for the ROI tool and the
  card shows a pending state if a hover beats it; every later hover is
  synchronous. It tallies membership by the active categorical variable only; a
  continuous column is gated out, since a composition card has nothing to
  count. **Hidden categories are excluded, counts and all** — the legend is how
  somebody narrows the question being asked of the slide, so the card answers
  the narrowed one, and the total is summed from the shown rows so the fixed
  0–100% bars stay comparable between regions. `recolor()` is the single funnel
  that keeps an open card truthful (hide, show, All/None, colour change).
  Because ROI geometry objects are replaced rather than mutated on edit, the
  bridge revalidates on every `store.onChange` by identity rather than a deep
  compare. `tests/js/roi_hover_probe.mjs` covers the announcing half (what is
  dispatched, when, in which coordinate space, and the pan re-anchor) and
  `tests/js/cell_explorer_roi_bridge_probe.mjs` the answering half (membership
  checked against a brute-force count, the tally, hidden categories, the
  ranking and `Other`), wired up by `test_roi_client_js.py` and
  `test_cell_explorer_roi_bridge.py`.
- **Changing a client file means bumping its `?v=` tag** in the template that
  loads it (and `plugins/<name>/__init__.py`'s `VERSION` for plugin assets, which
  stamps every URL `asset_urls` builds). Sources are served straight from
  `client/src/`, so a stale tag means the browser keeps running the old file and
  the fix looks like it did nothing. `viewerManager.js` and `glRenderer.js` are
  the exceptions: they are webpacked into `client/dist/vendor_bundle.js`, which
  has to be rebuilt *and* re-tagged.
- **A page template extends `layout`, not `'base.html'`.** Hardcoding the base
  back in makes that page unroutable: it would come back from a fragment fetch
  as a whole second document — navbar, `<head>`, another `<body>` — to be
  inserted next to the live viewer. `tests/test_app_shell.py` walks every page
  in both shapes.
- **A new page controller mounts through `PlexoraPage.register`.** A
  `DOMContentLoaded` listener works exactly once, so the page would be correct
  the first time it is opened and inert on every visit after that — with nothing
  in the console to say why. See "Navigation and the App Shell".
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

Python environment is the conda env `plexora`. The path differs per machine --
`C:/Users/aj/.conda/envs/plexora/python.exe` on Windows,
`/Users/aj/miniconda3/envs/plexora/bin/python` on macOS. Plain `python` is the
miniforge base env and has no Flask, so it is not a fallback.

```bash
# Test suite
python -m pytest -q -p no:randomly
```

The per-field Local/Remote data-location work added four test files
(`tests/test_node_dynamic.py`, `tests/test_data_location.py`,
`tests/test_reconnect.py`, `tests/js/data_location_probe.mjs`); the macOS line
below was reverified against a real run after it, and the Windows one was not.
The follow-up that made the switch appear on every launch and made Remote a
saved SSH connection chosen mid-form added no new files -- it extended those,
`tests/test_connect.py` (`NodeSession`, `as_node_kwargs`) and
`tests/test_remote_connect.py` (the two session kinds).

The listing-picker completion (`dir_listing.py`'s rewrite, `/picker_prefs`,
the `pathPicker.js` rebuild) added `tests/js/path_picker_probe.mjs` (73
checks, run with `node tests/js/path_picker_probe.mjs`) and
`tests/test_path_picker.py` (the pytest wrapper plus wiring assertions), and
extended `tests/test_browse_routes.py` and `tests/test_node_dynamic.py`.

Current healthy state on Windows/conda: **1921 passed, 1 failed, 0 skipped**
(2026-08-27, after the multi-source data-node work and the move of derived
segmentation masks to beside their source; not reverified against the
Figure Builder image-toolbar rebuild below). With
`plexora/plugins` on the path -- `testpaths` includes it. The one failure fails
on a clean tree:
`test_quick_view_routes.py::test_quick_view_dedupes_name_on_repeat_registration`.
`test_register_image_datasource.py::test_derive_dataset_name_from_path` is a
Windows path assertion, so it fails on macOS and passes here -- expect **2
failed** on macOS.

The unified connection architecture (`services/remoteState.js`, one owner of
remote-connection polling; `services/connectionModal.js`, the one dialog for
connecting from anywhere; `server/models/recipes.py`, the "Add a server"
preset catalogue; `services/remoteGlobe.js`, the navbar globe) added
`tests/test_remote_state.py`, `tests/test_connection_modal.py`,
`tests/test_recipes.py`, `tests/test_remote_globe.py`, and probes
`tests/js/remote_state_probe.mjs`, `connection_modal_probe.mjs`,
`remote_globe_probe.mjs`; it extended `tests/test_connect.py`,
`tests/test_remote_connect.py` and `tests/js/data_location_probe.mjs`.

The follow-up pass (one connection concept, `services/logTerminal.js`, the
globe's two-line rows, `GET /remote_health`) added
`tests/test_settings_remotes_page.py`, `tests/test_remote_health.py` and
`tests/js/settings_remotes_probe.mjs`; it rewrote `tests/js/remote_globe_probe
.mjs` and extended `connection_modal_probe.mjs` and `test_connection_modal.py`.

Making a disconnect quiet (`nodes._disconnected`, `http.speculative()`, and
the cache warm-up's calm exit) added `tests/test_node_disconnect.py` (9 tests);
it extended `tests/test_remote_health.py` -- whose `json_request` stub has to
carry `allow_disconnected` -- and the root `conftest.py`, which clears the
`_disconnected` set between tests because it is process state and the suite
reuses node names and loopback ports across tmp roots.

Making a RECONNECT visible to an already-loaded project
(`nodes.address_generation`, `_NodeBacked.node`'s re-resolve,
`ProviderSet.held_addresses` → `data_model.held_node_addresses()`, and
`/remote_health`'s `stale`) added `tests/test_node_reconnect.py` (8 tests) and
one check to `tests/js/remote_globe_probe.mjs`; `conftest.py` clears
`nodes._addresses` alongside `_disconnected`. This is the counterpart to the
paragraph above and the two must stay distinct: a node REMOVED from the map is
a disconnect and keeps its own sentence, while a node still on the map at a new
address is a reconnect and is picked up silently. The two meet in
disconnect-then-reconnect, which is a MOVE even though it writes over an
absence -- `test_disconnecting_and_connecting_again_is_a_move` is the one that
catches getting that backwards.

Note that three separate readers resolve a node's address and only one of them
was ever stale, which is what made this so hard to see from the symptoms:
`/resource_routing` (the browser's DIRECT tile path) and `/remote_health` both
`node_registry.find()` freshly per request, so tiles kept arriving and the
globe stayed green while every value the SERVER computed -- GMM, stats, a
proxied tile -- was refused. "Manual contrast works, Auto 503s" is that split.

Two features on top of that pass -- the countdown on a scheduled job
(`recipes.walltime_seconds`, `RemoteSession`'s clock, `nodes.json`'s
`expires_at`, `services/sessionExpiry.js`) and the connect offer for a project
whose node is not up (`node_for`/`NodeImageProvider.open`/
`NodeSegmentationProvider.open` no longer swallowing `ResourceUnavailable`,
`/resource_status`'s `profiles`, `POST /reload_datasource`,
`services/resourceStatus.js`) -- added `tests/test_session_walltime.py` (33),
`tests/test_unreachable_node.py` (20), `tests/test_session_expiry.py` and
`tests/js/session_expiry_probe.mjs`; extended `remote_globe_probe.mjs`,
`remote_state_probe.mjs`, `settings_remotes_probe.mjs` and
`resource_status_probe.mjs` with their wrappers, and
`tests/test_node_table.py`, whose `/resource_status` assertion is
shape-exact. Both probe stubs of `PlexoraRemotes` now have to carry
`remaining`/`duration`/`WARN_SECONDS`, and the resource-status probe's fake
element needed `append`/`showModal`/`close`.

Four follow-up bugs from using the two: a countdown that outlived its own
connection (`RemoteSession.expires_at`'s liveness test, `remaining()`'s, and
the expiry dialog closing when its clock goes), a project whose node had
been disconnected opening silently onto cached tiles (`/resource_status`'s
`ensure_loaded` and `_nodes_that_have_gone`, and `resourceStatus.forget()`),
and every tile that project then asked for arriving as an unhandled 500 with
a full traceback (`create_app`'s `ResourceUnavailable` handler).
Both probe stubs of `remaining()` -- in `session_expiry_probe.mjs` and in
`remote_state_probe.mjs`'s expectations -- mirror the liveness guard, so a
change to it has to be made in both. `resource_status_probe.mjs` no longer
asserts that a dismissed project skips the request: it must ask, because "it
is fine now" is the answer that clears the memory.

Thumbnails for a node-backed image and the globe's viewer-attachment match
(`data_model._node_thumbnail_plane`, `/data_places`'s `registered_node`,
`remoteGlobe.nodeNameOf`) added four tests to `tests/test_node_image.py` and
two to `tests/test_remote_health.py`, extended `tests/js/remote_globe_probe
.mjs` and `remote_state_probe.mjs` (with their wrappers in
`test_remote_globe.py` and `test_remote_state.py`), and regenerated
`tests/golden/boundary_*.json` -- the goldens record every page's `?v=` asset
tags, so bumping one in `base.html` requires
`PLEXORA_UPDATE_GOLDEN=1 pytest tests/test_plugin_boundary.py`.

The shared Local/Remote file-location layer (`utils/file_transfer.py`,
`routes/transfer_routes.py`'s `/fetch_file`/`/put_file`, `node/api.py`'s
`/node/v1/read_file`/`write_file`, `services/fileLocation.js`) added
`tests/test_file_transfer_node.py` (13), `tests/test_transfer_routes.py` (11),
`tests/test_file_location.py` (20) and `tests/js/file_location_probe.mjs` (30
checks, run with `node tests/js/file_location_probe.mjs`); it extended
`providers/http.py`'s `request()` (`raw_body`/`allow_status`) and
`tests/node_harness.py` (`post_bytes`), and touched `gatingApi.js`, `roiApi.js`,
`csvGatingList.js`, `dataLocation.js` and `channelNamesUpload.js` without
adding files for any of them.

Making a data-node death or a reconnect propagate without a reload
(`RemoteSession._tidy_after_end` and its `unregister` callback,
`settings_routes._forget_node_entry`, `services/resourceRouting.js`'s
`held`/`refresh`, `remoteGlobe.staleNodes`, `remoteState.js`'s `nodeChanges`
and the `plexora:remote-nodes-changed` event, `main.js`'s `applyRouting`/
`rebuildTileLayers`/`repairRouting`) added 7 tests to
`tests/test_remote_connect.py` (self-exit unregisters the node, a sibling
tunnel is stopped, a deliberate disconnect still leaves the forgetting to the
route, a failed connection releases its `connect._ACTIVE` entries, starting
over a dead session reaps it first, the connect route wires `unregister`
through, and `_forget_node_entry`'s own `managed_by` guard); it extended
`remote_state_probe.mjs` (a dispatched-events collector, +2 checks),
`remote_globe_probe.mjs` (+3 checks, including an integration section stubbing
`PlexoraRouting.held`) and `resource_routing_probe.mjs` (+2 sections), and
regenerated `tests/golden/boundary_*.json` for the `?v=` bumps on
`resourceRouting.js`, `remoteState.js`, `remoteGlobe.js` and `main.js`. **A
probe context that loads `remoteState.js` or `remoteGlobe.js` now needs a
`window` stub with `dispatchEvent` and `CustomEvent`**, since both files
dispatch `plexora:remote-nodes-changed` directly rather than through a
subscriber callback.

On macOS/conda, after that pass (2026-08-30): **2579 passed, 3 failed, 2
skipped**, with `python -m pytest -q -p no:randomly`. The 3 failures are the
same three as the file-location-layer baseline below — the quick-view dedupe
test, the Windows-path assertion, and the in-flight `settings.html` rewrite's
`test_connection_modal.py` failure, none of them caused by this pass. All JS
probes pass.

On macOS/conda, after that pass: **2481 passed, 3 failed, 2 skipped**, in
~4:13, with `python -m pytest -q -p no:randomly`. The third failure is
`test_connection_modal.py::test_one_connection_concept_reaches_the_page_that_explains_it`
and is NOT a baseline: the in-flight uncommitted `settings.html` rewrite in
this tree dropped the string it asserts (`git show HEAD:...settings.html`
still has it). Before it: 2360 passed, at the countdown/thumbnail pass, in
~3:30. Before that: 2318 passed.

On macOS/conda, at the disconnect pass: **2318 passed, 2 failed, 2 skipped**, with
`python -m pytest -q -p no:randomly`. The 2 failures are the same two named
above (the quick-view dedupe test and the Windows-path assertion in
`test_register_image_datasource.py`); the 2 skips are the same Font Awesome
icon-name pair, which skip when `plexora/client/node_modules` has no
`@fortawesome` package. Before it: 2303 passed at the unified connection
architecture.

Before that, on macOS/conda, after the browser-based file explorer's
completion (2026-08-28, `dir_listing.py`'s rewrite, `/picker_prefs`, and the
`pathPicker.js` rebuild): **2199 passed, 2 failed, 2 skipped**. The 2 failures
are the same two named above. The line before this one read 2156 passed, at the
location-chosen-when-data-is-added work (`placePicker.js`,
`connect.NodeSession`, `/data_places`, node `list_dir`, the Settings cleanup,
the per-field mount fix below, and node-backed quick view), in ~3:23 with
`-p no:randomly`. Before that, 2129 passed, at the per-modality Local/Remote
data-location work (`dataLocation.js`, dynamic node resources, the CSV upload
and the reconnect degradation). The
line here before that read 1927 passed as of the Figure Builder image-toolbar rebuild
(2026-08-27, `figureChoiceField.js`, the panel/legend/scalebar schema and
rendering changes). The 2 skips are `tests/test_icon_names.py`'s pair
(`test_every_icon_name_is_one_font_awesome_ships`,
`test_the_scan_would_notice_a_dead_name`): its module-scoped `shipped` fixture
skips both when `plexora/client/node_modules/@fortawesome` is absent, and on
this checkout `node_modules` is a partially-synced Dropbox copy holding only
`cross-spawn` and `mockttp`. Orthogonal to the reportlab story above -- see
Sharp Edges.

`plexora/plugins/figure_builder/tests/test_figure_builder_routes.py::test_downloading_before_the_job_finishes_is_409`
is order-dependent and flaky under `-p no:randomly` when only the figure_builder
subset is run -- it is not part of the two known failures above and its result
depends on what ran before it.

**`tests/golden/boundary_*.json` records every page's script and stylesheet
list**, so adding a `<script>` to base.html fails five tests until the goldens
are regenerated with `PLEXORA_UPDATE_GOLDEN=1 python -m pytest
tests/test_plugin_boundary.py`. Read the diff before accepting it -- that is the
whole point of the file.

**A test that writes `os.environ` directly must claim the variable with
`monkeypatch.setenv` first.** `cli.main()` writes `PLEXORA_BASE_URL` and
`PLEXORA_AUTH_TOKEN` into the real environment, and monkeypatch cannot undo a
write it did not make -- a leaked `PLEXORA_BASE_URL` then prefixes every route
in `tests/test_plugin_boundary.py`'s golden inventory, which runs in a
subprocess and inherits it. `tests/test_cli.py::_inside_a_job` is the pattern.

`pytest-randomly` is installed; the suite is order-stable, but pass
`-p no:randomly` anyway for a comparable baseline when counting failures.

**The data-node tests start a real second process.** `tests/node_harness.py`
spawns `python -m plexora node serve` on a free port and talks to it over a
socket; `tests/test_node_table.py`, `test_node_image.py`, `test_node_routes.py`
and `test_node_dynamic.py` (the `--dynamic` add/status/remove/browse endpoints)
run against it. That is deliberate -- the failures this architecture actually
has are failures of the seam between two processes (a header not exposed to a
browser, a body decoded twice, a float32 cast eating a text column, a lock held
across a stream), and a stub is a seam with nothing on the far side. It costs
~2 minutes of the suite's ~4:40. The node gets a data root of its own outside
`tmp_path`: a node must never be able to reach the primary's registry, and on
Windows a directory the child touched breaks pytest's cleanup in a later test.
`tests/test_data_location.py` and `tests/js/data_location_probe.mjs` pin the
Local/Remote switch itself (which shape each choice produces, per field), and
`tests/test_reconnect.py` pins `data_routes._reconnect_hint` -- that a node a
saved connection manages names the `plexora connect` command rather than
pointing at Settings, which cannot fix a tunnel that is gone.

**`spatialdata` is installed in the conda env** (0.8.0, verified 2026-08-24 on
Windows), so `tests/test_spatialdata_adapter.py` and the SpatialData cases in
`plexora/plugins/gating/tests/test_anndata_gates.py` run there. Do not pass
`--ignore=tests/test_spatialdata_adapter.py` unless an import actually fails --
the counts above are from a run that DID ignore it, so a full run reports more.
`.venv/` is a partially-synced Dropbox checkout (empty `pip list`, missing
`click`); ignore it.

**Pointing a test at its own data directory.** The repo-root `conftest.py` has
an autouse `plexora_data_root` fixture that sets `PLEXORA_DATA_PATH` to
`tmp_path` and calls `paths.reset()`. That is all a test needs — nothing binds
the root at import any more, so one env var reaches every module. Use
`tests.helpers.use_data_root(monkeypatch, root)` to point at a *different*
directory and `use_shared_roots(monkeypatch, *roots)` to add shared ones.
(This replaced ~108 per-module `monkeypatch.setattr(module, "data_path", ...)`
calls across 31 files, which had to name each module that had imported the
constant and silently missed any added later.)

**`data_model`'s module globals leak across test files.** It keeps the loaded
datasource in globals (`ball_tree, source, config, seg, zarray, channels,
metadata, _loaded_source, datasource`); the loadedness guards compare
`_loaded_source` against `loaded_scope(name)`, which is the bare name unless
shared roots are configured. Many test files register a datasource named
`"proj"`, so a test that loads a project and does not reset these leaves the
next file silently served the previous test's table — own all of them via
`monkeypatch` in any fixture that loads real data (see the ROI plugin's
`isolate_data_model`). Separately, a synthetic test image must be
`(2, 256, 256)`: a single-channel write comes back 2D and `data_model` indexes
`shape[2]`, and the pyramid walk needs every dimension >= 200.

**`load_datasource()` spawns a daemon thread**
(`_warm_datasource_caches`, `data_model.py` ~line 429) that outlives the test
that started it. Everything it calls goes through `_ensure_loaded()`, which
can reassign `data_model.config` wholesale and bump `load_generation` — against
whatever data root the *next* test set up. The symptom is a KeyError
in an unrelated file, and which file depends on wall-clock timing, so it used
to move whenever anything was added to the suite (this is what previously made
`test_segmentation_mapping.py::test_a_user_supplied_label_pyramid_is_served_without_conversion`
pass alone but fail in a full run). The repo-root `conftest.py` disables the
thread suite-wide with an autouse fixture; nothing asserts warming happens, and
disabling it changes no test result, only speed.

```bash
# Syntax gate for the unbundled viewer
node --check plexora/client/src/js/views/imageViewer.js

# Frontend build -- `npm run build`, NOT `npx webpack`. The config's own mode is
# 'development', so a bare `npx webpack` emits an 8 MB bundle under a different
# set of chunk names than the one that is committed; `build` passes
# `--mode production` and the diff is then only what you changed.
cd plexora/client && npm run build

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
  `"filled"` (the default since `sp.DEFAULT_MODE` changed: labels stored whole,
  boundaries derived client-side) or `"outlines"` (boundaries baked into the
  file; nothing in the UI selects it any more). Both are handled in
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
  pre-existing and **still unexplained**. Two real defects in the 16-bit range
  handling have since been found; neither accounts for a 45% drop, so a third
  cause remains open:
  - **Fixed (2026-08-24).** `getRawImageRange` took the HD slider's ceiling from
    `image_max`, which `get_image_channel_stats` computes from the mean-pooled
    `zarray` — the same source `get_channel_quantization_window` documents as
    invalid for a max-based ceiling. On a channel whose pooled max was 1313 the
    slider could not be moved above 1313 and everything brighter clamped to full
    intensity. It now reads `qmax` (full-resolution, already in the same packet).
    `tests/test_hd_slider_domain.py` pins it. Note `image_max` is deliberately
    left pooled — it is the axis `image_histogram` is plotted against.
  - **Open.** `frag.glsl`'s `u16_rg_range` reconstructs the sample as
    `pixel.r * 255 + pixel.g`; the high byte's weight is 256, not 255. Values are
    under-read by up to `r/65535` (~0.39% of full scale) and raw 255/256 collide.
    Separately the shader normalizes by 65535 while `toImageConnectorRange`
    normalizes by 65536. Both are one-token fixes but shift HD rendering
    slightly brighter and move where existing saved thresholds bite, so they
    need a deliberate pixel-hash comparison, not a drive-by edit.
- `tests/baseline_orion2.py` depends on datasource files that may not exist on
  the current machine; those tests skip rather than fail.
- A floating popup on the viewer page must be portaled with
  `PopoverPortal.attach` (`client/src/js/views/popoverPortal.js`), never with
  `document.body.appendChild`. Two reasons, and only the first is always in
  play: a dimmed row has `opacity < 1` and traps a popup in its own stacking
  context; and the Fullscreen API paints an opaque `::backdrop` over everything
  that is not the fullscreen element or a descendant of it, so anything left on
  `<body>` opens where nobody can see or click it whenever something SMALLER
  than the document goes fullscreen. The full-screen button itself fullscreens
  `document.documentElement` (`imageViewer.js`, `pre-full-page`) precisely so
  the navbar -- a sibling of `#bodyDiv`, not a child -- stays on screen, and
  `PopoverPortal.root()` returns `<body>` whenever the fullscreen element
  contains it. Whatever is attached must be handed back with
  `PopoverPortal.detach` on teardown: a portal still holding a destroyed element
  re-attaches the orphan on the next fullscreen toggle. The three viewer popups
  (`searchableSelect.js`, `colorSwatchPicker.js`,
  `cell_explorer/static/cellExplorerRoiBridge.js`) all go through it, and
  `tests/test_popover_portal.py` keeps them there -- and so does
  `views/segmentationWait.js`, which is not a floating popup at all but a
  full-screen overlay, because the backdrop covers siblings of the fullscreen
  element whatever their size. `views/segmentationProgress.js` still appends to
  `<body>` and is right to -- it is loaded only by `project_columns.html`, which
  has no viewer and no way to go fullscreen. A native `<dialog>` opened with
  `showModal()` is the OTHER exemption, and the better shape for anything that
  is a modal rather than a popover: the top layer sits above the fullscreen
  element, so `requirementsModal.js` and `views/channelNamesUpload.js` are
  correct on `<body>`.
- A new `"Browse…"` filter needs an entry in BOTH tables in
  `server/utils/native_dialog.py` -- `_TK_FILTERS` (which `FILTER_NAMES`, and
  therefore `/browse_path`'s allowlist, is derived from) and
  `_APPLESCRIPT_EXTENSIONS`. A filter missing from the second is a KeyError-free
  `None`, which is merely unfiltered; one missing from the first is a 400 and a
  button that looks ordinary and does nothing. Prefer `None` on the AppleScript
  side for any set containing an extension macOS has no UTI for (`.tsv`,
  `.h5ad`): one unregistered extension greys out every file in the dialog,
  including the ones that would have matched.
- Scrollbar chrome is defined **once**, in `viewer.css`, as one selector list
  covering `.viewer-sidebar *`, the two portaled popups and
  `.channel-names-modal *`. Anything new that scrolls goes in that list rather
  than shipping its own `::-webkit-scrollbar` rules -- left to the platform they
  are a white trough on a near-black panel, and a second definition is a second
  thing to keep in step. Note both spellings are needed: `scrollbar-*` for
  Firefox, `::-webkit-scrollbar` for Chromium.
- Anything that stores a channel by NAME has to be listed in "A rename lands in
  place" above and moved by `adoptChannelNames`. The failure is quiet: the page
  shows the channel twice and asks the server for one it no longer has. The
  non-obvious member of that list is the **saved channel list in the DB**, which
  is server-side and is what the sidebar restores slots from on the next load --
  so it is not fixed by reloading the page.
- `tests/test_icon_names.py`'s `shipped` fixture skips both its tests when
  `plexora/client/node_modules/@fortawesome` is missing -- true on this
  checkout, where `node_modules` is a partially-synced Dropbox copy holding
  only `cross-spawn` and `mockttp`. That means the suite CANNOT catch a dead
  Font Awesome name here: a new `fa-*` class added on this machine is
  unverified until someone runs `npm install` in `plexora/client`.

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
