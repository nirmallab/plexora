# Plexora documentation system: implementation plan

Status: plan only. Nothing in this document has been implemented.
Written 2026-09-27 against `main` at bac6e7d5 (0.0.25) plus the uncommitted
licensing/telemetry work in the tree. Every claim below was checked against
code; file:line references are to that state.

Scope follows the brief "Plexora Documentation System: API, Python, Notebook,
Remote/HPC, and Developer Reference". AI, MCP, agent, licensing, telemetry,
pricing and other commercial material is excluded from the public site
throughout.

---

## 0. What the audit changes about the brief

The brief was written from the outside. These findings reshape it:

1. **`import plexora` is not side-effect free.** `plexora/__init__.py` calls
   `create_app()` at module scope (builds the Flask app, discovers plugins,
   installs telemetry hooks, patches zarr). Every submodule import triggers it.
   The generator therefore runs in a subprocess with an isolated data root and
   telemetry off; it must never run against the user's live install.
2. **The public surface is a flat dict, not `__all__`.** `_PUBLIC_API`
   (`plexora/__init__.py:341-376`) maps name to module and is resolved lazily
   by module `__getattr__`. `plexora.view` is the one eager function. There is
   no `__all__`, no `__version__` attribute, and no API metadata of any kind.
   Functions fetched through `plexora.<name>` come back wrapped by a telemetry
   counter, so the generator resolves each name to the defining module's
   attribute directly.
3. **The plugin API has two entry points.** `plexora.api.__all__` is the
   runtime data surface. The descriptor (`Plugin`, `Requires`, `NavItem`,
   `Requirement`) lives in `plexora.api.plugin`, which `plexora.api` does not
   re-export, yet every bundled plugin imports from it. Both are public and the
   manifest must cover both.
4. **There are six bundled plugins, not four:** cell_explorer, figure_builder,
   gating (UI label "Thresholding"), roi, transcripts, visium_hd. The last two
   are viewer layer sections, not tools.
5. **CosMx and MERSCOPE have no reader.** They appear only in tests as fake
   `register_detector` callbacks. The modality section documents Multiplexed
   imaging, H&E/brightfield WSI (incl. DICOM), Xenium, Visium, Visium HD,
   SpatialData and AnnData, and says plainly what is not supported.
6. **The CLI parser is importable without the package.** `plexora.cli` is
   stdlib-only at import and `build_parser(command)` is pure argparse, so CLI
   reference generation is cheap. The one exception, the `ai` subparser,
   imports the package at build time and is excluded anyway.
7. **Vocabulary clash in the shipped UI.** The same record is called "Sample"
   (Import Sample… menu), "Project" (open_project tab) and the collection is
   "Dataset". The gating plugin is labelled "Thresholding". The docs will use
   project / dataset consistently; the UI relabel is a decision for you (§5).
8. **The docs URL is hardcoded twice**, in `helpMenu.js:17-18` and the Rust
   desktop menu `menu.rs:25`, with different anchors. Phase 12 centralizes it.
9. **Existing content is richer than expected.** `DEPLOYMENT.md` (1849 lines)
   and `docs/SETUP_GUIDE.md` (1340 lines) already cover local, SSH, SLURM,
   JupyterHub, Open OnDemand, Colab, Google Cloud, Docker and data nodes in
   user-facing prose. Phases 5 to 8 are largely migration plus restructuring,
   not writing from scratch. The setup guide's screenshot index lists 15+
   screenshots that were never captured.
10. **GitHub Pages is not enabled** on nirmallab/plexora (repo is public,
    `has_pages: false`). Enabling it with source "GitHub Actions" is a
    one-time repo-settings action only you can do.

---

## 1. Audit results (Phase 1 deliverables)

### 1.1 Public Python API (`plexora.*`)

Source of the list: `_PUBLIC_API` keys plus `view`. Docstrings today are all
freeform prose; none has a Parameters/Returns section; `import_sample` uses
epydoc `@param` lines; examples exist only in `create_dataset`,
`import_sample`, `configure_project`.

