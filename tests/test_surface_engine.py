"""The surface engine against shapes whose answers are known.

Spheres and ellipsoids with anisotropic voxels: closed, the right volume and
area in microns, the same mesh whether marched whole or in strips, and
reproducible to the byte. Label surfaces: one closed surface per object, ids
kept, the clipped object flagged, the mask never touched.
"""

import hashlib
import math

import numpy as np
import pytest

from plexora.server.surface import isosurface, levels, marching, suggest
from plexora.server.surface import labels as label_surfaces
from plexora.server.surface import mesh as mesh_mod

SX, SZ = 0.5, 2.0   # µm per voxel in x/y and in z


def _sphere_volume(depth=20, size=64, radius_um=10.0):
    z, y, x = np.mgrid[:depth, :size, :size].astype(np.float32)
    centre = (depth / 2, size / 2, size / 2)
    r = np.sqrt(((x - centre[2]) * SX) ** 2 + ((y - centre[1]) * SX) ** 2
                + ((z - centre[0]) * SZ) ** 2)
    # A linear ramp: the 50 isovalue is the sphere of radius `radius_um`.
    return (100 * np.clip(1 - r / (2 * radius_um), 0, 1)).astype(np.float32)


def _iso(vol, level=50.0, **kwargs):
    grid = marching.Grid.of(vol.shape)
    kwargs.setdefault("scale", (SX, SX, SZ))
    return isosurface.extract(lambda a, b: vol[a:b + 1], grid, level, **kwargs)


def test_an_anisotropic_sphere_is_closed_and_measured_in_microns():
    surface, report = _iso(_sphere_volume())
    columns = mesh_mod.measure(surface, scale=(SX, SX, SZ), physical=True, dims=(64, 64, 20))
    assert report["pieces"] == 1 and surface.n_objects == 1
    assert bool(columns["closed"][0])
    assert columns["mesh_volume_um3"][0] == pytest.approx(4 / 3 * math.pi * 1000, rel=0.02)
    assert columns["mesh_area_um2"][0] == pytest.approx(4 * math.pi * 100, rel=0.03)
    assert columns["sphericity"][0] == pytest.approx(1.0, abs=0.02)
    assert columns["centroid_x"][0] == pytest.approx(32.0, abs=0.05)
    assert columns["centroid_z"][0] == pytest.approx(10.0, abs=0.05)
    assert not bool(columns["touches_bounds"][0])


def test_strips_weld_into_the_same_closed_mesh_as_one_pass():
    vol = _sphere_volume()
    whole, _ = _iso(vol, strip=64)
    strips, _ = _iso(vol, strip=3)
    assert strips.n_vertices == whole.n_vertices
    assert strips.n_triangles == whole.n_triangles
    assert len(mesh_mod.boundary_vertices(strips)) == 0
    # Euler characteristic of a sphere.
    edges = len(np.unique(np.sort(np.concatenate([strips.triangles[:, [0, 1]],
                                                  strips.triangles[:, [1, 2]],
                                                  strips.triangles[:, [2, 0]]]), axis=1),
                          axis=0))
    assert strips.n_vertices - edges + strips.n_triangles == 2


def test_the_same_request_makes_the_same_bytes():
    vol = _sphere_volume()
    a = mesh_mod.to_bytes(_iso(vol, smoothing=mesh_mod.Smoothing.preset("light"))[0])
    b = mesh_mod.to_bytes(_iso(vol, smoothing=mesh_mod.Smoothing.preset("light"))[0])
    assert hashlib.sha256(a).hexdigest() == hashlib.sha256(b).hexdigest()


def test_the_file_round_trips_and_aligns_its_arrays():
    surface, _ = _iso(_sphere_volume(), smoothing=mesh_mod.Smoothing.preset("medium"))
    data = mesh_mod.to_bytes(surface, {"level": 0})
    back = mesh_mod.from_bytes(data)
    assert np.array_equal(back.positions, surface.positions)
    assert np.array_equal(back.triangles, surface.triangles)
    assert np.array_equal(back.object_id, surface.object_id)
    assert back.positions_raw is not None
    assert back.header["level"] == 0 and back.header["vertices"] == surface.n_vertices
    for entry in back.header["arrays"]:
        assert entry["offset"] % 4 == 0, entry
    with pytest.raises(mesh_mod.MeshError):
        mesh_mod.from_bytes(b"nope" + data[4:])


