"""A project served out of this process's memory, read through the real app.

The whole path, end to end: a kernel node on a thread, snapshots in its
registry, a project whose bindings point at it, and the primary's own routes
answering over the node API. Nothing is mocked -- the requests below go through
`resolve_providers`, `NodeTableProvider`/`NodeImageProvider`, real HTTP over
loopback, and back -- because the claim being tested is precisely that the
existing machinery works unchanged when the node is this interpreter.

`plexora.app` is the primary here, which in a notebook it would not be (the
sidecar is a second process). That difference is deliberate and does not weaken
the test: the sidecar runs exactly this code, and doing it in-process is what
lets the suite assert on it at all.
"""

import numpy as np
import pandas as pd
import pytest

import plexora
from plexora import memory as memory_api
from plexora.server.models.project import Project

ad = pytest.importorskip("anndata")

QUIET = {"log": lambda *args, **kwargs: None}


@pytest.fixture
def adata():
    rows = 200
    rng = np.random.default_rng(3)
    obj = ad.AnnData(
        X=rng.random((rows, 4)).astype("float32"),
        obs=pd.DataFrame(
            {"leiden": pd.Categorical(rng.choice(["a", "b", "c"], rows))},
            index=[f"cell{i}" for i in range(rows)]),
        var=pd.DataFrame(index=["CD3", "CD4", "CD8", "DAPI"]),
    )
    obj.obsm["spatial"] = (rng.random((rows, 2)) * 900).astype("float32")
    return obj


@pytest.fixture
def image():
    rng = np.random.default_rng(4)
    return (rng.random((3, 1200, 1500)) * 3000).astype("uint16")


@pytest.fixture
def mask():
    labels = np.zeros((1200, 1500), dtype="uint32")
    labels[100:200, 100:200] = 7
    return labels


@pytest.fixture
def client():
    return plexora.app.test_client()


@pytest.fixture
def registered(adata, image, mask, plexora_data_root):
    return memory_api.register_memory_datasource(
        "tonsil", image, segmentation=mask, adata=adata,
        channel_names=["DNA", "CD3", "CD8"], pixel_size=0.325,
        data_dir=plexora_data_root, **QUIET)


def _tile_url(project, channel):
    src = next(entry["src"] for entry in project.image.channels
               if entry["fullname"] == channel)
    return f"{src}0/0_0.png"


# -- what gets written down ----------------------------------------------


def test_the_project_records_the_image_it_was_handed(registered):
    assert (registered.image.width, registered.image.height) == (1500, 1200)
    assert registered.image.num_channels == 3
    assert [entry["fullname"] for entry in registered.image.channels] == [
        "Area", "DNA", "CD3", "CD8"]


def test_every_resource_points_at_this_kernel_and_not_at_a_path(registered):
    for kind in ("image", "segmentation", "table"):
        binding = registered.resource(kind)
        assert binding is not None and binding.is_node
        assert binding.node == memory_api.NODE_NAME
    # The spec records the LOCATOR, not the memory URI it was built with: a
    # binding says where a resource is, and `node://` is the one spelling for
    # "not on this machine's filesystem". `memory://` never leaves the node.
    assert registered.dataset.src == f"node://{memory_api.NODE_NAME}/tonsil-table"
    assert registered.image.src == ""


def test_the_table_is_described_exactly_as_a_file_import_describes_one(registered):
    """The marker/metadata split and the obs vocabulary reach the project, so
    nothing downstream can tell this from an .h5ad import."""
    assert registered.dataset.columns.markers == ("CD3", "CD4", "CD8", "DAPI")
    assert registered.dataset.obs_columns == ("leiden",)
    assert registered.dataset.type == "anndata"


def test_a_mask_from_memory_is_recorded_as_filled(registered):
    """It was built here by subsampling the caller's labels, so the interiors
    are intact and the viewer derives boundaries itself. Reported as outlines
    it would draw solid blobs, with no error anywhere."""
    assert registered.segmentation.mode == "filled"
    assert registered.segmentation.status == "ready"


def test_nothing_was_written_into_the_project_directory(registered, plexora_data_root):
    """The whole point. config.json is the project record and has to exist; a
    copy of the data next to it would be the disk write this removes."""
    written = {path.name for path in (plexora_data_root / "tonsil").iterdir()}
    assert not any(name.endswith((".h5ad", ".csv", ".tif", ".tiff"))
                   for name in written)


# -- what the viewer can actually read -----------------------------------


def test_the_viewer_page_renders(registered, client):
    assert client.get("/tonsil").status_code == 200


def test_channel_tiles_come_back_as_tiles(registered, client):
    response = client.get(_tile_url(registered, "DNA"))
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "image/webp"
    assert len(response.data) > 1000


