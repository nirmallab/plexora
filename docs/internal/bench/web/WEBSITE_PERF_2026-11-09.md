# Website performance, before and after (2026-11-09)

Branch `feature/website-perf`, measured against main at `fcd70d54` (website 0.0.41). Plan:
the website investigation of 2026-11-08 (root causes RC1–RC22, phases 0–5). Harness:
`tools/bench_web/` (see its README). Raw records: `data_2026-11-09/`.

Machine: M2 Pro, 10 cores, 32 GB, Chrome 157 headless via Playwright. Both arms are the static
site served from loopback by `tools/browser_build/serve.py`. **Before**: main's site with the
published `py/0.0.41/` bundle (CDN, HTTP-cached after the first run). **After**: this branch's
site with a bundle built from this branch (`tools/browser_build/build.py`), served locally.

## Startup (3 runs each, ms from navigation)

| | Home visible | Python ready | Bytes at boot |
|---|---|---|---|
| Before, cold (fresh profile) | 322–340 | 5,571–5,813 | 55.7 MB |
| After, cold | 429–440 | 3,215–3,231 | 41.7 MB |
| Before, reload | 267–289 | 4,751–4,790 | |
| After, reload | 270–283 | 3,032–3,051 | |
| Before, warm (new browser, same profile) | 388–401 | 4,746–4,777 | |
| After, warm | 386–429 | 3,055–3,117 | |

- Python's reload start: base packages 1,961 → 896 ms, wheels → ready 1,637 → 990 ms. The cause is
  scipy and scikit-learn as lazy stages (`scipy`, `fit`), plus lazy imports in `data_model` and
  gating.
- **The cold home page's +100 ms is the bundle's source, not this branch.** Main's own JS, served
  with the same local bundle, shows 428–441 ms (`startup_main_js_local_bundle.jsonl`). A local
  bundle arrives at once and competes with the first paint for the CPU; a CDN one does not.

## CODEX tonsil (59-ch uint8, 3139×5865, 40 k cells)

| Measure | Local server | Website before | Website after |
|---|---|---|---|
| Python ready (per visit) | 3,282 once | 6,818 | 3,407 |
| Import (inspect + sample) | 378 | 1,566 | 1,601 |
| Viewer drawn | 688 | 1,464 | 1,722 |
| Table + mask ready | 771 | 2,905 | 1,914 |
| Tiles while touring, p50 | 27–56 | 61–79 | 13–39 |
| **Reload → viewer drawn** | 279 | **6,785** | **445** |
| Reload tiles p50 / p95 | 6–9 | 87 / 117 | 23 / 38 |
| Open QC / ROI / Cell Explorer / Figure Builder | 458 / 64 / 230 / 114 | 1,130 / 240 / 357 / 111 | 1,061 / 89 / 352 / 91 |
| Figures page → back | 95 → 4 | 107 → 110 | 72 → 109 |
| Python requests while opening: wall / Python time | | 13.6 s / 1.5 s (31) | 8.7 s / 0.5 s (29) |
| Wasm heap / renderer memory after | | 695 / 609 MB | 631 / 568 MB |

- Every channel tile is now read by the page (`tileFetch.local`, 9 of 9 on the reload). None
  crosses the service worker; they don't appear in Resource Timing, so the harness times them by
  wrapping `tileFetch.local`.
- The reload draws the viewer from kept answers at 445 ms. Python is ready at about 2.9 s; the
  page's remaining requests wait for it then.
- The mask pyramid was not rebuilt on reload (`builds: []`; the OPFS copy was mounted).
- Gating was declined on both arms: this CSV has no Image ID column.

## LSP11385 (40-ch uint16 OME-TIFF, 66.7 GB, 7 levels, label pyramid, 202,049 cells)

