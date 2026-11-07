# Website Phase 1 spikes: S-1 to S-4

The programme plan's Phase 1 named four questions to answer before building on
the Python worker. S-1 to S-3 are answered first; S-4 (h5py in Pyodide over
WORKERFS against h5wasm) was measured with the h5ad stage, in its own section
below.

Measured 2026-11-06 on this Windows machine (i9, NVMe), Chrome 156.0.8078.76
headless, a fresh profile per run, served from this checkout by
`tools/browser_build/serve.py` on loopback (no network throttling), against the
bundle `tools/browser_build/build.py` wrote for plexora 0.0.36: Pyodide
314.0.7, every module precompiled, wheels stored and served with brotli.

| Spike | Question | Answer | Gate | Verdict |
|---|---|---|---|---|
| S-2 | Does `create_app()` with every plugin boot in Pyodide in time? | Ready 6.1 s after navigation (core 1.4 s, base packages 3.9 s, wheels 4.0 s, app 6.1 s); `create_app()` 1.55 s; 415 routes; no stage loaded at boot; 319 MiB wasm heap | ready ≤ P9 trimmed (7.1 s) + 2 s | **GO** |
| S-1 | Can the worker answer a request while Python is suspended inside a job (a poll waiting on the user, through JSPI)? | A job sleeping 3 s through `_work.wait` (`run_sync(asyncio.sleep)`): `/health` answered in 15 ms and `/projects` in 2 ms during the sleep; the job woke at 3.00 s and finished | answered during the sleep | **GO** |
| S-3 | What does writing the OPFS project store back cost, does sqlite work on it, and does it all survive a reload? | `syncfs` writes only what changed: 18 ms after 1 MB, 31 ms after 10 MB, 117 ms after 50 MB, 2–3 ms with nothing new. sqlite on the mount: 20,000 inserts in 28 ms. After a reload: every row and file there; boot 5.8 s with 61 MB in the store | works, and persists | **GO**, with the constraint below |

## What they decide

- **S-2.** The plan's design D1 stands: the website's primary is Plexora's own
  Flask app in the worker. Booting it costs 1.5 s on top of the packages, less
  than P9's stub package root saved, so the 7.1 s P9 measured for the gating
  path alone is now 6.1 s for the whole app. The first visit downloads 46.8 MB
  (core 5.5 MB, base stage 41.3 MB).
- **S-1.** The idle hook (`_work.set_idle`) can let time pass while the page is
  served: a gating session's pause, a set-up form, a viewer command waiting for
  the tab's acknowledgement do not deadlock the worker. The worker answers
  requests in that gap only (`plexoraIdle`); a stage loading on first import
  suspends Python too, and requests wait for it.
- **S-3.** The project store can stay on NativeFS with a write back after each
  writing request. **Constraint:** mounting reads the whole directory into
  memory (Pyodide's NativeFS keeps a MEMFS copy), so `/data` must hold only
  state -- configs, plugin stores, sessions, records -- and never pixels:
  pyramids, column stores and other caches live in OPFS outside it and are read
  by the page's own code or through WORKERFS.

## Rerun

    python tools/browser_build/build.py --python314 <CPython 3.14> --brotli <brotli>
    node tests/browser/boot.mjs           # S-2
    node tests/browser/spike_reentry.mjs  # S-1
    node tests/browser/spike_store.mjs    # S-3

`tests/test_browser_python.py` asserts S-2's correctness half on every run that
has Chrome, the build and playwright-core (`npm ci` in tools/poc_browser_node).

## S-4: h5py in Pyodide against h5wasm

Measured 2026-11-06 on the same machine and Chrome. The bundle is the 0.0.36
one built at 5b841785, with the wheels its h5ad stage lacked (226ad50e). That
stage holds h5py 3.13.0 on HDF5 1.12.1, anndata 0.13.2 and pandas 3.0.2.
Records: `S4-S-2026-11-06.json`, `S4-L-2026-11-06.json`.

**Question.** An .h5ad the user picked: does the website read it with h5py in
the Python worker (the file mounted with WORKERFS like every pick, read in the
worker's 1 MiB blocks), or with h5wasm in the page (P4)? The plan's rule:
Python reads what it reads, unchanged, unless the page is needed for capacity
or for a codec Pyodide lacks. P4 found h5wasm's lzf filter aborting on
strings, so lzf could have decided it.

**Set-up.** P4's fixtures: 11,201 (S) and 1,260,592 (L) cells x 90 float32
features, written by anndata 0.13 as {dense, CSR} x {none, gzip, lzf}. Each
file is read two ways in the worker and in CPython (h5py 3.16, HDF5 2.0; run
first, so the OS cache is warm for both):

- P4's eight reads (obs names, a numeric obs column, a categorical, obsm
  spatial, var names, a 64 MB row block of X, one X column, all of X as
  columns). Both Pythons run `truth_p4.py` from its own source, median of 3,
  and every answer is SHA-256-hashed.
- The table load the viewer runs, `AnnDataAdapter.load_table()`, with a digest
  of every column.

h5wasm is P4 rerun the same evening (`node bench/run.mjs --poc P4 --fixture
S|L`): h5wasm 0.10.3 and h5wasm-plugins 0.3.0 in a worker, over plain WORKERFS.

| | h5py in Pyodide | h5wasm in the page |
|---|---|---|
| Reads identical to CPython's | **96 of 96** (S and L), lzf strings included | 84 of 96: all 12 lzf string reads abort (`memory access out of bounds`) |
| Time against CPython, L, reads CPython takes >= 5 ms | median 1.4x; 0.6-2.3x, except one X column on its own (8-26x) | median 6.8x; 1.9-96x |
| All of X as columns, L | 0.9-3.7 s (CPython 0.5-2.3 s) | 1.0-9.4 s |
| The viewer's table load, L | 1.4-4.9 s, 1.2-2.0x CPython; every column identical | (no Python path) |
| Memory, one L table (113 M values) | worker heap 0.38 -> 1.04 GiB after the load (+0.66 GiB, ~6 B a value); renderer peak 2.0 GB | renderer peak 3.2 GB over its matrix |
| Loading the reader | the h5ad stage, on the first import: 0.75-0.95 s from loopback | in the page bundle |

Pyodide's h5py builds in the filters h5py ships (gzip, shuffle, lzf;
`h5py.h5z.filter_avail`), so nothing needs a plugin.

