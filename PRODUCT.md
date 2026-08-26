# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

**One install, three kinds of expertise, no persona switch.** A lab adopts
Plexora as a lab, and the same interface is used by:

- **The wet-lab biologist or pathologist.** Does not code. Generated or received
  the slide. For this person Plexora *is* the analysis environment: open the
  image, pick channels, assign colour and contrast, threshold a marker, draw an
  ROI, take a figure panel.
- **The computational biologist.** Arrives with an AnnData / SpatialData / CSV
  and a pipeline already run. Uses Plexora to see the image behind the numbers —
  verify a cluster, check where a gate actually falls, colour cells by a
  phenotype column.
- **Imaging core facility staff.** Registers datasets, converts pyramids,
  configures the data directory and shared directories, and sets up remote or
  HPC access so other people can open slides.

**The situation.** The data is large and lives where it landed — a laptop, a lab
workstation, a mounted share, or cluster scratch. Sessions are long and mostly
spent on one image. The screen is often reached through an SSH tunnel or a
notebook, not a local browser tab.

## Product Purpose

A viewer and analysis tool for large multiplexed microscopy images — OME-TIFF
whole-slide data, tens of thousands of pixels square, many fluorescence
channels. The user selects a subset of channels, assigns each a colour and a
contrast range, and pans and zooms an additively blended composite. Optional
layers sit on top: a segmentation mask with cell outlines, cell centroids,
marker-threshold gating, and per-cell colouring by a metadata column.

Success is a researcher getting from a raw multiplexed image to a threshold, an
annotation, or a publication figure they trust — on the machine where the data
already is, with no upload, no conversion service, and no server administrator.

## Positioning

**Plexora runs where the data already is.** `pip install plexora`, then one
command, and the same viewer appears from a laptop terminal, a notebook cell, an
SSH'd server, or an HPC compute node. `plexora connect user@host --srun "…"`
submits the job and builds the two-hop tunnel; JupyterHub, Open OnDemand and
Colab are detected and the correct proxied URL is constructed without being
told. Shared read-only dataset directories coexist with private per-user work.

Neighbouring tools ask the data to come to them — an upload, a conversion
service, a tiling server someone has to run. The differentiator here is reach,
not rendering.

## Operating Context

**Four routes in, one viewer.** `plexora` in a terminal; `plexora.view("name")`
in any notebook; `plexora connect user@host` from a laptop against a remote
machine; `plexora --remote` / `--ood` on the host. All four end in the same
interface, and the design must hold up in all four framings, including inside a
notebook output cell and under a proxied path prefix.

**Data lives in one resolved directory**, chosen by `--data-dir` →
`PLEXORA_DATA_PATH` → a recorded setting → a platform default. Moving it matters
on HPC and on small system drives. Shared projects are read-only and marked
*Shared*; anything a user produces while exploring one is written to their own
directory.

**Import is one screen, and it asks for the minimum.** Name, image, optional
mask, optional table — no tab per format. A project may legitimately be nothing
but an image. Further facts (which column is the cell id, marker vs metadata,
which expression layer) are collected only when a feature actually needs them,
stored on the project, and reused by the next feature that needs the same
answer.

**Tools are plugins, and several can be open at once.** Thresholding, ROI, Cell
Explorer and Figure Builder are bundled examples of a documented public API;
each plugin that draws cells gets its own layer, colours and opacity. Figure
Builder additionally has a life outside any project — a library and a canvas
that are whole pages of their own.

**The app is server-rendered and multi-page**, but a client router keeps the
open viewer alive across internal navigation. The viewer is rebuilt when, and
only when, the project changes. State the user built by hand — viewport above
all — is the thing that must survive a trip to another page.

## Capabilities and Constraints

**Stack (existing, not a new decision).** Flask + Jinja server-rendered
multi-page app served by Waitress; OpenSeadragon with a WebGL2 colorize pass in
the browser; classic `<script>` tags plus a small webpack bundle. No SPA
framework and no HMR dev server. Design tokens live in
`plexora/client/src/css/tokens.css`; client assets carry `?v=` cache-busting
tags in the templates that load them.

**Binding constraints, confirmed:**

- **No network at runtime.** No CDN fonts, no external scripts, no accounts, no
  telemetry. Plexora must work air-gapped and through an SSH tunnel on a compute
  node. Every asset ships inside the wheel.
- **WebGL2 and a modern browser are the floor.** There is no fallback rendering
  path and no legacy browser support. Design may assume both.
