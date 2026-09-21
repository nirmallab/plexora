"""Do a project's table, mask and image describe the same sample?

Every case here is one a real project has arrived in, and every one of them
produces a viewer that works. That is what makes the check worth having: a
table paired with the wrong image does not fail to load, a mask exported at
another resolution does not fail to draw. They quietly answer about something
else.

The subject is `consistency.report`, which is pure -- a project record, a
per-column summary and a mask size in, a list of findings out -- so each case
below is three small literals rather than a fixture on disk.
"""

import json

import numpy as np
import polars as pl
import pytest
import tifffile

import plexora
from plexora.server.models import consistency, data_model

from tests.helpers import csv_spec, entry, image_spec, project, use_data_root


def described(**columns):
    """The `dd` shape `data_model._describe_frame` produces, for the few
    columns these checks read. `count` is what marks a column as having
    anything in it at all."""
    return {
        name: {"count": count, "min": float(low), "max": float(high),
               "histogram": [{"x": low, "y": 1.0}]}
        for name, (count, low, high) in columns.items()
    }


def sample(*, width=1000, height=800, segmentation=None):
    """A project with a CSV whose roles are the usual three."""
    return project(
        "demo",
        dataset=csv_spec("/tmp/cells.csv", markers=("CD3",)),
        image=image_spec(width=width, height=height, with_area=bool(segmentation)),
        segmentation=segmentation,
    )


def codes(findings):
    return [finding["code"] for finding in findings]


# -- the ordinary answer ----------------------------------------------------

def test_a_table_that_fits_its_image_reports_nothing():
    """An empty list means the checks ran and found nothing, which is the
    answer for almost every project and has to stay cheap to reach."""
    report = consistency.report(
        sample(),
        described(CellID=(500, 1, 500), X_centroid=(500, 3.5, 995.0),
                  Y_centroid=(500, 2.0, 796.0)))

    assert report == []


def test_a_project_with_no_table_is_not_a_mismatch():
    """There is nothing to disagree with an image about. An image-only project
    is an ordinary project, not a broken one."""
    assert consistency.report(project("demo"), {}) == []


# -- the table against the image -------------------------------------------

def test_coordinates_past_the_edge_name_the_axis_and_both_numbers():
    """The commonest way to pair a table with the wrong image, and invisible
    once it happens: the cells that fall outside are simply never drawn, and
    the ones inside look fine.

    The message carries the two numbers because "past the edge" alone leaves
    the user to go and find both of them."""
    report = consistency.report(
        sample(width=1000, height=800),
        described(CellID=(500, 1, 500), X_centroid=(500, 3.0, 4310.0),
                  Y_centroid=(500, 2.0, 700.0)))

    assert codes(report) == ["cells_outside_image"]
    assert "4,310" in report[0]["message"] and "1,000" in report[0]["message"]
    assert "x reaches" in report[0]["message"]


def test_a_centroid_on_the_last_row_of_pixels_is_not_a_wrong_image():
    """Half a pixel of slop is rounding, not a different sample. Reporting it
    would fire on a correctly paired table whose rightmost cell happens to sit
    against the edge -- which is most whole-slide tables."""
    report = consistency.report(
        sample(width=1000, height=800),
        described(CellID=(500, 1, 500), X_centroid=(500, 0.0, 1000.0),
                  Y_centroid=(500, 0.0, 800.0)))

    assert report == []


def test_negative_coordinates_are_reported_too():
    report = consistency.report(
        sample(),
        described(CellID=(500, 1, 500), X_centroid=(500, -40.0, 900.0),
                  Y_centroid=(500, 5.0, 700.0)))

    assert codes(report) == ["cells_outside_image"]
    assert "negative" in report[0]["message"]


def test_coordinates_in_microns_look_like_one_scale_on_both_axes():
    """0.325 um/px is an ordinary scan, and a micron coordinate on one lands at
    about a third of its pixel address -- identically in x and y, because it is
    one scale factor and not a crop."""
    report = consistency.report(
        sample(width=10000, height=8000),
        described(CellID=(500, 1, 500), X_centroid=(500, 2.0, 3250.0),
                  Y_centroid=(500, 1.0, 2600.0)))

    assert codes(report) == ["cells_in_a_corner"]
    assert "microns" in report[0]["message"]


def test_a_crop_of_the_slide_is_not_reported_as_units():
    """A region quantified out of a whole slide genuinely covers part of it,
    and somebody meant to make it. The discriminator is shape: a crop is not
    the same fraction of both axes, and does not start at the origin."""
    report = consistency.report(
        sample(width=10000, height=8000),
        described(CellID=(500, 1, 500), X_centroid=(500, 4200.0, 5900.0),
                  Y_centroid=(500, 300.0, 5100.0)))

    assert report == []


def test_a_table_with_no_rows_for_this_image_says_so():
    """Named columns holding nothing: the image-id subset selected no rows, or
    every coordinate is null. The panel would otherwise simply stay empty and
    give no account of why."""
    report = consistency.report(
        sample(),
        described(CellID=(500, 1, 500), X_centroid=(0, 0, 0), Y_centroid=(0, 0, 0)))

    assert codes(report) == ["no_cells"]


# -- the table against the mask --------------------------------------------

def test_ids_starting_at_zero_are_reported_only_when_there_is_a_mask():
    """0 is background in every mask format there is, so a table whose ids
    start at 0 is one whose ids are row positions. With no mask there is
    nothing for an id to address and nothing to report."""
    description = described(CellID=(500, 0, 499), X_centroid=(500, 3.0, 900.0),
                            Y_centroid=(500, 2.0, 700.0))

    with_mask = consistency.report(sample(segmentation="/tmp/mask.tif"), description)
    without = consistency.report(sample(), description)

    assert codes(with_mask) == ["ids_below_one"]
    assert "row positions" in with_mask[0]["message"]
    assert without == []


