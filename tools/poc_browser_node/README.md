# Browser-node proofs of concept

The proofs of concept behind `docs/internal/BROWSER_NATIVE_INVESTIGATION.md`. They test whether the browser can be Plexora's local node: reading local files, drawing them with the product's own renderer, and answering the viewer's table questions. Data nodes stay first-class peers. Nothing here ships, and nothing here edits `plexora/`. The pages load the product client from the checkout exactly as it is.

Records land in `docs/internal/bench/browser_node/` (one JSON and one MD per POC). The campaign report is `BROWSER_NODE_POC_2026-11-05.md` in the same folder.

## Run

```bash
cd tools/poc_browser_node
npm ci                    # geotiff, DuckDB-Wasm, apache-arrow, kdbush, esbuild, playwright-core
node build.mjs            # page/src -> page/dist (the page bundle and three workers)
npm test                  # the numeric kernels against numpy's rules
node bench/run.mjs --poc P1 --fixture M          # one POC; --out DIR, --label X, --params '{...}'
python bench/verdict.py ../../docs/internal/bench/browser_node --write   # GO / PARTIAL / STOP
```

Requirements:
- Chrome stable at its default path (`PLEXORA_BENCH_CHROME` overrides). Runs are headless, on the GPU (`--use-angle=d3d11`), with the frame-rate limit off, so frame intervals are frame cost.
- The Plexora conda env (`PLEXORA_PYTHON`) for the data nodes and the Python ground truth.
- Fixture paths in `bench/fixtures.json` (`POC_FIXTURES` overrides).
- Scratch caches (channel windows, Python truth, Python's own pyramids, a throwaway TLS certificate) go under `$PLEXORA_BENCH_HOME/browser_node/cache` (default `C:/plexora-bench`).

## What each POC runs

| POC | Handler | What it answers |
|---|---|---|
| P0 | `P0` | The rendering modules (`glInit`, `tileColorize`, `tileDecode`, `tileFetch`, `labelTile`, `labelGpu`, shaders, decode worker) run unmodified from a static page. HD PNG byte order. A probe of `frag.glsl`'s 16-bit rebuild. Canvas identity between a node feed and a browser feed. `bench/p0_audit.mjs` counts the same-origin assumptions in every client script. |
| P1 | `P1`, `P1V`, `P1R` | A local OME-TIFF through geotiff.js workers: open time, per-tile latency and throughput by pool size, native and pako inflate, 200-tile identity with the node's HD PNG, and auto-level against the node. The viewer arms run ABBA against a local Python node at 3/7/15 channels (`P1V`). The same reader runs over HTTP range requests against the Orion S3 bucket (`P1R`). |
| P2 / P2b | `P2`, `P2B` | A label pyramid through the GPU cell layer, with a gate drag and label-tile identity, at 202k and 1.26M ids. A flat mask is turned into a pyramid in a worker into OPFS (Origin Private File System) and compared tile by tile with the pyramid Python builds from the same file. |
| P3 | `P3`, `P3B` | A CSV in DuckDB-Wasm, then a raw column store in OPFS (`--params '{"store":"parquet"}'` for the Parquet copy). The five table questions are checked against the node and against `bench/truth_table.py`. `P3B` breaks down where cold-ingest time goes. |
| P5 | `P5`, `P5LNA` | An existing `plexora node serve` used from a static origin. Scenarios: (a) protocol identity, (d) COOP/COEP, (e) restart, (f) `--allow-origin` spellings, (g) connector pairing (`connector_stub.py`), and (h) the node serving the app under `/rnode/`. `P5LNA` covers (b), Chrome's Local Network Access with the page marked public, over https and http. |

## Layout

- `page/`: the static page (`index.html` loads the product client from `../repo/...` and then `dist/poc.js`).
  - `src/viewer.js` is the minimal viewer made of the product modules.
  - `src/tileSource.js` is copied from `viewerManager.js`, where these helpers are private to the bundle.
  - `src/lib/tiffReader.js` is the geotiff.js reader.
  - `src/table.js` holds DuckDB-Wasm and the column store.
  - `src/stats.js` holds the numpy-rule kernels.
  - `src/p2.js`, `src/p3.js` and `src/p5.js` hold the per-POC page code.
- `serve_static.py`: the static origin. It serves `/poc`, the client half of the checkout under `/repo`, and `/nm`. Flags: `--coop-coep`, `--rnode` (an Open OnDemand-style proxy that serves the app under a node's prefix) and `--tls-cert`.
- `connector_stub.py`: the minimal connector's loopback API, minus the SSH.
- `fixtures.py`: flat-mask fixtures and file fingerprints.
- `bench/`:
  - `run.mjs` is the driver; `pocs.mjs` holds one handler per POC; `procs.mjs` starts the static origin, nodes and Chrome.
  - `verdict.py` holds the thresholds.
  - `truth_table.py` and `truth_mask.py` are the Python ground truth.
  - `p0_audit.mjs` is P0's static audit.
- `tests/`: Node tests of the kernels.
