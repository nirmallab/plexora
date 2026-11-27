"""The notebook's tools: what each one asks, which Scimap Pro call it makes,
and what it needs before it can run.

One definition per tool serves the sidebar (availability and the reason a tool
is off), the step's form (its fields), the run (the exact Scimap Pro call) and
the export (the same call, written out). Nothing else in the plugin knows a
Scimap Pro argument name.

The arguments a tool passes are the ones that make a run reproducible and
fast, said explicitly rather than left to defaults that may change:

* every random step gets a stored seed (`randomState`) -- Scimap Pro's UMAP
  defaults to none, which would make every rerun different;
* `useGraph=True` lets UMAP reuse the neighbour graph clustering built;
* `fitSample="auto"` fits on a stratified sample of a large table and projects
  the rest, which is Scimap Pro's own speed path;
* `imageId` and the coordinate columns are the working file's, so a spatial
  graph never crosses from one image to another.
"""

from __future__ import annotations

import re

#: Sidebar order, by category: the order a first analysis runs in. Ids are
#: stable: they are stored in notebooks.
CATEGORIES = (
    ("prepare", "Prepare", ("prepare", "quality", "normalize", "pca")),
    ("celltypes", "Cell types", ("gating", "celltypes", "cluster", "markers", "name_groups")),
    ("explore", "Explore", ("umap", "composition", "expression")),
    ("spatial", "Spatial", ("distance", "interaction", "neighborhoods", "svg")),
    ("compare", "Compare", ("compare",)),
)

#: The layer Normalize writes and every expression step reads on counts data:
#: each cell scaled to the same total, then log1p.
LOG_LAYER = "log"
#: The scaled counts Normalize writes on the way.
NORMALIZED_LAYER = "normalized"


#: The steps that read expression: on counts data they read the logged layer Normalize writes.
READS_LOG = frozenset({"cluster", "umap", "markers", "expression", "svg", "pca"})
#: The steps that read the expression graph PCA & neighbours builds, on counts data.
READS_GRAPH = frozenset({"cluster", "umap"})
#: The steps whose defaults are SCIMAP Pro's standard workflow for the data (`sm.ai.recipe`).
COUNTS_TOOLS = frozenset({"normalize", "pca", "svg"})


def _rna(work) -> bool:
    """Counts data: single-cell RNA, spots or bins (`modes.work_facts`)."""
    return str((work or {}).get("data_mode") or "").startswith("st-")

#: Tools a notebook may still hold under an earlier id, and the id they open
#: as. Annotating and merging were four tools making one call (`tl.rename`):
#: giving several groups one name is a merge.
LEGACY = {"annotate_clusters": "name_groups", "merge_clusters": "name_groups",
          "annotate_neighborhoods": "name_groups", "merge_neighborhoods": "name_groups",
          "autophenotype": "celltypes"}

#: Tools whose result is one categorical column of cell labels (`label`).
LABEL_TOOLS = frozenset({"cluster", "name_groups", "neighborhoods", "celltypes"})

NUCLEAR = re.compile(r"(dna|dapi|hoechst|nucle|^h3$|draq|sytox)", re.I)


def _f(name, label, kind, default=None, **extra):
    return {"name": name, "label": label, "kind": kind, "default": default, **extra}


