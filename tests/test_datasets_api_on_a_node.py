"""Registering a cohort whose files are on another machine.

The command this covers is `plexora dataset create PCA --from projects.json
--node hms-o2` -- a script naming fifty slides that live on a cluster, run on
the laptop the viewer runs on. The projects are registered HERE, because the
registry is what the Samples page reads; the bytes stay THERE, because a node
serves files and nothing else. Before this, the two could only be married by
clicking Import Sample fifty times.

Three things are being pinned. A path under a node is SHARED before it is
attached -- the node is serving nothing when a spec names a file on it, so
looking the path up as an id it was never going to match is the failure mode
to prevent. Every role is placed independently, so the commonest split of all
(slide on the cluster, quantification on the laptop) is sayable. And a file
that is not there is refused before a single pyramid is built, which is the
promise `create_dataset` already makes locally and which only holds remotely
because the check is a question put to the node rather than a change made
to it.

The node is a real second process, never a stub: what is being tested is a
seam between two machines, and a stub is a seam with nothing on its far side.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import polars as pl
import pytest
import tifffile

import plexora
from plexora import datasets as api
from plexora.server.models.project import Project
from tests.node_harness import node_process, register  # noqa: F401 - fixture


#: 512px, not 256: the node's own detector walks a pyramid, and the size the
#: rest of the node tests use is the size known to survive that walk.
SIZE = 512


def _image(directory, name="LSP11641.ome.tif"):
    """A three-channel fluorescence slide, the way mcmicro names one."""
    rng = np.random.default_rng(7)
    data = np.zeros((3, SIZE, SIZE), dtype=np.uint16)
    for index in range(3):
        data[index] = rng.poisson(40 * (index + 1), (SIZE, SIZE)).astype(np.uint16)
        data[index, 100:160, 100:160] += 4000 * (index + 1)
    path = directory / name
    tifffile.imwrite(path, data, photometric="minisblack")
    return path


def _mask(directory, name="cell.ome.tif"):
    """What a cellpose run writes, under the name mcmicro gives it.

    Nothing in `cell.ome.tif` says mask, which is the point: sparse integer
    ids over a background of zero is what says it, and only the machine
    holding the file can read that.
    """
    labels = np.zeros((SIZE, SIZE), dtype=np.uint32)
    for index in range(1, 6):
        top = index * 60
        labels[top:top + 30, top:top + 30] = index
    path = directory / name
    tifffile.imwrite(path, labels)
    return path


def _csv(directory, name="cells.csv"):
    path = directory / name
    pl.DataFrame({
        "CellID": np.arange(4, dtype=np.uint32),
        "X_centroid": np.linspace(1, 4, 4),
        "Y_centroid": np.linspace(1, 4, 4),
        "CD3": np.linspace(0, 3, 4),
        "area": np.linspace(10, 40, 4),
    }).write_csv(path)
    return path


@pytest.fixture
def o2(node_process):
    """A dynamic node serving nothing, registered as `hms-o2`.

    Serving nothing on purpose: that is what a `plexora connect` node is at
    the moment a spec file naming paths on it is read.
    """
    node = node_process(dynamic=True)
    register("hms-o2", node)
    return node


def _served(node):
    """Every resource the node is offering, by id. Asked of the node itself,
    not of this process's idea of it."""
    return {str(described["id"]): described
            for described in node.get("/node/v1/hello").get("resources") or []}


# --------------------------------------------------------------------------
# Saying where a file is
# --------------------------------------------------------------------------

def test_a_plain_path_under_an_entry_node_is_shared_and_attached(tmp_path, o2):
    """The whole feature in one call: a path on the cluster, a project here.

    Nobody shared the file first. The node was serving nothing, and what makes
    this work is that registration tells it to serve the path rather than
    looking the path up as an id.
    """
    slide = _image(tmp_path)

    name = plexora.create_project({"path": str(slide), "node": "hms-o2"})

    binding = Project.load(name).resource("image")
    assert binding.node == "hms-o2"
    assert binding.provider == "node"
    # No path is recorded, by design: the file is on another machine and that
    # machine's layout is not this one's business.
    assert Project.load(name).image.src == ""
    assert len(_served(o2)) == 1
    assert name == "LSP11641"


