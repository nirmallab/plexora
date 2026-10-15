"""Opening a sample before everything about it is ready.

Four things, each pinned here:

- **Which image of a multi-image table is this sample?** Matched by name at
  import (`import_proposal.subset_match`), applied as the default, asked as a
  question to change -- and a folder's cohort table is shared by every sample
  it names, each reading its own cells.
- **Inspecting a large AnnData is cheap and is done once.** It used to count
  every row of every obs column four times per import: 16 s a time on a
  5M-cell table, for an answer about one categorical column.
- **A large table loads behind the viewer.** The image and the mask open, the
  request returns, and the cells follow on a thread of their own; what reads
  cells waits for them, what draws the picture does not.
- **A mask conversion that is overtaken does not write itself back.** Removing
  or replacing a mask while it converts used to see it reappear.
"""

import threading
import time

import anndata as ad
import numpy as np
import pandas as pd
import pytest
import tifffile

import plexora
from plexora.server.models import data_model, import_proposal, import_sample
from plexora.server.models.adapters import inspection
from plexora.server.models.import_proposal import inspect_paths, subset_match
from plexora.server.models.project import Project

from tests.helpers import use_data_root


def _image(path, size=512, channels=2, seed=0):
    tifffile.imwrite(path, np.random.default_rng(seed).integers(
        0, 3000, (channels, size, size)).astype(np.uint16))
    return path


def _cohort(path, images=("LSP1", "LSP2"), per=50):
    """A table spanning several images, keyed by an `imageid` column."""
    count = per * len(images)
    rng = np.random.default_rng(1)
    obs = pd.DataFrame({
        "imageid": pd.Categorical(np.repeat(list(images), per)),
        "X_centroid": rng.uniform(0, 500, count),
        "Y_centroid": rng.uniform(0, 500, count),
        "Area": rng.uniform(10, 50, count),
    }, index=[f"c{i}" for i in range(count)])
    adata = ad.AnnData(X=rng.gamma(2, 100, (count, 3)).astype(np.float32), obs=obs,
                       var=pd.DataFrame(index=["DNA", "CD3", "CD8"]))
    adata.obsm["spatial"] = obs[["X_centroid", "Y_centroid"]].to_numpy()
    adata.write_h5ad(path)
    return path


# -- which image a table's cells belong to --------------------------------------

@pytest.mark.parametrize("values, names, expected", [
    (["LSP10000_tumor", "LSP10003_tumor"], ["LSP10003_tumor"], "LSP10003_tumor"),
    # The value is a run of the name's words, or the other way round.
    (["LSP1", "LSP2"], ["LSP2.ome.tif"], "LSP2"),
    (["LSP10003"], ["LSP10003_tumor_region"], "LSP10003"),
    # Digits are numbers: LSP1 is not a prefix of LSP10003, and 0003 is 3.
    (["LSP1", "LSP2"], ["LSP10003"], None),
    (["LSP3", "LSP4"], ["LSP0003_scan"], "LSP3"),
    (["1", "3", "13"], ["slide_3"], "3"),
    # A bare number only when it is the name's one number: these are a date
    # and a run, and the answer is applied without asking.
    (["10", "12"], ["scan_2024_10_14"], None),
    (["1", "2", "3"], ["run3_2024"], None),
    # A folder names whatever is in it, so only exactly.
    (["1", "2", "3"], [import_proposal._ExactName("2024_run3"), "slide"], None),
    (["LSP1", "LSP10003"], [import_proposal._ExactName("LSP10003")], "LSP10003"),
    # Two equally good answers are a question, not a coin toss.
    (["tumor_a", "tumor_b"], ["tumor"], None),
    (["A", "B"], [], None),
])
def test_a_value_matches_an_image_by_its_words(values, names, expected):
    assert subset_match(values, names) == expected


def test_a_folders_number_is_not_an_images_id(tmp_path):
    (tmp_path / "2024_run3").mkdir()
    image = _image(tmp_path / "2024_run3" / "slide.ome.tif")
    names = import_proposal._image_names(str(image), "slide")
    assert subset_match(["1", "2", "3"], names) is None


def test_the_name_a_writer_gives_every_image_names_nothing(tmp_path):
    """tifffile calls every image it writes "Image0": not image 0 of a table."""
    image = _image(tmp_path / "slide.ome.tif")
    assert import_proposal._ome_image_name(str(image)) is None
    assert subset_match(["0", "1"], import_proposal._image_names(str(image))) is None