TOOLS = {
    "prepare": {
        "name": "Prepare data", "icon": "table", "call": None,
        "summary": "Makes the working AnnData every other step reads and writes.",
        "fields": [],
    },
    "gating": {
        "name": "Gating", "icon": "sliders", "call": None, "viewer": True,
        "summary": "Opens Plexora's gating in the image viewer.",
        "fields": [],
    },
    "celltypes": {
        "name": "Cell types from gates", "icon": "tag", "call": "sc.phenotype", "produces": "labels",
        "summary": "Names cell types from your gates and a few simple rules, such as CD3+ is a T cell.",
        "fields": [_f("rules", "Rules", "rules", []), _f("label", "Result column", "label", None)],
    },
    "cluster": {
        "name": "Clustering", "icon": "cluster", "call": "tl.cluster", "produces": "clusters",
        "summary": "Groups cells by their marker expression.",
        "fields": [
            _f("method", "Method", "segmented", "leiden",
               choices=[["leiden", "Leiden"], ["kmeans", "k-means"]]),
            _f("resolution", "Resolution", "slider", 1.0, min=0.1, max=3.0, step=0.1,
               show_if={"method": "leiden"}, help="Higher finds more, smaller clusters."),
            _f("k", "Clusters", "number", 10, min=2, max=60, step=1, show_if={"method": "kmeans"}),
            _f("markers", "Markers", "markers", None),
            _f("label", "Result column", "label", None),
            _f("seed", "Seed", "seed", 0),
            _f("fit", "Fit on", "segmented", "auto", group="advanced",
               choices=[["auto", "A sample of large tables"], ["all", "Every cell"]],
               help="A sample of 200,000 cells, chosen per image, then every cell is assigned."),
            _f("layer", "Matrix", "layer", None, group="advanced",
               help="Which expression values to read. Automatic uses the raw layer, logged, when the file has one; else X."),
        ],
    },
    "umap": {
        "name": "UMAP", "icon": "scatter", "call": "tl.umap", "produces": "embedding",
        "summary": "A two-dimensional map of the cells by expression.",
        "fields": [
            _f("neighbors", "Neighbors", "number", 15, min=2, max=200, step=1),
            _f("min_dist", "Minimum distance", "slider", 0.1, min=0.0, max=1.0, step=0.05),
            _f("label", "Result name", "label", None),
            _f("seed", "Seed", "seed", 0),
            _f("color", "Color by", "column", None, role="labels", display=True, optional=True),
            _f("fit", "Fit on", "segmented", "auto", group="advanced",
               choices=[["auto", "A sample of large tables"], ["all", "Every cell"]]),
            _f("layer", "Matrix", "layer", None, group="advanced",
               help="Which expression values to read. Automatic uses the raw layer, logged, when the file has one; else X."),
        ],
    },
    "markers": {
        "name": "Find markers", "icon": "list", "call": "sc.rankFeatures", "produces": "table",
        "summary": "The markers that set each cluster apart from the rest: what to read before naming it.",
        "fields": [_f("source", "Groups", "column", None, role="clusters_first"),
                   _f("method", "Test", "segmented", "ttest",
                      choices=[["ttest", "t-test"], ["wilcoxon", "Wilcoxon"]], group="advanced",
                      help="Ranks the markers; with cells as the unit it describes the groups, it does not test them."),
                   _f("label", "Result name", "label", None)],
    },
    "name_groups": {
        "name": "Name groups", "icon": "pen", "call": "tl.rename", "produces": "same",
        "summary": "Names each cluster or neighborhood; the same name on several joins them. Writes a new column.",
        "fields": [_f("source", "Groups", "column", None, role="clusters_first"),
                   _f("names", "Labels", "mapping", {}), _f("label", "Result column", "label", None)],
    },
    "composition": {
        "name": "Cell composition", "icon": "bars", "call": None,
        "summary": "How many cells of each kind, per image.",
        "fields": [_f("source", "Cell labels", "column", None, role="labels")],
    },
    "expression": {
        "name": "Marker expression", "icon": "grid", "call": None,
        "summary": "Mean marker expression of each group.",
        "fields": [_f("source", "Group by", "column", None, role="labels"),
                   _f("markers", "Markers", "markers", None)],
    },
    "distance": {
        "name": "Spatial distance", "icon": "distance", "call": "sp.spatialDistance",
        "produces": "table",
        "summary": "How far each kind of cell is from the nearest cell of each other kind.",
        "fields": [_f("source", "Cell labels", "column", None, role="labels"),
                   _f("statistic", "Summary", "segmented", "median",
                      choices=[["median", "Median"], ["mean", "Mean"]]),
                   _f("label", "Result name", "label", None)],
    },
    "interaction": {
        "name": "Cell–cell interaction", "icon": "network", "call": "sp.spatialCooccurrence",
        "produces": "table",
        "summary": "Which kinds of cell are neighbors more, or less, often than by chance.",
        "fields": [_f("source", "Cell labels", "column", None, role="labels"),
                   _f("graph", "Neighbors", "segmented", "radius",
                      choices=[["radius", "Within a radius"], ["knn", "Nearest cells"]]),
                   _f("radius", "Radius", "number", 30, min=1, max=1000, step=1, unit="px",
                      show_if={"graph": "radius"}),
                   _f("knn", "Nearest", "number", 10, min=1, max=100, step=1,
                      show_if={"graph": "knn"}),
                   _f("permutations", "Permutations", "number", 1000, min=10, max=10000, step=10,
                      group="advanced", help="Shuffles of the labels that make the chance level."),
                   _f("seed", "Seed", "seed", 0),
                   _f("label", "Result name", "label", None)],
    },
    "neighborhoods": {
        "name": "Neighborhood detection", "icon": "hexes", "call": "sp.spatialDomains",
        "produces": "neighborhoods",
        "summary": "Finds recurring mixtures of cell kinds around each cell.",
        "fields": [_f("source", "Cell labels", "column", None, role="labels"),
                   _f("graph", "Neighbors", "segmented", "radius",
                      choices=[["radius", "Within a radius"], ["knn", "Nearest cells"]]),
                   _f("radius", "Radius", "number", 30, min=1, max=1000, step=1, unit="px",
                      show_if={"graph": "radius"}),
                   _f("knn", "Nearest", "number", 10, min=1, max=100, step=1,
                      show_if={"graph": "knn"}),
                   _f("n", "Neighborhoods", "number", 6, min=2, max=40, step=1),
                   _f("seed", "Seed", "seed", 0),
                   _f("label", "Result column", "label", None)],
    },
    "compare": {
        "name": "Compare groups", "icon": "compare", "call": "tl.compareGroups", "produces": "table",
        "summary": "Whether the share of each cell type differs between groups of samples, counting each sample once.",
        "fields": [_f("by", "Compare", "column", None, role="samples"),
                   _f("source", "Cell labels", "column", None, role="labels"),
                   _f("replicate", "Count each", "column", None, role="replicates", optional=True,
                      help="The unit of replication: a patient when several images come from one patient."),
                   _f("reference", "Against", "text", None, optional=True, group="advanced",
                      help="One group as the baseline; left empty, every group is tested at once."),
                   _f("label", "Result name", "label", None)],
    },
    "normalize": {
        "name": "Normalize", "icon": "violin", "call": "pp.normalizeTotal", "produces": "layer",
        "summary": "Scales each cell to the same total count, then logs it, into a layer every later step reads.",
        "fields": [_f("target", "Counts per cell", "number", 10000, min=100, max=1000000, step=100,
                      help="Each cell's counts are scaled to add up to this before the log."),
                   _f("counts", "Counts", "layer", None, group="advanced",
                      help="Which matrix holds the raw counts. Automatic uses the raw layer when the file has one; "
                           "else X.")],
    },
    "pca": {
        "name": "PCA & neighbours", "icon": "layers", "call": "pp.neighbors", "produces": "graph",
        "summary": "Reduces the genes to their main axes of variation and links each cell to the cells most like "
                   "it: the graph clustering and UMAP read.",
        "fields": [_f("components", "Components", "number", 30, min=2, max=200, step=1),
                   _f("variable", "Variable genes only", "toggle", True,
                      help="Reduce only the most variable genes, found first. Off uses every gene."),
                   _f("neighbors", "Neighbors", "number", 15, min=2, max=200, step=1)],
    },
    "svg": {
        "name": "Spatially variable genes", "icon": "map", "call": "sp.spatiallyVariableFeatures",
        "produces": "table",
        "summary": "Ranks the genes whose expression is patterned in the tissue, each sample on its own.",
        "fields": [_f("method", "Statistic", "segmented", "moran",
                      choices=[["moran", "Moran's I"], ["geary", "Geary's C"]]),
                   _f("graph", "Neighbors", "segmented", "knn",
                      choices=[["knn", "Nearest"], ["radius", "Within a radius"]]),
                   _f("knn", "Nearest", "number", 6, min=2, max=50, step=1, show_if={"graph": "knn"}),
                   _f("radius", "Radius", "number", 30, min=1, max=1000, step=1, unit="px",
                      show_if={"graph": "radius"}),
                   _f("markers", "Genes", "markers", None, help="Leave empty to rank every gene."),
                   _f("label", "Result name", "label", None)],
    },
    "quality": {
        "name": "Check quality", "icon": "shield", "call": "sp.filterSpatialOutliers", "produces": "flags",
        "summary": "Flags cells whose position looks wrong, such as isolated specks and crowded pile-ups. Nothing is deleted.",
        "fields": [_f("radius", "Radius", "number", 30, min=1, max=1000, step=1, unit="px",
                      help="How far around each cell its neighbors are counted."),
                   _f("strict", "Strictness", "segmented", "standard",
                      choices=[["lenient", "Lenient"], ["standard", "Standard"], ["strict", "Strict"]],
                      help="How far from the image's usual crowding a cell must be to be flagged."),
                   _f("edge", "Flag edge cells", "toggle", False, group="advanced",
                      help="Also flag cells on the outer edge of the tissue."),
                   _f("label", "Result name", "label", None)],
    },
}

