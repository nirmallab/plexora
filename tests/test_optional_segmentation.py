"""End-to-end check that a datasource can be registered and loaded with no
segmentation mask at all -- requirements §3.1-3.3 require segmentation to be
optional, but data_model.py's load_datasource() used to crash unconditionally
on a missing/None config['segmentation'] (AttributeError/KeyError on
`.endswith(...)`), and datasource.py's register_datasource() had no way to
omit it. This test exercises the real registration -> load path end to end,
not just the isolated pieces, to catch any other place that assumes
segmentation is always present.
"""

import numpy as np
import polars as pl
import tifffile

from plexora import datasource
from plexora.server.models import data_model


def _write_image(path, size=256, channels=2):
    tifffile.imwrite(path, np.zeros((channels, size, size), dtype=np.uint8))


def _write_csv(path, count=8):
    df = pl.DataFrame(
        {
            "CellID": np.arange(count, dtype=np.uint32),
            "X_centroid": np.linspace(10, 200, count, dtype=np.float32),
            "Y_centroid": np.linspace(10, 200, count, dtype=np.float32),
            "MarkerA": np.linspace(0, 5, count, dtype=np.float32),
        }
    )
    df.write_csv(path)


def test_register_and_load_datasource_without_segmentation(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    image_path = tmp_path / "image.tif"
    csv_path = tmp_path / "cells.csv"
    _write_image(image_path)
    _write_csv(csv_path)

    entry = datasource.register_datasource(
        name="no_seg_sample",
        image=image_path,
        features=csv_path,
        x="X_centroid",
        y="Y_centroid",
        segmentation=None,
        data_dir=data_dir,
    )

    assert entry["segmentation"] is None
    assert all(channel["name"] != "Area" for channel in entry["imageData"])

    monkeypatch.setattr(data_model, "config_json_path", data_dir / "config.json")
    monkeypatch.setattr(data_model, "data_path", data_dir)

    data_model.load_datasource("no_seg_sample", reload=True)

    assert data_model.seg is None
    assert data_model.channels is not None
    assert data_model.datasource.height == 8
