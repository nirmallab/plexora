"""Label images at a web address, as a sample's segmentation.

IDR ships segmentations the NGFF way: `labels/` inside the image's own store.
One pick of the store proposes both, and the mask is converted into the
project once, read through the chunk cache. Never served from the web tile by
tile, and never written "beside the source" -- a web address has no folder
beside it, and `Path` of one would create `https:/...` under the working
directory.
"""

import os
import time

import numpy as np
import pytest
import zarr

from plexora.server.models import data_model, import_proposal, import_sample
from plexora.server.models.project import Project
from plexora.server.utils import remote_store, segmentation_pyramid
from tests.ngff_fixtures import write_ngff
from tests.remote_fixtures import cache_root, http_store  # noqa: F401


def _labelled_store(path, version="0.5", names=("cells",), axes="cyx", dtype="uint32",
                    offset=0):
    write_ngff(path, shape=(2, 128, 128), levels=2, version=version)
    fmt = 2 if version == "0.4" else 3
    labels = zarr.open_group(str(path / "labels"), mode="w", zarr_format=fmt)
    attrs = {"labels": list(names)}
    labels.attrs.update(attrs if fmt == 2 else {"ome": attrs})
    shape = {"cyx": (1, 128, 128), "yx": (128, 128), "tczyx": (1, 1, 3, 128, 128)}[axes]
    for index, name in enumerate(names):
        mask = write_ngff(path / "labels" / name, shape=shape, levels=1, version=version,
                          axes=axes, dtype=dtype, seed=index)
        array = zarr.open_array(str(mask / "0"), mode="a")
        plane = np.zeros(shape, dtype=dtype)
        plane[..., 10:40, 10:40] = 1 + offset
        plane[..., 60:90, 50:100] = 2 + offset
        array[:] = plane
    return path


def _wait_for_mask(name, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = data_model.get_segmentation_job_status(name)
        if status.get("status") in ("ready", "error"):
            return status
        time.sleep(0.1)
    raise AssertionError(f"mask job for {name} did not finish")


@pytest.mark.parametrize("version", ["0.4", "0.5"])
def test_one_pick_proposes_the_image_and_its_mask(tmp_path, http_store, version):
    _labelled_store(tmp_path / "served" / "img.zarr", version=version)
    server = http_store()
    url = server.url("img.zarr")
    [sample] = import_proposal.inspect_paths([url]).to_dict()["samples"]
    by_role = {layer["role"]: layer for layer in sample["layers"]}
    assert by_role["image"]["src"] == url
    assert by_role["mask"]["src"] == remote_store.url_join(url, "labels", "cells")
    assert by_role["mask"]["geometry"]["width"] == 128


def test_several_label_images_are_a_question(tmp_path, http_store):
    _labelled_store(tmp_path / "served" / "img.zarr", names=("cells", "nuclei"))
    server = http_store()
    url = server.url("img.zarr")
    [sample] = import_proposal.inspect_paths([url]).to_dict()["samples"]
    [question] = [q for q in sample["questions"] if q["id"] == "labels"]
    assert [o["value"] for o in question["options"]] == ["cells", "nuclei", "none"]
    [sample] = import_proposal.inspect_paths(
        [url], answers={"labels": "nuclei"}).to_dict()["samples"]
    mask = next(l for l in sample["layers"] if l["role"] == "mask")
    assert mask["src"].endswith("/labels/nuclei")
    [sample] = import_proposal.inspect_paths(
        [url], answers={"labels": "none"}).to_dict()["samples"]
    assert not [l for l in sample["layers"] if l["role"] == "mask"]


def test_the_mask_is_converted_into_the_project(tmp_path, http_store, monkeypatch):
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    _labelled_store(tmp_path / "served" / "img.zarr")
    server = http_store()
    url = server.url("img.zarr")
    proposal = import_proposal.inspect_paths([url])

    import_sample.register_sample(proposal.samples[0], name="web")
    status = _wait_for_mask("web")

    assert status["status"] == "ready", status
    project = Project.load("web")
    assert project.segmentation.source == remote_store.url_join(url, "labels", "cells")
    assert project.segmentation.source_key.startswith("etag:")
    derived = project.segmentation.derived
    assert derived and os.path.exists(derived)
    assert str(tmp_path) in derived and "img_cells" in os.path.basename(derived)
    # Nothing was created "beside" the URL, under the working directory.
    assert not any(workdir.iterdir())

    data_model.load_datasource("web", reload=True)
    seg = np.asarray(data_model.seg)
    assert seg.shape == (128, 128) and set(np.unique(seg)) == {0, 1, 2}


def test_a_five_dimensional_label_image_reads_as_one_plane(tmp_path, http_store):
    _labelled_store(tmp_path / "served" / "img.zarr", axes="tczyx")
    server = http_store()
    plane, close = segmentation_pyramid._open_level_zero(
        remote_store.url_join(server.url("img.zarr"), "labels", "cells"))
    try:
        assert plane.shape == (128, 128)
        assert int(np.asarray(plane)[70, 60]) == 2
    finally:
        close()


def test_an_offline_load_keeps_the_converted_mask(tmp_path, http_store):
    _labelled_store(tmp_path / "served" / "img.zarr")
    server = http_store()
    proposal = import_proposal.inspect_paths([server.url("img.zarr")])
    import_sample.register_sample(proposal.samples[0], name="web")
    _wait_for_mask("web")
    entry = dict(Project.load_all()["web"])

    with server.outage():
        remote_store._probes.clear()
        remote_store._fingerprints.clear()
        assert data_model._segmentation_mapping_is_current(entry)
        changed, pending = data_model.refresh_segmentation_mapping(entry, "web")
    assert pending is None


def test_a_remote_mask_is_never_served_as_is(tmp_path, http_store):
    _labelled_store(tmp_path / "served" / "img.zarr")
    server = http_store()
    src = remote_store.url_join(server.url("img.zarr"), "labels", "cells")
    assert not data_model._servable_as_is(src, segmentation_pyramid.MODE_FILLED)
    with pytest.raises(ValueError):
        segmentation_pyramid.resolve_derived_mask(src, None)


def test_a_64_bit_label_image_converts(tmp_path, http_store):
    """IDR's omero-zarr labels are int64 with ids in the millions
    (idr0095B/11511422.zarr), and OME-TIFF has no 64-bit integer type: the
    conversion used to die with "data type 'int64' not supported"."""
    _labelled_store(tmp_path / "served" / "img.zarr", version="0.4", dtype="int64",
                    offset=2122925)
    server = http_store()
    proposal = import_proposal.inspect_paths([server.url("img.zarr")])
    import_sample.register_sample(proposal.samples[0], name="web")
    status = _wait_for_mask("web")
    assert status["status"] == "ready", status
    data_model.load_datasource("web", reload=True)
    seg = np.asarray(data_model.seg)
    assert seg.dtype == np.uint32
    assert set(np.unique(seg)) == {0, 2122926, 2122927}


def test_label_ids_too_large_for_32_bits_are_refused(tmp_path):
    import tifffile

    plane = np.zeros((64, 64), dtype="int64")
    plane[5:20, 5:20] = 2 ** 40
    tifffile.imwrite(tmp_path / "big.tif", plane)
    with pytest.raises(ValueError, match="Relabel"):
        segmentation_pyramid.pyramidize_segmentation_mask(
            str(tmp_path / "big.tif"), str(tmp_path / "big.ome.tif"))