def test_smoothing_is_capped_and_reported():
    surface, report = _iso(_sphere_volume(), smoothing=mesh_mod.Smoothing.preset("strong"))
    moved = np.abs(surface.positions - surface.positions_raw)
    # Half a voxel per axis, in voxels.
    assert moved.max() <= 0.5 + 1e-4
    assert report["smoothing"]["iterations"] == mesh_mod.Smoothing.PRESETS["strong"]
    assert report["smoothing"]["max_displacement"] > 0
    assert abs(report["volume_change_pct"]) < 3


def test_pieces_are_numbered_by_size_and_small_ones_dropped():
    vol = np.zeros((16, 48, 48), np.float32)
    vol[3:13, 5:25, 5:25] = 100     # big box
    vol[6:9, 34:38, 34:38] = 100    # small box
    big, report = _iso(vol, scale=(1, 1, 1))
    assert report["pieces"] == 2 and big.n_objects == 2
    columns = mesh_mod.measure(big)
    order = np.argsort(columns["object"])
    assert columns["mesh_volume_vox3"][order][0] > columns["mesh_volume_vox3"][order][1]
    kept, report = _iso(vol, scale=(1, 1, 1), min_size=200)
    assert report["dropped_small"] == 1 and kept.n_objects == 1


def test_a_structure_cut_by_the_edge_is_capped_and_flagged():
    vol = np.zeros((10, 32, 32), np.float32)
    vol[:, 8:20, 8:20] = 100        # runs through every plane
    surface, _ = _iso(vol, scale=(1, 1, 1))
    columns = mesh_mod.measure(surface, dims=(32, 32, 10))
    assert bool(columns["closed"][0]) and bool(columns["touches_bounds"][0])


def test_nothing_above_the_isovalue_says_so():
    with pytest.raises(marching.NoSurface):
        _iso(np.zeros((6, 16, 16), np.float32))


def test_preprocessing_runs_with_a_halo_so_strips_match_one_pass():
    rng = np.random.default_rng(1)
    vol = _sphere_volume() + rng.normal(0, 3, (20, 64, 64)).astype(np.float32)
    pre = isosurface.Preprocess(sigma_um=1.0)
    whole, _ = _iso(vol, pre=pre, voxel_um=(SX, SX, SZ), strip=64)
    strips, _ = _iso(vol, pre=pre, voxel_um=(SX, SX, SZ), strip=4)
    assert strips.n_triangles == whole.n_triangles
    np.testing.assert_allclose(np.sort(strips.positions, axis=0),
                               np.sort(whole.positions, axis=0), atol=1e-4)


def test_background_subtraction_lifts_a_dim_object_off_a_bright_bed():
    vol = np.full((12, 64, 64), 500.0, np.float32)
    vol += (400 * np.arange(64) / 63).astype(np.float32)   # uneven illumination
    vol[4:8, 10:20, 10:20] += 150    # dim object on the dim half
    vol[4:8, 10:20, 44:54] += 150    # same object on the bright half
    pre = isosurface.Preprocess(background_um=12.0)
    surface, report = _iso(vol, level=60.0, pre=pre, voxel_um=(1, 1, 1), scale=(1, 1, 1),
                           min_size=50)
    assert surface.n_objects == 2, report


def test_grids_map_coarse_voxels_onto_level_zero():
    grid = marching.Grid.of((4, 10, 10), factor_xy=(4, 4), z_stride=2, origin=(3, 5, 6))
    p = grid.to_level0(np.array([[0, 0, 0], [1, 1, 1]], np.float32))
    # Column 3 of a 4x level covers level-0 columns 12..15, centre 13.5.
    assert p[0].tolist() == pytest.approx([13.5, 21.5, 6.5])
    assert p[1].tolist() == pytest.approx([17.5, 25.5, 8.5])


# -- label surfaces --------------------------------------------------------------------


