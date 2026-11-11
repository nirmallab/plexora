# First view of an image: website vs local, and the plan to make it instant (2026-11-10)

Reported: on a first visit "Starting" took a long time; `1_750.ome.tif` from the file server took
several minutes to open on the website; the same file from Downloads was faster but not seconds.

Goal set: **when a user gives Plexora an image, the first channel's lowest resolution is on screen
at once** -- that view needs a few hundred KB of the file, nothing else.

## How it was measured

- A brand-new Chrome profile for every run (no cache, history, site data or storage; Chrome 157,
  a visible window), driven as a person would: the website loaded, the file **dragged onto the home
  page** (a real browser-level drop, so Plexora gets a real file handle), the first-use questions
  answered ("I'm new", "Use browser storage", "Continue to my image"); on the local server the path
  pasted and **Load** pressed. Live site 0.0.49; local server = this checkout (`main` at `24a9d03c`)
  from `uv sync --frozen`, Python 3.13, a fresh server and empty data folder per run.
- "On screen" = the viewer area lit in a screenshot; local-server rows also give when the first tiles
  came back and were drawn, since its tiles are drawn dim until the contrast arrives.
- Files: `1_750.ome.tif` on `Z:` (`\\research.files.med.harvard.edu\hits`) and its copy in
  Downloads; for the file-type check `LSP26261_..._001668.ome.tiff` on the same share and a local copy.

| | `1_750.ome.tif` | `LSP26261_..._001668.ome.tiff` |
|---|---|---|
| Size | 105.9 GB | 14.0 GB |
| Channels, type | 40 x uint16 | 19 x uint16 |
| Compression | none | LZW |
| Pyramid | 7 levels **as 240 extra pages in the main chain** | 5 levels as SubIFDs (standard OME) |
| Page headers (IFDs) | 280, **spread over the whole 106 GB** (each after its plane's data) | 19, packed in the first 0.1 MB |
| Lowest level | 576 x 404 (one tile) | 710 x 535 (one tile) |

tifffile (`is_ome=False`, as Plexora opens it) finds all 7 levels of `1_750`; CPython opens it from
the share in 0.55 s. The layout is valid -- it is the website's read path that it breaks.

## Results

Drop (website) or Load (local) -> first channel on screen. Website times exclude the 2.5-2.6 s my
automated clicks spent in the first-use questions.

| | Website, fresh browser | Local server: tiles drawn / contrast applied |
|---|---|---|
| `1_750`, Downloads | **23.1 s** | 3.2 s / 5.1 s |
| `1_750`, share | **never in 15 min** -- `/import/inspect` alone 642 s; chip "Disconnected" from 5 min 20 s | 4.5 s / 22.6 s |
| LSP26261, local disk | 4.4 s | 3.0 s / 3.4 s |
| LSP26261, share | 15.0 s | 3.2 s / 7.8 s |

Before any of that, a first visit: home visible 1.4-2.7 s, **Python ready 9.0-10.2 s** ("Starting"),
then three first-use screens on the first drop.

Website, `1_750` from Downloads, where the 23 s went: `/import/inspect` 8.2 s, `/import/sample`
7.9 s (in CPython the two are 0.2 s and 0.9 s), a full page load to `/1_750`, then every first
request of the viewer 3.4 s behind the project's load, then tiles. Local server, same file: inspect
0.2 s, sample 0.9 s, viewer page 1.2 s, first tiles back 2.4 s; the contrast waits for
`/get_image_channel_stats` (2.6 s local, **18.7 s from the share**) and `/get_channel_gmm` (1.7 s).

## Why

1. **The first view waits for the whole import.** inspect -> sample (which runs inspect again) ->
   register -> full navigation -> project load (every channel's overview) -> viewer requests ->
   tiles. Every step is Python's, and on the website Python must have started first.
2. **The website's reads of a picked file are 1 MiB blocks, 64 kept** (`workers/pythonWorker.js`
   `BLOCK`, `KEEP`). Each of `1_750`'s 280 headers (a few hundred bytes) is its own block, so one
   walk is 280 x 1 MiB, more than the cache holds; the next open walks again. The import opens the
   file ~6 times in inspect and again in sample (CPython profile: 6 `TiffFile` opens, 1,130 page
   reads for inspect alone). On the share each 1 MiB read through the handle is ~0.25 s, one after
   another: 280 x 0.25 s x 6+ = the 10 minutes.
3. **Reads through a file handle share Chrome's queue with the site's storage.** Six readers of
   `1_750` on the share through `handle.getFile()`: storage calls went from 7 ms to 265-279 ms
   typical, worst 3.6-5.4 s, while reading (milder, 78/286 ms, on a faster spell). The same file
   through a plain `File` (a file input, or `dataTransfer.files` of the same drop): storage
   untouched (7 ms, worst 23-24 ms). A whole viewer reading from the share stalled the user's
   storage 15-55 s at a time -- and Plexora's start waits on storage (`browserState`'s write check),
   which is what made "Starting" long with a share-backed tab open.
