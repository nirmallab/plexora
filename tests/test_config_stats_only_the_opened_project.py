"""/config looks at the files of the project being opened, and no other's.

A tile's address carries its file's content version (`c=`), which /config
works out from the image's and the mask's size and mtime
(datasource.tile_content_version). /config answers for every project the user
has, on every viewer boot; a stat of a file on a share that is slow or gone
can take seconds, so only the project a viewer asks about (`?datasource=`,
main.js's boot) is looked at -- and its version is the one its tiles are kept
for a year under.
"""

import os
import time

import numpy as np
import tifffile

import plexora
from plexora import datasource
from plexora.server.models import data_model
from tests.helpers import use_data_root

_DATA_MODEL_GLOBALS = (
    "source", "_loaded_source", "config", "channels", "seg", "zarray",
    "datasource", "ball_tree", "metadata",
)


def _own_data_model(monkeypatch):
    for name in _DATA_MODEL_GLOBALS:
        if hasattr(data_model, name):
            monkeypatch.setattr(data_model, name, None)
    monkeypatch.setattr(data_model, "_gmm_cache", {})
    monkeypatch.setattr(data_model, "_image_stats_cache", {})


def _write_image(path, seed):
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    tifffile.imwrite(path, rng.integers(1, 4000, size=(2, 512, 512), dtype=np.uint16))
    return path


def _write_mask(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    labels = np.zeros((512, 512), dtype=np.uint32)
    labels[8:48, 8:48] = 1
    labels[100:140, 100:140] = 2
    tifffile.imwrite(path, labels)
    return path


def _wait_for_mask(name, deadline_s=120):
    for _ in range(int(deadline_s * 10)):
        record = data_model._segmentation_jobs.get(name)
        if record is None or record.get("status") != "pending":
            return record
        time.sleep(0.1)
    raise AssertionError(f"mask conversion for {name} never finished")


def _norm(path):
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def test_config_stats_only_the_project_being_opened(tmp_path, monkeypatch):
    from plexora.server.routes.import_routes import attach_segmentation

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    use_data_root(monkeypatch, data_dir)
    _own_data_model(monkeypatch)
    client = plexora.app.test_client()

    opened_image = _write_image(tmp_path / "opened" / "slide.tif", seed=1)
    other_image = _write_image(tmp_path / "other" / "slide.tif", seed=2)
    other_mask = _write_mask(tmp_path / "other" / "cell.tif")
    datasource.register_image_datasource(name="cfg_opened", image=opened_image,
                                         data_dir=data_dir)
    datasource.register_image_datasource(name="cfg_other", image=other_image,
                                         data_dir=data_dir)
    attach_segmentation("cfg_other", str(other_mask))
    record = _wait_for_mask("cfg_other")
    assert record is None or record.get("status") != "error", record
    other_entry = client.get("/config").get_json()["cfg_other"]
    others = {_norm(other_image), _norm(other_mask)}
    for field in ("channelFile", "segmentation", "segmentationSource"):
        if isinstance(other_entry.get(field), str) and other_entry[field]:
            others.add(_norm(other_entry[field]))

    statted = []
    real_stat = os.stat

    def counting_stat(path, *args, **kwargs):
        try:
            statted.append(_norm(path))
        except TypeError:
            pass
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", counting_stat)

    # A boot's /config: no project named, no file looked at, no version given.
    plain = client.get("/config").get_json()
    assert not others & set(statted), sorted(others & set(statted))
    assert _norm(opened_image) not in statted
    assert all("version" not in channel for entry in plain.values()
               for channel in entry.get("imageData") or ())

    # The viewer opening one project: that project's files only.
    statted.clear()
    answer = client.get("/config?datasource=cfg_opened").get_json()
    assert not others & set(statted), (
        "/config stats a project nobody is opening: "
        f"{sorted(others & set(statted))}")
    assert all("version" not in channel for channel in answer["cfg_other"]["imageData"])
    version = answer["cfg_opened"]["imageData"][0]["version"]
    assert version
    monkeypatch.setattr(os, "stat", real_stat)

    # ...and that version is the one the opened project's tiles are kept under.
    data_model.load_datasource("cfg_opened", reload=True)
    src = answer["cfg_opened"]["imageData"][0]["src"]
    tile = client.get(f"{src}0/0_0.png?c={version}")
    assert tile.status_code == 200
    assert tile.headers["Cache-Control"] == "private, max-age=31536000"
    assert client.get("/config?datasource=cfg_opened").get_json()[
        "cfg_opened"]["imageData"][0]["version"] == version