def _ellipsoid(labels, centre, radii, label):
    zz, yy, xx = np.ogrid[:labels.shape[0], :labels.shape[1], :labels.shape[2]]
    inside = (((zz - centre[0]) / radii[0]) ** 2 + ((yy - centre[1]) / radii[1]) ** 2
              + ((xx - centre[2]) / radii[2]) ** 2) <= 1.0
    labels[inside] = label
    return int(inside.sum())


class _Labels:
    def __init__(self, array):
        self.array = array
        self.depth, self.height, self.width = array.shape

    def level_shapes(self):
        return [[self.height, self.width]]

    def read(self, level, z0, z1, rows, cols):
        return self.array[z0:z1 + 1, rows, cols]


class _Pyramid(_Labels):
    """Levels made as the label store makes them: every other row and column."""

    def __init__(self, array, n_levels=3):
        super().__init__(array)
        self.levels = [array[:, ::2 ** k, ::2 ** k] for k in range(n_levels)]

    def level_shapes(self):
        return [[int(a.shape[1]), int(a.shape[2])] for a in self.levels]

    def read(self, level, z0, z1, rows, cols):
        return self.levels[level][z0:z1 + 1, rows, cols]


def _label_scene():
    labels = np.zeros((20, 128, 128), np.uint16)
    counts = {1: _ellipsoid(labels, (8, 40, 40), (5, 12, 12), 1),
              7: _ellipsoid(labels, (10, 90, 85), (6, 15, 10), 7),
              3: _ellipsoid(labels, (0, 100, 20), (3, 8, 8), 3)}   # clipped at z 0
    return labels, counts


def _extract(labels, grid=None, **kwargs):
    grid = grid or marching.Grid.of(labels.shape)
    return label_surfaces.extract_labels(_Labels(labels), 0, grid, **kwargs)


def _sha(surface):
    return hashlib.sha256(mesh_mod.to_bytes(surface)).hexdigest()


def test_every_object_gets_its_own_closed_surface_under_its_id():
    labels, counts = _label_scene()
    before = labels.copy()
    ids, voxels = label_surfaces.label_counts(_Labels(labels))
    assert ids.tolist() == [1, 3, 7]
    assert dict(zip(ids.tolist(), voxels.tolist())) == counts
    surface, report = _extract(labels, smoothing=mesh_mod.Smoothing.preset("medium"))
    assert surface.ids().tolist() == [1, 3, 7] and report["missing_count"] == 0
    assert report["engine"]["algorithm"] == "surface_nets_multilabel"
    assert np.array_equal(labels, before)
    columns = mesh_mod.measure(surface, dims=(128, 128, 20))
    assert columns["closed"].all()
    for label, volume_mesh, touches in zip(columns["object"], columns["mesh_volume_vox3"],
                                           columns["touches_bounds"]):
        # The surface of the voxels, a little inside them (smoothing, inset).
        assert volume_mesh == pytest.approx(counts[int(label)], rel=0.15), label
        assert bool(touches) == (int(label) == 3)


def test_every_surface_faces_out_of_its_cell():
    labels, _ = _label_scene()
    surface, _ = _extract(labels, inset=0.0)
    for label in (1, 3, 7):
        mine = mesh_mod.subset(surface, [label])
        assert mesh_mod.signed_volume(mine.positions, mine.triangles) > 0, label


def test_a_selection_makes_only_those_surfaces_and_the_inset_is_applied():
    labels, _ = _label_scene()
    plain, _ = _extract(labels, ids=[7], inset=0.0)
    inset, report = _extract(labels, ids=[7], inset=0.25)
    assert plain.ids().tolist() == [7] and report["inset_vox"] == 0.25
    assert mesh_mod.signed_volume(inset.positions, inset.triangles) < \
        mesh_mod.signed_volume(plain.positions, plain.triangles)


def test_a_selection_is_the_same_mesh_as_that_cell_of_the_whole():
    labels, _ = _label_scene()
    whole, _ = _extract(labels, inset=0.0)
    one, _ = _extract(labels, ids=[7], inset=0.0)
    ref = mesh_mod.subset(whole, [7])
    assert np.array_equal(one.positions, ref.positions)
    assert np.array_equal(one.triangles, ref.triangles)


