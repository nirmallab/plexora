"""New Dataset from a bucket: a folder at a web address its host lists.

`s3://lin-2023-orion-crc/data` is the case this was built for: forty-one sample
folders, each an Orion slide, the H&E of the same section under a name that
shares nothing with it, a mask, a cell table and a channel list. The far side
here is a real HTTP server in its `listing` mode (`remote_fixtures`), standing
in for a bucket as it does in the chunk cache's own tests: the same
`remote_store` listing and range reads an `s3://` address takes.

Pinned: each folder plans as one sample named by the folder, with the H&E as
a layer; a tree that sorts its samples into folders carries its groups;
creating it streams the images and masks and leaves each table where it is,
remembered in the dataset, until somebody asks for it; a mask that is already
a tiled pyramid is read where it is, and any other is converted only once its
sample is asked for, never while the dataset is made; Import Sample reads one
sample's folder as that sample and hands a folder of samples to New Dataset;
and a pyramid's SubIFDs come down together rather than one round trip each.
"""

from __future__ import annotations

import numpy as np
import pytest
import tifffile

import plexora
from plexora.server.models import dataset_build, dataset_scan, layer_jobs
from plexora.server.models import datasets as registry
from plexora.server.models import import_proposal, import_sample
from plexora.server.models.project import Project
from tests import dataset_fixtures as fx
from tests.remote_fixtures import cache_root, http_store  # noqa: F401

SAMPLES = (("CRC01", "P37_S29"), ("CRC02", "P37_S30"))


@pytest.fixture(autouse=True)
def scans():
    dataset_scan.forget()
    dataset_build.forget()
    yield
    for owner, task_id in layer_jobs.running_tasks():
        if owner in (dataset_scan.TASK_OWNER, dataset_build.TASK_OWNER):
            layer_jobs.stop_task(owner, task_id)
    dataset_scan.forget()
    dataset_build.forget()


def rgb(path, size=256):
    path.parent.mkdir(parents=True, exist_ok=True)
    tifffile.imwrite(path, np.random.default_rng(1).integers(
        0, 255, (size, size, 3)).astype(np.uint8), photometric="rgb")
    return path


def orion(folder, stem):
    """One sample folder as the Orion bucket holds it."""
    sample = folder.name
    fx.image(folder / f"{stem}_A24_E15-zlib.ome.tiff", channels=4)
    rgb(folder / f"18459_LSP{stem[-2:]}_SCAN-registered.ome.tif")
    fx.mask(folder / f"{stem}-{sample}-mask.ome.tif")
    fx.csv(folder / f"{stem}-{sample}.csv", rows=6)
    (folder / "markers.csv").write_text(fx.MARKERS + "4,1,CD20\n", encoding="utf-8")


@pytest.fixture
def bucket(tmp_path, http_store):  # noqa: F811 -- the fixture, imported
    """`(server, served)`: a listing host over `served`, with the cohort in
    `served/data`."""
    served = tmp_path / "served"
    for sample, stem in SAMPLES:
        orion(served / "data" / sample, stem)
    return http_store("listing"), served


def _by_name(plan):
    return {project["name"]: project for project in plan["projects"]}


# -- the plan ------------------------------------------------------------------


def test_a_bucket_of_sample_folders_plans_one_sample_a_folder(bucket):
    server, _served = bucket
    plan = plexora.plan_dataset(server.url("data"))
    assert plan["place"] == {"kind": "web", "name": server.url()}
    projects = _by_name(plan)
    assert list(projects) == ["CRC01", "CRC02"]
    crc01 = projects["CRC01"]
    assert crc01["image"]["name"] == "P37_S29_A24_E15-zlib.ome.tiff"
    assert crc01["image"]["modality"] == "multiplex"
    assert [layer["name"] for layer in crc01["layers"]] == [
        "18459_LSP29_SCAN-registered.ome.tif"]
    assert crc01["mask"]["name"] == "P37_S29-CRC01-mask.ome.tif"
    assert crc01["mask"]["confidence"] == "high"
    assert crc01["table"]["name"] == "P37_S29-CRC01.csv"
    assert crc01["channels"]["names"] == ["DNA", "CD3", "CD8", "CD20"]
    assert crc01["status"] == "matched"
    assert plan["unassigned"] == [] and plan["groups"] == []


