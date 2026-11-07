# Browser-node proofs of concept

The proofs of concept behind `docs/internal/BROWSER_NATIVE_INVESTIGATION.md`. They test whether the browser can be Plexora's local node: reading local files, drawing them with the product's own renderer, and answering the viewer's table questions. Data nodes stay first-class peers. Nothing here ships, and nothing here edits `plexora/`. The pages load the product client from the checkout exactly as it is.

Records land in `docs/internal/bench/browser_node/` (one JSON and one MD per POC). The campaign report is `BROWSER_NODE_POC_2026-11-05.md` in the same folder.

## Run

```bash
cd tools/poc_browser_node
npm ci                    # geotiff, DuckDB-Wasm, apache-arrow, kdbush, h5wasm (+ plugins), zarrita, hyparquet, esbuild, playwright-core
node build.mjs            # page/src -> page/dist (the page bundle and its workers)
npm test                  # kernels against numpy's and Python's rules; the CSV parser's edge cases
node bench/run.mjs --poc P1 --fixture M          # one POC; --out DIR, --label X, --params '{...}', --browser firefox|webkit
bash bench/campaign.sh    # every record, in order (about 1.5 h here); HEADED=1 adds P5LNAH
python bench/verdict.py ../../docs/internal/bench/browser_node --write   # GO / PARTIAL / STOP
```