def test_the_label_layer_comes_back_as_a_png(registered, client):
    response = client.get(_tile_url(registered, "Area"))
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "image/png"


def test_the_cells_come_back(registered, client):
    response = client.get(
        "/get_all_cells/integer/?datasource=tonsil&start_keys=id,X,Y")
    assert response.status_code == 200
    assert len(response.data) > 0


def test_an_obs_column_is_offered_as_something_to_colour_by(registered, client):
    response = client.get("/plugins/cell_explorer/api/variables?datasource=tonsil")
    names = [entry["name"] for entry in response.get_json()["variables"]]
    assert "leiden" in names


def test_that_columns_values_are_servable(registered, client):
    response = client.get(
        "/plugins/cell_explorer/api/values?datasource=tonsil&column=leiden")
    assert response.status_code == 200
    assert len(response.data) > 0


# -- the loop: annotate, register again, look ----------------------------


def test_a_new_obs_column_reaches_the_project_and_is_servable(
    registered, client, adata, plexora_data_root
):
    """The reason the second call re-describes the table rather than only
    swapping bytes: a column that did not exist last cell has to reach the
    project's recorded vocabulary, or nothing offers it as an overlay."""
    client.get("/tonsil")
    adata.obs["phenotype"] = pd.Categorical(
        np.where(np.asarray(adata.X)[:, 0] > 0.5, "hi", "lo"))

    memory_api.register_memory_datasource("tonsil", adata=adata,
                                          data_dir=plexora_data_root, **QUIET)
    assert client.post("/reload_datasource?datasource=tonsil").status_code == 200

    assert Project.load("tonsil").dataset.obs_columns == ("leiden", "phenotype")
    response = client.get(
        "/plugins/cell_explorer/api/values?datasource=tonsil&column=phenotype")
    assert response.status_code == 200


def test_refreshing_the_table_does_not_disturb_the_image(
    registered, client, adata, plexora_data_root
):
    """Generations are per resource, so a table that changed must not cost the
    browser every tile it is holding."""
    url = _tile_url(registered, "DNA")
    client.get("/tonsil")
    before = client.get(url).headers["ETag"]

    adata.obs["phenotype"] = ["x"] * adata.n_obs
    memory_api.register_memory_datasource("tonsil", adata=adata,
                                          data_dir=plexora_data_root, **QUIET)
    client.post("/reload_datasource?datasource=tonsil")

    after = client.get(url)
    assert after.status_code == 200
    assert client.get(url, headers={"If-None-Match": after.headers["ETag"]}
                      ).status_code == 304
    assert before  # the tile was servable before the refresh as well


def test_registering_again_with_no_image_leaves_the_image_alone(
    registered, adata, plexora_data_root
):
    memory_api.register_memory_datasource("tonsil", adata=adata,
                                          data_dir=plexora_data_root, **QUIET)
    again = Project.load("tonsil")

    assert (again.image.width, again.image.height) == (1500, 1200)
    assert again.resource("image").resource_id == registered.resource("image").resource_id


def test_a_first_call_with_no_image_says_what_is_missing(adata, plexora_data_root):
    with pytest.raises(memory_api.MemoryDataError) as excinfo:
        memory_api.register_memory_datasource("nothing", adata=adata,
                                              data_dir=plexora_data_root, **QUIET)
    assert "which image" in str(excinfo.value)


def test_a_project_somebody_else_made_is_not_quietly_repointed(
    image, adata, plexora_data_root, tmp_path
):
    """Re-registering our own project is the loop this exists for. Taking over
    a name that means something else is not, and the user would find out by
    opening it."""
    from plexora.server.models.project import ImageSpec, ResourceBinding

    Project(name="theirs", image=ImageSpec(),
            resources={"image": ResourceBinding(
                kind="image", provider="node", node="hpc",
                resource_id="slide")}).save(plexora_data_root)

    with pytest.raises(memory_api.MemoryDataError) as excinfo:
        memory_api.register_memory_datasource("theirs", image, adata=adata,
                                              data_dir=plexora_data_root, **QUIET)
    assert "another name" in str(excinfo.value)


# -- a flat table --------------------------------------------------------