def test_a_channel_list_planned_before_its_copy_lands_is_read_once_it_has(bucket):
    """The dialog asks for the plan while the scan is still reading, so a
    bucket's `markers.csv` is planned once before its local copy exists. That
    first, empty reading must not stick."""
    from plexora.server.models import dataset_plan

    server, _served = bucket
    roots, place = dataset_scan.check_roots([server.url("data")])
    inventory = dataset_scan.Inventory(place=place)
    dataset_scan.walk(roots, inventory)
    for entry in inventory.entries.values():
        if entry.kind == "raster":
            dataset_scan._apply(entry, dataset_scan._inspect_one(entry, place))
    early = _by_name(dataset_plan.plan(inventory, config={}))
    assert early["CRC01"]["channels"]["names"] is None
    dataset_scan.inspect(inventory)
    late = _by_name(dataset_plan.plan(inventory, config={}))
    assert late["CRC01"]["channels"]["names"] == ["DNA", "CD3", "CD8", "CD20"]


def test_samples_sorted_into_folders_carry_their_groups(tmp_path, http_store):  # noqa: F811
    served = tmp_path / "served"
    for group, samples in (("CRC", ("S01", "S02")), ("Lung", ("L01", "L02"))):
        for sample in samples:
            fx.image(served / "cohort" / group / sample / f"{sample}.ome.tiff")
            fx.mask(served / "cohort" / group / sample / f"{sample}_mask.tif")
    server = http_store("listing")
    plan = plexora.plan_dataset(server.url("cohort"))
    assert plan["groups"] == [{"name": "CRC", "projects": 2}, {"name": "Lung", "projects": 2}]
    assert [(p["name"], p["group"]) for p in plan["projects"]] == [
        ("S01", "CRC"), ("S02", "CRC"), ("L01", "Lung"), ("L02", "Lung")]

    dataset = plexora.create_dataset_from_directory("cohort", server.url("cohort"))
    assert registry.get(dataset.id).meta["groups"] == {
        "S01": "CRC", "S02": "CRC", "L01": "Lung", "L02": "Lung"}


def test_a_host_that_does_not_list_is_said(bucket, http_store):  # noqa: F811
    _server, served = bucket
    gateway = http_store("gateway", root=served)
    with pytest.raises(ValueError, match="could not be listed"):
        plexora.plan_dataset(gateway.url("data"))


def test_a_file_is_not_a_folder_to_scan(bucket):
    server, _served = bucket
    with pytest.raises(ValueError, match="is a file"):
        plexora.plan_dataset(server.url("data/CRC01/P37_S29-CRC01-mask.ome.tif"))


# -- creating it -----------------------------------------------------------------


def test_creating_it_streams_the_images_and_leaves_the_tables_where_they_are(bucket):
    server, _served = bucket
    dataset = plexora.create_dataset_from_directory("orion", server.url("data"))
    assert list(dataset) == ["CRC01", "CRC02"]

    project = Project.load("CRC01")
    folder = server.url("data/CRC01")
    assert project.image.src == f"{folder}/P37_S29_A24_E15-zlib.ome.tiff"
    assert [layer.src for layer in project.spatial_layers if layer.kind == "image"] == [
        f"{folder}/18459_LSP29_SCAN-registered.ome.tif"]
    entry = plexora.get_config()["CRC01"]
    assert entry["segmentationSource"] == f"{folder}/P37_S29-CRC01-mask.ome.tif"
    assert entry.get("dataset") is None, "a bucket's table is not copied unasked"

    meta = registry.get(dataset.id).meta
    assert meta["tables"]["CRC01"] == {"path": f"{folder}/P37_S29-CRC01.csv",
                                       "name": "P37_S29-CRC01.csv"}
    assert meta["source"] == {"roots": [server.url("data")],
                              "place": {"kind": "web", "name": server.url()}}
    assert "groups" not in meta