#: Which tool's result a "Requires …" reason names.
ROLE_WORDS = {"clusters": "clusters", "neighborhoods": "neighborhoods", "labels": "cell labels",
              "clusters_first": "clusters or neighborhoods", "samples": "a sample column with two groups or more"}

#: Strictness, as `filterSpatialOutliers`' `nDeviations`.
STRICTNESS = {"lenient": 7.0, "standard": 5.0, "strict": 3.0}


def migrate(document) -> dict:
    """Open an older notebook's steps under today's tools, in place."""
    for cell in document.get("cells") or []:
        new = LEGACY.get(cell.get("tool"))
        if new is None:
            continue
        old = cell["tool"]
        params = dict(cell.get("params") or {})
        if old.startswith("merge_"):
            names = {}
            for group in params.pop("groups", None) or []:
                name = str(group.get("name") or "").strip()
                for member in group.get("members") or []:
                    if name:
                        names[str(member)] = name
            params["names"] = names
        cell["tool"] = new
        cell["params"] = params
        cell["views"] = None
    return document


def describe() -> list:
    """The catalogue the page renders, in sidebar order."""
    out = []
    for key, title, ids in CATEGORIES:
        out.append({"id": key, "title": title,
                    "tools": [{"id": i, **{k: v for k, v in TOOLS[i].items()}} for i in ids]})
    return out


# -- what exists in a notebook --------------------------------------------


def column_roles(document, work) -> dict:
    """`{column: role}` for every categorical column a step can read.

    A column a step of this notebook produced has that step's role (Name
    groups passes on the role of what it named); a sample metadata column
    (patient, condition) is `samples`; a quality flag is `flags`; any other
    categorical column already in the working file (a phenotype column that
    came with the data, say) counts as cell labels."""
    roles = {}
    columns = (work or {}).get("columns") or []
    present = {c["name"] for c in columns if c.get("kind") == "category"}
    skip = {(work or {}).get("image_column") or "imageid"}
    for name in present - skip:
        roles[name] = "labels"
    for name in sample_columns(document):
        if name in present:
            roles[name] = "samples"
    for cell in document.get("cells") or []:
        run = cell.get("run") or {}
        if run.get("status") != "done":
            continue
        produces = TOOLS.get(cell["tool"], {}).get("produces")
        eff = run.get("effective") or {}
        column = eff.get("label")
        if produces == "same":
            produces = roles.get((eff.get("inputs") or {}).get("source"))
        if produces in ("clusters", "neighborhoods") and column in present:
            roles[column] = produces
        elif produces == "flags":
            for spec in run.get("outputs") or []:
                kind, _, name = str(spec).partition(":")
                if kind == "obs" and name in present:
                    roles[name] = "flags"
            if column in present:
                roles[column] = "flags"
            # What the measuring call before it wrote (`<label>_qc_isEdge`).
            for name in present:
                if column and name.startswith(f"{column}_qc_"):
                    roles[name] = "flags"
    return roles


def sample_columns(document) -> list:
    """The sample metadata columns of the notebook's data, with at least one value."""
    try:
        from plexora.plugins.scimap.server import samples

        table = samples.get(document.get("context") or {}) or {}
    except Exception:  # noqa: BLE001 -- no context, no table
        return []
    rows = table.get("rows") or {}
    return [c for c in table.get("columns") or [] if any((r or {}).get(c) for r in rows.values())]


#: Samples each group needs before Compare groups tests it (Scimap Pro's own
#: `minReplicates`).
MIN_SAMPLES = 3


