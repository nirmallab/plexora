"""Multi-channel bright cells (`cells.multibright`, reason
`multi_channel_bright`): a cell bright in markers of lineages the marker graph
keeps apart is flagged; real lineages bright in many of their own markers are
not; a panel that cannot tell enough lineages apart skips the check; the
preset moves warn to exclude without anything read again."""

import numpy as np
import polars as pl
import pytest

from plexora.plugins.qc.server import schemas, strictness
from plexora.plugins.qc.server.cells import calls, multibright

PANEL = ["DNA1", "CD3E", "CD8a", "CD69", "TCF1", "CD163", "CD206", "IBA1", "HLADRA",
         "Ecadherin", "bCAT", "panCK", "CDX2", "CD31", "CD34", "VIM", "CD66b", "CD11b",
         "CD11c", "CD20", "PDPN", "FOXP3", "CD4", "Ki67", "Rb"]
LINEAGES = {"t": ["CD3E", "CD8a", "CD69", "TCF1"],
            "mac": ["CD163", "CD206", "IBA1", "HLADRA"],
            "epi": ["Ecadherin", "bCAT", "panCK", "CDX2"],
            "endo": ["CD31", "CD34", "VIM"],
            "b": ["CD20"], "neut": ["CD66b", "CD11b"]}


def _table(n=8000, af_share=0.015, seed=0, panel=PANEL):
    """{marker: log values}, {population: row mask}. Background cells are
    dim noise; each lineage's cells are bright in all its markers (T cells in
    CD3E, CD8a, CD69 and TCF1 at once); a proliferating share of epithelium in
    Ki67 and Rb; the autofluorescent cells glow in every channel, about as
    bright as a real positive."""
    rng = np.random.default_rng(seed)
    shares = {"t": 0.12, "mac": 0.10, "epi": 0.25, "endo": 0.05, "b": 0.03, "neut": 0.03}
    labels = np.full(n, "other", dtype=object)
    cut = 0
    order = rng.permutation(n)
    for pop, share in shares.items():
        k = int(share * n)
        labels[order[cut:cut + k]] = pop
        cut += k
    k = int(af_share * n)
    labels[order[cut:cut + k]] = "af"
    values = {m: rng.normal(1.0, 0.3, n) for m in panel}
    for pop, members in LINEAGES.items():
        rows = labels == pop
        for m in members:
            if m in values:
                values[m][rows] = rng.normal(4.0, 0.4, int(rows.sum()))
    epi = np.flatnonzero(labels == "epi")
    cycling = epi[: epi.size // 4]
    for m in ("Ki67", "Rb"):
        if m in values:
            values[m][cycling] = rng.normal(4.0, 0.4, cycling.size)
    af = labels == "af"
    for m in panel:
        values[m][af] = rng.normal(4.6, 0.3, int(af.sum()))
    masks = {pop: labels == pop for pop in (*shares, "af", "other")}
    return values, masks


def _standard():
    table = strictness.thresholds("standard")
    return table["cells.multi_bright_quantile"], table["cells.multi_bright_lineages"]


# -- the graph's lineages ----------------------------------------------------------


def test_the_panel_resolves_its_lineages_and_exclusions():
    info = multibright.panel(PANEL)
    assert "DNA1" in info["skipped"]
    assert {"Ki67", "CD69", "TCF1", "HLADRA", "bCAT"} <= set(info["states"])
    assert info["lineage"]["CD3E"]["family"] == "t_cell"
    assert info["lineage"]["CDX2"]["family"] == "intestinal_epithelial"
    # Exclusive across lineages the graph keeps apart...
    assert ("CD3E", "CD163") in info["exclusive"]
    assert ("CD3E", "panCK") in info["exclusive"]
    assert ("CD31", "Ecadherin") in info["exclusive"]
    # ...never within one, nor across an `also_on` exception the graph holds
    # everywhere (CD31 is on immune cells, CD4 on macrophages, vimentin on
    # almost everything).
    assert ("CD3E", "CD8a") not in info["exclusive"]
    assert ("CD163", "IBA1") not in info["exclusive"]
    assert ("Ecadherin", "CDX2") not in info["exclusive"]
    assert ("CD31", "CD163") not in info["exclusive"]
    assert ("CD4", "CD163") not in info["exclusive"]
    assert not any("VIM" in pair for pair in info["exclusive"])
    assert info["max_lineages"] >= 3


# -- (a) and (b): planted autofluorescence flagged, real lineages not --------------


def test_autofluorescent_cells_are_flagged_and_real_lineages_are_not():
    values, pops = _table()
    quantile, lineages = _standard()
    flagged, record = multibright.detect(values, quantile=quantile, min_lineages=lineages)
    assert record["method"] == "graph" and not record["skipped"]
    af = pops["af"]
    assert flagged[af].mean() >= 0.95, flagged[af].mean()
    # T cells bright in CD3E, CD8a, CD69 and TCF1, macrophages in CD163,
    # CD206, IBA1 and HLA-DR, epithelium in E-cadherin, beta-catenin, panCK
    # and CDX2 (and Ki67 with Rb): one lineage each, however many markers.
    for pop in ("t", "mac", "epi", "endo", "b", "neut", "other"):
        assert flagged[pops[pop]].mean() <= 0.01, (pop, flagged[pops[pop]].mean())
    assert record["n_flagged"] == int(flagged.sum())
    assert set(record["lit_in_flagged"]) >= {"CD3E", "CD163", "Ecadherin"}
    assert len(record["families"]) >= 3
    assert record["cutoffs"] and "Ki67" not in record["cutoffs"]


def test_many_markers_of_one_lineage_never_flag():
    """Without the glowing cells nothing is flagged: every T cell lit in four
    T-cell markers, every macrophage in four of its own, is one lineage."""
    values, pops = _table(af_share=0.0)
    quantile, lineages = _standard()
    flagged, record = multibright.detect(values, quantile=quantile, min_lineages=lineages)
    assert record["method"] == "graph"
    assert flagged.sum() <= 0.005 * flagged.size


def test_cutoffs_are_read_in_the_tissue():
    """Cells outside the tissue (the Background ROI) set no cutoff: a glass
    full of dim cells does not make dim tissue cells lit."""
    values, pops = _table()
    tissue = np.ones(next(iter(values.values())).size, dtype=bool)
    tissue[:2000] = False
    for m in values:
        values[m][:2000] = 0.0
    _f, inside = multibright.detect(values, quantile=0.97, min_lineages=3, tissue=tissue)
    _f, everywhere = multibright.detect(values, quantile=0.97, min_lineages=3)
    assert inside["cutoffs"]["CD3E"] >= everywhere["cutoffs"]["CD3E"]


# -- (c): a panel too few lineages resolve is skipped, with why -------------------


@pytest.mark.parametrize("panel, covers", [
    (["DNA1", "CD3E", "CD8a", "CD69", "TCF1", "Ki67", "HLADRA"], 1),
    (["DNA1", "Mystery1", "Mystery2", "Mystery3", "Mystery4", "Mystery5", "Mystery6",
      "Mystery7", "Mystery8", "Mystery9", "Mystery10", "Mystery11"], 0),
    (["CD3E", "CD8a", "Ecadherin", "panCK", "Ki67"], 2),
])
def test_a_panel_of_too_few_lineages_is_skipped(panel, covers):
    values, pops = _table(panel=panel)
    flagged, record = multibright.detect(values, quantile=0.97, min_lineages=3)
    assert record["skipped"] and record["method"] == "skipped"
    assert record["panel_lineages"] == covers
    assert f"covers {covers} exclusive lineage" in record["why"] and "needs 3" in record["why"]
    assert not flagged.any()


# -- (d) the calls: warn under standard, exclude under strict ----------------------


class _Schema:
    cell_id = "cell_id"
    x = "x"
    y = "y"


class _Table:
    available = True
    log_transformed = True
    expression_fingerprint = None
    metadata_columns = ("cell_id", "x", "y")

    def __init__(self, values, readable=True):
        self._values = values
        self.markers = list(values)
        self.readable = readable
        n = next(iter(values.values())).size
        self._geometry = pl.DataFrame({"cell_id": np.arange(1, n + 1),
                                       "x": np.arange(n, dtype=np.float64),
                                       "y": np.zeros(n)})

    def geometry(self):
        return self._geometry

    def columns(self, names):
        if not self.readable:
            raise RuntimeError("no expression values")
        return {m: self._values[m] for m in names if m in self._values}


class _Ds:
    name = "multibright"
    schema = _Schema()

    def __init__(self, values, readable=True):
        self.table = _Table(values, readable)


def _empty_pairs():
    return pl.DataFrame(schema={"cell_id": pl.Int64, "roi_id": pl.Utf8,
                                "fraction": pl.Float32, "method": pl.Utf8})


def _derive(ds, preset, **kwargs):
    result = {"result_id": "r1", "candidates": {}}
    return calls.derive(ds, result, strictness.thresholds(preset), pairs=_empty_pairs(),
                        **kwargs)


def test_the_preset_moves_warn_to_exclude_without_reading_anything_new():
    values, pops = _table()
    ds = _Ds(values)
    reason = schemas.MULTI_BRIGHT_REASON
    frames = {}
    for preset in ("standard", "strict"):
        frame, _pairs, summary = _derive(ds, preset)
        frames[preset] = frame
        record = summary["multi_channel_bright"]
        assert record["method"] == "graph"
        assert summary["n_multi_channel_bright"] == record["n_flagged"] > 0
        ev = summary["evidence"][reason]
        assert ev["tool"] == "multi_channel_bright" and ev["channels"]
        assert ev["cutoffs"]["quantile"] == 0.97 and ev["cutoffs"]["min_lineages"] == 3
        assert summary["by_category"]["staining_signal"]["n_any"] == record["n_flagged"]
    standard, strict = frames["standard"], frames["strict"]
    hit = standard["reasons"].list.contains(reason).to_numpy()
    assert np.array_equal(hit, strict["reasons"].list.contains(reason).to_numpy())
    assert hit[pops["af"]].mean() >= 0.95
    # Standard: warned, the cell passes. Strict: excluded.
    assert set(standard.filter(pl.Series(hit))["action"].to_list()) == {"warn"}
    assert standard.filter(pl.Series(hit))["pass"].all()
    assert set(strict.filter(pl.Series(hit))["action"].to_list()) == {"exclude"}
    assert set(strict.filter(pl.Series(hit))["primary_reason"].to_list()) == {reason}


def test_a_category_switched_off_or_a_dismissal_leaves_the_cells_in():
    values, _pops = _table()
    ds = _Ds(values)
    reason = schemas.MULTI_BRIGHT_REASON
    frame, _p, summary = _derive(ds, "strict", disabled=("staining_signal",))
    hit = frame["reasons"].list.contains(reason)
    assert hit.any() and frame.filter(hit)["pass"].all()
    assert summary["note_by_reason"][reason] == int(hit.sum())
    result = {"result_id": "r1", "candidates": {},
              "user_dismissed": [{"finding": "cell_reason", "reason": reason, "by": "user"}]}
    frame, _p, summary = calls.derive(ds, result, strictness.thresholds("strict"),
                                      pairs=_empty_pairs())
    assert not frame["reasons"].list.contains(reason).any()
    assert summary["evidence"][reason]["dismissed"]


def test_no_expression_values_skips_the_check():
    values, _pops = _table()
    frame, _p, summary = _derive(_Ds(values, readable=False), "strict")
    record = summary["multi_channel_bright"]
    assert record["skipped"] and "no expression values" in record["why"]
    assert summary["n_multi_channel_bright"] == 0
    assert schemas.MULTI_BRIGHT_REASON not in summary["evidence"]


def test_a_skipped_panel_says_why_in_the_calls():
    values, _pops = _table(panel=["DNA1", "CD3E", "CD8a", "CD69", "TCF1", "Ki67"])
    _frame, _p, summary = _derive(_Ds(values), "strict")
    record = summary["multi_channel_bright"]
    assert record["skipped"] and "covers 1 exclusive lineage; needs 3" in record["why"]
    assert summary["n_multi_channel_bright"] == 0


def test_the_presets_stay_nested():
    lenient, standard, strict = (strictness.thresholds(p)
                                 for p in ("lenient", "standard", "strict"))
    assert lenient["cells.multi_bright_quantile"] >= standard["cells.multi_bright_quantile"] \
        >= strict["cells.multi_bright_quantile"]
    assert lenient["cells.multi_bright_lineages"] >= standard["cells.multi_bright_lineages"] \
        >= strict["cells.multi_bright_lineages"]
    assert (lenient["cells.multi_bright_exclude"], standard["cells.multi_bright_exclude"],
            strict["cells.multi_bright_exclude"]) == (0, 0, 1)
    values, _pops = _table()
    flagged = {p: multibright.detect(values, quantile=t["cells.multi_bright_quantile"],
                                     min_lineages=t["cells.multi_bright_lineages"])[0]
               for p, t in (("lenient", lenient), ("standard", standard), ("strict", strict))}
    assert not (flagged["lenient"] & ~flagged["standard"]).any()
    assert not (flagged["standard"] & ~flagged["strict"]).any()
