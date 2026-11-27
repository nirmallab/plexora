"""Which tools a notebook lists, in which words, with which defaults and in which order -- as SCIMAP Pro says.

Plexora orchestrates; SCIMAP Pro decides. For the notebook's data this module
asks SCIMAP Pro three things, once per file state (`advice`):

    sm.ai.roadmap   which catalogue analyses fit the data: a tool whose
                    analysis is `not_applicable` is not listed
    sm.ai.recipe    the standard workflow for the data (st-cell, st-spot,
                    imaging-single ...): each counts step's default arguments
                    and the order "Suggested next" offers them in
    sm.ai.facts     what the data is (inside the roadmap's answer): the words

Plexora's part is only the wiring -- which tool carries out which catalogue
analysis (`ANALYSIS`) and makes which call (`FUNCTIONS`), how a field maps to
an argument (`FIELD_ARGS`) -- and the wording (`TEXT`, `lexicon`). On a SCIMAP
Pro without these functions (0.1.28) nothing is hidden, nothing is reordered,
and the counts tools say they need a newer Scimap Pro.
"""

from __future__ import annotations

import copy
import json
import os
import threading

from plexora.plugins.scimap.server import facts, lexicon, tools

#: The catalogue analysis each tool carries out: `sm.ai.roadmap` says whether it fits the data. A tool with none
#: is always listed.
ANALYSIS = {"gating": "annotate-cells-gating", "celltypes": "annotate-cells-gating",
            "normalize": "normalize-counts", "pca": "reduce-dimensions",
            "cluster": "cluster-expression", "markers": "rank-markers", "umap": "embed-umap",
            "name_groups": "rename-labels", "composition": "describe-composition",
            "distance": "measure-distances", "interaction": "test-cooccurrence",
            "neighborhoods": "detect-domains", "svg": "spatially-variable-features",
            "compare": "compare-composition", "quality": "spatial-qc"}
#: The tool that makes each call a recipe step names: how a recipe's order becomes "Suggested next".
FUNCTIONS = {"pp.normalizeTotal": "normalize", "pp.transform": "normalize", "pp.log1p": "normalize",
             "pp.highlyVariableGenes": "pca", "pp.pca": "pca", "pp.neighbors": "pca",
             "tl.cluster": "cluster", "sc.rankFeatures": "markers", "tl.umap": "umap",
             "sp.spatiallyVariableFeatures": "svg", "sp.spatialDomains": "neighborhoods",
             "sp.spatialCooccurrence": "interaction", "sp.spatialDistance": "distance",
             "tl.compareGroups": "compare", "sp.filterSpatialOutliers": "quality"}
#: A tool's field -> (the recipe call it sets, that call's argument), for the defaults a form opens with.
#: `"@name"` reads the recipe's parameters instead (`maxDistance`: a neighbourhood's reach in the coordinates'
#: own units, from the cells' spacing).
FIELD_ARGS = {
    "normalize": {"target": ("pp.normalizeTotal", "targetSum")},
    "pca": {"components": ("pp.pca", "nComps"), "variable": ("pp.pca", "useHighlyVariable"),
            "neighbors": ("pp.neighbors", "nNeighbors")},
    "cluster": {"resolution": ("tl.cluster", "leidenResolution")},
    "svg": {"method": ("sp.spatiallyVariableFeatures", "method"),
            "graph": ("sp.spatiallyVariableFeatures", "graphMethod"),
            "knn": ("sp.spatiallyVariableFeatures", "knn"), "radius": (None, "@maxDistance")},
    "interaction": {"radius": (None, "@maxDistance")},
    "neighborhoods": {"radius": (None, "@maxDistance")},
    "quality": {"radius": (None, "@maxDistance")},
}
#: The tools added for counts data. On other data they are listed only when a SCIMAP Pro roadmap says they fit
#: (its catalogue lets spatially variable features apply to an image's markers); with no roadmap, not at all.
COUNTS_ONLY = frozenset({"normalize", "pca", "svg"})

