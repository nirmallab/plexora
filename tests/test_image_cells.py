"""Cells found in the image itself (`agent/evidence/image_cells.py`).

An auto-contrast check shows a marker's own cells, banded on their values.
A project with no cell table has no rows to band, so its cells are found in
the pixels -- the mask's labels, else the nuclear stain's nuclei -- and each
is ranked by its disc mean. Checked against the synthetic scene, whose cells
and values are known (`tests/autogate_fixtures.py`).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pytest

from plexora.agent import AgentSession, registry
from plexora.agent.evidence import image_cells
from tests.autogate_fixtures import make_gating_project

#: The estimate and the calibration run under the gating plugin's Pro
#: capabilities, so a test licence is installed.
pytestmark = pytest.mark.pro


@pytest.fixture(autouse=True)
def _gating():
    registry.discover(["gating"])


def _keys(session, name):
    from plexora.agent.render import resolve_channel
    from plexora.server.utils import source_image

    channels = list(session.project(name).image.real_channels)
    keys = {}
    for channel in channels:
        label = channel.get("fullname") or channel.get("name")
        _index, found = resolve_channel(label, channels)
        keys[label] = source_image.channel_key(found)
    return keys


def _ranks(values):
    return np.argsort(np.argsort(np.asarray(values)))


def test_a_mask_label_is_a_cell_at_its_centroid_unless_the_field_cuts_it():
    labels = np.zeros((40, 40), dtype=np.uint32)
    labels[10:16, 10:16] = 7          # whole
    labels[0:5, 30:36] = 9            # cut by the field's top edge
    labels[30, 5] = 11                # a sliver, not a cell
    ids, xs, ys = image_cells.mask_cells(labels, radius=3.0)
    assert ids.tolist() == [7]
    assert (xs[0], ys[0]) == (12.5, 12.5)


def test_a_flat_topped_nucleus_is_one_peak():
    rng = np.random.default_rng(0)
    plane = rng.normal(100, 3, (120, 120))
    yy, xx = np.mgrid[:120, :120]
    centres = [(30, 30), (30, 80), (85, 50)]
    for cy, cx in centres:
        plane[(yy - cy) ** 2 + (xx - cx) ** 2 <= 12 ** 2] = 3000
    xs, ys = image_cells.nuclei(plane, radius=5.0)
    assert len(xs) == 3
    for cy, cx in centres:
        assert np.hypot(xs - cx, ys - cy).min() < 1.0


def test_fields_are_tissue_tiles_spread_out_and_never_overlapping():
    tissue = np.zeros((64, 64), dtype=bool)
    tissue[:, :32] = True                                 # the left half, at level 6
    boxes = image_cells.fields((4096, 4096), 0, tissue, 6, seed=1)
    assert len(boxes) == 16
    assert all(box[2] <= 2048 for box in boxes)
    assert len({box[1] for box in boxes}) >= 6            # down the whole height
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            assert a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]
    assert image_cells.fields((4096, 4096), 0, tissue, 6, seed=1) == boxes


@pytest.mark.parametrize("mask", [True, False])
def test_the_scene_cells_are_found_where_and_as_bright_as_painted(tmp_path, mask):
    info = make_gating_project(tmp_path, table=False, mask=mask)
    session = AgentSession()
    found = image_cells.sample(session, "gsynth", _keys(session, "gsynth"), nuclear="DNA")
    assert found["source"] == ("mask" if mask else "nuclei")
    truth = np.array([[c["x"], c["y"]] for c in info["cells"]])
    got = np.stack([found["xs"], found["ys"]], axis=1)
    # Every cell but those a field's edge cuts (this scene's cells are twice
    # a typical cell's size, so a 512 px field cuts a good few).
    assert got.shape[0] >= 0.8 * truth.shape[0]
    nearest = np.argmin(((got[:, None, :] - truth[None]) ** 2).sum(-1), axis=1)
    assert np.abs(got - truth[nearest]).max() < 1.0
    assert np.unique(nearest).size == nearest.size
    if mask:
        assert found["ids"].tolist() == [info["cells"][i]["id"] for i in nearest]
    for marker in ("CD3", "CD8", "CD20"):
        painted = [info["cells"][i][marker] for i in nearest]
        assert np.corrcoef(_ranks(painted), _ranks(found["values"][marker]))[0, 1] > 0.99


def test_an_image_with_neither_mask_nor_nuclear_stain_has_no_cells_to_find(tmp_path):
    make_gating_project(tmp_path, table=False, mask=False)
    session = AgentSession()
    keys = {k: v for k, v in _keys(session, "gsynth").items() if k != "DNA"}
    assert image_cells.sample(session, "gsynth", keys, nuclear=None) is None


@pytest.mark.parametrize("mask", [True, False])
def test_image_found_cells_anchor_where_the_table_does(tmp_path, mask):
    """The image-found cells set each marker's window where the table's rows
    do: the same bands, read in the same level-0 discs."""
    from plexora.agent.evidence import cell_window

    make_gating_project(tmp_path, name="gtable")
    make_gating_project(tmp_path, table=False, mask=mask)
    session = AgentSession()
    keys = _keys(session, "gsynth")
    from_table = cell_window.cell_windows(session, "gtable", keys, nuclear="DNA")
    # Never on its own: a render must not read fields of every channel.
    assert cell_window.cell_windows(session, "gsynth", keys, nuclear="DNA") == {}
    from_image = cell_window.cell_windows(session, "gsynth", keys, nuclear="DNA",
                                          from_image=True)
    assert set(from_image) == set(from_table)
    for marker in ("CD3", "CD8", "CD20"):
        assert from_image[marker]["cells_from"] == f"image:{'mask' if mask else 'nuclei'}"
        assert from_image[marker]["column"] is None
        # Other cells drawn from the same bands: close, not identical.
        for key in ("black", "noise_p99", "titre"):
            assert from_image[marker][key] == pytest.approx(from_table[marker][key], rel=0.05)


def test_the_launcher_quotes_auto_contrast_for_an_image_with_no_table(tmp_path):
    """`ai.balance`: the image's channels are the markers (the nuclear stain
    left out), one grid for three; gating still needs the table."""
    from plexora.ai.harness.capabilities import _units
    from plexora.ai.harness.decision import contrast_grids

    make_gating_project(tmp_path, table=False)
    call = SimpleNamespace(session=AgentSession())
    units, why, words = _units(call, "contrast", "gsynth", None)
    assert (units, why) == (contrast_grids(3), None) and "blocked" not in words
    units, why, _words = _units(call, "gating", "gsynth", None)
    assert units == 0 and "no cell table" in why


def test_what_a_check_saw_of_image_cells_goes_once_a_table_anchors_the_image(tmp_path):
    """A choice outlives a recomputation; what its check saw names cells the
    image found, which a table's rows are not -- dropped, the gain kept."""
    from plexora.agent.evidence import calibration

    make_gating_project(tmp_path, name="gtable")
    make_gating_project(tmp_path, table=False)
    session = AgentSession()
    record, _changed = calibration.calibrate(session, "gsynth", image_cells=True)
    assert record["cells_from"] == "image:mask"
    # Asked again, an image-anchored record stands.
    assert calibration.calibrate(session, "gsynth", image_cells=True) == (record, False)
    seen = {"pattern_seen": "membrane", "stain_location": "cell_surface", "dim": [],
            "bright": [{"id": "b1", "cell_id": 3, "x": 1.0, "y": 1.0, "value": 900.0,
                        "artifact": False}]}
    calibration.record_choices("gsynth", {"CD3": {
        "column": "c2", "gain": 0.8, "band": "negatives", "chosen_by": "vision",
        "observed": seen}})
    assert calibration.observed(calibration.load("gsynth"), "CD3")
    # The table arrives (the same scene's, registered under the image).
    path = tmp_path / "config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["gsynth"]["dataset"] = config["gtable"]["dataset"]
    path.write_text(json.dumps(config), encoding="utf-8")
    record, changed = calibration.calibrate(AgentSession(), "gsynth")
    assert changed and "cells_from" not in record
    assert record["choices"]["CD3"]["gain"] == 0.8
    assert calibration.observed(record, "CD3") is None
