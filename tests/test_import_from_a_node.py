"""Importing a sample whose files are on another machine.

The screen this covers is **Import sample** with the Local/Remote switch set
to a data node: the user browses the node's filesystem, picks a slide and a
mask, and expects a project. What that browse hands back is a PATH on the
node's disk, and nothing has told the node to serve it -- so every test here
starts from a node serving nothing at all, which is exactly what a `plexora
connect` node is at the moment somebody opens the dialog.

Two things are being pinned. A picked path becomes a served resource, rather
than being looked up as an id it was never going to match. And what the file
IS comes from the node: `cell.ome.tif` out of an mcmicro run is a segmentation
mask and its name says so nowhere, so the only process that can read the
pixels is the one that decides.
"""

from __future__ import annotations

import numpy as np
import pytest
import tifffile

from tests.node_harness import node_process, register  # noqa: F401 - fixture


SIZE = 512


def _image_file(directory, name="LSP11641.ome.tif"):
    """A three-channel fluorescence slide, the way mcmicro names one."""
    rng = np.random.default_rng(7)
    data = np.zeros((3, SIZE, SIZE), dtype=np.uint16)
    for index in range(3):
        data[index] = rng.poisson(40 * (index + 1), (SIZE, SIZE)).astype(np.uint16)
        data[index, 100:160, 100:160] += 4000 * (index + 1)
    path = directory / name
    tifffile.imwrite(path, data, photometric="minisblack")
    return path


def _mask_file(directory, name="cell.ome.tif"):
    """What a cellpose run writes, under the name mcmicro gives it.

    Nothing in `cell.ome.tif` says mask -- no `_mask`, no `_seg`, no `labels`
    -- which is the point: one plane of sparse integer ids over a background
    of zero is what says it, and that is only readable where the file is.
    """
    labels = np.zeros((SIZE, SIZE), dtype=np.uint32)
    for index in range(1, 6):
        top = index * 60
        labels[top:top + 30, top:top + 30] = index
    path = directory / name
    tifffile.imwrite(path, labels)
    return path


def _ambiguous_file(directory, name="LSP11641_extra.tif"):
    """One 8-bit plane: a small mask and a grey photograph read the same."""
    rng = np.random.default_rng(3)
    plane = rng.integers(0, 6, (SIZE, SIZE), dtype=np.uint8)
    path = directory / name
    tifffile.imwrite(path, plane)
    return path


@pytest.fixture
def o2(node_process):
    """A dynamic node serving nothing, registered as `hms-o2`."""
    node = node_process(dynamic=True)
    register("hms-o2", node)
    return node


def _inspect(*paths, answers=None):
    from plexora.server.models.import_proposal import inspect_paths

    return inspect_paths([f"node://hms-o2/{path}" for path in paths],
                         answers=answers)


def _by_role(sample):
    return {layer.role: layer for layer in sample.layers}


# -- a path becomes a resource ---------------------------------------------


def test_a_browsed_path_is_served_rather_than_looked_up(tmp_path, o2):
    """The bug this file exists for.

    A browse on the node hands back `/n/scratch/.../LSP11641.ome.tif`, which
    is a path and not a resource id. Reading it as an id produced "hms-o2 is
    not serving '/n/scratch/…'" -- a true sentence about a question nobody
    asked, on a screen whose only other option was to give up.
    """
    image = _image_file(tmp_path)

    proposal = _inspect(image)

    assert not proposal.unrecognised, proposal.unrecognised
    sample = proposal.samples[0]
    layer = _by_role(sample)["image"]
    assert layer.src.startswith("node://hms-o2/")
    # The address carries the id the node derived, never the path: another
    # machine's mount points are not this one's business.
    assert str(image) not in layer.src
    assert o2.get("/node/v1/hello")["resources"][0]["kind"] == "image"


def test_the_sample_is_named_after_the_file_and_not_the_id(tmp_path, o2):
    """A resource id is a slug and a hash. Nobody recognises their slide in it."""
    image = _image_file(tmp_path)

    sample = _inspect(image).samples[0]

    # Lowercased by the grouping stem, exactly as a local import of the
    # same filename is -- what matters is that it is the filename at all.
    assert sample.name == "lsp11641"
    assert "LSP11641.ome.tif" in _by_role(sample)["image"].label


