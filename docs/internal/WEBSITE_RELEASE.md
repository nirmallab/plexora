# Releasing the website

The website is two things on Cloudflare: the **shell** (the static build,
`python -m plexora.client.static_build`) on Pages, and the **Python bundle**
(`tools/browser_build/build.py`) on the R2 bucket `plexora-assets`, served at
`https://assets.plexoraapp.com/py/<version>/`. The shell's `client/shell.json`
names the bundle it loads, so a shell and its bundle always travel together.
A released bundle prefix (`py/X.Y.Z/`) is written once and never rewritten or
deleted; development prefixes (`py/dev/<sha12>/`) come and go.

## What runs where

| Workflow / job | When | What |
|---|---|---|
| `web.yml` `static` | every PR and push to main (not desktop/, backend/, website/, docs/) | both static builds; Pages' limits (`budget.py --site`); the website's 24 test files (Python and the node probes) |
| `web.yml` `bundle` | same | `build.py`, cached on Pyodide's version + `uv.lock` + `build.py` + `plexora/browser/stages.py` (a hit rebuilds only Plexora's wheel); first visit <= 50 MB (`budget.py --bundle`); the bundle says pyproject's version |
| `web.yml` `import_check` | same | `tests/browser/import_check.mjs`: the bundle booted in Node from disk; the app builds with no stage; every gating/QC/AI/agent module imports with every stage in; `registry.discover()` registers gating and QC; a calibrated OME-TIFF reports 0.5 um/px |
| `web.yml` `smoke` | same | `tests/browser/smoke.mjs` in headless Chromium on SwiftShader, correctness only: isolated, Python ready, a picked image opens and draws, no `securitypolicyviolation`, no failed request; the shell a first visit loads <= 4 MB gzipped (`budget.py --shell`) |
| `web.yml` `preview` | push to main, after the four above | bundle to `py/dev/<sha12>/`, read back from assets.plexoraapp.com, shell (`--noindex`) to Pages project `plexora-viewer-preview` (branch `main`), the previous development bundle pruned (`publish.py current`) |
| `release.yml` `web` | every Release run, beside the installers | bundle and shell from the tagged tree, no cache; budgets; `release.py propagate --check --bundle --site --tag`; the Node import check |
| `release.yml` `web_deploy` | `v*` tags, after every installer built | bundle to `py/<version>/`, read back, shell to Pages project `plexora-viewer`, branch `release` (production); `publish` waits for it |

Wrangler runs as `npx --yes wrangler@3.114.17` (the version `backend/`'s lock
pins), never `latest`; `publish.py` refuses an `npx` line without an exact
version. Only the deploy steps see the token, and they run the standard library,
`publish.py` and wrangler, never the project's code.

## What the user sets up, once

1. **API token.** One Cloudflare API token, account-scoped to the account that
   holds plexoraapp.com, with:
   - Account > **Cloudflare Pages: Edit**
   - Account > **Workers R2 Storage: Edit**
   - and whatever the token already has for `backend.yml`'s Worker deploy (the
     plan adds these two to the existing token rather than making a second).
   No zone permission: nothing here touches DNS or cache rules.
2. **Repository secrets** `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID`
   (the names `backend.yml` already reads). Without them the preview says so
   and deploys nothing, and a release warns and leaves the website where it was.
3. **Pages project `plexora-viewer`**, created with production branch `release`
   (a hand deploy with the default branch can then never go live):
   `npx wrangler@3.114.17 pages project create plexora-viewer --production-branch release`.
   `plexora-viewer-preview` exists; its deploys so far went to branch `main`,
   served at plexora-viewer-preview.pages.dev, which is what `web.yml` keeps doing.
4. Optional: the environments `website-preview` and `website-production`
   appear on the first run; give `website-production` required reviewers to
   approve each release's deploy, or move the two secrets into it.
5. At GA: repository variable `PLEXORA_WEB_RELEASE` = `required`, so a release
   without the secrets fails instead of skipping the website.