def test_requested_ids_absent_from_the_mask_are_reported_missing():
    labels, _ = _label_scene()
    ids, counts = label_surfaces.label_counts(_Labels(labels), ids=[7, 99])
    assert ids.tolist() == [7, 99] and counts[1] == 1     # one coarse pixel, for planning
    surface, report = _extract(labels, ids=[7, 99])
    assert surface.ids().tolist() == [7]
    assert report["missing"] == [99] and report["missing_count"] == 1


def test_neighbours_that_touch_stay_two_surfaces():
    labels = np.zeros((8, 32, 32), np.uint16)
    labels[2:6, 8:16, 8:16] = 4
    labels[2:6, 8:16, 16:24] = 5     # shares a face with 4
    surface, _ = _extract(labels)
    assert surface.ids().tolist() == [4, 5]
    columns = mesh_mod.measure(surface)
    assert columns["closed"].all()
    a = surface.positions[surface.object_id == 4][:, 0].max()
    b = surface.positions[surface.object_id == 5][:, 0].min()
    assert a < b                     # the inset parts them


def test_a_cell_cut_by_the_edge_is_capped_on_its_faces():
    labels, counts = _label_scene()
    surface, _ = _extract(labels, ids=[3], inset=0.0)
    columns = mesh_mod.measure(surface, dims=(128, 128, 20))
    assert bool(columns["closed"][0]) and bool(columns["touches_bounds"][0])
    # Capped on the outer faces of plane 0, half a voxel outside its centre.
    assert surface.positions[:, 2].min() == pytest.approx(-0.5)
    assert columns["mesh_volume_vox3"][0] == pytest.approx(counts[3], rel=0.15)


def test_blocks_weld_into_the_same_mesh_as_one_block():
    """Tiles of 17 px and slabs of a few planes -- every seam crossed by a
    cell -- make the same bytes as the volume read whole."""
    labels, _ = _label_scene()
    smooth = mesh_mod.Smoothing.preset("medium")
    whole, report = _extract(labels, smoothing=smooth)
    assert report["engine"]["blocks"] == 1
    tiled, report = _extract(labels, smoothing=smooth, tile=17, block_voxels=17 * 17 * 4)
    assert report["engine"]["blocks"] > 20 and report["engine"]["seams"] > 10
    assert _sha(tiled) == _sha(whole)
    assert len(mesh_mod.boundary_vertices(tiled)) == 0


def test_thread_count_does_not_change_the_bytes():
    labels, _ = _label_scene()
    one, _ = _extract(labels, tile=24, threads=1)
    four, report = _extract(labels, tile=24, threads=4)
    assert report["engine"]["threads"] == 4
    assert _sha(one) == _sha(four)


def test_tiles_without_an_object_are_not_read():
    labels, _ = _label_scene()
    reads = []

    class Counting(_Labels):
        def read(self, level, z0, z1, rows, cols):
            reads.append((rows.start, cols.start))
            return super().read(level, z0, z1, rows, cols)

    grid = marching.Grid.of(labels.shape)
    whole, _ = label_surfaces.extract_labels(Counting(labels), 0, grid, tile=32, inset=0.0)
    every = len(reads)
    reads.clear()
    regions = np.array([[0, 19, 70, 110, 70, 100]])     # around cell 7 only
    only, _ = label_surfaces.extract_labels(Counting(labels), 0, grid, ids=[7], regions=regions,
                                            tile=32, inset=0.0)
    assert len(reads) < every / 2
    ref = mesh_mod.subset(whole, [7])
    assert np.array_equal(only.positions, ref.positions)


def test_the_interpreted_kernel_matches_the_compiled_one(monkeypatch):
    from plexora.server.surface import nets
    from plexora.server.utils import jit

    if not jit.enabled():
        pytest.skip("numba is off: every run is interpreted already")
    labels, _ = _label_scene()
    labels = labels[:, 24:112, 8:104].copy()      # small: the interpreted loops are slow
    compiled, _ = _extract(labels, tile=40)
    for name in ("_count", "_fill", "_pool_runs", "_corners", "_distinct", "_triangulate"):
        monkeypatch.setattr(nets, name, getattr(nets, name).py_func)
    interpreted, _ = _extract(labels, tile=40)
    assert _sha(interpreted) == _sha(compiled)


