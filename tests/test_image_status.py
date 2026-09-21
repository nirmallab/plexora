"""Whether a project's image can be read, and which way it fails when it cannot.

Every way an image can be unreadable used to reach the browser identically: a
500 per tile, or -- when the load failed before any tile was asked for --
nothing at all, and a blank canvas. The three causes want three different
sentences, because they want three different actions. A file that moved is
repointed; one this process may not read is a permissions or mount problem; one
whose bytes are not an image is re-exported.

The load stays LOUD. `data_model.load_datasource` still raises on a bad image,
for the reason recorded beside it: a project whose image has gone has nothing to
draw and no coordinate space to draw it in, and opening it onto an empty viewer
is a worse answer than saying so. What is added is a RECORD of which failure it
was, taken on the way past and re-raised unchanged.
"""

import dataclasses
import os

import numpy as np
import pytest
import tifffile

import plexora
from plexora.server.models import data_model
from plexora.server.models.project import Project
from plexora.server import providers


def _image(tmp_path, name="slide.ome.tif", channels=("DAPI", "CD3")):
    """A small pyramidal-enough stack.

    512px because `load_datasource` walks down to the last level with every
    dimension >= 200; anything smaller never loads at all and would fail this
    file's healthy case for the wrong reason.
    """
    path = tmp_path / name
    rng = np.random.default_rng(4)
    planes = rng.integers(0, 4000, size=(len(channels), 512, 512), dtype=np.uint16)
    tifffile.imwrite(
        path, planes,
        metadata={"axes": "CYX", "Channel": {"Name": list(channels)}},
        photometric="minisblack",
    )
    return path


@pytest.fixture(autouse=True)
def _forget_failures():
    """Each test gets a clean record. The module remembers a failure for a few
    seconds so a burst of failing tiles asks once, and that memory would
    otherwise leak between tests."""
    data_model._image_failures.clear()
    yield
    data_model._image_failures.clear()


# -- the classifier, with no file in sight ---------------------------------

@pytest.mark.parametrize("exc,expected", [
    (FileNotFoundError("no such file"), "missing"),
    (PermissionError("denied"), "inaccessible"),
    (tifffile.TiffFileError("not a TIFF file"), "corrupt"),
    (ValueError("garbage"), "corrupt"),
    (OSError("something else"), "corrupt"),
    (providers.ResourceUnavailable("node asleep"), "unavailable"),
])
def test_each_way_an_image_fails_is_told_apart(exc, expected):
    status, _ = data_model.classify_image_error(exc)
    assert status == expected


def test_a_truncated_tiff_is_corrupt_and_not_merely_a_value_error():
    """`TiffFileError` subclasses `ValueError`, so a classifier that special-
    cased ValueError would have to get the order right. It does not special-case
    it at all -- "anything else" IS the corrupt case -- and this pins that."""
    assert issubclass(tifffile.TiffFileError, ValueError)
    status, _ = data_model.classify_image_error(tifffile.TiffFileError("x"))
    assert status == "corrupt"


def test_the_detail_is_one_line_somebody_can_read():
    _, detail = data_model.classify_image_error(
        FileNotFoundError("missing.tif\nstack frame\nanother frame"))
    assert "\n" not in detail
    assert detail.startswith("missing.tif")


def test_an_exception_with_no_message_still_names_itself():
    _, detail = data_model.classify_image_error(ValueError())
    assert detail == "ValueError"


# -- against real files ----------------------------------------------------

def test_an_image_that_opens_is_ok(tmp_path):
    plexora.create_project(str(_image(tmp_path)), name="healthy")
    assert data_model.image_status("healthy")["status"] == "ok"


def test_a_deleted_image_is_missing_and_names_the_file(tmp_path, monkeypatch):
    """A project whose image is no longer where the record says it is.

    The record is pointed at a path that was never written, rather than a real
    file being unlinked: registering a project opens the image to read its
    geometry, and Windows keeps an open file locked, so the unlink is not
    something this test can rely on being allowed to do. What is under test is
    what `image_status` makes of a recorded path with nothing at the end of it,
    which is the same question either way -- and
    `test_a_path_that_is_not_there_at_all_needs_no_loader` covers the real
    filesystem check with no project in the way.
    """
    src = _image(tmp_path)
    plexora.create_project(str(src), name="gone")
    real = Project.load("gone")
    moved = dataclasses.replace(
        real, image=dataclasses.replace(
            real.image, src=str(tmp_path / "taken-away.ome.tif")))
    monkeypatch.setattr(Project, "load", staticmethod(
        lambda name, *a, **k: moved if name == "gone" else real))

    report = data_model.image_status("gone")
    assert report["status"] == "missing"
    assert report["src"].endswith("taken-away.ome.tif"), "the message names the file"


def test_a_truncated_image_is_corrupt(tmp_path):
    src = _image(tmp_path)
    plexora.create_project(str(src), name="trunc")
    with open(src, "r+b") as handle:
        handle.truncate(100)
    assert data_model.image_status("trunc")["status"] == "corrupt"


