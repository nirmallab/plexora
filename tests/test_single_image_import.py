"""One image, one pick: the narrowest thing `POST /import/sample` is asked to
do, and the one people do most.

There is one import ENGINE now, reached from several surfaces. What used to be
a second engine -- `POST /quick_view`, which could open exactly one image and
nothing else -- is gone, but both the case it existed for and the page that
called it are not: the home page still opens one pick in one gesture (see
tests/test_home_landing.py), and it does so through this route. So this file
follows that path, including the parts only ever exercised by a lone image: the
OME-Zarr store that arrives as a directory, the HCS plate that holds hundreds
of them, the repeat pick that must not become a second project, and the image
that is on another machine.

`GET /generated/rgb/<name>` is here for the same reason: it serves the whole
flat picture for an `rgb` datasource, which is what a dropped PNG becomes, and
it is the only route that reads that datasource's pixels.

The data directory is `tmp_path` -- the suite-wide `plexora_data_root` fixture
in conftest.py points the whole app at one per test, so a test only has to write
the `config.json` it wants to read back.
"""

import json

import numpy as np
import pytest
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


def _import(client, path, **extra):
    """One pick, as the dialog posts it. `paths` is always a list -- one image
    is the degenerate case of a set of files, not a different request."""
    return client.post("/import/sample", json={"paths": [str(path)], **extra})


def test_an_ome_tiff_registers_and_redirects(tmp_path):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    image_path = tmp_path / "sample.ome.tif"
    _write_image(image_path)
    client = plexora.app.test_client()

    response = _import(client, image_path)

    assert response.status_code == 200
    data = response.get_json()
    assert data["name"] == "sample"
    assert data["redirect"] == "/sample"
    # Nothing to build for a lone image: its pixels are already in a format the
    # tile route reads, so the viewer can be opened the moment this returns.
    assert data["pending"] is False

    config = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert config["sample"]["image_kind"] == "ome_tiff"


def test_the_same_image_reopens_its_project_rather_than_making_a_second(tmp_path):
    """Picking one image is one gesture and people repeat it -- the slide
    dropped this morning is the same slide this afternoon. A second project over
    one file is worse than useless: the gates, ROIs and figures saved against the
    first are simply absent from the second, with nothing on screen to say why.
    `_find_existing_datasource_for_image` is what makes the repeat a no-op, and
    it resolves both sides, so a relative path or a symlink to the same file
    still lands on the project that is already there.

    `existing` is how the answer says so: the sample was not registered again,
    and the dialog offers "Open it" rather than claiming to have imported
    anything.
    """
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    image_path = tmp_path / "sample.ome.tif"
    _write_image(image_path)
    client = plexora.app.test_client()

    first = _import(client, image_path).get_json()
    second = _import(client, image_path).get_json()

    assert first["name"] == "sample"
    assert second["name"] == "sample"
    assert second["redirect"] == "/sample"
    assert second["existing"] is True
    config = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert list(config) == ["sample"]


def test_a_second_image_of_the_same_name_is_offered_a_name_of_its_own(tmp_path):
    """What the dedupe suffix is still for. A project is named after the file,
    and `sample.ome.tif` in two folders is two slides -- one per cohort, one per
    run -- so the second must not open the first.

    The suffix is taken silently, because nobody CHOSE this name: it came off
    the filename. A name somebody typed is different and collides loudly, with
    a free one offered -- see `test_import_sample_routes.py`. That split is the
    whole rule: an instruction is honoured or refused, a derivation is adjusted.
    """
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    first_path = tmp_path / "monday" / "sample.ome.tif"
    second_path = tmp_path / "tuesday" / "sample.ome.tif"
    for path in (first_path, second_path):
        path.parent.mkdir()
        _write_image(path)
    client = plexora.app.test_client()

    first = _import(client, first_path).get_json()
    second = _import(client, second_path).get_json()

    assert first["name"] == "sample"
    assert second["name"] == "sample_2"
    # Two files, two projects, neither hidden behind the other.
    config = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert sorted(config) == ["sample", "sample_2"]


def test_a_missing_file_is_refused(tmp_path):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    client = plexora.app.test_client()

    response = _import(client, tmp_path / "nope.tif")

    # 400 now, with the reason in `error`. Quick view answered 200 with
    # `success: false`, which made every caller check a body field before it
    # could tell a refusal from an import.
    assert response.status_code == 400
    assert "does not exist" in response.get_json()["error"]