Requirements:
- Chrome stable at its default path (`PLEXORA_BENCH_CHROME` overrides). Runs are headless, on the GPU (`--use-angle=d3d11`), with the frame-rate limit off (so frame intervals are frame cost), except P6, which runs Chrome as the product tour does.
- P8 needs Playwright's own Firefox and WebKit: `node node_modules/playwright-core/cli.js install firefox webkit`.
- P9 needs a Pyodide release and the wheels beside it: `python bench/p9_dist.py` (about 320 MB, once; the trimmed builds with `--variant pyc --python314 <a CPython 3.14>` and then `--variant pycbr`, which needs the `brotli` encoder). Its fixtures are made by `bench/p9_fixture.py`, CPython's reference by `bench/truth_p9.py`, both cached.
- The Plexora conda env (`PLEXORA_PYTHON`) for the data nodes, the scratch product server (P6) and the Python ground truth.
- Fixture paths in `bench/fixtures.json` (`POC_FIXTURES` overrides). `fixtures.py` writes the flat masks and flat images; `bench/p4_fixtures.py` the AnnData and SpatialData files.
- Scratch caches (channel windows, Python truth, Python's own pyramids, a throwaway TLS certificate, the P6 data root, screenshots) go under `$PLEXORA_BENCH_HOME/browser_node/` (default `C:/plexora-bench`).

## What each POC runs

| POC | Handler | What it answers |
|---|---|---|
| P0 | `P0` | The rendering modules (`glInit`, `tileColorize`, `tileDecode`, `tileFetch`, `labelTile`, `labelGpu`, shaders, decode worker) run unmodified from a static page. HD PNG byte order. A probe of `frag.glsl`'s 16-bit rebuild. Canvas identity between a node feed and a browser feed. `bench/p0_audit.mjs` counts the same-origin assumptions in every client script. |
| P1 | `P1`, `P1V`, `P1R` | A local OME-TIFF through geotiff.js workers: open time, per-tile latency and throughput by pool size, three inflates (native, native through a Blob as the product's tile worker does, pako), 200-tile identity with the node's HD PNG, and auto-level against the node. The viewer arms run ABBA against a local Python node at 3/7/15 channels (`P1V`). The same reader runs over HTTP range requests against the Orion S3 bucket (`P1R`). |
| P2 / P2b | `P2`, `P2B` | A label pyramid through the GPU cell layer, with a gate drag and label-tile identity, at 202k and 1.26M ids. A flat mask made into a pyramid by band workers into OPFS (Origin Private File System), compared tile by tile with the pyramid Python builds from the same file (`--params '{"workers":N}'`). |
| P3 | `P3`, `P3B` | A CSV into a raw column store in OPFS, parsed by plain workers (`workers/csvWorker.js`, the default) or by DuckDB-Wasm (`--params '{"parser":"duckdb","parallel":N}'`; `{"store":"parquet"}` for the Parquet copy). The five table questions are checked against the node and `bench/truth_table.py`. `P3B` compares the JavaScript parser with DuckDB bit for bit and breaks down where time goes. |
| P4 | `P4` | The AnnData / SpatialData matrix: six .h5ad files (dense and CSR X; none, gzip, lzf) through h5wasm over WORKERFS, and a SpatialData store (zarr v3) through zarrita and hyparquet (DuckDB beside it), every answer hashed against `bench/truth_p4.py` (h5py, zarr-python, pyarrow). |
| P5 | `P5`, `P5LNA`, `P5LNAH` | An existing `plexora node serve` used from a static origin. Scenarios: (a) protocol identity, (d) COOP/COEP, (e) restart, (f) `--allow-origin` spellings, (g) connector pairing (`connector_stub.py`), and (h) the node serving the app under `/rnode/`. `P5LNA` covers (b), Chrome's Local Network Access with the page marked public, headless. `P5LNAH` is the same headed: the prompt, a screenshot of it, and the fetch it holds (opens a visible window). |
| P6 | `P6` | The product itself (a scratch Plexora server, `tools/bench_remote/tour.mjs`: the app's own tour and completion test) against the browser node (the same steps, waypoints and completion rule), ABBA. |
| P7 | `P7` | A flat multichannel image (zlib strips, uncompressed, tiles) made into a pyramid by the same band workers, compared with Python's `pyramidize_image`, then reopened after a relaunch. |
| P8 | `P8` | The S features (P0, P1, P2b, P7, P3, P4) in Playwright's Firefox and WebKit, and the picture against Chrome's. |
| P9 | `P9` (`bench/p9.mjs`) | Plexora's AI layer in the browser: the gating session's Python, as it ships, in Pyodide in a worker beside the viewer (`page/src/workers/pyWorker.js`, `page/py/`), against CPython on the same synthetic project (`bench/truth_p9.py`, `bench/compare_p9.py`): startup cold, return and over a 50 Mbps / 20 ms link, the viewer with and without the worker booting, the oracle session's packets, gates and time, and a 1.26 M-cell table handed to Python. Not in `campaign.sh`'s default run. |
| L2 | `bench/l2.mjs` (page `bench/l2_page/`) | openslide-js (OpenSlide 4.0.0 in WebAssembly) against openslide-python on OpenSlide's public test slides (MRXS, NDPI, SVS, BIF, DICOM): opens, every property, read_region bytes (`bench/truth_l2.py`, `bench/compare_l2.py`), viewer-sized tile timing one at a time and over several opens; `--browser firefox|webkit` for function; `bench/desktop_vs_openslide_l2.py` holds the desktop's tifffile reader to OpenSlide. Record: `L2.md`. Not in `campaign.sh`. |

## Layout

- `page/`: the static page (`index.html` loads the product client from `../repo/...` and then `dist/poc.js`).
  - `src/viewer.js` is the minimal viewer made of the product modules.
  - `src/tileSource.js` is copied from `viewerManager.js`, where these helpers are private to the bundle.
  - `src/lib/tiffReader.js` is the geotiff.js reader (tiles, regions, direct reads of uncompressed and deflate strips).
  - `src/lib/reduce.js` holds the pyramid kernels (nearest neighbour, Python's `reduce2`).
  - `src/table.js` holds the column store and both CSV parsers; `src/stats.js` the numpy-rule kernels.
  - `src/workers/`: `tiffWorker`, `pyramidWorker` (the builder's coordinator) and `pyramidBandWorker`, `csvWorker`, `h5Worker`.
  - `src/p2.js` … `src/p5.js` hold the per-POC page code; P6's tour is in `poc.js`.
- `serve_static.py`: the static origin. It serves `/poc`, the client half of the checkout under `/repo`, and `/nm`. Flags: `--coop-coep`, `--rnode` (an Open OnDemand-style proxy that serves the app under a node's prefix) and `--tls-cert`.
- `connector_stub.py`: the minimal connector's loopback API, minus the SSH.
- `fixtures.py`: flat-mask and flat-image fixtures, and file fingerprints.
- `bench/`:
  - `run.mjs` is the driver; `pocs.mjs` holds one handler per POC; `procs.mjs` starts the static origin, nodes and browsers.
  - `verdict.py` holds the thresholds.
  - `truth_table.py`, `truth_mask.py`, `truth_image.py` and `truth_p4.py` are the Python ground truth; `p4_fixtures.py`, `p6_project.py` and `reduce_vectors.py` make inputs; `screen_capture.ps1` is P5LNAH's screenshot.
  - `p0_audit.mjs` is P0's static audit.
- `tests/`: Node tests of the kernels (`reduce_vectors.json` is Python's arithmetic) and of the CSV parser.
