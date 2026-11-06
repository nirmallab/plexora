# The browser as Plexora's local node: proof-of-concept campaign

Date: 2026-11-05, two rounds. The first round ran P0, P1, P2, P2b, P3 and P5. The second, the same evening, reworked P2b and P3, added P4, P6, P7, P8 and a headed Local Network Access check, and reran everything. Still to do: the remote-site records (O2, ERIS, GCP), and Safari on a Mac.

The plan is `docs/internal/BROWSER_NATIVE_INVESTIGATION.md` §8. The code is `tools/poc_browser_node/` at commit 6083284f, whose `bench/campaign.sh` made every record in this folder except `P5LNAH-S-…` (see §8).

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
| P1 local OME-TIFF | M (66.7 GB, 40 ch) | **GO** | 200/200 tiles byte-identical to the HD path; open 0.06 s; tours reach full 2.3–2.9× sooner than from a local Python node | [P1.md](P1.md) |
| P2 label pyramid | M, L (1.26 M ids) | **GO** | Label tiles identical; GPU gate drag 18.4 ms per drawn update at 1.26 M ids | [P2.md](P2.md) |
| P2b flat mask → pyramid | S, M (9.6 GB raw) | **GO** (round 1: PARTIAL) | Band workers: 9.6 s against Python's 21.8 s (0.44×), identical, no jank; one worker took 58 s | [P2.md](P2.md) |
| P3 cell table | S, M, L (1.26 M × 29) | **GO** (round 1: PARTIAL) | Plain-worker CSV parse: drawable in 0.9 s, 2.7× polars (bound 5×), every answer identical; DuckDB took 3.4 s | [P3.md](P3.md) |
| P4 AnnData / SpatialData | S, L (1.26 M × 90) | **PARTIAL** | Every read identical to h5py / zarr-python / pyarrow (lzf strings apart), but h5wasm is 2–13× h5py (bound 2×) | [P4.md](P4.md) |
| P5 remote node | M | **GO** (one item for O2) | Bytes identical to the desktop route; restart, pairing, isolation, the node-served app; Local Network Access headless and headed | [P5.md](P5.md) |
| P6 against the product | M | **GO** | The real app over local Python vs the browser node, same tour and completion test: tile-fetching steps 2.4–3.3× faster, open 0.3×, half the memory | [P6.md](P6.md) |
| P7 flat image → pyramid | S, M (9.6 GB raw) | **GO** | 27.5 s against Python's 31.2 s (zlib strips), 21.2 s against 178.6 s (uncompressed); identical; reopened in 14 ms | [P7.md](P7.md) |
| P8 Firefox / WebKit | S | **GO** for Firefox; Safari not measured | Firefox 159: every S feature, same picture as Chrome; Windows WebKit: all but OPFS, which that port lacks | [P8.md](P8.md) |

No STOP condition was met. On the ladder (investigation §8.1), P1, P3 and P5 are GO, which meets the gate for the first product change (the Phase 0 seams).

## 3. Set-up (Measured)

- **Machine:** Windows 11 (26200), i9-12900K (24 threads), 128 GB, RTX 3080, NVMe. Before each run the whole machine sat at 10–25 % CPU from the user's own browser and services; each record keeps its sample.
- **Browser:** Chrome 156 stable, headless, on the GPU (ANGLE/D3D11). The frame-rate limit was off, so frame intervals are frame cost, except in P6, which runs Chrome as the product's tour runs it. A fresh profile was used for every block, except where a test needs the same profile twice (reopen). P8 used Playwright's own Firefox 159 and WebKit 27.2.
- **Python:** `plexora node serve` from the same checkout, started per block as `tests/node_harness.py` starts one, with windows scanned in the background as in production. It is the reference for every byte compared, and the "local Python" arm of P1 and P2. P6's product arm is a scratch Plexora server on a bench data root. h5py, zarr-python and pyarrow are P4's reference; Python's own `pyramidize_*` conversions are P2b's and P7's.
- **Fixtures** (fingerprints in each record):

| | Image | Mask | Table | Other |
|---|---|---|---|---|
| S | `flat.ome.tif`, 3 × 2048², deflate | exemplar-001 `cell.ome.tif` (P2b) | exemplar-001, 11,201 × 22 | LSP11385 crops written flat 3 ways (P7); 6 small .h5ad and a SpatialData store (P4) |
| M | LSP11385, 66.7 GB, 40 × 52923 × 45381, deflate + predictor 2 | its label pyramid; its level 0 written flat, 9.6 GB raw (P2b) | LSP11385, 202,049 × 50 | 2 channels written flat as zlib strips and uncompressed, 9.6 GB raw (P7) |
| L | — | Orion CRC02, int32, 48460 × 58709 | Orion CRC02, 1,260,592 × 29 | 6 .h5ad of 1.26 M × 90 and a SpatialData store (P4) |

