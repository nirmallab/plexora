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
