# The website programme: what needs you

Everything the website work (plan `dreamy-brewing-sunset.md`, branch `web/phase0`)
is waiting on from you, in one place. Kept current as work lands; the newest
state is on `web/phase0`. Each item says what to do and what it unblocks.
**The website is live** at https://viewer.plexoraapp.com since 2026-11-08
00:40 UTC, now 0.0.42 (`main` at 30a01116, no tag; since 2026-11-09
11:42 UTC: the website's performance work, docs/internal/bench/web/
WEBSITE_PERF_2026-11-09.md; 0.0.41, the loader's orb, before it), Pages
project `plexora-viewer`
branch `release`, bundle `py/0.0.42/`, flags `viewer: on`, telemetry on. 0.0.38 was live for under
an hour and did not start Python on the self-check page, in a second tab or
without sync access handles; its `py/0.0.38/` stays, as every released
prefix does, and nothing points at it. Activating a licence and AI do not work there until section 1,
item 1 is deployed. Nothing else here has been done for you.

Last updated: 2026-11-07 evening (the launch; the preview still runs its
bundle `py/dev/4f583c3b9745/` and, sharing the flags, no longer shows its
"Preview" mark).

## 1. Deploys (in this order)

1. **biocognia-platform, branch `web/cors`** (worktree `C:\pxplat`): review,
   merge, deploy as `C:\pxplat\docs\deploy-web-browser.md` says, staging first.
   - core: the `environments` migration (the `browser` kind), then deploy
     bioc-core (exact-origin CORS for the viewer's origins).
   - ai: the `ai_requests` migration (the `web` origin) **outside 03:30-04:15
     UTC**, then deploy bioc-ai (CORS on its eight product routes).
   - Unblocks: activating a licence and every AI feature on the live website
     (live since 2026-11-08 without them, your call).
   - Your call inside it: `askingProductOnly` in `activation.ts` registers a
     browser only for the product that asked (so it does not use a SCIMAP Pro
     device slot). Revert that one function for the old behaviour.
2. **Done 2026-11-07:** the Worker is deployed from `main` and its
   `/healthz` reports `bae44f6538cfbaff`; kept for the record:
   **Plexora's telemetry Worker** (`backend/`, from Plexora `main`): `cd backend && npm ci && npx tsc --noEmit && npx
   vitest run`, then `python scripts/release.py telemetry` (deploys because
   the vectors differ; waits for `/healthz` to report `bae44f6538cfbaff`,
   the allowlist with the website's count of browsers it asks to update,
   `site.summary`; it was `68c8d195594ac126` before that).
   **Before** any website or desktop release that sends the new `web` /
   `runtime` values, or the old Worker drops them and the website's uploads
   fail CORS (they wait in the queue; nothing is lost). Until then the preview
   deployed by hand is built without `--telemetry` (it sends nothing); CI's
   preview passes it. Once both are live, the admin Performance page shows
   `site.summary.refused` (split by Browser version): how often visitors meet
   "Update Chrome to use Plexora here".
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
- **Cloudflare Web Analytics is injected** into viewer.plexoraapp.com's pages
  (`static.cloudflareinsights.com/beacon.min.js`, the zone's automatic
  setup). The site's Content-Security-Policy blocks it, so nothing is sent
  and nothing breaks, but every visit logs a policy violation in the console
  and the live self-check fails that one check. Turn the injection off for
  the zone rather than allowing it in the policy. Not where it looks
  (2026-11-08): plexoraapp.com is not in the account's Web Analytics list
  (only biocognia.com and scimap.xyz), and the script outlived a fresh
  Pages deployment, so it is not the Pages project's Metrics switch either.
  It is on viewer.plexoraapp.com only, never on *.pages.dev: the zone's own
  Real User Monitoring (plexoraapp.com > Speed > Observatory), which
  Cloudflare turns on by itself for Free zones (EEA/UK/CH excluded). Its
  beacon token is 9665ce7e6839434090ebd6d83050328d. If no switch stops it,
  `Cache-Control: no-transform` on the site's HTML does (Cloudflare's Web
  Analytics FAQ).
- **CI secrets** `CLOUDFLARE_API_TOKEN` (Pages: Edit + R2: Edit) and
  `CLOUDFLARE_ACCOUNT_ID` (`6978852cf325dd1049afd2aa427d77a7`), so releases
  and previews deploy the website themselves (docs/internal/WEBSITE_RELEASE.md).
  The project `plexora-viewer` exists (created 2026-11-07).
- **The docs move** (your call, 2026-11-09: GitHub Pages with the custom
  domain plexoraapp.com, not a `plexora-site` Pages project). The code is on
  `main`: the site is built at the root, `DOCS_URL` is https://plexoraapp.com/,
  and the Docs workflow installs with `--no-sources`. It needs these records
  in the plexoraapp.com zone, every one **DNS only** (grey cloud) so GitHub
  can issue the certificate: `A @` 185.199.108.153, 185.199.109.153,
  185.199.110.153, 185.199.111.153; `AAAA @` 2606:50c0:8000::153,
  2606:50c0:8001::153, 2606:50c0:8002::153, 2606:50c0:8003::153;
  `CNAME www` nirmallab.github.io. Then the repository's Pages custom domain
  is set to plexoraapp.com and the Docs workflow run again;
  nirmallab.github.io/plexora/<page> then redirects to plexoraapp.com/<page>.
- **Done 2026-11-07:** viewer.plexoraapp.com attached (you added the CNAME
  `viewer` -> `plexora-viewer.pages.dev`) and `flags --viewer on`. That
  switch also shows the desktop app's **File > Open on the Website** in
  desktop builds that have it (from 0.0.37's code on); `--viewer
  desktop-only` is the kill switch.

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
- **Browsers asked to update, in the install counts.** The count of visitors
  told "Update Chrome to use Plexora here" (`site.summary`) arrives as a batch
  from an install of its own, so each such visitor also appears as a website
  install, with no sessions, in daily usage and active installs. Keep that, or
  have the Worker leave installs whose only rows are `site.summary` out of
  those counts (small; say so and it gets built).
- **Report Pyodide's 2 GiB bug upstream?** Pyodide 314.0.7's own glue reads
  four memory addresses as signed numbers, so past 2 GiB of heap data
  crossing between Python and the page comes out wrong (a 60 M-value table
  answered every request with its own column values). The bundle build
  patches it (`tools/browser_build/build.py` GLUE_FIXES); an issue or pull
  request on github.com/pyodide/pyodide would let a later Pyodide drop the
  patch. Public, so your call; the write-up is ready to draft.

## 6. Git

- `web/phase0` was merged into `main` (a fast-forward) and pushed on
  2026-11-07, at your word. Work goes on on `web/phase0`; each later wave is
  merged the same way when you say so. The push redeployed the docs site, so
  its "Website or desktop app" page links viewer.plexoraapp.com before that
  is attached (your call, 2026-11-07).
- The **next desktop release** from `main` carries the removals (Segment in
  3-D, Carry an ROI through planes), the desktop speed-ups and the new
  telemetry values (the Worker that takes them is deployed). Make it
  **0.0.43** (`release.py ci --bump patch` from 0.0.42), not a tag of a
  version the website already has: its `py/0.0.37/` to `py/0.0.42/` are
  published and never rewritten, so once the CI secrets
  exist such a Release run's `web_deploy` would refuse its own rebuild of
  that bundle and hold the release.
- **CI on `main` has been red since 2026-11-04** (48dff7d1, before the
  website work): `[tool.uv.sources]` points `biocognia-markers` at
  `../biocognia-markers`, which CI does not have, so `uv lock --check` and
  every install fail (ci, web, Docs, security). The Docs workflow alone
  installs with `--no-sources` since 2026-11-09, so the docs deploy again.
  biocognia-markers is on PyPI now: dropping the override
  and moving its uv.lock entry to the registry (by hand; uv here rewrites
  the lock) fixes it. Say so and it gets done.
- Old merged branches you may delete: `web/client-seams`, `web/node-pair`,
  `web/removals`.

## 7. Clean-up the safety check would not let me do

Safe to delete (scratch copies; nothing reads them):
`C:\plexora-bench-{ai,ci,conn,desk,desk2,ff,gcp,h5,kern-nok,lic,mask,sam,telemetry,vol}`,
`C:\pxstage`, `C:\pxai` (empty), `C:\pxdesk-target` (cargo cache, several GB),
`C:\pxff-pw`, `C:\pxff-runs`, `C:\pxff-shots`, `C:\pxff-data`, `C:\pxff-swtest`,
`C:\plexora-bench-con` and `C:\plexora-bench-wb` (0.5 GB each, merged work),
`C:\plexora-bench-cap` (31 MB of capacity-run fixtures; the big ones are
deleted and `tools/bench_envelope/*.py` rebuilds any of them),
`C:\plexora-bench-dwins` (75 MB, the desktop speed-up measurements, merged),
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
  Firefox anywhere. **The Mac is done in Chrome** (2026-11-07: Chrome 131
  crashed starting the website's Python, so the website now asks a Chrome or
  Edge older than 137 to update; updated to 157, every step passed, Python
  ready in 7 s; both filed in `bench/envelope/selfcheck/`). Still wanted on
  that Mac: the check once in Safari.
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