def test_a_lone_ome_tiff_names_its_sample_without_ome(tmp_path):
    _image(tmp_path / "LSP1.ome.tif")
    _cohort(tmp_path / "cohort.h5ad")
    sample = inspect_paths([str(tmp_path / "LSP1.ome.tif"),
                            str(tmp_path / "cohort.h5ad")]).samples[0]
    assert sample.name == "LSP1"


def test_a_picked_table_reads_the_cells_of_the_image_beside_it(tmp_path):
    """Applied, not merely suggested: the table is registered reading LSP2's
    cells, and the question stays on the card to change it."""
    _image(tmp_path / "LSP2.ome.tif")
    _cohort(tmp_path / "cohort.h5ad")
    sample = inspect_paths([str(tmp_path / "LSP2.ome.tif"),
                            str(tmp_path / "cohort.h5ad")]).samples[0]
    table = next(layer for layer in sample.layers if layer.role == "table")
    assert table.subset == {"column": "imageid", "values": ["LSP1", "LSP2"],
                            "value": "LSP2"}
    [question] = [q for q in sample.questions if q.id.startswith("subset:")]
    assert question.default == "LSP2"
    assert not table.needs

    changed = inspect_paths([str(tmp_path / "LSP2.ome.tif"), str(tmp_path / "cohort.h5ad")],
                            answers={question.id: "LSP1"}).samples[0]
    assert next(l for l in changed.layers if l.role == "table").subset["value"] == "LSP1"


def test_no_match_is_left_for_the_user_and_recorded_as_such(tmp_path):
    _image(tmp_path / "slide.ome.tif")
    _cohort(tmp_path / "cohort.h5ad")
    sample = inspect_paths([str(tmp_path / "slide.ome.tif"),
                            str(tmp_path / "cohort.h5ad")]).samples[0]
    table = next(layer for layer in sample.layers if layer.role == "table")
    assert table.subset["value"] is None
    assert any(need.startswith("subset:") for need in table.needs)


def test_a_folders_cohort_table_is_shared_by_every_sample_it_names(tmp_path):
    """It used to go to the first card, with "Which sample does cohort.h5ad
    belong to?" -- and the other slide got no cells at all."""
    _image(tmp_path / "LSP1.ome.tif")
    _image(tmp_path / "LSP2.ome.tif", seed=1)
    _cohort(tmp_path / "cohort.h5ad")
    samples = inspect_paths([str(tmp_path)]).samples
    read = {sample.name: next(l.subset["value"] for l in sample.layers
                              if l.role == "table") for sample in samples}
    assert read == {"lsp1": "LSP1", "lsp2": "LSP2"}
    assert not [q for s in samples for q in s.questions
                if q.id.startswith("sample-for:")]


def test_registering_reads_only_the_matched_images_cells(tmp_path, monkeypatch):
    use_data_root(monkeypatch, tmp_path / "root")
    (tmp_path / "root" / "config.json").write_text("{}", encoding="utf-8")
    _image(tmp_path / "LSP2.ome.tif")
    _cohort(tmp_path / "cohort.h5ad")
    import_sample.import_sample([str(tmp_path / "LSP2.ome.tif"),
                                 str(tmp_path / "cohort.h5ad")], name="two")
    dataset = Project.load("two").dataset
    assert dataset.is_resolved
    assert dataset.subset == {"column": "imageid", "value": "LSP2"}


def test_adding_a_table_to_a_sample_matches_the_samples_own_image(tmp_path, monkeypatch):
    """The sidebar's Add Data: the scoped dialog reads the project's image."""
    use_data_root(monkeypatch, tmp_path / "root")
    (tmp_path / "root" / "config.json").write_text("{}", encoding="utf-8")
    _image(tmp_path / "LSP1.ome.tif")
    _cohort(tmp_path / "cohort.h5ad")
    import_sample.import_sample([str(tmp_path / "LSP1.ome.tif")], name="one")
    added = import_sample.add_layers("one", [str(tmp_path / "cohort.h5ad")])
    assert added["table"] is True
    assert Project.load("one").dataset.subset == {"column": "imageid", "value": "LSP1"}


