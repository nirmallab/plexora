# CI

The shared tiers and the report are described in
[nirmallab/.github](https://github.com/nirmallab/.github#the-three-tiers).
This repository is public: hosted minutes are free, and it never uses the
self-hosted machines.

| Workflow | When | What |
|---|---|---|
| `ci.yml` | every push to main, every PR | the tests `ci/selection.toml` picks (`-p no:randomly -m "not perf"`, `-n 4`); committed reference and browser-API pages current; the wheel in a clean venv. 3-5 min |
| `weekly.yml` | Monday 05:17 UTC; by hand; the release gate | full suite on Linux x 3.12/3.13/3.14, 4 shards each; `perf` (report-only); backend vitest; client Karma and bundle; `uv lock --check` |
| `monthly.yml` | the 2nd; by hand | full suite on Windows and macOS x 3.12/3.13/3.14; `plexora mcp serve` conformance; `uv lock --upgrade --dry-run` report |
| `docs.yml` | pushes and PRs outside desktop/ and backend/ | regenerates the generated pages, builds, link-checks, deploys from main |
| `backend.yml`, `client.yml` | path-filtered pushes | the telemetry Worker (typecheck and vitest; on main, a deploy once `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` exist, about a minute); the browser client |
| `release.yml` | `v*` tags | wheel, sdist and installers; the preflight requires a passed weekly or monthly run on the tagged commit (`scripts/release.py ci` runs one and waits) and a live telemetry Worker that has the tag's vectors (one GET; `release.py ci` deploys it first) |

## Local only

- `perf` tests run in CI as a report; as a gate only locally: `PLEXORA_PERF=1 python -m pytest -m perf`.
- `test_label_gpu_parity` (Playwright/Chromium) and `test_video_studio_browser` (Chrome) skip where no browser is installed.

## Known (2026-10-26)

The full suite (9,824 tests, 25 min at `-n 6` on an M2 Pro; `ci/durations.json`) has nine failures on main that reproduce serially -- product changes that outran their tests:
`test_mini_map` (miniMap.js clamp moved), `test_settings_remotes_page` (a `16` in the page's cache-busting query), `test_plugin_css_boundary[gating]` (gating.css styles `#openseadragon`), `test_slider_css` x2 (figure_builder.css styles a range input), `test_gating_delta` x2 (brief format and score components), `test_ai_routes::test_the_balance_carries_an_estimate...` (`contrast` now among the plugins), `test_roi_magic_js::test_probe`.
