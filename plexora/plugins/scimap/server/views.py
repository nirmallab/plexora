"""The views of a step's result: every way the page can draw what one run stored.

A step stores its result once (a column, a table in `uns`); a view is one
`scimappro.pl` call that reads it. Changing the view, or narrowing it to some
images or kinds, calls a different function or the same one with different
arguments -- never the analysis again. So nothing here touches the working
file: the settings are kept on the cell as `views` (display only, they never
mark a result outdated), and each drawn view is cached by run and settings.

One vocabulary of controls serves every tool, so a control means the same thing
on every plot and the sidebar lays them out in one order: View, Data,
Statistic, Order, Style, Export (`SECTIONS`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

SECTIONS = ("data", "statistic", "order", "style")

#: Most panels one step shows side by side.
MAX_PANELS = 3

#: Points a tissue map sends to the page; Scimap Pro samples the rest out.
TISSUE_POINTS = 40_000

_NUCLEAR = re.compile(r"^(dna|dapi|hoechst|nuclear|nucleus|h3|hh3|draq\d*|sytox)\d*$", re.I)


@dataclass(frozen=True)
class View:
    id: str
    name: str
    hint: str
    icon: str
    #: `scimappro.pl` function, or None for a view the page draws itself.
    fn: str | None
    kw: Callable | None = None
    title: Callable | str = ""
    controls: tuple = ()
    #: The one or two controls that sit in the plot's own bar.
    quick: tuple = ()
    defaults: dict = field(default_factory=dict)
    #: plot | image (matplotlib only, sent as a picture) | compute (needs a
    #: computation the step did not make) | table | embedding (page-drawn).
    kind: str = "plot"
    #: What a click on a mark means: the role of each axis (see `_roles`).
    axes: dict = field(default_factory=dict)
    #: Extra drill-down entries: `{label, view, set: {opt: axis}}`.
    actions: tuple = ()
    #: Offered only when this holds for the step.
    when: Callable | None = None
    #: A few words on what Compute costs, for compute views.
    cost: str = ""


@dataclass
class Ctx:
    """What a view needs to know about one step and the working file."""

    tool: str
    label: str | None
    source: str | None
    image: str
    images: list
    markers: list
    columns: dict
    layer: str | None
    log: bool | None
    x: str
    y: str
    layers: list = field(default_factory=list)
    color: str | None = None
    group: str | None = None
    splits: list = field(default_factory=list)

    @property
    def many(self) -> bool:
        return len(self.images) > 1

    def categories(self, column) -> list:
        return list((self.columns.get(column) or {}).get("categories") or [])

    @property
    def groups(self) -> list:
        return self.categories(self.group)

    @property
    def kinds(self) -> list:
        return self.categories(self.source)


def context(tool_id, eff, work, display=None) -> Ctx:
    label = eff.get("label")
    source = (eff.get("inputs") or {}).get("source")
    image = work.get("image_column") or "imageid"
    columns = {c["name"]: c for c in work.get("columns") or [] if c.get("kind") == "category"}
    raw = work.get("raw", True)
    c = Ctx(tool=tool_id, label=label, source=source, image=image,
            images=[str(i) for i in work.get("images") or []],
            markers=[m for m in work.get("markers") or [] if not _NUCLEAR.match(str(m))],
            columns=columns,
            layer="raw" if raw else "X",
            log=False if raw is False and work.get("x_logged") else None,
            x=work.get("x_column") or "X_centroid", y=work.get("y_column") or "Y_centroid",
            layers=[str(v) for v in (work.get("layers") or (["raw", "X"] if raw else ["X"])) if v not in (None, "None", "")],
            color=(display or {}).get("color"))
    c.group = label if FAMILY.get(tool_id) in ("groups", "neighborhoods", "nbh_labels") else source
    if tool_id == "umap":
        c.group = c.color
    # A sample column: a few values, more than one, not a step's own output.
    own = {label, source, image}
    n_images = max(2, len(c.images))
    c.splits = [name for name, info in columns.items()
                if name not in own and 1 < int(info.get("n") or 0) <= n_images]
    return c


# -- controls ---------------------------------------------------------------


def _seg(*pairs):
    return [{"value": v, "label": t} for v, t in pairs]


def controls(c: Ctx) -> dict:
    """Every control, resolved for this step: its label, section, type and
    choices. A view lists the ones it uses."""
    group_noun = {"groups": "groups", "markers": "groups", "neighborhoods": "neighborhoods", "nbh_labels": "neighborhoods",
                  "embedding": "groups"}.get(FAMILY.get(c.tool), "kinds")
    return {
        "images": {"label": "Images", "section": "data", "type": "multi", "choices": c.images, "noun": "images"},
        "image": {"label": "Image", "section": "data", "type": "one", "choices": c.images},
        "groups": {"label": group_noun.capitalize() if group_noun != "kinds" else "Cell kinds",
                   "section": "data", "type": "multi", "choices": c.groups, "noun": group_noun},
        "from": {"label": "From", "section": "data", "type": "multi", "choices": c.kinds, "noun": "kinds"},
        "to": {"label": "To", "section": "data", "type": "multi", "choices": c.kinds, "noun": "kinds"},
        "target": {"label": "Distance to", "section": "data", "type": "one", "choices": c.kinds},
        "markers": {"label": "Markers", "section": "data", "type": "multi", "choices": c.markers, "noun": "markers"},
        "markerA": {"label": "Marker", "section": "data", "type": "one", "choices": c.markers},
        "markerB": {"label": "Against", "section": "data", "type": "one", "choices": c.markers},
        "split": {"label": "Split by", "section": "data", "type": "one",
                  "choices": [""] + ([c.image] if c.many else []) + c.splits, "labels": {"": "Nothing"}},
        "groupby": {"label": "Group by", "section": "data", "type": "one", "choices": c.splits},
        "layer": {"label": "Layer", "section": "data", "type": "one", "choices": c.layers,
                  "labels": {"raw": "Raw intensities", "X": "Main matrix (X)"},
                  "note": "Which expression values the plot reads."},
        "log": {"label": "Log", "section": "statistic", "type": "toggle",
                "note": "log1p of each cell's value before the means are taken. Leave off for a matrix already logged."},
        "averaged": {"label": "Average", "section": "statistic", "type": "segment",
                     "choices": _seg(("sample", "Images"), ("cell", "Cells")),
                     "note": "Images: a median per image, then the mean of those. Cells: every cell pooled."},
        "scale": {"label": "Scale", "section": "statistic", "type": "segment",
                  "choices": _seg(("linear", "Linear"), ("log", "Log"))},
        "standard": {"label": "Color scale", "section": "statistic", "type": "segment",
                     "choices": _seg(("column", "Marker"), ("row", "Group"), ("none", "None")),
                     "note": "Each marker, or each group, scaled to its own range; or the means as they are."},
        "dotscale": {"label": "Color scale", "section": "statistic", "type": "segment",
                     "choices": _seg(("marker", "Marker"), ("group", "Group"))},
        "bars": {"label": "Values", "section": "statistic", "type": "segment",
                 "choices": _seg(("percent", "Share"), ("absolute", "Count")),
                 "note": "With some left out, the others still add to 100%."},
        "prevalence": {"label": "% expressing", "section": "statistic", "type": "toggle",
                       "note": "Adds the share of cells above each marker's threshold."},
        "order": {"label": "Order", "section": "order", "type": "segment",
                  "choices": _seg(("name", "By name"), ("cluster", "Clustered"))},
        "horizontal": {"label": "Orientation", "section": "style", "type": "segment",
                       "choices": _seg(("v", "Vertical"), ("h", "Horizontal"))},
        "points": {"label": "Point size", "section": "style", "type": "range",
                   "min": 0.4, "max": 4, "step": 0.1, "unit": "×"},
        "top": {"label": "Markers each", "section": "data", "type": "range", "min": 3, "max": 15, "step": 1,
                "unit": ""},
        "feature": {"label": "Cell type", "section": "data", "type": "one", "choices": c.kinds},
        "group": {"label": "Group", "section": "data", "type": "one", "choices": c.groups},
        "rank": {"label": "Rank by", "section": "order", "type": "segment",
                 "choices": _seg(("score", "Score"), ("logFC", "Fold change"))},
    }


# -- the views of each kind of step -------------------------------------------


def _clean(o, key):
    value = o.get(key)
    if value in (None, "", []):
        return None
    return value


def _cluster(o, default):
    return (o.get("order") or default) == "cluster"


def _one(c, o, key="image"):
    value = _clean(o, key)
    if value in c.images:
        return value
    return c.images[0] if c.images else None


def _scatter(c, o, color):
    image = _one(c, o)
    return dict(colorBy=[color], subset=[image] if image else None, maxPoints=TISSUE_POINTS,
                xCoordinate=c.x, yCoordinate=c.y, imageId=c.image)


def _layer(c, o, default):
    """The layer a view reads: the one chosen if this file has it."""
    chosen = o.get("layer")
    return chosen if chosen in c.layers else default


def _heatmap(c, o):
    standard = o.get("standard") or "column"
    layer = _layer(c, o, "X")
    # Scimap Pro's heatmap has no `log`: figures._call draws it on a log1p
    # copy of the layer, made in memory (`_log`).
    return dict(groupBy=c.group, standardScale=None if standard == "none" else standard,
                subsetGroups=_clean(o, "groups"), subsetMarkers=_clean(o, "markers"),
                showPrevalence=bool(o.get("prevalence")) or None,
                clusterRows=_cluster(o, "cluster"), clusterColumns=_cluster(o, "cluster"),
                layer=None if layer == "X" else layer, _log=True if o.get("log") else None)


def _dotplot(c, o):
    layer = _layer(c, o, c.layer)
    log = o.get("log")
    return dict(markers=_clean(o, "markers") or c.markers, groupBy=c.group, imageId=c.image,
                layer=layer, log=bool(log) if log is not None else (layer == "raw"), subset=_clean(o, "images"),
                standardScale=o.get("dotscale") or "marker")


def _violin(c, o):
    keys = (_clean(o, "markers") or c.markers[:3])[:6]
    layer = _layer(c, o, c.layer)
    log = o.get("log")
    if log is None and o.get("scale"):
        log = o.get("scale") == "log"  # kept settings from before the Log switch
    return dict(keys=keys, groupBy=c.group, layer=layer, imageId=c.image,
                log=bool(log) if log is not None else layer == "raw", subset=_clean(o, "images"))


def _sizes(c, o):
    return dict(xAxis=c.group, yAxis=c.image, method=o.get("bars") or "absolute",
                subsetXAxis=_clean(o, "groups"), subsetYAxis=_clean(o, "images"),
                horizontal=o.get("horizontal") == "h" or None)


def _noun(c):
    return "neighborhood" if FAMILY.get(c.tool) in ("neighborhoods", "nbh_labels") else "group"


def _bars_title(o, share, count):
    return share if (o.get("bars") or "percent") == "percent" else count


HEATMAP = View("heatmap", "Heatmap", "Mean of each marker in each group", "grid", "heatmap", _heatmap,
               "Mean expression by group",
               controls=("groups", "markers", "layer", "log", "standard", "prevalence", "order"),
               defaults={"order": "cluster", "layer": "X", "log": False},
               axes={"x": "marker", "y": "group"},
               actions=({"label": "{x} as violins across groups", "view": "violin", "set": {"markers": "x"}},))
DOTPLOT = View("dotplot", "Dot plot", "Mean and the share of cells expressing", "dots", "dotplot", _dotplot,
               "Expression and prevalence", controls=("markers", "images", "layer", "log", "dotscale", "points"),
               defaults={"layer": lambda c: c.layer, "log": lambda c: c.layer == "raw", "points": 1.0},
               axes={"x": "marker", "y": "group"},
               actions=({"label": "{x} as violins across groups", "view": "violin", "set": {"markers": "x"}},))
VIOLIN = View("violin", "Violin", "The spread of a marker in each group", "violin", "violin", _violin,
              lambda c, o: "Spread of " + ", ".join(_violin(c, o)["keys"]) + " by group",
              controls=("markers", "images", "layer", "log"), quick=("markers",), axes={"x": "group"},
              defaults={"layer": lambda c: c.layer, "log": lambda c: c.layer == "raw",
                        "markers": lambda c: c.markers[:3]})


def _tissue(name="Tissue map", hint="Where each group is"):
    return View("tissue", name, hint, "map", "spatialScatterPlot",
                lambda c, o: _scatter(c, o, c.group),
                lambda c, o: "Cells in " + (_one(c, o) or "the tissue") + (f", colored by {c.group}" if c.group else ""),
                controls=("image", "points"), quick=("image",), defaults={"points": 1.0},
                when=lambda c: bool(c.group) and bool(c.images))


SIZES = View("sizes", "Sizes", "How many cells each group holds, by image", "columns", "barplot", _sizes,
             lambda c, o: _bars_title(o | {"bars": o.get("bars") or "absolute"},
                                      f"Share of cells per {_noun(c)}", f"Cells per {_noun(c)}") + (", by image" if c.many else ""),
             controls=("groups", "images", "bars", "horizontal"), defaults={"bars": "absolute"},
             axes={"x": "group", "series": "image"})


def _composition(method):
    return lambda c, o: dict(xAxis=c.image, yAxis=c.source, method=method,
                             subsetXAxis=_clean(o, "images"), subsetYAxis=_clean(o, "groups"),
                             horizontal=o.get("horizontal") == "h" or None)


def _distance(kind):
    def kw(c, o):
        images = [_one(c, o)] if kind == "one" else _clean(o, "images")
        out = dict(kind="distribution" if kind == "distribution" else "heatmap", label=c.label,
                   phenotype=c.source, imageId=c.image, unit=o.get("averaged") or "sample",
                   log=(o.get("scale") or "linear") == "log", distanceFrom=_clean(o, "from"),
                   distanceTo=_clean(o, "to"),
                   subsetCol=c.image if images else None, subsetValue=images)
        if kind == "distribution":
            out["facetBy"] = _clean(o, "split")
            out.pop("unit")
        else:
            out["clusterRows"] = out["clusterColumns"] = _cluster(o, "name")
        return out
    return kw


def _distance_title(c, o):
    stat = "Mean" if (o.get("averaged") or "sample") == "cell" else "Median"
    return f"{stat} distance to the nearest cell of each kind"


def _cooccurrence(**fixed):
    def kw(c, o):
        out = dict(kind="heatmap", label=c.label, subsetPhenotype=_clean(o, "from"),
                   subsetNeighbourPhenotype=_clean(o, "to"),
                   clusterRows=_cluster(o, "name"), clusterColumns=_cluster(o, "name"))
        out.update(fixed)
        if out["kind"] != "heatmap":
            out.pop("clusterRows")
            out.pop("clusterColumns")
        return out
    return kw


def _radii(c, o):
    return dict(kind="scale", label="spatialColocalization",
                subsetPhenotype=_clean(o, "from"), subsetNeighbourPhenotype=_clean(o, "to"))


def _has_splits(c):
    return bool(c.splits)


PAIR = {"x": "to", "y": "from"}
TABLE = View("table", "Table", "The numbers, sortable", "table", None, kind="table")

CATALOGUE = {
    "groups": (
        HEATMAP, DOTPLOT, VIOLIN, _tissue(), SIZES,
    ),
    "embedding": (
        # Drawn by the page from every cell (scimapEmbed.js); downloads as
        # Scimap Pro's pl.umap.
        View("embedding", "Embedding", "Every cell on the UMAP", "scatter", "umap",
             lambda c, o: dict(colorBy=c.color, basis=c.label),
             lambda c, o: "UMAP" + (f" colored by {c.color}" if c.color else ""), kind="embedding"),
        _tissue("Tissue map", "The same colors, in the tissue"),
    ),
    "composition": (
        View("stacked", "Stacked share", "Share of each kind in each image", "stacked", "barplot",
             _composition("percent"), lambda c, o: "Share of cells" + (" per image" if c.many else ""),
             controls=("images", "groups", "horizontal"), axes={"x": "image", "series": "group"}),
        View("counts", "Counts", "Cells of each kind in each image", "columns", "barplot",
             _composition("absolute"), lambda c, o: "Cells of each kind" + (" per image" if c.many else ""),
             controls=("images", "groups", "horizontal"), axes={"x": "image", "series": "group"}),
        View("where", "Where each kind is", "Which images each kind comes from", "hbars", "barplot",
             lambda c, o: dict(xAxis=c.source, yAxis=c.image, method="percent",
                               subsetXAxis=_clean(o, "groups"), subsetYAxis=_clean(o, "images"),
                               horizontal=o.get("horizontal") == "h" or None),
             "Where each kind of cell is", controls=("groups", "images", "horizontal"),
             axes={"x": "group", "series": "image"}, when=lambda c: c.many),
        View("grouped", "Grouped by a sample column", "Images side by side within each group of samples",
             "grouped", "barplot",
             lambda c, o: dict(xAxis=c.image, yAxis=c.source, method="percent",
                               groupBy=_clean(o, "groupby") or (c.splits[0] if c.splits else None),
                               subsetYAxis=_clean(o, "groups")),
             lambda c, o: "Share of cells, grouped by " + (_clean(o, "groupby") or (c.splits[0] if c.splits else "")),
             controls=("groupby", "groups"), quick=("groupby",), axes={"x": "image", "series": "group"},
             when=_has_splits),
        View("similarity", "Sample similarity", "How alike the images' compositions are", "grid",
             "correlation",
             lambda c, o: dict(by="group", groupBy=c.image, condition=c.source, normalize=True, layer=None,
                               subsetGroups=_clean(o, "images"),
                               clusterRows=_cluster(o, "cluster"), clusterColumns=_cluster(o, "cluster")),
             "Correlation of the images' compositions", controls=("images", "order"), defaults={"order": "cluster"},
             when=lambda c: len(c.images) > 2),
        View("pies", "Pies", "One pie per image", "pie", "pie",
             lambda c, o: dict(phenotype=c.source, groupBy=c.image, subsetPhenotype=_clean(o, "groups"),
                               subsetGroupBy=_clean(o, "images")),
             lambda c, o: "Share of cells" + (" per image" if c.many else ""),
             controls=("images", "groups"), kind="image"),
        TABLE,
    ),
    "expression": (
        HEATMAP, DOTPLOT, VIOLIN,
        View("correlation", "Marker correlation", "Which markers rise and fall together", "grid", "correlation",
             lambda c, o: dict(by="marker", layer=(lambda v: None if v == "X" else v)(_layer(c, o, c.layer)),
                               subsetMarkers=_clean(o, "markers") or c.markers,
                               clusterRows=_cluster(o, "cluster"), clusterColumns=_cluster(o, "cluster"),
                               _log=True if o.get("log") else None),
             "Correlation between markers", controls=("markers", "layer", "log", "order"),
             axes={"x": "marker", "y": "marker"},
             defaults={"order": "cluster", "layer": lambda c: c.layer, "log": False},
             actions=({"label": "{y} against {x}", "view": "two", "set": {"markerA": "y", "markerB": "x"}},)),
        View("two", "Two markers", "The density of one marker against another", "density", "densityPlot2D",
             lambda c, o: dict(markerA=_clean(o, "markerA") or (c.markers[0] if c.markers else None),
                               markerB=_clean(o, "markerB") or (c.markers[1] if len(c.markers) > 1 else None),
                               layer=(lambda v: None if v == "X" else v)(_layer(c, o, c.layer)),
                               subset=_clean(o, "images"), imageId=c.image, _log=True if o.get("log") else None),
             lambda c, o: "{} against {}".format(_clean(o, "markerA") or (c.markers[0] if c.markers else ""),
                                                 _clean(o, "markerB") or (c.markers[1] if len(c.markers) > 1 else "")),
             controls=("markerA", "markerB", "images", "layer", "log"), quick=("markerA", "markerB"),
             defaults={"layer": lambda c: c.layer, "log": False}, when=lambda c: len(c.markers) > 1),
        View("histograms", "Histograms", "Each marker's distribution over all cells", "hist", "distPlot",
             lambda c, o: dict(markers=(_clean(o, "markers") or c.markers[:6]),
                               layer=(lambda v: None if v == "X" else v)(_layer(c, o, c.layer)),
                               subset=_clean(o, "images"), imageId=c.image, _log=True if o.get("log") else None),
             "Distribution of each marker", controls=("markers", "images", "layer", "log"), quick=("markers",),
             defaults={"markers": lambda c: c.markers[:6], "layer": lambda c: c.layer, "log": False}),
    ),
    "distance": (
        View("matrix", "Matrix", "Every pair at once", "grid", "spatialDistance", _distance("matrix"),
             _distance_title, controls=("images", "from", "to", "averaged", "scale", "order"),
             quick=("images",), axes=PAIR,
             actions=({"label": "Distribution for this pair", "view": "distribution", "set": {"from": "y", "to": "x"}},
                      {"label": "Only from {y}, to every kind", "set": {"from": "y", "to": None}},
                      {"label": "{y} by distance to {x}, in the tissue", "view": "tissue", "set": {"target": "x"}})),
        View("distribution", "Distribution", "The spread behind a median", "violin", "spatialDistance",
             _distance("distribution"),
             lambda c, o: "Distance from each cell" + (f" of {', '.join(o['from'])}" if _clean(o, "from") else "")
             + (f" to the nearest {', '.join(o['to'])}" if _clean(o, "to") else " to the nearest cell of each kind"),
             controls=("from", "to", "split", "images", "scale"), quick=("from", "to", "split"),
             axes={"x": "to"}),
        View("byimage", "By image", "One image's matrix at a time", "grid", "spatialDistance", _distance("one"),
             lambda c, o: f"Median distance in {_one(c, o)}", controls=("image", "from", "to", "scale", "order"),
             quick=("image",), axes=PAIR, when=lambda c: c.many,
             actions=({"label": "Distribution for this pair", "view": "distribution", "set": {"from": "y", "to": "x"}},)),
        View("tissue", "Tissue map", "Cells colored by distance to one kind", "map", "spatialScatterPlot",
             lambda c, o: _scatter(c, o, f"{c.label}:{_clean(o, 'target') or (c.kinds[0] if c.kinds else '')}"),
             lambda c, o: f"Distance to the nearest {_clean(o, 'target') or (c.kinds[0] if c.kinds else '')}, in {_one(c, o)}",
             controls=("target", "image", "points"), quick=("target", "image"), defaults={"points": 1.0}),
    ),
    "interaction": (
        View("matrix", "Matrix", "Every pair against chance", "grid", "spatialCooccurrence", _cooccurrence(),
             "Neighbors against chance", controls=("from", "to", "order"), axes=PAIR,
             actions=({"label": "{y} and {x} in each image", "view": "byimage", "set": {"from": "y", "to": "x"}},
                      {"label": "Only from {y}, to every kind", "set": {"from": "y", "to": None}})),
        View("network", "Network", "The pairs that stand out, as a graph", "network", "spatialCooccurrence",
             _cooccurrence(kind="network"), "Pairs that are neighbors more or less than chance",
             controls=("from", "to")),
        View("byimage", "By image", "The scores of each image", "grid", "spatialCooccurrence",
             _cooccurrence(summarizePlot=False), "Neighbors against chance, in each image",
             controls=("from", "to", "order"), axes={"x": "to", "y": "from"}, when=lambda c: c.many),
        View("binary", "More or less only", "Only whether a pair is together more or less than chance",
             "grid", "spatialCooccurrence", _cooccurrence(binaryView=True), "More or less than chance",
             controls=("from", "to", "order"), axes=PAIR),
        View("radii", "Across radii", "The pairs scored at 20, 50, 100 and 200 px", "radii", "spatialCooccurrence",
             _radii, "Neighbors against chance, across radii", controls=("from", "to"), kind="compute",
             cost="a few seconds"),
    ),
    "neighborhoods": (
        View("holds", "What each holds", "The kinds of cell in each neighborhood", "stacked", "barplot",
             lambda c, o: dict(xAxis=c.group, yAxis=c.source, method="percent",
                               subsetXAxis=_clean(o, "groups"), horizontal=o.get("horizontal") == "h" or None),
             "What each neighborhood holds", controls=("groups", "horizontal"),
             axes={"x": "group", "series": "kind"}, when=lambda c: bool(c.source)),
        _tissue("Tissue map", "Where each neighborhood is"),
        View("markers", "Marker heatmap", "Mean of each marker in each neighborhood", "grid", "heatmap", _heatmap,
             "Mean expression by neighborhood", controls=("groups", "markers", "layer", "log", "standard", "order"),
             axes={"x": "marker", "y": "group"}, defaults={"order": "cluster", "layer": "X", "log": False}),
        View(SIZES.id, "Sizes by image", SIZES.hint, SIZES.icon, SIZES.fn, SIZES.kw, SIZES.title,
             controls=SIZES.controls, axes=SIZES.axes, defaults=SIZES.defaults),
        View("voronoi", "Voronoi tiles", "Each cell's tile, colored by neighborhood", "voronoi", "voronoi",
             lambda c, o: dict(colorBy=c.group, subset=[_one(c, o)], xCoordinate=c.x, yCoordinate=c.y,
                               imageId=c.image),
             lambda c, o: f"Neighborhoods in {_one(c, o)}", controls=("image",), quick=("image",), kind="image",
             when=lambda c: bool(c.images)),
    ),
}
# -- the steps added for a first analysis -------------------------------------

def _pick_group(c, o):
    value = _clean(o, "group")
    return value if value in c.groups else (c.groups[0] if c.groups else None)


def _hist(c, o):
    return dict(markers=(_clean(o, "markers") or c.markers[:6]),
                layer=(lambda v: None if v == "X" else v)(_layer(c, o, c.layer)),
                subset=_clean(o, "images"), imageId=c.image, _log=True if o.get("log") else None)


CATALOGUE["markers"] = (
    # Scimap Pro draws one group at a time (its own menu switches them); the
    # page's Group picker is that menu.
    View("ranked", "Top markers", "The markers that set one group apart", "hbars", "rankFeatures",
         lambda c, o: dict(label=c.label, groups=[_pick_group(c, o)] if _pick_group(c, o) else None,
                           nFeatures=int(o.get("top") or 5),
                           sortBy=o.get("rank") if o.get("rank") in ("score", "logFC") else "score"),
         lambda c, o: f"Top markers of {_pick_group(c, o)}" if _pick_group(c, o) else "Top markers",
         controls=("group", "top", "rank"), quick=("group",),
         defaults={"top": 5, "rank": "score"}),
    HEATMAP, DOTPLOT, TABLE,
)
CATALOGUE["compare"] = (
    View("forest", "Differences", "Each cell type's difference between the groups, with its interval",
         "forest", "compareGroups", lambda c, o: dict(label=c.label, kind="forest"),
         "Difference in the share of each cell type"),
    View("samples", "Samples", "The samples behind one cell type", "dots", "compareGroups",
         lambda c, o: dict(label=c.label, kind="samples", feature=_clean(o, "feature")),
         lambda c, o: "Share of " + (_clean(o, "feature") or "the top cell type") + " in each sample",
         controls=("feature",), quick=("feature",)),
    TABLE,
)
CATALOGUE["quality"] = (
    View("perimage", "By image", "How many cells were flagged in each image", "columns", "barplot",
         lambda c, o: dict(xAxis=c.image, yAxis=c.label, method=o.get("bars") or "percent",
                           subsetXAxis=_clean(o, "images")),
         lambda c, o: "Flagged cells per image", controls=("images", "bars"), defaults={"bars": "percent"}),
    View("tissue", "Tissue map", "Where the flagged cells are", "map", "spatialScatterPlot",
         lambda c, o: _scatter(c, o, c.label),
         lambda c, o: "Flagged cells in " + (_one(c, o) or "the tissue"),
         controls=("image", "points"), quick=("image",), defaults={"points": 1.0},
         when=lambda c: bool(c.images)),
    View("histograms", "Marker histograms", "Each marker's spread: look for a clear positive peak",
         "hist", "distPlot", _hist, "Distribution of each marker",
         controls=("markers", "images", "layer", "log"), quick=("markers",),
         defaults={"markers": lambda c: c.markers[:6], "layer": lambda c: c.layer, "log": False}),
    TABLE,
)

CATALOGUE["normalized"] = (
    View("histograms", "Histograms", "Each gene's spread after normalizing", "hist", "distPlot", _hist,
         "Distribution of each gene, normalized and logged",
         controls=("markers", "images", "layer", "log"), quick=("markers",),
         defaults={"markers": lambda c: c.markers[:6], "layer": "log", "log": False}),
)
CATALOGUE["graph"] = (
    View("pca", "Components", "The cells on their first principal components, with the variance each explains",
         "scatter", "pca", lambda c, o: dict(useRep="X_pca", showVariance=True, maxPoints=TISSUE_POINTS),
         "Principal components"),
)
CATALOGUE["svg"] = (
    TABLE,
    View("tissue", "Tissue map", "Where one gene is expressed", "map", "spatialScatterPlot",
         lambda c, o: {**_scatter(c, o, _clean(o, "markerA") or (c.markers[0] if c.markers else None)),
                       "layer": "log"},
         lambda c, o: (_clean(o, "markerA") or "A gene") + " in " + (_one(c, o) or "the tissue"),
         controls=("markerA", "image", "points"), quick=("markerA",), defaults={"points": 1.0},
         when=lambda c: bool(c.markers)),
)

# A renamed neighborhood column has no phenotype beside it: no "What each
# holds", and Sizes (its default) first.
CATALOGUE["nbh_labels"] = tuple(sorted((v for v in CATALOGUE["neighborhoods"] if v.id != "holds"),
                                       key=lambda v: v.id != "sizes"))

FAMILY = {
    "cluster": "groups", "name_groups": "groups", "celltypes": "groups",
    "umap": "embedding", "composition": "composition", "expression": "expression",
    "distance": "distance", "interaction": "interaction", "neighborhoods": "neighborhoods",
    "markers": "markers", "compare": "compare", "quality": "quality",
    "normalize": "normalized", "pca": "graph", "svg": "svg",
}

#: What a step opens on: one panel per entry, the first view of each.
DEFAULTS = {
    "groups": ("heatmap", "sizes"),
    "embedding": ("embedding",),
    "composition": ("stacked", "table"),
    "expression": ("heatmap", "dotplot"),
    "distance": ("matrix",),
    "interaction": ("matrix",),
    "neighborhoods": ("holds", "sizes"),
    "nbh_labels": ("sizes",),
    "markers": ("ranked",),
    "compare": ("forest", "table"),
    "quality": ("perimage", "tissue"),
    "normalized": ("histograms",),
    "graph": ("pca",),
    "svg": ("table", "tissue"),
}


def offered(tool_id, c: Ctx | None = None) -> list[View]:
    views = list(CATALOGUE.get(FAMILY.get(tool_id), ()))
    if c is not None:
        views = [v for v in views if v.when is None or v.when(c)]
    return views


def find(tool_id, view_id, c: Ctx | None = None) -> View | None:
    return next((v for v in offered(tool_id, c) if v.id == view_id), None)


def default_panels(tool_id, c: Ctx | None = None) -> list[dict]:
    ids = {v.id for v in offered(tool_id, c)}
    # A default panel is named after its view, so `figures/heatmap.png`
    # keeps naming the same figure.
    return [{"id": view, "view": view, "opts": {}}
            for view in DEFAULTS.get(FAMILY.get(tool_id), ()) if view in ids]


# -- settings kept on the cell -------------------------------------------------


_KEYS = {"images", "image", "groups", "from", "to", "target", "markers", "markerA", "markerB", "split",
         "groupby", "averaged", "scale", "standard", "dotscale", "bars", "prevalence", "order",
         "horizontal", "points", "layer", "log", "top", "feature", "rank", "group"}


def clean_opts(opts) -> dict:
    """Only known keys, only plain values, nothing large: these are display
    settings, stored with the notebook."""
    out = {}
    for key, value in (opts or {}).items():
        if key not in _KEYS:
            continue
        if isinstance(value, list):
            value = [str(v)[:200] for v in value[:500]]
        elif isinstance(value, bool):
            pass
        elif isinstance(value, (int, float)):
            value = float(value)
        elif value is None:
            continue
        else:
            value = str(value)[:200]
        out[key] = value
    return out


def clean_panels(tool_id, panels) -> list[dict]:
    known = {v.id for v in offered(tool_id)}
    out, seen = [], set()
    for panel in panels or []:
        if len(out) >= MAX_PANELS:
            break
        if not isinstance(panel, dict) or panel.get("view") not in known:
            continue
        pid = str(panel.get("id") or "")[:12] or chr(97 + len(out))
        if pid in seen:
            continue
        seen.add(pid)
        out.append({"id": pid, "view": panel["view"], "opts": clean_opts(panel.get("opts"))})
    return out


def panels_of(cell, c: Ctx | None = None) -> list[dict]:
    """The panels a step shows: its kept ones, else the tool's defaults."""
    kept = clean_panels(cell.get("tool"), cell.get("views"))
    ids = {v.id for v in offered(cell.get("tool"), c)}
    kept = [p for p in kept if p["view"] in ids]
    return kept or default_panels(cell.get("tool"), c)


