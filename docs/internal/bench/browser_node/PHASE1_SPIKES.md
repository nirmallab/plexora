# Website Phase 1 spikes: S-1, S-2, S-3

The programme plan's Phase 1 named four questions to answer before building on
the Python worker. Three are answered here; S-4 (h5py in Pyodide over WORKERFS
against h5wasm) belongs to the h5ad stage and is measured with it.

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