| Measure | Website before | Website after |
|---|---|---|
| Python ready | 5,771 | 3,193 |
| Import (inspect + sample) | 3,063 | 2,934 |
| Viewer first item / drawn | 2,163 / 2,182 | 3,331 / 3,352 |
| **Table + mask ready** | **9,516** | **3,575** |
| Tour tiles p50 (immediate steps) | 100–191 | 22–64 |
| Tour tiles p95 (all steps) | 122–385 | 26–286 |
| **Open ROI / Cell Explorer** | **15,981 / 11,692** | **2,194 / 4,123** |
| Last 60 Python requests: wall / Python time | 170.7 s / 13.5 s | 29.5 s / 7.0 s |
| Background steps over 0.5 s (of those recorded) | 8 of 10 (7–9.5 s each) | 1 of 74 (1.5 s) |
| "Connect a node" toast | shown | none |
| Wasm heap / renderer memory after | 1,298 / 1,036 MB | 1,296 / 928 MB |

- The longest remaining waits are `get_image_channel_stats` at 8.4 s of wall time and 5 ms of
  Python. They wait for the page's full-resolution scan of the channel; they no longer hold the
  queue, so everything else is answered meanwhile.
- **Viewer first item +1.2 s.** `init_database` now loads the `scipy` stage (1.1 s) that used to
  load during boot. The idle preload (`stage-scipy`) normally has it in long before a project
  opens, but the harness imports the moment Python is up. Net: Python is ready 2.6 s sooner.
- **The before run's 2,217 ms fit-to-screen stall was the harness.** Its fit step returned
  `viewport.goHome()`, so Chrome serialized OpenSeadragon's whole object graph back to the
  driver: 587 MB on this image. That is also what ended the local server's arm at the same step
  (`ERR_STRING_TOO_LONG`), not a console message. Fixed in `benchlib.mjs`; the after run's fit
  step has a 17 ms longest frame.

## Found and fixed during this verification

1. **The worker froze.** The first plan for channel stats let a request wait for the page's scan
   with other requests answered in the gap. On a single thread, a request answered inside a
   suspended one can need a lock it holds, or suspend in its turn. The worker stopped answering
   even `state`; every tool open failed with `Failed to fetch` once the service worker gave up.
   Now the route answers "again in 200 ms"
   (`providers.browser.WindowPending` → `X-Plexora-Retry-Ms` → `bridge.handle` `retry`), and the
   worker puts the request back in its queue. After 150 retries (about 30 s) the route waits for
   the scan as it always did. Tests: `tests/test_page_window_warm.py`.
2. **A reload with a tool open skipped the kept viewer.** `toolLoader` keeps `?tool=` in the
   address, and `PlexoraKept.early` accepted only `/<project>`. Now an address whose only query is
   `?tool=` gets the kept plain viewer at once. The tool opens in it once Python is up, and the
   reconcile compares against Python's plain page. Any other query waits for Python, as before.
   Tests: `tests/test_kept_answers.py`.
3. **Contrast fits nobody asked for held the worker for 1–7 s each** (59 of them on tonsil). On
   the website the warm-up still computes every channel's stats, but fits only the channels last
   switched on. A channel turned on later draws from its stats at once and is fitted when it asks.

## Against the plan's targets

| Target | Status |
|---|---|
| Home visible ≤ 0.2 s warm / ≤ 0.6 s cold | warm 0.39–0.43 s (unchanged, not met); cold 0.43 s (met) |
| Python ready after reload ≤ 2.5 s | 3.0 s (was 4.75 s); not met |
| Viewer after a reload ≤ 1 s, independent of Python | 0.45 s (met) |
| Tile p50 / p95 ≤ 30 / 80 ms | tonsil 13–39 / ≤ 59 (met); LSP 22–64 / 26–286 (p50 mostly met, p95 not on animated zooms) |
| Longest main-thread stall ≤ 100 ms | 17 ms during the tour, both images (met) |
| Wall ≤ 1.5× Python time; no request behind speculative work | LSP 4.2× (was 12.7×); a 1.5 s `display-contrast` step and the 1.1 s scipy stage still delay the first requests |
| Plugin first open ≤ 1 s | tonsil met except QC (1.06 s); LSP ROI 2.2 s, Cell Explorer 4.1 s, mostly Python work (`/image/requirements` 1.4–1.6 s, Cell Explorer `variables` 1.9 s) |
| No "Connect a node" toast | met |