| Category | Symbol | Defined at | Docstring state |
|---|---|---|---|
| Viewer & Notebook | `view(datasource, **kwargs)` | `__init__.py:295` | prose, documents ephemeral `tool/overlay/channels` |
| Viewer & Notebook | `PlexoraViewer` (start, url, refresh, iframe, open, from_anndata, from_memory, from_files) | `jupyter.py:506` | not exported; `__init__` docstring starts mid-sentence; `from_files`, `iframe` undocumented |
| Viewer & Notebook | `ServerStartError` | `jupyter.py:50` | not exported |
| Projects | `create_project` (24 params) | `datasets.py:383` | prose, no example |
| Projects | `import_sample(*paths, ...)` | `datasets.py:312` | `@param` lines, two examples |
| Projects | `add_layers` | `datasets.py:354` | one sentence |
| Projects | `configure_project(name, **answers)` | `datasets.py:513` | prose, one example |
| Projects | `project_manifest` | `datasets.py:208` | one sentence |
| Projects | `project_from_spec` | `datasets.py:302` | one paragraph |
| Projects | `PROJECT_SPEC_KEYS` | `datasets.py:43` | comment only |
| Datasets | `create_dataset` | `datasets.py:229` | prose, two examples |
| Datasets | `list_datasets`, `dataset` | `datasets.py:177,192` | one sentence each |
| Datasets | `Dataset` (refresh, add, remove, rename, describe, delete, manifest, len/iter/in) | `datasets.py:84` | `rename` undocumented |
| Datasets | `DatasetCreateError` | `datasets.py:62` | prose |
| Data sources | `register_datasource`, `register_image_datasource`, `register_anndata_datasource`, `register_spatialdata_datasource` | `datasource.py:744,1265,1066,1235` | prose |
| Data sources | `register_memory_datasource` (28 params) | `memory.py:425` | prose |
| Remote data nodes | `register_node`, `forget_node`, `list_nodes`, `node_resources`, `inspect_table`, `attach_image`, `attach_table`, `attach_segmentation`, `detach` | `nodes.py` | prose, param-by-param paragraphs |

Deliberately not public (module-level, no underscore, not in `_PUBLIC_API`):
`datasets.pending_conversions/failed_conversions/conversion_warnings`,
`datasource.rename_channels/set_pixel_size/register_rgb_datasource/
register_blank_datasource/reregister_image/...`, thirteen `nodes.*` helpers,
`memory.KernelNode/snapshot_*`. The manifest lists these under `excluded:`
with a reason so the coverage test is explicit rather than silent.

`plexora.connect` is CLI machinery only; it is documented under the CLI, not
the Python API.

### 1.2 Plugin developer API

`plexora.api.__all__` (26 names), grouped:

| Group | Symbols |
|---|---|
| Entry points | `project_data`, `dataset` (alias), `Dataset` (alias), `sample`, `layers`, `layer`, `manifest` (module re-export; document only `answered`, `never_confirmed`, `GIVEN_KEYS`) |
| Handles | `ProjectData`, `DatasetSchema`, `ImageHandle`, `ImageSource`, `SegHandle`, `TableHandle`, `TableSource`, `MetadataColumn` |
| Storage | `PluginStore`, `store` |
| Viewer & HTTP | `notify_viewers`, `json_response` |
| Remote resources | `ResourceLocator`, `ResourceNotLocal`, `ResourceUnavailable`, `table_operation`, `table_stream` |
| Utilities | `deduplicate_names` |

`plexora.api.plugin` (descriptor): `Plugin` (fields name, label, version,
blueprint_factory, panels, scripts, styles, requires, intro, nav_items,
owns_cell_layer, icon, shortcut, menu, `LAYER_SECTION_SLOT`), `Requires`,
`Requirement`, `NavItem`, `requirement()`, `layer_requirement()`,
`normalize_shortcut()`. Excluded from public docs with a stated reason:
`capabilities_factory` (agent), `entitlement`, `endpoint_entitlements`
(licensing). `plexora.api.features` is internal and stays undocumented.

Discovery: entry-point group `plexora.plugins` (`pyproject.toml:181-185`),
bundled-package scan (`server/plugins.py:56-70`), `PLEXORA_PLUGINS` filter.

Browser contract (all in plain block comments today, no parsed JSDoc):
definition shape and lifecycle in `pluginRegistry.js:1-135` (`createInstance`,
`createSidebarController`, `onShow`, `onHide`, `onVisibilityChange`,
`captureCarryState`, `applyCarryState`, `bindEvents`, `destroy`,
`ownsCellLayer`, `preferredCellMode`, `supportedCellModes`, `help`, `lazy`,
`hasLayer`); context object in `main.js:1607-1668`; layer API in
`main.js:1463-1604`; tool loading in `views/toolLoader.js`.

### 1.3 CLI

Document (all pure argparse, `plexora/cli.py:937-1126` and the
`_build_*_parser` functions):

- `plexora [datasource]` with `--host --port --data-dir --data-dir-default
  --base-url --plugins -r/--remote --login-host --bind-node --ood --also-serve
  --node-port --node-allow-origin --no-detect --browser/--no-browser`.
  `--desktop` is shown in help but marked internal in the docs.
