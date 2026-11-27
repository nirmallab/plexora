"""A step's figures, drawn by Scimap Pro itself.

Every plot on the page is one of `scimappro.pl`'s: the same function, the same
ordering, colours and statistics a script gets. For the page it is asked for
`interactive=True` (a plotly figure, sent as JSON and drawn by plotly.js); for a
download the same call without it goes through matplotlib, so a PNG, PDF or SVG
for a paper is the publication figure Scimap Pro makes, not a screenshot.

Figures are built on the runner thread, never beside a run: Scimap Pro's numba
pool is not re-entrant (runner.py). They are cached per run and display.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

from plexora.plugins.scimap.server import views

#: Bump when what a figure looks like changes, so cached figures redraw.
FIGURE_VERSION = 17

FORMATS = {"png": "image/png", "pdf": "application/pdf", "svg": "image/svg+xml"}

FONT = '"Source Sans 3", "Source Sans Pro", -apple-system, system-ui, sans-serif'
INK = "#344054"
MUTED = "#6b7689"
RULE = "#eef0f4"
EDGE = "#e4e7ee"

#: Most values one violin or box sends to the page. plotly draws the shape
#: from the values themselves, so a distance distribution over every cell is
#: megabytes; a fixed sample of each draws the same shape.
SPREAD_POINTS = 2500


def _advanced(eff, spec):
    """The step's Advanced lines for this plotting function, as written."""
    extra = (eff.get("advanced") or {}).get(f"pl.{spec['fn']}") or {}
    spec["kwargs"].update(extra)
    return spec


def plan(tool_id, eff, work, display=None, cell=None) -> list[dict]:
    """The figures a step draws through Scimap Pro: `[{name, title, fn, kwargs,
    view, opts}]`, one per panel the step shows (its kept views, else the
    tool's defaults). `name` is the panel's id. A view the page draws itself
    (a table) has no figure; the UMAP's embedding is drawn by the page but
    downloads as Scimap Pro's `pl.umap`.
    """
    c = views.context(tool_id, eff, work, display)
    full = bool(work.get("columns"))
    out = []
    for panel in views.panels_of(cell or {"tool": tool_id}, c if full else None):
        view = views.find(tool_id, panel["view"])
        if view is None or not view.fn:
            continue
        spec = views.spec(tool_id, view, c, panel["opts"])
        spec.update(name=panel["id"], view=view.id, opts=panel["opts"])
        out.append(_advanced(eff, spec))
    return out


def key(run_id, display=None) -> str:
    digest = hashlib.sha1(json.dumps(display or {}, sort_keys=True).encode()).hexdigest()[:10]
    return f"fig:{run_id}:{FIGURE_VERSION}:{digest}"


def _call(spec, data, **switches):
    from plexora.plugins.scimap.server.adapter import scimappro

    sm = scimappro()
    fn = getattr(sm.pl, spec["fn"])
    kw = {k: v for k, v in spec["kwargs"].items() if v is not None and not k.startswith("_")}
    if spec["kwargs"].get("_log"):
        data = _logged(sm, data, kw.get("layer"))
        kw["layer"] = LOG_LAYER
    return fn(data, show=False, verbose=False, **kw, **switches)


#: The layer a logged view reads, made in memory by Scimap Pro's `pp.log1p`.
LOG_LAYER = "plexora_view_log"
_LOGGED = {}
_LOGGED_LOCK = __import__("threading").Lock()


def _logged(sm, data, layer):
    """The working file in memory with `pp.log1p` of `layer` added as
    LOG_LAYER: what a plot with no `log` of its own draws when the page asks
    for logged values. Scimap Pro computes it; nothing is written back. The
    last one is kept, keyed by the file's stamp, so switching views is quick."""
    import os

    if not isinstance(data, (str, Path)):
        _log1p(sm, data, layer)
        return data
    path = str(data)
    key = (path, os.stat(path).st_mtime_ns, layer)
    with _LOGGED_LOCK:
        if _LOGGED.get("key") == key:
            return _LOGGED["adata"]
        adata = sm.io.init(path, streamData=False, verbose=False)
        _log1p(sm, adata, layer)
        _LOGGED.clear()
        _LOGGED.update(key=key, adata=adata)
        return adata