def test_fractional_ids_cannot_name_a_label():
    report = consistency.report(
        sample(segmentation="/tmp/mask.tif"),
        described(CellID=(500, 1.5, 500.5), X_centroid=(500, 3.0, 900.0),
                  Y_centroid=(500, 2.0, 700.0)))

    assert codes(report) == ["ids_not_whole"]


# -- the mask against the image --------------------------------------------

def test_a_mask_of_another_size_is_the_hardest_one_to_see():
    """The viewer draws mask tiles in the IMAGE's coordinate system -- the mask
    layer is synthesized with the image's width and height -- so a mask of
    another size is never rejected. It is stretched over the wrong pixels and
    every outline sits a little off the cell it came from."""
    report = consistency.report(
        sample(width=1000, height=800, segmentation="/tmp/mask.tif"),
        described(CellID=(500, 1, 500), X_centroid=(500, 3.0, 900.0),
                  Y_centroid=(500, 2.0, 700.0)),
        mask_size=(500, 400))

    assert codes(report) == ["mask_size"]
    assert "500 x 400" in report[0]["message"]
    assert "1,000 x 800" in report[0]["message"]


def test_a_mask_that_matches_is_silent():
    report = consistency.report(
        sample(width=1000, height=800, segmentation="/tmp/mask.tif"),
        described(CellID=(500, 1, 500), X_centroid=(500, 3.0, 900.0),
                  Y_centroid=(500, 2.0, 700.0)),
        mask_size=(1000, 800))

    assert report == []


def test_a_mask_whose_size_could_not_be_read_is_not_reported_as_differing():
    """None means "cannot say" -- a mask on a data node, or a file that would
    not open. Reporting that as a mismatch would put a warning on every
    node-backed project."""
    report = consistency.report(
        sample(width=1000, height=800, segmentation="/tmp/mask.tif"),
        described(CellID=(500, 1, 500), X_centroid=(500, 3.0, 900.0),
                  Y_centroid=(500, 2.0, 700.0)),
        mask_size=None)

    assert report == []


# -- several at once --------------------------------------------------------

def test_findings_accumulate_and_the_mask_comes_first():
    """A project can be wrong in more than one way, and the mask's size is the
    one the user can do least about by squinting at the screen."""
    report = consistency.report(
        sample(width=1000, height=800, segmentation="/tmp/mask.tif"),
        described(CellID=(500, 0, 499), X_centroid=(500, 3.0, 4310.0),
                  Y_centroid=(500, 2.0, 700.0)),
        mask_size=(512, 512))

    assert codes(report) == ["mask_size", "cells_outside_image", "ids_below_one"]


def test_every_finding_is_a_code_and_a_sentence():
    """Nothing here is a bare code. The whole value of this module is the
    sentence -- a user shown `ids_below_one` has learned nothing -- and the
    code is for the panel, which keys its own behaviour off it."""
    report = consistency.report(
        sample(width=1000, height=800, segmentation="/tmp/mask.tif"),
        described(CellID=(500, 0, 499), X_centroid=(500, 3.0, 4310.0),
                  Y_centroid=(500, 2.0, 700.0)),
        mask_size=(512, 512))

    assert report
    for finding in report:
        assert set(finding) == {"code", "message"}
        assert finding["code"] and finding["message"].endswith(".")
        assert finding["code"] not in finding["message"]


# -- through the route ------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    """A project with a 256x256 image and nothing else. Same shape as
    test_requirements_routes' fixture, and unwound the same way -- answering a
    requirement reloads the datasource and leaves data_model's globals pointing
    at this test's config."""
    use_data_root(monkeypatch, tmp_path)
    for name in ("ball_tree", "source", "config", "seg", "zarray", "channels",
                 "metadata", "_loaded_source", "datasource"):
        if hasattr(data_model, name):
            monkeypatch.setattr(data_model, name, None)
    image = tmp_path / "image.ome.tif"
    # 256px is the floor load_datasource's pyramid walk can cope with.
    tifffile.imwrite(image, np.zeros((2, 256, 256), dtype=np.uint8))
    (tmp_path / "config.json").write_text(
        json.dumps({"proj": entry("proj", dataset=None, src=str(image),
                                  width=256, height=256)}),
        encoding="utf-8")
    return plexora.app.test_client()


def _cells(tmp_path, x_max):
    path = tmp_path / "cells.csv"
    pl.DataFrame({
        "CellID": np.arange(1, 5, dtype=np.uint32),
        "X_centroid": np.linspace(1.0, x_max, 4),
        "Y_centroid": np.linspace(1.0, 200.0, 4),
        "CD3": np.linspace(0.0, 3.0, 4),
    }).write_csv(path)
    return path


def test_the_route_reports_a_table_that_does_not_fit_its_image(client, tmp_path):
    """End to end, because the gathering half is where the wiring can be wrong:
    the project record, the per-column summary and the mask size are three
    different reads and the pure function never sees any of them directly."""
    client.post("/proj/requirements", json={"data": str(_cells(tmp_path, 4000.0))})

    report = client.get("/get_consistency_report?datasource=proj").get_json()

    assert [finding["code"] for finding in report] == ["cells_outside_image"]


def test_the_route_answers_an_empty_list_for_a_project_that_agrees(client, tmp_path):
    """The ordinary answer, and it has to be an array rather than a null or an
    error -- the client renders whatever comes back."""
    client.post("/proj/requirements", json={"data": str(_cells(tmp_path, 250.0))})

    assert client.get("/get_consistency_report?datasource=proj").get_json() == []