- `plexora where [--data-dir-only]`
- `plexora config [show | set {data-dir,shared-dirs,mask-output,remote-cache-gb} VALUE]`
- `plexora connect TARGET [DATASOURCE]` with 20 options incl. `--srun`,
  `--bind-node`, `--jump`, `--ssh-opt`, `--forward`, `--save`, `--install`,
  `--also-serve`, `--local-serve`, `--no-local-node`
- `plexora node serve | connect | prepare`
- `plexora dataset create | list | show | add | remove | rename | delete`
- `plexora project create | show | set` (shared `_PROJECT_OPTIONS`)

Exclude: `plexora mcp`, `plexora ai`, `plexora telemetry`, `plexora license`.

### 1.4 Launch modes and environments (all verified in code)

| Surface | Mechanism | Code |
|---|---|---|
| Local terminal | Waitress in-process, environment detection unless `--no-detect` | `cli.py:2887-3102` |
| Desktop app | Tauri v2 spawns `python -m plexora --desktop`, reads one JSON ready line | `desktop/src-tauri/src/server.rs` |
| `--remote` | binds loopback, prints the exact `ssh -N -L` (or `-J` two-hop) command | `cli.py:409-503` |
| `plexora connect` | local ssh (reuses `~/.ssh/config`), remote `plexora --remote`, tunnel, optional local/remote data nodes | `connect.py` |
| `--srun` | login node holds `srun … plexora --remote`, second ssh tunnels to the compute node | `connect.py:196-247, 1012-1056` |
| `--ood` | binds `0.0.0.0` under `/rnode/<node>/<port>`, per-session token | `cli.py:505-539` |
| Docker | `Dockerfile` + `run.py`, `PLEXORA_HOST=0.0.0.0` | root |
| Notebook | `plexora.view` spawns/adopts a sidecar `plexora-server --notebook-mode`; display ladder: explicit base_url, proxy=False, Colab, OOD, JupyterHub proxy, direct | `jupyter.py:291-412`, `notebook_env.py:287-366` |
| Data node | `plexora node serve --serve KIND:ID=PATH`, KIND in {image, segmentation, table}, token mandatory | `cli.py:1129-1286`, `server/node/app.py` |

Notebook environments with tests: local Jupyter/Lab, VS Code, JupyterHub
(needs `jupyter-server-proxy`, in the `jupyter` extra), Open OnDemand, Colab.

Data-root precedence (`paths.py:242-268`): `PLEXORA_DATA_PATH` →
settings.json `data_dir` → `PLEXORA_DATA_PATH_DEFAULT` (suggestion, conflict
raises) → platformdirs default. Settings file: `user_config_dir/plexora/settings.json`.

Public environment variables: `PLEXORA_HOST`, `PLEXORA_DATA_PATH`,
`PLEXORA_DATA_PATH_DEFAULT`, `PLEXORA_SHARED_PATH`, `PLEXORA_MASK_OUTPUT`,
`PLEXORA_REMOTE_CACHE_BYTES`, `PLEXORA_BASE_URL`, `PLEXORA_PLUGINS`,
`PLEXORA_NOTEBOOK_MODE`, `PLEXORA_AUTH_TOKEN`, `PLEXORA_NODE_HOST`,
`PLEXORA_NODE_TOKEN`, `PLEXORA_DOCKER`. About twenty more are internal,
tuning, test or release-only and stay out of the public table.

### 1.5 Modality and format support

Supported and detected automatically: multiplex OME-TIFF/TIFF/hyperstack,
OME-Zarr (local and https/s3, gs/az with the `remote` extra), SVS/NDPI/SCN/BIF,
MRXS/SVSLIDE (OpenSlide, `wsi` extra), DICOM WSI (wsidicom, `wsi` extra),
PNG/JPEG pictures, CSV/TSV/TXT, Parquet, AnnData `.h5ad`/zarr, SpatialData
`.zarr`, Xenium run, Visium run (incl. CytAssist IF), Visium HD run, label
masks (TIFF/Zarr/numpy), Xenium boundary parquet, Visium HD segmentation GeoJSON.

Not supported or partial, and the docs must say so: CosMx, MERSCOPE (no
reader), Feather/Arrow IPC, generic GeoJSON shapes (recorded, not drawn),
Xenium cell×gene matrix (recorded, not read).