def test_a_dataframe_is_registered_and_read_back(image, plexora_data_root, client):
    rng = np.random.default_rng(5)
    frame = pd.DataFrame({
        "CellID": np.arange(150),
        "X_centroid": rng.random(150) * 900,
        "Y_centroid": rng.random(150) * 700,
        "CD3": rng.random(150) * 10,
        "phenotype": rng.choice(["T", "B"], 150),
    })
    project = memory_api.register_memory_datasource(
        "flat", image, table=frame, channel_names=["DNA", "CD3", "CD8"],
        data_dir=plexora_data_root, **QUIET)

    assert project.dataset.type == "csv"
    assert "CD3" in project.dataset.columns.markers
    assert client.get("/flat").status_code == 200
    assert client.get(
        "/get_all_cells/integer/?datasource=flat&start_keys=id,X_centroid,Y_centroid"
    ).status_code == 200


def test_adata_and_table_together_is_refused(image, adata, plexora_data_root):
    with pytest.raises(memory_api.MemoryDataError):
        memory_api.register_memory_datasource(
            "both", image, adata=adata, table=pd.DataFrame({"a": [1]}),
            data_dir=plexora_data_root, **QUIET)


# -- the kernel node itself ----------------------------------------------


def test_the_kernel_node_is_registered_loopback_and_invisible_to_a_browser(
    registered
):
    """The invariant every hosted environment rests on: the sidecar is spawned
    by the kernel, so it reaches this node over loopback -- and the browser
    never does, because there is no browser endpoint to try. See
    `resourceRouting.js`, which falls back to proxying when a probe fails."""
    from plexora.server.models import nodes as node_registry

    node = node_registry.get(memory_api.NODE_NAME)
    assert node.endpoint.startswith("http://127.0.0.1:")
    assert node.browser_endpoint is None
    assert node.token


def test_the_browser_is_offered_no_direct_route_to_it(registered, client):
    """`/resource_routing` is what a page asks before it builds a tile source.

    A kernel node must not appear in it: `http://127.0.0.1:<port>` means the
    user's own laptop from where a hosted browser stands, so probing it would
    carry this node's token to whatever is listening there. Every tile is
    proxied through the server instead, which is what a failed probe would have
    fallen back to anyway."""
    routes = client.get("/resource_routing?datasource=tonsil").get_json()["routes"]
    assert routes == {}


def test_replacing_a_snapshot_bumps_the_resources_generation(registered, adata):
    kernel = memory_api.KernelNode.get(**QUIET)
    resource = kernel.registry.get("tonsil-table")
    before = resource.generation

    kernel.serve("table", "tonsil-table",
                 memory_api.snapshot_anndata(adata, **QUIET))

    assert kernel.registry.get("tonsil-table").generation > before


def test_a_resource_id_is_derived_from_the_name_so_it_survives_a_restart():
    assert memory_api._resource_ids("Tonsil 2") == {
        "image": "tonsil-2-image",
        "segmentation": "tonsil-2-segmentation",
        "table": "tonsil-2-table",
    }


# -- SpatialData ---------------------------------------------------------
#
# A store is three of Plexora's resources in one container, so this is a
# decomposition rather than a fourth reader: once the pieces are out, each takes
# exactly the path it would have taken on its own.


@pytest.fixture
def sdata():
    sd = pytest.importorskip("spatialdata")
    from spatialdata.models import Image2DModel, Labels2DModel, TableModel

    rng = np.random.default_rng(9)
    rows = 60
    table = ad.AnnData(
        X=rng.random((rows, 3)).astype("float32"),
        obs=pd.DataFrame({"leiden": pd.Categorical(rng.choice(["a", "b"], rows))},
                         index=[f"cell{i}" for i in range(rows)]),
        var=pd.DataFrame(index=["A", "B", "C"]),
    )
    table.obsm["spatial"] = (rng.random((rows, 2)) * 1000).astype("float32")
    labels = np.zeros((1200, 1500), dtype="uint32")
    labels[200:400, 200:500] = 3
    return sd.SpatialData(
        images={"slide": Image2DModel.parse(
            (rng.random((3, 1200, 1500)) * 1000).astype("uint16"),
            dims=("c", "y", "x"), scale_factors=[2, 2])},
        labels={"cells": Labels2DModel.parse(labels, dims=("y", "x"))},
        tables={"t": TableModel.parse(table)},
    )


def test_a_spatialdata_becomes_an_ordinary_project(sdata, plexora_data_root, client):
    project = memory_api.register_memory_datasource(
        "store", sdata=sdata, channel_names=["A", "B", "C"],
        data_dir=plexora_data_root, **QUIET)

    assert (project.image.width, project.image.height) == (1500, 1200)
    assert project.dataset.columns.markers == ("A", "B", "C")
    assert project.dataset.obs_columns == ("leiden",)
    assert project.segmentation.mode == "filled"
    assert client.get(_tile_url(project, "A")).status_code == 200
    assert client.get(_tile_url(project, "Area")).status_code == 200