def label_pyramid(path, size=256):
    """A mask as the Orion bucket's are: int32, tiled, its levels as SubIFDs.
    Returns the full-resolution labels."""
    labels = np.zeros((size, size), dtype=np.int32)
    for row in range(4):
        for col in range(4):
            top, left = row * size // 4, col * size // 4
            labels[top + 4:top + size // 4 - 4, left + 4:left + size // 4 - 4] = row * 4 + col + 1
    path.unlink(missing_ok=True)
    with tifffile.TiffWriter(path) as tif:
        options = dict(photometric="minisblack", tile=(64, 64), compression="zlib")
        tif.write(labels, subifds=2, **options)
        tif.write(labels[::2, ::2], subfiletype=1, **options)
        tif.write(labels[::4, ::4], subfiletype=1, **options)
    return labels


def test_a_mask_that_is_a_pyramid_is_read_where_it_is(bucket):
    """Nothing is downloaded or converted to make the dataset, or to draw it:
    the tile route reads the bucket's own levels, a tile at a time."""
    from plexora.server import providers
    from plexora.server.models import data_model

    server, served = bucket
    labels = label_pyramid(served / "data" / "CRC01" / "P37_S29-CRC01-mask.ome.tif")
    plexora.create_dataset_from_directory("orion", server.url("data"))

    url = server.url("data/CRC01/P37_S29-CRC01-mask.ome.tif")
    entry = plexora.get_config()["CRC01"]
    assert (entry["segmentation"], entry["segmentation_status"]) == (url, "ready")
    assert "CRC01" not in data_model._segmentation_jobs
    assert data_model._segmentation_mapping_is_current(entry)
    assert not list(plexora.paths.derived_root("CRC01").glob("*.labels.pyramid*"))

    mask = providers.resolve_providers(Project.load("CRC01")).segmentation
    assert np.array_equal(mask.read_region(0, (0, 0, 256, 256)), labels)
    assert np.array_equal(mask.read_region(1, (32, 32, 96, 96)), labels[::2, ::2][32:96, 32:96])
    # Past the bucket's last level: every 2nd pixel of it, as for a mask on disk.
    assert np.array_equal(mask.read_region(3, (0, 0, 32, 32)), labels[::8, ::8])


def test_a_flat_mask_waits_until_its_sample_is_asked_for(bucket):
    """A mask that has to be converted is not touched while the dataset is
    made -- somebody may keep three samples of forty. The viewer's poll, or a
    script's `wait=True`, is what starts it."""
    from plexora.server.models import data_model

    server, _served = bucket
    plexora.create_dataset_from_directory("orion", server.url("data"))
    for name in ("CRC01", "CRC02"):
        entry = plexora.get_config()[name]
        assert (entry["segmentation"], entry["segmentation_status"]) == (None, "pending")
        assert name not in data_model._segmentation_jobs

    assert layer_jobs.status("CRC01")["pending"]
    assert plexora.datasets._wait_for_layers("CRC01", timeout=120)
    entry = plexora.get_config()["CRC01"]
    assert entry["segmentation_status"] == "ready"
    assert "://" not in entry["segmentation"] and entry["segmentation"].endswith(".ome.tiff")
    assert plexora.get_config()["CRC02"]["segmentation_status"] == "pending"
    assert "CRC02" not in data_model._segmentation_jobs


def test_a_table_left_in_the_bucket_is_copied_when_asked(bucket):
    server, _served = bucket
    plexora.create_dataset_from_directory("orion", server.url("data"))
    table = server.url("data/CRC01/P37_S29-CRC01.csv")
    import_sample.add_layers("CRC01", [table])
    spec = Project.load("CRC01").dataset
    assert spec is not None and spec.type == "csv"
    assert "://" not in str(spec.path), "copied to this computer, then read"


def test_deleting_a_sample_deletes_what_was_kept_of_it_here(bucket):
    """"Delete dataset and its samples" deletes each sample. What Plexora kept
    of one goes with it: its directory (the table copied from the bucket) and
    the image and mask tiles cached from its bucket folder. The bucket's own
    files are never touched; a folder another project still reads stays
    cached; and "Delete dataset only" deletes nothing at all."""
    from plexora import paths
    from plexora.server import providers
    from plexora.server.utils import remote_store

    server, served = bucket
    for sample, stem in SAMPLES:
        label_pyramid(served / "data" / sample / f"{stem}-{sample}-mask.ome.tif")
    dataset = plexora.create_dataset_from_directory("orion", server.url("data"))
    import_sample.add_layers("CRC01", [server.url("data/CRC01/P37_S29-CRC01.csv")])
    image = server.url("data/CRC02/P37_S30_A24_E15-zlib.ome.tiff")
    import_sample.import_sample([image], name="CRC02 again")
    for name in ("CRC01", "CRC02"):
        providers.resolve_providers(Project.load(name)).segmentation.read_region(
            0, (0, 0, 256, 256))

    index = remote_store.cache_index()

    def cached(sample):
        root, _ = remote_store.split_locator_url(server.url(f"data/{sample}/x.tif"))
        return (index.store_row(remote_store.store_id_of(root)) or {}).get("bytes", 0)

    assert cached("CRC01") and cached("CRC02")
    assert list(paths.project_state_dir("CRC01").glob("*.csv"))

    client = plexora.app.test_client()
    for name in ("CRC01", "CRC02"):
        assert client.post(f"/project/{name}/delete").status_code == 200
    assert client.post(f"/datasets/{dataset.id}/delete").status_code == 200

    assert not paths.project_state_dir("CRC01").exists()
    assert not paths.project_state_dir("CRC02").exists()
    assert set(plexora.get_config()) == {"CRC02 again"}
    assert registry.find(dataset.id) is None
    assert cached("CRC01") == 0
    assert cached("CRC02"), "CRC02 again still reads that folder"
    assert (served / "data" / "CRC01" / "P37_S29-CRC01.csv").exists()
    assert (served / "data" / "CRC01" / "P37_S29-CRC01-mask.ome.tif").exists()


def test_copying_a_buckets_table_shows_how_far_it_has_got(bucket, monkeypatch):
    """Add Data names its request, as a fresh import does, so the dialog's row
    for the table shows the copy moving -- a bucket's cell table can be a
    gigabyte -- and then that it is being read."""
    server, _served = bucket
    plexora.create_dataset_from_directory("orion", server.url("data"))
    seen = []
    record = layer_jobs.registration_progress

    def spy(done, total, label=None, **kwargs):
        record(done, total, label, **kwargs)
        seen.append(dict(layer_jobs.registration("add-1")["layers"]))

    monkeypatch.setattr(layer_jobs, "registration_progress", spy)
    import_sample.add_layers("CRC01", [server.url("data/CRC01/P37_S29-CRC01.csv")],
                             token="add-1")
    labels = [(row["stage_label"], row["progress"]) for rows in seen for row in rows.values()
              if row.get("stage") == "registering"]
    copying = [(label, progress) for label, progress in labels if label.startswith("Copying")]
    assert copying and copying[-1][1] == 100
    assert copying[-1][0].startswith("Copying P37_S29-CRC01.csv to this computer, ")
    assert copying[-1][0].endswith(" MB")
    assert labels[-1] == ("Reading the table", 0)
    assert layer_jobs.registration("add-1") is None, "dropped once the request answered"


# -- Import Sample -----------------------------------------------------------------


def test_import_sample_reads_one_folder_as_that_sample(bucket):
    server, _served = bucket
    proposal = import_proposal.inspect_paths([server.url("data/CRC02")])
    [sample] = proposal.samples
    assert sample.name == "CRC02"
    roles = sorted((layer.role, layer.modality) for layer in sample.layers)
    assert roles == [("image", "multiplex"), ("layer", "he"), ("mask", "mask")]
    [said] = [w for w in proposal.warnings if "P37_S30-CRC02.csv" in w]
    assert server.url("data/CRC02/P37_S30-CRC02.csv") in said


def test_import_sample_hands_a_folder_of_samples_to_new_dataset(bucket):
    server, _served = bucket
    proposal = import_proposal.inspect_paths([server.url("data")]).to_dict()
    assert proposal["samples"] == []
    [entry] = proposal["unrecognised"]
    assert entry["dataset"] is True and "New Dataset" in entry["reason"]


# -- reading a pyramid ---------------------------------------------------------------


def test_a_pyramids_subifds_come_down_before_the_series_is_built(tmp_path, http_store):  # noqa: F811
    """An OME-TIFF keeps each channel's lower levels as SubIFDs, which
    tifffile reads one at a time. Opening fetches them together, so building
    the series afterwards asks the host for nothing."""
    from plexora.server.utils import tiff_region

    data = np.random.default_rng(2).integers(0, 3000, (4, 512, 512)).astype(np.uint16)
    path = tmp_path / "served" / "pyramid.ome.tif"
    path.parent.mkdir(parents=True, exist_ok=True)
    with tifffile.TiffWriter(path) as tif:
        options = dict(photometric="minisblack", tile=(128, 128), compression="zlib")
        tif.write(data, subifds=2, metadata={"axes": "CYX"}, **options)
        tif.write(data[:, ::2, ::2], subfiletype=1, **options)
        tif.write(data[:, ::4, ::4], subfiletype=1, **options)
    server = http_store()
    tiff = tiff_region.open_tiff(server.url("pyramid.ome.tif"))
    try:
        opened = server.count(method="GET")
        series = tiff.series[0]
        assert series.shape == (4, 512, 512)
        assert [level.shape for level in series.levels] == [
            (4, 512, 512), (4, 256, 256), (4, 128, 128)]
        assert server.count(method="GET") == opened
    finally:
        tiff_region.close_tiff(tiff)
