# The website programme: what needs you

Everything the website work (plan `dreamy-brewing-sunset.md`, branch `web/phase0`)
is waiting on from you, in one place. Kept current as work lands; the newest
state is on `web/phase0`. Each item says what to do and what it unblocks.
Nothing here has been done for you: nothing pushed, nothing deployed beyond
the preview, no DNS changed after assets.plexoraapp.com.

Last updated: 2026-11-07 (web/phase0 at 39a42309 and later; the preview runs
its bundle `py/dev/39a42309569f/`, with /selfcheck).

## 1. Deploys (in this order)

1. **biocognia-platform, branch `web/cors`** (worktree `C:\pxplat`): review,
   merge, deploy as `C:\pxplat\docs\deploy-web-browser.md` says, staging first.
   - core: the `environments` migration (the `browser` kind), then deploy
     bioc-core (exact-origin CORS for the viewer's origins).
   - ai: the `ai_requests` migration (the `web` origin) **outside 03:30-04:15
     UTC**, then deploy bioc-ai (CORS on its eight product routes).
   - Unblocks: activating a licence and every AI feature on the live website.
   - Your call inside it: `askingProductOnly` in `activation.ts` registers a
     browser only for the product that asked (so it does not use a SCIMAP Pro
     device slot). Revert that one function for the old behaviour.
2. **Plexora's telemetry Worker** (`backend/`, from Plexora `main` once
   `web/phase0` is merged): `cd backend && npm ci && npx tsc --noEmit && npx
   vitest run`, then `python scripts/release.py telemetry` (deploys because
   the vectors differ; waits for `/healthz` to report `68c8d195594ac126`).
   **Before** any website or desktop release that sends the new `web` /
   `runtime` values, or the old Worker drops them and the website's uploads
   fail CORS (they wait in the queue; nothing is lost). Until then the preview
   deployed by hand is built without `--telemetry` (it sends nothing); CI's
   preview passes it.
3. **Sibling releases** (no rush; shims cover them until then):
   - spatialagents: a runtime hook and `"web"` in `ORIGINS` (until then the
     website's AI calls are filed as `app`).
   - biocognia: `"browser"` in `ENVIRONMENT_TYPES` (models.py); the store
     without `os.link` fix (worktree `C:\pxbio`, branch `web/no-os-link`).

## 2. Cloudflare (dashboard or a wider token)

- **Cache Rule** on assets.plexoraapp.com for `.wasm` and `.whl` (they show
  `cf-cache-status: DYNAMIC`, so every first visit reads R2).
- **Zero Trust Access** on the preview (`plexora-viewer-preview`); it is
  public but unlisted (`noindex`) today.
- **Production Pages project `plexora-viewer`** (production branch `release`)
  and the CI secrets `CLOUDFLARE_API_TOKEN` (Pages: Edit + R2: Edit) and
  `CLOUDFLARE_ACCOUNT_ID` (docs/internal/WEBSITE_RELEASE.md).
- **The docs move** (decision 5): create the `plexora-site` Pages project and
  the DNS for plexoraapp.com / www (refused here as a new public surface).
- **At GA**: attach viewer.plexoraapp.com, then
  `python tools/browser_build/publish.py flags --viewer on` (it says
  `preview` now: the preview shows a "Preview" mark). The same switch shows
  the desktop app's **File > Open on the Website** (it reads that file; no
  desktop release needed), and `--viewer desktop-only` hides it again.

## 3. Google (for Google Cloud on the website, plan L9)

- Register a BioCognia **OAuth client**: type Web application, JavaScript
  origin `https://viewer.plexoraapp.com`, redirect URI exactly
  `https://viewer.plexoraapp.com/google-auth.html`, an External consent
  screen, scopes `compute`, `devstorage.read_only`,
  `cloudplatformprojects.readonly` (plus `openid email`). Verification needs a
  demo video and a privacy policy (no security assessment: none is
  restricted); until verified, 100 test users. Then build with
  `static_build --google-client-id <id>`. (L9.md)
- After that, one run against real Google (CORS on the googleapis hosts, the
  node VM's startup script on a real Debian 13 VM: both unchecked).
- Optional later: a "Desktop app" OAuth client, for the desktop's Google
  Cloud without the CLI.

## 4. Counsel / licences

- **openslide-js 1.0.0-beta.5**: LGPL-2.1-only (OpenSlide, GLib, Cairo,
  gdk-pixbuf statically linked in one .wasm loaded at run time; source at the
  gitHead in its `SOURCE` file). THIRD_PARTY_NOTICES.md marks it "to be
  confirmed by counsel". (L2.md)
- **plexora-kernels**: its licence is set to Plexora's EULA; confirm before
  publishing. (L5.md)
- **MobileSAM** (Apache-2.0) on the bucket and on the GitHub release has no
  LICENSE file beside it; Apache-2.0 asks redistributors to include one.
- **ffmpeg.wasm**: nothing from FFmpeg ships with the website (the plan's
  `@ffmpeg/core` is GPL with x264). An LGPL build for ProRes is written up in
  L10.md, for when it is built.
- For the list: OpenJPEG via @cornerstonejs/codec-openjpeg (MIT, BSD-2),
  onnxruntime-web 1.30.0 (MIT), numcodecs.js 0.3.2 (MIT, c-blosc BSD-3),
  mediabunny (MPL-2.0, already there).

## 5. Decisions

- Hide the desktop's **Settings > Advanced "Browser rendering"** until GA? (It
  is visible now, default Off.) One entry in `settings_routes.DESKTOP_SECTIONS`.