Detection is an if/else ladder (`import_proposal.py`) with a plugin
`register_detector` hook; suffix vocabularies are constants
(`IMAGE_SUFFIXES`, `TABLE_SUFFIXES`, `FLAT_TABLE_SUFFIXES`,
`BRIGHTFIELD_ONLY_SUFFIXES`, `_ADAPTERS`, `RESOURCE_KINDS`). The user-facing
modality catalogue already exists in `client/src/js/views/importHelp.js` and is
held in sync with the detector by `tests/test_import_help.py`.

### 1.6 Existing documentation and its disposition

| File | Lines | Disposition |
|---|---|---|
| `README.md` | 560 | migrate About/Install/desktop/data-lives/datasets/register-first/shared/SSH/HPC/Docker/nodes/notebooks; keep dev clone; drop smoke test; **exclude** Usage data, License |
| `DEPLOYMENT.md` | 1849 | migrate almost entirely (sections 1-8, Reference, Troubleshooting); Building/Signing go to contributing |
| `docs/SETUP_GUIDE.md` | 1340 | migrate wholesale: Part 1 → Getting Started, Part 2 → Remote & HPC / Notebooks, Part 3 → Data nodes; compatibility matrix → Reference |
| `PRODUCT.md`, `DESIGN.md`, `SKILL.md` | | internal; source material only (tagline, brand tokens for the theme) |
| `docs/internal/REMOTE_DATA_PERFORMANCE.md` | 390 | internal; its conclusion shapes the Remote & HPC recommendation |
| `docs/TELEMETRY.md`, `AI_AGENTS_REMOTE.md`, `AI_NATIVE_ROADMAP.md`, `AUTOMATIC_GATING.md`, `AUTOGATE_LIVE_RUN_*.md` | | **exclude**; move under `docs/internal/` |

### 1.7 In-app documentation links

`helpMenu.js:17-18` (`DOCS_URL = REPO#readme`), `menu.rs:25` (`DOCS_URL`
without anchor, also the About dialog), `tauri.conf.json:22` homepage,
`plexora/updates.py:41-43` (`GITHUB_REPO`, issues URL). No shared constant.

### 1.8 Packaging and CI facts

Runtime needs no Node (vendor bundle is committed; app JS is served as plain
files). Only workflow is `release.yml` (tag-triggered; Python 3.13, Node 22,
uv). Version lives only in `pyproject.toml:9`, propagated by
`scripts/release.py`. Test fixtures that docs can reuse:
`tests/test_datasets_api.py` `_image`/`_csv`, `tests/helpers.py`,
`ngff_fixtures.py`, `tenx_fixtures.py`, `spatial_fixtures.py`,
`brightfield_fixtures.py`, `node_harness.py`.

---

## 2. Architecture

### 2.1 Repository layout

```
website/                         docs app; its own package.json, never a runtime dep
  app/                           layout, docs/[[...slug]]/page.tsx, static search route, not-found
  components/mdx/                ApiSignature, ParamTable, Returns, Raises, Compare,
                                 Related, CliCommand, SourceLink, PlatformTabs,
                                 Callout, FileTree, ModalityBadge, PluginBadge
  content/docs/                  hand-authored MDX (everything not listed below)
  content/docs/python-api/       GENERATED
  content/docs/plugin-api/       GENERATED (python descriptor + runtime + browser)
  content/docs/cli/              GENERATED, with curated examples merged in
  content/docs/reference/formats.mdx, environment-variables.mdx   GENERATED
  lib/site.ts                    the only place URLs live: site, basePath, repo,
                                 issues, PyPI, version (read from generated JSON)
  scripts/                       generate_browser_api.mjs, build_llms.mjs, check_links.mjs
  source.config.ts, next.config.mjs, tsconfig.json, package.json
tools/docs/
  sync_docs.py                   `generate` and `check` entry point
  manifest.yaml                  public API classification (see 2.5)
  cli_examples.yaml              curated CLI examples + Python equivalents
  env_vars.yaml                  environment-variable metadata
  docstrings.py                  section parser (see 2.4)
  python_api.py cli_api.py formats.py envvars.py mdx.py
tests/test_docs_*.py             coverage, freshness, drift, docstring quality
.github/workflows/docs.yml
CONTRIBUTING_DOCS.md
```

Generated MDX is committed. That is what makes the freshness check
(`git diff --exit-code`) meaningful and lets the site build without Python.

### 2.2 Source-of-truth matrix