def test_inspect_data_suggests_the_projects_image(tmp_path, monkeypatch):
    """The requirements modal and the edit page preselect it."""
    use_data_root(monkeypatch, tmp_path / "root")
    (tmp_path / "root" / "config.json").write_text("{}", encoding="utf-8")
    _image(tmp_path / "LSP2.ome.tif")
    _cohort(tmp_path / "cohort.h5ad")
    import_sample.import_sample([str(tmp_path / "LSP2.ome.tif")], name="two")
    answer = plexora.app.test_client().post("/inspect_data", json={
        "path": str(tmp_path / "cohort.h5ad"), "datasource": "two"}).get_json()
    assert answer["ambiguous"] == [{"column": "imageid", "values": ["LSP1", "LSP2"],
                                    "suggested": "LSP2"}]


# -- inspection, once and cheaply ----------------------------------------------

def test_an_inspection_is_answered_once_per_file_version(tmp_path, monkeypatch):
    path = _cohort(tmp_path / "cohort.h5ad")
    inspection._INSPECTED.clear()
    first = inspection.inspect_anndata(path)
    first["obs_columns"].clear()          # a caller editing what it was given
    calls = []
    import h5py
    real = h5py.File
    monkeypatch.setattr(h5py, "File", lambda *a, **k: calls.append(a) or real(*a, **k))
    again = inspection.inspect_anndata(path)
    assert not calls, "a file that has not changed was read again"
    assert [c["name"] for c in again["obs_columns"]] == [
        "imageid", "X_centroid", "Y_centroid", "Area"]


def test_a_continuous_column_is_not_counted_in_full(monkeypatch):
    """Its first rows already hold more distinct values than any dropdown, so
    it is reported as past the limit without the full count."""
    series = pd.Series(np.random.default_rng(0).uniform(size=60_000))
    counted = []
    monkeypatch.setattr(pd.Series, "value_counts",
                        lambda self, *a, **k: counted.append(1))
    n_unique, values = inspection._distinct_values(series)
    assert n_unique > inspection._MAX_CANDIDATE_VALUES and values == []
    assert not counted


def test_a_categorical_is_counted_from_its_codes():
    series = pd.Series(pd.Categorical(["b", "a", "b"], categories=["a", "b", "c"]))
    assert inspection._distinct_values(series) == (2, ["a", "b"])


def test_an_answered_subset_skips_the_inspection(tmp_path, monkeypatch):
    from plexora.datasource import deferred_spec

    monkeypatch.setattr(inspection, "inspect_anndata",
                        lambda path: pytest.fail("inspected anyway"))
    assert deferred_spec(tmp_path / "cohort.h5ad", "anndata", subset_by="imageid") is None


# -- a large table behind the viewer --------------------------------------------

@pytest.fixture
def held_table(tmp_path, monkeypatch):
    """Two projects whose tables load behind the viewer -- "held" (200 cells)
    and "other" (50) -- with every read held open until the test lets go."""
    use_data_root(monkeypatch, tmp_path / "root")
    (tmp_path / "root" / "config.json").write_text("{}", encoding="utf-8")
    for name, image, per in (("held", "LSP1", 200), ("other", "LSP9", 50)):
        _image(tmp_path / f"{image}.ome.tif")
        _cohort(tmp_path / f"{image}_cells.h5ad", images=(image,), per=per)
        import_sample.import_sample([str(tmp_path / f"{image}.ome.tif"),
                                     str(tmp_path / f"{image}_cells.h5ad")], name=name)
    monkeypatch.setattr(data_model, "BACKGROUND_TABLE_BYTES", 0)
    release = threading.Event()
    from plexora.server.providers import local

    real = local.LocalTableProvider.load

    def held(self, *args, **kwargs):
        release.wait(10)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(local.LocalTableProvider, "load", held)
    yield release
    release.set()