def test_a_file_plexora_cannot_read_is_refused(tmp_path):
    """Nothing recognised among the picks is the one thing that stops an import.

    Not "an unsupported extension", which is what quick view refused on: that
    route read images and only images, so a CSV was an error there. Here a table
    is an ordinary pick -- it becomes the sample's cells -- and the ladder in
    `import_proposal` tries content before it gives up. What is left is a file
    nothing recognises, and it says so rather than registering an empty sample.
    """
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    junk = tmp_path / "notes.bin"
    junk.write_bytes(b"\x00\x01\x02 not an image, not a table")
    client = plexora.app.test_client()

    response = _import(client, junk)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Nothing Plexora can read here."


def test_a_flat_picture_registers_as_rgb_and_is_served_whole(tmp_path):
    """A lone PNG stays a flat picture. It is converted to a tiled OME-TIFF only
    when it has to be the reference of a sample with other layers over it -- see
    `import_sample._tiled_picture` -- because opening a screenshot should stay
    instant."""
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    png_path = tmp_path / "photo.png"
    _write_png(png_path)
    client = plexora.app.test_client()

    response = _import(client, png_path)
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
# A store is a directory, and so is the SpatialData store somebody drags in
# whole. Quick view checked `.is_file()`, which is the one line that made every
# one of these impossible to open; the detection ladder answers directories
# first, so the cases below are about what it finds INSIDE one.


def test_an_ome_zarr_store_registers(tmp_path):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    store = write_ngff(tmp_path / "sample.ome.zarr", shape=(2, 128, 128), levels=1)
    client = plexora.app.test_client()

    response = _import(client, store)

    assert response.status_code == 200
    data = response.get_json()
    assert data["name"] == "sample"

    config = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert config["sample"]["image_kind"] == "ome_zarr"


def test_a_store_root_resolves_to_the_image_inside_it(tmp_path):
    """Dropping the store root is the natural gesture; the project is named for
    what was dropped, and points at the image group inside it -- not at a project
    called "morphology" after an element nobody named.

    The name keeps the `.zarr`, because a bundle is named after its directory
    and that is what the directory is called.
    """
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    store = write_spatialdata_like(tmp_path / "sample.zarr", shape=(1, 64, 64),
                                   levels=1)
    client = plexora.app.test_client()

    data = _import(client, store).get_json()

    # The store's name without the format's extension: a project called
    # `sample.zarr` puts it in the URL and on the card.
    assert data["name"] == "sample"
    config = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert config["sample"]["channelFile"] == str(
        store / "images" / "morphology")


def test_a_store_picked_twice_reopens_rather_than_duplicating(tmp_path):
    """The duplicate check compares the RESOLVED element path, since that is
    what a registration records -- comparing the store root would miss."""
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    store = write_spatialdata_like(tmp_path / "sample.zarr", shape=(1, 64, 64),
                                   levels=1)
    client = plexora.app.test_client()

    first = _import(client, store).get_json()
    second = _import(client, store).get_json()

    assert first["name"] == second["name"] == "sample"
    assert second["existing"] is True


def test_an_ambiguous_store_asks_which_image_rather_than_refusing(tmp_path):
    """Two images in one store used to be a refusal that named them both, since
    quick view had one field and nowhere to put a question. The dialog does have
    somewhere, so the candidates come back as a question with a default and the
    import proceeds either way -- a store the user cannot be bothered to answer
    about still opens, drawn in the first image, and the second is registered
    beside it as a layer rather than thrown away.
    """
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    store = write_spatialdata_like(tmp_path / "sample.zarr",
                                   elements=("dapi", "morphology"),
                                   shape=(1, 64, 64), levels=1)
    client = plexora.app.test_client()

    proposal = client.post("/import/inspect",
                           json={"paths": [str(store)]}).get_json()
    questions = [q for sample in proposal["samples"]
                 for q in sample["questions"] if q["id"] == "reference"]
    assert len(questions) == 1
    labels = [option["label"] for option in questions[0]["options"]]
    assert labels == ["dapi", "morphology"]

    response = _import(client, store)
    assert response.status_code == 200
    # One sample, drawn in the default image, with the other alongside it.
    assert [layer["id"] for layer in response.get_json()["layers"]] == [
        "images_morphology"]


