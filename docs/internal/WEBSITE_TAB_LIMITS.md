# What a browser tab holds: large data on the website

Date: 2026-11-11. Branch `fix/website-big-remote`.

## The case

A 349 GB Imaris file was opened on the website from the HMS O2 transfer node.
It was opened over a files-only SFTP server as
`sftp://…/Dataset1-LSP13626-melanoma-in-situ.ims`. The file has 70 channels and
194 planes of 10,908 × 5,508 uint16, gzip, in 7 levels. The website's Python
(Pyodide) died, and the page then filled the console with 500s.

## Why it died

Measured by running the app's own routes in desktop CPython against the real
file, over one SFTP connection.

| Step | Time | Fetched | Notes |
|---|---|---|---|
| `/import/inspect` | 740 s | 2.21 GB | the import's mask guess reads a level-0 window of all 70 channels |
| first `/get_image_channel_stats` | 112 s | 2.34 GB | the exact window scans the level-2 stack, 1.78 GB per channel; process peak 4.48 GB |

What 0.0.52 named when it died the same way (its fatal hook, below): **not
memory.** "SuspendError: trying to suspend JS frames", heap 0.26 GiB.
- On the website Python waits on the network by suspending (JSPI). h5py reads
  a remote .ims through Python's file object, called from HDF5's C code, and a
  suspension with JavaScript frames under it ends Pyodide for good.
- Reproduced in the live worker (2026-11-12): h5py reading a file object
  whose `readinto` waits on a promise.
- Behind it, a second wall: h5py's file-object driver addresses at most
  `SIZE_MAX - 1` bytes, 4 GiB in 32-bit WebAssembly. With the reads fixed the
  import stopped at "Unable to get link info (attempting I/O in temporary
  file space)", the first group past 4 GiB.

The measured costs above were real too, and the limits below still apply:
- the first channel's stats peaked at 4.48 GB in CPython with desktop
  budgets;
- an exact scan per channel held decoded chunks in caches sized for a
  desktop.

The remote chunk cache is not a suspect. On the website it lives in the site's
storage (OPFS, `plexora/browser/cachefiles.py`, since 2026-11-08), and only its
index is in memory.

## What changed

- **Website limits** (`plexora/browser/boot.py` `WEBSITE_LIMITS`), set through
  the variables each module already reads, so the desktop is unchanged:

  | Limit | Desktop | Website |
  |---|---|---|
  | Volume chunk cache | up to 8 GB | 256 MB |
  | HDF5 chunk cache per .ims dataset | 96 MB | 24 MB |
  | 3-D level cache | 512 MB | 128 MB |
  | Bricks copy of a stack | on | off |

- **The import's mask guess** (`ims.mask_verdict`, `GUESS_CHANNELS = 4`).
  - In a file of more than 4 channels, only channels named as a mask are
    read.
  - Saying "this is a mask" still searches every channel (`label_channel`).
- **Stated windows** (`ims.ImsVolumeSource.stated_ceiling`,
  `windows.stated_window`).
  - An Imaris file's saved `HistogramMax` is the exact window, with no scan.
  - It is believed only if no voxel of the coarsest level is brighter.
- **Exact chunk spans** (`RemoteFile.pread_many(exact=True)`).
  - Imaris chunks read by offset are fetched as themselves.
  - Before, each came as the 2 MB grid blocks around it: 4.4× the bytes on
    this file.
- **Refusals instead of crashes** (`server/utils/tab_room.py`). Each build
  below is refused before it reads a byte when it cannot fit, with a 507 and a
  notice offering the desktop app:

  | Build | Checks |
  |---|---|
  | A mask's Python-built label pyramid | its output, under 512 MB |
  | Missing 2-D pyramid levels | their output, under 512 MB |
  | Missing z-stack levels | their output, and the plane read whole (1 GB) |
  | 3-D label volumes | their output, and the plane read whole (1 GB) |
  | Python-built image pyramids | their output, under 512 MB |

  Also on the website:
  - whole-mask reads stream past 256 MB;
  - the overview caches keep 2 entries, not 8.