def test_the_image_opens_while_the_cells_are_still_being_read(held_table):
    data_model.load_datasource("held")
    assert data_model.is_loaded("held")
    assert data_model.table_pending("held")
    # The picture's requests answer now.
    assert data_model.get_channel_names("held")
    # The boot's description answers image-only rather than waiting, and the
    # route says why, so the page takes the cells on when they land...
    assert data_model.get_datasource_description("held", wait=False) is None
    answer = plexora.app.test_client().get("/get_database_description?datasource=held")
    assert answer.get_json() == {}
    assert answer.headers["X-Plexora-Cells-Pending"] == "1"
    # ...and the status says the cells are still coming.
    assert data_model.get_load_status("held")["table"]["status"] == "pending"

    held_table.set()
    # What reads cells waits for them -- and for their index, so the first
    # picks do not each build one.
    cells = data_model.get_all_cells("held", ["id", "X", "Y"], int)
    assert len(cells) == 200 * 3
    assert data_model.ball_tree is not None and len(data_model.ball_tree) == 200
    _join_tables()
    assert not data_model.table_preparing("held")
    assert "table" not in data_model.get_load_status("held")
    assert data_model.get_datasource_description("held", wait=False)
    answer = plexora.app.test_client().get("/get_database_description?datasource=held")
    assert "X-Plexora-Cells-Pending" not in answer.headers


def _join_tables():
    for thread in threading.enumerate():
        if thread.name.startswith("table-"):
            thread.join(20)


def test_a_reload_that_validates_reads_the_table_in_place(held_table):
    """A route that restores the previous project when the table will not
    read asks to wait; whatever was waiting on the earlier read reads what
    the reload put in place."""
    data_model.load_datasource("held")
    assert data_model.table_pending("held")
    held_table.set()
    data_model.load_datasource("held", reload=True, wait_for_table=True)
    assert not data_model.table_pending("held")
    assert data_model.get_datasource_df() is not None


def test_any_other_reload_reads_behind_and_the_page_keeps_waiting(held_table):
    """A mask that finished converting reloads the open project. The status
    carries `table` the whole way through, so the page never reads the
    reload as the cells arriving; the first read stops, and its record is
    not the one the page sees finish."""
    data_model.load_datasource("held")
    data_model.load_datasource("held", reload=True)
    assert data_model.get_load_status("held")["table"]["status"] == "pending"
    held_table.set()
    assert len(data_model.get_datasource_df()) == 200
    _join_tables()
    assert "table" not in data_model.get_load_status("held")
    assert data_model.get_table_job_status("held")["status"] == "ready"


def test_a_reader_waiting_through_a_reload_gets_the_cells(held_table):
    """Woken by the reload that superseded the read it was waiting on, it
    waits on the reload's own read rather than reading no table at all."""
    data_model.load_datasource("held")
    got = {}
    reader = threading.Thread(
        target=lambda: got.setdefault("frame", data_model.get_datasource_df()))
    reader.start()
    time.sleep(0.3)  # waiting on the first read by now
    data_model.load_datasource("held", reload=True)
    held_table.set()
    reader.join(20)
    assert got["frame"] is not None and len(got["frame"]) == 200


def test_a_switch_away_never_lands_the_old_tables_cells_or_index(held_table):
    data_model.load_datasource("held")
    data_model.load_datasource("other")
    held_table.set()
    _join_tables()
    assert data_model.is_loaded("other")
    assert len(data_model.get_datasource_df()) == 50
    assert len(data_model.ball_tree) == 50
    assert not data_model.table_preparing("held")
    assert not data_model.table_preparing("other")


def test_another_projects_failed_load_leaves_this_tables_read_to_land(held_table,
                                                                     monkeypatch):
    from plexora.server.providers import local

    data_model.load_datasource("held")

    def corrupt(self, *args, **kwargs):
        # "held"'s image is open already: the next one opened is "other"'s.
        raise RuntimeError("the image is corrupt")

    monkeypatch.setattr(local.LocalImageProvider, "open", corrupt)
    with pytest.raises(RuntimeError, match="corrupt"):
        data_model.load_datasource("other")
    held_table.set()
    _join_tables()
    assert data_model.is_loaded("held")
    assert len(data_model.get_datasource_df()) == 200


def test_a_table_that_fails_behind_the_viewer_says_so(held_table, monkeypatch):
    from plexora.server.providers import local

    def truncated(self, *args, **kwargs):
        raise ValueError("the file is truncated")

    monkeypatch.setattr(local.LocalTableProvider, "load", truncated)
    data_model.load_datasource("held")
    _join_tables()
    table = data_model.get_load_status("held")["table"]
    assert table["status"] == "error" and "truncated" in table["error"]
    # Read as a project with no table is, rather than failing on the absence.
    assert data_model.get_datasource_description("held") == {}
    assert len(data_model.get_all_cells("held", ["id"], int)) == 0
    assert data_model.get_channel_cells("held", ["CD3"]) == []


