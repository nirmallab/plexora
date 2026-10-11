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
    nothing (tests/js/nav_revision_probe.mjs)."""
    _passed(_run("nav_revision_probe.mjs"), 20)


def test_the_section_panes_show_the_newest_section_in_step():
    """Superseded section handovers never land; the z-line follows the picture;
    a sub-pixel cut keeps its section (tests/js/section_panes_probe.mjs)."""
    _passed(_run("section_panes_probe.mjs"), 15)


def test_one_scheduler_fetches_and_decodes_each_tile_once():
    """Dedup, order of need, decode once with a transferred buffer, cancellation
    with the item that wanted the tile; batched only on a slow link
    (tests/js/tile_fetch_probe.mjs)."""
    _passed(_run("tile_fetch_probe.mjs"), 27)


def test_the_3d_view_keeps_its_textures():
    """A channel switched off and on in 3-D is a uniform, not a download; HD and
    another area fetch, going back does not; LRU past the GPU budget; zoomed in
    at rest, the part seen is fetched finer, once (tests/js/volume_renderer_probe.mjs)."""
    _passed(_run("volume_renderer_probe.mjs"), 15)


def test_the_3d_view_draws_finer_bricks_where_the_camera_needs_them():
    """The atlas: levels finer than the base, the cut (fine near, coarse far,
    within the atlas's room), the page table, slot reuse, batched requests,
    gzip only on a slow link, and the shader's walk
    (tests/js/volume_atlas_probe.mjs)."""
    _passed(_run("volume_atlas_probe.mjs"), 35)


def test_a_volume_starts_with_a_provisional_window_and_takes_the_exact_one():
    """services/volumeWindows.js: batched, polled while provisional, a change
    announced once (tests/js/volume_windows_probe.mjs)."""
    _passed(_run("volume_windows_probe.mjs"), 6)