def test_a_zero_byte_image_is_corrupt(tmp_path):
    src = _image(tmp_path)
    plexora.create_project(str(src), name="emptied")
    with open(src, "r+b") as handle:
        handle.truncate(0)
    report = data_model.image_status("emptied")
    assert report["status"] == "corrupt"
    assert report["detail"], "and says what the reader made of it"


@pytest.mark.skipif(os.name == "nt",
                    reason="chmod does not block reads on NTFS")
def test_an_unreadable_image_is_inaccessible(tmp_path):
    src = _image(tmp_path)
    plexora.create_project(str(src), name="locked")
    os.chmod(src, 0)
    try:
        assert data_model.image_status("locked")["status"] == "inaccessible"
    finally:
        os.chmod(src, 0o644)


def test_a_file_that_goes_missing_after_it_loaded_is_still_caught(
        tmp_path, monkeypatch):
    """The case the stat-before-load ordering exists for.

    `load_datasource` returns immediately when the project asked about is
    already the loaded one, so a probe that reached for the loader first would
    answer "ok" for a file deleted since -- which is exactly the mid-session
    case, where a burst of failing tiles is what triggers the ask.

    The file is swapped in the RECORD rather than deleted on disk: Windows
    keeps the image open for as long as the project is loaded, so the real
    deletion cannot be performed here, and what is under test is the order the
    two questions are asked in rather than the unlink.
    """
    src = _image(tmp_path)
    plexora.create_project(str(src), name="vanishes")
    data_model.load_datasource("vanishes")
    assert data_model._loaded_source == data_model.loaded_scope("vanishes")

    real = Project.load("vanishes")
    moved = dataclasses.replace(
        real, image=dataclasses.replace(
            real.image, src=str(tmp_path / "moved-away.tif")))
    monkeypatch.setattr(Project, "load", staticmethod(
        lambda name, *a, **k: moved if name == "vanishes" else real))

    assert data_model.image_status("vanishes")["status"] == "missing"


def test_a_directory_image_is_read_by_listing_it(tmp_path):
    """An OME-Zarr store and a Xenium morphology folder are both directories,
    and a readability test that opened them as files would call every one of
    them corrupt."""
    store = tmp_path / "store.zarr"
    store.mkdir()
    assert data_model._stat_image(str(store)) is None


def test_a_path_that_is_not_there_at_all_needs_no_loader():
    assert data_model._stat_image(str("/no/such/path.tif"))[0] == "missing"


def test_loading_a_broken_image_still_raises(tmp_path):
    """The loud path is unchanged. Classifying happens on the way past; the
    exception carries on out of load_datasource exactly as it did."""
    src = _image(tmp_path)
    plexora.create_project(str(src), name="loud")
    with open(src, "r+b") as handle:
        handle.truncate(0)
    with pytest.raises(Exception):
        data_model.load_datasource("loud", reload=True)


def test_and_records_why_on_its_way_out(tmp_path):
    src = _image(tmp_path)
    plexora.create_project(str(src), name="recorded")
    with open(src, "r+b") as handle:
        handle.truncate(0)
    with pytest.raises(Exception):
        data_model.load_datasource("recorded", reload=True)
    remembered = data_model._image_failures[data_model.loaded_scope("recorded")]
    assert remembered["status"] == "corrupt"
    assert remembered["src"].endswith("slide.ome.tif")


def test_a_healthy_load_clears_a_previous_failure(tmp_path):
    """So a repointed sample stops being reported as broken without a restart."""
    src = _image(tmp_path)
    plexora.create_project(str(src), name="repaired")
    data_model._record_image_failure("repaired", "missing", "gone", src)
    data_model.load_datasource("repaired", reload=True)
    assert data_model.loaded_scope("repaired") not in data_model._image_failures


# -- the route -------------------------------------------------------------

def test_the_route_answers_for_a_real_project(tmp_path):
    plexora.create_project(str(_image(tmp_path)), name="served")
    client = plexora.app.test_client()
    response = client.get("/image_status?datasource=served")
    assert response.status_code == 200
    body = response.get_json()
    assert body["success"] is True
    assert body["status"] == "ok"


def test_the_route_404s_for_a_project_that_does_not_exist():
    client = plexora.app.test_client()
    assert client.get("/image_status?datasource=nope").status_code == 404


def test_the_route_reports_a_broken_image_rather_than_500ing(tmp_path):
    """The whole point: the browser gets an answer it can put on the canvas,
    not the 500 that every tile is already returning."""
    src = _image(tmp_path)
    plexora.create_project(str(src), name="broken")
    with open(src, "r+b") as handle:
        handle.truncate(0)
    client = plexora.app.test_client()
    response = client.get("/image_status?datasource=broken")
    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "corrupt"
    assert body["src"].endswith("slide.ome.tif")
