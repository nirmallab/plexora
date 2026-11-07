# P6D: the desktop app adopts the browser node (plan Phase 6, step 1)

Date: 2026-11-07. Records: `P6D-M-2026-11-07.json` (the tour, ABBA), `P6D-parity-M-2026-11-07.json`
(tile parity). Branch `web/desktop`. Then, on branch `web/desktop2`: `P6D-open-M-2026-11-07.json`
(the open fixed, eight blocks on a release shell), `P6D-zarr-M-2026-11-07.json` and
`P6D-parity-zarr-2026-11-07.json` (an OME-Zarr project in the window),
`P6D-web-node-parity-2026-11-07.json` (the website's node against its Python, OME-Zarr folders
included), `P6D-parity-M-final-2026-11-07.json` (LSP11385's tiles with the final code).

Status: **step 1 built and switched off, now with OME-Zarr images; the open is no longer slower
with it on.** Tile parity holds in the real window (LSP11385 108 of 108 with the final code,
the OME-Zarr project 48 of 48); the timings were taken while other agents loaded the machine
(CPU 10–71 % before each block), so they are provisional, not the desktop's final rows.

## The open, fixed (web/desktop2, 7f673247)

The first record's open was 1.10–1.16× slower with the setting on. Where it went, read in the
app's window (`PlexoraNode.project(name).timing` and each pool's `opening`, warm passes): the
grant 40–75 ms after `/resource_routing`, then the image's pool 160–270 ms -- 49 reads one after
another per worker (the header, LSP11385's 40 main IFDs, which sit beside each plane's
gigabytes, and the reduced levels' IFDs), ~2.5 ms each through the scheme, in six workers, plus
the workers' start (their 367 KB script) -- all after the first tile had asked. Now:

- **one open per pool**: the first worker records the slices it read; the others open from them
  (`node/tiffReader.js` `SeededSource`) and read nothing (`tests/js/tiff_seed_probe.mjs`: 49
  reads → 0 on LSP11385, 1120 tiles byte-identical);
- **the IFDs at once**: Python's spec carries the main IFDs' offsets (`node_spec.main_ifds`, a
  walk of the chain in 0.9 ms, kept per file until it changes; `ifds` on the desktop's image and
  mask), which the page parses in parallel and takes only when they ARE the chain (first = the
  header's, each next = the following offset, the last's next 0); the reduced levels' IFDs are
  parsed together too. Main IFDs 29–31 ms (were 99–173);
- **opened while the page boots**: `main.js` attaches the routing as soon as `/resource_routing`
  answers, `attachLocal` starts the grant and the open at once, and a project's first check
  starts its workers before the grant.

From the routing to the files open: 126–150 ms (was 234–276); the image pool's first open
46–56 ms (was 112–195).

| Step (time to full, ms, mean of 4 blocks) | C1 off | C1 on | on/off | C2 off | C2 on | on/off |
|---|---|---|---|---|---|---|
| open | 2370 | 2365 | **1.00** | 822 | 776 | **0.94** |
| zoom to 1:4 at a | 1795 | 949 | 0.53 | 1721 | 922 | 0.54 |
| zoom to 1:1 at a | 551 | 279 | 0.51 | 468 | 273 | 0.58 |
| pan to b at 1:1 | 1133 | 557 | 0.49 | 1205 | 567 | 0.47 |
| home | 192 | 27 | 0.14 | 174 | 35 | 0.20 |
| revisit a at 1:1 | 28 | 26 | 0.91 | 23 | 25 | 1.09 |
| jump to c at 1:1 | 857 | 497 | 0.58 | 858 | 473 | 0.55 |
| animated zoom to 1:4 at b | 1403 | 1192 | 0.85 | 1201 | 1192 | 0.99 |
| animated pan to c | 1197 | 1193 | 1.00 | 1204 | 1197 | 0.99 |
| **geometric mean, the four tile steps** | | | **0.53** | | | **0.53** |

Release shell (`cargo build --release`, gnullvm), eight blocks off, on, on, off, on, off, off,
on, each a fresh app; renderer peak 1.33–1.37 GB on against 1.84–1.91 GB off. The open's spread
is wide (C1 on 2152–2586 ms, off 2214–2572 ms): read the two as equal, not as a gain.
`tour.mjs` now pauses only the three write routes it answers itself; it used to pause every
server request over CDP, which added a round trip to each tile the server answered and none to
those the page read (the first record's tile steps were measured that way; off's tile steps
moved by about 4 %).

## What is adopted

The desktop app's page reads an opened project's **image tiles** (a local pyramidal OME-TIFF or
TIFF, 8- or 16-bit, lossless compression) and **mask tiles** (a TIFF label pyramid, at the levels
the file holds) itself, in workers, by byte range, with the same reader the website uses for a
picked file (`node/tiffReader.js` in `workers/tiffWorker.js`, pools in `services/browserNode.js`).
Python answers everything the page declines and everything else it always answered. Pyodide never
loads; `services/backend.js` keeps target `"server"`.

How a tile gets there:

1. `/resource_routing` (desktop app, setting on) adds `local` beside `routes`: Python's own
   description of what it reads (`browser/node_spec.describe(name, local=True)`: path, `format`,
   every level's shape, dtype, the tile grid, the mask's levels), or why the page must not.
2. `services/resourceRouting.js` chooses the third routing, **`local-browser`**, only when the
   answer says `setting: "auto"`, the page is in the app's window with a shell that grants files
   (`PlexoraDesktop.grantLocalFiles`, `.localFiles`), and the image or mask is served as OME-TIFF
   or TIFF. It is kept apart from the node `routes` (`resolved.local`), so node routing, the
   globe and the resource-status report read exactly what they did.
3. `attachLocal` loads `browserNode.js` into the page and `use`s the desktop's source: the spec
   from `/resource_routing`, the files from `grant_local_files`. The page checks every level's
   shape, the channel count, sample size and compression against Python's before its first tile.
4. `views/tileFetch.js` asks the page (`local`) before the network. A channel tile comes back as
   the decoded pixels the HD PNG decodes to (16-bit big-endian; 8-bit widened as Python's
   `astype("uint16")`), with no PNG made or decoded and nothing on the wire; a mask tile as the
   RGBA PNG OpenSeadragon's raster path takes. `viewerManager.js` attaches a planar HD channel
   source to the scheduler when the page reads it. Never batched past the page.

**OME-Zarr images too** (web/desktop2, ba22659f, cea36941; see "OME-Zarr" below): the page's own
reader (`node/zarrReader.js`) serves a picked OME-Zarr folder on the website and a project's
store on the desktop app, at the store's own levels; the levels Python derived into the project
stay Python's.

Not adopted yet (steps of the plan's order still open): an OME-Zarr **mask** (`labels/...`;
TIFF label pyramids only), 8-bit "fast" tiles (HD only — the desktop starts in HD), volumes,
brightfield/RGB slides, a TIFF pyramid whose coarse levels are a project-store extension, masks
Python draws at a finer scale; step 2 tables via the column store; step 3 contrast windows / GMM
in JS (measured below, not built); steps 4–7.

## OME-Zarr (web/desktop2)

`node/zarrReader.js` (`createZarrReader`: `open`, `tile`, the TIFF reader's interface and output)
reads what Python reads: `node_spec.describe` describes an OME-Zarr image as `zarr: {axes, t, z,
arrays: [{path, shape, format}]}` — the store's levels that halve from level 0 (`ome_zarr.
open_image`'s `base_levels`), each array's path, full shape and zarr format — beside `levels`
(every level Python serves). The page reads each array's `.zarray` / `zarr.json` itself and
refuses what it would read differently (shape, sample type, Fortran order, v2 filters, a
transpose, a codec it has no decoder for, nested shards).

- Zarr v2 and v3 (NGFF 0.4 and 0.5); chunk keys "." or "/" (v2), `c/…` or v2-style (v3); a chunk
  never written is the fill value.
- Codecs: blosc (every inner compressor, every shuffle) and v2 lz4 from **numcodecs.js 0.3.2**
  (MIT; c-blosc 1.18.1 in WebAssembly, BSD-3, with its bundled LZ4 / Zstandard / zlib / Snappy /
  BloscLZ under permissive licences), vendored in `client/external/numcodecs-0.3.2` with
  `LICENSE`, `LICENSES/` and `SOURCE`, loaded by native `import()` on the first chunk that needs
  it (615 KB, never in the worker bundle); zstd from **zstddec** (geotiff.js's, already in the
  bundle); zlib and gzip from `DecompressionStream`; v3 `bytes` in either endianness, `crc32c`,
  `sharding_indexed` (the shard index by a suffix range, each inner chunk by its range).
- Website: the picked folder is the store; a worker asks the page for each file by path
  (`{ask, path}`), answered only inside the store its pool opened (`TiffPool.fileInStore`).
- Desktop: the shell grants the store's **folder** (below); the worker reads `<base>/<id>/<key>`.

| | where | tiles | identical |
|---|---|---|---|
| eighteen stores (v2 blosc lz4 / zstd+bitshuffle 8-bit / zlib / blosclz, zstd with "/" keys, zlib, gzip, lz4, raw with unwritten chunks, yx, tczyx; v3 zstd, zstd+checksum 8-bit, blosc, gzip+crc32c, sharded, sharded with unwritten chunks, raw with "." keys) | Node, against the tile route's `?q=hd` PNG for the same URL (`tests/test_browser_zarr_node.py`) | every tile, 128 grid | all |
| `zarr_v2_blosc`, `zarr_v3_sharded` picked as folders | the website, against the worker's Python (`tests/browser/node_parity.mjs`) | 42, 16 | 42, 16 |
| `lsp11385_zarr` (LSP11385's levels 2.. for channels 0–5, NGFF 0.4, blosc lz4, 1024 chunks) | the desktop window (`desktop_parity.mjs`) | 48 | 48 |

The tour on `lsp11385_zarr` (eight blocks, release shell, `P6D-zarr-M-2026-11-07.json`;
waypoints P6's ÷ 4; provisional, CPU 10–32 % before the blocks):

| Step (time to full, ms, mean of 4) | C1 off | C1 on | on/off | C2 off | C2 on | on/off |
|---|---|---|---|---|---|---|
| open | 1555 | 1388 | 0.89 | 748 | 674 | 0.90 |
| zoom to 1:4 at a | 1069 | 374 | 0.35 | 928 | 328 | 0.35 |
| zoom to 1:1 at a | 399 | 159 | 0.40 | 321 | 141 | 0.44 |
| pan to b at 1:1 | 492 | 218 | 0.44 | 448 | 189 | 0.42 |
| jump to c at 1:1 | 464 | 203 | 0.44 | 345 | 196 | 0.57 |
| **geometric mean, the four tile steps** | | | **0.41** | | | **0.44** |

Renderer peak 1.12–1.37 GB on against 1.76–2.36 GB off.

## Step 3 measured, not built: the contrast fit in JS

(The persisted fit has since landed; see "What is left".)

What `get_channel_gmm` costs on the desktop for LSP11385 (`desktop2_gmm_py.py` in the session's
scratch; the parts timed apart): the quantization window ~0 s (already known to the project),
the overview ~0 s (read at load), **the GaussianMixture fit 0.53–0.64 s per channel** cold
(0.19–0.56 s warm, five runs each). The POC's JS port (`tools/poc_browser_node/page/src/stats.js`
`gmmOf`: sklearn's EM, seeded k-means start) on the very overview planes Python fitted, in Node:
**0.12–0.18 s, 0.36–0.38 s, 0.38–0.40 s** for channels 0–2 against Python's 0.19–0.34, 0.48–0.56,
0.49–0.56 s: 0.6–0.75× Python's, single-threaded both, and the same `vmin`/`vmax` on all three
(894/2529, 625/2015, 388/1009). sklearn's fit is unseeded, so parity is a tolerance, not
equality. The gain is a few tenths of a second per channel, once per channel per session (the
packet is cached in memory); porting the whole packet (`log_window` through
`display_curve.solve`, the histogram curves) and persisting it (`gmm_cache_get_or_set`) is the
remaining work — persisting it alone would remove the cost from every reopen on both arms.

## The setting

Settings › **Advanced** › **Browser rendering: Auto | Off** (desktop app only; the section is not
rendered in a browser tab or on the website). Stored as `"browser_rendering"` in the settings
file; **default `off`** until the website's GA (`plexora/browser_rendering.py DEFAULT`). It takes
effect for projects opened after the change. `/desktop/info` reports `browser_rendering` and,
when on, `paths` (`image_tiles`/`label_tiles`: `browser`; `tables`/`contrast`: `server`).
`/settings/advanced` GET/POST (409 off the desktop app). Off, `/resource_routing` answers
`{"routes": {}}` exactly as before, the shell grants nothing, and its scheme answers 404.

## The scheme's security: what it can and cannot read

`plexora-file` (`desktop/src-tauri/src/localfiles.rs`, registered in `lib.rs`; the page reaches it
at `http://plexora-file.localhost/<id>` on WebView2, `plexora-file://localhost/<id>` elsewhere —
the base is in the init script `windows.rs` builds).

- **Only granted files.** A grant is made only by `grant_local_files(datasource)` (commands.rs):
  behind `check_caller` (this launch's server page only) and its own capability permission. The
  page names a **project**, never a path. The shell asks this launch's server over loopback, with
  the launch token in `X-Plexora-Token` (never in a URL), which files that project reads — the
  same `/resource_routing` answer — and grants those. The server names files only with the
  setting on, and only regular files on this computer's own disks (`node_spec.is_local_file`:
  no URL, no `node://`, no `\\server\share`, no folder).
- **A grant is one canonical regular file** under a 128-bit random id; never a network path
  (`\\?\UNC\` refused after canonicalisation); at most 256 per run, in memory only. The URL
  carries the id alone: no path to traverse, nothing to list, no encoding to abuse.
- **Or one OME-Zarr store** (web/desktop2): only where the server's part says `format:
  "ome-zarr"` (`node_spec.is_local_store`: a folder with zarr metadata on a local disk), granted
  by `grant_store` (canonical, local, a folder holding `.zattrs`/`.zgroup`/`zarr.json`). Its files
  are read as `<base>/<id>/<key>`: at most 32 plain segments (`[A-Za-z0-9._-]`, never `.`/`..`),
  joined onto the canonical folder and canonicalised again (links followed), and it must land on
  a regular file inside it; anything else, a missing chunk included, is 404. A file's grant takes
  no key; a store's id alone reads nothing. Nothing can be listed.
- **Requests:** GET and HEAD (and the CORS preflight) only; only from the app's `main` windows;
  only with an `Origin` equal to this launch's server origin (a request without one, or from
  another loopback port, is 403); a GET reads one byte range of at most 64 MiB (a whole file only
  when it is that small). Responses are `no-store`, `nosniff`, with
  `Access-Control-Allow-Origin` set to the server origin alone.
- **Cannot:** read any file the server did not name for a project the page asked about; read
  anything with the setting off; write, list or stat anything; be reached from another page or
  origin. Tests: `cargo test` (`localfiles::tests`: ranges, caller, grant/read/refuse,
  folders/shares, the grant limit, a store read by key inside it and nowhere else, only a local
  folder with zarr metadata as a store; `commands::tests::only_files_the_server_serves_with_the_setting_on_are_named`):
  27 passed.

## Results (Measured)

### Tile parity (real window, setting on)

`tools/bench_remote/desktop_parity.mjs` in the desktop app's own window over CDP: for channels
0, 1, 2, 10, 39 at every level (first, middle and last tile) and the mask at every level, the
page's tile (`PlexoraNode.decoded`) against the server's answer for the same URL (`fetch`,
decoded as the viewer decodes it), sample for sample and by FNV-1a hash.

| | tiles | identical | different |
|---|---|---|---|
| channel (uint16, 7 levels) | 90 | 90 | 0 |
| mask (uint32 labels, 7 levels) | 18 | 18 | 0 |

### The tour, off against on (provisional: machine under load, debug shell)

`tools/bench_remote/desktop_campaign.mjs` drives `tour.mjs --cdp` in the app's window: LSP11385
(52923 × 45381, 40 channels, deflate SubIFD pyramid; its label pyramid; 202,049 cells), channels
0–2, P6's waypoints, HD; four blocks off, on, on, off, each a fresh app (new server, WebView2
profile of its own, C1 with the HTTP cache emptied, C2 warm). The OS file cache is not dropped.
Time to full per step, ms, mean of the two blocks of each arm:

| Step | C1 off | C1 on | on/off | C2 off | C2 on | on/off |
|---|---|---|---|---|---|---|
| open | 2496 | 2752 | 1.10 | 1024 | 1185 | 1.16 |
| zoom to 1:4 at a | 1861 | 1082 | 0.58 | 1736 | 1096 | 0.63 |
| zoom to 1:1 at a | 562 | 283 | 0.50 | 506 | 317 | 0.63 |
| pan to b at 1:1 | 1195 | 643 | 0.54 | 1227 | 628 | 0.51 |
| home | 209 | 26 | 0.13 | 191 | 24 | 0.12 |
| revisit a at 1:1 | 26 | 32 | 1.24 | 23 | 25 | 1.13 |
| jump to c at 1:1 | 849 | 527 | 0.62 | 927 | 553 | 0.60 |
| animated zoom to 1:4 at b | 1338 | 1192 | 0.89 | 1317 | 1191 | 0.90 |
| animated pan to c | 1204 | 1192 | 0.99 | 1194 | 1193 | 1.00 |
| **geometric mean, the four tile steps** | | | **0.56** | | | **0.59** |
| geometric mean, every step after open | | | 0.58 | | | 0.59 |

| | off | on |
|---|---|---|
| WebView2 renderer peak working set (GB), two blocks | 1.91, 1.90 | 1.35, 1.39 |
| tile requests to the server over both passes (block 0 / 1) | 1105 | 0 |
| every step reached full | yes | yes |

Reading it:

- **Every step that needs new tiles takes 0.50–0.63 of the Python route's time**, and the
  renderer peaks 0.5 GB lower (no PNG bodies or WebP decodes in it). P6 measured 0.30–0.41 for
  the same steps in Chrome with the POC's viewer; the gap here is not yet attributed: the machine
  was loaded, the shell was a debug build, every range read crosses WebView2's
  WebResourceRequested on the window's thread, and the scheduler keeps its network cap of six in
  flight for the page's reads.
- **Open is 10–16 % slower with it on.** The page opens the 40-channel file in nine workers (six
  for the image, three for the mask) through the scheme before its first tile, after a grant
  round trip to the server. P6's open (0.30) counted a static page's boot, not the product page's.
- Steps that need no new tiles are the same (revisit, the animated springs).

## What is left

- Rerun the campaign on an idle machine (the release shell is built: `cargo build --release
  --target x86_64-pc-windows-gnullvm` with `_cargo_env`'s variables) before the numbers enter
  the measured map; until then these are the desktop's provisional rows.
- Open: done (above). The grant is still a second `/resource_routing` round trip after the
  page's own (40–120 ms). Steps: a separate in-flight cap for the page's reads (the scheduler
  keeps six in flight, one per image worker, so a worker idles while its range read is in
  flight); measure the scheme's raw range throughput from a worker against the decode time.
- OME-Zarr: done for images (above). An OME-Zarr mask (`labels/<name>`, the same reader with
  `raw` tiles and `_labels`' checks), a store reached over HTTP/S3 (the reader's `{base}` store
  would read it, but `node_spec` serves only local or picked stores), 8-bit tiles in the page,
  volumes.
- Step 2 (tables via the column store, the Arrow sidecar): measured, **not adopted** (below).
- Step 3 (contrast windows / GMM in JS): measured (above) at 0.6–0.75× Python's fit, not built.
  Persisting the fit is the larger, simpler win, and it is done (web/contrast,
  `database_model.ChannelFit`: a reopen's fits in 27.5 ms for six LSP11385 channels instead of
  4.1 s). The JS fit would now save a few tenths of a second per channel on a project's first
  open only.

## Step 2 measured, not adopted: tables via the column store (2026-11-07)

The desktop's open of Orion CRC02's table (1,260,592 × 29, 634 MB CSV) with a small image, through
the routes the viewer calls (`/init_database`, then `/get_all_cells` for ids and coordinates),
CPython Plexora on this machine, quiet; each a new process (`px_tableopen.py` in the session's
scratch):

| | First open (project made) | Reopen (3 runs) |
|---|---|---|
| `/init_database` (s) | 0.59 | 0.49–0.63 |
| `/get_all_cells` (s) | 1.67 | 0.79–0.87 |
| Open, both (s) | **2.26** | **1.35–1.44** |

Profiled, a reopen in a new process is mostly not the table: `ome_types`' first import is 0.78 s
of it (the image's OME-XML, `pixel_scale._local_metadata` / `LocalImageProvider.open`), polars'
read 0.22 s, the cells array 0.30 s (0.02 s once built), their gzip at level 3 0.32 s. In a
desktop app already running, the table costs about 0.5 s. The column store draws the same table
in 0.90 s on a first visit and 0.04 s on a reopen (P3), but on the desktop it would have to parse
from the scheme and keep its columns beside the source; the gain is a few tenths of a second, so
the cell reads **either** and step 2 is not built. The plan's "minutes-scale first open" is not
this machine's. Two smaller desktop wins found on the way are **both taken** (web/desktop-wins):

- `/get_all_cells` is sent raw by the desktop app's own server (`data_routes._cells_gzipped`:
  decided by `PLEXORA_DESKTOP`, never by the caller's address, because a terminal or `--remote`
  server reached through an SSH tunnel sees 127.0.0.1 too and keeps gzip): 15.1 MB raw instead
  of 7.7 MB gzipped, over loopback, and the route takes 0.53–0.56 s instead of 0.82–0.87 s
  (medians by pause, below).
- `ome_types` is imported by `data_model.warm_after_ready`, a background thread the desktop
  server starts after its ready line and that waits for the window's first page to finish (the
  server busy, then 0.4 s quiet; waitress' own counts, `cli._waitress_busy`); `prime_hot_code`
  loads only its native part (`pydantic_core`, 25 ms), so the thread dlopens nothing beside a
  request. Measured three placements: inside `prime_hot_code` it held the splash **+0.73 s**
  (paired median of the ready line against the same code without it, 11 of 12 pairs slower);
  a thread started at the announce cost the window's first page **+62 ms** (ready to `load`,
  22 of 30 pairs slower); waiting for the page costs nothing measurable (−18 ms, 8 of 18 pairs
  slower). The price of waiting: a project opened at the very moment the page loads pays the
  import itself, as before.

Reopen of the same project in a new desktop server process per run (`python -B -m plexora
--desktop` from each arm's code, headless Chrome on the ready url, then from the page
`/init_database` and `/get_all_cells/integer` read to an ArrayBuffer), six or twelve pairs per
cell, arms alternated and rotated every round, the machine shared with other work:

| Opened after the page's load event | before (web/phase0) | gzip off | gzip off + warm-up |
|---|---|---|---|
| at once | 1.45 s (1.31–1.58) | 1.17 s (1.00–1.33) | 1.15 s (1.10–1.22) |
| 1 s later | 1.44 s (1.37–1.72) | 1.10 s (1.06–1.78) | 0.63 s (0.55–0.68) |
| 2 s later | 1.40 s (1.27–1.70) | 1.09 s (0.96–1.41) | 0.55 s (0.50–0.60) |

Medians (ranges). `/init_database` falls from 0.55–0.62 s to 0.035 s once the warm-up has run;
the ready line (5.2–5.7 s medians, ranges of seconds from the other work) and the page's load
(0.6–0.8 s after it) do not move with either change.
- Steps 4–7 as the plan orders them.
- macOS/Linux: the scheme is served as `plexora-file://localhost`; whether WKWebView/WebKitGTK
  route worker fetches of a custom scheme with CORS is untested here.

## How to rerun

```
# build the shell (no MSVC here: _cargo_env's gnullvm variables), libunwind.dll beside the exe
node tools/bench_remote/desktop_campaign.mjs --exe <plexora-desktop.exe> --home C:\plexora-bench-desk \
     --dataset lsp11385 --order off,on,on,off --out <dir>\campaign.json \
     --params '{"passes": ["C1","C2"], "channels": [0,1,2], "waypoints": {...}}'
node tools/bench_remote/desktop_parity.mjs --dataset lsp11385 --out parity.json   # app open, setting on
# web/desktop2's runs: release shell (cargo build --release; libunwind.dll and WebView2Loader.dll
# beside it), --home C:\plexora-bench-desk2 --cwd <worktree>, eight blocks
# off,on,on,off,on,off,off,on; the OME-Zarr project lsp11385_zarr was written from LSP11385's
# levels 2.. (channels 0-5, zarr v2, blosc lz4, 1024 chunks, "/" keys) and registered with
# datasource.register_image_datasource; its tour's waypoints are P6's divided by 4
# the website's node against its Python (bundle and wheel in the bench home, the static build):
PLEXORA_BENCH_HOME=<home> node tests/browser/node_parity.mjs --dir <home>/browser_node/node_fixtures
# the two desktop wins (web/desktop-wins): one exported tree per arm (`git archive <rev> plexora`,
# compileall'd), a data root holding Orion CRC02's table on a 2x512x512 OME-TIFF, then per run a
# fresh Chrome profile, `python -B -u -X utf8 -m plexora --desktop --port 0` from the arm's tree
# (PLEXORA_DATA_PATH, WIN_PD_OVERRIDE_LOCAL_APPDATA, PLEXORA_TELEMETRY=off), the ready url to
# `load`, a pause, then the two fetches; scripts dwins_make_fixture.py and dwins_bench.mjs
# (session scratch), records in C:\plexora-bench-dwins\runs1.jsonl and runs2.jsonl
```

The bench home holds the data root, a settings file of its own (`WIN_PD_OVERRIDE_LOCAL_APPDATA`)
and the WebView2 profile (`WEBVIEW2_USER_DATA_FOLDER`); the user's settings file is never written.
A debug shell re-registers `HKCU\Software\Classes\plexora` to itself on every start: export the
key first and import it back afterwards. A release shell (no `debug_assertions`) registers nothing.