def _log1p(sm, adata, layer):
    """log1p of `layer` into LOG_LAYER, by whichever Scimap Pro call this
    release has: `pp.transform(method="log1p")` (0.1.29 on) or `pp.log1p`."""
    kw = dict(layer=_source_layer(layer), targetLayer=LOG_LAYER, verbose=False, progress=False)
    if hasattr(sm.pp, "transform"):
        sm.pp.transform(adata, method="log1p", **kw)
    else:
        sm.pp.log1p(adata, **kw)


def _source_layer(layer):
    """`pp.log1p`'s name for a view's matrix: `None` is X."""
    return None if layer in (None, "X") else layer


def _first_line(exc) -> str:
    return (str(exc).splitlines() or [type(exc).__name__])[0][:300]


def draw(tool_id, eff, path, work, view_id, opts=None, display=None, data=None) -> dict:
    """One view with its settings: `{view, title, kind, figure | image | error}`.

    `data` stands in for the working file when a view needs something the
    step did not store (Across radii: `computed`). A view that cannot be
    drawn says why instead of failing the step: the numbers are in the
    summary either way.
    """
    c = views.context(tool_id, eff, work, display)
    view = views.find(tool_id, view_id, c)
    if view is None:
        return {"view": view_id, "title": "", "kind": "plot",
                "error": "This step does not offer that view."}
    spec = _advanced(eff, views.spec(tool_id, view, c, opts))
    entry = {"view": view.id, "title": spec["title"], "fn": view.fn, "kind": view.kind, "call": call_text(spec)}
    if view.kind in ("table", "embedding"):
        return entry
    if view.kind == "compute" and data is None:
        return {**entry, "needs_compute": True, "cost": view.cost}
    try:
        if view.kind == "image":
            entry["image"] = _picture(spec, data if data is not None else path)
        else:
            made = _call(spec, data if data is not None else path, interactive=True, returnFig=True)
            figure = made[0] if isinstance(made, tuple) else made
            raw = json.loads(figure.to_json())
            note = _subtitle(raw)
            fig, thinned = restyle(raw, spec, opts)
            if thinned:
                note = (note + " · " if note else "") + thinned
            entry.update(note=note, figure=fig)
    except Exception as exc:  # noqa: BLE001 -- one figure must not cost the others
        entry["error"] = _first_line(exc)
    return entry


def call_text(spec) -> str:
    """The view as the line a script would write: `sm.pl.<fn>(adata, ...)`."""
    if not spec.get("fn"):
        return ""
    args = ", ".join(f"{k}={v!r}" for k, v in spec["kwargs"].items() if v is not None and not k.startswith("_"))
    line = f"sm.pl.{spec['fn']}(adata{', ' + args if args else ''})"
    if spec["kwargs"].get("_log"):
        line += "  # on log1p of the layer's values, made in memory"
    return line


def _picture(spec, data) -> str:
    """A view Scimap Pro draws only with matplotlib, as a PNG data URL."""
    import base64

    with tempfile.TemporaryDirectory(prefix="scimap-pic-") as folder:
        _call(spec, data, outputDir=folder, fileName="view.png", dpi=120)
        files = sorted(Path(folder).rglob("*.png"))
        if not files:
            raise RuntimeError("Scimap Pro wrote no picture.")
        return "data:image/png;base64," + base64.b64encode(files[0].read_bytes()).decode()


_computed: dict = {}


def computed(run_id, tool_id, eff, path, work):
    """What a compute view reads, made in memory from the working file and kept
    for the last few runs: nothing is written back, so the step's result is
    exactly what its run stored. Today that is Across radii
    (`sp.spatialColocalization`)."""
    if run_id in _computed:
        return _computed[run_id]
    import anndata as ad

    from plexora.plugins.scimap.server.adapter import scimappro

    c = views.context(tool_id, eff, work)
    backed = ad.read_h5ad(path, backed="r")
    try:
        obs = backed.obs[[c.image, c.source, c.x, c.y]].copy()
    finally:
        backed.file.close()
    adata = ad.AnnData(obs=obs)
    made = scimappro().sp.spatialColocalization(
        adata, phenotype=c.source, radii=(20.0, 50.0, 100.0, 200.0), nPerms=100, randomState=0,
        xCoordinate=c.x, yCoordinate=c.y, imageId=c.image, label="spatialColocalization", verbose=False)
    adata = made if made is not None and hasattr(made, "uns") else adata
    while len(_computed) >= 3:
        _computed.pop(next(iter(_computed)))
    _computed[run_id] = adata
    return adata


