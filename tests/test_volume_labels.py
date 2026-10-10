"""3-D label volumes and object measurement, checked against shapes whose answers are known."""

import math

import numpy as np
import pytest
import tifffile

from plexora.server.volume import labels as volume_labels
from plexora.server.volume import measure as volume_measure
from plexora.server.volume.axes import AxisSpacing, Spacing


def _ellipsoid(shape, centre, radii, label, out):
    zz, yy, xx = np.ogrid[:shape[0], :shape[1], :shape[2]]
    inside = (((zz - centre[0]) / radii[0]) ** 2 + ((yy - centre[1]) / radii[1]) ** 2
              + ((xx - centre[2]) / radii[2]) ** 2) <= 1.0
    out[inside] = label
    return inside


def _scene(depth=20, size=128):
    labels = np.zeros((depth, size, size), dtype=np.uint16)
    a = _ellipsoid(labels.shape, (8, 40, 40), (5, 12, 12), 1, labels)
    b = _ellipsoid(labels.shape, (10, 90, 85), (6, 15, 10), 7, labels)
    c = _ellipsoid(labels.shape, (0, 100, 20), (3, 8, 8), 3, labels)   # clipped at z 0
    return labels, {1: a, 7: b, 3: c}


SPACING = Spacing(x=AxisSpacing(0.5, "µm", "metadata"), y=AxisSpacing(0.5, "µm", "metadata"),
                  z=AxisSpacing(2.0, "µm", "metadata"))


def test_a_label_volume_round_trips_plane_by_plane(tmp_path):
    labels, _ = _scene()
    source = tmp_path / "labels.tif"
    tifffile.imwrite(source, labels)
    assert volume_labels.label_volume_shape(source) == labels.shape
    store = volume_labels.build_label_volume(source, tmp_path / "labels_v1.zarr", target=64)
    assert volume_labels.is_label_volume(store)
    volume = volume_labels.open_label_volume(store)
    assert (volume.depth, volume.height, volume.width) == labels.shape
    for z in (0, 8, 19):
        plane = volume.plane(z)
        assert np.array_equal(np.asarray(plane["0"][:, :]), labels[z])
        # Labels are subsampled, never averaged: every coarse id is a real id.
        coarse = np.asarray(plane["1"][:, :])
        assert np.array_equal(coarse, labels[z, ::2, ::2])
    # The default plane answers what a 2-D mask was asked.
    assert np.array_equal(np.asarray(volume["0"][:, :]), labels[10])


def test_a_planar_or_colour_tiff_is_not_a_label_volume(tmp_path):
    tifffile.imwrite(tmp_path / "flat.tif", np.zeros((64, 64), np.uint16))
    tifffile.imwrite(tmp_path / "float.tif", np.zeros((4, 64, 64), np.float32))
    assert volume_labels.label_volume_shape(tmp_path / "flat.tif") is None
    assert volume_labels.label_volume_shape(tmp_path / "float.tif") is None


def _measure(tmp_path, units="physical", spacing=SPACING, intensity=None):
    labels, masks = _scene()
    store = volume_labels.build_label_volume(labels, tmp_path / "labels_v1.zarr")
    volume = volume_labels.open_label_volume(store)
    if intensity is None:
        intensity = np.zeros((2,) + labels.shape, dtype=np.uint16)
        intensity[0][labels == 1] = 1000
        intensity[0][labels == 7] = 200
        intensity[1] = 50

    def read(channel, z, rows, cols):
        return intensity[channel, z, rows, cols]

    columns, manifest = volume_measure.measure(volume, read, channel_names=["DNA", "CD8"],
                                               spacing=spacing, units=units)
    return columns, manifest, masks


def test_objects_are_measured_whole(tmp_path):
    columns, manifest, masks = _measure(tmp_path)
    assert list(columns["label"]) == [1, 3, 7]
    assert manifest["objects"] == 3
    row = {name: columns[name][0] for name in columns}
    voxels = int(masks[1].sum())
    assert row["volume_vox"] == voxels
    assert row["volume_um3"] == pytest.approx(voxels * 0.5 * 0.5 * 2.0)
    zz, yy, xx = np.nonzero(masks[1])
    assert row["X"] == pytest.approx(xx.mean())
    assert row["Y"] == pytest.approx(yy.mean())
    assert row["Z"] == pytest.approx(zz.mean())
    assert row["z_um"] == pytest.approx(zz.mean() * 2.0)
    assert (row["bbox_z0"], row["bbox_z1"]) == (zz.min(), zz.max())
    assert row["n_planes"] == zz.max() - zz.min() + 1
    assert row["DNA"] == pytest.approx(1000.0)
    assert row["DNA__integrated"] == pytest.approx(1000.0 * voxels)
    assert row["CD8"] == pytest.approx(50.0)


def test_a_sphere_is_roughly_spherical_and_a_clipped_object_is_flagged(tmp_path):
    columns, _, masks = _measure(tmp_path)
    index = {int(i): k for k, i in enumerate(columns["label"])}
    # An analytic ellipsoid volume (4/3 pi abc in microns): voxelization is
    # within a few percent at these radii.
    analytic = 4 / 3 * math.pi * (5 * 2.0) * (12 * 0.5) * (12 * 0.5)
    assert columns["volume_um3"][index[1]] == pytest.approx(analytic, rel=0.08)
    # Face counting over-estimates a smooth surface; sphericity ranks shapes.
    assert 0.5 < columns["sphericity"][index[1]] < 1.0
    assert bool(columns["touches_z_bounds"][index[3]]) is True
    assert bool(columns["touches_z_bounds"][index[1]]) is False


