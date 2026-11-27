"""The tools a notebook lists, their words, defaults and order: SCIMAP Pro's answer, wired to the page.

`modes` decides nothing: a tool is hidden because SCIMAP Pro's roadmap says its analysis does not fit, a default
is the argument SCIMAP Pro's workflow passes, "Suggested next" is the workflow's order. These tests feed it
SCIMAP Pro-shaped answers and check the wiring; `test_counts_steps` reads real ones.
"""

from __future__ import annotations

import pytest

from plexora.plugins.scimap.server import lexicon, modes, tools


def _ids(catalog):
    return [t["id"] for c in catalog for t in c["tools"]]


def _tool(catalog, tool_id):
    return next(t for c in catalog for t in c["tools"] if t["id"] == tool_id)


def _said(mode, states=None, recipe=None):
    return {"facts": {"mode": mode}, "states": states or {}, "recipe": recipe or {}}


#: A recipe shaped as `sm.ai.recipe` returns it for a cell-resolved spatial transcriptomics dataset.
ST_CELL = {"template": "st-cell", "parameters": {"maxDistance": 53.7, "nPcs": 20},
           "plans": {"baseline": [
               {"id": "qc-metrics", "function": "pp.qcMetrics", "arguments": {"imageId": "{imageColumn}"}},
               {"id": "normalize", "function": "pp.normalizeTotal", "arguments": {"targetSum": 10000}},
               {"id": "log", "function": "pp.transform", "arguments": {"layer": "normalized", "targetLayer": "log"}},
               {"id": "pca", "function": "pp.pca", "arguments": {"layer": "log", "nComps": 20,
                                                                  "useHighlyVariable": False}},
               {"id": "neighbors", "function": "pp.neighbors", "arguments": {"useRep": "X_pca", "nNeighbors": 15}},
               {"id": "cluster", "function": "tl.cluster", "arguments": {"layer": "log", "useGraph": True,
                                                                         "graphKey": "connectivities",
                                                                         "leidenResolution": 1.0, "label": "leiden"}},
               {"id": "rank-markers", "function": "sc.rankFeatures", "arguments": {"groupBy": "leiden"}}],
               "space": [{"id": "svg", "function": "sp.spatiallyVariableFeatures",
                          "arguments": {"method": "moran", "graphMethod": "knn", "knn": 6, "genes": "{svgGenes}"}}]}}


@pytest.mark.parametrize("mode", ["imaging", None])
def test_without_a_roadmap_imaging_is_exactly_todays_catalogue(mode):
    """No SCIMAP Pro roadmap: every tool but the counts-only ones, word for word."""
    expected = [{**c, "tools": [t for t in c["tools"] if t["id"] not in modes.COUNTS_ONLY]}
                for c in tools.describe()]
    assert modes.catalog(_said(mode)) == [c for c in expected if c["tools"]]


def test_every_template_reads_as_the_tool_table_for_imaging():
    """The templates are the tool table's own text with slots: filled for imaging they must match it."""
    for tool_id, text in modes.TEXT.items():
        if tool_id in modes.COUNTS_ONLY | {"svg"}:
            continue
        tool = tools.TOOLS[tool_id]
        for key in ("name", "summary"):
            if key in text:
                assert lexicon.fill(text[key], "imaging") == tool[key], (tool_id, key)
        fields = {f["name"]: f for f in tool["fields"]}
        for name, entries in (text.get("fields") or {}).items():
            for key, value in entries.items():
                assert lexicon.fill(value, "imaging") == fields[name].get(key), (tool_id, name, key)


def test_a_tool_is_hidden_exactly_when_scimap_pro_says_its_analysis_does_not_fit():
    states = {"annotate-cells-gating": "not_applicable", "measure-distances": "not_applicable",
              "normalize-counts": "ready", "spatially-variable-features": "blocked"}
    catalog = modes.catalog(_said("st-spot", states))
    assert not {"gating", "celltypes", "distance"} & set(_ids(catalog))
    # Blocked is not "does not fit": listed, and availability says what it needs.
    assert {"normalize", "svg", "interaction"} <= set(_ids(catalog))


def test_the_words_follow_the_data():
    catalog = modes.catalog(_said("st-bin", {"annotate-cells-gating": "not_applicable"}))
    assert _tool(catalog, "composition")["name"] == "Bin composition"
    assert _tool(catalog, "expression")["name"] == "Gene expression"
    assert _tool(catalog, "svg")["name"] == "Spatially variable genes"
    assert _tool(modes.catalog(_said("imaging", {"normalize-counts": "not_applicable"})), "svg")["name"] == \
        "Spatially variable markers"


def test_a_form_opens_with_scimap_pros_workflow_for_the_data():
    said = _said("st-cell", {}, ST_CELL)
    catalog = modes.catalog(said)
    fields = {f["name"]: f["default"] for f in _tool(catalog, "pca")["fields"]}
    assert fields == {"components": 20, "variable": False, "neighbors": 15}
    svg = {f["name"]: f["default"] for f in _tool(catalog, "svg")["fields"]}
    assert svg["knn"] == 6 and svg["radius"] == 53.7
    # A placeholder the workflow could not fill is no default.
    assert "genes" not in modes.recipe_args(said, "sp.spatiallyVariableFeatures")


def test_suggested_next_is_the_workflows_order_on_counts_only():
    nxt = modes.next_steps(_said("st-cell", {}, ST_CELL))
    assert nxt["prepare"][0] == "normalize"
    assert nxt["normalize"][:2] == ["pca", "cluster"]
    assert modes.next_steps(_said("imaging", {}, ST_CELL)) is None
    assert modes.next_steps(_said("st-cell")) is None


def test_wording_never_changes_the_tool_table_itself():
    before = tools.describe()
    modes.catalog(_said("st-bin", {}, ST_CELL))
    assert tools.describe() == before