| Documentation | Edit here |
|---|---|
| Python API description, when-to-use, examples, notes | the function/class docstring |
| Python signature, defaults, annotations | Python source (introspected) |
| API category, order, aliases, "how is this different" pairs, related guides | `tools/docs/manifest.yaml` |
| Parameter groups for long signatures | `manifest.yaml` `param_groups` |
| CLI arguments, defaults, choices, help | argparse definitions in `plexora/cli.py` |
| CLI examples and Python equivalents | `tools/docs/cli_examples.yaml` |
| Supported file suffixes and resource kinds | the constants in `import_proposal.py`, `adapters/`, `providers/base.py` |
| Modality prose | hand-authored MDX (seeded from `importHelp.js`) |
| Environment variables | `tools/docs/env_vars.yaml`, checked by grep against the package |
| Browser plugin API | JSDoc typedefs in `pluginRegistry.js` and `main.js` |
| Concept guides, tutorials, plugin user guides | MDX |
| Generated MDX | never edited by hand |

### 2.3 Generator design

`python tools/docs/sync_docs.py generate` does, in order:

1. Spawn a child interpreter with `PLEXORA_DATA_PATH=<tempdir>`,
   `PLEXORA_TELEMETRY=off`, `PLEXORA_TESTING=1`, and the working tree on
   `sys.path`. The child imports `plexora`, walks `_PUBLIC_API` and `view`,
   `plexora.api.__all__` and the manifest's `plexora.api.plugin` list, and
   writes one JSON model: for each symbol the dotted name, kind, defining
   file and line range, `inspect.signature` rendered with annotations and
   defaults, the raw docstring, and for classes the public methods and
   properties. Resolution goes through the defining module attribute, not
   `plexora.<name>`, so the telemetry wrapper is never in the way. The
   subprocess boundary means the parent never pays the Flask import and a
   crash in the child cannot poison state.
2. Parse docstrings (2.4) and join with `manifest.yaml`.
3. Validate: every symbol classified; every `See Also` target exists; every
   `Parameters` entry matches a real parameter and vice versa (explicit
   `allow_partial: true` in the manifest for `**answers`-style functions);
   every `compare` pair references two documented symbols.
4. Emit MDX deterministically: sorted, no timestamps, header comment naming
   the source symbol, frontmatter (2.6), one page per function or class, a
   category index per group, an API landing page, `python-api/aliases.json`
   for search.
5. CLI: import `plexora/cli.py` from disk with `spec_from_file_location`
   (the tests already do this), call `build_parser(cmd)` for the documented
   commands, walk `_actions` and subparsers, merge `cli_examples.yaml`, emit
   one page per command path.
6. Formats: import the suffix constants and adapter registry and emit the
   Supported Formats tables.
7. Environment variables: emit from `env_vars.yaml`; a test greps
   `plexora/` for `PLEXORA_[A-Z_]+` and fails on any name absent from the
   YAML (public or internal).
8. Write `website/generated/version.json` from `importlib.metadata`.

`sync_docs.py check` runs generate into a temp dir and diffs against the
committed tree, printing which symbol changed.

Browser API: `website/scripts/generate_browser_api.mjs` parses the JSDoc
typedefs with `comment-parser` and emits `plugin-api/browser/*.mdx`. This is
Node, so it runs in the website build, never in the Python package.

Import time is measured in Phase 3 with `python -X importtime`; the child
process is the mitigation, not a runtime refactor.

### 2.4 Docstring convention

NumPy style, because underlined section titles are trivially parseable and
read well in `help()`. Sections, in this order, all optional except the
summary: summary line, extended description, `When to use`, `Parameters`,
`Returns`, `Raises`, `Examples`, `Notes`, `See Also`. `When to use` is a
custom section; the parser in `tools/docs/docstrings.py` is a ~150-line
section splitter plus a Parameters entry regex, so no third-party docstring
parser is needed. Defaults are not restated in prose. Examples are fenced
`python` blocks inside the `Examples` section and are the same blocks the
example runner executes (2.8).

### 2.5 Manifest schema

