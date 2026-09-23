"""What kind of image a node is serving, and who decides.

An image's mode -- one colour picture, or a stack of independent marker
channels -- is read off the file at registration. For an image on a data node
the primary has no file to read: it has a `node://<node>/<resource>` address,
which is not something it can open. So the node runs the same detector when the
resource is added and reports the answer, and the primary records it.

Before that, every image reached through a node was registered as a channel
stack whatever it was. An H&E slide came out as a three-channel fluorescence
project -- R, G and B offered as markers, composited additively on black -- and
nothing anywhere reported an error, because nothing was in a position to
notice. That is what these tests are about: the same slide has to come out the
same way whichever machine is holding it.

A real node in a real second process, as in `test_node_image.py`, and every
answer compared against a local registration of the same file rather than
against a hand-written expectation.
"""

from __future__ import annotations

import pytest

from tests.brightfield_fixtures import (
    write_ambiguous_planar,
    write_planar_fluorescence,
    write_rgb_ome_tiff,
)
from tests.helpers import ALL_CONFIRMED, project
from tests.node_harness import node_process, register  # noqa: F401 - fixture

@pytest.fixture
def client():
    import plexora

    return plexora.app.test_client()


def _local(name, path):
    """The same file, registered the ordinary way, as the comparison."""
    from plexora.datasource import register_image_datasource
    from plexora.server.models.project import Project

    register_image_datasource(name, path)
    return Project.load(name)


def _attached(node_process, path, name="remote", image_type=None):
    """`name`, with its image served by a node that was pointed at `path`.

    Built the way `plexora.datasets.create_project` builds it: an empty
    `ImageSpec`, so the geometry is whatever the node reports rather than
    something this test asserted in advance. A project with dimensions already
    on it is a DIFFERENT case -- `_same_image` guards it -- and not the one
    being tested here.
    """
    from plexora.nodes import attach_image
    from plexora.server.models.project import ImageSpec, Project

    node = node_process(f"image:slide={path}")
    register("imgnode", node)
    Project(name=name, image=ImageSpec(), confirmed=ALL_CONFIRMED).save()
    return attach_image(name, node="imgnode", resource_id="slide",
                        image_type=image_type)


# -- what the node concludes ------------------------------------------------


def test_a_node_says_what_kind_of_image_it_is_serving(tmp_path, node_process):
    """The handshake carries it, so the upload form can say so before anything
    has been imported -- see `/detect_image_type`'s node branch."""
    from plexora.nodes import image_type_on_node

    node = node_process(f"image:slide={write_rgb_ome_tiff(tmp_path / 'he.ome.tif')}")
    register("imgnode", node)

    verdict, reason = image_type_on_node("imgnode", "slide")

    assert verdict == "brightfield"
    # The reason travels too: the edit page shows it, and "it is brightfield"
    # with no account of why is not something a user can judge.
    assert reason


def test_a_node_says_fluorescence_for_a_panel_with_three_markers(
        tmp_path, node_process):
    """The mistake worth guarding: three planes is not three samples, and a
    3-plex panel called H&E would be the worst answer available here."""
    from plexora.nodes import image_type_on_node

    path = write_planar_fluorescence(tmp_path / "panel.ome.tif")
    node = node_process(f"image:slide={path}")
    register("imgnode", node)

    verdict, _reason = image_type_on_node("imgnode", "slide")

    assert verdict == "fluorescence"


# -- what the primary records -----------------------------------------------


def test_an_he_slide_on_a_node_is_registered_as_brightfield(
        tmp_path, node_process):
    path = write_rgb_ome_tiff(tmp_path / "he.ome.tif")
    attached = _attached(node_process, path)

    assert attached.image.kind == "brightfield"
    assert attached.image_type == "brightfield"
    # One servable layer, not three. `num_channels` still counts the planes the
    # pyramid has -- that is what `_same_image` compares against -- and the two
    # deliberately differ for brightfield.
    assert attached.image.channel_names == ["Image"]
    assert attached.image.num_channels == 3
    # And why, so the edit page can show it next to what was asked for.
    assert attached.image.image_type_detected == "brightfield"
    assert attached.image.image_type_reason


def test_the_node_and_the_local_path_agree_about_the_same_slide(
        tmp_path, node_process):
    """The claim the whole feature makes: where a slide lives cannot change
    what it is."""
    path = write_rgb_ome_tiff(tmp_path / "he.ome.tif")
    here = _local("here", path)
    there = _attached(node_process, path, name="there")

    assert there.image.kind == here.image.kind
    assert there.image.channel_names == here.image.channel_names
    assert there.image_type == here.image_type


def test_a_panel_on_a_node_is_still_a_channel_stack(tmp_path, node_process):
    path = write_planar_fluorescence(tmp_path / "panel.ome.tif")
    attached = _attached(node_process, path)

    assert attached.image.kind == "ome_tiff"
    assert attached.image_type == "fluorescence"
    # One tile key per plane, each carrying its own index -- the arrangement
    # the node parses the identical string for.
    assert attached.image.channel_names == ["slide_0", "slide_1", "slide_2"]