def test_the_object_form_with_node_null_keeps_one_file_on_this_machine(tmp_path, o2):
    """The per-field opt-out. Without it "slides on the cluster, table on my
    laptop" -- the commonest arrangement there is -- cannot be written down."""
    slide = _image(tmp_path)
    table = _csv(tmp_path)

    name = plexora.create_project(
        str(slide), data={"path": str(table), "node": None},
        cell_id="CellID", node="hms-o2")

    project = Project.load(name)
    assert project.resource("image").node == "hms-o2"
    # No binding at all is what "on this machine" is recorded as, and the
    # source is a path something here can open rather than an address.
    assert project.resource("table") is None
    assert project.dataset.src.endswith("cells.csv")
    assert Path(project.dataset.src).exists()


def test_a_node_address_string_still_works_as_the_ui_sends_it(tmp_path, o2):
    """`node://<node>/<path>` is what Import Sample posts. A spec that pastes
    one has said the same thing the form said, and must mean the same thing."""
    slide = _image(tmp_path)

    name = plexora.create_project(f"node://hms-o2/{slide}")

    assert Project.load(name).resource("image").node == "hms-o2"


def test_a_node_address_naming_a_resource_the_node_already_serves(tmp_path,
                                                                  node_process):
    """The other half of the address: an id, not a path.

    A node started with `--serve image:slide=...` is already offering the
    resource, and the address then names it directly -- no share to make.
    """
    slide = _image(tmp_path)
    node = node_process(f"image:slide={slide}")
    register("hms-o2", node)

    name = plexora.create_project("node://hms-o2/slide")

    assert Project.load(name).resource("image").resource_id == "slide"


def test_the_batch_default_node_reaches_every_entry_that_names_none(tmp_path, o2):
    """The fifty-image command, at two. `--node hms-o2` is what makes a list of
    bare paths a list of paths on the cluster."""
    first = _image(tmp_path, "LSP11641.ome.tif")
    second = _image(tmp_path, "LSP20209.ome.tif")

    dataset = plexora.create_dataset(
        "PCA", images=[str(first), str(second)], node="hms-o2")

    assert len(dataset) == 2
    for name in dataset.projects:
        assert Project.load(name).resource("image").node == "hms-o2"
    assert len(_served(o2)) == 2


# --------------------------------------------------------------------------
# One project, several machines
# --------------------------------------------------------------------------

def test_an_image_on_the_node_and_a_table_here_records_each_where_it_is(tmp_path, o2):
    """The split the plan exists for, stated per field rather than per project."""
    slide = _image(tmp_path)
    table = _csv(tmp_path)

    name = plexora.create_project(
        {"path": str(slide), "node": "hms-o2"}, data=str(table), cell_id="CellID")

    project = Project.load(name)
    assert project.resource("image").node == "hms-o2"
    assert project.resource("table") is None
    # The answer given here is an ANSWER, exactly as it is for an all-local
    # project: the machine a file is on changes nothing about that contract.
    manifest = api.project_manifest(name)["manifest"]
    assert manifest["role:cell_id"]["confirmed"] is True


def test_an_image_here_and_a_mask_on_the_node_goes_through_the_shared_writer(
        tmp_path, o2):
    """The reverse split: the slide is local, the mask was left beside the
    segmentation job that wrote it.

    This is the case where registration happens the way it always has and only
    the mask is held back -- and it has to reach `attach_segmentation`'s node
    branch, which is the one writer that knows how to address one.
    """
    slide = _image(tmp_path)
    mask = _mask(tmp_path)

    name = plexora.create_project(
        str(slide), segmentation={"path": str(mask), "node": "hms-o2"})

    project = Project.load(name)
    assert project.resource("image") is None
    assert project.resource("segmentation").node == "hms-o2"
    assert project.segmentation.derived.startswith("node://hms-o2/")


# --------------------------------------------------------------------------
# Refusing before anything is written
# --------------------------------------------------------------------------