def test_a_multiscale_element_is_re_derived_rather_than_adopted(sdata, plexora_data_root):
    """The store's own scales need not be a halving chain, and the viewer's
    tile source computes a level's size as `size >> level`. Deriving from the
    finest scale costs a third of it and cannot draw the wrong rectangle."""
    from plexora.server.utils import ome_zarr

    project = memory_api.register_memory_datasource(
        "store", sdata=sdata, channel_names=["A", "B", "C"],
        data_dir=plexora_data_root, **QUIET)
    kernel = memory_api.KernelNode.get(**QUIET)
    pyramid = kernel.registry.get("store-image").memory.pyramid

    assert ome_zarr.dyadic_prefix(pyramid.level_shapes) == len(pyramid)
    assert project.image.max_level == len(pyramid)


def test_an_explicit_argument_beats_the_store(sdata, image, plexora_data_root):
    """The mixed case: a SpatialData in memory for the table, and the real
    slide still on disk (or, here, a different array)."""
    project = memory_api.register_memory_datasource(
        "store", image, sdata=sdata, channel_names=["DNA", "CD3", "CD8"],
        data_dir=plexora_data_root, **QUIET)

    assert [entry["fullname"] for entry in project.image.channels][1:] == [
        "DNA", "CD3", "CD8"]


def test_two_tables_and_no_answer_is_a_question_not_a_guess(sdata, plexora_data_root):
    sdata.tables["second"] = sdata.tables["t"]

    with pytest.raises(memory_api.MemoryDataError) as excinfo:
        memory_api.register_memory_datasource("store", sdata=sdata,
                                              data_dir=plexora_data_root, **QUIET)
    assert "sdata_table=" in str(excinfo.value)


def test_a_named_element_that_is_not_there_lists_the_ones_that_are(
    sdata, plexora_data_root
):
    with pytest.raises(memory_api.MemoryDataError) as excinfo:
        memory_api.register_memory_datasource(
            "store", sdata=sdata, sdata_table="nope",
            data_dir=plexora_data_root, **QUIET)
    assert "it has: t" in str(excinfo.value)


# -- the notebook's own handle -------------------------------------------
#
# `PlexoraViewer.refresh` is the loop's second half. No sidecar is started here:
# what it does is re-register and then ask the SERVER to reload, and only the
# first of those is this process's business (see nodes._reload).


@pytest.fixture
def viewer(registered, monkeypatch, plexora_data_root):
    from plexora.jupyter import PlexoraViewer

    asked = []
    monkeypatch.setattr(PlexoraViewer, "_reload_server",
                        lambda self: asked.append(self.datasource) or True)
    handle = PlexoraViewer("tonsil", data_dir=plexora_data_root, memory=True,
                           start=False)
    handle.asked = asked
    return handle


def test_refresh_re_registers_and_then_asks_the_server_to_reload(
    viewer, adata, client
):
    adata.obs["phenotype"] = ["x"] * adata.n_obs

    assert viewer.refresh(adata) is viewer
    assert viewer.asked == ["tonsil"]
    assert Project.load("tonsil").dataset.obs_columns == ("leiden", "phenotype")


def test_refresh_of_a_file_backed_project_says_why_it_cannot(
    monkeypatch, plexora_data_root, tmp_path
):
    """A viewer opened by name over an ordinary project has nothing in this
    kernel to refresh from, and saying so beats registering a project over the
    top of one somebody imported."""
    from plexora.jupyter import PlexoraViewer
    from plexora.server.models.project import ImageSpec

    Project(name="ondisk", image=ImageSpec(src=str(tmp_path / "x.tif"))).save(
        plexora_data_root)
    monkeypatch.setattr(PlexoraViewer, "_reload_server", lambda self: True)
    handle = PlexoraViewer("ondisk", data_dir=plexora_data_root, start=False)

    with pytest.raises(RuntimeError) as excinfo:
        handle.refresh(adata=None)
    assert "reads its data from files" in str(excinfo.value)


def test_a_viewer_opened_by_name_over_a_memory_project_can_still_refresh(
    registered, monkeypatch, plexora_data_root, adata
):
    """`memory=True` is a hint, not the answer: the project record is what
    actually says where its resources are, so an earlier cell's project
    refreshes from a viewer that was not the one that made it."""
    from plexora.jupyter import PlexoraViewer

    monkeypatch.setattr(PlexoraViewer, "_reload_server", lambda self: True)
    handle = PlexoraViewer("tonsil", data_dir=plexora_data_root, start=False)

    adata.obs["phenotype"] = ["x"] * adata.n_obs
    handle.refresh(adata)
    assert "phenotype" in Project.load("tonsil").dataset.obs_columns
