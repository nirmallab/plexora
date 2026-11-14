"""A load that re-finds a boundary mask by name adopts it only for its frame.

`refresh_segmentation_mapping` backfills an entry whose drawn mask is gone, or
predates the stamps, by looking for one by name -- beside the run first. With
two samples of one run registered to different images, the file beside the
run is the other sample's drawing, and adopting it puts every cell where the
OTHER registration put it. It is adopted only when drawn for this project's
frame; otherwise the entry goes back to pending and the job redraws it.
"""

from pathlib import Path

import polars as pl

from plexora.server.models import data_model
from plexora.server.utils import boundary_mask, segmentation_pyramid


def _boundaries(path):
    pl.DataFrame({
        "cell_id": ["cell1-1"] * 4,
        "vertex_x": [10.0, 30.0, 30.0, 10.0],
        "vertex_y": [10.0, 10.0, 30.0, 30.0],
        "label_id": [1] * 4,
    }).write_parquet(path)
    return path


def _geometry(pixel_size):
    return {"width": 256, "height": 256, "tile_size": 64,
            "pixel_size": pixel_size}


def _setup(tmp_path, monkeypatch, pixel_size):
    run = tmp_path / "run"
    run.mkdir()
    table = _boundaries(run / "cell_boundaries.parquet")
    other = tmp_path / "derived" / "sample_a"
    other.mkdir(parents=True)
    # Sample A's drawing, beside the run, at 0.5 um/px.
    shared = boundary_mask.resolve_mask(table, other, geometry=_geometry(0.5))
    assert Path(shared) == segmentation_pyramid.derived_output_path(
        table, None, mode=segmentation_pyramid.MODE_FILLED)

    own = tmp_path / "derived" / "sample_b"
    own.mkdir(parents=True)
    monkeypatch.setattr(data_model.paths, "derived_root", lambda name: own)
    monkeypatch.setattr(data_model, "_label_geometry_for",
                        lambda name, label_file: _geometry(pixel_size))
    entry = {
        # Sample B's own drawing, deleted since.
        "segmentation": str(own / "gone.ome.tif"),
        "segmentationSource": str(table),
        "segmentationSourceKey": segmentation_pyramid.source_fingerprint(table),
        "segmentationMode": segmentation_pyramid.MODE_FILLED,
    }
    return entry, shared, table


def test_another_frames_mask_beside_the_run_is_not_adopted(tmp_path, monkeypatch):
    entry, shared, table = _setup(tmp_path, monkeypatch, pixel_size=0.25)

    changed, pending = data_model.refresh_segmentation_mapping(entry, "sample_b")

    assert changed
    assert entry["segmentation"] != str(shared)
    assert entry["segmentation_status"] == "pending"
    assert pending == str(table)


def test_this_frames_mask_beside_the_run_is_still_adopted(tmp_path, monkeypatch):
    entry, shared, _table = _setup(tmp_path, monkeypatch, pixel_size=0.5)

    changed, pending = data_model.refresh_segmentation_mapping(entry, "sample_b")

    assert changed
    assert entry["segmentation"] == str(shared)
    assert entry["segmentation_status"] == "ready"
    assert pending is None
