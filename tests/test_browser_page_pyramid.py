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
        assert kind == "pyramid_status" and args["id"] == 7
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
    monkeypatch.setattr(_work, "pause", lambda seconds: waits.append(seconds))
    # Nothing queued in Python is what the page's build waits on.
    monkeypatch.setattr(_work, "wait", lambda seconds: pytest.fail("ran queued work while waiting on the page"))
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


def test_a_build_whose_page_went_away_is_asked_of_the_page_there_now(website):
    """The tab reloaded and kept its Python (one per tab, kept across a
    reload): the page that was building the copy is gone, and the one there
    now does not know the build. It is asked to build it again, a few times
    at most."""
    class Reloaded(Page):
        def __init__(self, losses):
            super().__init__(looks=2)
            self.losses = losses

        def __call__(self, kind, args):
            if kind == "pyramid_status" and self.losses:
                self.asked.append((kind, args))
                self.losses -= 1
                return {"state": "lost", "error": "no such build in this page"}
            return super().__call__(kind, args)

    page = Reloaded(losses=1)
    browser.set_asker(page)
    path, _ = _build()
    assert path == "/derived/k/s.pyramid.ome.tiff"
    assert [k for k, _ in page.asked] == ["pyramid", "pyramid_status", "pyramid", "pyramid_status", "pyramid_status"]
    # The look itself answered with nothing (the page went while it waited).
    class Vanished(Reloaded):
        def __call__(self, kind, args):
            if kind == "pyramid_status" and self.losses:
                self.asked.append((kind, args))
                self.losses -= 1
                return None
            return super().__call__(kind, args)

    browser.set_asker(Vanished(losses=1))
    assert _build()[0] == "/derived/k/s.pyramid.ome.tiff"
    # A page that keeps losing it: said, not asked for ever.
    browser.set_asker(Reloaded(losses=10))
    with pytest.raises(PyramidError, match="no such build in this page"):
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


# -- a picked flat mask's label pyramid ---------------------------------------
#
# `data_model.resolve_outline_segmentation` asks the page the same way for a
# flat mask's filled label pyramid (`segmentation_pyramid.page_build`, the
# file `pyramidize_segmentation_mask` writes; tests/test_tiff_writer.py holds
# the page's file to it). Outline mode, anything off the website and anything
# the page would read differently stay Python's.

LABELS = {"kind": "labels", "shape": [1, 4000, 5000], "dtype": "uint32", "factors": [1, 2, 4, 8],
          "tile": 1024, "description": "<?xml ...?><OME/>", "fingerprint": "123-456"}


def test_a_mask_is_asked_for_as_labels_and_kept_apart_from_an_image(website):
    asked = {}
    for kind, spec in (("image", SPEC), ("labels", LABELS)):
        page = Page(looks=1)
        browser.set_asker(page)
        _build(spec=lambda s=spec: dict(s))
        asked[kind] = page.asked[0][1]
    assert asked["image"]["kind"] == "image" and asked["labels"]["kind"] == "labels"
    assert asked["labels"]["shape"] == [1, 4000, 5000]
    # The same file, the same moment: two pyramids, two places.
    assert asked["image"]["key"] != asked["labels"]["key"]


@pytest.mark.parametrize("judged, built_in_browser", [(True, False), (False, True)])
def test_a_file_already_there_is_judged_by_the_callers_rule(website, judged, built_in_browser):
    """The page hands back a file already under the pyramid's name; the
    caller's `adoptable` (a mask's own rule, not an image's) decides."""
    answers = iter([{"id": 7}, {"state": "done", "existing": True, "path": "/derived/k/m.labels.pyramid.ome.tiff"},
                    {"id": 8}, {"state": "done", "path": "/derived/k/m.labels.pyramid.ome.tiff"}])
    asked = []

    def page(kind, args):
        asked.append((kind, args))
        return next(answers)

    browser.set_asker(page)
    judged_paths = []
    path = browser.page_pyramid(
        "/picked/ab/m.tif", name="m.labels.pyramid.ome.tiff", spec=lambda: dict(LABELS),
        adoptable=lambda found: judged_paths.append(found) or judged)
    assert path == "/derived/k/m.labels.pyramid.ome.tiff"
    assert judged_paths == ["/derived/k/m.labels.pyramid.ome.tiff"]
    rebuilt = [args for kind, args in asked if kind == "pyramid" and args.get("browser")]
    assert bool(rebuilt) is built_in_browser


