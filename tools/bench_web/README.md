# Website vs local server benchmark

The website (Plexora in Pyodide, in a tab) measured against the local Python
webserver on the same data and the same machine. Written for the 2026-11
website performance investigation; run it before and after each change to
the website's startup, request path, tiles or plugins.

## Setup

```bash
export PLEXORA_PLAYWRIGHT_MODULES=<a node_modules holding playwright-core>   # as tests/browser/lib.mjs
export PLEXORA_PYTHON=<the Python with Plexora's dependencies>               # the conda env's python
export PLEXORA_BENCH_WORK=<a scratch folder>                                 # fixtures, site, out/
# optional: PLEXORA_CHROME=<a Chrome binary>
```

- **Fixtures.** `$PLEXORA_BENCH_WORK/fixture/<name>/{image.ome.tif, mask.tif, cells.csv}`, one copy
  per arm (`<name>` and `<name>_srv`) so each builds its own mask pyramid. The small one used so far
  is `plexora-public-data/codex-spacec-tonsil`; the large one LSP11385 (66 GB, 202,049 cells; symlinks
  are fine). A gating-capable fixture needs an Image ID column.
- **The site.** `python -m plexora.client.static_build --out $PLEXORA_BENCH_WORK/site --py <bundle URL>`:
  the published bundle (`https://assets.plexoraapp.com/py/<version>/`) for JS-only changes, or a
  bundle built from this checkout (`tools/browser_build/build.py`) served next to the site for
  Python changes.

## Scripts

| Script | What it measures |
|---|---|
| `bench_startup.mjs --url URL --runs N` | the website cold (fresh profile), on reload, and warm (new launch, same profile): loader paint, frame, home visible, Python's marks |
| `bench_server_startup.mjs --runs N` | the local server: process start to "Serving", home page, reload, a second tab |
| `bench_dataset_web.mjs [--fixture DIR] [--files a,b,c] [--tools a,b] [--no-reload] [--input] [--site DIR] [--py-dir DIR]` | import, viewer drawn, tiles p50/p95, a 7-step tour (frame p95), every tool's open, the figure library and back, a reload, memory; which Python stage arrived when and what asked for it (`stages`, the last page's) |
| `bench_dataset_server.mjs [--fixture DIR] [--tools none] [--no-figures] [--no-reload]` | the same flow on the local server |
| `bench_packages.mjs` | Python's boot, per package and per import, against a bundle |
| `bench_flow_web.mjs --site DIR --py-dir DIR --fixture DIR [--files a,b,c] [--pause MS]` (or `--origin URL`) | the user's flow with real file handles: import, open (drawn, lit), four channels on (their stats, fit and auto-contrast as they land), a reload restored from kept answers; the page's pyramid builds |
| `bench_flow_server.mjs --repo DIR --fixture DIR [--files a,b,c] [--pause MS]` | the same flow on the local server |
| `bench_tile_path.mjs --file TIFF [--checkout REPO] [--channels a,b] [--regions "x,y;x,y"] [--detail] [--out F]` | a checkout's own tile path (tileFetch.js, browserNode.js, the worker bundle) on a file where it is -- a share included, which the fixture benches cannot do: every view of a zoom-and-pan tour, ms; `--detail`, the reads the workers made. Compare two checkouts by alternating them, each run on a region and channels nothing read before |
| `bench_first_view.mjs --mode web\|local --file PATH [--url URL] [--label L] [--out DIR] [--limit S] [--no-interact]` | a person opening an image, in a new Chrome profile with a visible window: the site (a real drop of the file on the home page, the first-use questions answered) or the local server (the path pasted, Load), then at once zoom in, three more channels on, zoom to full resolution, pan; when the image was first on screen (the page's first view, services/firstView.js), when the viewer was drawn, each interaction until drawn. A file on a share is read where it is. Results: `docs/internal/bench/web/WEBSITE_FIRST_VIEW_2026-11-10.md` |
| `bench_python_open.mjs --site DIR --py DIR [--ahead-first] FILE [FILE ...]` | Python's open of picked TIFFs in the website's own worker (a build of the checkout), with the read ahead (plexora/browser/tiff_ahead.py) and without, alternating, one file a trial: ms, rounds and pieces read ahead. Prints lines; give files of one kind not read lately |

Each writes one JSON record to `$PLEXORA_BENCH_WORK/out/`. Results so far are in
`docs/internal/bench/web/`.

## Traps

- A tool whose prerequisites are missing opens a requirements dialog whose promise only its own
  buttons settle: answer it, never `dialog.close()` (benchlib's `openTools` presses Save, then the X).
- An `evaluate` whose expression returns an OpenSeadragon object (`viewport.goHome()` returns the
  viewport) sends the viewer's whole object graph back: 587 MB on LSP11385, which ends Playwright's
  connection (`ERR_STRING_TOO_LONG`). Prefix such an expression with `void`. (This, not a console
  message, is what ended the local server's arm at fit-to-screen.) benchlib prints the first and
  last bytes of any DevTools message too long to be a string, and the run goes on.
- A server started without `-u` / `PYTHONUNBUFFERED=1` never prints its address in time.
- On the website, channel tiles are read by the page itself (`tileFetch.local`) and never appear in
  Resource Timing: `viewerDrawn` wraps that function to time them (`tiles.local` counts them), and
  `openTileFetch`/`reloadTileFetch` record the fetcher's own counters.
- `frameNavigations` lists every navigation of the app's frame: a tool or fetch that fails for no
  reason of its own is often a frame reloaded under it.