def test_a_brightfield_tile_from_a_node_is_byte_identical_to_a_local_read(
        tmp_path, node_process):
    """Not merely "it works": the primary forwards a node's tile bytes to the
    browser verbatim, which is only correct if the two ends produce the same
    bytes for the same input."""
    from plexora.server.models import data_model

    path = write_rgb_ome_tiff(tmp_path / "he.ome.tif")
    _attached(node_process, path)
    _local("here", path)

    data_model.load_datasource("here", reload=True)
    local_bytes, local_type = data_model.encode_tile("here", "rgb", 0, "0_0", "webp")
    data_model.load_datasource("remote", reload=True)
    remote_bytes, remote_type = data_model.encode_tile("remote", "rgb", 0, "0_0", "webp")

    assert remote_type == local_type
    assert remote_bytes == local_bytes


# -- what the user can say about it -----------------------------------------


def test_the_import_forms_override_reaches_a_node_image(tmp_path, node_process):
    """It used to be dropped, which made the Image type control on the form a
    control that did nothing for exactly the images the form could say least
    about."""
    path = write_rgb_ome_tiff(tmp_path / "he.ome.tif")
    attached = _attached(node_process, path, image_type="fluorescence")

    # The same three planes, read through the same CYX views by code that never
    # learns the samples were interleaved -- the honest reading of "this file is
    # three markers that happen to be stored as RGB".
    assert attached.image.kind == "ome_tiff"
    assert attached.image.channel_names == ["slide_0", "slide_1", "slide_2"]
    assert attached.image.image_type_choice == "fluorescence"
    # What the file says is still recorded next to what was asked for.
    assert attached.image.image_type_detected == "brightfield"


def test_an_override_outlives_the_attachment_that_made_it(tmp_path, node_process):
    """A laptop that came back, or a project repointed at another node, reads
    the image the way it was told to without being told again."""
    from plexora.nodes import attach_image

    path = write_rgb_ome_tiff(tmp_path / "he.ome.tif")
    _attached(node_process, path, image_type="fluorescence")

    # No `image_type` this time: the stored choice is the one that has to win,
    # not the detector's verdict.
    again = attach_image("remote", node="imgnode", resource_id="slide")

    assert again.image.kind == "ome_tiff"
    assert again.image.image_type_choice == "fluorescence"


def test_calling_an_ambiguous_planar_file_brightfield_reaches_the_node(
        tmp_path, node_process):
    """The case the override exists for: three `minisblack` planes say only
    "three planes", so nothing in the file can settle it and the person who ran
    the scan is the only one who knows.

    A node needs no telling for this to work -- its pyramid presents (channel,
    y, x) whichever way it opened the file, and `brightfield.rgb_region` stacks
    three of those planes when the level has no `.rgb` of its own.
    """
    path = write_ambiguous_planar(tmp_path / "flat.ome.tif", light=False)
    attached = _attached(node_process, path, image_type="brightfield")

    assert attached.image.kind == "brightfield"
    assert attached.image.channel_names == ["Image"]
    # The detector's own answer is kept alongside, precisely because it
    # disagrees: that is the pair the edit page shows.
    assert attached.image.image_type_detected == "fluorescence"


def test_brightfield_is_refused_for_an_image_with_nothing_to_put_in_the_third_sample(
        tmp_path, node_process):
    """A one- or two-plane image can be bright -- a scanned grayscale IHC
    section is -- and reading it as colour would fail on the missing planes."""
    path = write_planar_fluorescence(tmp_path / "two.ome.tif", channels=2,
                                     names=("DAPI", "CD3"))
    attached = _attached(node_process, path, image_type="brightfield")

    assert attached.image.kind != "brightfield"
    assert attached.image.channel_names == ["slide_0", "slide_1"]


def test_changing_the_type_on_the_edit_page_rebuilds_a_node_image(
        tmp_path, node_process, client):
    """The control used to record the choice and stop there, because there is
    no local file to re-read -- so it looked like it had done nothing. The
    node-backed rebuild is `attach_image` again, which reads the stored choice
    itself."""
    from plexora.server.models.project import Project

    path = write_rgb_ome_tiff(tmp_path / "he.ome.tif")
    attached = _attached(node_process, path)
    assert attached.image.channel_names == ["Image"]

    answer = client.post("/project/remote",
                         json={"imageType": "fluorescence"})

    assert answer.status_code == 200
    after = Project.load("remote")
    assert after.image.image_type_choice == "fluorescence"
    # The half that was missing: the layer list is rebuilt, not just recorded.
    assert after.image.kind == "ome_tiff"
    assert after.image.channel_names == ["slide_0", "slide_1", "slide_2"]


# -- the form's question, before anything has been imported -----------------


def test_the_upload_form_can_ask_about_an_image_on_a_node(
        tmp_path, node_process, client):
    """`Path("node://imgnode/slide")` exists nowhere, so the route used to
    report a null verdict for every image not on this server's own disk -- and
    the line beside the project name simply never appeared in remote mode."""
    node = node_process(f"image:slide={write_rgb_ome_tiff(tmp_path / 'he.ome.tif')}")
    register("imgnode", node)

    answer = client.post("/detect_image_type",
                         json={"path": "node://imgnode/slide"}).get_json()

    assert answer["verdict"] == "brightfield"
    assert answer["reason"]


def test_an_unknown_node_is_a_null_verdict_rather_than_an_error(client):
    """Never an error the form has to handle: the select stays on Automatic and
    the import detects again from scratch."""
    answer = client.post("/detect_image_type",
                         json={"path": "node://nowhere/slide"}).get_json()

    assert answer["verdict"] is None
