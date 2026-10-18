"""Choosing fields to check a gate in: deterministic, and honest per field."""

import numpy as np
import pytest

from plexora.agent.gate_sampling import sample_gate_validation_regions
from tests.agent_fixtures import local_handles, make_synthetic_project


@pytest.fixture
def ds(tmp_path):
    make_synthetic_project(tmp_path)
    return local_handles("synth")


ALL = ("clear_negative", "clear_positive", "borderline", "high_density", "low_density",
       "bright_isolated", "edge", "weak", "bright", "sparse")


def _sample(ds, **kwargs):
    kwargs.setdefault("field_px", 128)
    kwargs.setdefault("image_size", (512, 512))
    return sample_gate_validation_regions(ds, "CD8", kwargs.pop("low", 500.0), 1e9, **kwargs)


def test_same_inputs_same_fields(ds):
    assert _sample(ds, classes=ALL) == _sample(ds, classes=ALL)


def test_fields_are_what_their_class_says(ds):
    # One-cell fields: with this grid any larger square holds all three groups.
    result = _sample(ds, classes=ALL, band=(400.0, 1000.0), field_px=64)
    by_class = {}
    for field in result["fields"]:
        by_class.setdefault(field["class"], []).append(field)
        b = field["bounds"]
        assert 0 <= b["x"] and b["x"] + b["width"] <= 512
        assert b["x"] % result["grid_step_px"] == 0
    assert all(f["positives"] == 0 for f in by_class["clear_negative"])
    assert all(f["positives"] > 0 for f in by_class["clear_positive"])
    assert all(f["borderline_below"] + f["borderline_above"] > 0 for f in by_class["borderline"])


def test_weak_and_bright_fields_show_dim_and_clear_stain(ds):
    # `weak`: band cells and no clear positive beside them; `bright`: a few
    # clear positives (the brightest such field).
    result = _sample(ds, classes=("weak", "bright"), band=(400.0, 1000.0), field_px=64)
    by_class = {f["class"]: f for f in result["fields"]}
    weak = by_class.get("weak")
    if weak is not None:
        assert weak["borderline_below"] + weak["borderline_above"] > 0
    bright = by_class.get("bright")
    if bright is not None:
        assert bright["positives"] >= 3


def test_stats_match_the_range_mask(ds):
    result = _sample(ds)
    x = ds.table.geometry()["X_centroid"].to_numpy()
    y = ds.table.geometry()["Y_centroid"].to_numpy()
    values = np.asarray(ds.table.columns(["CD8"])["CD8"])
    for field in result["fields"]:
        b = field["bounds"]
        inside = (x >= b["x"]) & (x < b["x"] + b["width"]) & (y >= b["y"]) \
            & (y < b["y"] + b["height"])
        assert field["cells"] == int(inside.sum())
        assert field["positives"] == int((inside & (values > 500) & (values <= 1e9)).sum())


def test_fields_do_not_overlap_much(ds):
    fields = _sample(ds, classes=ALL, n_per_class=4)["fields"]
    boxes = [(f["bounds"]["x"], f["bounds"]["y"], f["bounds"]["x"] + f["bounds"]["width"],
              f["bounds"]["y"] + f["bounds"]["height"]) for f in fields]
    from plexora.agent.gate_sampling import MAX_IOU, _iou

    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            assert _iou(boxes[i], boxes[j]) <= MAX_IOU


def test_max_iou_zero_shares_no_tissue_within_or_with_an_earlier_picture(ds):
    """lsp35766_p193 NGFR: a second absence picture of "other" regions sat
    half a field from the first one's, sharing a quarter of each."""
    from plexora.agent.gate_sampling import _iou

    def boxes(fields):
        return [(f["bounds"]["x"], f["bounds"]["y"], f["bounds"]["x"] + f["bounds"]["width"],
                 f["bounds"]["y"] + f["bounds"]["height"]) for f in fields]

    first = boxes(_sample(ds, classes=ALL, n_per_class=2, max_iou=0.0)["fields"])
    assert all(_iou(a, b) == 0 for i, a in enumerate(first) for b in first[i + 1:])
    second = boxes(_sample(ds, classes=ALL, n_per_class=2, max_iou=0.0, avoid=first)["fields"])
    assert all(_iou(a, b) == 0 for a in second for b in first)
    # The default still lets a field half a side away through.
    half = _sample(ds, classes=ALL, n_per_class=4)["fields"]
    assert len(half) >= len(_sample(ds, classes=ALL, n_per_class=4, max_iou=0.0)["fields"])


def test_unknown_class_is_refused(ds):
    from plexora.agent import AgentError

    with pytest.raises(AgentError):
        _sample(ds, classes=("nonsense",))
