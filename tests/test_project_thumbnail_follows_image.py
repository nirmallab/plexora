"""The Open Project card's thumbnail is cached beside the project, keyed by
the image it was drawn from: a project whose image is replaced (re-exported
in place, or pointed at another file) gets a new card instead of the old
picture forever (audit lead flow-project-6)."""

import os

import numpy as np
import tifffile

from plexora import app, datasource, paths
from tests.helpers import use_data_root


def _write(path, value):
    plane = np.zeros((2, 256, 256), dtype=np.uint16)
    plane[:, :128] = value
    plane[:, 128:] = 7
    tifffile.imwrite(path, plane)


def test_a_replaced_image_gets_a_new_card_and_an_unchanged_one_is_served_from_the_cache(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    use_data_root(monkeypatch, data_dir)
    image = tmp_path / "slide.tif"
    _write(image, 60000)
    datasource.register_image_datasource(name="thumb", image=str(image), data_dir=data_dir)
    paths.derived_root("thumb").mkdir(parents=True, exist_ok=True)
    client = app.test_client()

    first = client.get("/project_thumbnail/thumb").data
    assert client.get("/project_thumbnail/thumb").data == first

    # The same path, other pixels (bright on the right now).
    plane = np.zeros((2, 256, 256), dtype=np.uint16)
    plane[:, 128:] = 60000
    tifffile.imwrite(image, plane)
    stat = os.stat(image)
    os.utime(image, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    second = client.get("/project_thumbnail/thumb").data
    assert second and second != first
    assert client.get("/project_thumbnail/thumb").data == second