```yaml
version: 1
python_api:
  categories:
    - id: viewer
      title: Viewer & Notebook
      order: 1
      symbols:
        - name: plexora.view
          related: [plexora.register_memory_datasource]
          guides: [notebooks/in-memory-data]
        - name: plexora.jupyter.PlexoraViewer
          public_as: plexora.PlexoraViewer   # after the Phase 4 export
    - id: projects
      symbols:
        - name: plexora.create_project
          param_groups:
            Core resources: [image, name, segmentation, data, table]
            Table interpretation: [cell_id, x, y, sample, celltype, markers, metadata, subset, log1p, row_number_ids]
            Image: [channel_names, image_type, single_image, layer, coordinates]
            Location: [node, copy]
            Registry: [dataset, exist_ok]
  compare:
    - [plexora.import_sample, plexora.create_project]
    - [plexora.create_project, plexora.register_datasource]
    - [plexora.create_dataset, plexora.create_project]
  excluded:
    - name: plexora.datasource.rename_channels
      reason: module-level helper, not exported
  aliases:
    sample: [plexora.create_project, data/projects]
    mask: [plexora.attach_segmentation, data/segmentation]
plugin_api:
  runtime: { module: plexora.api, source: __all__ }
  descriptor:
    module: plexora.api.plugin
    symbols: [Plugin, Requires, Requirement, NavItem, requirement, layer_requirement, normalize_shortcut, LAYER_SECTION_SLOT]
    excluded_fields:
      - { name: Plugin.capabilities_factory, reason: agent surface }
      - { name: Plugin.entitlement, reason: licensing }
      - { name: Plugin.endpoint_entitlements, reason: licensing }
cli:
  include: ["", where, config, connect, node, dataset, project]
  exclude: [mcp, ai, telemetry, license]
  internal_flags: ["--desktop"]
```

The coverage test fails if a `_PUBLIC_API` key, `plexora.api.__all__` name
or documented CLI command is neither in a category nor in `excluded`.

### 2.6 Website stack and static export

Next.js (App Router) + Fumadocs (`fumadocs-core`, `fumadocs-ui`,
`fumadocs-mdx`), TypeScript, `output: 'export'`. Settings that GitHub Pages
needs and that are easy to get wrong:

- `basePath` and `assetPrefix` from `NEXT_PUBLIC_BASE_PATH`, default
  `/plexora`; a custom domain later means changing one env var and the CNAME.
- `trailingSlash: true` so `/python-api/` resolves to `index.html`.
- `images.unoptimized: true`.
- `app/not-found.tsx` exported so Pages serves `404.html`.
- Search: Fumadocs static search (index JSON produced at build, client in
  `static` mode). No API route at runtime.
- `.nojekyll` in `public/`.
- llms: `scripts/build_llms.mjs` writes `public/llms.txt` (index) and
  `public/llms-full.txt` (concepts, Python API, plugin API, CLI, remote and
  notebook guides, chrome stripped).

Frontmatter (validated by `source.config.ts` schema): `title`,
`description`, `icon`, `badge`, `related`, `aliases`, `generated`, `source`.
Order and grouping come from `meta.json` files, which the generator writes for
generated folders.

Theme: light/dark via Fumadocs; accent taken from `DESIGN.md` tokens so the
site reads as the same product as the app.

### 2.7 Information architecture

See §3 for the route map. Landing page is task-oriented (eight entry cards);
the API landing page has six category cards; no alphabetical index anywhere
except search.

### 2.8 Checks and CI

`tests/test_docs_*.py` (run by the normal suite and by docs CI):

- `test_docs_coverage`: every public symbol classified (2.5).
- `test_docs_freshness`: `sync_docs.py check` returns clean.
- `test_docs_signatures`: signature in each generated page equals
  `inspect.signature` of the live object.
- `test_docs_docstrings`: non-empty summary; Parameters coverage; valid See
  Also; no empty sections; per-symbol waivers in the manifest.