- **A remote .ims's reads never suspend** (`server/utils/no_suspend.py`).
  Inside `held()` coroutines run on the boot's SyncLoop, requests are
  synchronous XMLHttpRequests (the service worker relays them like `fetch`,
  a files-only server's included), a question to the page goes over shared
  memory, and no stage is loaded.
- **A remote .ims is a path** (`server/utils/fs_mount.py`,
  `browser/remote_mount.py`, the worker's `plexoraMountReader`). h5py opens a
  file in Pyodide's filesystem whose reads call `RemoteFile` and its cache, so
  HDF5's own driver addresses 64 bits. The desktop keeps the file object.
- **When Python dies anyway** (`pythonWorker.js`, `pythonClient.js`,
  `backend.js`, `appStatus.js`, `agentBridge.js`):
  - the worker records the real cause through Pyodide's `on_fatal` hook;
  - it stops its step loop;
  - every request gets a 503 from the page at once;
  - the health and agent polls stand down;
  - the notice names the cause.

## After, the same file

Same measurement, with the website's limits. The desktop's remote cache was
held to 256 MB here, which can only add re-reads.

| Step | Before | After |
|---|---|---|
| `/import/inspect` | 740 s, 2.21 GB | 3.1 s, 4 MB |
| first channel's stats | 112 s, 2.34 GB, peak 4.48 GB | 11.4 s, exact window 34199, process peak 0.96 GB |
| next channels | a ~2 min background scan each | 0.1–0.4 s |
| HD tile, level 2 / level 4 | — | 2.5 s / 0.5 s |
| 3-D level (level 4) | — | 0.5–0.8 s |

The first view's remaining cost is the overview of all 70 channels at level
4: about 120 MB of chunks. Its reads ask for about 3× those bytes, which is not
explained yet.

## In a real browser, the real file (2026-11-12)

Chrome, the fixed build served here, the 349 GB file over one SFTP connection
to O2's transfer node (one Duo push) served as HTTP ranges on 127.0.0.1 -- the
same h5py path as the page's own SFTP reads. Like a user: paste the address,
Load, turn channels on, zoom, 3-D.

| Step | Result |
|---|---|
| Load | the viewer, Hoechst drawn; 413 MB read |
| a second channel (5'hmC) | drawn; heap 1.03 GB |
| zoom to 50 µm | sharp; heap unchanged |
| the stack in 3-D | rendered, two channels; heap 1.51 GB |
| a third channel (MART1), one plane | drawn; heap 1.51 GB |

Python never stopped; 1.17 GB was read in all, at about 6.9 MB/s, and
"Waiting on the network" said so. The first view's reads (413 MB for the
overview) are the open item below.

## Not done

- **Derived files Python writes still go to memory**, up to the 512 MB budget.
  An OPFS-backed filesystem for `/data`, using sync access handles in the
  worker, would lift that. The page's own pyramid builds already write to OPFS.
- **A zoomed-out 2-D tile of a single-level, very large image** still reads
  whatever level exists. Its missing levels are now refused on the website
  rather than built.
- **`quantization_window_of`'s other-shapes branch** (`data_model.py`) still
  reads a whole plane in one call.
- **The page's own SFTP path with the real file** was not driven (it needs
  the O2 password typed in the page). Its reads go through the same switch,
  and the service worker was shown to relay a synchronous request to it.

## When to tell the user something is slow

The same rule covers all three causes:
- tell them only when they are waiting on it;
- say what is slow and why, with a measured number;
- offer the one thing that is faster;
- say it once per project per cause in a tab. Closing it is remembered for
  30 days, per host or file.

| Cause | Known | Said when | Says | Offers |
|---|---|---|---|---|
| Browser limit (memory, one thread) | before, from sizes | at the click: the open, a build, a batch, the model; a refusal | the size or time | the desktop app |
| Slow network (https, s3, gs, sftp) | measured | 10 of the last 15 s went to waiting on one host, at under 50 MB/s | "Waiting on the network" and the rate and host, e.g. "3.0 MB/s from transfer.rc.hms.harvard.edu." | "Set up a data node" (the connection dialog, on its presets) |
| Network drive (a picked file) | measured | 10 of the last 15 s went to waiting on one file, at under 100 MB/s | "Reading a slow drive" and the file and rate, then "copy it here" | — |

### How it is measured

- **The network:** `server/utils/read_pace.py`.
  - Every fetch in `remote_store.ChunkCacheStore._fetch` is bracketed.
  - Per host, the time at least one fetch was in flight is reported as
    intervals `{cause, source, start, end, busy, bytes}`, through
    `boot.start`'s `post`.
  - An interval closes after 0.25 s of quiet, or after 1 s of waiting.
- **A picked file:**
  - Python's reads are timed in `workers/pythonWorker.js` `drivePace`. They
    are synchronous, so their times add up.
  - The tile workers' reads are timed in `services/browserNode.js` `paced`:
    each tile's own read time, as an interval.
- **The desktop app** says the same two notices.
  - Its page loads `handoffNotice.js` itself (`templates/base.html`,
    `data-reads="desktop"`). It asks `GET /read_pace` every 2 s while
    visible, lined up with the server's clock.
  - The route's first call turns measuring on (`read_pace.keep`), so a
    process no page watches measures nothing.
  - The network is timed as on the website. A drive is timed in tifffile's
    reads of a file it opened from a path (`read_pace.time_tifffile`): TIFF,
    OME-TIFF, SVS, NDPI and the other TIFF-based formats.
- **The judge:** `services/handoffNotice.js` `judgeSlow`/`pace`.
  - Only the waiting counts. Intervals that are all waiting, such as tiles
    read side by side, are joined where they overlap. One that waits for part
    of its span counts that share.
  - So decoding between reads never counts as a slow drive, and twenty
    parallel tiles count once.
  - A fast source read a lot is not called slow. The other notices cover a
    large amount of work.

Thresholds are in `client/src/envelope.json` `slow`. They were chosen between
measurements, not fitted:
- O2's transfer node over SFTP: about 20 MB/s;
- a lab SMB share: 1 MiB in about 220 ms;
- a local NVMe: over 1 GB/s.

The notice that offers a data node changes the earlier rule that no notice
offers one. You decided that on 2026-11-11.

Not done:
- **On the desktop, a drive's other readers** are not timed: HDF5's own reads
  of a local .ims, zarr folders, and the other formats' readers.
- **The other notices stay on the website.** They are about what a browser
  tab holds, which the desktop app does not share.
- **A remote read made by the page itself** is not timed. That is a bucket the
  page reads directly; only Python's reads are.