def test_smoothing_reports_each_iteration():
    labels, _ = _label_scene()
    stages, ticks = [], []
    _extract(labels, smoothing=mesh_mod.Smoothing.preset("medium"), stage=stages.append,
             progress=lambda done, total: ticks.append((done, total)))
    assert stages == ["smoothing"]
    iterations = mesh_mod.Smoothing.PRESETS["medium"]
    assert ticks[-iterations:] == [(i + 1, iterations) for i in range(iterations)]


def test_counts_from_the_coarsest_level_plan_the_same_level():
    labels = np.zeros((12, 256, 256), np.uint32)
    exact = {}
    for n in range(12):
        exact[n + 1] = _ellipsoid(labels, (6, 30 + 60 * (n % 4), 40 + 70 * (n // 4)),
                                  (4, 18, 14), n + 1)
    pyramid = _Pyramid(labels)
    ids, counts = label_surfaces.label_counts(pyramid)
    assert ids.tolist() == sorted(exact)
    np.testing.assert_allclose(counts, [exact[i] for i in ids.tolist()], rtol=0.1)
    for quality in levels.QUALITIES:
        assert levels.plan_labels(pyramid, counts, quality=quality)[0] == \
            levels.plan_labels(pyramid, np.array(list(exact.values())), quality=quality)[0]


def test_pooled_runs_take_the_label_on_most_planes_and_keep_a_thin_one():
    from plexora.server.surface import nets

    planes = np.zeros((6, 1, 4), np.uint32)
    planes[:, 0, 0] = [2, 2, 3, 0, 0, 0]          # 2 on most of run 0
    planes[:, 0, 1] = [0, 5, 6, 0, 0, 0]          # a tie: the earlier plane's
    planes[:, 0, 2] = [0, 0, 0, 0, 9, 0]          # one plane thick: kept
    planes[:, 0, 3] = [4, 4, 4, 4, 4, 4]
    pooled = nets.pool(planes, 3)
    assert pooled[:, 0, :].tolist() == [[2, 5, 0, 4], [0, 0, 9, 4]]
    assert nets.pool(planes[:5], 3)[1, 0, :].tolist() == [0, 0, 9, 4]   # a short last run


def test_a_coarse_level_of_a_fine_z_stack_pools_planes():
    """A label volume's levels are XY-only: at a level whose pixel is wider
    than the z step, planes are pooled so the grid's voxels are about cubic."""
    assert levels.label_z_stride(32, (0.14, 0.14, 0.28)) == 16
    assert levels.label_z_stride(1, (0.14, 0.14, 0.28)) == 1
    assert levels.label_z_stride(4, (0.5, 0.5, 1.5)) == 1
    assert levels.label_z_stride(8, (0.5, 0.5, 1.5)) == 3
    assert levels.label_z_stride(32, None) == 1
    assert levels.label_z_stride(64, (1.0, 1.0, 0.1), depth=20) == 20


def test_pooled_planes_keep_each_cell_closed_and_in_place():
    labels = np.zeros((40, 64, 64), np.uint16)
    counts = {1: _ellipsoid(labels, (14, 20, 20), (10, 6, 6), 1),
              2: _ellipsoid(labels, (26, 44, 40), (12, 7, 9), 2)}
    labels[33, 50:58, 6:14] = 9          # one plane thick: pooled, not lost
    full, _ = _extract(labels)
    pooled, report = _extract(labels, marching.Grid.of((10, 64, 64), z_stride=4), z_stride=4)
    assert pooled.ids().tolist() == [1, 2, 9] and report["missing_count"] == 0
    assert pooled.n_triangles < full.n_triangles / 2
    columns = mesh_mod.measure(pooled, dims=(64, 64, 40))
    assert columns["closed"].all()
    for label in (1, 2):
        mine = pooled.positions[pooled.object_id == label]
        ref = full.positions[full.object_id == label]
        # In level-0 voxels, where the full-resolution surface is: z within
        # one pooled run of it.
        assert np.abs(mine.mean(axis=0) - ref.mean(axis=0)).max() < 2.0, label
        assert ref[:, 2].min() - 4 <= mine[:, 2].min() <= ref[:, 2].min() + 4, label
        assert ref[:, 2].max() - 4 <= mine[:, 2].max() <= ref[:, 2].max() + 4, label
    sizes = dict(zip(columns["object"].tolist(), columns["mesh_volume_vox3"].tolist()))
    assert sizes[1] == pytest.approx(counts[1], rel=0.35)


def test_the_planner_counts_pooled_planes_and_fits_a_fine_z_stack():
    """10 000 nuclei of a 200-plane stack at 0.14 um pixels and a 0.28 um z
    step outgrew the browser at every XY-only level; pooling planes fits them."""
    class Stack(_Labels):
        def level_shapes(self):
            return [[4096 // 2 ** k, 4096 // 2 ** k] for k in range(6)]

    stack = Stack(np.zeros((200, 2, 2), np.uint16))
    counts = np.full(10_000, 140_000)
    with pytest.raises(levels.SurfaceRefused):
        levels.plan_labels(stack, counts, quality="standard")
    level, estimate = levels.plan_labels(stack, counts, quality="standard",
                                         scale=(0.14, 0.14, 0.28))
    assert estimate <= levels.QUALITY["standard"][1]
    assert levels.label_z_stride(4096 / (4096 // 2 ** level), (0.14, 0.14, 0.28)) > 1


# -- levels and suggestions ------------------------------------------------------------


def test_the_label_triangle_estimate_is_within_a_quarter():
    labels, _ = _label_scene()
    ids, voxels = label_surfaces.label_counts(_Labels(labels))
    surface, _ = _extract(labels)
    estimate = levels.label_triangles(voxels)
    assert estimate == pytest.approx(surface.n_triangles, rel=0.25)


def test_exposed_faces_give_the_exact_triangle_count():
    labels, _ = _label_scene()
    faces = []
    for label in (1, 3, 7):
        inside = np.pad(labels == label, 1)
        faces.append([int((inside[:, :, 1:] != inside[:, :, :-1]).sum()),
                      int((inside[:, 1:, :] != inside[:, :-1, :]).sum()),
                      int((inside[1:] != inside[:-1]).sum())])
    surface, _ = _extract(labels)
    fx, fy, fz = np.asarray(faces).T
    assert levels.label_triangles(None, (fx, fy, fz)) == surface.n_triangles


def test_too_many_triangles_is_refused_with_the_estimate():
    class Big(_Labels):
        def level_shapes(self):
            return [[4096, 4096]]

    with pytest.raises(levels.SurfaceRefused) as refused:
        levels.plan_labels(Big(np.zeros((2, 2, 2), np.uint16)), np.full(2_000_000, 4000),
                           quality="standard")
    assert refused.value.estimate["triangles"] > levels.RESIDENT_TRIANGLES


def test_regions_are_clamped_and_whole_extents_drop_out():
    region = levels.Region.from_dict({"box": [-10, 5, 50, 40], "z": [3, 99]},
                                     width=40, height=60, depth=10)
    assert region.box == (0, 5, 40, 45) and region.planes == (3, 9)
    assert levels.Region.from_dict({"box": [0, 0, 40, 60]}, width=40, height=60,
                                   depth=10).to_dict() is None
    with pytest.raises(levels.SurfaceRefused):
        levels.Region.from_dict({"box": [100, 100, 5, 5]}, width=40, height=60, depth=10)


def test_suggestions_split_two_populations_in_raw_units():
    rng = np.random.default_rng(0)
    block = np.concatenate([rng.normal(200, 20, 90_000), rng.normal(1500, 100, 10_000)])
    answer = suggest.suggest(block.astype(np.float32))
    for name in ("otsu", "triangle", "gmm"):
        assert 250 < answer["thresholds"][name] < 1400, (name, answer["thresholds"])
    assert answer["default"] == answer["thresholds"]["otsu"]
    assert len(answer["histogram"]["counts"]) == suggest.BINS