#: Each tool's text with the words that change between kinds of data as `{slots}`. Filled for imaging, every
#: entry of a tool listed for imaging reads exactly as the tool table does (a test holds them to it).
TEXT = {
    "cluster": {"summary": "Groups {units} by their {feature} expression.",
                "fields": {"markers": {"label": "{Features}"}}},
    "umap": {"summary": "A two-dimensional map of the {units} by expression."},
    "markers": {"summary": "The {features} that set each cluster apart from the rest: what to read before "
                           "naming it.",
                "fields": {"method": {"help": "Ranks the {features}; with {units} as the unit it describes the "
                                              "groups, it does not test them."}}},
    "composition": {"name": "{Unit} composition", "summary": "How many {units} of each kind, per {sample}.",
                    "fields": {"source": {"label": "{Unit} labels"}}},
    "expression": {"name": "{Feature} expression", "summary": "Mean {feature} expression of each group.",
                   "fields": {"markers": {"label": "{Features}"}}},
    "distance": {"summary": "How far each kind of {unit} is from the nearest {unit} of each other kind.",
                 "fields": {"source": {"label": "{Unit} labels"}}},
    "interaction": {"summary": "Which kinds of {unit} are neighbors more, or less, often than by chance.",
                    "fields": {"source": {"label": "{Unit} labels"}}},
    "neighborhoods": {"summary": "Finds recurring mixtures of {unit} kinds around each {unit}.",
                      "fields": {"source": {"label": "{Unit} labels"}}},
    "compare": {"summary": "Whether the share of each {identity} differs between groups of samples, counting "
                           "each sample once.",
                "fields": {"source": {"label": "{Unit} labels"}}},
    "quality": {"summary": "Flags {units} whose position looks wrong, such as isolated specks and crowded "
                           "pile-ups. Nothing is deleted.",
                "fields": {"radius": {"help": "How far around each {unit} its neighbors are counted."},
                           "strict": {"help": "How far from the {sample}'s usual crowding a {unit} must be to be "
                                              "flagged."},
                           "edge": {"label": "Flag edge {units}",
                                    "help": "Also flag {units} on the outer edge of the tissue."}}},
    "normalize": {"summary": "Scales each {unit} to the same total count, then logs it, into a layer every later "
                             "step reads.",
                  "fields": {"target": {"label": "Counts per {unit}",
                                        "help": "Each {unit}'s counts are scaled to add up to this before the log."}}},
    "pca": {"summary": "Reduces the {features} to their main axes of variation and links each {unit} to the "
                       "{units} most like it: the graph clustering and UMAP read."},
    "svg": {"name": "Spatially variable {features}",
            "summary": "Ranks the {features} whose expression is patterned in the tissue, each {sample} on its own.",
            "fields": {"markers": {"label": "{Features}", "help": "Leave empty to rank every {feature}."}}},
}


# -- SCIMAP Pro's advice for one notebook ----------------------------------------------------------------------

_cache: dict = {}
_lock = threading.Lock()


def advice(document, work) -> dict:
    """`{facts, states, recipe}` for the notebook's data, from SCIMAP Pro; cached on the data's file, its state,
    Plexora's records and the person's correction. `states` and `recipe` are empty when SCIMAP Pro has not read
    the data (no file yet, or a release without `sm.ai.roadmap`)."""
    infos = _infos(document)
    context = facts.context_for(infos, override=document.get("data_type"))
    path = _data_path(work, infos)
    try:
        from plexora.api import records

        stamp = records.records_stamp()
    except Exception:  # noqa: BLE001
        stamp = None
    mtime = _mtime(path)
    key = json.dumps([path, mtime, context, stamp], sort_keys=True, default=str)
    with _lock:
        if key in _cache:
            return _cache[key]
    found = _read(path, context, work, infos)
    with _lock:
        if len(_cache) > 32:
            _cache.clear()
        _cache[key] = found
    return found


def _read(path, context, work, infos) -> dict:
    from plexora.plugins.scimap.server import install

    counts = {len(i.get("markers") or ()) for i in infos if i.get("markers")}
    n_features = len((work or {}).get("markers") or ()) or (counts.pop() if len(counts) == 1 else None)
    out = {"facts": None, "states": {}, "recipe": {}}
    if path and install.supports("advice"):
        try:
            import scimappro as sm

            resolved = sm.ai.roadmap(path, context=context)
            out["facts"] = facts.reduce(resolved.get("dataset_facts") or {})
            out["states"] = {k: v.get("state") for k, v in (resolved.get("states") or {}).items()}
            out["recipe"] = _recipe(sm, path, context)
        except Exception:  # noqa: BLE001 -- the notebook still opens, with every tool listed
            out["facts"] = None
    if out["facts"] is None:
        out["facts"] = facts.detect(None, context=context, n_features=n_features)
    return out


_recipes: dict = {}


def _recipe(sm, path, context) -> dict:
    """`sm.ai.recipe`, kept per file and context: the workflow a dataset gets does not change step by step, and
    reading its parameters measures the cells' spacing."""
    key = json.dumps([path, context], sort_keys=True, default=str)
    if key not in _recipes:
        if len(_recipes) > 16:
            _recipes.clear()
        _recipes[key] = sm.ai.recipe(path, context=context)
    return _recipes[key]


def _data_path(work, infos) -> str | None:
    """The AnnData SCIMAP Pro reads: the working file once prepared, else the project's own (a 10x conversion)."""
    if work and work.get("exists") and work.get("path"):
        return str(work["path"])
    tables = [str(i.get("table") or i.get("src") or "") for i in infos]
    if len(tables) == 1 and tables[0].endswith(".h5ad") and os.path.exists(tables[0]):
        return tables[0]
    return None


def _mtime(path):
    try:
        return os.stat(path).st_mtime_ns if path else None
    except OSError:
        return None