# -- what the page is sent --------------------------------------------------------


def defaults(view: View, c: Ctx) -> dict:
    """A view's defaults for this step (some depend on the matrix)."""
    return {k: (v(c) if callable(v) else v) for k, v in view.defaults.items()}


def spec(tool_id, view: View, c: Ctx, opts) -> dict:
    """`{name, title, fn, kwargs}` for one view with its settings."""
    opts = dict(opts or {})
    # A violin kept from before the Log switch: its Linear / Log scale is the log.
    if view.id == "violin" and opts.get("scale") in ("linear", "log") and "log" not in opts:
        opts["log"] = opts.pop("scale") == "log"
    o = {**defaults(view, c), **opts}
    title = view.title(c, o) if callable(view.title) else (view.title or view.name)
    kwargs = view.kw(c, o) if view.kw else {}
    return {"name": view.id, "title": title, "fn": view.fn, "kwargs": kwargs, "kind": view.kind}


def _roles(c: Ctx) -> dict:
    """What each axis role of a view is: the setting a click narrows, and the
    column the tissue can be colored by."""
    return {
        "group": {"opt": "groups", "column": c.group},
        "kind": {"opt": "groups", "column": c.source},
        "from": {"opt": "from", "column": c.source},
        "to": {"opt": "to", "column": c.source},
        "image": {"opt": "images", "column": None},
        "marker": {"opt": "markers", "column": None},
    }


def catalogue(tool_id, c: Ctx) -> dict:
    """The views this step offers and the controls they use, for the bar, the
    sidebar and the drill-down menu."""
    every = controls(c)
    roles = _roles(c)
    views, used = [], set()
    for v in offered(tool_id, c):
        used |= set(v.controls)
        views.append({
            "id": v.id, "name": v.name, "hint": v.hint, "icon": v.icon, "kind": v.kind, "cost": v.cost,
            "controls": list(v.controls), "quick": list(v.quick), "defaults": defaults(v, c),
            "axes": {axis: {"role": role, **roles.get(role, {})} for axis, role in v.axes.items()},
            "actions": [dict(a) for a in v.actions],
        })
    return {"views": views, "controls": {k: every[k] for k in sorted(used) if k in every},
            "sections": list(SECTIONS), "max_panels": MAX_PANELS,
            "column": c.group, "source": c.source}
