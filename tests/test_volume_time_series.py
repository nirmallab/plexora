"""A time series' cells: a label volume per frame, measured frame by frame,
linked across frames, and regions that hold one frame's rows.

What has to hold: a (T, Z, Y, X) mask is stored a frame at a time and served
at the frame the view names; measuring writes one row per object per frame
(with the frame in `t`, the table's `t` role); linking gives the same cell
the same `track` in every frame however each frame numbered it, and refuses
a jump farther than its cap; and a region drawn on one frame holds only that
frame's rows.
"""

import io
import time

import numpy as np
import pytest
import tifffile

import plexora
from plexora import datasets
from plexora.datasource import register_image_datasource
from plexora.server.models.project import Project
from plexora.server.volume import labels as volume_labels

#: Plexora 3D's analysis paths (plexora/licensing/manifest.py `volume`): these
#: tests exercise them, so they run on a Paid test licence.
pytestmark = pytest.mark.paid

FRAMES, DEPTH, SIZE = 3, 4, 64


@pytest.fixture
def client():
    return plexora.app.test_client()


def _series_image(path):
    rng = np.random.default_rng(0)
    data = rng.integers(50, 60, (FRAMES, 1, DEPTH, SIZE, SIZE)).astype(np.uint16)
    data[1] += 1000                                  # frame 1 is brighter everywhere
    metadata = {"axes": "TCZYX", "PhysicalSizeX": 0.5, "PhysicalSizeY": 0.5,
                "PhysicalSizeZ": 1.0, "TimeIncrement": 30.0, "TimeIncrementUnit": "s",
                "Channel": {"Name": ["DNA"]}}
    with tifffile.TiffWriter(path, bigtiff=True, ome=True) as writer:
        writer.write(data, metadata=metadata)
    return path


def _moving_labels():
    """Two cells; A moves 3 px right per frame, B stays. Each frame numbers
    them differently, as a per-frame segmentation does."""
    labels = np.zeros((FRAMES, DEPTH, SIZE, SIZE), dtype=np.uint16)
    ids = [(1, 2), (7, 3), (4, 9)]                   # (A, B) per frame
    for t, (a, b) in enumerate(ids):
        labels[t, 1:3, 10:18, 10 + 3 * t:18 + 3 * t] = a
        labels[t, 1:4, 40:50, 40:50] = b
    return labels, ids


def _project(tmp_path, name="series"):
    image = _series_image(tmp_path / f"{name}.ome.tif")
    labels, ids = _moving_labels()
    tifffile.imwrite(tmp_path / f"{name}_mask.tif", labels, photometric="minisblack")
    entry = register_image_datasource(name, str(image))
    assert entry["imageShape"]["t"] == FRAMES and entry["imageShape"]["z"] == DEPTH
    datasets.add_layers(name, tmp_path / f"{name}_mask.tif", wait=True)
    return labels, ids


def test_a_label_volume_per_frame_is_stored_a_frame_at_a_time(tmp_path):
    labels, _ = _moving_labels()
    built = volume_labels.build_label_volume(labels, tmp_path / "series_labels.zarr")
    volume = volume_labels.open_label_volume(built)
    assert volume.frames == FRAMES and volume.depth == DEPTH
    for t in range(FRAMES):
        frame = volume.frame(t)
        assert frame.t == t
        np.testing.assert_array_equal(np.asarray(frame.plane(1)["0"]), labels[t, 1])
    # One frame stays the layout every store had before series.
    single = volume_labels.build_label_volume(labels[0], tmp_path / "one_labels.zarr")
    flat = volume_labels.open_label_volume(single)
    assert flat.frames == 1 and flat.frame(2) is flat
    assert volume_labels.label_volume_shape(tmp_path / "missing.tif") is None


def test_label_tiles_follow_the_frame_the_view_names(tmp_path, client):
    labels, _ = _project(tmp_path, "tiles")
    project = Project.find("tiles")
    assert volume_labels.open_label_volume(project.segmentation.derived).frames == FRAMES
    from PIL import Image

    from plexora.server.models import data_model

    data_model.load_datasource("tiles", reload=True)
    # The mask's entry: the one whose key is not an image channel's.
    entry = next(item for item in data_model.config["tiles"]["imageData"]
                 if not str(item["src"]).rstrip("/").rsplit("_", 1)[-1].isdigit())
    src = entry["src"]
    for t in range(FRAMES):
        response = client.get(f"{src}0/0_0.png?view=t{t}z1")
        assert response.status_code == 200, response.get_data(as_text=True)
        rgba = np.asarray(Image.open(io.BytesIO(response.data)).convert("RGBA")).astype(np.uint32)
        ids = rgba[..., 0] | (rgba[..., 1] << 8) | (rgba[..., 2] << 16)
        np.testing.assert_array_equal(ids[:SIZE, :SIZE], labels[t, 1])