def test_surface_of_a_box_is_exact(tmp_path):
    labels = np.zeros((6, 32, 32), dtype=np.uint16)
    labels[1:4, 5:15, 10:14] = 9                     # 3 x 10 x 4 voxels
    store = volume_labels.build_label_volume(labels, tmp_path / "box.zarr")
    volume = volume_labels.open_label_volume(store)
    columns, _ = volume_measure.measure(volume, lambda c, z, r, s: np.zeros((r.stop - r.start, 32)),
                                        channel_names=[], spacing=SPACING)
    sx, sy, sz = 0.5, 0.5, 2.0
    nz, ny, nx = 3, 10, 4
    expected = 2 * (ny * nz * sy * sz + nx * nz * sx * sz + nx * ny * sx * sy)
    assert columns["surface_area_um2"][0] == pytest.approx(expected)


def test_physical_columns_need_a_known_z_step(tmp_path):
    no_z = Spacing(x=AxisSpacing(0.5, "µm", "metadata"), y=AxisSpacing(0.5, "µm", "metadata"))
    with pytest.raises(volume_measure.SpacingMissing):
        _measure(tmp_path, spacing=no_z)
    columns, manifest, _ = _measure(tmp_path, units="voxel", spacing=no_z)
    assert manifest["units"] == "voxel"
    assert "volume_um3" not in columns and "z_um" not in columns
    assert "volume_vox" in columns


def test_strips_and_planes_merge_exactly(tmp_path, monkeypatch):
    whole, _, _ = _measure(tmp_path)
    monkeypatch.setattr(volume_measure, "STRIP_ROWS", 17)
    stripped, _, _ = _measure(tmp_path / "s")
    for name in whole:
        assert np.allclose(whole[name], stripped[name], equal_nan=True), name


# -- registered and served -----------------------------------------------------


def _decode_label_tile(payload):
    import io

    from PIL import Image

    rgba = np.asarray(Image.open(io.BytesIO(payload)).convert("RGBA")).astype(np.uint32)
    return rgba[..., 0] + (rgba[..., 1] << 8) + (rgba[..., 2] << 16)


def test_a_registered_3d_mask_serves_its_labels_plane_by_plane(tmp_path):
    import plexora
    from plexora.datasource import register_datasource
    from plexora.server.models.project import Project
    from tests import volume_fixtures as vf

    path, _ = vf.write_ome_volume(tmp_path / "v.ome.tif", axes="CZYX", channels=2, depth=6,
                                  height=64, width=64, names=["DNA", "CD3"])
    labels = np.zeros((6, 64, 64), dtype=np.uint16)
    labels[1:4, 10:20, 10:20] = 5
    labels[3:6, 30:40, 30:40] = 9
    tifffile.imwrite(tmp_path / "mask.tif", labels)
    table = tmp_path / "cells.csv"
    table.write_text("CellID,X,Y,DNA,CD3\n5,15,15,10,1\n9,35,35,12,3\n", encoding="utf-8")
    entry = register_datasource("v3", path, table, x="X", y="Y", id_column="CellID",
                                segmentation=tmp_path / "mask.tif")
    project = Project.find("v3")
    assert volume_labels.is_label_volume(project.segmentation.derived)

    client = plexora.app.test_client()
    info = client.get("/volume/v3/info").get_json()
    assert info.get("segmentation") == {"ndim": 3}, info
    src = entry["imageData"][0]["src"]
    key = src.rstrip("/").rsplit("/", 1)[-1]
    for z in (0, 2, 4):
        response = client.get(f"/generated/data/v3/{key}/0/0_0.png?view=z{z}")
        assert response.status_code == 200
        ids = _decode_label_tile(response.data)
        assert np.array_equal(ids[:64, :64], labels[z])


# -- volumetric QC -----------------------------------------------------------------


def _stack(depth=12, size=96, seed=0):
    rng = np.random.default_rng(seed)
    base = np.zeros((size, size))
    for _ in range(40):
        y, x = rng.integers(8, size - 8, 2)
        base[y - 3:y + 3, x - 3:x + 3] = 2000
    planes = []
    for z in range(depth):
        planes.append(base + rng.normal(100, 5, base.shape))
    return planes


def test_qc3d_flags_blurred_faded_empty_and_repeated_planes():
    from scipy import ndimage

    from plexora.server.volume import qc3d

    planes = _stack()
    planes[0] = ndimage.gaussian_filter(planes[0], 4)
    planes[1] = ndimage.gaussian_filter(planes[1], 4)
    planes[6] = np.zeros_like(planes[6])
    planes[8] = planes[7].copy()
    findings, stats = qc3d.run_checks(planes, channel="DNA")
    kinds = {(f["check"], f.get("kind"), f["z0"], f["z1"]) for f in findings}
    assert ("focus_profile", None, 0, 1) in kinds
    assert ("missing_slices", "empty", 6, 6) in kinds
    assert ("missing_slices", "duplicate", 8, 8) in kinds
    assert len(stats) == 12


def test_qc3d_reports_fading_with_depth():
    from plexora.server.volume import qc3d

    planes = [plane * np.exp(-z / 3.0) for z, plane in enumerate(_stack(depth=10))]
    findings, _ = qc3d.run_checks(planes, channel="DNA", checks=("axial_attenuation",))
    assert findings and findings[0]["check"] == "axial_attenuation"
    assert findings[0]["evidence"]["peak_z"] == 0


def test_qc3d_says_nothing_about_a_clean_stack():
    from plexora.server.volume import qc3d

    findings, _ = qc3d.run_checks(_stack())
    assert findings == []