def test_an_unknown_node_is_refused_before_anything_is_written(tmp_path, o2):
    """A typo in the last entry must not cost the first entry's conversion.

    That promise is the reason validation asks the node rather than telling it
    anything: `detect_on_node` is a read, so a batch can be checked end to end
    and still have written nothing.
    """
    first = _image(tmp_path, "LSP11641.ome.tif")
    second = _image(tmp_path, "LSP20209.ome.tif")

    with pytest.raises(ValueError) as caught:
        plexora.create_dataset("PCA", projects=[
            {"image": str(first), "node": "hms-o2"},
            {"image": str(second), "node": "ghost"},
        ])

    assert "ghost" in str(caught.value)
    assert Project.load_all() == {}
    assert _served(o2) == {}


def test_a_path_missing_on_the_node_is_refused_before_anything_is_written(
        tmp_path, o2):
    """Named both ways round -- which role, and which machine -- because with
    files on two machines "no such file" alone does not say where to look."""
    slide = _image(tmp_path)

    with pytest.raises(ValueError) as caught:
        plexora.create_project(str(slide), node="hms-o2",
                               segmentation="/n/scratch/nope/cell.ome.tif")

    assert "segmentation" in str(caught.value)
    assert "hms-o2" in str(caught.value)
    assert Project.load_all() == {}


def test_a_table_named_as_an_image_is_refused(tmp_path, o2):
    """The node read the file and it is a table. A picture and a table are not
    the same kind of thing, and a project claiming otherwise opens onto an
    error later instead of a sentence now.

    Image versus segmentation is deliberately NOT refused this way: whether a
    label field is a mask is a reading, and the role is the user's statement.
    """
    with pytest.raises(ValueError) as caught:
        plexora.create_project(str(_csv(tmp_path)), node="hms-o2")

    assert "not a image" in str(caught.value) or "not an image" in str(caught.value)
    assert Project.load_all() == {}


def test_copy_has_nothing_to_copy_for_an_image_on_a_node(tmp_path, o2):
    """`--copy` means "put the files in the project directory", and a node
    serves the file where it lies. Said rather than silently ignored."""
    with pytest.raises(ValueError) as caught:
        plexora.create_project(_image(tmp_path), node="hms-o2", copy=True)

    assert "copy" in str(caught.value)


# --------------------------------------------------------------------------
# Re-running, and failing
# --------------------------------------------------------------------------

def test_exist_ok_adopts_the_project_already_reading_that_resource(tmp_path, o2):
    """Re-running a fifty-image command after fixing one bad path must not make
    forty-nine duplicates.

    A node-backed project records no path here to compare, so the comparison is
    the binding -- the same one the import screen makes.
    """
    slide = _image(tmp_path)
    first = plexora.create_project(str(slide), node="hms-o2")

    second = plexora.create_project(str(slide), node="hms-o2", exist_ok=True)

    assert second == first
    assert list(Project.load_all()) == [first]
    assert len(_served(o2)) == 1


def test_a_failed_image_attach_leaves_no_half_project(tmp_path, o2, monkeypatch):
    """A half-registered project is worse than none: it appears in the picker,
    opens onto an error, and its name is taken so the call cannot be re-run."""
    import plexora.nodes

    def _fail(*args, **kwargs):
        raise RuntimeError("the node went away")

    monkeypatch.setattr(plexora.nodes, "attach_image", _fail)

    with pytest.raises(RuntimeError):
        plexora.create_project(_image(tmp_path), node="hms-o2")

    assert Project.load_all() == {}


def test_a_node_started_without_dynamic_says_so_rather_than_blaming_the_token(
        tmp_path, node_process):
    """Both refusals are 403s and only one is about the token.

    Telling somebody to re-register a node that was answering them perfectly
    well sends them to fix the one thing that was not wrong.

    The node serves one resource, because a node with nothing to serve and no
    `--dynamic` refuses to start at all -- what is being tested is a node that
    is up and healthy and will not take anything new.
    """
    slide = _image(tmp_path)
    node = node_process(f"image:slide={slide}")
    register("hms-o2", node)

    with pytest.raises(ValueError) as caught:
        plexora.create_project(_image(tmp_path, "other.ome.tif"), node="hms-o2")

    message = str(caught.value)
    assert "--dynamic" in message
    assert "token" not in message.lower()