- **Licensing and attribution are facts, not marketing.** Plexora Academic
  License 1.0 — free for academic, nonprofit and government research; *not* an
  open source license; forks and commercial use are not permitted without a
  separate license; plugins built against the documented API are explicitly
  carved out and belong to whoever wrote them. Any public-facing surface must
  state this correctly.

**Explicitly NOT binding: the dark chrome.** The near-black surface tokens are
the incumbent choice, not a rule. This was offered as a constraint and
deliberately not taken, so future work is free to revisit the visual world.
Legibility of the image itself remains a real design consideration wherever the
composite is on screen — but it is a consideration, not a locked palette.

**No user accounts, by design.** A multi-user deployment is one process per user
behind a reverse proxy. The single place any authentication exists is the Open
OnDemand route, where a per-server token protects a single-user server rather
than distinguishing users.

**Fixed vocabulary.** project / datasource, channel, marker vs metadata,
segmentation mask, centroid, gate / threshold, ROI, cell layer, figure panel and
scene, shared project, data directory. These are the words in the code, the
docs and the UI; a redesign renames them only deliberately.

**Undecided / not established.** Whether a public-facing marketing site or hosted
documentation exists or is planned — there is none in this repository, and no
design work should assume one.

## Brand Commitments

- **Name:** Plexora. **Owner:** Nirmal Lab (`github.com/nirmallab/plexora`).
  Contact for commercial licensing: Ajit Johnson Nirmal.
- **Existing assets:** `plexora/client/src/img/logo.svg` and
  `logo_with_text.svg`, with `.ai` sources beside them; `favicon.ico`,
  `apple-touch-icon.png`; `icon.icns` and `icon.ico` at the repository root.
- **Voice, as observed across README.md, DEPLOYMENT.md and SKILL.md:** plain,
  precise, second person. It explains *why* a rule exists, names the failure the
  rule prevents, and states limits rather than hiding them. No marketing
  adjectives, no exclamation marks, no superlatives. This describes the existing
  corpus; it has not been declared binding.
- No colour, typography, or aesthetic direction has been made binding.

## Evidence on Hand

**Real and usable:**

- `README.md` and `DEPLOYMENT.md` (~31 KB, with the actual output of each
  command) — genuine, detailed, already-written product copy.
- `LICENSE` — the full Academic License 1.0 text.
- `SKILL.md` (~88 KB) — an internal engineering guide including a
  "Performance: Measured Facts" section with real measured numbers.
- Logo and icon assets (SVG plus editable `.ai`).
- A real, running, reasonably mature interface: 8 core templates, 5 plugin
  templates, ~120 KB of hand-maintained CSS with a shared token file.
- A test suite: `tests/`, browser probes under `tests/js/`, and
  `tests/baseline_orion2` (which depends on a local `orion2` dataset that is
  **not** in the repository).

**Absent — must never be fabricated:**

- No testimonials, quotes, or named users or institutions beyond the lab itself.
- No publication, preprint, or citation.
- No benchmark or performance claims other than the measured ones already in
  SKILL.md.
- No pricing or terms for the commercial license.
- No screenshots, demo dataset, or sample images in the repository.
- No public documentation site, changelog, or roadmap.

## Product Principles

1. **One interface, three kinds of expertise.** No persona switch and no
   advanced mode. Every surface must stay legible to someone who has never
   opened a terminal, without slowing down someone who arrived from a notebook.
2. **Ask for the minimum, then only what a feature needs — and never twice.** An
   image alone is a valid project. A guess is not an answer, and an answer, once
   given, is never asked for again.
3. **Reach is the product.** Any surface must survive being viewed through an
   SSH tunnel, inside a notebook output cell, under a proxied path prefix, and
   entirely offline. If it only works on a local laptop tab, it does not work.
4. **The image is the workspace.** Chrome earns its pixels beside a composite the
   user is actively reading, and the state a user built by hand — viewport,
   channels, contrast windows — is the state that must survive.
5. **State what it does and what it does not.** The existing corpus names its own
   limits plainly. No surface may invent capability, evidence, or claims to fill
   a gap.

## Accessibility & Inclusion

No accessibility standard has been established for this product. What exists in
the code today:

- `tokens.css` collapses `--duration-fast` and `--duration-base` to `0ms` under
  `prefers-reduced-motion: reduce`, so motion is already opt-out at the token
  layer.
- `:focus-visible` styling is present in `main.css`, `viewer.css` and
  `quickView.css`.
- There is **no** `prefers-contrast` handling and **no** colour-vision-safe
  default palette for channel assignment. In a tool whose meaning is carried by
  additively blended colour, that is an open question worth deciding rather than
  a settled one — recorded here as undecided, not as a requirement.
