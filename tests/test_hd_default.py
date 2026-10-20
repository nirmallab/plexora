"""The viewer starts in HD unless the image is read from a web address.

main.js presets HD at boot for a channel image whose tiles are built where
the image is: `image_streamed` (page_routes) is the server's half of that --
an image read from a web address, with no compute beside it; a data node
builds its own tiles and does not count -- and the viewer page hands it over
in `flaskVariables` (pinned by the boundary goldens).
"""

from plexora.server.models.project import Project
from plexora.server.routes.page_routes import image_streamed


def _project(**entry):
    return Project.from_entry("p", {"image_kind": "ome_tiff", "imageData": [], **entry})


def test_a_file_on_this_machine_is_not_streamed():
    assert image_streamed(_project(channelFile="/data/run1/image.ome.tiff")) is False


def test_an_image_at_a_web_address_is_streamed():
    for src in ("https://example.org/a.ome.zarr", "s3://bucket/a.ome.zarr",
                "gs://bucket/a.zarr", "az://container/a.zarr"):
        assert image_streamed(_project(channelFile=src)) is True, src


def test_an_image_on_a_data_node_starts_in_hd():
    """The user's rule: a node has compute where the image is."""
    assert image_streamed(_project(channelFile="node://hpc/r1")) is False
    bound = _project(channelFile="/n/scratch/image.ome.tiff",
                     resources={"image": {"provider": "node", "node": "hpc",
                                          "resource_id": "r1"}})
    assert image_streamed(bound) is False
    # A node reading a web address builds the tiles there too.
    bound_remote = _project(channelFile="https://example.org/a.ome.zarr",
                            resources={"image": {"provider": "node", "node": "hpc",
                                                 "resource_id": "r2"}})
    assert image_streamed(bound_remote) is False


def test_only_the_image_binding_counts():
    # A mask on a node does not move the image's tiles anywhere.
    mask_remote = _project(channelFile="/data/image.ome.tiff",
                           resources={"segmentation": {"provider": "node", "node": "hpc",
                                                       "resource_id": "m1"}})
    assert image_streamed(mask_remote) is False