(0.0.42 shipped these, 2026-11-09 11:42 UTC.)

## Round two (after 0.0.42)

Branch `feature/website-perf-2`. Records: `data_2026-11-09/*_round2.json`.

**The cell index waits for the first screen.** The cell index (a cKDTree, so
scipy) was built or read back in `init_database` and inside the table loaded
behind the viewer. On the website it is now a background task once the
project is loaded (`data_model._index_later`). A pick made before that task
runs builds the index itself.

| | 0.0.42 | Round two |
|---|---|---|
| Tonsil: viewer drawn / table + mask ready | 1,722 / 1,914 | 1,449 / 1,650 |
| LSP11385: viewer drawn | 3,352 | 2,052 |
| LSP11385: `init_database` Python time | 1,131 | 1 |
| LSP11385: table + mask ready | 3,575 | 5,276 |
| LSP11385, in isolation: saving a tool's set-up form (reloads the table) | 3,824 first, 1,231 again | 1,286 first, 1,203 again |

The LSP "table + mask ready" moved the other way. The harness opens the
project the moment Python is up, before the idle preload of the scipy stage
has run. So the stage now loads inside the first background job that needs it,
which is display contrast's tissue mask (`image_qc.tissue_mask`, 1.5 → 3.0 s),
and the status polls wait behind that job. A visitor who picks files first
gives the preload its pause, and then nothing on the open path pays for scipy.

**Cell Explorer's first `variables`.** It no longer makes text out of every
value to count a numeric column's distinct values: the text is made only for a
column that turns out to be categorical (`variables._count_numbers`, equal
counts by construction, tested against the text path). It also scans the
installed plugins once, not once per plugin (`cell_exclusions`). On LSP11385
the first `variables` went from 2,394 ms to about 300 ms in Pyodide, and from
1,359 to 281 ms in CPython.

**A worker kept in a browser's cache.** The route answers "again later" only
to a worker that says it follows a retry (`bridge.RETRYABLE`). The site's
`.js` reach browsers with a 4-hour `max-age` from the zone (WEBSITE_NEEDS_YOU.md
§2), so an older worker can meet a newer bundle.

**Background work waits for quiet after the boot too.** The worker runs
background work (a stage preload, a cache warm-up) only after 600 ms without
a request, counted from the last request. At the end of the boot there had
been none, so the first background task took the worker at once, and a
reloaded project's first screen, which the page sends a moment after Python
is ready, waited behind it: `/config` 786–810 ms behind the scipy preload.
Now the quiet counts from the boot's end (`pythonWorker.js`).

| Tonsil, ms (`tonsil_web_*.json`) | Round two (2 runs) | Quiet after the boot |
|---|---|---|
| Reload: `/config` | 786–810 | 26 |
| Reload → table and mask ready | 5,152–5,380 | 4,414 |
| Import / viewer drawn / table + mask ready | 1,599–1,652 / 1,449–1,538 / 1,650–1,741 | 1,556 / 1,435 / 1,630 |

**Tried and backed out: polars as a stage of its own** (commit 20fd6bee).
Of the base packages' 0.9 s, polars is 0.69 s, and the home page reads no
table. The app-building modules kept `import polars as pl` at their top (the
desktop imports every C extension before a request thread exists:
`data_model.prime_hot_code`), and on the website a stand-in in `sys.modules`
loaded the stage at the first attribute read, preloaded with scipy in one
`loadPackage` once the first page was answered. Python was ready 0.55 s
sooner (reload 3.17 → 2.54–2.68 s) and a first visit fetched 22.9 MB
instead of 32.8 before it. But nothing a visitor does next works without
polars, and polars loaded after the boot costs ~0.7 s where it costs ~0.5 s
beside the other base packages, so a project opened at once landed later:

| ms (`*_quiet_after_boot.json`, `*_polars_stage.json`) | Quiet after the boot | + polars stage |
|---|---|---|
| Python ready, reload (`startup_*.jsonl`; the quiet fix does not touch the boot) | 3,161–3,181 | 2,541–2,683 |
| LSP11385: import | 2,887 | 4,108 |
| LSP11385: table + mask ready | 5,593 | 6,485 |
| LSP11385: ROI / Cell Explorer | 4,315 / 3,941 | 4,850 / 4,495 |
| Tonsil: reload → table and mask ready | 4,414 | 4,614 |

Worth another look if Pyodide's package loading gets cheaper after the boot,
or if a visit's first seconds stop needing a table.

**Measured, left as it is:**
- Python's boot, 0.6 s of it: `create_app` 247 ms, chunk cache 107, parquet 75,
  buckets 53, cooperative 43 ms (`boot.start` now returns `timings`). The rest
  of the 3.0 s is Pyodide (1.07 s) and the base packages (0.9 s, polars 0.69 s
  of it; deferring it is above).
- Warm home, 0.39 s: the frame reloads the loader's five files, then waits two
  animation frames (the pointer-capture guard), then the home page loads 111
  files (`base.html`, ~95 scripts) and runs them. Reaching 0.2 s needs a lighter
  home page or one script file: the page-model phase.
- `/image/requirements` is 6 ms in Pyodide. The 1.4 s in the tool rows is the
  POST that saves a tool's set-up form and reloads the table, once per project.

## Round three (after 0.0.43): the live site's freeze, contrast, a plain mask

Reported on the live site (0.0.43) on a large local image: a reload that never finished loading,
channels that took "forever" to draw, tile errors when zooming, `get_image_channel_stats ... Failed
to fetch` and a 504 in the console. Reproduced on LSP11385 with the files picked by real handles,
so a reload restores them as "Allow on every visit" does (`tools/bench_web/bench_flow_web.mjs`;
the local server's arm is `bench_flow_server.mjs`).

- **The freeze.** `agent/evidence/display_contrast.py` `compute` answered with `describe()` while
  holding `_LOCK`, which `describe` takes again (a plain `threading.Lock`): a second auto-contrast ask
  while a job ran waited on itself for ever. On the desktop one request thread hung; on the website
  the one Python thread did, and the whole site stopped. Found by pausing the frozen worker over CDP
  and tracing Python with `?pydebug` wrappers. `tests/test_display_contrast_steps.py` fails on the
  old code.
- **The scan.** A channel's exact window waited for its full-resolution maximum, read window by
  window through geotiff.js (7-9 s a channel). `node/tiffReader.js` `tiledMax` reads a tiled plane in
  large reads with the browser's zlib: 3.1-3.4 s, as the desktop's own read (tifffile, 8 threads:
  3.7 s); held to numpy (`tests/js/tiff_max_probe.mjs`).
- **Provisional windows.** In HD a tile is windowed on the GPU in raw units, so the exact window only
  bounds a channel's slider: stats and fit come at once with (0, the overview's max),
  `window_exact: false`, and the viewer takes the exact one when it lands (`awaitExactWindow`).
- **Auto-contrast at desktop speed.** Its cell crops, the mask labels under them and the overview
  planes are read by the page's workers in one batch (`SourceImage.read_many` -> `page_regions`):
  a four-channel calibration 7.0 s -> 1.1 s. The job steps four channels at a time; after a project
  opens the website fits every channel (0.4 s each) and calibrates the rest in the background while
  nothing else is asked.
- **Reload's kept answers.** Answers Python moves on by itself (the browser node's spec while a
  mask's pyramid is built, its `generation`, auto-contrast calibrating channel after channel) are no
  longer taken for a change, which refused every write of the page (the channel list unsaved, the
  agent bridge signing in every 5 s, a 409 each time); channel names and the spec are kept again
  after a write. The agent bridge no longer asks for a session before registering (a 404 on every
  reload): the registration says whether it was `resumed`.