def too_few_samples(document, columns) -> str:
    """Why no sample column can be compared yet, or "" when one can: each of
    two groups or more needs MIN_SAMPLES images."""
    try:
        from plexora.plugins.scimap.server import samples

        rows = (samples.get(document.get("context") or {}) or {}).get("rows") or {}
    except Exception:  # noqa: BLE001
        return ""
    worst = None
    tallies = []
    for column in columns:
        counts = {}
        for values in rows.values():
            value = str((values or {}).get(column) or "").strip()
            if value:
                counts[value] = counts.get(value, 0) + 1
        if len(counts) >= 2 and min(counts.values()) >= MIN_SAMPLES:
            return ""
        # A column with one sample per value (a patient id) is not a grouping.
        if len(counts) >= 2 and max(counts.values()) > 1:
            tallies.append((len(counts), column, counts))
    if tallies:
        _n, column, counts = min(tallies)
        worst = (column, counts)
    elif columns:
        return f"Needs {MIN_SAMPLES} samples per group, and a column such as condition"
    if worst is None:
        return ""
    column, counts = worst
    have = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
    return f"Needs {MIN_SAMPLES} samples per group ({column}: {have})"


def columns_for(role, roles, work=None) -> list:
    if role == "labels":
        return [c for c, r in roles.items() if r in ("labels", "clusters", "neighborhoods")]
    if role == "clusters_first":
        order = {"clusters": 0, "neighborhoods": 1, "labels": 2}
        return sorted((c for c, r in roles.items() if r in order), key=lambda c: order[roles[c]])
    if role == "samples":
        # Two groups or more to compare.
        counts = {c["name"]: int(c.get("n") or 0) for c in (work or {}).get("columns") or []}
        return [c for c, r in roles.items() if r == "samples" and (work is None or counts.get(c, 0) >= 2)]
    if role == "replicates":
        image = (work or {}).get("image_column") or "imageid"
        return [image] + [c for c, r in roles.items() if r == "samples"]
    return [c for c, r in roles.items() if r == role]


def availability(tool_id, document, work, status) -> dict:
    """`{enabled, reason, link}` for the sidebar."""
    tool = TOOLS[tool_id]
    off = lambda reason, link=None: {"enabled": False, "reason": reason, "link": link}  # noqa: E731
    if not status.get("installed") or not status.get("compatible"):
        return off("")
    if tool_id == "prepare":
        return {"enabled": True, "reason": "", "link": None,
                "done": bool(work and work.get("exists")),
                "start": not (work and work.get("exists"))}
    if tool_id == "gating":
        return {"enabled": True, "reason": "", "link": None, "viewer": True}
    if not (work and work.get("exists")):
        return off("Requires prepared data")
    call = tool.get("call")
    if call:
        from plexora.plugins.scimap.server import install

        if not install.has_function(call):
            return off("Needs a newer Scimap Pro")
    if tool_id in COUNTS_TOOLS:
        from plexora.plugins.scimap.server import install

        if not install.supports("advice"):
            return off("Needs a newer Scimap Pro")
        if tool_id == "svg" and not install.supports("svg_plot"):
            return off("Needs a newer Scimap Pro")
    if _rna(work) and tool_id in READS_LOG and LOG_LAYER not in (work.get("layers") or []):
        return off("Requires normalized counts", "normalize")
    if _rna(work) and tool_id in READS_GRAPH and "X_pca" not in (work.get("obsm") or []):
        return off("Requires PCA & neighbours", "pca")
    if tool_id == "celltypes":
        from plexora.plugins.scimap.server import gates, install

        if not install.supports("gated_layer"):
            return off("Needs a newer Scimap Pro")
        if not gates.any_set(document):
            return off("Requires gates: set some in Gating first", "gating")
    roles = column_roles(document, work)
    if tool_id == "compare":
        if len(work.get("images") or []) < 2:
            return off("Requires a dataset of several samples")
        if not columns_for("samples", roles, work):
            return off("Requires a sample column, such as condition, in Prepare data", "prepare")
        short = too_few_samples(document, columns_for("samples", roles, work))
        if short:
            return off(short)
        if not columns_for("labels", roles):
            return off("Requires cell labels", "cluster")
        return {"enabled": True, "reason": "", "link": None}
    needed = next((f.get("role") for f in tool["fields"] if f["kind"] == "column" and not f.get("optional")), None)
    if needed and not columns_for(needed, roles, work):
        if needed == "neighborhoods":
            return off("Requires neighborhoods", "neighborhoods")
        if needed == "clusters":
            return off("Requires clusters", "cluster")
        if needed == "clusters_first":
            return off("Requires clusters or neighborhoods", "cluster")
        return off("Requires cell labels", "cluster")
    return {"enabled": True, "reason": "", "link": None}


# -- turning a step into a call -------------------------------------------


def _unique(base, taken):
    name, i = base, 2
    while name in taken:
        name, i = f"{base}_{i}", i + 1
    return name


def default_label(tool_id, params, taken, source=None) -> str:
    if tool_id == "cluster":
        if params.get("method", "leiden") == "kmeans":
            base = f"kmeans_k{int(params.get('k', 10))}"
        else:
            base = f"leiden_r{_short(params.get('resolution', 1.0))}"
    elif tool_id == "umap":
        base = "umap"
    elif tool_id == "name_groups":
        base = f"{source or 'labels'}_named"
    elif tool_id == "celltypes":
        base = "celltypes"
    elif tool_id == "markers":
        base = f"markers_{source}" if source else "rankFeatures"
    elif tool_id == "compare":
        base = "compareGroups"
    elif tool_id == "quality":
        base = "spatialOutlier"
    elif tool_id == "normalize":
        base = LOG_LAYER
    elif tool_id == "svg":
        base = "spatiallyVariableFeatures"
    elif tool_id == "distance":
        base = "spatialDistance"
    elif tool_id == "interaction":
        base = "spatialCooccurrence"
    elif tool_id == "neighborhoods":
        base = "neighborhoods"
    else:
        base = tool_id
    return _unique(base, taken)


