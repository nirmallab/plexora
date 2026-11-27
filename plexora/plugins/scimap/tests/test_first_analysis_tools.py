"""The steps of a first analysis: the regrouped sidebar, Name groups in place
of the four annotate/merge tools, and the calls Cell types from gates, Check
quality and Compare groups make. No Scimap Pro is needed here."""

from __future__ import annotations

import pytest

from plexora.plugins.scimap.server import adapter, gates, tools


def test_sidebar_runs_in_the_order_of_a_first_analysis():
    order = [t for _key, _title, ids in tools.CATEGORIES for t in ids]
    # Normalize and Spatially variable genes are listed for counts data only (modes.COUNTS_ONLY).
    assert order == ["prepare", "quality", "normalize", "pca", "gating", "celltypes", "cluster", "markers", "name_groups",
                     "umap", "composition", "expression", "distance", "interaction", "neighborhoods", "svg",
                     "compare"]
    assert len(order) == len(set(order)) and set(order) == set(tools.TOOLS)
    assert not set(tools.LEGACY) & set(tools.TOOLS)


def test_older_annotate_and_merge_steps_open_as_name_groups():
    doc = {"cells": [
        {"id": "a", "tool": "annotate_clusters", "params": {"source": "k", "names": {"0": "T"}}, "views": [{"id": "x"}]},
        {"id": "b", "tool": "merge_neighborhoods",
         "params": {"source": "n", "groups": [{"name": "Core", "members": ["0", "2"]}]}},
        {"id": "c", "tool": "autophenotype", "params": {}},
        {"id": "d", "tool": "cluster", "params": {"k": 4}},
    ]}
    tools.migrate(doc)
    tools_now = [c["tool"] for c in doc["cells"]]
    assert tools_now == ["name_groups", "name_groups", "celltypes", "cluster"]
    assert doc["cells"][0]["params"]["names"] == {"0": "T"} and doc["cells"][0]["views"] is None
    # A merge is the same name on several groups.
    assert doc["cells"][1]["params"] == {"source": "n", "names": {"0": "Core", "2": "Core"}}
    assert doc["cells"][3]["params"] == {"k": 4}


WORK = {"markers": ["CD3", "CD4", "CD8", "CD20", "DNA1"], "image_column": "imageid",
        "images": ["a", "b"], "columns": [], "raw": True, "x_logged": False}


def test_rules_become_scimap_pros_gating_workflow():
    rules = tools.clean_rules([
        {"name": "T cells", "pos": ["CD3"]},
        {"parent": "T cells", "name": "CD4 T", "pos": ["CD4"], "neg": ["CD8"]},
        {"name": "Double", "pos": ["CD4", "CD8"]},
        {"name": "", "pos": ["CD20"]},              # no name: dropped
        {"name": "Ghost", "pos": ["NOPE"]},         # no marker in the panel: dropped
    ], WORK)
    assert [r["name"] for r in rules] == ["T cells", "CD4 T", "Double"]
    table = tools.rules_frame(rules, ["CD3", "CD4", "CD8"])["__frame__"]
    assert table["columns"] == ["parent", "celltype", "CD3", "CD4", "CD8"]
    assert table["rows"] == [["all", "T cells", "pos", "", ""],
                             ["T cells", "CD4 T", "", "pos", "neg"],
                             ["all", "Double", "", "allpos", "allpos"]]
    with pytest.raises(ValueError, match="not defined above"):
        tools.clean_rules([{"parent": "Later", "name": "X", "pos": ["CD3"]}, {"name": "Later", "pos": ["CD4"]}], WORK)


def test_a_table_argument_reaches_scimap_pro_as_a_dataframe():
    frame = adapter.as_frame(tools.frame(["marker", "a"], [["CD3", 1.5]]))
    assert list(frame.columns) == ["marker", "a"] and frame.iloc[0, 1] == 1.5
    assert adapter.as_frame(3) == 3


def test_gates_are_read_by_scimap_pro_on_the_scale_plexora_stored_them(monkeypatch):
    stored = {"a": ([{"channel": "CD3"}, {"channel": "CD4"}], False, "ra"),
              "b": ([{"channel": "CD3"}], False, "rb")}
    monkeypatch.setattr(gates, "_rows_of", lambda project: stored[project])
    doc = {"context": {"projects": ["a", "b"]}}
    src = gates.sources(doc, {**WORK, "images": ["a", "b"]})
    assert src["projects"] == {"a": "a", "b": "b"} and src["logged"] is False
    assert gates.missing(src, ["CD3", "CD4"]) == [("CD4", "b")]
    # One pp.rescale reads every image's gates on one scale.
    stored["b"] = ([{"channel": "CD3"}], True, "rb")
    with pytest.raises(ValueError, match="different scales"):
        gates.sources(doc, {**WORK, "images": ["a", "b"]})


def test_cell_types_call_syncfromviewer_then_rescale_into_its_own_layer(monkeypatch):
    from plexora.plugins.scimap.server import install

    monkeypatch.setattr(install, "supports", lambda feature: True)
    monkeypatch.setattr(gates, "_rows_of", lambda project: ([{"channel": "CD3"}, {"channel": "CD4"}], True, "r"))
    doc = {"context": {"projects": ["a", "b"]}, "cells": []}
    eff = tools.effective("celltypes", {"rules": [{"name": "T", "pos": ["CD3"]}]}, {**WORK, "images": ["a", "b"]},
                          doc, "c1")
    keys = [b["key"] for b in eff["before"]]
    assert keys == ["hl.syncFromViewer", "hl.syncFromViewer", "pp.rescale"]
    assert all(b["kwargs"]["peer"] == "inprocess" for b in eff["before"][:2])
    rescale = eff["before"][-1]
    assert rescale["kwargs"]["targetLayer"] == tools.GATED_LAYER and rescale["kwargs"]["log"] is True
    assert "gate" not in rescale["kwargs"]  # read from uns['gates'], where syncFromViewer put them
    assert eff["key"] == "sc.phenotype" and eff["kwargs"]["layer"] == tools.GATED_LAYER


def test_features_wait_for_the_scimap_pro_that_has_them(monkeypatch):
    from plexora.plugins.scimap.server import catalog, install

    monkeypatch.setattr(catalog, "load", lambda: {"functions": {
        "pp.rescale": {"params": [{"name": "data"}, {"name": "gate"}]},
        "tl.addMetadata": {"params": [{"name": "data"}, {"name": "overwrite"}]}}})
    assert not install.supports("gated_layer") and "targetLayer" in install.why_not("gated_layer")
    assert install.supports("metadata") and install.why_not("metadata") == ""
    assert not install.supports("labels")


def test_compare_needs_three_samples_in_each_group(monkeypatch):
    from plexora.plugins.scimap.server import samples

    table = {"columns": ["condition"], "rows": {"a": {"condition": "x"}, "b": {"condition": "x"},
                                                "c": {"condition": "y"}}}
    monkeypatch.setattr(samples, "get", lambda context: table)
    why = tools.too_few_samples({"context": {}}, ["condition"])
    assert "3 samples" in why and "x 2" in why and "y 1" in why
    table["rows"].update({k: {"condition": v} for k, v in [("d", "x"), ("e", "y"), ("f", "y")]})
    assert tools.too_few_samples({"context": {}}, ["condition"]) == ""