- **A plain mask's first open.** The page builds a plain mask's label pyramid on eight workers; the
  project's load beside it ran 6-11x slower (the 66 GB image opened in 11 s instead of 1 s, the table
  read in 3.8 s instead of 0.6 s), and the viewer was drawn 17 s after the open. The build now works
  on one worker while a page waits on Python for more than 50 ms or on the browser node's tiles, and
  on all of them otherwise or when a request waits on the build itself (`node/pace.js`,
  `browserPyramid.js` `pace`): drawn in 4.3 s; the mask, built beside a working viewer, lands at
  26 s instead of 20 s.
- Also: a folder named `plexora` in any case is the library (a lowercase `Documents/plexora` got a
  second library inside it); the threadpoolctl deprecation warning is silenced; two inline `onerror`
  handlers the CSP blocked are listeners.

LSP11385 (40-ch uint16, 66.7 GB, prepared label pyramid, 202,049 cells), this Mac, nothing else
running, seconds:

| | local server | website (0.0.44) | live 0.0.43 |
|---|---|---|---|
| import (image, mask, CSV) | 0.5 | 3.0 | 4.3 |
| open -> first channel visible | 3.9 | 3.0 | 15.9 |
| 4 channels on at once -> visible | 4.8 | 2.7 | 13.3 |
| ... at their fitted contrast | (with the final) | 5.2 | ~30 |
| ... at the final auto-contrast | 4.8 | 8.9 | not in 50 |
| 4 channels on after a 5 s look -> final auto-contrast | 3.6 | 2.3 | -- |
| reload -> image back | 0.9-1.0 | 1.6 | 3.7-20.5 |

The same with the plain mask `cell.ome.tif` (its pyramid built on import): viewer drawn after the
open, local server 4.5, website 4.3 (17 before pacing); the mask's build 23 (15 alone).

Left: auto-contrast on channels switched on the moment the image appears (one Python thread beside
the page's tile reads: ~2x the local server), Python's restart on a reload (done in round four:
one SharedWorker per tab), the page's own CSV parse on import, geotiff.js's big-endian predictor
tiles (the fast scan does not share the bug; the tiles still come from geotiff.js).

## Round four (after 0.0.44): tiles that never came, the tab's Python kept

- **Tiles that never came (seen on 0.0.44).** OpenSeadragon gave up on channel tiles after 90 s
  ("Image load exceeded timeout"), and the mask was slow to sharpen. When the page finishes a mask's
  pyramid, Python attaches it and loads the project again, so the browser node's spec changes (the
  mask served, a new `generation`). The node then closed every reader of the project, the image's
  too, and `TiffPool.close()` terminated its workers without failing the reads they had not
  answered. Each lost read kept one of the viewer's six tile slots (`views/tileFetch.js`) for good.
  Reproduced on the live site by loading the project again while zooming: a tile stuck and its slot
  never freed (`inFlight` 1 for good). Now a part whose spec and file are unchanged keeps its readers,
  a closed or dead reader fails what it was asked (the tile then comes from Python), a mask still
  being prepared is asked about again aside (`recheck`) so the image's tiles never wait on Python,
  and a request OSD gives up on frees its slot (`untilDropped`). Pinned in
  `tests/js/browser_node_swap_probe.mjs` (fails on 0.0.44) and `tests/js/tile_fetch_probe.mjs`.
- **A mask built while the user looks around.** The build stepped down to one worker whenever the
  browser node read tiles: LSP11385's plain mask took 77 s while zooming (23 s alone). Half its
  workers there instead: 30 s, with tiles at p50 214 ms / p90 323 ms and the open unchanged (image
  drawn 4.1 s). One worker while Python is waited on stays: half there made the open 1.1 s slower.
