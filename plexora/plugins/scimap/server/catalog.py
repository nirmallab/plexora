"""SCIMAP Pro's function catalogue, read without importing SCIMAP Pro.

The plugin is loaded when Plexora starts, and importing `scimappro` there
would pull in the whole analysis library (and its licence banner) for a user
who may never open the Analysis panel. So the catalogue is read the way a
build tool would read it: `registry.json` is located through the import
system's finder -- which does not execute the package -- and parsed as data.

`toolName` must match `scimappro.ai._bridge.capabilities.toolName`;
`plexora/plugins/scimap/tests/test_capabilities.py` checks the names it makes.
"""

from __future__ import annotations

import functools
import importlib.util
import json
import re
from pathlib import Path

#: Namespaces the plugin offers. Readers (`io`) are Plexora's business, and
#: `hl` is the bridge's own client half.
OFFERED = ("pp", "sc", "sp", "tl", "cl", "pl")

#: Parameters the plugin supplies itself: the data is the project's table, the
#: image column and the image come from the project's binding.
SUPPLIED = ("data", "imageId", "subset", "verbose", "progress", "streamData",
            "sdataTable", "outputDir", "show", "interactive", "returnFig")

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def snake(name):
    return _CAMEL.sub("_", str(name)).lower()


def toolName(key):
    namespace, name = str(key).split(".", 1)
    return f"scimappro_{namespace}_{snake(name)}"


def registryPath():
    """Where `scimappro/ai/registry.json` is, or None when SCIMAP Pro is not
    installed. Never imports `scimappro`."""
    try:
        spec = importlib.util.find_spec("scimappro")
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.submodule_search_locations:
        return None
    for location in spec.submodule_search_locations:
        candidate = Path(location) / "ai" / "registry.json"
        if candidate.is_file():
            return candidate
    return None


@functools.lru_cache(maxsize=1)
def load():
    """The registry as a dict; `{}` when SCIMAP Pro is not installed."""
    path = registryPath()
    if path is None:
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def offered(registry=None):
    """`{key: record}` for every function the plugin exposes."""
    registry = registry if registry is not None else load()
    return {key: record for key, record in sorted((registry.get("functions") or {}).items())
            if record.get("ns") in OFFERED and not record.get("umbrellaOnly")
            and (record.get("provider") or "scimappro") == "scimappro"}


def purpose(record):
    parts = [record.get("summary") or ""]
    if record.get("use_when"):
        parts.append("Use when: " + "; ".join(record["use_when"][:3]))
    if record.get("not_when"):
        parts.append("Not when: " + "; ".join(record["not_when"][:2]))
    unit = record.get("experimental_unit") or {}
    if unit.get("unit"):
        parts.append(f"Experimental unit: {unit['unit']}.")
    return " ".join(p for p in parts if p)[:1500]


def parameters(record):
    """The function's own parameters a caller sets: `[(name, required,
    description, default)]`."""
    out = []
    for parameter in record.get("params") or []:
        name = parameter["name"]
        # `*args`/`**kwargs` are not something a form or a tool call can set,
        # and Plexora builds a keyword signature from these names.
        if name in SUPPLIED or not name.isidentifier():
            continue
        out.append((name, bool(parameter.get("required")),
                    (parameter.get("description") or "")[:500], parameter.get("default")))
    return out


def search(query, limit=8, registry=None):
    """A small lexical search, for the `scimappro.search` capability when
    SCIMAP Pro cannot be imported to use its own."""
    words = {w for w in re.findall(r"[a-z0-9]+", str(query).lower()) if len(w) > 2}
    scored = []
    for key, record in offered(registry).items():
        text = " ".join([key, record.get("summary") or "", " ".join(record.get("synonyms")
                                                                     or []),
                         " ".join(record.get("use_when") or [])]).lower()
        score = sum(text.count(word) for word in words)
        if score:
            scored.append((score, key))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [{"key": key, "tool": toolName(key),
             "summary": offered(registry)[key].get("summary")} for _, key in scored[:limit]]