def _short(value):
    text = f"{float(value):.2f}".rstrip("0").rstrip(".")
    return text.replace(".", "p") if "." in text else text


def defaults(tool_id) -> dict:
    return {f["name"]: f["default"] for f in TOOLS[tool_id]["fields"]
            if f["default"] is not None}


def expression_layer(work, chosen=None) -> dict:
    """`{layer, log}` for a function that reads expression.

    `chosen` is the step's Matrix setting ("raw", "X" or a named layer). Left
    automatic: the raw layer, logged on the fly, when the file has one (Scimap
    Pro's default); else `X`, logged only if it is not already
    (working._matrix_facts). `raw` is always logged; `X` only when it is not
    logged yet; a named layer is read as it is."""
    available = work.get("layers") or (["raw", "X"] if work.get("raw", True) else ["X"])
    if chosen and chosen not in available:
        raise ValueError(f"The working file has no matrix {chosen!r}. Choose one of: {', '.join(available)}.")
    if not chosen and _rna(work):
        # Counts: the normalized, logged layer Normalize wrote; never raw
        # counts logged on the fly, which keep each cell's depth.
        if LOG_LAYER not in available:
            raise ValueError("Normalize the counts first: this step reads the logged layer Normalize writes.")
        return {"layer": LOG_LAYER, "log": False}
    layer = chosen or ("raw" if "raw" in available else "X")
    if layer == "raw":
        return {"layer": "raw", "log": True}
    if layer == "X":
        return {"layer": None, "log": not work.get("x_logged", False)}
    return {"layer": layer, "log": False}