def build(tool_id, eff, path, work, display=None, cell=None) -> list[dict]:
    """Every panel of a step, drawn: `[{name, view, opts, title, figure...}]`."""
    c = views.context(tool_id, eff, work, display)
    out = []
    for panel in views.panels_of(cell or {"tool": tool_id}, c):
        entry = draw(tool_id, eff, path, work, panel["view"], panel["opts"], display)
        out.append({"name": panel["id"], "opts": panel["opts"], **entry})
    return out


def export(tool_id, eff, path, work, name, fmt, display=None, cell=None, view=None, opts=None,
           data=None) -> tuple[bytes, str]:
    """One figure through matplotlib, as `fmt` bytes and a file name.

    `view` and `opts` name the view as the page shows it now; without them the
    panel `name` of the step's kept views."""
    if fmt not in FORMATS:
        raise ValueError(f"Unknown format {fmt!r}.")
    c = views.context(tool_id, eff, work, display)
    if view:
        found = views.find(tool_id, view, c)
        if found is None or not found.fn:
            raise KeyError(view)
        spec = _advanced(eff, views.spec(tool_id, found, c, opts))
    else:
        spec = next((s for s in plan(tool_id, eff, work, display, cell) if s["name"] == name), None)
        if spec is None:
            raise KeyError(name)
    stem = f"{eff.get('label') or tool_id}_{spec['name']}"
    with tempfile.TemporaryDirectory(prefix="scimap-fig-") as folder:
        _call(spec, data if data is not None else path, outputDir=folder, fileName=f"{stem}.{fmt}", dpi=300)
        files = sorted(Path(folder).rglob(f"*.{fmt}"))
        if not files:
            raise RuntimeError("Scimap Pro wrote no figure.")
        return files[0].read_bytes(), files[0].name


# -- the page's look -------------------------------------------------------


def _subtitle(fig) -> str:
    title = (fig.get("layout") or {}).get("title") or {}
    sub = title.get("subtitle") or {}
    return str(sub.get("text") or "")


def _axis(axis, categorical):
    axis = dict(axis or {})
    if categorical:
        axis["type"] = "category"
        # Upright labels collide in a half-width panel; plotly tilts them
        # only when they do not fit.
        if axis.get("tickangle") in (0, None):
            axis["tickangle"] = "auto"
    title = dict(axis.get("title") or {})
    if title.get("text"):
        title["font"] = {"size": 11, "color": MUTED, "family": FONT}
        title["standoff"] = 8
    axis["title"] = title
    axis["tickfont"] = {**(axis.get("tickfont") or {}), "size": 11, "color": INK, "family": FONT}
    axis["automargin"] = True
    axis.setdefault("linecolor", EDGE)
    axis["gridcolor"] = RULE
    axis["zerolinecolor"] = EDGE
    return axis


def _categories(values) -> list:
    """The distinct values of a plain axis array (a typed array has none)."""
    if not isinstance(values, (list, tuple)):
        return []
    seen = []
    for v in values:
        if v not in seen:
            seen.append(v)
    return seen


def _blank_empty_bins(trace):
    """A 2-D density's empty bins drawn as the paper, not as the colour scale's
    darkest colour, so the cells that are there stand out."""
    import numpy as np

    z = _decode(trace.get("z"))
    try:
        arr = np.asarray(z, dtype="float64")
    except (TypeError, ValueError):
        return
    if arr.ndim != 2 or not arr.size:
        return
    trace["z"] = [[None if not np.isfinite(v) or v <= 0 else float(v) for v in row] for row in arr]