## 4. Findings that change the design (Design, from Measured)

1. **No rendering code has to change.** Four seams were found (P0):
   - the tile-source helpers are private to the bundle;
   - `toFloatColor` lives in `imageViewer.js`;
   - OpenSeadragon infers transparency from a `.png` URL, so a browser tile source must keep the `.png` address;
   - handlers must be registered before items are added.
2. **The browser node needs no decode step on the page.** geotiff.js output, byte-swapped in the worker, is exactly what `glInit.js` uploads. A uint32 label tile *is* the RGBA8 layout `labelGpu.js` reads.
3. **geotiff.js's lazy IFD parse removes the feared cold-open cost:** 60 ms locally and 7.0 s for a 73 GB file in S3 (P1, P1R).
4. **Tables: plain workers and a raw column store, not DuckDB and not Parquet.** Single-threaded wasm spends about 8 s encoding Parquet for 1.26 M × 29 doubles (round 1), and 2.7 s parsing the CSV; several DuckDB instances trade that for their start-up and heaps (2.84 GB). Plain workers parsing with V8's `Number()` are bit-identical to DuckDB's parse and reach drawable in 0.9 s at 1.09 GB (P3, P3b).
5. **DuckDB-Wasm's Parquet reader is downloaded** from `extensions.duckdb.org` on first use (P4). A browser node that should work offline uses hyparquet (identical to pyarrow), and keeps DuckDB for quoted CSVs, served by Plexora if ever needed.
6. **Flat masks and flat images become pyramids in the tab, faster than Python.** Band workers, with Python's own arithmetic, build a 9.6 GB-raw mask in 0.44× Python's time and a 9.6 GB-raw image in 0.12–0.88×, depending on how it was written; every tile is identical (P2b, P7).
7. **Bytes go straight into Compression/DecompressionStream.** Wrapping them in a Blob ships them to the browser process and back; at gigabytes in flight the blob store refuses ("Failed to fetch"), and per tile it costs about 1 ms of inflate (P1, P7). The product's HD tile worker uses the Blob form: a note, not a defect.
8. **h5ad in the browser is correct and slow.** h5wasm reads match h5py exactly, at 2–13× its time; reading a column of an uncompressed dense X alone is ~50× (one tiny read per row). One pass copies X into the column store (P4). lzf-compressed string arrays abort h5wasm-plugins' filter.
9. **Node protocol (P5).** Drop two planned additions:
   - the private-network preflight answer: Chrome 156 grants loopback access by a `loopback-network` permission, with no server header;
   - the `Cross-Origin-Resource-Policy` stamp: CORS reads survive COEP `require-corp`.

   Keep pairing, `features`, several origins per launch (127.0.0.1 and localhost are different origins), and `/app/`.
10. **The Local Network Access prompt, seen** (P5LNAH): a public page's first loopback fetch is held while "127.0.0.1:PORT wants to — Access other apps and services on this device — Allow / Block" waits. The pairing page should announce it and retry after it is answered.
11. **Chrome's storage estimate is not its limit.** `navigator.storage.estimate().quota` reads usage + 10 GiB; 14.2 GB wrote without error (P7). The first round's "10.7 GB quota" was this report. No Plexora setting is involved.
12. **A product bug, fixed.** `frag.glsl` rebuilt 16-bit values as `r*255+g`: up to 11 of 255 levels dark in a tight bright window (P0, round 1). Fixed on main (9a2b1912) with a browser test; the second round's P0 probe reads 0 error.

## 5. Thresholds that are now numbers

From investigation §8.3 (fallback modes A–E):

| Variable | Value (this machine, Chrome) | Source |
|---|---|---|
| tile read and decode, local NVMe | p50 20.5 ms, p95 35.5 ms (4 workers) | P1 |
| throughput by workers | 56 / 108 / 180 / 275 / 468 tiles/s at 1 / 2 / 4 / 8 / 24 | P1 |
| `T_build`, flat mask → pyramid | 9.6 s for 9.6 GB raw (12 workers); 15.7 s with 4 | P2b |
| `T_build`, flat image → pyramid | 27.5 s for 9.6 GB raw from zlib strips, 21.2 s uncompressed (12 workers, 1.8 GB peak) | P7 |
| `T_derived_bytes` | Chrome: the browser's own limit (the estimate is not it); Firefox: 10 GiB | P7 |
| cold table ingest | 1.26 M × 29 drawable in 0.9 s, every column in 1.75 s | P3 |
| table working set | 1.09 GB peak at 1.26 M × 29 | P3 |
| h5ad → column store | one pass over X: 1–9 s at 1.26 M × 90 (h5py: 0.5–2.3 s) | P4 |
| `T_read_p95` (slow media) | not measured: NVMe only | — |