def test_a_series_is_measured_frame_by_frame_and_linked(tmp_path):
    from plexora.server.volume import objects, tracking

    labels, ids = _project(tmp_path)
    project = Project.find("series")
    summary = objects.measure_project_objects(project, units="physical")
    assert summary["objects"] == 2 * FRAMES and summary["attached"]
    import polars as pl

    table = pl.read_parquet(summary["table"])
    assert sorted(table["t"].unique().to_list()) == list(range(FRAMES))
    # Each frame's own pixels: frame 1 is 1000 brighter.
    by_frame = {t: table.filter(pl.col("t") == t)["DNA"].mean() for t in range(FRAMES)}
    assert by_frame[1] - by_frame[0] > 900
    roles = Project.find("series").dataset.roles
    assert roles.t == "t" and roles.z == "Z"

    result = tracking.track_project_objects(Project.find("series"))
    table = pl.read_parquet(summary["table"])
    tracks = {(row["t"], row["label"]): row["track"] for row in table.iter_rows(named=True)}
    a = [tracks[(t, ids[t][0])] for t in range(FRAMES)]
    b = [tracks[(t, ids[t][1])] for t in range(FRAMES)]
    assert len(set(a)) == 1 and len(set(b)) == 1 and a[0] != b[0], tracks
    assert result["tracks"] == 2 and result["full_length"] == 2
    assert result["units"] == "µm" and "divisions and merges" in result["not_done"]
    assert "track" in Project.find("series").dataset.columns.metadata


def test_a_cap_tighter_than_a_cells_step_breaks_its_track(tmp_path):
    from plexora.server.volume import objects, tracking

    _project(tmp_path, "capped")
    objects.measure_project_objects(Project.find("capped"), units="physical")
    # A moves 3 px = 1.5 µm a frame; a 1 µm cap cannot follow it.
    result = tracking.track_project_objects(Project.find("capped"), max_distance=1.0)
    assert result["tracks"] == 1 + FRAMES and result["max_distance_from"] == "given"


def test_the_assignment_takes_the_least_total_distance():
    from plexora.server.volume import tracking

    before = np.array([[0.0, 0, 0], [10.0, 0, 0]])
    # Greedy would give before[1] the closer point 6 and leave before[0] 11 away;
    # the assignment pairs 0 with 4 (4) and 1 with 6 (4), 8 in all.
    after = np.array([[6.0, 0, 0], [4.0, 0, 0]])
    pairs = sorted(tracking.match(before, after, max_distance=20))
    assert pairs == [(0, 1), (1, 0)]
    assert tracking.match(before, after, max_distance=1) == []


def test_a_region_on_one_frame_holds_that_frames_rows():
    from plexora.plugins.roi.server import mapping

    square = {"type": "Polygon", "coordinates": [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]]}
    features = [
        {"id": "a", "name": "on frame 1", "category_id": "c", "geometry": square,
         "extent": {"kind": "plane", "z": 0, "t": 1}},
        {"id": "b", "name": "every frame", "category_id": "c", "geometry": square},
    ]
    categories = [{"id": "c", "label": "Region"}]
    xs, ys = np.array([5.0, 5.0, 5.0]), np.array([5.0, 5.0, 5.0])
    zs, ts = np.zeros(3), np.array([0, 1, 2])
    _, names = mapping.assign(features, categories, xs, ys, zs, ts)
    assert names[1] == "on frame 1 | every frame" or names[1].startswith("on frame 1")
    assert "on frame 1" not in names[0] and "on frame 1" not in names[2]
    assert all("every frame" in name for name in names)


def test_the_page_links_a_measured_series(tmp_path, client):
    _project(tmp_path, "page")
    measured = client.post("/volume/page/objects", json={"units": "physical"})
    assert measured.status_code == 202
    _settle(client, "page", "task")
    state = client.get("/volume/page/objects").get_json()
    assert state["measured"]["frames"] == FRAMES and state["measured"]["tracking"] is None
    started = client.post("/volume/page/tracks", json={})
    assert started.status_code == 202, started.get_data(as_text=True)
    state = _settle(client, "page", "tracks_task")
    assert state["tracks_task"]["status"] == "ready", state["tracks_task"]
    assert state["measured"]["tracking"]["tracks"] == 2
    assert client.post("/volume/page/tracks", json={"max_distance": -1}).status_code == 400


def _settle(client, name, key, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = client.get(f"/volume/{name}/objects").get_json()
        if (state.get(key) or {}).get("status") not in (None, "pending", "running"):
            return state
        time.sleep(0.05)
    raise AssertionError("the task did not finish")


def test_the_viewer_reads_the_frame_on_screens_rows(tmp_path, client):
    """With a time series' table attached, each request for rows can name a
    frame: the cells, the nearest cell to a click and Cell Explorer's values
    are then that frame's alone, so every cell id appears once."""
    from plexora.server.models import data_model
    from plexora.server.volume import objects

    _, ids = _project(tmp_path, "rows")
    objects.measure_project_objects(Project.find("rows"), units="physical")
    data_model.load_datasource("rows", reload=True)
    assert client.get("/volume/rows/info").get_json()["table_t"] == "t"

    every = data_model.get_all_cells("rows", ["label"], int)
    assert len(every) == 2 * FRAMES
    for t in range(FRAMES):
        labels = sorted(int(v) for v in data_model.get_all_cells("rows", ["label"], int, t=t))
        assert labels == sorted(ids[t]), t
    # A on frame 2 sits at x 16..23; the click finds frame 2's A, not frame 0's.
    nearest = data_model.query_for_closest_cell(19.5, 13.5, "rows", t=2)
    assert nearest["t"] == 2 and nearest["label"] == ids[2][0]
    values = client.get("/plugins/cell_explorer/api/values?datasource=rows&column=n_planes&t=1")
    assert values.status_code == 200, values.get_data(as_text=True)
    assert int(values.headers["X-Cell-Count"]) == 2
    everything = client.get("/plugins/cell_explorer/api/values?datasource=rows&column=n_planes")
    assert int(everything.headers["X-Cell-Count"]) == 2 * FRAMES