- **One Python per tab, kept across a reload** (Chrome and Edge). The tab's Python is a SharedWorker
  named for the tab (an id in sessionStorage, its Web Lock held by the page), created with
  `extendedLifetime`, so the page a reload brings connects to the Python the last one left. Each tab
  has its own, as before: one thread and one heap each. A duplicated tab finds the id's lock held
  and takes a new one. The worker answers only the page that asked (a reloaded page numbers its
  requests from 1 again), answers what Python had asked the old page with nothing (each ask's
  fallback), and closes itself 30 s after its tab's lock comes free with no page back. Chrome would
  otherwise keep it 300 s, measured: its blog says 30. A SharedWorker is not cross-origin isolated,
  so no SharedArrayBuffer: Python reaches the page by JSPI and a job is stopped by message. A page
  build lost to the reload is asked of the new page (`lost`, at most three times), Help › Restart
  Plexora in This Tab starts a new one, and a tab whose Python failed or does not answer gets a new
  one. Firefox and Safari keep the page's own worker.
- **Background contrast in smaller steps.** The channels nobody asked about yet are calibrated one a
  step, not four: while that runs, requests waited p50 289 / p90 2,066 / max 3,540 ms, now
  175 / 700 / 868 (the work itself 21 → 25 s, nobody waiting on it). What remains of auto-contrast
  on channels switched on the moment the image appears is compute in one WebAssembly thread
  (in that run ~9.7 s: the project's own load ~2.3 s, four fits ~2.1 s one after another, the
  calibration's preparation 1.7 s and four channels 3.3 s), which the desktop runs in threads.
- `layer_jobs._finish` writes the project before the in-memory outcome: a reader between the two saw
  "ready" with the layer still pending (`tests/test_layer_jobs.py` failed 3 runs in 10 on main, 0 in
  10 now).

LSP11385 with the plain mask, this Mac:

| | local server | 0.0.44 | round four |
|---|---|---|---|
| reload -> image drawn | 0.9-1.0 | 1.6 | 0.47-0.53 |
| reload -> a request only Python answers | -- | after Python's start (~3 s) | 2 ms |
| mask built while zooming | -- | 77 | 30 |
| reload while the page builds the mask | -- | Python restarted, build again | Python kept, build again: mask on 26 s later |

## Round five (after 0.0.45): auto-contrast on channels switched on at once

What was left after round four: four channels switched on the moment LSP11385's image appears
took 8-11 s on the website to show their calibrated windows, 4.7-4.9 s on the local server. The
website does the same work in one WebAssembly thread; profiled in the page's own Python
(`?pydebug`, cProfile around the routes' views while a user's clicks ran):

- the calibration's preparation read the cell table a second time (the agent session's own copy,
  parsed afresh from the page: 1.5 s), though the viewer held the very same table;
- its image reader walked the 66 GB OME-TIFF's directories again (280 pages, 1,217 reads through
  the page: about 1 s), though the viewer had opened the same file;
- each channel's cells were sorted twice (`select_display_cells`);
- the calibration waited behind the four fits (0.4-0.7 s each: 93 EM iterations a channel on
  these, not overhead around them), because a request goes before a job's step;
- and the viewer read the result on a 1.5 s, then 2.4 s, then 3.8 s schedule, so a window ready
  at 4.5 s was seen at 5.7 or 8.1 s depending only on where it fell between two polls.

Changed, every result the same as before:

- **The session takes the viewer's table** (`data_model.viewer_table`, through
  `plexora.api.dataset._viewer_table`; agent/session.py `_borrowed`) when it is the table the
  session would read: the same project, read with the same spec (table correction included) from
  the file as it is now (its fingerprint taken when the viewer read it), loaded and not still
  loading, a file this process reads (never a node's, never a zarr store, whose directory's stat
  misses a rewritten key; on threads, never a wide table read through its adapter on demand). The
  session only reads it. Desktop too: an agent's first look at the open project no longer reads
  its table again. tests/test_agent_session_viewer_table.py.
- **One opened TIFF on the website** (`tiff_series.open_channel_series`): the viewer's image
  provider and an agent's `SourceImage` share `(TiffFile, channel series)` by path, size and
  mtime; the reader leaves a shared file open. With threads each keeps its own handle, as before
  (tifffile reads one handle behind a lock). tests/test_shared_channel_series.py.
- **One sort per channel** (`select_anchor_cells(order=...)`), the same cells:
  tests/test_cell_window_sorted_once.py checks them against the two-sort version.
