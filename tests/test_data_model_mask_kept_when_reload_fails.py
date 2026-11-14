"""A mask that finished converting stays attached when the reload after it fails.

`start_segmentation_job` converts the mask, records the derived pyramid on the
project ("ready"), and then reloads the open project so the viewer picks the
mask up. That reload re-opens the image and the table too, and can fail for a
reason that has nothing to do with the mask: an image on a share that is
momentarily unreadable, a file another program holds locked on Windows. The
conversion has already succeeded by then, so its record must not be rewritten
to "no mask, failed" with the image's error -- the next successful open would
otherwise have no mask, and the user would have to convert it again.
"""

import json
import threading

import numpy as np
import tifffile

from plexora.datasource import register_image_datasource
from plexora.server.models import data_model
from plexora.server.routes import import_routes

NAME = "mask_kept_reload_fails"


def _join_segmentation_threads():
    for thread in list(threading.enumerate()):
        if thread.name.startswith("segmentation-"):
            thread.join(120)


def test_a_converted_mask_stays_ready_when_the_reload_after_it_fails(tmp_path, monkeypatch):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    image = inputs / "image.tif"
    tifffile.imwrite(image, np.random.default_rng(0).integers(
        0, 4000, (2, 256, 256), dtype=np.uint16))
    mask = inputs / "mask.tif"
    labels = np.zeros((256, 256), dtype=np.uint32)
    labels[20:40, 20:40] = 1
    labels[100:130, 100:130] = 2
    tifffile.imwrite(mask, labels)

    register_image_datasource(NAME, image, channel_names=["DNA", "CD3"])
    data_model.load_datasource(NAME)

    real_load = data_model.load_datasource
    reloads_refused = []

    def image_locked_during_reload(name, reload=False, **kwargs):
        # The job's own reload of the open project: the image cannot be read
        # at that moment (another program holds it, a share hiccups).
        if reload and threading.current_thread().name.startswith("segmentation-"):
            reloads_refused.append(name)
            raise PermissionError(13, "The process cannot access the file", str(image))
        return real_load(name, reload=reload, **kwargs)

    monkeypatch.setattr(data_model, "load_datasource", image_locked_during_reload)
    import_routes.attach_segmentation(NAME, str(mask))
    _join_segmentation_threads()

    assert reloads_refused == [NAME], "the job never reached its reload of the open project"

    entry = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))[NAME]
    assert entry.get("segmentation"), (
        "the converted mask was taken off the project because the reload after "
        f"it failed: {entry.get('segmentation')!r}, status "
        f"{entry.get('segmentation_status')!r}")
    assert entry.get("segmentation_status") == "ready"
    assert entry.get("segmentationSource") == str(mask)

    status = data_model.get_segmentation_job_status(NAME)
    assert status["status"] == "ready", (
        "the mask is reported failed with the image's error: "
        f"{status['status']!r} {status.get('error')!r}")
