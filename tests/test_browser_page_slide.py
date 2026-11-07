"""A whole-slide image read through the website's page (plan L2), in CPython
with a stand-in for the page: providers/browser.py `PageSlide` and
brightfield.py's `_page_slide`.

On the website the page reads MRXS, Sakura, NDPI and SVS with openslide-js
and Python asks it for levels and regions; here a fake asker answers from an
array, and these hold what brightfield.py does with the answers: the levels,
the pixels composited over white, the page's reason when it cannot open an
MRXS, tifffile again when it cannot open an SVS, and nothing of it on the
desktop (no page).
"""

from __future__ import annotations

import numpy as np
import pytest

from plexora.server.providers import browser
from plexora.server.utils import brightfield
from tests import brightfield_fixtures

W, H = 2600, 1900


class _Page:
    """Answers the page's two questions from one RGBA picture and its halves."""

    def __init__(self, fail=None):
        rng = np.random.default_rng(5)
        self.full = rng.integers(0, 256, (H, W, 4), dtype=np.uint8)
        self.full[..., 3] = 255
        self.full[:200, :300, 3] = 0       # unscanned: transparent
        self.full[200:260, :300, 3] = 128  # half covered
        self.levels = [self.full, self.full[::2, ::2], self.full[::4, ::4]]
        self.fail = fail
        self.asked = []

    def __call__(self, kind, args):
        self.asked.append(kind)
        if kind == "slide":
            if self.fail:
                return {"error": self.fail}
            return {"levels": 3, "dims": [[a.shape[1], a.shape[0]] for a in self.levels],
                    "downsamples": [1.0, 2.0, 4.0], "properties": {"openslide.mpp-x": "0.25",
                                                                    "openslide.mpp-y": "0.25"},
                    "associated": ["label"]}
        if kind == "slide_region":
            level = self.levels[args["level"]]
            down = [1, 2, 4][args["level"]]
            x, y = args["x"] // down, args["y"] // down
            block = level[y:y + args["h"], x:x + args["w"]]
            out = np.zeros((args["h"], args["w"], 4), dtype=np.uint8)
            out[:block.shape[0], :block.shape[1]] = block
            return {"width": args["w"], "height": args["h"], "data": out.tobytes()}
        raise AssertionError(kind)


def _over_white(rgba):
    rgba = rgba.astype(np.uint16)
    return ((rgba[..., :3] * rgba[..., 3:] + 255 * (255 - rgba[..., 3:])) // 255).astype(np.uint8)


@pytest.fixture
def website(monkeypatch):
    monkeypatch.setenv("PLEXORA_BACKEND", "browser")
    yield
    browser.set_asker(None)


def test_an_mrxs_is_read_through_the_page(website):
    page = _Page()
    browser.set_asker(page)
    pyramid = brightfield.open_rgb("/picked/abc/slide.mrxs")
    assert pyramid.level_shapes[0] == [H, W]
    tile = brightfield.rgb_region(pyramid[0], slice(0, 1024), slice(0, 1024))
    assert np.array_equal(tile, _over_white(page.full[:1024, :1024]))
    assert (tile[:200, :300] == 255).all()  # unscanned is slide-white
    # Level 1 is the page's own level 1, read whole.
    assert np.array_equal(brightfield.rgb_region(pyramid[1], slice(0, 64), slice(0, 64)),
                          _over_white(page.levels[1][:64, :64]))
    # One "slide" question per pick, however many readers open it.
    brightfield.open_rgb("/picked/abc/slide.mrxs")
    assert page.asked.count("slide") == 1
    # physical_metadata takes the path as a Path, which on Windows turns the
    # worker's "/picked/..." round (never on the website, a POSIX system).
    real = browser.is_page_file
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(browser, "is_page_file", lambda p: real(str(p).replace("\\", "/")))
        assert brightfield.physical_metadata("/picked/abc/slide.mrxs")["physical_size_x"] == 0.25
    layout = brightfield.page_tile_layout(pyramid)
    assert layout["path"] == "/picked/abc/slide.mrxs" and len(layout["levels"]) == pyramid.base_levels


def test_an_mrxs_the_page_cannot_open_says_why(website):
    browser.set_asker(_Page(fail="an MRXS slide keeps its pixels in the folder \"slide\" beside slide.mrxs"))
    with pytest.raises(browser.PageSlideError, match="keeps its pixels in the folder"):
        brightfield.open_rgb("/picked/abc/slide.mrxs")


def test_an_svs_the_page_cannot_open_is_read_by_tifffile(website, tmp_path, monkeypatch):
    path = tmp_path / "slide.svs"
    brightfield_fixtures.write_svs_like(path)
    monkeypatch.setattr(browser, "is_page_file", lambda p: True)
    page = _Page(fail="Unsupported or missing image file")
    browser.set_asker(page)
    pyramid = brightfield.open_rgb(path)
    assert page.asked == ["slide"]
    assert pyramid.level_shapes[0] == [1024, 1280]
    assert brightfield.page_tile_layout(pyramid) is None


def test_the_desktop_never_asks(tmp_path, monkeypatch):
    monkeypatch.delenv("PLEXORA_BACKEND", raising=False)
    path = tmp_path / "slide.svs"
    brightfield_fixtures.write_svs_like(path)
    page = _Page()
    browser.set_asker(page)
    try:
        pyramid = brightfield.open_rgb(path)
    finally:
        browser.set_asker(None)
    assert page.asked == []
    assert brightfield.page_tile_layout(pyramid) is None
