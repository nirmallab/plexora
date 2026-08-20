"""Tests for the recorded source -> derived-mask mapping in config.json.

Before this mapping existed, every datasource load called
ensure_outline_segmentation(), which re-sampled the mask's pixels to guess
whether outlines still needed generating -- work repeated on each load, and a
guess that was wrong on large masks. Loads now decide from a recorded
`segmentationSource`/`segmentationSourceKey` pair with a stat() instead.

The cases that matter:
  * a current mapping is left completely alone (no regeneration, no re-sniff),
  * a source that changed on disk is re-derived,
  * a legacy entry with neither key still resolves, and is backfilled,
  * a legacy entry whose "outline" file is really a filled mask (the old sniff
    bug) is corrected rather than trusted forever.
"""

import json
from pathlib import Path

import numpy as np
import tifffile

import plexora
from plexora.server.models import data_model
from plexora.server.utils import segmentation_pyramid as sp


def _touching_cells(height=400, width=600, cell=20, dtype=np.uint32):
    labels = np.zeros((height, width), dtype=dtype)
    next_id = 1
    for y in range(0, height - cell + 1, cell):
        for x in range(0, width - cell + 1, cell):
            labels[y:y + cell, x:x + cell] = next_id
            next_id += 1
    return labels


def _data_dir(tmp_path, monkeypatch, entry):
    """A data root with config.json holding one datasource entry, wired into
    data_model's module-level paths."""
    data_dir = tmp_path / "data"
    (data_dir / "sample").mkdir(parents=True)
    config_path = data_dir / "config.json"
    config_path.write_text(json.dumps({"sample": entry}), encoding="utf-8")
    monkeypatch.setattr(data_model, "config_json_path", config_path)
    monkeypatch.setattr(data_model, "data_path", data_dir)
    return data_dir, config_path


def _capture_jobs(monkeypatch):
    """Records (name, source, directory) per started job. The mode the job was
    given lands in `started.modes` so the existing assertions stay readable."""
    class _Started(list):
        """A plain list can't carry the extra attribute."""
        modes = ()

    started = _Started()
    started.modes = []

    def record(name, source, directory, mode=sp.DEFAULT_MODE):
        started.append((name, str(source), str(directory)))
        started.modes.append(mode)

    monkeypatch.setattr(data_model, "start_segmentation_job", record)
    return started


def _filled_mask(path):
    tifffile.imwrite(path, _touching_cells())
    return path


