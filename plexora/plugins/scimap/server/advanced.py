"""Advanced parameters: anything else a Scimap Pro function takes, by name.

A step's form sets the arguments that matter most; the Advanced box takes the
rest as `name = value` lines in Python, one per line, for each function the
step calls (its analysis, and the `pl.*` functions that draw its figures).

Checked against the function's own signature, from Scimap Pro's catalogue
(`ai/registry.json`, read without importing Scimap Pro), so a typo is caught
as it is typed and never reaches a run:

* a name the function does not take is refused, with the closest name it
  does take ("Did you mean leidenFlavor?"); a function that passes extra
  keywords on (`**kwargs`) takes any name, and the box says so;
* a name the form above already sets is refused with the field to change --
  two places setting one argument would leave the user unsure which won;
* a name the notebook supplies itself (the data, the image column, output
  switches) is refused;
* a value must be a Python literal (`30`, `"text"`, `True`, `None`,
  `["CD3", "CD8"]`); ranges are left to Scimap Pro, which says what it
  refuses when the step runs.

The text is saved with the step's version and the parsed values are passed in
the call, so an exported script writes them as they ran.
"""

from __future__ import annotations

import ast
import difflib
import re

from plexora.plugins.scimap.server import catalog

DOCS = "https://pro.scimap.xyz/api/{ns}/{fn}/"

#: Names the notebook passes itself, in every call.
NOTEBOOK = frozenset(catalog.SUPPLIED) | {"adata", "data", "maxWorkers", "recompute",
                                           "fileName", "saveDir", "dpi"}

#: The form's fields, by the Scimap Pro argument each one sets.
FIELD_ARGS = {
    "cluster": {"method": "method", "leidenResolution": "resolution", "k": "k",
                "subsetMarkers": "markers", "label": "label", "randomState": "seed",
                "fitSample": "fit", "layer": "layer", "log": "layer", "useGraph": None},
    "umap": {"nNeighbors": "neighbors", "minDist": "min_dist", "label": "label",
             "randomState": "seed", "fitSample": "fit", "layer": "layer", "log": "layer",
             "useGraph": None},
    "name_groups": {"rename": "names", "fromColumn": "source", "toColumn": "label"},
    "markers": {"groupBy": "source", "method": "method", "label": "label", "layer": None},
    "celltypes": {"phenotype": "rules", "gate": None, "label": "label", "layer": None},
    "compare": {"groupBy": "by", "what": None, "values": "source", "replicateBy": "replicate",
                "reference": "reference", "label": "label"},
    "quality": {"metrics": None, "nDeviations": "strict", "dropEdge": "edge", "removeCells": None,
                "label": "label"},
    "distance": {"phenotype": "source", "summary": "statistic", "label": "label",
                 "xCoordinate": None, "yCoordinate": None},
    "interaction": {"phenotype": "source", "method": "graph", "graphMethod": "graph", "radius": "radius",
                    "knn": "knn", "permutation": "permutations", "nPerms": "permutations", "randomState": "seed", "label": "label",
                    "xCoordinate": None, "yCoordinate": None},
    "neighborhoods": {"phenotype": "source", "graphMethod": "graph", "radius": "radius",
                      "knn": "knn", "nDomains": "n", "randomState": "seed", "label": "label",
                      "method": None, "xCoordinate": None, "yCoordinate": None},
}

_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*$")


def docs_url(key) -> str:
    ns, fn = str(key).split(".", 1)
    return DOCS.format(ns=ns, fn=fn)


def functions(tool_id) -> list:
    """`[{key, role, docs}]`: the functions a step calls, analysis first."""
    from plexora.plugins.scimap.server import tools, views

    tool = tools.TOOLS.get(tool_id) or {}
    out = []
    if tool.get("call"):
        out.append({"key": tool["call"], "role": "Runs the analysis", "docs": docs_url(tool["call"])})
    # Every plotting function a view of this step can call, so a line for
    # pl.violin applies when the plot is switched to its violins.
    seen = {}
    for view in views.offered(tool_id):
        if view.fn:
            seen.setdefault(f"pl.{view.fn}", []).append(view.name)
    for key, names in seen.items():
        out.append({"key": key, "role": "Draws the " + ", ".join(names).lower() + (" view" if len(names) == 1 else " views"),
                    "docs": docs_url(key)})
    return out