def _rows(trace) -> int:
    """How many rows a heatmap trace draws: its y labels, else its z."""
    n = len(_categories(trace.get("y")))
    if n:
        return n
    z = trace.get("z")
    if isinstance(z, list):
        return len(z)
    if isinstance(z, dict) and z.get("shape"):
        return int(str(z["shape"]).split(",")[0])
    return 0


def _length(values) -> int:
    """How many values an array holds, plain or plotly's base64 typed array."""
    if isinstance(values, dict) and "bdata" in values:
        import base64

        import numpy as np

        shape = values.get("shape")
        if shape:
            return int(np.prod([int(v) for v in str(shape).split(",")]))
        return len(base64.b64decode(values["bdata"])) // np.dtype(values.get("dtype", "f8")).itemsize
    return len(values or [])


def _strings(values):
    return isinstance(values, list) and values and all(isinstance(v, str) for v in values)


def restyle(fig: dict, spec: dict, opts=None) -> tuple[dict, str]:
    """Scimap Pro's figure in the page's type and colours, sized by the page.

    The figure's own title moves out (the page prints it above the plot), its
    in-figure controls go (the page has its own), and category axes are
    declared as such: plotly reads "0", "1", "2" as numbers otherwise, which
    put a heatmap's cluster rows at numeric positions. Returns the figure and
    a note when a spread was drawn from a sample of its values (`_thin`).
    """
    layout = dict(fig.get("layout") or {})
    data = fig.get("data") or []
    thinned = _thin(data)
    layout.pop("template", None)
    layout.pop("title", None)
    layout.pop("sliders", None)
    layout.pop("updatemenus", None)
    height = float(layout.pop("height", 420) or 420)
    top = float((layout.get("margin") or {}).get("t") or 0)
    # Tight margins: every axis sets `automargin`, and plotly pushes the
    # margin out for a legend or a colour bar itself, so nothing is held
    # empty for labels a plot does not have.
    layout["margin"] = {"t": 6, "r": 6, "b": 6, "l": 6, "pad": 2}
    layout.pop("width", None)
    layout["autosize"] = True
    layout["height"] = int(max(240, min(620, height - top + 6)))

    kinds = {t.get("type") for t in data}
    cat_x = any(_strings(t.get("x")) for t in data if t.get("type") in ("heatmap", "bar"))
    cat_y = any(_strings(t.get("y")) for t in data if t.get("type") in ("heatmap",))
    for name in [k for k in layout if k.startswith("xaxis")]:
        layout[name] = _axis(layout[name], cat_x)
    for name in [k for k in layout if k.startswith("yaxis")]:
        layout[name] = _axis(layout[name], cat_y)

    layout["font"] = {"family": FONT, "size": 11, "color": INK}
    layout["paper_bgcolor"] = "#ffffff"
    layout["plot_bgcolor"] = "#ffffff"
    layout["hoverlabel"] = {"bgcolor": "#ffffff", "bordercolor": EDGE,
                            "font": {"family": FONT, "size": 12, "color": INK}}
    legend = dict(layout.get("legend") or {})
    legend["font"] = {"family": FONT, "size": 11, "color": INK}
    legend["bgcolor"] = "rgba(0,0,0,0)"
    if legend.get("title"):
        legend["title"] = {**legend["title"], "font": {"family": FONT, "size": 11, "color": MUTED}}
    layout["legend"] = legend
    for note in layout.get("annotations") or []:
        if "scimap" in str(note.get("text", "")).lower():
            note["font"] = {"family": FONT, "size": 9, "color": "#98a2b3"}
    if "bar" in kinds:
        layout["height"] = min(layout["height"], 380)
    # A heatmap is as tall as its rows need, not a fixed box around them.
    if "heatmap" in kinds and kinds <= {"heatmap", "scatter", "scattergl"} and (cat_x or cat_y):
        rows = max((_rows(t) for t in data if t.get("type") == "heatmap"), default=0)
        if rows:
            layout["height"] = int(max(170, min(620, rows * 30 + 96)))
    if "scattergl" in kinds or "scatter" in kinds:
        layout["dragmode"] = layout.get("dragmode") or "pan"
        for name in [k for k in layout if k.startswith(("xaxis", "yaxis"))]:
            layout[name].update({"showgrid": False, "zeroline": False})
        # Scimap Pro's own size control is gone, so the points are sized for
        # how many there are: small enough that a dense embedding shows its
        # structure, not a blot.
        # A dot plot's sizes are its data (the share expressing) and stay;
        # the page scales every size by the point-size setting itself, so a
        # change of it never redraws (scimapViews.applyPoints).
        points = sum(_length(t.get("x")) for t in data if t.get("type") in ("scattergl", "scatter"))
        size = max(1.5, min(6.0, 800.0 / max(1.0, points) ** 0.5))
        for trace in data:
            marker = trace.get("marker") or {}
            given = marker.get("size")
            if isinstance(given, (list, dict)):
                continue
            if trace.get("type") in ("scattergl", "scatter") and trace.get("mode", "markers") == "markers":
                trace["marker"] = {**marker, "size": round(size, 2)}
    for trace in data:
        bar = trace.get("colorbar")
        if isinstance(bar, dict):
            bar.update({"thickness": 10, "outlinewidth": 0, "len": 0.86,
                        "tickfont": {"family": FONT, "size": 10, "color": MUTED}})
            if isinstance(bar.get("title"), dict):
                bar["title"] = {**bar["title"], "font": {"family": FONT, "size": 11, "color": MUTED}}
        # Gaps only between the cells of a table of categories: on a 2-D
        # density they draw a grid over the data, and they make plotly draw
        # every bin as its own rectangle instead of one image.
        if trace.get("type") == "heatmap" and (cat_x or cat_y):
            trace["xgap"] = 1
            trace["ygap"] = 1
        elif trace.get("type") == "heatmap":
            trace.pop("xgap", None)
            trace.pop("ygap", None)
            layout["height"] = min(layout["height"], 460)
            _blank_empty_bins(trace)
            for name in [k for k in layout if k.startswith(("xaxis", "yaxis"))]:
                layout[name].update({"showgrid": False, "zeroline": False})
    return {"data": data, "layout": layout}, thinned