- **A fit waits for its channel's calibration on the website** (data_routes `_calibration_first`,
  display_contrast `calibrating`): while the job asked for has the channel still to do, or has
  done it and the viewer has not read it (at most 8 s), the fit is asked again 250 ms later. The
  viewer keeps the calibrated window either way (a later fit never replaces it), so only the order
  changes. Not for a job asked for every channel, not for `_ahead`, not on the desktop.
  tests/test_fit_after_calibration.py.
- **The viewer polls for the calibration sooner** (viewerSidebar `GATING_POLL_FIRST_MS` 1500 ->
  500, each wait 1.3x the last, not 1.6x, up to 8 s as before). Both front ends.
- The empty SharedWorker that tells whether the browser keeps a worker across a reload closes
  itself at once (it lived as long as its page).

LSP11385, prepared mask, four channels switched on the moment the image is drawn
(tools/bench_web/bench_flow_*.mjs `--pause 0`, now with `levels`: when each channel's window
lands in the viewer), ms after the first click until the last calibrated window is on screen
(02_AF1, an autofluorescence channel, has none on either):

| | runs |
|---|---|
| local server, main | 4,851 / 4,701 |
| local server, this round (polls sooner) | 2,751 / 2,751 / 2,802 |
| website 0.0.45 | 11,350 / 8,301 |
| website, the table, the TIFF and the sort | the calibration's step ends at 6.1-6.2 s (was 9.2-9.5) |
| website, this round | **5,101 / 4,400 / 4,801** |