def test_a_current_mapping_is_reused_untouched(tmp_path, monkeypatch):
    source = _filled_mask(tmp_path / "mask.tiff")
    data_dir, config_path = _data_dir(tmp_path, monkeypatch, {"dataset": None})
    derived = sp.pyramidize_segmentation_mask(
        source, sp.outline_output_path(source, data_dir / "sample"), tile_size=256
    )
    entry = json.loads(config_path.read_text(encoding="utf-8"))["sample"]
    entry.update({
        "segmentation": derived,
        "segmentationSource": str(source),
        "segmentationSourceKey": sp.source_fingerprint(source),
    })
    config_path.write_text(json.dumps({"sample": entry}), encoding="utf-8")

    started = _capture_jobs(monkeypatch)
    before = Path(derived).stat()

    data_model.load_config("sample")

    assert started == []
    after = Path(derived).stat()
    # Byte-identical and untouched: the load did no conversion work at all.
    assert (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
    assert data_model.config["sample"]["segmentation"] == derived


def test_a_changed_source_triggers_regeneration(tmp_path, monkeypatch):
    source = _filled_mask(tmp_path / "mask.tiff")
    data_dir, config_path = _data_dir(tmp_path, monkeypatch, {"dataset": None})
    derived = sp.pyramidize_segmentation_mask(
        source, sp.outline_output_path(source, data_dir / "sample"), tile_size=256
    )
    entry = {
        "dataset": None,
        "segmentation": derived,
        "segmentationSource": str(source),
        "segmentationSourceKey": "0-0",  # deliberately stale
    }
    config_path.write_text(json.dumps({"sample": entry}), encoding="utf-8")

    started = _capture_jobs(monkeypatch)
    data_model.load_config("sample")

    assert started == [("sample", str(source), str(data_dir / "sample"))]
    assert data_model.config["sample"]["segmentation"] is None
    assert data_model.config["sample"]["segmentation_status"] == "pending"


def test_a_legacy_entry_pointing_at_a_generated_outline_is_adopted(tmp_path, monkeypatch):
    """Existing installs have no mapping keys. Their derived file is one of ours
    and nothing says the source moved on, so adopt it rather than spend a minute
    reproducing it -- and record the keys so the next load is a stat."""
    source = _filled_mask(tmp_path / "mask.tiff")
    data_dir, config_path = _data_dir(tmp_path, monkeypatch, {"dataset": None})
    derived = sp.pyramidize_segmentation_mask(
        source, sp.outline_output_path(source, data_dir / "sample"), tile_size=256
    )
    config_path.write_text(
        json.dumps({"sample": {"dataset": None, "segmentation": derived}}), encoding="utf-8"
    )

    started = _capture_jobs(monkeypatch)
    data_model.load_config("sample")

    assert started == []
    entry = data_model.config["sample"]
    assert entry["segmentation"] == derived
    assert entry["segmentationSource"] == derived
    assert entry["segmentationSourceKey"] == sp.source_fingerprint(derived)
    # The mode is read back off the file rather than assumed to be the current
    # default: these entries predate the key and are all outlines, so defaulting
    # them to filled would make every pre-existing project rebuild its mask on
    # the next load. Recorded now, so this is the last load that has to look.
    assert entry["segmentationMode"] == sp.MODE_OUTLINES
    assert data_model.segmentation_mode(entry) == sp.MODE_OUTLINES
    # Backfilled on disk, not just in memory.
    saved = json.loads(config_path.read_text(encoding="utf-8"))["sample"]
    assert saved["segmentationSourceKey"]
    assert saved["segmentationMode"] == sp.MODE_OUTLINES


def test_a_legacy_entry_pointing_at_a_filled_mask_is_corrected(tmp_path, monkeypatch):
    """The old content sniff mistook large filled masks for outlines, so some
    installs have config['segmentation'] aimed at filled labels. That should be
    re-derived now, not trusted."""
    source = _filled_mask(tmp_path / "mask.tiff")
    data_dir, config_path = _data_dir(tmp_path, monkeypatch, {"dataset": None})
    config_path.write_text(
        json.dumps({"sample": {"dataset": None, "segmentation": str(source)}}), encoding="utf-8"
    )

    started = _capture_jobs(monkeypatch)
    data_model.load_config("sample")

    assert started == [("sample", str(source), str(data_dir / "sample"))]
    assert data_model.config["sample"]["segmentation"] is None
    assert data_model.config["sample"]["segmentation_status"] == "pending"


def test_a_user_supplied_outline_mask_is_served_directly(tmp_path, monkeypatch):
    """Nothing to derive when the user already hands us outlines -- and the
    mapping must not then loop trying to convert it."""
    source = _filled_mask(tmp_path / "mask.tiff")
    data_dir, config_path = _data_dir(tmp_path, monkeypatch, {"dataset": None})
    outlines = sp.pyramidize_segmentation_mask(
        source, tmp_path / "user_outlines.ome.tiff", tile_size=256
    )
    config_path.write_text(
        json.dumps({"sample": {"dataset": None, "segmentation": outlines}}), encoding="utf-8"
    )

    started = _capture_jobs(monkeypatch)
    data_model.load_config("sample")
    assert started == []
    assert data_model.config["sample"]["segmentation"] == outlines

    # Second load sees a current mapping and does nothing at all.
    data_model.load_config("sample")
    assert started == []


def test_a_datasource_without_segmentation_is_left_alone(tmp_path, monkeypatch):
    _data_dir(tmp_path, monkeypatch, {"dataset": None, "segmentation": None})

    started = _capture_jobs(monkeypatch)
    data_model.load_config("sample")

    assert started == []
    assert data_model.config["sample"]["segmentation"] is None


def test_registering_with_segmentation_async_defers_to_a_job(tmp_path, monkeypatch):
    """The import pages pass segmentation_async=True so a large mask does not
    hold their form submission open; the page then waits on job progress."""
    import polars as pl

    from plexora import datasource

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    image_path = tmp_path / "image.tif"
    tifffile.imwrite(image_path, np.zeros((2, 256, 256), dtype=np.uint8))
    csv_path = tmp_path / "cells.csv"
    pl.DataFrame({
        "CellID": np.arange(4, dtype=np.uint32),
        "X_centroid": np.linspace(10, 200, 4, dtype=np.float32),
        "Y_centroid": np.linspace(10, 200, 4, dtype=np.float32),
        "MarkerA": np.linspace(0, 5, 4, dtype=np.float32),
    }).write_csv(csv_path)
    source = _filled_mask(tmp_path / "mask.tiff")

    started = _capture_jobs(monkeypatch)
    entry = datasource.register_datasource(
        name="async_sample",
        image=image_path,
        features=csv_path,
        x="X_centroid",
        y="Y_centroid",
        segmentation=source,
        data_dir=data_dir,
        segmentation_async=True,
    )

    assert entry["segmentation"] is None
    assert entry["segmentation_status"] == "pending"
    assert entry["segmentationSource"] == str(source)
    assert entry["segmentationSourceKey"] == sp.source_fingerprint(source)
    assert started == [("async_sample", str(source), str(data_dir / "async_sample"))]
    # The label channel is still advertised up front -- its src is derived from
    # the source mask's name, not from the not-yet-written derived file.
    assert entry["imageData"][0]["fullname"] == "Area"


def test_registering_synchronously_records_a_ready_mapping(tmp_path):
    """Default (programmatic) registration still returns a fully-usable
    datasource, and records the mapping so loads never re-derive."""
    import polars as pl

    from plexora import datasource

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    image_path = tmp_path / "image.tif"
    tifffile.imwrite(image_path, np.zeros((2, 256, 256), dtype=np.uint8))
    csv_path = tmp_path / "cells.csv"
    pl.DataFrame({
        "CellID": np.arange(4, dtype=np.uint32),
        "X_centroid": np.linspace(10, 200, 4, dtype=np.float32),
        "Y_centroid": np.linspace(10, 200, 4, dtype=np.float32),
        "MarkerA": np.linspace(0, 5, 4, dtype=np.float32),
    }).write_csv(csv_path)
    source = _filled_mask(tmp_path / "mask.tiff")

    entry = datasource.register_datasource(
        name="sync_sample",
        image=image_path,
        features=csv_path,
        x="X_centroid",
        y="Y_centroid",
        segmentation=source,
        data_dir=data_dir,
    )

    assert entry["segmentation_status"] == "ready"
    assert sp.generated_mask_kind(entry["segmentation"]) == sp.DEFAULT_MODE
    assert entry["segmentation"].startswith(str(data_dir / "sync_sample"))
    assert entry["segmentationSourceKey"] == sp.source_fingerprint(source)
    # Recorded explicitly, so a later load never has to infer it from the file.
    assert entry["segmentationMode"] == sp.DEFAULT_MODE


def test_filled_mode_stores_labels_and_records_the_mode(tmp_path):
    source = _filled_mask(tmp_path / "mask.tiff")
    dataset_dir = tmp_path / "dataset"
    dataset_dir.mkdir()

    resolved = data_model.resolve_outline_segmentation(
        source, dataset_dir, mode=sp.MODE_FILLED
    )

    assert sp.generated_mask_kind(resolved) == sp.MODE_FILLED
    assert resolved.endswith(sp.FILLED_SUFFIX)


def test_switching_a_datasource_between_modes_re_derives(tmp_path, monkeypatch):
    """The recorded derived file is the other kind after a mode switch. Serving
    a filled mask to a viewer that is not outlining paints solid cells over the
    image, so this has to regenerate rather than reuse."""
    source = _filled_mask(tmp_path / "mask.tiff")
    data_dir, config_path = _data_dir(tmp_path, monkeypatch, {"dataset": None})
    outlines = sp.pyramidize_segmentation_mask(
        source, sp.derived_output_path(source, data_dir / "sample", mode=sp.MODE_OUTLINES),
        tile_size=256,
    )
    entry = {
        "dataset": None,
        "segmentation": outlines,
        "segmentationSource": str(source),
        "segmentationSourceKey": sp.source_fingerprint(source),
        "segmentationMode": sp.MODE_FILLED,   # the user re-imported in filled mode
    }
    config_path.write_text(json.dumps({"sample": entry}), encoding="utf-8")

    started = _capture_jobs(monkeypatch)
    data_model.load_config("sample")

    assert started == [("sample", str(source), str(data_dir / "sample"))]
    assert started.modes == [sp.MODE_FILLED]
    assert data_model.config["sample"]["segmentation_status"] == "pending"


def test_a_user_supplied_label_pyramid_is_served_without_conversion(tmp_path, monkeypatch):
    """The one case where importing a mask costs nothing: filled mode plus a
    mask that is already a tiled pyramid."""
    source = _filled_mask(tmp_path / "mask.tiff")
    pyramidal = sp.pyramidize_segmentation_mask(
        source, tmp_path / "user_pyramid.ome.tiff", tile_size=256, outline=False
    )
    data_dir, config_path = _data_dir(tmp_path, monkeypatch, {"dataset": None})
    config_path.write_text(json.dumps({"sample": {
        "dataset": None,
        "segmentation": pyramidal,
        "segmentationMode": sp.MODE_FILLED,
    }}), encoding="utf-8")

    started = _capture_jobs(monkeypatch)
    data_model.load_config("sample")

    assert started == []
    assert data_model.config["sample"]["segmentation"] == pyramidal
    assert data_model.config["sample"]["segmentation_status"] == "ready"


def test_a_label_pyramid_is_served_untouched_with_no_mode_recorded(tmp_path, monkeypatch):
    """The same, arrived at by default rather than by an explicit mode.

    This is the rule the import defaults encode: hand it a mask that is already
    a tiled pyramid and it is served as-is, with no conversion and no second
    copy on disk. The entry deliberately carries no `segmentationMode`, so a
    regression in the default shows up here as a background job starting.
    """
    source = _filled_mask(tmp_path / "mask.tiff")
    pyramidal = sp.pyramidize_segmentation_mask(
        source, tmp_path / "user_pyramid.ome.tiff", tile_size=256, outline=False
    )
    data_dir, config_path = _data_dir(tmp_path, monkeypatch, {"dataset": None})
    config_path.write_text(json.dumps({"sample": {
        "dataset": None,
        "segmentation": pyramidal,
    }}), encoding="utf-8")

    started = _capture_jobs(monkeypatch)
    data_model.load_config("sample")

    assert started == []
    entry = data_model.config["sample"]
    assert entry["segmentation"] == pyramidal
    assert entry["segmentation_status"] == "ready"
    assert entry["segmentationMode"] == sp.MODE_FILLED
    # And no second copy was written into the dataset directory.
    assert list((data_dir / "sample").glob("*.pyramid.ome.tiff")) == []


def test_a_flat_mask_gets_a_filled_pyramid_by_default(tmp_path, monkeypatch):
    """The other half of the rule: no pyramid present means build one, and the
    kind built is the filled pyramid, not outlines."""
    source = _filled_mask(tmp_path / "mask.tiff")
    data_dir, config_path = _data_dir(tmp_path, monkeypatch, {"dataset": None})
    config_path.write_text(json.dumps({"sample": {
        "dataset": None,
        "segmentation": str(source),
    }}), encoding="utf-8")

    started = _capture_jobs(monkeypatch)
    data_model.load_config("sample")

    assert started == [("sample", str(source), str(data_dir / "sample"))]
    assert started.modes == [sp.MODE_FILLED]


def test_filled_mode_ignores_a_flat_user_mask_and_converts_it(tmp_path, monkeypatch):
    """A flat mask has no coarse levels, so `_zarr_level` would hand the viewer
    full resolution for every zoom level. It must be converted, not served."""
    source = _filled_mask(tmp_path / "mask.tiff")
    data_dir, config_path = _data_dir(tmp_path, monkeypatch, {"dataset": None})
    config_path.write_text(json.dumps({"sample": {
        "dataset": None,
        "segmentation": str(source),
        "segmentationMode": sp.MODE_FILLED,
    }}), encoding="utf-8")

    started = _capture_jobs(monkeypatch)
    data_model.load_config("sample")

    assert started == [("sample", str(source), str(data_dir / "sample"))]


def test_filled_mode_falls_back_when_the_source_is_already_outlines(tmp_path, monkeypatch):
    """Ticking "outline while viewing" and supplying an outline export would
    have the shader trace the boundary of each outline stroke. There is nothing
    to outline in that input, so the datasource reverts to outline mode."""
    source = _filled_mask(tmp_path / "mask.tiff")
    outlines = sp.pyramidize_segmentation_mask(
        source, tmp_path / "user_outlines.ome.tiff", tile_size=256
    )
    data_dir, config_path = _data_dir(tmp_path, monkeypatch, {"dataset": None})
    config_path.write_text(json.dumps({"sample": {
        "dataset": None,
        "segmentation": outlines,
        "segmentationMode": sp.MODE_FILLED,
    }}), encoding="utf-8")

    started = _capture_jobs(monkeypatch)
    data_model.load_config("sample")

    assert started == []
    assert data_model.config["sample"]["segmentation"] == outlines
    assert data_model.config["sample"]["segmentationMode"] == sp.MODE_OUTLINES
    # Persisted, so the viewer's shader uniform agrees on the next load too.
    saved = json.loads(config_path.read_text(encoding="utf-8"))["sample"]
    assert saved["segmentationMode"] == sp.MODE_OUTLINES


def test_registering_in_filled_mode_records_the_mode(tmp_path):
    import polars as pl

    from plexora import datasource

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    image_path = tmp_path / "image.tif"
    tifffile.imwrite(image_path, np.zeros((2, 256, 256), dtype=np.uint8))
    csv_path = tmp_path / "cells.csv"
    pl.DataFrame({
        "CellID": np.arange(4, dtype=np.uint32),
        "X_centroid": np.linspace(10, 200, 4, dtype=np.float32),
        "Y_centroid": np.linspace(10, 200, 4, dtype=np.float32),
        "MarkerA": np.linspace(0, 5, 4, dtype=np.float32),
    }).write_csv(csv_path)
    source = _filled_mask(tmp_path / "mask.tiff")

    entry = datasource.register_datasource(
        name="filled_sample",
        image=image_path,
        features=csv_path,
        x="X_centroid",
        y="Y_centroid",
        segmentation=source,
        data_dir=data_dir,
        segmentation_mode=sp.MODE_FILLED,
    )

    assert entry["segmentationMode"] == sp.MODE_FILLED
    assert sp.generated_mask_kind(entry["segmentation"]) == sp.MODE_FILLED
    # And the viewer reads this key to decide whether to outline in the shader.
    assert data_model.segmentation_mode(entry) == sp.MODE_FILLED


def test_resolve_returns_the_source_when_it_is_already_outlines(tmp_path):
    source = _filled_mask(tmp_path / "mask.tiff")
    outlines = sp.pyramidize_segmentation_mask(
        source, tmp_path / "outlines.ome.tiff", tile_size=256
    )

    assert data_model.resolve_outline_segmentation(outlines, tmp_path) == str(outlines)


def test_resolve_writes_into_the_dataset_directory(tmp_path):
    source = _filled_mask(tmp_path / "mask.tiff")
    dataset_dir = tmp_path / "dataset"
    dataset_dir.mkdir()

    resolved = data_model.resolve_outline_segmentation(source, dataset_dir)

    assert resolved.startswith(str(dataset_dir))
    assert sp.generated_mask_kind(resolved) == sp.DEFAULT_MODE


def test_outline_mode_still_works_when_asked_for_explicitly(tmp_path):
    """No UI selects outlines any more, but the code path stays supported --
    this is what keeps it from rotting unnoticed."""
    source = _filled_mask(tmp_path / "mask.tiff")
    dataset_dir = tmp_path / "dataset"
    dataset_dir.mkdir()

    resolved = data_model.resolve_outline_segmentation(
        source, dataset_dir, mode=sp.MODE_OUTLINES
    )

    assert resolved.startswith(str(dataset_dir))
    assert sp.is_generated_outline_mask(resolved)
    assert resolved.endswith(sp.OUTLINE_SUFFIX)


def test_a_finished_job_reports_the_mask_it_produced(tmp_path, monkeypatch):
    """The viewer takes a finished mask on in place rather than reloading the
    page, and it needs the derived path to do it. This branch is the one a
    server that restarted mid-session serves -- the in-memory job record is
    gone, and without a path here the viewer would have no way to pick the mask
    up short of the full reload this replaced.
    """
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "proj": {"segmentation": "/derived/mask.ome.tif",
                 "segmentation_status": "ready"},
    }), encoding="utf-8")
    monkeypatch.setattr(plexora, "config_json_path", config_path)
    monkeypatch.setattr(plexora, "data_path", tmp_path)
    monkeypatch.setattr(data_model, "_segmentation_jobs", {})

    status = data_model.get_segmentation_job_status("proj")

    assert status["status"] == "ready"
    assert status["segmentation"] == "/derived/mask.ome.tif"


def test_a_job_still_running_reports_no_mask_yet(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "proj": {"segmentation": None, "segmentation_status": "pending"},
    }), encoding="utf-8")
    monkeypatch.setattr(plexora, "config_json_path", config_path)
    monkeypatch.setattr(plexora, "data_path", tmp_path)
    monkeypatch.setattr(data_model, "_segmentation_jobs", {})

    status = data_model.get_segmentation_job_status("proj")

    assert status["status"] == "pending"
    assert status["segmentation"] is None