# -- what the page and the tools are given ---------------------------------------------------------------------

def hidden(said) -> frozenset:
    """The tools SCIMAP Pro says do not fit the data: their analysis is `not_applicable`. With no roadmap, the
    counts-only tools on data that is not counts."""
    states = (said or {}).get("states") or {}
    if states:
        return frozenset(t for t, a in ANALYSIS.items() if states.get(a) == "not_applicable")
    mode = ((said or {}).get("facts") or {}).get("mode")
    return frozenset() if str(mode or "").startswith("st-") else COUNTS_ONLY


def catalog(said) -> list:
    """`tools.describe()` for this notebook: the tools that fit, in its words, opening with SCIMAP Pro's
    defaults for its data."""
    mode = ((said or {}).get("facts") or {}).get("mode")
    leave_out = hidden(said)
    defaults = field_defaults_all(said)
    out = []
    for category in tools.describe():
        listed = [_worded(tool, mode, defaults.get(tool["id"]) or {}) for tool in category["tools"]
                  if tool["id"] not in leave_out]
        if listed:
            out.append({**category, "tools": listed})
    return out


def _worded(tool, mode, defaults):
    text = TEXT.get(tool["id"]) or {}
    if not text and not defaults:
        return tool
    tool = copy.deepcopy(tool)
    for key in ("name", "summary"):
        if key in text:
            tool[key] = lexicon.fill(text[key], mode)
    for field in tool.get("fields") or ():
        for key, value in (text.get("fields") or {}).get(field["name"], {}).items():
            field[key] = lexicon.fill(value, mode)
        if field["name"] in defaults:
            field["default"] = defaults[field["name"]]
    return tool


def recipe_args(said, function) -> dict:
    """The arguments SCIMAP Pro's workflow for this data passes `function`, placeholders it could not fill left
    out; `{}` when the workflow has no such step."""
    for steps in (((said or {}).get("recipe") or {}).get("plans") or {}).values():
        for step in steps:
            if step.get("function") == function:
                return {k: v for k, v in (step.get("arguments") or {}).items()
                        if not (isinstance(v, str) and v.startswith("{") and v.endswith("}"))}
    return {}


def field_defaults(said, tool_id) -> dict:
    """`{field: value}` a tool's form opens with on this data, from the workflow's arguments and parameters."""
    parameters = ((said or {}).get("recipe") or {}).get("parameters") or {}
    out = {}
    for name, (function, arg) in FIELD_ARGS.get(tool_id, {}).items():
        if arg.startswith("@"):
            value = parameters.get(arg[1:])
        else:
            value = recipe_args(said, function).get(arg)
        if value is not None:
            out[name] = value
    return out


def field_defaults_all(said) -> dict:
    return {tool_id: field_defaults(said, tool_id) for tool_id in FIELD_ARGS}


def next_steps(said) -> dict | None:
    """"Suggested next" on counts data, in the order SCIMAP Pro's workflow runs its steps; None keeps the page's
    own map (imaging, or no workflow read)."""
    mode = ((said or {}).get("facts") or {}).get("mode")
    plans = ((said or {}).get("recipe") or {}).get("plans") or {}
    if not str(mode or "").startswith("st-") or not plans:
        return None
    order = []
    for steps in plans.values():
        for step in steps:
            tool = FUNCTIONS.get(step.get("function"))
            if tool and tool not in order:
                order.append(tool)
    out = {"prepare": order[:2]}
    for i, tool in enumerate(order):
        out[tool] = order[i + 1:i + 4]
    return out


#: What `work_facts` adds to the working-file record the tools read, under these names (the record's own `mode`
#: says project or dataset).
WORK_KEYS = {"data_mode": "mode", "coordinate_unit": "coordinate_unit", "microns_per_unit": "microns_per_unit",
             "panel": "panel", "raw_counts": "raw_counts", "n_features": "n_features"}


def work_facts(document, work) -> dict:
    """The facts a step needs (`WORK_KEYS`) for the working-file record; `{}` when nothing can be read, which
    leaves every step as it was for imaging."""
    try:
        found = advice(document, work)["facts"] or {}
    except Exception:  # noqa: BLE001
        return {}
    return {name: found.get(key) for name, key in WORK_KEYS.items()}


def _infos(document) -> list:
    """Plexora's record of each project the notebook reads, as `records.project_info` gives it."""
    context = document.get("context") or {}
    names = list(context.get("projects") or ()) if context.get("kind") == "dataset" else [context.get("id")]
    out = []
    for name in names:
        if not name:
            continue
        try:
            from plexora.api import records

            out.append(records.project_info(name))
        except Exception:  # noqa: BLE001 -- a project that cannot be read adds nothing
            continue
    return out


__all__ = ["ANALYSIS", "COUNTS_ONLY", "FIELD_ARGS", "FUNCTIONS", "TEXT", "advice", "catalog", "field_defaults",
           "hidden", "next_steps", "recipe_args", "work_facts"]
