"""The 3-D viewer's client probes: what the plane, the sections and the 3-D
view show while the user moves through a stack.

Each probe runs the real classic script in node against fakes it owns:

- plane_flash_probe.mjs: a plane step never shows a frame outside the two
  planes it joins (the fade's ghost is an opaque copy of ground + canvas);
- gl_init_probe.mjs: the tile shaders are fetched and compiled once per page,
  however often the viewer re-raises `open`.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(probe_name):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(
        [node, str(REPO_ROOT / "tests" / "js" / probe_name)],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


def _passed(out, at_least):
    assert "checks passed" in out, out
    assert out.count("ok - ") >= at_least, out


def test_a_plane_step_never_flashes():
    _passed(_run("plane_flash_probe.mjs"), 10)


def test_the_tile_shaders_are_compiled_once_per_page():
    _passed(_run("gl_init_probe.mjs"), 9)


def test_every_surface_draws_one_navigation_revision():
    """The plane, the panes, the outlines and the 3-D view commit in one task;
    late surfaces never overwrite a newer revision; superseded revisions commit
    nothing; while the hand moves an uncovered revision gives way sooner and a
    ready one always lands; a far jump supersedes in the request's own task;
    the hand let go stops counting as moving at once, while a request made
    before it let go keeps its scrub revision (tests/js/nav_revision_probe.mjs)."""
    _passed(_run("nav_revision_probe.mjs"), 35)


def test_the_section_panes_show_the_newest_section_in_step():
    """Superseded section handovers never land; a section that never covers
    keeps the old one and says slow, landing when it covers; XZ and YZ of one
    pan each reveal when they cover, the faster first, each label at its own
    commit, and an older pan's late result never lands; a pan during a new
    frame waits for it; a scrolled pane loads rows past its edges; each pane
    says how its loading goes; the z-line follows the picture; a sub-pixel cut
    keeps its section (tests/js/section_panes_probe.mjs)."""
    _passed(_run("section_panes_probe.mjs"), 50)


def test_each_pane_says_when_it_is_loading():
    """views/paneStatus.js: a pill on the plane and on each section from
    `plexora:surface-status` -- never under 220 ms, a dot then a ring that
    never goes back within a revision, held 300 ms then faded, an error at
    once with a retry, the three independent and mounted lazily, the strip's
    thumb on the same status (tests/js/pane_status_probe.mjs)."""
    _passed(_run("pane_status_probe.mjs"), 14)


def test_one_scheduler_fetches_and_decodes_each_tile_once():
    """Dedup, order of need, decode once with a transferred buffer, cancellation
    with the item that wanted the tile; while the hand moves what only sharpens
    waits in line, and goes when it stops -- at once when it lets go; a failed
    tile says whether OSD will ask again, and one whose retries are spent can
    be asked for again; batched only on a slow link
    (tests/js/tile_fetch_probe.mjs)."""
    _passed(_run("tile_fetch_probe.mjs"), 47)


def test_the_3d_view_keeps_its_textures():
    """A channel switched off and on in 3-D is a uniform, not a download; HD and
    another area fetch, going back does not; LRU past the GPU budget; zoomed in
    at rest, the part seen is fetched finer, once; a channel switched on for the
    first time is fetched under its window and drawn; a channel new to the
    picture fades in, at once with motion off or for a capture; the reference
    grid's steps, units, numbers on screen, preference and uniforms
    (tests/js/volume_renderer_probe.mjs)."""
    _passed(_run("volume_renderer_probe.mjs"), 70)


def test_the_3d_view_draws_finer_bricks_where_the_camera_needs_them():
    """The atlas: levels finer than the base, the cut (fine near, coarse far,
    within the atlas's room), the page table, slot reuse, batched requests,
    gzip only on a slow link, and the shader's walk
    (tests/js/volume_atlas_probe.mjs)."""
    _passed(_run("volume_atlas_probe.mjs"), 37)


def test_a_volume_starts_with_a_provisional_window_and_takes_the_exact_one():
    """services/volumeWindows.js: batched, polled while provisional, a change
    announced once (tests/js/volume_windows_probe.mjs)."""
    _passed(_run("volume_windows_probe.mjs"), 6)


def test_surfaces_upload_once_and_composite_with_the_volume():
    """Surfaces in the 3-D view: one upload per mesh, style changes are
    uniforms, order-independent glass with one blend state (sorted without
    float targets), the ray caster's surface variant in every variant,
    picking and unprojection (tests/js/surface_renderer_probe.mjs)."""
    _passed(_run("surface_renderer_probe.mjs"), 28)


def test_quick_surface_previews_debounced_and_posts_raw_units():
    """Surface Studio's dock: refused on Free with the Paid modal, a debounced
    preview, a superseded one aborted, the raw isovalue posted
    (tests/js/surface_studio_probe.mjs)."""
    _passed(_run("surface_studio_probe.mjs"), 15)


def test_a_3d_mask_is_drawn_as_cell_surfaces_in_3d():
    """Outlines and Filled in the 3-D view (services/maskSurfaces.js): built
    only in 3-D with a 3-D mask and Surface Studio; a preview then the standard
    build, posted once each; the standard replaces the preview the frame its
    mesh is in, never a frame drawing neither; glass for Outlines, solid for
    Filled, coloured as the cells are; a failure reported once and retried; a
    page that finds them made posts nothing (tests/js/mask_surfaces_probe.mjs)."""
    out = _run("mask_surfaces_probe.mjs")
    assert "all checks passed" in out, out
    assert out.count("PASS ") >= 45, out


def test_one_modal_and_chip_serve_every_background_job():
    """views/jobWait.js: the mask pyramid's wait and the cell surfaces' wait
    are two instances on two mounts, Escape closes the one on top, leaving
    the viewer closes both, a failure offers Retry (tests/js/job_wait_probe.mjs)."""
    out = _run("job_wait_probe.mjs")
    assert "all checks passed" in out, out
    assert out.count("PASS ") >= 10, out