def effective(tool_id, params, work, document, cell_id) -> dict:
    """The step's settings with every default and implicit argument filled in:
    `{key, kwargs, label, inputs}`. What is stored with the run is what was
    called, so an export writes exactly this."""
    tool = TOOLS[tool_id]
    # On counts data a step opens with SCIMAP Pro's standard workflow for the
    # data (`sm.ai.recipe`): its arguments are the defaults, the form's fields
    # the person's own.
    said = None
    if _rna(work):
        from plexora.plugins.scimap.server import modes

        said = modes.advice(document, work)
    p = {**defaults(tool_id), **(_field_defaults(said, tool_id) if said else {}),
         **{k: v for k, v in (params or {}).items() if v not in (None, "")}}
    image = work.get("image_column") or "imageid"
    xy = {"xCoordinate": work.get("x_column") or "X_centroid",
          "yCoordinate": work.get("y_column") or "Y_centroid"}
    roles = column_roles(document, work)
    taken = {c["name"] for c in work.get("columns") or []}
    # This step's own previous label is not "taken": a rerun overwrites it.
    own = None
    for cell in document.get("cells") or []:
        if cell["id"] == cell_id:
            own = ((cell.get("run") or {}).get("effective") or {}).get("label")
            taken.discard(own)
    source = p.get("source")
    field = next((f for f in tool["fields"] if f["name"] == "source"), None)
    if field is not None and (not source or source not in roles):
        choices = columns_for(field.get("role"), roles, work)
        source = default_choice(field.get("role"), choices, roles, document)
        if source is None:
            raise ValueError(f"{tool['name']} needs {ROLE_WORDS.get(field.get('role'), 'labels')} first.")
    label = str(p.get("label") or "").strip() or (
        # A rerun of Name groups keeps its column, so the steps reading it stay put.
        own if tool_id == "name_groups" and own else default_label(tool_id, p, taken, source))
    if p.get("label") and tool_id == "name_groups":
        # A name the user typed: never the column it renames, never another
        # step's column (a rerun of this step may reuse its own).
        if label == source:
            raise ValueError("The output column must differ from the groups it names.")
        if label in taken:
            raise ValueError(f"A column named {label} already exists; choose another name.")
    inputs = {"source": source} if source else {}
    kwargs = {}
    before = []
    gates_revision = None
    call = tool.get("call")
    if tool_id == "cluster":
        kwargs = {"method": p["method"], "randomState": int(p.get("seed", 0)), "label": label,
                  "imageId": image, "useGraph": True,
                  "fitSample": "auto" if p.get("fit", "auto") == "auto" else None,
                  **expression_layer(work, p.get("layer"))}
        if p["method"] == "leiden":
            kwargs["leidenResolution"] = float(p.get("resolution", 1.0))
        else:
            kwargs["k"] = int(p.get("k", 10))
        markers = _markers(p.get("markers"), work)
        if markers is not None:
            kwargs["subsetMarkers"] = markers
        if said:
            # The graph PCA & neighbours built, read as SCIMAP Pro's workflow reads it.
            kwargs = {**_recipe(said, "tl.cluster"), **kwargs}
    elif tool_id == "umap":
        kwargs = {"nNeighbors": int(p.get("neighbors", 15)), "minDist": float(p.get("min_dist", 0.1)),
                  "randomState": int(p.get("seed", 0)), "label": label, "imageId": image,
                  "useGraph": True, "fitSample": "auto" if p.get("fit", "auto") == "auto" else None,
                  **expression_layer(work, p.get("layer"))}
    elif tool_id == "name_groups":
        names = {str(k): str(v).strip() for k, v in (p.get("names") or {}).items() if str(v).strip()}
        rename = {}
        for old, new in names.items():
            rename.setdefault(new, []).append(old)
        if not rename:
            raise ValueError("Give at least one name.")
        kwargs = {"rename": rename, "fromColumn": source, "toColumn": label}
    elif tool_id == "markers":
        kwargs = {"groupBy": source, "method": p.get("method", "ttest"), "label": label,
                  **_rank_layer(work)}
    elif tool_id == "celltypes":
        from plexora.plugins.scimap.server import gates as _gates
        from plexora.plugins.scimap.server import install

        if not install.supports("gated_layer"):
            raise ValueError(install.why_not("gated_layer"))
        if not work.get("raw", True):
            raise ValueError("Cell types from gates rescales the raw values, and this working file has none (.raw).")
        rules = clean_rules(p.get("rules"), work)
        if not rules:
            raise ValueError("Add at least one rule: a cell type and the markers it is positive for.")
        used = sorted({m for r in rules for m in r["pos"] + r["neg"]})
        source_of = _gates.sources(document, work)
        missing = _gates.missing(source_of, used)
        if missing:
            marker, image = missing[0]
            more = f" (and {len(missing) - 1} more)" if len(missing) > 1 else ""
            raise ValueError(f"{marker} has no gate in {image}{more}. Set it in Gating, or take it out of the rules.")
        # Scimap Pro reads the gates out of this Plexora (`hl.syncFromViewer`,
        # into uns['gates']), rescales the raw values into their own layer so
        # 0.5 is each gate, then names the cells from the rules. Gates are on
        # the scale Plexora read the table on: log1p when its log switch is on.
        before = [{"key": "hl.syncFromViewer", "label": None,
                   "kwargs": {"what": ["gates"], "viewer": "plexora", "project": project, "subset": [img],
                              "imageId": image, "overwrite": True, "recompute": True, "peer": "inprocess"}}
                  for img, project in source_of["projects"].items()]
        before.append({"key": "pp.rescale", "label": None, "writes": [f"layers:{GATED_LAYER}"],
                       "kwargs": {"log": source_of["logged"], "layer": "raw", "targetLayer": GATED_LAYER,
                                  "imageId": image, "randomState": 0, "recompute": True}})
        # The layer it reads was just rewritten, which Scimap Pro's ledger
        # cannot see: recompute, never reuse an earlier answer.
        kwargs = {"phenotype": rules_frame(rules, used), "gate": 0.5, "label": label,
                  "imageId": image, "layer": GATED_LAYER, "recompute": True}
        gates_revision = _gates.revision(document)
    elif tool_id == "compare":
        samples = columns_for("samples", roles, work)
        by = p.get("by") if p.get("by") in samples else (samples[0] if samples else None)
        if by is None:
            raise ValueError("Compare groups needs a sample column with two groups or more, "
                             "such as condition; add it in Prepare data.")
        replicate = p.get("replicate") if p.get("replicate") in columns_for("replicates", roles, work) else image
        kwargs = {"groupBy": by, "what": "proportion", "values": source, "replicateBy": replicate,
                  "label": label}
        reference = str(p.get("reference") or "").strip()
        if reference:
            kwargs["reference"] = reference
        inputs["by"] = by
    elif tool_id == "quality":
        qc = f"{label}_qc"
        before = [{"key": "sp.spatialQcMetrics", "label": qc,
                   "kwargs": {"radius": float(p.get("radius", 30)), "graphMethod": "radius",
                              "label": qc, "imageId": image, **xy}}]
        kwargs = {"metrics": qc, "nDeviations": STRICTNESS.get(p.get("strict"), 5.0),
                  "dropEdge": bool(p.get("edge")), "removeCells": False, "label": label,
                  "imageId": image}
    elif tool_id == "distance":
        kwargs = {"phenotype": source, "summary": p.get("statistic", "median"), "label": label,
                  "imageId": image, **xy}
    elif tool_id == "interaction":
        # 0.1.29 renamed these (`graphMethod`, `nPerms`); the installed
        # release's own names are used.
        kwargs = {"phenotype": source, _named(call, "graphMethod", "method"): p.get("graph", "radius"),
                  "radius": float(p.get("radius", 30)), "knn": int(p.get("knn", 10)),
                  _named(call, "nPerms", "permutation"): int(p.get("permutations", 1000)),
                  "randomState": int(p.get("seed", 0)), "label": label, "imageId": image, **xy}
    elif tool_id == "neighborhoods":
        kwargs = {"method": "count", "phenotype": source, "graphMethod": p.get("graph", "radius"),
                  "radius": float(p.get("radius", 30)), "knn": int(p.get("knn", 10)),
                  "nDomains": int(p.get("n", 6)), "randomState": int(p.get("seed", 0)),
                  "label": label, "imageId": image, **xy}
    elif tool_id == "normalize":
        source_layer = p.get("counts") or ("raw" if "raw" in (work.get("layers") or []) else "X")
        before = [{"key": "pp.normalizeTotal", "label": None, "writes": [f"layers:{NORMALIZED_LAYER}"],
                   "kwargs": {**_recipe(said, "pp.normalizeTotal"), "targetSum": float(p.get("target", 10000)),
                              "layer": None if source_layer == "X" else source_layer,
                              "targetLayer": NORMALIZED_LAYER, "recompute": True}}]
        # 0.1.29 folded log1p into pp.transform; 0.1.28 has pp.log1p.
        from plexora.plugins.scimap.server import install

        if install.has_function("pp.transform"):
            call = "pp.transform"
            kwargs = {"method": "log1p", "layer": NORMALIZED_LAYER, "targetLayer": LOG_LAYER, "recompute": True}
        else:
            call = "pp.log1p"
            kwargs = {"layer": NORMALIZED_LAYER, "targetLayer": LOG_LAYER, "recompute": True}
        label = LOG_LAYER
        inputs = {"layer": source_layer}
    elif tool_id == "pca":
        layer = expression_layer(work)["layer"]
        variable = bool(p.get("variable", True))
        if variable:
            before.append({"key": "pp.highlyVariableGenes", "label": None,
                           "kwargs": {**_recipe(said, "pp.highlyVariableGenes"), "layer": layer}})
        before.append({"key": "pp.pca", "label": None,
                       "kwargs": {**_recipe(said, "pp.pca"), "layer": layer, "nComps": int(p.get("components", 30)),
                                  "useHighlyVariable": variable, "randomState": int(p.get("seed", 0))}})
        kwargs = {**_recipe(said, "pp.neighbors"), "nNeighbors": int(p.get("neighbors", 15)),
                  "randomState": int(p.get("seed", 0))}
        label = None
    elif tool_id == "svg":
        genes = _markers(p.get("markers"), work)
        kwargs = {**_recipe(said, "sp.spatiallyVariableFeatures"),
                  "method": p.get("method", "moran"), "layer": _rank_layer(work)["layer"],
                  "graphMethod": p.get("graph", "knn"), "knn": int(p.get("knn", 6)),
                  "radius": float(p.get("radius", 30)), "genes": genes, "label": label,
                  "imageId": image, **xy}
    elif tool_id in ("composition", "expression"):
        kwargs = {"groupBy": source, "imageId": image}
        if tool_id == "expression":
            markers = _markers(p.get("markers"), work)
            if markers is not None:
                kwargs["markers"] = markers
        label = source
    # None is meaningful for fitSample ("every cell") and nowhere else.
    kwargs = {k: v for k, v in kwargs.items() if v is not None or k in ("fitSample", "layer")}
    # The Advanced box: checked against each function's signature, refused
    # whole if any line is wrong, and passed exactly as written.
    from plexora.plugins.scimap.server import advanced as _advanced

    extra = _advanced.for_step(tool_id, p.get("advanced"), kwargs)
    if call and extra.get(call):
        kwargs.update(extra[call])
    return {"key": call, "kwargs": kwargs, "label": label, "inputs": inputs, "before": before,
            **({"gates": gates_revision} if gates_revision else {}),
            "extra": extra.get(call) or {} if call else {},
            "advanced": {k: v for k, v in extra.items() if k != call},
            # The colour a UMAP is drawn in is settled when it runs, so a label
            # column made later does not repaint it by itself.
            "display": {"color": p.get("color") if p.get("color") in roles
                        else (_latest(columns_for("labels", roles), document)
                              if columns_for("labels", roles) else None)} if tool_id == "umap" else {}}


