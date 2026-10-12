"""The viewer starts in HD unless the image comes over a network.

main.js presets HD at boot for a channel image whose tiles do not cross a
network: `image_streamed` (page_routes) is the server's half of that -- an
image read from a web address or held by a data node -- and the viewer page
hands it over in `flaskVariables` (pinned by the boundary goldens).
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


def test_an_image_on_a_data_node_is_streamed():
    assert image_streamed(_project(channelFile="node://hpc/r1")) is True
    bound = _project(channelFile="/n/scratch/image.ome.tiff",
                     resources={"image": {"provider": "node", "node": "hpc",
                                          "resource_id": "r1"}})
    assert image_streamed(bound) is True


def test_only_the_image_binding_counts():
    # A mask on a node does not move the image's tiles anywhere.
    mask_remote = _project(channelFile="/data/image.ome.tiff",
                           resources={"segmentation": {"provider": "node", "node": "hpc",
                                                       "resource_id": "m1"}})
    assert image_streamed(mask_remote) is False