def signature(key) -> tuple[list, bool]:
    """`(names, takes_extra)` of a function, from the catalogue."""
    record = (catalog.load().get("functions") or {}).get(key) or {}
    names, extra = [], False
    for parameter in record.get("params") or []:
        name = parameter.get("name") or ""
        if name.startswith("**"):
            extra = True
        elif name.isidentifier():
            names.append(name)
    return names, extra


def parse(text) -> tuple[list, list]:
    """`([(line, name, value)], [error])` from the box's text. Blank lines and
    `#` comments are skipped."""
    rows, errors = [], []
    for number, raw in enumerate(str(text or "").splitlines(), start=1):
        line = raw.split("#", 1)[0] if not re.search(r"[\"'].*#", raw) else raw
        if not line.strip():
            continue
        match = _LINE.match(line)
        if not match:
            errors.append({"line": number, "message": "Write one name = value per line."})
            continue
        name, source = match.group(1), match.group(2)
        try:
            value = ast.literal_eval(source)
        except (ValueError, SyntaxError):
            errors.append({"line": number, "name": name,
                           "message": f"The value of {name} is not a Python value. "
                                      "Text goes in quotes: \"text\"."})
            continue
        rows.append((number, name, value))
    return rows, errors


def check(tool_id, key, text, form=None) -> dict:
    """`{ok, params, errors, notes}` for one function's box.

    `form` is `{argument: "the field's words"}` for the arguments the form
    sets now (from the step's effective call), so a conflict names the value
    the form holds."""
    from plexora.plugins.scimap.server import tools

    rows, errors = parse(text)
    names, extra = signature(key)
    known = set(names)
    field_of = FIELD_ARGS.get(tool_id, {}) if key == (tools.TOOLS.get(tool_id) or {}).get("call") else {}
    labels = {f["name"]: f["label"] for f in (tools.TOOLS.get(tool_id) or {}).get("fields", [])}
    form = form or {}
    params, notes, seen = {}, [], {}
    for number, name, value in rows:
        if name in seen:
            errors.append({"line": number, "name": name,
                           "message": f"{name} is already set on line {seen[name]}."})
            continue
        seen[name] = number
        if name in field_of or name in form:
            field = labels.get(field_of.get(name) or "", "")
            where = f"the {field} field above" if field else "the form above"
            shown = _shown(field, form.get(name))
            errors.append({"line": number, "name": name,
                           "message": f"{name} is set by {where}{shown}. Change it there."})
            continue
        if name in NOTEBOOK:
            errors.append({"line": number, "name": name,
                           "message": f"{name} is set by the notebook for every run."})
            continue
        if name not in known:
            # A field's own word ("resolution") for an argument it sets.
            by_word = {v: k for k, v in field_of.items() if v}
            if name in by_word:
                field = labels.get(name, name)
                arg = by_word[name]
                shown = _shown(field, form.get(arg))
                errors.append({"line": number, "name": name,
                               "message": f"{name} is not a {key} parameter, and the {field.lower()} "
                                          f"is set above{shown}, sent as {arg}. Change it there.",
                               "suggestion": None})
                continue
            if not extra:
                close = difflib.get_close_matches(name, names, n=1, cutoff=0.7)
                errors.append({"line": number, "name": name,
                               "message": f"{name} is not a parameter of {key}."
                                          + (f" Did you mean {close[0]}?" if close else ""),
                               "suggestion": close[0] if close else None})
                continue
            notes.append({"line": number, "name": name,
                          "message": f"{key} passes {name} on to the plotting library."})
        params[name] = value
    errors.sort(key=lambda e: e["line"])
    return {"ok": not errors, "params": params if not errors else {}, "errors": errors,
            "notes": notes, "count": len(rows)}


def for_step(tool_id, advanced, form=None) -> dict:
    """`{key: params}` for every function with a box, or ValueError naming the
    first problem -- a run never starts with an unchecked line."""
    out = {}
    for key, text in (advanced or {}).items():
        if not str(text or "").strip():
            continue
        result = check(tool_id, key, text, form if key == _call(tool_id) else None)
        if not result["ok"]:
            first = result["errors"][0]
            raise ValueError(f"Advanced parameters for {key}, line {first['line']}: {first['message']}")
        out[key] = result["params"]
    return out


def _shown(field, value):
    if value is None:
        return ""
    text = f"{value:g}" if isinstance(value, float) else (
        f"{len(value)} items" if isinstance(value, (list, tuple, dict)) else str(value))
    return f" ({field} {text})" if field else f" ({text})"


def _call(tool_id):
    from plexora.plugins.scimap.server import tools

    return (tools.TOOLS.get(tool_id) or {}).get("call")
