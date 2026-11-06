# The browser as Plexora's local node: first proof-of-concept campaign

Date: 2026-11-05. Status: P0, P1, P2, P2b, P3 and P5 done on one Windows machine. The remote-site records (O2, ERIS, GCP) and P4, P6, P7 and P8 are still to do.

The plan is `docs/internal/BROWSER_NATIVE_INVESTIGATION.md` §8. The code is `tools/poc_browser_node/`, at commit 4e18de00 plus `bench/campaign.sh`, which ran it.

Claims are tagged as in the investigation:
- **Measured**: a number from this folder's records.
- **Design**: read from the source.
- **Predicted**: an estimate, not measured here.

Every number below comes from a JSON record in this folder. `python tools/poc_browser_node/bench/verdict.py docs/internal/bench/browser_node` prints each threshold with the value beside it.

## 1. Question

Can a page served as static files be Plexora's local node? It would have to read the user's files, draw them with the product's renderer and answer the viewer's table questions, and use the existing remote data nodes as peers. Python would not be required on the user's machine, and the result must be the same pixels and numbers the Python modes produce.

## 2. Verdicts

| POC | Fixture | Verdict | In one line | Report |
|---|---|---|---|---|
| P0 rendering layer | S, M | **GO** | The product's rendering modules run unmodified from a static page; node-fed and browser-fed canvases are identical | [P0.md](P0.md) |
| P1 local OME-TIFF | M (66.7 GB, 40 ch) | **GO** | 200/200 tiles byte-identical to the HD path; open 0.1 s; tours reach full 2.2–2.6× sooner than from a local Python node | [P1.md](P1.md) |
| P2 label pyramid | M, L (1.26 M ids) | **GO** | Label tiles identical; GPU gate drag 18.6 ms per drawn update at 1.26 M ids | [P2.md](P2.md) |
| P2b flat mask → pyramid | S, M (9.6 GB raw) | **PARTIAL** | 58 s in a worker, identical to Python's, no jank; 2.7× Python's time (hypothesis: 2×) | [P2.md](P2.md) |
| P3 cell table | S, M, L (1.26 M × 29) | **PARTIAL** | Every answer identical; reopen 31 ms; first-ever CSV open 5–10× polars (bound: 5×) | [P3.md](P3.md) |
| P5 remote node | M | **GO** (one item for O2) | Bytes identical to the desktop route; restart, pairing, isolation, the node-served app and Local Network Access all work | [P5.md](P5.md) |

No STOP condition was met. On the ladder (investigation §8.1), P1 and P5 are GO and P3 is PARTIAL. The rule "no product change until P1, P3 and P5 are GO" is therefore not yet met. One P3 threshold, the cold CSV ingest ratio, is open, and the decision on it is the user's (§6).

## 3. Set-up (Measured)

- **Machine:** Windows 11 (26200), i9-12900K (24 threads), 128 GB, RTX 3080, NVMe. Before each run the whole machine sat at 10–25 % CPU from the user's own browser and services; each record keeps its sample.
- **Browser:** Chrome 156 stable, headless, on the GPU (ANGLE/D3D11). The frame-rate limit was off, so frame intervals are frame cost. A fresh profile was used for every block, except where a test needs the same profile twice (reopen).
- **Python:** `plexora node serve` from the same checkout, started per block as `tests/node_harness.py` starts one, with windows scanned in the background as in production. It is the reference for every byte compared, and the "local Python" arm.
- **Fixtures** (fingerprints in each record):

| | Image | Mask | Table |
|---|---|---|---|
| S | `flat.ome.tif`, 3 × 2048², deflate | exemplar-001 `cell.ome.tif` (P2b) | exemplar-001, 11,201 × 22 |
| M | LSP11385, 66.7 GB, 40 × 52923 × 45381, deflate + predictor 2 | its label pyramid; its level 0 written flat, 9.6 GB raw (P2b) | LSP11385, 202,049 × 50 |
| L | — | Orion CRC02, int32, 48460 × 58709 | Orion CRC02, 1,260,592 × 29 |

## 4. Findings that change the design (Design, from Measured)

1. **No rendering code has to change.** Four seams were found (P0):
   - the tile-source helpers are private to the bundle;
   - `toFloatColor` lives in `imageViewer.js`;
   - OpenSeadragon infers transparency from a `.png` URL, so a browser tile source must keep the `.png` address;
   - handlers must be registered before items are added.
2. **The browser node needs no decode step on the page.** geotiff.js output, byte-swapped in the worker, is exactly what `glInit.js` uploads. A uint32 label tile *is* the RGBA8 layout `labelGpu.js` reads.
3. **geotiff.js's lazy IFD parse removes the feared cold-open cost:** 73 ms locally and 6.9 s for a 73 GB file in S3 (P1, P1R). The investigation's §9 question 1 is answered.
4. **Tables: a raw column store in OPFS, not Parquet.** Single-threaded wasm spends about 8 s encoding Parquet for 1.26 M × 29 doubles, whatever the codec. A raw column costs only its write, and reopens in 31 ms without DuckDB (P3, P3b).
5. **A whole-slide flat mask can be pyramidised in a tab.** Streaming peaks at 1.09 GB, at 57 fps, with no long tasks. Only derived levels are kept, deflated: 20 MB for a 9.6 GB mask (P2b). The prediction that it is "beyond a browser tab" was wrong.
6. **Node protocol (P5).** Drop two planned additions:
   - the private-network preflight answer: Chrome 156 grants loopback access by a `loopback-network` permission, with no server header;
   - the `Cross-Origin-Resource-Policy` stamp: CORS reads survive COEP `require-corp`.

   Keep pairing, `features`, several origins per launch (127.0.0.1 and localhost are different origins), and `/app/`.