Time at L, ms: Pyodide / h5wasm / CPython (median of 3; - means it aborted):

| Read | dense, none | dense, gzip | dense, lzf | CSR, none | CSR, gzip | CSR, lzf |
|---|---|---|---|---|---|---|
| obs names (1.26 M strings) | 404 / 1425 / 365 | 415 / 1987 / 428 | 418 / - / 414 | 385 / 1193 / 371 | 419 / 1420 / 453 | 396 / - / 409 |
| a numeric obs column | 12 / 24 / 5 | 44 / 288 / 39 | 10 / 371 / 14 | 11 / 16 / 5 | 49 / 231 / 54 | 9 / 259 / 14 |
| obsm["spatial"] | 22 / 47 / 10 | 97 / 730 / 80 | 30 / 635 / 33 | 18 / 30 / 10 | 105 / 519 / 84 | 29 / 481 / 35 |
| X, a 64 MB row block | 66 / 167 / 33 | 372 / 1489 / 290 | 221 / 1016 / 76 | 98 / 696 / 64 | 196 / 702 / 164 | 76 / 930 / 76 |
| X, one column alone | 1089 / 7312 / 76 | 332 / 242 / 40 | 310 / 148 / 12 | 844 / 3804 / 280 | 1514 / 4371 / 979 | 666 / 4821 / 347 |
| X, all of it as columns | 875 / 1005 / 488 | 3658 / 9419 / 2319 | 1835 / 7265 / 846 | 1172 / 4155 / 515 | 1702 / 7211 / 1251 | 934 / 5546 / 575 |
| the adapter's table load | 1386 / - / 1120 | 4921 / - / 3027 | 2991 / - / 1497 | 1990 / - / 1301 | 2658 / - / 2214 | 1849 / - / 1529 |

**Decision: the website reads an .h5ad with h5py in the Python worker.**
Plexora's own adapter reads it unchanged, and the page reads nothing of it.

- **Codec.** lzf decides nothing: Pyodide's h5py reads it, strings included,
  where h5wasm's plugin aborts. A filter neither side ships (hdf5plugin's
  zstd, blosc and the rest) fails on the website as it does in the desktop
  app, whose AnnData reader does not load hdf5plugin either: "Can't
  synchronously read data (can't open directory)".
- **Capacity.** The page is not needed. An L table takes 0.66 GiB of the
  worker's heap, well short of the 3 GiB STOP.
- **Speed.** At L, h5py in Pyodide is faster than h5wasm at every read but one:
  a single column of chunked, compressed X, which the adapter never reads for a
  panel (it reads row blocks). At S both take a few milliseconds, and h5wasm
  reads uncompressed obs names 2-4 ms sooner. A likely part of the difference,
  not measured on its own: the worker reads picked files in 1 MiB blocks (P9),
  and h5wasm's plain WORKERFS does not. The table load is 1.2-2.0x CPython.
- **h5wasm is not needed.** A later browser node that answers cell routes from
  the page should take the columns from Python's frame, not read the file a
  second way. The threshold table's h5ad row ("desktop reads 2-13x faster";
  "lzf strings -> D/N") came from h5wasm and no longer holds for this path.

### Rerun S-4

    node tests/browser/spike_h5ad.mjs [--dir <p4-s or p4-l>] [--repeat 3] [--out FILE]
    node tests/browser/spike_h5ad.mjs --dir <p4-l> --files cells-dense-gzip.h5ad --adapter-only --repeat 1
    cd tools/poc_browser_node && node build.mjs && node bench/run.mjs --poc P4 --fixture S|L --out DIR

Each needs `PLEXORA_BENCH_HOME` pointing at the bundle's home. The P4 fixtures
are in `C:/plexora-bench/browser_node/fixtures/p4-{s,l}` (`bench/p4_fixtures.py`).
