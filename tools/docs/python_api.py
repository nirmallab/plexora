"""Dump Plexora's public Python surface to JSON.

This is the only module in tools/docs that imports `plexora`, and it is meant
to run in a child process (`sync_docs.py` starts it with `config.child_env`):
`import plexora` builds the Flask app, discovers plugins and resolves the data
root, none of which belongs in the generator's own interpreter.

    python -m tools.docs.python_api --out model.json

Everything is read from the live objects and the source files: signatures via
`inspect.signature` (annotations kept as the strings `from __future__ import
annotations` makes them, never evaluated), line ranges via
`inspect.getsourcelines`, constant and field documentation from `#:` comment
blocks in the source. Nothing here renders; mdx.py does that.
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import enum
import importlib
import inspect
import json
import sys
import types
from functools import lru_cache
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

#: Container protocol methods documented when a class defines them itself.
PUBLIC_DUNDERS = ("__len__", "__iter__", "__contains__", "__getitem__", "__call__")


# -- names ---------------------------------------------------------------


def public_names() -> dict[str, tuple[object, str]]:
    """`{qualname: (object, defining module)}` for every candidate public name."""
    import plexora
    import plexora.api as api
    import plexora.api.plugin as plugin_module

    found: dict[str, tuple[object, str]] = {}
    found["plexora.view"] = (plexora.view, "plexora")
    for name, module in plexora._PUBLIC_API.items():
        # Through the defining module, never `plexora.<name>`: that would
        # resolve (and cache) the telemetry-counting wrapper.
        found[f"plexora.{name}"] = (getattr(importlib.import_module(module), name), module)
    for name in api.__all__:
        obj = getattr(api, name)
        found[f"plexora.api.{name}"] = (obj, defining_module(obj, "plexora.api"))
        if isinstance(obj, types.ModuleType):
            for member, value in module_members(obj):
                found[f"plexora.api.{name}.{member}"] = (value, obj.__name__)
    for member, value in module_members(plugin_module):
        found[f"plexora.api.plugin.{member}"] = (value, "plexora.api.plugin")
    return found


def defining_module(obj, fallback: str) -> str:
    module = getattr(obj, "__module__", None)
    if isinstance(obj, types.ModuleType):
        return obj.__name__
    if module and not isinstance(obj, (str, int, float, tuple, frozenset, set, list, dict)):
        return module
    return fallback


def module_members(module: types.ModuleType):
    """Public functions and classes defined in `module`, and its UPPERCASE
    module-level constants, in source order."""
    tree = module_ast(module.__name__)
    order = {}
    constants = set()
    for index, node in enumerate(tree.body if tree else ()):
        for target in assigned_names(node):
            order.setdefault(target, index)
            if target.isupper():
                constants.add(target)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            order.setdefault(node.name, index)
    out = []
    for name, value in vars(module).items():
        if name.startswith("_"):
            continue
        if isinstance(value, (types.FunctionType, type)) and getattr(value, "__module__", None) == module.__name__:
            out.append((name, value))
        elif name in constants:
            out.append((name, value))
    out.sort(key=lambda item: order.get(item[0], 10**6))
    return out


# -- source --------------------------------------------------------------


@lru_cache(maxsize=None)
def module_ast(module_name: str):
    path = module_path(module_name)
    if path is None:
        return None
    return ast.parse(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=None)
def module_lines(module_name: str) -> tuple[str, ...]:
    path = module_path(module_name)
    return tuple(path.read_text(encoding="utf-8").splitlines()) if path else ()


def module_path(module_name: str) -> Path | None:
    module = sys.modules.get(module_name) or importlib.import_module(module_name)
    file = getattr(module, "__file__", None)
    return Path(file) if file else None


def rel(path: str | Path) -> str:
    return Path(path).resolve().relative_to(REPO).as_posix()


def assigned_names(node) -> list[str]:
    if isinstance(node, ast.Assign):
        return [t.id for t in node.targets if isinstance(t, ast.Name)]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id]
    return []


def comment_block_above(lines: tuple[str, ...], lineno: int) -> str:
    """The `#:` comment block ending on the line above 1-based `lineno`."""
    block = []
    index = lineno - 2
    while index >= 0:
        stripped = lines[index].strip()
        if stripped.startswith("#:"):
            block.append(stripped[2:][1:] if stripped[2:3] == " " else stripped[2:])
            index -= 1
            continue
        if stripped.startswith("@"):
            index -= 1
            continue
        break
    return "\n".join(reversed(block)).strip()


def find_assignment(module_name: str, name: str, depth: int = 0):
    """`(module_name, ast node)` where `name` is assigned at module level,
    following `from x import name` re-exports."""
    tree = module_ast(module_name)
    if tree is None or depth > 4:
        return None
    for node in tree.body:
        if name in assigned_names(node):
            return module_name, node
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if (alias.asname or alias.name) == name:
                    return find_assignment(node.module, alias.name, depth + 1)
    return None


def locate(obj, module_name: str, name: str):
    """`{"path", "start", "end"}` for an object, or None."""
    target = inspect.unwrap(obj) if callable(obj) else obj
    if isinstance(target, (classmethod, staticmethod)):
        target = target.__func__
    if isinstance(target, property):
        target = target.fget
    if isinstance(target, (types.FunctionType, type, types.ModuleType)):
        try:
            file = inspect.getsourcefile(target)
            lines, start = inspect.getsourcelines(target)
        except (OSError, TypeError):
            return None
        if isinstance(target, types.ModuleType):
            return {"path": rel(file), "start": 1, "end": len(lines)}
        return {"path": rel(file), "start": start, "end": start + len(lines) - 1}
    found = find_assignment(module_name, name)
    if found:
        mod, node = found
        return {"path": rel(module_path(mod)), "start": node.lineno, "end": node.end_lineno}
    return None


def constant_doc(module_name: str, name: str) -> str:
    found = find_assignment(module_name, name)
    if not found:
        return ""
    mod, node = found
    return comment_block_above(module_lines(mod), node.lineno)


# -- signatures ----------------------------------------------------------


def annotation_text(annotation) -> str | None:
    if annotation is inspect.Parameter.empty or annotation is inspect.Signature.empty:
        return None
    if isinstance(annotation, str):
        return annotation
    return inspect.formatannotation(annotation)


def default_text(default) -> str | None:
    """How a default reads in a signature, or None when it cannot be shown."""
    if default is inspect.Parameter.empty:
        return None
    if default is dataclasses.MISSING:
        return None
    if isinstance(default, (types.FunctionType, types.BuiltinFunctionType, type)):
        return default.__name__
    if isinstance(default, enum.Enum):
        return f"{type(default).__name__}.{default.name}"
    if isinstance(default, (str, int, float, bool, type(None), tuple, frozenset, list, dict, bytes)):
        text = repr(default)
        return text if len(text) <= 60 else None
    text = repr(default)
    if text.startswith("<"):
        return None
    return text


KIND_NAMES = {
    inspect.Parameter.POSITIONAL_ONLY: "positional-only",
    inspect.Parameter.POSITIONAL_OR_KEYWORD: "positional-or-keyword",
    inspect.Parameter.VAR_POSITIONAL: "var-positional",
    inspect.Parameter.KEYWORD_ONLY: "keyword-only",
    inspect.Parameter.VAR_KEYWORD: "var-keyword",
}


def describe_signature(obj, drop_first: bool = False, dataclass_fields=None):
    """`(params, returns, diagnostics)` for a callable."""
    try:
        signature = inspect.signature(obj)
    except (TypeError, ValueError):
        return None, None, []
    params = []
    diagnostics = []
    for index, param in enumerate(signature.parameters.values()):
        if drop_first and index == 0:
            continue
        default = param.default
        factory = None
        if dataclass_fields and param.name in dataclass_fields:
            f = dataclass_fields[param.name]
            if f.default_factory is not dataclasses.MISSING:
                factory = f"{getattr(f.default_factory, '__name__', 'factory')}()"
        text = factory or default_text(default)
        if text is None and default is not inspect.Parameter.empty and not factory:
            diagnostics.append(("unrepresentable_default", param.name))
            text = "..."
        params.append({
            "name": param.name,
            "kind": KIND_NAMES[param.kind],
            "annotation": annotation_text(param.annotation),
            "default": text,
            "has_default": default is not inspect.Parameter.empty,
            "hidden": param.name.startswith("_"),
        })
    return params, annotation_text(signature.return_annotation), diagnostics


def render_signature(name: str, params, returns) -> str:
    """One-line signature text: `name(a, *, b=None) -> str`.

    Hidden parameters (a leading underscore) are left out; a `*` separator
    is inserted before the first keyword-only parameter when there is no
    `*args`, and a `/` after the last positional-only one.
    """
    if params is None:
        return name
    parts = []
    visible = [p for p in params if not p["hidden"]]
    saw_star = any(p["kind"] == "var-positional" for p in visible)
    for index, p in enumerate(visible):
        if p["kind"] == "keyword-only" and not saw_star:
            parts.append("*")
            saw_star = True
        text = p["name"]
        if p["kind"] == "var-positional":
            text = "*" + text
        elif p["kind"] == "var-keyword":
            text = "**" + text
        if p["annotation"]:
            text += f": {p['annotation']}"
        if p["has_default"]:
            text += (" = " if p["annotation"] else "=") + (p["default"] or "...")
        parts.append(text)
        if p["kind"] == "positional-only" and (index + 1 == len(visible) or visible[index + 1]["kind"] != "positional-only"):
            parts.append("/")
    text = f"{name}({', '.join(parts)})"
    if returns:
        text += f" -> {returns}"
    return text


# -- classes -------------------------------------------------------------


def class_attribute_docs(cls) -> dict[str, str]:
    """`#:` blocks above (or a string literal below) class-body assignments."""
    try:
        file = inspect.getsourcefile(cls)
        source_lines, start = inspect.getsourcelines(cls)
    except (OSError, TypeError):
        return {}
    text = "".join(source_lines)
    import textwrap

    try:
        tree = ast.parse(textwrap.dedent(text))
    except SyntaxError:
        return {}
    node = tree.body[0]
    all_lines = tuple(Path(file).read_text(encoding="utf-8").splitlines())
    docs = {}
    body = getattr(node, "body", [])
    for index, stmt in enumerate(body):
        names = assigned_names(stmt)
        if not names:
            continue
        lineno = start + stmt.lineno - 1
        doc = comment_block_above(all_lines, lineno)
        if not doc and index + 1 < len(body):
            following = body[index + 1]
            if isinstance(following, ast.Expr) and isinstance(getattr(following, "value", None), ast.Constant) \
                    and isinstance(following.value.value, str):
                doc = inspect.cleandoc(following.value.value)
        for name in names:
            docs[name] = doc
    return docs


def describe_member(cls, name, module_name):
    raw = inspect.getattr_static(cls, name)
    record = {"name": name}
    if isinstance(raw, staticmethod):
        func, record["kind"], drop = raw.__func__, "staticmethod", False
    elif isinstance(raw, classmethod):
        func, record["kind"], drop = raw.__func__, "classmethod", True
    elif isinstance(raw, property):
        func, record["kind"], drop = raw.fget, "property", True
    elif isinstance(raw, types.FunctionType):
        func, record["kind"], drop = raw, "method", True
    else:
        func = None
        record["kind"] = "attribute"
    if func is not None:
        params, returns, diags = describe_signature(func, drop_first=drop)
        if record["kind"] == "property":
            record["annotation"] = returns
            record["signature"] = name
        else:
            record["params"] = params
            record["returns"] = returns
            record["signature"] = render_signature(name, params, returns)
        record["doc"] = inspect.cleandoc(func.__doc__) if func.__doc__ else ""
        record["source"] = locate(func, module_name, name)
        record["diagnostics"] = diags
    else:
        record["value"] = default_text(raw) if not callable(raw) else None
        record["doc"] = ""
    return record


def describe_class(cls, module_name):
    record = {}
    fields = {f.name: f for f in dataclasses.fields(cls)} if dataclasses.is_dataclass(cls) else {}
    attribute_docs = class_attribute_docs(cls)
    defines_init = "__init__" in vars(cls) or bool(fields)
    if defines_init:
        params, _returns, diags = describe_signature(cls, dataclass_fields=fields)
        record["params"] = params
        record["signature"] = render_signature(cls.__name__, params, None)
        record["diagnostics"] = diags
    else:
        record["params"] = None
        record["signature"] = cls.__name__
        record["diagnostics"] = []
    init = vars(cls).get("__init__")
    record["init_doc"] = inspect.cleandoc(init.__doc__) if isinstance(init, types.FunctionType) and init.__doc__ else ""
    record["bases"] = [b.__name__ for b in cls.__bases__ if b is not object]
    record["is_exception"] = issubclass(cls, BaseException)
    record["is_dataclass"] = bool(fields)
    record["fields"] = []
    for name, f in fields.items():
        if not f.init and name.startswith("_"):
            continue
        default = None
        if f.default is not dataclasses.MISSING:
            default = default_text(f.default)
        elif f.default_factory is not dataclasses.MISSING:
            default = f"{getattr(f.default_factory, '__name__', 'factory')}()"
        record["fields"].append({
            "name": name,
            "annotation": f.type if isinstance(f.type, str) else annotation_text(f.type),
            "default": default,
            "required": f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING,
            "doc": attribute_docs.get(name, ""),
            "hidden": name.startswith("_"),
        })
    members = []
    for name in vars(cls):
        if name in fields:
            continue
        if name.startswith("_") and name not in PUBLIC_DUNDERS:
            continue
        member = describe_member(cls, name, module_name)
        if member["kind"] == "attribute":
            member["doc"] = attribute_docs.get(name, "")
        members.append(member)
    record["members"] = members
    return record


# -- symbols -------------------------------------------------------------


def describe(qualname: str, obj, module_name: str) -> dict:
    name = qualname.rsplit(".", 1)[1]
    record = {"qualname": qualname, "name": name, "module": module_name}
    if isinstance(obj, types.ModuleType):
        record["kind"] = "module"
        record["doc"] = inspect.cleandoc(obj.__doc__) if obj.__doc__ else ""
        record["source"] = locate(obj, module_name, name)
        record["signature"] = name
        return record
    if isinstance(obj, type):
        record["kind"] = "exception" if issubclass(obj, BaseException) else "class"
        record["doc"] = inspect.cleandoc(obj.__doc__) if obj.__doc__ and obj.__doc__ is not type.__doc__ else ""
        if dataclasses.is_dataclass(obj) and record["doc"].startswith(f"{obj.__name__}("):
            record["doc"] = ""  # the auto-generated dataclass docstring
        record.update(describe_class(obj, obj.__module__))
        record["source"] = locate(obj, module_name, name)
        return record
    if callable(obj):
        target = inspect.unwrap(obj)
        record["kind"] = "function"
        record["doc"] = inspect.cleandoc(target.__doc__) if target.__doc__ else ""
        params, returns, diags = describe_signature(target)
        record["params"] = params
        record["returns"] = returns
        record["signature"] = render_signature(name, params, returns)
        record["diagnostics"] = diags
        record["source"] = locate(target, getattr(target, "__module__", module_name), name)
        return record
    record["kind"] = "constant"
    record["doc"] = constant_doc(module_name, name)
    record["type"] = type(obj).__name__
    record["value"] = constant_value(obj)
    record["signature"] = name
    record["source"] = locate(obj, module_name, name)
    return record


def constant_value(obj):
    if isinstance(obj, (frozenset, set)):
        return "{" + ", ".join(repr(v) for v in sorted(obj, key=repr)) + "}" if obj else "frozenset()"
    text = repr(obj)
    return text if len(text) <= 400 else text[:397] + "..."


def collect_formats() -> dict:
    """The importer's own suffix lists (see tools/docs/formats.py)."""
    from plexora.server.models import import_proposal
    from plexora.server.models.adapters import _SUFFIX_TYPES
    from plexora.server.providers.base import RESOURCE_KINDS
    from plexora.server.utils.brightfield import BRIGHTFIELD_ONLY_SUFFIXES

    return {
        "image_suffixes": sorted(import_proposal.IMAGE_SUFFIXES),
        "table_suffix_types": dict(sorted(_SUFFIX_TYPES.items())),
        "brightfield_only": sorted(BRIGHTFIELD_ONLY_SUFFIXES),
        "resource_kinds": list(RESOURCE_KINDS),
    }


def build_model() -> dict:
    names = public_names()
    identities: dict[int, list[str]] = {}
    symbols = {}
    for qualname, (obj, module_name) in names.items():
        symbols[qualname] = describe(qualname, obj, module_name)
        if not isinstance(obj, (str, int, float, bool, type(None))):
            identities.setdefault(id(obj), []).append(qualname)
    for group in identities.values():
        if len(group) > 1:
            for qualname in group:
                symbols[qualname]["same_as"] = sorted(q for q in group if q != qualname)
    return {"symbols": dict(sorted(symbols.items())), "formats": collect_formats()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    model = build_model()
    Path(args.out).write_text(json.dumps(model, indent=1, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    main()