def test_the_node_reads_the_pixels_that_say_mask(tmp_path, o2):
    """`cell.ome.tif` is a mask, and only the machine holding it knows that."""
    image = _image_file(tmp_path)
    mask = _mask_file(tmp_path)

    proposal = _inspect(image, mask)

    assert not proposal.unrecognised, proposal.unrecognised
    assert len(proposal.samples) == 1, "a slide and its mask are one sample"
    roles = _by_role(proposal.samples[0])
    assert roles["image"].label.endswith("LSP11641.ome.tif")
    assert roles["mask"].label.endswith("cell.ome.tif")
    kinds = {entry["kind"] for entry in o2.get("/node/v1/hello")["resources"]}
    assert kinds == {"image", "segmentation"}


def test_a_file_that_reads_both_ways_is_asked_about(tmp_path, o2):
    """The same question a file on this server's own disk gets, same id.

    One 8-bit plane is consistent with both readings, and guessing wrong is
    invisible: a mask drawn as a channel is a grey square, an image read as a
    mask is a cell-id lookup over a photograph.
    """
    image = _image_file(tmp_path)
    ambiguous = _ambiguous_file(tmp_path)

    sample = _inspect(image, ambiguous).samples[0]
    asked = [q for q in sample.questions
             if q.id == "mask-or-image:LSP11641_extra.tif"]
    assert asked, [q.id for q in sample.questions]
    assert asked[0].default == "image"

    answered = _inspect(image, ambiguous,
                        answers={"mask-or-image:LSP11641_extra.tif": "mask"})
    roles = _by_role(answered.samples[0])
    assert roles["mask"].label.endswith("LSP11641_extra.tif")
    # And the node was told to re-serve it, or the mask would be bound to a
    # resource it still serves as an image.
    served = {entry["id"]: entry["kind"]
              for entry in o2.get("/node/v1/hello")["resources"]}
    assert "segmentation" in served.values()
    assert list(served.values()).count("image") == 1


# -- what cannot be imported, said as itself -------------------------------


def test_a_folder_on_a_node_says_it_is_a_folder(tmp_path, o2):
    """A run directory is a bundle, and bundling needs a walk this side has not.

    Worth its own test because the wrong answer here is the one that reads as
    a broken connection rather than as a limit.
    """
    folder = tmp_path / "mcmicro_output"
    folder.mkdir()
    _image_file(folder)

    proposal = _inspect(folder)

    assert not proposal.samples
    reason = proposal.unrecognised[0]["reason"]
    assert "folder" in reason
    assert "not serving" not in reason


def test_a_file_nothing_reads_names_itself(tmp_path, o2):
    notes = tmp_path / "params.yml"
    notes.write_text("cycle: 1\n", encoding="utf-8")

    proposal = _inspect(notes)

    assert "params.yml" in proposal.unrecognised[0]["reason"]


def test_an_id_a_node_was_started_with_still_works(tmp_path, node_process):
    """The other half of the address: `--serve image:slide=…` names its own id.

    A resource id has no separator in it and a path always does, which is what
    tells the two apart -- so this is the regression test for the sniff.
    """
    image = _image_file(tmp_path)
    node = node_process(f"image:slide={image}")
    register("hms-o2", node)

    sample = _inspect("slide").samples[0]

    assert _by_role(sample)["image"].src == "node://hms-o2/slide"


# -- and the project it makes ----------------------------------------------


def test_the_registered_project_reads_from_the_node(tmp_path, o2):
    """End to end: pick two files on a cluster, get a project that draws.

    The assertion that matters is the tile: it proves the id written into the
    project's binding is the id the node ended up serving, which is the pair
    that had no way of meeting before.
    """
    from plexora.server.models import data_model
    from plexora.server.models.import_sample import import_sample
    from plexora.server.models.project import Project

    image = _image_file(tmp_path)
    mask = _mask_file(tmp_path)

    result = import_sample([f"node://hms-o2/{image}",
                            f"node://hms-o2/{mask}"])

    project = Project.load(result["name"])
    assert project.resource("image").node == "hms-o2"
    assert project.resource("segmentation").node == "hms-o2"
    data_model.load_datasource(result["name"], reload=True)
    # The tile key is the tail of the channel's own src, which is how every
    # other node-backed image test addresses one.
    key = project.image.channels[0]["src"].rstrip("/").rsplit("/", 1)[-1]
    tile, _ = data_model.encode_tile(result["name"], key, 0, "0_0", "webp")
    assert len(tile) > 0