def test_a_plate_offers_its_fields_and_imports_the_one_chosen(tmp_path):
    """A plate is hundreds of images, and which one is wanted is not something
    any heuristic can answer. Quick view refused and suggested a path to pick
    instead; here the fields are the options of a question, and answering it is
    what decides which field the sample is drawn in.
    """
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    store = write_plate_like(tmp_path / "screen.zarr", wells=("B/2", "C/3"),
                             fields=("0", "1"), shape=(1, 64, 64), levels=1)
    client = plexora.app.test_client()

    proposal = client.post("/import/inspect",
                           json={"paths": [str(store)]}).get_json()
    questions = [q for sample in proposal["samples"]
                 for q in sample["questions"] if q["id"] == "image"]
    assert len(questions) == 1
    assert [option["value"] for option in questions[0]["options"]] == [
        "B/2/0", "B/2/1", "C/3/0", "C/3/1"]

    # The answer is the field id the question offered, and what it decides is
    # which of the four the project actually reads.
    response = _import(client, store, answers={"image": "C/3/1"},
                       name="screen_C_3_1")
    assert response.status_code == 200
    config = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert config["screen_C_3_1"]["channelFile"] == str(store / "C" / "3" / "1")


def test_plate_fields_are_named_apart(tmp_path):
    """Every field in a plate is called "0" or "1". Named on that alone the
    second one collides with the first and tells nobody which well it is."""
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    store = write_plate_like(tmp_path / "screen.zarr", wells=("B/2", "C/3"),
                             fields=("0", "1"), shape=(1, 64, 64), levels=1)
    client = plexora.app.test_client()

    names = [_import(client, store / well / field).get_json()["name"]
             for well in ("B/2", "C/3") for field in ("0", "1")]

    assert names == ["screen_B_2_0", "screen_B_2_1", "screen_C_3_0",
                     "screen_C_3_1"]


def test_a_folder_holding_nothing_readable_is_refused(tmp_path):
    """A plain folder is scanned one level deep -- somebody who points at their
    home directory gets an answer rather than a five-minute walk -- and an empty
    one is the same refusal as a file nothing recognises."""
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    folder = tmp_path / "pictures"
    folder.mkdir()
    client = plexora.app.test_client()

    response = _import(client, folder)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Nothing Plexora can read here."


def test_generated_rgb_rejects_non_rgb_datasource(tmp_path):
    """The route sends a whole file, so it checks what the datasource IS rather
    than trusting the name in the url -- an OME-TIFF answered here would be tens
    of gigabytes down a route meant for a screenshot."""
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    image_path = tmp_path / "sample.ome.tif"
    _write_image(image_path)
    client = plexora.app.test_client()
    _import(client, image_path)

    response = client.get("/generated/rgb/sample")
    assert response.status_code == 404


# -- an image on another machine -------------------------------------------
#
# A path is only an answer about the machine Plexora is running on, and the
# moment somebody's slide is on a cluster it stops being one. `node://<node>/
# <resource>` goes in the same `paths` list as everything else and is inspected
# by the machine that can open it, so picking a slide off a cluster is the same
# gesture as picking one off a desk.


def test_an_image_on_a_node_becomes_a_binding(tmp_path, node_process):
    from plexora.server.models.project import Project
    from tests.node_harness import register

    image_path = tmp_path / "slide.ome.tif"
    _write_image(image_path, size=128, channels=3)
    node = node_process(f"image:slide={image_path}")
    register("hpc", node)
    client = plexora.app.test_client()

    answer = _import(client, "node://hpc/slide")

    assert answer.status_code == 200, answer.get_json()
    data = answer.get_json()
    assert data["redirect"] == f"/{data['name']}"

    # A real project, whose image is a binding rather than a path -- the
    # primary never records a path on another machine.
    project = Project.load(data["name"])
    assert project.resource("image").node == "hpc"
    assert project.image.width == 128
    # And the geometry came from the node, not from a guess.
    assert len(project.image.channels) == 3


def test_the_same_node_image_reopens_rather_than_duplicating(tmp_path,
                                                             node_process):
    """The local branch dedupes on the resolved file path. A node-backed
    project has none to compare -- what it has is the binding."""
    from tests.node_harness import register

    image_path = tmp_path / "slide.ome.tif"
    _write_image(image_path, size=128, channels=3)
    node = node_process(f"image:slide={image_path}")
    register("hpc", node)
    client = plexora.app.test_client()

    first = _import(client, "node://hpc/slide").get_json()
    second = _import(client, "node://hpc/slide").get_json()

    assert first["name"] == second["name"]


def test_an_address_naming_no_known_node_says_which_one(tmp_path):
    """Not "File does not exist": `Path("node://hpc/slide")` is a valid
    relative path that exists nowhere, and that refusal answers a question
    nobody asked."""
    client = plexora.app.test_client()

    answer = _import(client, "node://nosuchnode/slide")

    assert answer.status_code == 400
    assert "nosuchnode" in answer.get_json()["error"]


def test_a_malformed_node_address_is_refused(tmp_path):
    client = plexora.app.test_client()

    answer = _import(client, "node://hpc")

    assert answer.status_code == 400
    assert "node://<node>/<resource>" in answer.get_json()["error"]