def test_a_mask_the_page_cannot_finish_says_so(website):
    browser.set_asker(Page(looks=1, end="failed"))
    with pytest.raises(PyramidError, match="could not build the mask's pyramid"):
        _build(spec=lambda: dict(LABELS))


@pytest.fixture
def conversion(monkeypatch):
    """`resolve_outline_segmentation` up to its two ways of making the
    pyramid: the page's, and Python's (`pyramidize_segmentation_mask`, here a
    stand-in that says it ran)."""
    from plexora.server.models import data_model
    from plexora.server.utils import segmentation_pyramid

    ran = {"python": [], "specs": [], "judged": []}
    monkeypatch.setattr(data_model, "_resolve_label_volume", lambda *a, **k: None)
    monkeypatch.setattr(data_model, "_servable_as_is", lambda *a, **k: False)
    monkeypatch.setattr(segmentation_pyramid, "page_build",
                        lambda source, **k: ran["specs"].append(source) or dict(LABELS))
    monkeypatch.setattr(segmentation_pyramid, "resolve_derived_mask",
                        lambda source, directory, mode: segmentation_pyramid.DerivedMask(
                            None, segmentation_pyramid.derived_output_path(source, directory, mode=mode), True))
    monkeypatch.setattr(segmentation_pyramid, "pyramidize_segmentation_mask",
                        lambda source, target, **k: ran["python"].append((source, k["outline"])) or str(target))
    monkeypatch.setattr(segmentation_pyramid, "is_adoptable_mask",
                        lambda found, source, mode: ran["judged"].append((found, source, mode)) or True)
    return data_model, segmentation_pyramid, ran


def test_the_import_asks_the_page_for_a_picked_flat_mask(website, conversion):
    data_model, segmentation_pyramid, ran = conversion
    page = Page(looks=2)
    browser.set_asker(page)
    progress, stages = [], []
    path = data_model.resolve_outline_segmentation(
        "/picked/ab/cells.ome.tif", "/data/derived/s",
        progress_callback=lambda done, total: progress.append((done, total)),
        mode=segmentation_pyramid.MODE_FILLED,
        # A mask's stage callback takes the key alone.
        stage_callback=lambda key: stages.append(key))
    assert path == "/derived/k/s.pyramid.ome.tiff"  # what the page answered
    kind, args = page.asked[0]
    assert kind == "pyramid" and args["kind"] == "labels"
    assert args["path"] == "/picked/ab/cells.ome.tif"
    assert args["name"] == "cells" + segmentation_pyramid.FILLED_SUFFIX
    assert ran["specs"] == ["/picked/ab/cells.ome.tif"] and ran["python"] == []
    assert progress == [(10, 40), (40, 40)]
    assert stages == ["inspecting", "building", "writing"]


def test_a_mask_file_already_there_is_judged_as_a_mask(website, conversion):
    data_model, segmentation_pyramid, ran = conversion
    answers = iter([{"id": 7}, {"state": "done", "existing": True, "path": "/derived/k/c.labels.pyramid.ome.tiff"}])
    browser.set_asker(lambda kind, args: next(answers))
    path = data_model.resolve_outline_segmentation("/picked/ab/c.tif", "/data/derived/s",
                                                   mode=segmentation_pyramid.MODE_FILLED)
    assert path == "/derived/k/c.labels.pyramid.ome.tiff"
    assert ran["judged"] == [("/derived/k/c.labels.pyramid.ome.tiff", "/picked/ab/c.tif",
                              segmentation_pyramid.MODE_FILLED)]


def test_outlines_are_left_to_python(website, conversion):
    data_model, segmentation_pyramid, ran = conversion
    browser.set_asker(lambda kind, args: pytest.fail("asked the page"))
    data_model.resolve_outline_segmentation("/picked/ab/c.tif", "/data/derived/s",
                                            mode=segmentation_pyramid.MODE_OUTLINES)
    assert ran["python"] == [("/picked/ab/c.tif", True)]


