"""Route-level check of the quick-view landing page backend: POST /quick_view
registers a datasource from a bare local path (no CSV/segmentation) and
returns a redirect into the viewer; GET /generated/rgb/<name> serves the
flat image for an RGB datasource. Isolated from the real app data directory
via monkeypatching plexora.data_path/config_json_path, matching the
pattern in test_datasource_config_routes.py.
"""

import json

import numpy as np
import tifffile
from PIL import Image

import plexora
from tests.ngff_fixtures import write_ngff, write_plate_like, write_spatialdata_like
from tests.node_harness import node_process  # noqa: F401 - fixture


def _write_image(path, size=64, channels=2):
    rng = np.random.default_rng(0)
    tifffile.imwrite(path, rng.integers(1, 255, size=(channels, size, size), dtype=np.uint8))


def _write_png(path, size=32):
    Image.fromarray(np.zeros((size, size, 3), dtype=np.uint8)).save(path)


def test_quick_view_registers_ome_tiff_and_redirects(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    image_path = tmp_path / "sample.ome.tif"
    _write_image(image_path)
    client = plexora.app.test_client()

    response = client.post("/quick_view", json={"path": str(image_path)})

    assert response.status_code == 200
    data = response.get_json()
    assert data["success"] is True
    assert data["name"] == "sample"
    assert data["redirect"] == "/sample"

    config = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert config["sample"]["image_kind"] == "ome_tiff"


def test_quick_view_dedupes_name_on_repeat_registration(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    image_path = tmp_path / "sample.ome.tif"
    _write_image(image_path)
    client = plexora.app.test_client()

    first = client.post("/quick_view", json={"path": str(image_path)}).get_json()
    second = client.post("/quick_view", json={"path": str(image_path)}).get_json()

    assert first["name"] == "sample"
    assert second["name"] == "sample_2"


def test_quick_view_rejects_missing_file(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    client = plexora.app.test_client()

    response = client.post("/quick_view", json={"path": str(tmp_path / "nope.tif")})

    assert response.status_code == 400
    assert response.get_json()["success"] is False


def test_quick_view_rejects_unsupported_extension(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    bogus = tmp_path / "notes.csv"
    bogus.write_text("a,b\n1,2\n", encoding="utf-8")
    client = plexora.app.test_client()

    response = client.post("/quick_view", json={"path": str(bogus)})

    assert response.status_code == 400
    assert response.get_json()["success"] is False


def test_quick_view_registers_rgb_and_serves_image(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    png_path = tmp_path / "photo.png"
    _write_png(png_path)
    client = plexora.app.test_client()

    response = client.post("/quick_view", json={"path": str(png_path)})
    assert response.status_code == 200
    data = response.get_json()
    assert data["name"] == "photo"

    config = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert config["photo"]["image_kind"] == "rgb"

    image_response = client.get("/generated/rgb/photo")
    assert image_response.status_code == 200
    assert image_response.mimetype == "image/png"


# -- OME-Zarr, which arrives as a folder ------------------------------------
#
# The landing page's existence check was `.is_file()`, which is the one line
# that made a store impossible to open: an OME-Zarr image IS a directory, and so
# is the SpatialData store somebody drags in whole.


def test_quick_view_registers_an_ome_zarr_store(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    store = write_ngff(tmp_path / "sample.ome.zarr", shape=(2, 128, 128), levels=1)
    client = plexora.app.test_client()

    response = client.post("/quick_view", json={"path": str(store)})

    assert response.status_code == 200
    data = response.get_json()
    assert data["success"] is True
    assert data["name"] == "sample"

    config = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert config["sample"]["image_kind"] == "ome_zarr"


def test_quick_view_resolves_a_store_root_to_its_image(tmp_path, monkeypatch):
    """Dropping the store root is the natural gesture; the project is named for
    what was dropped, and points at the image group inside it."""
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    store = write_spatialdata_like(tmp_path / "sample.zarr", shape=(1, 64, 64),
                                   levels=1)
    client = plexora.app.test_client()

    data = client.post("/quick_view", json={"path": str(store)}).get_json()

    assert data["name"] == "sample"
    config = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert config["sample"]["channelFile"] == str(store / "images" / "morphology")


def test_quick_view_reopens_a_store_instead_of_duplicating_it(tmp_path, monkeypatch):
    """The duplicate check compares the RESOLVED element path, since that is
    what a registration records -- comparing the store root would miss."""
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    store = write_spatialdata_like(tmp_path / "sample.zarr", shape=(1, 64, 64),
                                   levels=1)
    client = plexora.app.test_client()

    first = client.post("/quick_view", json={"path": str(store)}).get_json()
    second = client.post("/quick_view", json={"path": str(store)}).get_json()

    assert first["name"] == second["name"] == "sample"


def test_quick_view_names_the_candidates_in_an_ambiguous_store(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    store = write_spatialdata_like(tmp_path / "sample.zarr",
                                   elements=("dapi", "morphology"),
                                   shape=(1, 64, 64), levels=1)
    client = plexora.app.test_client()

    response = client.post("/quick_view", json={"path": str(store)})

    assert response.status_code == 400
    error = response.get_json()["error"]
    assert "dapi" in error and "morphology" in error


def test_quick_view_sends_a_plate_to_one_of_its_fields(tmp_path, monkeypatch):
    """A plate is hundreds of images, so there is nothing to auto-pick. What the
    user needs back is not "no image here" -- it is a path that works."""
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    store = write_plate_like(tmp_path / "screen.zarr", wells=("B/2", "C/3"),
                             fields=("0", "1"), shape=(1, 64, 64), levels=1)
    client = plexora.app.test_client()

    response = client.post("/quick_view", json={"path": str(store)})

    assert response.status_code == 400
    error = response.get_json()["error"]
    assert "plate" in error and "4 images" in error
    # The path it suggests is one the next request can actually use.
    suggested = str(tmp_path / error.rsplit(" ", 1)[-1].rstrip("."))
    assert client.post("/quick_view", json={"path": suggested}).status_code == 200


def test_quick_view_names_plate_fields_apart(tmp_path, monkeypatch):
    """Every field in a plate is called "0" or "1". Named on that alone the
    second one would collide with the first and tell nobody which well it is."""
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    store = write_plate_like(tmp_path / "screen.zarr", wells=("B/2", "C/3"),
                             fields=("0", "1"), shape=(1, 64, 64), levels=1)
    client = plexora.app.test_client()

    names = [client.post("/quick_view", json={"path": str(store / well / field)})
             .get_json()["name"]
             for well in ("B/2", "C/3") for field in ("0", "1")]

    assert names == ["screen_B_2_0", "screen_B_2_1", "screen_C_3_0", "screen_C_3_1"]


def test_quick_view_rejects_a_plain_folder(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    folder = tmp_path / "pictures"
    folder.mkdir()
    client = plexora.app.test_client()

    response = client.post("/quick_view", json={"path": str(folder)})

    assert response.status_code == 400
    assert "folder" in response.get_json()["error"]


def test_generated_rgb_rejects_non_rgb_datasource(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    image_path = tmp_path / "sample.ome.tif"
    _write_image(image_path)
    client = plexora.app.test_client()
    client.post("/quick_view", json={"path": str(image_path)})

    response = client.get("/generated/rgb/sample")
    assert response.status_code == 404


# -- an image on another machine -------------------------------------------
#
# "Take a quick look" stopped meaning anything the moment somebody's slide was
# on a cluster: the landing page took a path, and a path is only an answer
# about the machine Plexora is running on. It is the same act either way, so it
# is the same one gesture -- no CSV, no mask, straight into the viewer.


def test_quick_view_opens_an_image_that_is_on_a_node(tmp_path, node_process):
    from plexora.server.models.project import Project
    from tests.node_harness import register

    image_path = tmp_path / "slide.ome.tif"
    _write_image(image_path, size=128, channels=3)
    node = node_process(f"image:slide={image_path}")
    register("hpc", node)
    client = plexora.app.test_client()

    answer = client.post("/quick_view", json={"path": "node://hpc/slide"})

    assert answer.status_code == 200, answer.get_json()
    data = answer.get_json()
    assert data["success"] is True and data["redirect"] == f"/{data['name']}"

    # A real project, whose image is a binding rather than a path -- the
    # primary never records a path on another machine.
    project = Project.load(data["name"])
    assert project.resource("image").node == "hpc"
    assert project.image.width == 128
    # And the geometry came from the node, not from a guess.
    assert len(project.image.channels) == 3


def test_quick_view_reopens_the_same_node_image_rather_than_duplicating(
        tmp_path, node_process):
    """The local branch dedupes on the resolved file path. A node-backed
    project has none to compare -- what it has is the binding."""
    from tests.node_harness import register

    image_path = tmp_path / "slide.ome.tif"
    _write_image(image_path, size=128, channels=3)
    node = node_process(f"image:slide={image_path}")
    register("hpc", node)
    client = plexora.app.test_client()

    first = client.post("/quick_view", json={"path": "node://hpc/slide"}).get_json()
    second = client.post("/quick_view", json={"path": "node://hpc/slide"}).get_json()

    assert first["name"] == second["name"]


def test_quick_view_says_which_address_it_could_not_read(tmp_path):
    """Not "File does not exist": `Path("node://hpc/slide")` is a valid
    relative path that exists nowhere, and that refusal answers a question
    nobody asked."""
    client = plexora.app.test_client()

    answer = client.post("/quick_view", json={"path": "node://nosuchnode/slide"})

    assert answer.status_code == 400
    assert "nosuchnode" in answer.get_json()["error"]


def test_quick_view_refuses_a_malformed_node_address(tmp_path):
    client = plexora.app.test_client()

    answer = client.post("/quick_view", json={"path": "node://hpc"})

    assert answer.status_code == 400
    assert "node://<node>/<resource>" in answer.get_json()["error"]