- **Fresh desktop installs** keep projects in `Documents/Plexora/projects`
  (one library shared with the website)? Existing installs are unaffected.
- **Publish plexora-kernels** to PyPI and ship it in the desktop runtime (the
  desktop then never imports numba: start 3.6 s instead of 5.6-15.2 s)?
- **`.bif`** in both apps' import list (`import_proposal.IMAGE_SUFFIXES`)?
- **Sakura**: wait for openslide-js to fix its crash (report it upstream with
  `bench/sakura_fixture.py`), or have a small Python reader built?
- **Emscripten** in WSL Ubuntu + the FFmpeg source download (refused by the
  permission check here): needed for ProRes on the website; otherwise ProRes
  stays a "use the desktop app" notice.
- **ROI/QC columns on a CSV or Parquet project.** The desktop copies the
  table into the project at import and writes the columns there; the website
  writes them into the user's own CSV (with "Save into your files" on) and
  refuses without it. Recommended: the website copies the table into the
  project as the desktop does, so both behave alike and the setting is only
  for .h5ad/SpatialData. Say so and it gets built.

## 6. Git

- Merge `web/phase0` into `main` and push (`git push plexora main`) when you
  are ready; nothing is pushed.
- Cherry-pick **13de91ea** (`/resource_status` answers again for a project
  whose mask pyramid is not its source) to main: main has the bug too.
- Old merged branches you may delete: `web/client-seams`, `web/node-pair`,
  `web/removals`.

## 7. Clean-up the safety check would not let me do

Safe to delete (scratch copies; nothing reads them):
`C:\plexora-bench-{ai,ci,conn,desk,desk2,ff,gcp,h5,kern-nok,lic,mask,sam,telemetry,vol}`,
`C:\pxstage`, `C:\pxai` (empty), `C:\pxdesk-target` (cargo cache, several GB),
`C:\pxff-pw`, `C:\pxff-runs`, `C:\pxff-shots`, `C:\pxff-data`, `C:\pxff-swtest`,
`C:\plexora-bench-con` and `C:\plexora-bench-wb` (0.5 GB each, merged work),
`C:\pxweb\website\node_modules`, and the stray locked worktree
`plexora\.claude\worktrees\agent-a04461bf5800bad41`.

Keep (tests or builds read them): `C:\plexora-bench` (the bundle and
fixtures), `C:\plexora-bench-kern` (until kernels/dist is the only home of the
wasm wheel; a copy is now in `C:\pxweb\kernels\dist`), `C:\plexora-bench-slides`
(slide fixtures and the OpenSlide truth venv), `C:\plexora-bench-vid` (the
desktop FFmpeg the video test compares with), WSL `~/pxkern-build` (rebuilds
the kernels' Pyodide wheel), `C:\pxplat` (until web/cors is merged).

## 8. Records only you can make

- **The self-check, on every computer you can reach** (the quickest of
  these): open https://plexora-viewer-preview.pages.dev/selfcheck, click
  **Run the check** (about a minute), then **Download results** and send me the
  file. It measures the browser, storage, download rate, Python's start, a
  pyramid build, memory and WebGPU, and says what each means there; nothing
  is sent anywhere. Wanted most: Safari on a Mac, an 8 GB laptop (Chrome),
  Firefox anywhere. **The Mac (2026-11-07) ran Chrome 131**, which crashed
  starting the website's Python (filed: `bench/envelope/selfcheck/`); the
  website now asks a Chrome or Edge older than 137 to update instead. On that
  Mac: update Chrome (menu > Help > About Google Chrome) and run the check
  again (it starts afresh after an update); and once in Safari.
- Safari on a Mac (real OPFS, the 7-day purge, `persist()`), an 8 GB laptop,
  the ERIS portal's rewriting, a GCP VM node behind TLS: the preview's exit
  records (plan Phase 5). The self-check covers the first two's numbers; the
  purge and `persist()` still need a person over a week.
- Chrome's local-network prompt when pairing a node (headless Chrome cannot
  show it).
- Chrome's write prompt when the website saves into your file (Settings >
  Data > "Save into your files", then Save gates on an .h5ad): it should ask
  once per file; and what "Allow on every visit" does. The test stands the
  prompt in.