#: The layer Cell types from gates rescales: a copy of the raw values, so the
#: matrix (X) the other steps read is left as it is.
GATED_LAYER = "gated"


def frame(columns, rows) -> dict:
    """A table argument, kept as plain JSON with the run; the adapter hands
    Scimap Pro a DataFrame and the export writes one."""
    return {"__frame__": {"columns": list(columns), "rows": [list(r) for r in rows]}}


def clean_rules(rules, work) -> list:
    """`[{parent, name, pos, neg}]`: named rows whose markers are in the panel,
    each parent defined above it (or `all`)."""
    markers = set(work.get("markers") or [])
    out, names = [], set()
    for rule in rules or []:
        if not isinstance(rule, dict):
            continue
        name = str(rule.get("name") or "").strip()[:80]
        pos = [str(m) for m in rule.get("pos") or [] if str(m) in markers]
        neg = [str(m) for m in rule.get("neg") or [] if str(m) in markers and str(m) not in pos]
        if not name or not (pos or neg):
            continue
        parent = str(rule.get("parent") or "all").strip() or "all"
        if parent != "all" and parent not in names:
            raise ValueError(f"{name} sits inside {parent}, which is not defined above it.")
        names.add(name)
        out.append({"parent": parent, "name": name, "pos": pos, "neg": neg})
    return out


def rules_frame(rules, markers) -> dict:
    """The rules as Scimap Pro's gating workflow: parent, cell type, then one
    column per marker holding pos / neg (allpos / allneg for several)."""
    rows = []
    for rule in rules:
        word_pos = "allpos" if len(rule["pos"]) > 1 else "pos"
        word_neg = "allneg" if len(rule["neg"]) > 1 else "neg"
        rows.append([rule["parent"], rule["name"]] + [
            word_pos if m in rule["pos"] else word_neg if m in rule["neg"] else "" for m in markers])
    return frame(["parent", "celltype", *markers], rows)


def _rank_layer(work) -> dict:
    """Find markers reads the matrix the heatmaps read: X, which Prepare
    logged, or the raw layer when X was not; on counts, the logged layer
    Normalize wrote."""
    if _rna(work):
        return {"layer": LOG_LAYER}
    return {"layer": None if work.get("x_logged", True) else "raw"}


def _named(key, *names):
    """The first of `names` the installed Scimap Pro's `key` takes."""
    from plexora.plugins.scimap.server import advanced

    known, _extra = advanced.signature(key)
    return next((n for n in names if n in known), names[-1])


def default_choice(role, choices, roles, document):
    """The column a field reads when none is chosen: the latest made, and for
    Find markers and Name groups the latest clusters before any neighborhoods."""
    if not choices:
        return None
    if role == "clusters_first":
        for kind in ("clusters", "neighborhoods"):
            own = [c for c in choices if roles.get(c) == kind]
            if own:
                return _latest(own, document)
    return _latest(choices, document)


def _latest(choices, document):
    """The most recently produced of `choices`, else the first."""
    produced = []
    for cell in document.get("cells") or []:
        label = ((cell.get("run") or {}).get("effective") or {}).get("label")
        if label in choices:
            produced.append(label)
    return produced[-1] if produced else choices[0]


