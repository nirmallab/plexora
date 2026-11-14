"""An answer that is refused takes the rest of the same post down with it.

The requirements modal and the project edit page both post several answers at
once -- a data file and a mask, say -- and both say "nothing was saved" when
one of them is refused. That has to be true: the project record must be as it
was before the post, not hold the answers that happened to be applied before
the refused one. A record that names a table the server never loaded leaves
the viewer's /config describing one thing and the loaded datasource another.
"""

import numpy as np
import tifffile
from PIL import Image

from plexora.server.models import data_model
from plexora.server.models.project import Project
from tests.test_requirements_routes import _csv, client  # noqa: F401


def test_a_refused_mask_in_the_modal_leaves_the_data_file_unattached(
        client, tmp_path):  # noqa: F811
    """A good table and a JPEG mask in one modal post: the JPEG is refused,
    so the table must not be recorded either."""
    photo = tmp_path / "photo.jpg"
    Image.fromarray(np.zeros((64, 64, 3), np.uint8)).save(photo)
    assert Project.load("proj").dataset is None

    response = client.post("/proj/requirements", json={
        "data": str(_csv(tmp_path)), "segmentation": str(photo)})

    assert response.status_code == 400
    assert "JPEG" in response.get_json()["error"]
    after = Project.load("proj")
    assert after.dataset is None, (
        f"the post was refused, yet the project now names {after.dataset.src}")


def test_a_mistyped_data_path_on_the_edit_page_leaves_the_mask_unattached(
        client, tmp_path, monkeypatch):  # noqa: F811
    """A new mask and a data path that does not exist in one save: the save is
    refused, so the mask must not be recorded (nor its conversion started)."""
    started = []
    monkeypatch.setattr(data_model, "start_segmentation_job",
                        lambda *args, **kwargs: started.append(args))
    mask = tmp_path / "cells_mask.tif"
    labels = np.zeros((256, 256), np.uint16)
    labels[10:40, 10:40] = 1
    tifffile.imwrite(mask, labels)
    assert Project.load("proj").segmentation.source is None

    response = client.post("/project/proj", json={
        "segmentation": str(mask), "data": str(tmp_path / "missing" / "cells.csv")})

    assert response.status_code == 400
    assert "No such data file" in response.get_json()["error"]
    after = Project.load("proj")
    assert after.segmentation.source is None, (
        f"the save was refused, yet the project now names the mask "
        f"{after.segmentation.source}")
    assert started == [], "a refused save started the mask's conversion job"
