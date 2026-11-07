# The website in Firefox and WebKit

Date: 2026-11-07. Branch `web/browsers`. Chromium is the timing target; Firefox and Safari must
function (the user's decision). This records what the website's drivers do in each engine, what broke
and why, and what was changed (each a capability check, never the engine's name).

## Set-up

- **Engines:** Chrome 156 (system), and Playwright 1.65's Firefox 159 and WebKit 27.2 (Safari's engine,
  built for Windows). All three are cross-origin isolated on the site, have SharedArrayBuffer and
  Atomics.wait in workers, module and nested workers, CompressionStream, and **JSPI**
  (`WebAssembly.Suspending`). Firefox and WebKit have no File System Access pickers.
- **Without JSPI:** Firefox with `javascript.options.wasm_js_promise_integration` off (`--no-jspi`),
  which is what Firefox and Safari releases before JSPI shipped are.
- **Storage:** WebKit on Windows opens OPFS but fails every write (`UnknownError`; sync access handles
  `NotSupportedError`): it stands in for a browser with no usable OPFS (a private window), not for
  Safari on macOS, which has OPFS.
- **Fixture:** LSP11385's three channels written flat, 252 MB (`fixtures/flat/lsp11385-s3ch-raw.ome.tif`),
  whose import builds a pyramidized copy in the page; ORION CRC02's 634 MB CSV (1,260,592 x 29) for
  the table hand-over.
- **Run:** `node tests/browser/<driver>.mjs --browser chromium|firefox|webkit [--no-jspi]`
  (`PLEXORA_BROWSER` likewise; `PLEXORA_PLAYWRIGHT_CORE` names a playwright-core folder);
  find_files through `PLEXORA_BROWSER=<engine> pytest tests/test_browser_find_files.py`.

## Drivers (Measured)

| Driver | Chrome 156 | Firefox 159 | Firefox 159, no JSPI | WebKit 27.2 (Windows) |
|---|---|---|---|---|
| boot.mjs (harness ready) | pass, 8.3 s | pass, 8.1-8.7 s | pass, 11.3-12.1 s | pass, 10.4-13.0 s |
| site.mjs (`/`, `/settings`, `/open_project` written) | pass, 8.1-8.6 s | pass, 9.0-11.0 s | pass, 10.8-11.8 s | pass, 9.8-12.8 s |
| import.mjs (252 MB flat: import / viewer drawn) | pass, 4.1 / 5.2-5.7 s | pass, 4.3-4.9 / 6.2-7.2 s | pass, 4.3-5.6 / 5.7-8.0 s | pass, 5.2 / 7.0-7.4 s |
| pyramid copy built by the page | 0.8-0.9 s, OPFS | 0.9-1.1 s, OPFS | 0.9-1.4 s, OPFS | 1.3-1.7 s, memory then IndexedDB |
| places.mjs (first visit, pyramid question, reopen) | pass (folder; reopen 14.3 s) | pass (browser; 10.0 s) | pass (12.7 s) | pass (IndexedDB; 12.7 s) |
| store_sync.mjs | pass (6 folder checks) | pass (3 browser-store checks) | pass | pass (IndexedDB) |
| handoff.mjs ("Open in Desktop App") | pass (moves, offers download) | pass (says it cannot) | pass | pass (says it cannot) |
| find_files (desktop project's image) | pass ("Find them…") | pass ("Locate file…") | pass | pass ("Locate file…") |
| quota.mjs (pyramid past the quota) | not run (no real limit) | pass: a sentence in 3.6-5.1 s | not run | not run |

- **Times** are the range over two to four single runs each on this machine (i9, RTX 3080), each
  driver alone; boot is the harness page's ready, site the address until the app's page is written,
  places' reopen includes answering the loader's question. Firefox and WebKit draw the imported image
  about 0.5-1.7 s after Chrome; the copies differ by a few hundred KB between engines (each engine's
  deflate).
- **Without JSPI** every stage (34 MB) loads at boot (+2.9 s), and Python's waits on the page go
  through shared memory; the import was 0-0.7 s slower than with JSPI over two runs. The CSV
  hand-over (1.26 M x 29) took 2.05 s over shared memory against 1.67 s through JSPI, with the same
  column sum.

## What broke, and why

| Engine | What | Why | Fix |
|---|---|---|---|
| Firefox (Playwright) | the loader never left "Starting Plexora" on any address after the first | Playwright's Firefox never hands a navigation to an active service worker, so `controllerchange` never fired | the loader asks the worker to claim the page (`plexora-claim`) when it is not controlled |
| Firefox (Playwright) | every route the app's frame asked for reached the static server (`POST /import/inspect` 501, GETs got the loader) | Playwright's Firefox never lets a worker see a frame's requests, though the frame reports a `controller` | the worker answers `/__plexora/relayed` itself; a frame that does not get that answer installs `services/pageRelay.js` (fetch, XMLHttpRequest, `<img>` answered by `PlexoraBackend.answer`). It also serves a window with no service workers |
| Firefox (Playwright) | a picked file never arrived | Firefox fires `cancel` on the input as Playwright takes over its dialog, then sets the files | a `change` within a second of a `cancel` counts |
| Firefox | a pyramid past the quota was written short, then failed as `IndexError` in tifffile | Firefox's sync access handle does not throw at the quota: `write()` writes short and returns the count | every write is checked (`QuotaExceededError` otherwise), the unfinished copy is dropped, and the import says "this browser could not build the image's pyramid (no room left for it: this site may keep 10.7 GB here), so no copy was made." |
| Firefox | once in five fresh profiles, the first OPFS write was refused | not found | three tries; projects a visit kept in IndexedDB move back into OPFS when it can be written |
| WebKit | the Python worker died with `NoGilError` while the import read the picked TIFF | WebKit runs FinalizationRegistry callbacks inside `FileReaderSync`'s read (1999 of 2000 in a probe; no timers, messages or microtasks; Chrome and Firefox run none there), while Python has released its lock around the read; Pyodide's finalizer aborts without it | the worker's block reader takes the lock back (`PyGILState_Ensure`/`Release`) for the read |
| WebKit | the frame's address stayed `/` after every navigation (the tab's too) | `document.open()` gives the frame the host's URL: WebKit takes the entry document from the host's promise the loader continues | the loader restores the page's address (`history.replaceState`) before writing it |
| WebKit (Windows) | projects and pyramid copies not kept | OPFS opens but every write fails | the store falls back to a folder kept in IndexedDB (`workers/idbFolder.js`); the copy is built in memory and kept in IndexedDB |
| no JSPI | `/import/sample` failed opening any TIFF | zarr 3.2.1 in Pyodide refuses its synchronous API without JSPI (`run_until_complete` -> `run_sync`), and Python's own selector loop cannot start there (no `socketpair`) | `boot.without_jspi`: Pyodide's `run_until_complete` runs the coroutine on `SyncLoop`, a selector-less loop |
| no JSPI | Python could not wait on the page (`run_sync`): stages on first import, the table's columns, the image's window, the pyramid and its status, a poll's sleep | no stack switching | stages load at boot; asks block in `Atomics.wait` while the page writes the answer into shared memory (8 MiB chunks; files by `blob:` URL read with a synchronous request); sleeps are `Atomics.wait` |

**Why shared memory, not Python doing the work itself, without JSPI:** polars in Pyodide panics on a
large CSV (P9), so the page's parse is the only reader of a big table; a pyramid built by Python would
sit in the 32-bit heap (and take minutes); and the site is cross-origin isolated anyway (D5). The cost
measured above is 0.4 s on 1.26 M rows and 0.7 s on the import. A browser with neither JSPI nor shared
memory gets `null` from every ask, and Python then reads the file itself.

## What the user sees where there is no folder picker

- The first visit does not ask where projects go (that question needs a folder picker); it says once
  "Projects are kept in this browser / It may clear them if the site goes unused. Ask it to keep
  them?" with Not now / Keep them (`navigator.storage.persist()` from the click; Firefox asks the user,
  so headless runs record `persisted: false`). Where nothing can be kept (no OPFS, no IndexedDB) it says
  "Projects are not kept" on every visit. Settings > Data has "Ask to keep it".
- "Open in Desktop App" says "This browser can't move projects to the desktop app / Chrome and Edge can."
- The import's pyramid question takes its other form ("Build a pyramid first?", the copy "in this
  browser's storage for this site").
- A picked file is not remembered across visits (no handle): opening its project asks "This project's
  image is on this computer" with Skip / Locate file… (a file input; Chromium keeps "Find them…", a
  folder).

## Not measured, or open

- **Safari on a Mac** (OPFS with sync access handles, the 7-day purge of script-written storage,
  what `persist()` answers there): needs the M2 laptop. On Windows WebKit's storage rows are the
  no-OPFS fallback.
- Whether stock Firefox (not Playwright's) hands frames to the service worker: the in-page relay makes
  the website work either way; it was not measured.
- Without JSPI the worker answers nothing while Python waits (a held session poll blocks for its
  seconds); AI sessions without JSPI are not exercised here.
- A store write past the quota is logged, not said to the user.
- `<img>` in a page's own HTML and a stylesheet's `url()` with a route address are not relayed by the
  in-page relay (thumbnails, re-pointed once in the document).
