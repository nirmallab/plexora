# Envelope cells: capacity runs

Raw output of the capacity cells of plan Phase E, one file per run. Run on the
reference computer (i9, 24 threads, RTX 3080, Chrome 156) on 2026-11-07. The
fixtures come from `tools/bench_envelope/` and are rebuilt by its scripts.

| Record | What ran | Result |
|---|---|---|
| `mask-ids-2e24-website.json`, `mask-ids-2e32-website.json` | `tests/browser/high_ids.mjs` on `high_ids.py`'s `hi24` (ids either side of 2^24) and `hi32` (ids near 2^32) | All 8 checks pass after 5071d0e0. Before it, label tiles dropped each id's high byte, so 16,777,216 drew as background and 16,777,217 as cell 1. The desktop's server shows the same in `tests/test_high_cell_ids.py`. |
| `table-38m-website.json` | `tests/browser/table_capacity.mjs` on `big_table.py --rows 1200000 --columns 32` (38.4 M values) | No notice. Table ready 2 s after the viewer. Whole table in Python. Heap 2.1 GiB. The gate is right. |
| `table-60m-website-before-glue-fix.json` | the same with 2 M x 30 (60 M values), before the fix | Notice, then "Continue here". Every JSON route answered with column values (Pyodide's glue read addresses past 2 GiB as negative). The viewer never finished. |
| `table-60m-website.json` | the same after `build.py` GLUE_FIXES | The viewer is drawn in 22 s. Whole table in Python (2 M x 31). Heap 3.5 GiB. The gate is right (56,066 cells). |
| `table-80m-website.json` | 2.5 M x 32 (80 M values) | Notice ("about 4.3 GB"), then "Continue here". Polars' allocation fails at the worker's 4 GiB, so Pyodide stops at 4.07 GiB, and the tab says "This tab ran out of memory" with Open in Desktop App / Reload. |
| `heap-2gib-before-glue-fix.json`, `heap-2gib-after-glue-fix.json` | `tests/browser/heap_2g.mjs` (heap pushed past 2 GiB, each crossing at an address above 2^31) | Before: 4 of 4 crossings wrong (`to_py` throws; `assign`, `to_js` and long strings are read from the wrong place). After: all whole. |

What follows for the limits is in `plexora/client/src/envelope.json`'s `table.measured`.