7. **An existing product bug.** `frag.glsl:504` rebuilds 16-bit values as `r*255+g`. A bright channel levelled tightly draws up to 11 of 255 levels dark (P0). It is independent of this work.
8. **A storage surprise.** OPFS quota in a fresh Chrome profile on this machine was about 10.7 GB, not 60 % of the disk. Derived data must be compressed or bounded (P2b).

## 5. Thresholds that are now numbers

From investigation §8.3 (fallback modes A–E):

| Variable | Value (this machine, Chrome) | Source |
|---|---|---|
| tile read and decode, local NVMe | p50 21 ms, p95 31 ms (4 workers) | P1 |
| throughput by workers | 48 / 97 / 149 / 223 / 376 tiles/s at 1 / 2 / 4 / 8 / 24 | P1 |
| `T_build` (flat mask → pyramid) | about 6 s per raw GB, one worker | P2b |
| `T_derived_bytes` | about 10.7 GB origin quota (fresh profile) | P2b, P3 |
| cold table ingest | about 2.1 µs per row to parse (29 columns); 1.26 M rows drawable in 3.4 s | P3 |
| table working set | 1.67 GB peak at 1.26 M × 29 | P3 |
| `T_rows×cols_max` | not reached: 36.6 M values used 1.67 GB. Predicted near 80 M before 4 GB | P3 (Predicted) |
| `T_read_p95` (slow media) | not measured: NVMe only | — |

## 6. What is open

- **P3's cold-ingest ratio** (5.0× on M, 9.9× on L, against ≤ 5×). The options are:
  - accept a one-time 3–4 s at 1.26 M cells;
  - parallelise ingest with cross-origin isolation (Phase 4; P5(d) shows nodes survive it);
  - move CSV ingest to "prepare first" above a size.
- **Remote records** (investigation §8.4): O2 (Duo, 640 GB Imaris, NLU290), ERIS and GCP. P5 ran on loopback only.
- **Headed checks:**
  - the Local Network Access prompt as a user sees it;
  - the quota in an ordinary user profile.
- **Not run in this round:**
  - P4 (AnnData/SpatialData);
  - P6 (the product page against the static page, the same bytes);
  - P7 (OPFS persistence across browsers; the reopens here are Chrome only);
  - P8 (Firefox, Safari).
- **Not tried, for speed:** reading level 0 in several workers (P2b); geotiff.js's JavaScript predictor (P1, about 12 ms of a 21 ms tile).

## 7. Limitations

- **One machine, one browser, loopback nodes.** No network sits between page and node except in P1R (S3).
- **The local-Python arm is a node.** The desktop's local path serves the same tile bytes from the same code (`encode_tile_array`). The product page itself was not timed against the static page (P6).
- **The machine was not idle** (10–25 % CPU). Comparisons are paired ABBA blocks, never single runs.
- **Coverage is limited.** Codecs tested were deflate with and without predictor 2. GPU paths were tested on one NVIDIA card.

## 8. Deviations from the plan

- **Fixtures.**
  - M is LSP11385, already on this machine, rather than an Orion sample pulled to NVMe. The Orion CRC02 mask and table (pulled from S3) are L for P2 and P3.
  - P2b's M is LSP11385's mask written flat by `fixtures.py`.
- **P1's viewer comparison** uses the static page's own 11-step seeded tour, for both arms, instead of `tools/bench_remote/tour.mjs`, which drives the product page.
- **P3 changed design mid-campaign,** from Parquet to the column store, when P3b showed why. Both records are kept (`-parquet`).
- **P5 changes:**
  - (b) ran without mkcert: a self-signed certificate, with certificate errors ignored, and `--ip-address-space-overrides` to make the page public;
  - (c), an https node behind a TLS proxy, was not run.
- **Record metadata.**
  - Records carry commit `4e18de00-dirty`; the only file outside that commit was `bench/campaign.sh`, committed beside the records.
  - The driver first named records by UTC date. They were renamed to the local date, the evening of 2026-11-05.

## 9. Files

| File | What |
|---|---|
| `P0-{S,M}-2026-11-05.json`, `p0_audit-2026-11-05.json` | P0 records; the static audit of every client script |
| `P1-M-…`, `P1V-M-…`, `P1R-none-…` | P1: tiles, viewer arms, bucket |
| `P2-{M,L}-…`, `P2B-{S,M}-…` | P2, P2b |
| `P3-{S,M,L}-…`, `P3-{M,L}-…-parquet`, `P3B-L-…` | P3, the Parquet design, ingest breakdown |
| `P5-M-…`, `P5LNA-S-…` | P5 (a, d–h), P5 (b) |
| `P0.md` … `P5.md` | one report per POC |

To reproduce: `cd tools/poc_browser_node && npm ci && bash bench/campaign.sh` (about 20 minutes on this machine), then `python bench/verdict.py ../../docs/internal/bench/browser_node --write`.
