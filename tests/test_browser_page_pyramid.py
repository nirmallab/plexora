"""A picked flat image's pyramid, built by the website's page.

`data_model._pyramidized_copy` asks the page first (providers/browser.py
`page_pyramid`): on the website the page's workers build the copy Python
would write (tests/test_tiff_writer.py holds them to that) into the site's
storage, and Python waits for it a look at a time. These pin when it asks,
what with, how it waits, and what it says when the page cannot finish.
"""

import pytest

from plexora import _work
from plexora.server.providers import browser
from plexora.server.utils.segmentation_pyramid import PyramidError

SPEC = {"shape": [3, 4000, 5000], "dtype": "uint16", "factors": [1, 2, 4, 8], "tile": 1024,
        "description": "<?xml ...?><OME/>", "fingerprint": "123-456"}


class Page:
    """The page's answers: a build that reaches `done` after `looks` looks."""

    def __init__(self, looks=2, end="done", start=True):
        self.asked = []
        self.looks = looks
        self.end = end
        self.start = start

    def __call__(self, kind, args):
        self.asked.append((kind, args))
        if kind == "pyramid":
            return {"id": 7} if self.start else None
        assert kind == "pyramid_status" and args == {"id": 7}
        self.looks -= 1
        if self.looks > 0:
            return {"state": "building", "tiles": 10, "total": 40}
        if self.end == "done":
            return {"state": "done", "tiles": 40, "total": 40, "path": "/derived/k/s.pyramid.ome.tiff"}
        return {"state": "failed", "error": "the disk is full"}


@pytest.fixture
def website(monkeypatch):
    monkeypatch.setenv("PLEXORA_BACKEND", "browser")
    waits = []
    monkeypatch.setattr(_work, "wait", lambda seconds: waits.append(seconds))
    yield waits
    browser.set_asker(None)


def _build(**kwargs):
    seen = {"progress": [], "stages": []}
    path = browser.page_pyramid(
        kwargs.pop("source", "/picked/ab/slide.tif"), name="slide.pyramid.ome.tiff",
        spec=kwargs.pop("spec", lambda: dict(SPEC)),
        progress=lambda done, total: seen["progress"].append((done, total)),
        stage=lambda key, detail: seen["stages"].append(key))
    return path, seen


def test_the_page_builds_it_and_python_waits_a_look_at_a_time(website):
    page = Page(looks=3)
    browser.set_asker(page)
    path, seen = _build()
    assert path == "/derived/k/s.pyramid.ome.tiff"
    kind, args = page.asked[0]
    assert kind == "pyramid"
    assert args["path"] == "/picked/ab/slide.tif" and args["name"] == "slide.pyramid.ome.tiff"
    assert {k: args[k] for k in ("shape", "dtype", "factors", "tile", "description")} == \
        {k: SPEC[k] for k in ("shape", "dtype", "factors", "tile", "description")}
    assert len(args["key"]) == 24
    assert [k for k, _ in page.asked[1:]] == ["pyramid_status"] * 3
    # Between looks the import waits as a poll does: requests are answered.
    assert website == [browser._POLL_S] * 2
    assert seen["progress"] == [(10, 40), (10, 40), (40, 40)]
    assert seen["stages"] == ["building", "writing"]


def test_one_copy_per_source_file_as_it_is_now(website):
    keys = []
    for source, fingerprint in [("/picked/ab/slide.tif", "1-2"), ("/picked/zz/slide.tif", "1-2"),
                                ("/picked/ab/slide.tif", "1-3")]:
        page = Page(looks=1)
        browser.set_asker(page)
        _build(source=source, spec=lambda f=fingerprint: {**SPEC, "fingerprint": f})
        keys.append(page.asked[0][1]["key"])
    assert keys[0] == keys[1] != keys[2]


def test_a_build_the_page_cannot_finish_says_why(website):
    browser.set_asker(Page(looks=2, end="failed"))
    with pytest.raises(PyramidError, match="the disk is full"):
        _build()


def test_a_page_that_will_not_build_leaves_it_to_python(website):
    browser.set_asker(Page(start=False))
    assert _build()[0] is None


def test_an_answer_from_javascript_is_read_as_python(website):
    class Proxy:
        def __init__(self, value):
            self.value = value

        def to_py(self):
            return self.value

    page = Page(looks=1)
    browser.set_asker(lambda kind, args: Proxy(page(kind, args)))
    assert _build()[0] == "/derived/k/s.pyramid.ome.tiff"


@pytest.mark.parametrize("source, spec", [
    ("C:/data/slide.tif", lambda: dict(SPEC)),          # not a pick
    ("/picked/ab/slide.zarr", lambda: dict(SPEC)),      # not a TIFF
    ("/picked/ab/slide.tif", lambda: None),             # the page would read another stack
])
def test_anything_else_is_left_to_python(website, source, spec):
    page = Page()
    browser.set_asker(page)
    assert _build(source=source, spec=spec)[0] is None
    assert page.asked == []


def test_off_the_website_it_never_asks(monkeypatch):
    monkeypatch.delenv("PLEXORA_BACKEND", raising=False)
    browser.set_asker(lambda kind, args: pytest.fail("asked the page"))
    try:
        assert _build(spec=lambda: pytest.fail("built a spec"))[0] is None
    finally:
        browser.set_asker(None)