4. **Long Python work starves the page.** During the 10-minute inspect the tab's Python answered
   nothing; `/health` failed and the chip said Disconnected.
5. **The local server's contrast** needs the exact window, a full-resolution scan: 18.7 s from the
   share. Its tiles are on screen at 4.5 s, dim.
6. Smaller: Python's start 9-10 s on a cold visit here (`.wasm`/`.whl` still `cf-cache-status:
   DYNAMIC` at the CDN, WEBSITE_NEEDS_YOU.md section 2); first-use questions before the first image;
   a drop from a folder Chrome refuses (e.g. under AppData) does nothing and says nothing.

`1_750`'s headers are perfectly regular (each plane's header a fixed distance after the previous:
1,962,949,604 bytes at full resolution, 490,737,608 at half, ...), so in an uncompressed file of
this kind every header's offset can be computed from the first two and checked with one read.

## Plan

Targets: drop/pick -> first channel's lowest level on screen **<= 1 s from local disk, <= 2 s from
a share, any pyramidal TIFF layout, on a first visit too** (not waiting for Python, the import or
the first-use questions); the full viewer within today's local-server times.

### Phase 1 -- the instant preview (the page alone, no Python)

1. **Preview reader in the page** (a tiffWorker, `node/tiffReader.js` reused): from the dropped or
   picked File read the header and IFD 0 (OME-XML: channel count), find channel 0's smallest level --
   SubIFDs, or a main-chain pyramid -- read its tile(s), decode (none/LZW/deflate/zstd/JPEG already
   in the page), window it from its own pixels (0.5-99.5 percentile), draw. A few reads, ~1 MB.
   - Main-chain pyramids: uncompressed -> compute the level's IFD offset from IFD 0 and IFD 1 and
     verify it; otherwise walk with 4-64 KiB reads, never 1 MiB.
   - Remember the IFD index (name, size, lastModified) in IndexedDB: a re-open walks nothing.
2. **Show it at once in the viewer shell** -- a single-image layer, the channel name, a progress
   line for the import -- and start the import behind it. When the project is registered, the
   viewer becomes the full one in place (the kept-answers swap, no full navigation, the preview
   tile staying until real tiles cover it).
3. **Nothing in front of it**: the first-use storage question moves to the first save (or after
   the first view); the drop does not wait for Python's start (the preview needs none; the import
   is queued for when Python is up).

### Phase 2 -- an import that reads the file once

4. One `TiffFile` per import, shared by inspect, sample, registration and the first load (the
   `tiff_series.open_channel_series` cache extended to `import_proposal`, `brightfield`,
   `physical_metadata`, `_focal_note`, channel names), keyed by path, size and mtime.
5. `/import/sample` takes inspect's proposal (a token) instead of running inspect again.
6. No label-image pixel probe for an OME file with SizeC > 1; OME-XML parsed once.
7. The viewer's first requests do not wait for every channel's overview: open the image, answer
   `/config`, `/get_channel_names`, the spec; overview, stats and fits in the background.

### Phase 3 -- the website's file reads

8. Small reads for TIFF structure: 64 KiB pieces for IFDs and tile tables, 1 MiB blocks only for
   pixel data; IFD pieces kept separately from pixel blocks; `held_ifds` (or the page's IFD index
   from step 1) handed to every open, so Python never walks what the page already walked.
9. Read a dropped file through its plain `File` (no storage stalls); keep the handle only to find
   the file again next visit. For handle reads on a share, fewer reads in flight.
10. The import as a job: progress the page shows ("reading the file's structure, 120 of 280"),
    `/health` answered meanwhile, cancellable -- never "Disconnected".
11. Start-up: the storage write check off the critical path (time-boxed, or after the first page).
    The CDN cache rule for `.wasm`/`.whl` (dashboard). A refused drop says why.

### Phase 4 -- the local server's first contrast

12. Tiles at a provisional window from the overview at once (as the website does); the exact
    full-resolution scan and the fit in the background.

### Proving it

- Turn this harness into `tools/bench_web/bench_first_view.mjs` (fresh profile, a real drop, the
  share and local copies of both layouts, both front ends) and record before/after here.
- Pass when every row above meets the targets, and the tile, gating and contrast numbers of
  WEBSITE_PERF_2026-11-09.md do not regress.

Order: Phase 1 is the user-visible fix on its own (steps 1-3); Phase 2 and step 8 make everything
after the preview fast and stop the multi-minute imports; steps 9-12 are independent and small.

## Done (branch `feature/instant-first-view`)

What changed, by the plan's steps:

- **1-3, the first view** (`services/firstView.js`, loaded by the host page so it outlives the frame's
  way to the viewer). A drop on the home page shows channel 0's smallest level in the drop's own turn:
  the page reads the header, the directories (SubIFDs; a main-chain pyramid level by level, each level's
  first directory computed from the stride of the one above and read to be sure; walked one by one
  where that does not hold), and that level's tile -- 10 reads for LSP26261, 61 for `1_750` -- decodes
  it (none, LZW, deflate, predictor 2), windows it from its own pixels and draws it where the viewer
  will be. It fades when the viewer has drawn the image, the person reaches for it, or the import asks
  a question. The same pixels as tifffile, byte for byte (tests/test_first_view.py, both layouts, 8-
  and 16-bit). The local app's home page shows `/import/preview` (Python's read of the path, 0.2-0.5 s).
  The first-use question no longer stands in front of the first image: browser storage is made ready as
  its button makes it and the viewer offers the choice once, after (views/webOnboarding.js `quiet`).
- **4-7, an import that reads the file once.** `/import/sample` takes the proposal the inspect a moment
  before made of the same unchanged files (`import_proposal.inspected`: keyed by path, size and mtime,
  taken once, dropped at every registration). On the website every reader of a picked TIFF shares one
  open (`tiff_region.open_tiff`/`close_tiff`, over `tiff_series.open_channel_series`). A file that says
  it is three channels or more of one plane is a panel without a pixel read. The overview's level is read
  by the page's workers side by side (`providers.local._page_overview`).
- **8-10, the website's reads.** A small read that misses (a directory, a tile table) reads the 64 KiB
  piece around it, kept (1,024 at most), not a 1 MiB block; the page hands the directories it found to
  the worker before Python opens the file (`PlexoraPython.prefetch`). A drop is read through its own
  File, not its handle (no stall of the site's storage); a handle Chrome refuses still opens. A `/health`
  poll behind a long request is Python at work, not "Disconnected".
- **12, and what follows the open.** The exact window (a whole full-resolution plane) is made only for
  the channels shown, on both front ends; a channel switched on later is drawn at once with its
  provisional window, and its scan is not urgent. The local app answers a viewer's provisional ask at
  once and makes the exact window on a thread of its own (`data_model._window_behind`). A background
  scan reads at most ~64 MB a round (was ~640 MB), so a zoom's tiles never wait long behind it.

Not done, and why: the start-up's storage write check was left as it is -- when Chrome's storage queue
stalls, every storage call stalls with it, so time-boxing one does not shorten "Starting"; the cause (a
share's file read through its handle) is gone for drops. A file chosen with Browse is still read through
its handle, which is what remembers it for the next visit. The CDN cache rule is the dashboard's
(WEBSITE_NEEDS_YOU.md section 2).

### Results

The same harness, both arms on this machine: the website's arms are main (`24a9d03c`) and this branch,
each served from loopback with its own bundle (the live 0.0.49 bundle; it with this branch's Plexora
wheel); the local arms a fresh server and data folder each. Seconds from the drop (website, the first-use
questions' clicks taken out) or Load (local). "On screen": the image visible to a person (the first view,
or the viewer lit). Interactions: until the viewer has stopped and every channel shown is drawn (zoom and
pan include OpenSeadragon's ~1 s animation).

| Website | On screen | Viewer drawn | Zoom in | +3 channels | Full-res zoom | Pan |
|---|---|---|---|---|---|---|
| `1_750` Downloads, before | 18.6 | 19.4 | 1.2 | 0.5 | 1.2 | 1.2 |
| `1_750` Downloads, after | **0.3** | **5.0** | 1.2 | 0.4 | 1.1 | 1.2 |
| `1_750` share, before | 319.0 | 319.8 | (after 5 min) 1.2 | 1.0 | 1.4 | 1.4 |
| `1_750` share, after (2 runs) | **0.57 / 0.55** | **9.2 / 9.0** | 1.2 / 1.2 | 2.6 / 1.6 | 3.4 / 2.9 | 2.0 / 2.1 |
| LSP26261 local, before | 4.1 | 5.1 | 1.0 | 0.4 | 2.0 | 1.2 |
| LSP26261 local, after | **0.3** | **3.8** | 1.1 | 0.3 | 1.1 | 1.3 |
| LSP26261 share, before (2 runs) | 4.4 / 4.4 | 5.2 / 5.3 | 4.7 / 4.8 | 2.1 / 2.1 | 10.1 / 9.9 | 1.3 / 1.2 |
| LSP26261 share, after (2 runs) | **0.35 / 0.33** | **4.1 / 4.3** | **1.2 / 1.2** | **0.8 / 0.4** | **2.7 / 1.9** | 1.3 / 1.3 |

| Local server | On screen | Viewer drawn | Zoom in | +3 channels | Full-res zoom | Pan |
|---|---|---|---|---|---|---|
| `1_750` Downloads, before | 5.5 | 4.2 | 1.2 | 1.1 | 1.8 | 1.4 |
| `1_750` Downloads, after | **0.3** | 3.8 | 1.2 | 0.8 | 2.5 | 1.5 |
| `1_750` share, before | 22.1 | 5.4 | 1.5 | 8.8 | 26.4 | 5.5 |
| `1_750` share, after | **0.5** | 5.3 | 1.6 | 7.1 | **7.6** | 4.0 |
| LSP26261 local, before | 3.3 | 3.5 | 1.1 | 0.4 | 1.3 | 1.2 |
| LSP26261 local, after | **0.3** | 3.5 | 1.2 | 0.9 | 1.5 | 1.2 |
| LSP26261 share, before | 7.7 | 3.6 | 1.3 | 3.4 | 4.2 | 1.6 |
| LSP26261 share, after | **0.3** | 3.6 | 1.2 | 2.9 | 3.0 | 1.6 |

The website's import of `1_750` from the share: `/import/inspect` 131.8 s and `/import/sample` 113.7 s
before (642 s for the inspect alone in the first run, on a slower spell of the share), 1.1 s and 1.0 s
after. Python's start on a first visit is unchanged (6.3-6.8 s here, served from loopback).

Rerun: `tools/bench_web/bench_first_view.mjs` (README), against two static builds served by
`tools/browser_build/serve.py` (`--site`, `--py`), the after arm's bundle made with `build.py
--plexora-only --no-kernels` over a copy of the released one.