def test_a_table_added_to_the_open_sample_is_the_one_it_serves(tmp_path, monkeypatch):
    """The viewer's Add data: the record alone left the open project serving
    no cells until the server restarted."""
    use_data_root(monkeypatch, tmp_path / "root")
    (tmp_path / "root" / "config.json").write_text("{}", encoding="utf-8")
    _image(tmp_path / "LSP1.ome.tif")
    _cohort(tmp_path / "cohort.h5ad")
    import_sample.import_sample([str(tmp_path / "LSP1.ome.tif")], name="one")
    data_model.load_datasource("one")
    assert data_model.get_datasource_df() is None
    import_sample.add_layers("one", [str(tmp_path / "cohort.h5ad")])
    assert len(data_model.get_datasource_df()) == 50  # LSP1's cells, not LSP2's
    assert data_model.get_datasource_description("one")


def test_a_small_table_still_opens_in_place(tmp_path, monkeypatch):
    use_data_root(monkeypatch, tmp_path / "root")
    (tmp_path / "root" / "config.json").write_text("{}", encoding="utf-8")
    _image(tmp_path / "LSP1.ome.tif")
    _cohort(tmp_path / "LSP1_cells.h5ad", images=("LSP1",))
    import_sample.import_sample([str(tmp_path / "LSP1.ome.tif"),
                                 str(tmp_path / "LSP1_cells.h5ad")], name="small")
    data_model.load_datasource("small")
    assert not data_model.table_pending("small")
    assert data_model.get_datasource_df() is not None
    # And is indexed as the viewer opens, as it always was.
    data_model.init("small")
    assert data_model.ball_tree is not None


# -- an overtaken mask conversion ------------------------------------------------

def _labels(path, size=256, offset=0):
    plane = np.zeros((size, size), dtype=np.uint32)
    for index in range(1, 6):
        top = offset + index * 30
        plane[top:top + 12, top:top + 12] = index
    tifffile.imwrite(path, plane)
    return path


@pytest.fixture
def held_conversion(monkeypatch):
    release = threading.Event()
    real = data_model.convertOmeTiff

    def held(*args, **kwargs):
        release.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(data_model, "convertOmeTiff", held)
    yield release
    release.set()


def _join_conversions():
    for thread in threading.enumerate():
        if thread.name.startswith("segmentation-"):
            thread.join(20)


def test_a_mask_removed_while_converting_stays_removed(tmp_path, monkeypatch, held_conversion):
    from plexora.server.routes.import_routes import attach_segmentation

    use_data_root(monkeypatch, tmp_path / "root")
    (tmp_path / "root" / "config.json").write_text("{}", encoding="utf-8")
    _image(tmp_path / "slide.ome.tif")
    import_sample.import_sample([str(tmp_path / "slide.ome.tif")], name="s")
    attach_segmentation("s", str(_labels(tmp_path / "cells.tif")))
    attach_segmentation("s", None)
    held_conversion.set()
    _join_conversions()
    project = Project.load("s")
    assert not project.segmentation.source and not project.segmentation.derived
    # "Ready" with nothing in it is what a sample with no mask reports; what
    # must not happen is the status handing the viewer the removed mask.
    assert not data_model.get_segmentation_job_status("s").get("segmentation")


def test_a_mask_replaced_while_converting_is_the_one_that_lands(tmp_path, monkeypatch,
                                                                held_conversion):
    """The replacement's own job was refused ("already running"), so the
    overtaken one hands over to it on the way out."""
    from plexora.server.routes.import_routes import attach_segmentation

    use_data_root(monkeypatch, tmp_path / "root")
    (tmp_path / "root" / "config.json").write_text("{}", encoding="utf-8")
    _image(tmp_path / "slide.ome.tif")
    import_sample.import_sample([str(tmp_path / "slide.ome.tif")], name="s")
    first = _labels(tmp_path / "first.tif")
    second = _labels(tmp_path / "second.tif", offset=5)
    attach_segmentation("s", str(first))
    attach_segmentation("s", str(second))
    held_conversion.set()
    _join_conversions()
    _join_conversions()
    project = Project.load("s")
    assert project.segmentation.source == str(second)
    assert project.segmentation.derived
    assert project.segmentation.status == "ready"