def _decode(values):
    """A plotly array as numpy: a plain list, or the base64 typed array plotly 6
    writes (`{dtype, bdata, shape}`)."""
    import base64

    import numpy as np

    if isinstance(values, dict) and "bdata" in values:
        arr = np.frombuffer(base64.b64decode(values["bdata"]), dtype=np.dtype(values.get("dtype", "f8")))
        if values.get("shape"):
            arr = arr.reshape([int(v) for v in str(values["shape"]).split(",")])
        return arr
    return values


def _encode(arr, like):
    import base64

    if isinstance(like, dict) and "bdata" in like:
        out = {"dtype": like.get("dtype", "f8"), "bdata": base64.b64encode(arr.tobytes()).decode()}
        if arr.ndim > 1:
            out["shape"] = ",".join(str(v) for v in arr.shape)
        return out
    return [like[i] for i in arr] if isinstance(arr, list) else arr


def _thin(data) -> str:
    """A violin or box with more than `SPREAD_POINTS` values keeps a fixed
    random sample of them (the same every time): the shape plotly draws from
    them is the same and the figure is a fraction of the size. Returns a note
    naming how many were drawn, or ""."""
    import numpy as np

    largest, kept = 0, 0
    for trace in data:
        if trace.get("type") not in ("violin", "box"):
            continue
        axis = "y" if _length(trace.get("y")) >= _length(trace.get("x")) else "x"
        total = _length(trace.get(axis))
        if total <= SPREAD_POINTS:
            continue
        pick = np.sort(np.random.default_rng(0).choice(total, SPREAD_POINTS, replace=False))
        for name in ("x", "y", "text", "hovertext", "customdata", "ids", "selectedpoints"):
            values = trace.get(name)
            if values is None or isinstance(values, str) or _length(values) != total:
                continue
            arr = _decode(values)
            if isinstance(arr, list):
                trace[name] = [arr[i] for i in pick]
            else:
                trace[name] = _encode(np.ascontiguousarray(arr[pick]), values)
        largest, kept = max(largest, total), SPREAD_POINTS
        trace["points"] = False
    if not kept:
        return ""
    return f"each spread drawn from {kept:,} of up to {largest:,} cells; the download uses every cell"