Already in place (2026-11-07): the bucket with `tools/browser_build/r2_cors.json`,
its custom domain assets.plexoraapp.com. Still the user's, in the dashboard: a
Cache Rule so `.wasm`/`.whl` are edge-cached (they show `cf-cache-status:
DYNAMIC`), and Cloudflare Access on the preview.

## Cutting a release

Nothing new: `python scripts/release.py ci --bump patch` tags, and
`release.yml` builds the installers and, beside them, the website's bundle and
shell; once every installer built, `web_deploy` publishes the website, then
`publish` makes the GitHub release.

**The hand release (0.0.30 onward) cancels the Release run, so it skips the
website too.** After the tag, release the website by hand from a clean
checkout of the tag (the same commands the jobs run; `<v>` is the version):

```bash
python tools/browser_build/build.py --out OUT          # PLEXORA_PYTHON314, PLEXORA_BROTLI set
python -m plexora.client.static_build --out OUT/static --py https://assets.plexoraapp.com/py/<v>/
python tools/browser_build/budget.py --bundle OUT/py/<v> --site OUT/static
python scripts/release.py propagate --check --bundle OUT/py/<v> --site OUT/static --tag v<v>
node tests/browser/import_check.mjs --bundle OUT/py/<v>
python tools/browser_build/publish.py --wrangler W assets --bundle OUT/py/<v> --prefix py/<v>/
python tools/browser_build/publish.py verify --bundle OUT/py/<v> --base https://assets.plexoraapp.com/py/<v>/
python tools/browser_build/publish.py --wrangler W site --dir OUT/static --project plexora-viewer --branch release
```

`W` is wrangler's `bin/wrangler.js` or `"npx --yes wrangler@3.114.17"`. The
upload puts `manifest.json` last, so an upload that stopped can simply run
again; a re-run against a prefix that already holds this same bundle does
nothing.

## Rolling back

- **The website:** Pages keeps every deployment. In the dashboard, project
  `plexora-viewer` > Deployments > the last good one > *Rollback to this
  deployment*. Its `shell.json` names its own `py/<old>/`, which is never
  deleted, so the shell and its Python roll back together, at once (the shell
  is served `no-cache`).
- **A bad bundle** is never patched in place: roll the shell back, fix, and
  release a new version.
- **The preview:** re-run an older main commit's `web.yml` run.
- **A kill switch** (`flags.json` on the bucket, `{"viewer": "desktop-only"}`,
  effective in five minutes; plan Phase 5 "Operations") is **future work**:
  nothing writes or reads `flags.json` yet.

## Running the checks here

From a checkout, with the plexora conda env and a bundle under
`$PLEXORA_BENCH_HOME/browser_node/web/py/<v>/`:

```bash
python -m plexora.client.static_build --out S                       # 2 s
python tools/browser_build/budget.py --bundle B --site S
python scripts/release.py propagate --check --bundle B --site S --tag v<v>
node tests/browser/import_check.mjs --bundle B                      # ~20 s
python tests/browser/smoke_fixture.py F
node tests/browser/smoke.mjs --site S --py <bundle's py/ folder> --fixture F/smoke.ome.tif \
     --swiftshader --shell-out shell.json                           # ~13 s, system Chrome
python tools/browser_build/budget.py --shell shell.json
```

`tests/test_browser_import_check.py` runs the import check under pytest when
the bundle is there, and says when the bundle is older than
`plexora/browser/stages.py` (rebuild it: a stage-list change needs the full
build, a Python change `--plexora-only`).

## Not covered yet

The plan's CI also lists: the P9 session against its CPython truth in the
browser, activation against a mocked platform, pairing against
`tests/node_harness.py`, MobileSAM from a stub, Firefox and WebKit subsets,
a `modulefinder` closure check, and previews for pull-request branches. None
of these is in `web.yml` yet.