def test_a_mask_the_page_would_read_differently_is_left_to_python(website, conversion, monkeypatch):
    data_model, segmentation_pyramid, ran = conversion
    monkeypatch.setattr(segmentation_pyramid, "page_build", lambda source, **k: None)
    page = Page()
    browser.set_asker(page)
    data_model.resolve_outline_segmentation("/picked/ab/c.tif", "/data/derived/s",
                                            mode=segmentation_pyramid.MODE_FILLED)
    assert page.asked == []
    assert ran["python"] == [("/picked/ab/c.tif", False)]


def test_a_mask_off_the_website_is_pythons(monkeypatch, conversion):
    data_model, segmentation_pyramid, ran = conversion
    monkeypatch.delenv("PLEXORA_BACKEND", raising=False)
    browser.set_asker(lambda kind, args: pytest.fail("asked the page"))
    try:
        data_model.resolve_outline_segmentation("C:/data/c.tif", "C:/data/derived/s",
                                                mode=segmentation_pyramid.MODE_FILLED)
    finally:
        browser.set_asker(None)
    assert ran["specs"] == [] and ran["python"] == [("C:/data/c.tif", False)]


@pytest.fixture
def mask_job(monkeypatch, tmp_path):
    """`start_segmentation_job` with the conversion stood in: it raises
    `PageBuilding` for its first `building` runs, as `page_pyramid(hold=False)`
    does while the page is at it, then answers the page's path."""
    from plexora.server.models import data_model

    seen = {"holds": [], "patched": []}

    def use(building):
        def convert(label_file, **kwargs):
            seen["holds"].append(data_model._page_holds.get())
            if len(seen["holds"]) <= building:
                kwargs["progress_callback"](len(seen["holds"]), building + 1)
                raise browser.PageBuilding(len(seen["holds"]), building + 1)
            return {"segmentation": "/derived/k/cells.labels.pyramid.ome.tiff"}

        monkeypatch.setattr(data_model, "convertOmeTiff", convert)
        return seen

    monkeypatch.setattr(data_model, "_label_geometry_for", lambda *a: None)
    monkeypatch.setattr(data_model, "_image_levels_for", lambda *a: None)
    monkeypatch.setattr(data_model, "describe_segmentation_work", lambda *a: "Building")
    monkeypatch.setattr(data_model, "_attached_mask",
                        lambda name, config_file: ("/picked/ab/cells.tif", "pending", "filled"))
    monkeypatch.setattr(data_model, "_patch_config_segmentation",
                        lambda name, path, status, **k: seen["patched"].append((path, status)) or True)
    monkeypatch.setattr(data_model.Project, "config_path_for", lambda name: tmp_path / "config.json")
    yield use
    data_model._segmentation_jobs.pop("s", None)
    _work._reset_for_tests()


def test_a_mask_job_gives_the_worker_back_while_the_page_builds(monkeypatch, mask_job):
    """Without threads, each look at the page's build is a task of its own,
    and requests are answered between them; the job holds its lock (no second
    job starts) until the pyramid is there."""
    from plexora.server.models import data_model

    monkeypatch.setenv("PLEXORA_WORK", "inline")
    _work._reset_for_tests()
    seen = mask_job(building=2)
    data_model.start_segmentation_job("s", "/picked/ab/cells.tif", "/data/derived/s")
    ran = []
    while _work.run_pending(budget_s=0, ran=ran):
        if len(ran) < 3:
            assert data_model._segmentation_jobs["s"]["status"] == "pending"
            assert not data_model._segmentation_job_lock_for("s").acquire(blocking=False)
    assert ran == ["segmentation-s"] * 3
    assert seen["holds"] == [False] * 3  # never waits inside a run
    assert seen["patched"] == [("/derived/k/cells.labels.pyramid.ome.tiff", "ready")]
    record = data_model._segmentation_jobs["s"]
    assert record["status"] == "ready" and record["segmentation"].startswith("/derived/")
    lock = data_model._segmentation_job_lock_for("s")
    assert lock.acquire(blocking=False)
    lock.release()


def test_on_threads_a_mask_job_waits_where_it_is(monkeypatch, mask_job):
    import threading

    from plexora.server.models import data_model

    monkeypatch.setenv("PLEXORA_WORK", "threads")
    seen = mask_job(building=0)
    data_model.start_segmentation_job("s", "/picked/ab/cells.tif", "/data/derived/s")
    for thread in threading.enumerate():
        if thread.name == "segmentation-s":
            thread.join(10)
    assert seen["holds"] == [True]
    assert data_model._segmentation_jobs["s"]["status"] == "ready"
