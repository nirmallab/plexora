"""Progress reporting while a project's cell table is prepared.

The complaint: naming a data file and pressing Save showed a disabled button
and nothing else, for as long as reading that file took. On a table spanning
sixty images that is the longest thing either form does.

The load stays SYNCHRONOUS -- inside the save request -- because that request is
also what validates the user's answer and puts the previous project back when
the answer turns out to be unreadable. All that is added is somewhere for the
work to say where it has got to, which the browser polls alongside.
"""

import numpy as np
import pytest

from plexora.server.models import data_model


@pytest.fixture(autouse=True)
def _clean_jobs():
    data_model._table_jobs.clear()
    yield
    data_model._table_jobs.clear()


def test_an_unknown_project_reads_as_ready_not_as_missing():
    """The browser starts polling the moment it posts, and may well ask before
    the thread doing the work has reached its first stage. An absent record has
    to mean "nothing to report", never "something went wrong"."""
    status = data_model.get_table_job_status("never-loaded")

    assert status["status"] == "ready"
    assert status["error"] is None


def test_the_bands_are_ordered_contiguous_and_end_below_a_hundred():
    bands = list(data_model.TABLE_STAGES.values())
    for (_, end, _label), (start, _, _next) in zip(bands, bands[1:]):
        assert end == start
    assert bands[0][0] == 0
    assert bands[-1][1] < 100


def test_the_record_carries_the_stage_the_bar_needs():
    stage, report = data_model.table_progress("proj")

    stage("preparing")
    status = data_model.get_table_job_status("proj")

    assert status["status"] == "pending"
    assert status["stage"] == "preparing"
    assert status["stage_label"] == "Loading marker values"
    assert status["progress"] == data_model.TABLE_STAGES["preparing"][0]


def test_block_reports_move_within_the_stages_band():
    stage, report = data_model.table_progress("proj")
    start, end, _ = data_model.TABLE_STAGES["preparing"]

    stage("preparing")
    report(1, 2)
    mid = data_model.get_table_job_status("proj")["progress"]
    report(2, 2)
    done = data_model.get_table_job_status("proj")["progress"]

    assert start <= mid <= end
    assert done == end


def test_finishing_clears_the_record_to_ready():
    stage, _report = data_model.table_progress("proj")
    stage("preparing")

    data_model.finish_table_job("proj")

    status = data_model.get_table_job_status("proj")
    assert status["status"] == "ready"
    assert status["progress"] == 100
    assert status["error"] is None


def test_a_failed_load_is_reported_with_its_reason():
    stage, _report = data_model.table_progress("proj")
    stage("metadata")

    data_model.finish_table_job("proj", ValueError("no such column 'nope'"))

    status = data_model.get_table_job_status("proj")
    assert status["status"] == "error"
    assert "nope" in status["error"]


def test_the_adapter_reports_its_stages_while_reading(tmp_path):
    """End to end through the real adapter: naming the stages is only useful if
    the read actually announces them."""
    import anndata as ad
    import pandas as pd

    from plexora.server.models.adapters import AnnDataAdapter
    from plexora.server.models.project import ColumnRoles, DataSpec

    n = 40
    adata = ad.AnnData(
        X=np.random.default_rng(0).random((n, 3)).astype(np.float32),
        obs=pd.DataFrame({"image_id": ["one"] * n}, index=[f"c{i}" for i in range(n)]),
        var=pd.DataFrame(index=["a", "b", "c"]),
    )
    adata.obsm["spatial"] = np.stack([np.arange(n), np.arange(n) * 1.0], axis=1)
    path = tmp_path / "cells.h5ad"
    adata.write_h5ad(path)

    stage, report = data_model.table_progress("proj")
    spec = DataSpec(type="anndata", src=str(path), coordinates={},
                    features={"source": "X"},
                    roles=ColumnRoles(x="X", y="Y", cell_id="id"))

    AnnDataAdapter(spec).load_table(stage=stage, report=report)

    status = data_model.get_table_job_status("proj")
    # It got at least as far as reading values, and said so on the way.
    assert status["stage"] in ("preparing", "metadata")
    assert status["progress"] >= data_model.TABLE_STAGES["metadata"][0]


def test_load_table_still_works_with_no_reporters(tmp_path):
    """Both are optional everywhere they are threaded -- a node calls the local
    provider with neither, and so does every other caller in the tree."""
    import anndata as ad
    import pandas as pd

    from plexora.server.models.adapters import AnnDataAdapter
    from plexora.server.models.project import ColumnRoles, DataSpec

    adata = ad.AnnData(
        X=np.zeros((5, 2), dtype=np.float32),
        obs=pd.DataFrame(index=[f"c{i}" for i in range(5)]),
        var=pd.DataFrame(index=["m0", "m1"]),
    )
    adata.obsm["spatial"] = np.zeros((5, 2))
    path = tmp_path / "plain.h5ad"
    adata.write_h5ad(path)

    spec = DataSpec(type="anndata", src=str(path), coordinates={},
                    features={"source": "X"},
                    roles=ColumnRoles(x="X", y="Y", cell_id="id"))

    assert AnnDataAdapter(spec).load_table().table.height == 5