## 6. What is open

- **Remote records** (investigation §8.4): O2 (Duo, 640 GB Imaris, NLU290), ERIS and GCP. P5 ran on loopback only; `bench/p5remote.mjs` (`P5R`) runs its protocol check against a node already up, once someone is at the Duo prompt (P5.md).
- **Safari on a Mac:** P8 with WebKit on macOS, which has OPFS.
- **h5ad speed** (P4): accept the one-time copy into the column store, or have a node read large h5ad files.
- **Not tried:** an XL table (19.9 M rows); the h5wasm-plugins LZ4 filter on a real .ims; geotiff.js's JavaScript predictor (P1, about 12 ms of a 20 ms tile).

## 7. Limitations

- **One machine, loopback nodes.** No network sits between page and node except in P1R (S3).
- **P6's browser arm is the POC's minimal viewer**, not the static build of the whole app. The product's open includes its page boot; the browser arm's page load is added to its open in the verdict (P6.md).
- **The machine was not idle** (10–25 % CPU). Timing comparisons are paired ABBA blocks, or Python's median of 3, never single runs.
- **Coverage is limited.** Codecs: deflate with and without predictor 2, zlib strips, uncompressed strips; gzip and lzf in HDF5; zstd in zarr v3. GPU paths were tested on one NVIDIA card.

## 8. Deviations from the plan

- **Fixtures.**
  - M is LSP11385, already on this machine, rather than an Orion sample pulled to NVMe. The Orion CRC02 mask and table (pulled from S3) are L for P2 and P3, and the source of P4's 1.26 M-cell h5ad.
  - P2b's M is LSP11385's mask written flat by `fixtures.py`; P7's are its channels written flat (`fixtures.py flat-image`).
  - P4's SpatialData store is written by spatialdata 0.8 from LSP11385 crops, not a Xenium output.
- **P1's viewer comparison** uses the static page's own 11-step seeded tour, for both arms. P6 then runs `tools/bench_remote/tour.mjs` against the product page.
- **P3 changed design twice,** from Parquet to the column store (round 1), and from DuckDB to plain workers (round 2). The superseded designs keep labelled records (`-parquet`, `-duckdb`, `-duckdb-par4`).
- **P5 changes:**
  - (b) ran without mkcert: a self-signed certificate, with certificate errors ignored, and `--ip-address-space-overrides` to make the page public; it ran headless in the campaign, and headed once by hand (`P5LNAH-S-…`, made at `9a2b1912-dirty`, the same handler as in 6083284f, which opens a visible window and so is left out of the campaign unless `HEADED=1`);
  - (c), an https node behind a TLS proxy, was not run.
- **P8** ran Playwright's WebKit build for Windows in place of Safari; its storage results say nothing about Safari.
- **Record metadata.** The driver names records by the local date. Most records carry commit `6083284f-dirty`: during the campaign the uncommitted files were the edits to `bench/verdict.py` and the new `bench/p5remote.mjs`, neither on a measured path; both are in the next commit.
- **The campaign hung after P8**, its record written: P8's WebKit runs of the storage features threw before their handlers closed the browser, and the leftover browser kept the driver alive. The driver now exits once its record is written. `p0_audit` (the campaign's last step) was then run by hand.

## 9. Files

| File | What |
|---|---|
| `P0-{S,M}-2026-11-05.json`, `p0_audit-2026-11-05.json` | P0 records; the static audit of every client script |
| `P1-M-…`, `P1V-M-…`, `P1R-none-…` | P1: tiles, viewer arms, bucket |
| `P2-{M,L}-…`, `P2B-{S,M}-…`, `P2B-M-…-{w4,w8}` | P2, P2b and its worker sweep |
| `P3-{S,M,L}-…`, `P3-L-…-{duckdb,duckdb-par4}`, `P3-{M,L}-…-parquet`, `P3B-L-…` | P3, the superseded parsers and store, the parser comparison |
| `P4-{S,L}-…` | P4 |
| `P5-M-…`, `P5LNA-S-…`, `P5LNAH-S-…`, `P5LNAH-prompt-chrome156.png` | P5 (a, d–h), P5 (b) headless and headed |
| `P6-M-…` | P6 |
| `P7-{S,M}-…`, `P7-M-…-w8` | P7 |
| `P8-S-…` | P8 |
| `P0.md` … `P8.md` | one report per POC |

To reproduce: `cd tools/poc_browser_node && npm ci && bash bench/campaign.sh` (about 1.5 hours on this machine), then `python bench/verdict.py ../../docs/internal/bench/browser_node --write`.