- `test_docs_envvars`: grep vs `env_vars.yaml`.
- `test_docs_examples`: extracts fenced python blocks tagged
  ` ```python title="runnable" ` from docstrings and a listed set of guides,
  substitutes fixture paths (`sample.ome.tif`, `cells.csv`, …) for tiny
  synthetic files built with the existing test helpers, and executes them
  under the conftest data-root isolation. `plexora.view` examples are tagged
  `no-run`; nothing spawns a sidecar in CI.

`.github/workflows/docs.yml`:

- Triggers: PRs and pushes to `main`, ignoring `desktop/**`, `backend/**`,
  `licensing/**`.
- Job `check` (Python 3.13, uv, `pip install -e ".[dev,spatial,jupyter]"`,
  no `wsi`): run `sync_docs.py check`, run `tests/test_docs_*.py`.
- Job `build` (Node 22): `npm ci`, generate browser API, `npm run build`,
  `check_links.mjs` over `out/` (internal links, anchors, assets, generated
  routes; external links lenient).
- Job `deploy` (main only): `actions/configure-pages`,
  `upload-pages-artifact` from `website/out`, `deploy-pages`. Concurrency
  group `pages` with cancel-in-progress.

`release.yml` is untouched. Docs deploy from `main`; "latest stable" is the
only version initially; the layout leaves room for `/v0.0.x/` later.

---

## 3. Route map

```
/                                   task-oriented home
/getting-started/
  what-is-plexora, installation, quick-start, first-project, core-concepts, desktop-app
/data/
  projects, datasets, layers, images, segmentation, cell-tables,
  importing (detection-first vs explicit), progressive-configuration, data-locations
/modalities/
  multiplexed-imaging, brightfield-wsi, xenium, visium, visium-hd, spatialdata,
  anndata-tables, not-supported
/viewer/
  navigation, image-channels, layers, segmentation, cell-overlays,
  transcript-rendering, display-controls, hd-mode, rotation-and-flip,
  dataset-navigation, keyboard-shortcuts, export
/plugins/
  thresholding (gating), roi, cell-explorer, figure-builder, transcripts, visium-hd-bins
/python/
  overview, standalone-workflows, projects, datasets, importing, configuring,
  data-sources, viewer, remote-data-nodes, split-data
/notebooks/
  overview, jupyter, vscode, in-memory-data, anndata, spatialdata, mixed-sources,
  refreshing, performance, jupyterhub, open-ondemand, colab
/remote/
  overview, ssh-connect, manual-tunnel, slurm, bind-node, open-ondemand,
  jupyterhub, google-cloud, docker, data-nodes, split-data, troubleshooting
/python-api/                         GENERATED
  viewer, projects, datasets, data-sources, remote-data-nodes, <one page per symbol>
/plugin-api/                         GENERATED
  descriptor/, runtime/, browser/
/cli/                                GENERATED
  plexora, where, config, connect, node, node-serve, node-connect, node-prepare,
  dataset, dataset-*, project, project-*
/plugin-development/
  overview, architecture, first-plugin, descriptor, requirements, python-api,
  browser-api, context, layers, state-and-storage, routes, lifecycle,
  testing, packaging, distribution, custom-detectors
/reference/
  supported-formats (GENERATED), environment-variables (GENERATED),
  configuration, file-locations, compatibility-matrix, troubleshooting, version
/contributing/
  documentation (from CONTRIBUTING_DOCS.md), development-setup, release-process
```

Stable slugs are chosen now so the in-app help can deep-link later
(`/plugins/roi`, `/viewer/image-channels`, `/remote/slurm`, …).

---

## 4. Phases

Sizes: S under a day, M one to three days, L a week or more of focused work.
Each phase ends in a mergeable state.

| # | Phase | Size | Depends on | Deliverables | Exit criterion |
|---|---|---|---|---|---|
| 1 | Audit | done | | this document | you have read §5 and answered the decisions |
| 2 | Website foundation | M | 1 | `website/` scaffold, `lib/site.ts`, MDX components, theme, static search, 404, llms scripts, `.gitignore` entries, `docs.yml` build job (no deploy yet), placeholder home | `npm run build` produces a static `out/` that serves under `/plexora/` locally via `npx serve` |
| 3 | Python API generator | L | 2 | `tools/docs/` package, `manifest.yaml` with every symbol classified, subprocess model dump, docstring parser, MDX emitters, coverage/freshness/signature tests, first generated `python-api/` and `plugin-api/` trees (from current docstrings, ugly but complete) | `sync_docs.py check` clean twice in a row; coverage test green |
| 4 | Docstring review | L | 3 | rewritten docstrings for the 31 `plexora.*` symbols, `Dataset` and `PlexoraViewer` methods, and the ~40 `plexora.api` / `plexora.api.plugin` symbols; small code changes: export `PlexoraViewer` and `ServerStartError` in `_PUBLIC_API`, add `plexora.__version__` via `importlib.metadata`; `compare` pairs and `param_groups` in the manifest | every generated page renders without an empty section; docstring quality test green with no waivers except listed ones |
| 5 | Standalone Python guides | M | 4 | `/python/*` from README, DEPLOYMENT and the tests in `test_datasets_api.py`; runnable examples wired to fixtures | example runner green |
| 6 | Notebook guides | M | 4 | `/notebooks/*`; environment pages from `notebook_env.py` and its tests | each hosted environment page states what is detected, from code |
| 7 | CLI reference | M | 3 | `cli_api.py`, `cli_examples.yaml`, `/cli/*`, Python-equivalent links | every documented command has purpose, usage, arguments, options, at least one example |
| 8 | Remote & HPC | M | 2 | `/remote/*` migrated from DEPLOYMENT §3-7 and SETUP_GUIDE Part 2-3; two diagrams (tunnel, data node); the data-node performance recommendation from `REMOTE_DATA_PERFORMANCE.md` | every command shown matches the audited CLI |
| 9 | Plugins | L | 3 | six user guides; `/plugin-development/*`; JSDoc typedefs in `pluginRegistry.js`/`main.js` (a JS change, so `?v=` tags and VERSION bump apply); `generate_browser_api.mjs`; a walk-through built from `tests/fixtures/plugins/future_modality` | a reader can build the fixture plugin from the docs alone |
| 10 | Migration and README | M | 5-9 | modality pages from `importHelp.js`; viewer section; `docs/internal/` for engineering notes; README cut to the brief's list; screenshot capture pass for the setup-guide index (needs the headless Chrome harness and one real project on disk) | no public claim in README that the site does not also make |
| 11 | CI hardening | S | 2, 3 | deploy job, link checker, path filters, `CONTRIBUTING_DOCS.md`; enable Pages (your action) | first deploy at the Pages URL; a PR that edits a docstring without regenerating fails with a named symbol |
| 12 | In-app links | S | 11 | `plexora/links.py` (`DOCS_URL`, repo, issues) rendered into `base.html` as a data attribute and read by `helpMenu.js`; `menu.rs` constant checked against it by a test; per-plugin `help.docs` slug so the `?` button can deep-link | Help → Documentation opens the hosted site in browser and desktop |

Suggested order: 2 → 3 → 4 → 7 → 5 → 6 → 8 → 9 → 10 → 11 → 12. Phase 7
moves earlier than the brief because it is cheap and exercises the site with
real generated content before the docstring rewrite lands.

Recommended branch: `feature/docs-site`, cut after the current licensing and
telemetry work is committed, so generated pages never encode uncommitted
state. Phases 4, 9 and 12 touch runtime code and should each be their own PR.

---

## 5. Decisions needed from you

1. **Site URL now.** Recommend `https://nirmallab.github.io/plexora/` with
   `basePath` centralized; a custom domain later is a one-line change plus a
   CNAME. You must enable Pages (Settings → Pages → Source: GitHub Actions).
2. **UI vocabulary.** Docs will say project and dataset. Should the app's
   "Import Sample…" / "Samples…" menu be relabelled to match, and should the
   gating plugin's label "Thresholding" become "Gating" (or the docs adopt
   "Thresholding")? This affects Phase 9 and 10 wording; nothing else blocks.
3. **Small public-surface additions** in Phase 4: export `PlexoraViewer`
   and `ServerStartError` through `_PUBLIC_API`, add `plexora.__version__`.
   Recommend yes; all three are additive.
4. **`plexora.api.plugin` status.** Either re-export the descriptor names
   from `plexora.api` (one import line; then the "only surface a plugin may
   import" docstring becomes true) or document two entry points. Recommend
   re-export.
5. **Module-only helpers** (`rename_channels`, `set_pixel_size`,
   `pending_conversions`, the `nodes.*` helpers). Recommend leaving them out
   of v1 docs via the manifest `excluded` list rather than promoting them.
6. **Screenshots.** Phase 10 needs one real project rendered on this machine
   (the Xenium or Visium HD run in Downloads would do). Confirm that is
   acceptable, or the viewer and plugin pages ship text-only first.
7. **Docs CI on every PR** (recommended, ~3 to 4 minutes) versus only on
   paths under `plexora/**`, `tools/**`, `website/**`.

---

## 6. Risks and traps

- **Live data root.** Any generator or example runner that imports `plexora`
  outside the conftest isolation writes into the user's real install. The
  subprocess sets `PLEXORA_DATA_PATH` to a temp dir unconditionally.
- **Layer-build threads outlive tests.** Example runner must not register
  anything that starts a background conversion; the tiny fixtures avoid it,
  and `import_sample(wait=True)` is the only form used in runnable examples.
- **Asset-tag bumps.** Phase 9 and 12 edit client JS; `base.html` `?v=`
  tags must be bumped and the boundary goldens will need refreshing.
- **Import cost in CI.** The generator imports the full runtime (Flask,
  zarr, polars, anndata). Acceptable in CI with uv caching; measure in
  Phase 3 and report the number.
- **Generated diff noise.** Determinism is a hard requirement: sorted
  output, no timestamps, no absolute paths, stable `meta.json` ordering.
- **Plugin API surface creep.** The runtime handles expose `read_region`,
  `centroid_tiles`, `run`/`stream` operations that are stable in intent but
  large in shape; document signatures and the guarantee, not internals.
- **Dirty tree.** 136 modified files and untracked licensing/telemetry code
  are in the working tree today; none of the docs work should start from it.