def _markers(chosen, work):
    """The markers a step uses: the user's choice, else every marker that is not
    a nuclear stain (an image's DNA channel; genes such as DNAJB1 are not
    stains). None when that is every marker (no argument passed)."""
    every = list(work.get("markers") or [])
    if chosen:
        picked = [m for m in chosen if m in every]
    elif _rna(work):
        picked = every
    else:
        picked = [m for m in every if not NUCLEAR.search(m)]
    return None if picked == every or not picked else picked


def _recipe(said, function) -> dict:
    """What SCIMAP Pro's standard workflow passes `function` on this data, the step's own names left out."""
    if not said:
        return {}
    from plexora.plugins.scimap.server import modes

    return {k: v for k, v in modes.recipe_args(said, function).items() if k not in ("label", "imageId")}


def _field_defaults(said, tool_id) -> dict:
    from plexora.plugins.scimap.server import modes

    return modes.field_defaults(said, tool_id)


def _matrix_chip(kwargs, work) -> list:
    """Named only when it is not Scimap Pro's own default (raw, logged)."""
    if "layer" not in kwargs or kwargs.get("layer") == "raw":
        return []
    layer = kwargs.get("layer") or "X"
    return [f"Matrix {layer}" + (", logged" if kwargs.get("log") else "")]


def _radius_chip(k, work) -> str:
    """A radius in the coordinates' own units, as the data records them."""
    unit = "µm" if (work or {}).get("coordinate_unit") == "um" else "px"
    return f"Radius {float(k.get('radius') or 0):g} {unit}"


def chips(tool_id, eff, work) -> list:
    """The settings line under a completed step's title."""
    k = eff.get("kwargs") or {}
    out = []
    n_markers = len(work.get("markers") or [])
    if tool_id == "cluster":
        if k.get("method") == "leiden":
            out += ["Leiden", f"Resolution {k.get('leidenResolution', 1.0):g}"]
        else:
            out += ["k-means", f"{k.get('k')} clusters"]
        used = len(k.get("subsetMarkers") or []) or n_markers
        out.append(f"{used} of {n_markers} markers")
        out.append(f"Seed {k.get('randomState', 0)}")
        out.append("Fit on a sample of large tables" if k.get("fitSample") == "auto" else "Fit on every cell")
        out += _matrix_chip(k, work)
    elif tool_id == "umap":
        out += [f"{k.get('nNeighbors')} neighbors", f"Minimum distance {k.get('minDist'):g}",
                f"Seed {k.get('randomState')}"]
        out += _matrix_chip(k, work)
    elif tool_id in ("distance",):
        out += [f"From {k.get('phenotype')}", k.get("summary", "median").capitalize()]
    elif tool_id == "interaction":
        out += [f"From {k.get('phenotype')}",
                _radius_chip(k, work) if k.get("graphMethod", k.get("method")) == "radius" else f"{k.get('knn')} nearest",
                f"{k.get('nPerms', k.get('permutation'))} permutations", f"Seed {k.get('randomState')}"]
    elif tool_id == "neighborhoods":
        out += [f"From {k.get('phenotype')}",
                _radius_chip(k, work) if k.get("graphMethod") == "radius" else f"{k.get('knn')} nearest",
                f"{k.get('nDomains')} neighborhoods", f"Seed {k.get('randomState')}"]
    elif tool_id in ("composition", "expression"):
        out += [f"By {k.get('groupBy')}", "Per image"]
    elif tool_id == "name_groups":
        rename = k.get("rename") or {}
        n = sum(len(v) for v in rename.values())
        out += [f"From {k.get('fromColumn')}", f"{n} named as {len(rename)}"]
    elif tool_id == "markers":
        out += [f"By {k.get('groupBy')}", {"wilcoxon": "Wilcoxon"}.get(k.get("method"), "t-test")]
    elif tool_id == "celltypes":
        rows = ((k.get("phenotype") or {}).get("__frame__") or {}).get("rows") or []
        out += [f"{len(rows)} rule" + ("" if len(rows) == 1 else "s"), "Gates from Plexora"]
    elif tool_id == "compare":
        out += [f"By {k.get('groupBy')}", f"Share of {k.get('values')}", f"Each {k.get('replicateBy')} once"]
    elif tool_id == "normalize":
        scaled = next((s.get("kwargs") or {} for s in eff.get("before") or () if s.get("key") == "pp.normalizeTotal"),
                      {})
        out += [f"{float(scaled.get('targetSum') or 10000):,.0f} counts per cell", "log1p",
                f"Into layer {LOG_LAYER}"]
    elif tool_id == "pca":
        pca = next((s.get("kwargs") or {} for s in eff.get("before") or () if s.get("key") == "pp.pca"), {})
        out += [f"{pca.get('nComps')} components", "Variable genes" if pca.get("useHighlyVariable") else "Every gene",
                f"{k.get('nNeighbors')} neighbors"]
    elif tool_id == "svg":
        out += ["Geary's C" if k.get("method") == "geary" else "Moran's I",
                _radius_chip(k, work) if k.get("graphMethod") == "radius" else f"{k.get('knn')} nearest",
                f"{len(k['genes'])} genes" if k.get("genes") else "Every gene"]
    elif tool_id == "quality":
        word = {7.0: "Lenient", 5.0: "Standard", 3.0: "Strict"}.get(float(k.get("nDeviations", 5.0)), "Custom")
        out += [word, "Edge cells flagged" if k.get("dropEdge") else "Edge cells kept"]
    for name, value in (eff.get("extra") or {}).items():
        out.append(f"{name} = {value!r}")
    return out