What is left on the website (from the requests and steps of those runs): the page's scan of the
four channels' windows (1.6-2.4 s, already in the page's workers), the preparation (0.4 s), then
calibrating the four channels (2.0 s, one after another). The local server does the last two on
threads beside the fits. A pool of helper Pythons for the per-channel work (the plan's step 3) is
what would close the rest.

## Round six (after 0.0.46): mixture fits in helper Pythons

Traced in the page's Python (a recorder installed at boot: every fit, window look and task, on
Python's clock), four channels switched on the moment LSP11385's image appears: the open's own
channel's fit (Hoechst, 2.7 s of EM) was running when the click came, and the click's
calibration was asked for 2.9 s later, behind it. With one thread, any fit holds everything; the
desktop fits on threads of its own.

- **Helper Pythons** (services/fitHelpers.js, workers/fitWorker.js): Pyodide with numpy and
  scikit-learn only, two on a machine with eight cores or more, one with four, none below that
  or under 4 GB (`navigator.deviceMemory`), loaded one after the other once Python is ready
  (about 108 MB each).
- **The same fit, bit for bit.** The fit itself is plexora/server/models/mixture_fit.py (numpy
  and scikit-learn only, any number of components, in the values' own type), which a helper
  runs exactly as the main Python imported it: the bundle's compiled file, sent with each fit
  (the bundle ships no sources), or its text where there is one. In the page: eight LSP11385
  channels' contrast fits and four markers' gating fits equal the main Python's byte for byte,
  and the gates the gating route answers are the ones 0.0.46 answered.
- **Who hands fits over**: the contrast fit route (`get_channel_gmm`), the gating panel's
  per-marker fit (`/plugins/gating/get_gating_gmm`, through the plugin API's new
  `api.page_fits()` / `api.fit_mixture` / `api.ask_again`), and the warm-up's fits after an open
  (`_warm_on_page`, one at a time, so a helper is left for a click; it comes back every 100 ms
  with a cheap "done?" -- `_work.spawn(after_s=...)`, a task that waits its turn, the worker
  sleeping until it is due). A request is asked again every 100 ms until the fit is back
  (`FitPending`). No helper ready, one failed, a reload's unknown job: the fit is made in the
  main Python, as before. Off the website nothing changes.

The five candidates, measured in the page's Python before choosing (LSP11385 has 202,049 cells;
synthetic columns for 2 and 5 million, as large slides have; the machine loaded by another
session's tests, load average 8-27):

| | 202 k cells | 2 M | 5 M | done |
|---|---|---|---|---|
| (1) warm-up fits after an open, each | 0.25-0.66 s | same (the overview) | same | helpers |
| (2) gating's per-marker fit (auto gate) | 1.5-5.6 s | 4.3 s | 4.3 s (100 k sample) | helpers |
| (3) calibration's cell selection, a channel | 0.06 s | 0.30 s | 1.0 s | no (below) |
| (4) QC's neighbour context | 0.63 s | 13.3 s | 40.7 s | no (below) |
| (5) the classifier: fit / scores | 1.05 / 0.13 s | - / 1.2 s | - / 2.9 s | no (below) |

(3), (4) and (5) run inside a job or a calibration step that holds the main Python for its whole
run, so a helper would not free it; all a helper could add is splitting the work, at 2-5 million
cells shipping 40-240 MB each way and holding a second copy of the tree. Left as they are. The
calibration's cost at scale is its stable sort of every cell for four narrow rank bands; an exact
band-only selection (the same cells, ties broken by row as the sort breaks them) would be about
2.5x cheaper at 5 million cells and is the follow-up there.

LSP11385, prepared mask, four channels switched on as the image appears, 0.0.46 and this round
alternated on the same machine, ms after the first click:

| | 0.0.46 | helpers |
|---|---|---|
| last calibrated window on screen | 5,450 / 4,401 / 6,250 / 5,250 / 5,152 | 5,101 / 4,300 / 4,001 / 4,050 / 4,001 |
| last window of any kind | 6,352 / 5,852 / 7,702 / 6,103 / 6,001 | 5,101 / 4,300 / 4,001 / 4,000 / 4,001 |
| open: image lit | 3,383-4,424 | 3,264-3,655 |
| warm-up fits on the main Python (25 s after the click) | 1.0-1.1 s, up to 0.49 s at once | 0.54-0.57 s: the first fit, made before the helpers had loaded |

Gating panel, a marker's fit (`/plugins/gating/get_gating_gmm`) with a `/health` every 100 ms
beside it:

| marker | 0.0.46: fit / longest /health | helpers: fit / longest /health |
|---|---|---|
| CD45 | 5,952 / 5,827 ms | 5,718 / 41 ms |
| Ki67 | 1,373 / 1,272 | 1,327 / 43 |
| CD3e | 1,820 / 1,717 | 1,756 / 41 |
| Nucleus | 1,853 / 1,749 | 1,905 / 43 |

Memory of the page's workers (`measureUserAgentSpecificMemory`, the tab's Python not counted):
129 MB on 0.0.46, 330 MB with two helpers (215 MB theirs).

What is left on the click: the calibration's own step, 2.5 s cold for four channels, three
quarters of it the cells' crops read through the page (0.84 s a channel, already spread over the
page's TIFF workers), to be measured on a quiet machine before changing.

## Rerun

```bash
export PLEXORA_PLAYWRIGHT_MODULES=... PLEXORA_PYTHON=... PLEXORA_BENCH_WORK=...
python tools/browser_build/build.py --out $PLEXORA_BENCH_WORK/bundle --python314 <CPython 3.14.2> --brotli <brotli> --kernels-wheel <whl>
python -m plexora.client.static_build --out $PLEXORA_BENCH_WORK/site_new --py /py/<version>/
node tools/bench_web/bench_dataset_web.mjs --site $PLEXORA_BENCH_WORK/site_new --py-dir $PLEXORA_BENCH_WORK/bundle/py --out tonsil.json
node tools/bench_web/bench_dataset_web.mjs --fixture $PLEXORA_BENCH_WORK/fixture/lsp \
    --files image.ome.tif,mask.labels.pyramid.ome.tiff,cells.csv --input --no-reload \
    --tools roi,cell_explorer --site ... --py-dir ... --out lsp.json
```

The CPython must be 3.14.2: uv's `python3.14` is 3.14.0rc2, whose bytecode Pyodide refuses.
Don't clear stale pyramids with `find fixture -name '*.pyramid.ome.tiff' -delete`: it also
deletes a fixture's symlinked label pyramid.
