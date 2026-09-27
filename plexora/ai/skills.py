"""The runtime scientific skills: what they are, and whether they still hold.

A skill is a SKILL.md an agent reads before doing a kind of work -- when to use
it, the decision logic, which tools it may call, how to pick evidence, what it
may change and how to report. `skill_manifest.yaml` lists them with the tool
names each one relies on, and `validate()` checks those names against the live
capability registry, so a renamed tool breaks a test instead of an agent.
"""

from __future__ import annotations

import re
from pathlib import Path

SKILLS_DIR = Path(__file__).parent / "skills"
MANIFEST_PATH = Path(__file__).parent / "skill_manifest.yaml"

#: Headings every SKILL.md must carry, in this order.
REQUIRED_SECTIONS = (
    "When to use",
    "When not to use",
    "Required features",
    "Decision logic",
    "Tools",
    "Evidence",
    "Uncertainty",
    "Mutation policy",
    "Provenance",
    "Done when",
    "Failure modes",
)


def manifest() -> dict:
    if not MANIFEST_PATH.exists():
        return {"version": "0", "skills": []}
    import yaml

    with open(MANIFEST_PATH, encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {"version": "0", "skills": []}


def list_skills() -> list:
    out = []
    for entry in manifest().get("skills", []):
        path = SKILLS_DIR / entry["name"] / "SKILL.md"
        out.append({
            "name": entry["name"],
            "title": entry.get("title", entry["name"]),
            "description": entry.get("description", ""),
            "when": entry.get("when", ""),
            "tools": list(entry.get("tools", [])),
            "available": path.exists(),
            "uri": f"plexora://skill/{entry['name']}",
        })
    return out


def raw_skill(name: str) -> str:
    """The SKILL.md as written, placeholders and all. KeyError naming the
    skills that exist."""
    names = [entry["name"] for entry in manifest().get("skills", [])]
    path = SKILLS_DIR / str(name) / "SKILL.md"
    if name not in names or not path.exists():
        raise KeyError(f"no skill named {name!r}; available: {', '.join(names)}")
    return path.read_text(encoding="utf-8")


def read_skill(name: str) -> str:
    """The SKILL.md an agent reads: its `{{placeholders}}` filled from the
    code's own constants (`constants`)."""
    return render(raw_skill(name))


# -- placeholders --------------------------------------------------------------

#: `{{name.key}}` in a SKILL.md: a number the code owns, never restated.
PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][\w.]*)\s*\}\}")


def constants() -> dict:
    """What a skill's placeholders may name: the constants the server
    decides with. Plugins that are not installed contribute nothing."""
    from plexora.agent.sessions import budget

    from plexora.agent.limits import MAX_CHANNELS

    out = {"budget": dict(budget.UNIT_DEFAULT), "render": {"max_channels": MAX_CHANNELS}}
    try:
        from plexora.agent.evidence import collage
        from plexora.plugins.gating.server.autogate import schemas
    except ImportError:  # pragma: no cover - the gating plugin is always shipped
        return out
    out["ENGINE"] = dict(schemas.ENGINE)
    out["strip"] = {"cells_each_side": schemas.ENGINE["strip_cells"] // 2,
                    "markers": schemas.ENGINE["strip_batch"]}
    out["layouts"] = {name: dict(spec) for name, spec in collage.LAYOUTS.items()}
    return out


def render(text: str, values=None) -> str:
    """`text` with every `{{a.b}}` replaced; KeyError for one that names nothing."""
    values = constants() if values is None else values

    def lookup(match):
        node = values
        for part in match.group(1).split("."):
            if not isinstance(node, dict) or part not in node:
                raise KeyError(f"skill placeholder {{{{{match.group(1)}}}}} names nothing")
            node = node[part]
        return f"{node:g}" if isinstance(node, float) else str(node)

    return PLACEHOLDER.sub(lookup, text)


# -- the lint: every name a skill uses comes from the code ------------------------

#: The trees whose code names what a skill may name.
_VOCABULARY_SOURCES = ("plugins/gating", "agent", "mcp", "ai", "cli.py")
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_FENCE = re.compile(r"```.*?```", re.S)
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_QUOTED = re.compile(r"\"[^\"]*\"|'[^']*'")
_LIST_MARKER = re.compile(r"^\s*\d+\.\s")
_LITERALS = {"true", "false", "null", "none"}


def _harvest(tree, words):
    """The names a module gives its data: dict keys, subscripts, `.get()`s,
    keyword arguments, annotated fields, Literal values, CLI names, and the
    strings of its UPPERCASE constants."""
    import ast

    def strings(node):
        return [c.value for c in ast.walk(node)
                if isinstance(c, ast.Constant) and isinstance(c.value, str)]

    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            words.update(k.value for k in node.keys
                         if isinstance(k, ast.Constant) and isinstance(k.value, str))
        elif isinstance(node, ast.Subscript):
            if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                words.add(node.slice.value)
            if getattr(node.value, "id", None) == "Literal" or \
                    getattr(node.value, "attr", None) == "Literal":
                words.update(strings(node.slice))
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name in ("get", "setdefault", "pop", "add_parser", "add_argument"):
                for arg in node.args[:1]:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        words.add(arg.value.lstrip("-").replace("-", "_"))
            words.update(kw.arg for kw in node.keywords if kw.arg)
        elif isinstance(node, ast.Assign) and all(
                isinstance(t, ast.Name) and t.id.isupper() for t in node.targets):
            words.update(strings(node.value))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            words.add(node.target.id)
            words.update(strings(node.annotation))
        elif isinstance(node, ast.FunctionDef):
            words.add(node.name)


def vocabulary() -> set:
    """Every name a skill may put in backticks: tool and capability names,
    and the names the code itself gives its fields, states and values. A name
    the code stops using drops out, and the lint then names the skill."""
    import ast

    from plexora.agent import registry
    from plexora.mcp import SERVER_TOOLS

    root = Path(__file__).resolve().parent.parent
    words = set(SERVER_TOOLS)
    for relative in _VOCABULARY_SOURCES:
        path = root / relative
        for file in ([path] if path.is_file() else sorted(path.rglob("*.py"))):
            if "tests" in file.parts:
                continue
            _harvest(ast.parse(file.read_text(encoding="utf-8")), words)
    registry.discover()
    for capability in registry.all_capabilities():
        words.update((capability.name, capability.tool_name))
    for entry in manifest().get("skills", []):
        words.update((entry["name"], entry.get("prompt") or entry["name"]))
    # Marker names, as the shipped vocabulary knows them: a skill's examples.
    from plexora.ai import vocabulary as markers

    for canonical, known in markers.load()["entries"].items():
        for name in (canonical, *(known.get("synonyms") or ())):
            words.update(_IDENT.findall(str(name)))
    return words


def _prose(text):
    """The words of a skill outside code: no fences, spans or placeholders."""
    text = _FENCE.sub("", text)
    text = _CODE_SPAN.sub("", text)
    return PLACEHOLDER.sub("", text)


def lint(vocab=None) -> list:
    """Problems with what the skills say, as sentences. [] when all is well:
    every backticked name is one the code uses; prose restates no number (a
    number the server owns is a `{{placeholder}}`); every placeholder renders."""
    vocab = vocabulary() if vocab is None else vocab
    problems = []
    values = constants()
    for entry in manifest().get("skills", []):
        name = entry["name"]
        text = raw_skill(name)
        spans = _CODE_SPAN.findall(_FENCE.sub("", text))
        unknown = sorted({token for span in spans
                          for token in _IDENT.findall(_QUOTED.sub("", span))
                          if token not in vocab and token.lower() not in _LITERALS})
        if unknown:
            problems.append(f"{name}: backticked names the code does not use: {unknown}")
        for line in _prose(text).splitlines():
            if re.search(r"\d", _LIST_MARKER.sub("", line)):
                problems.append(f"{name}: a number in prose (make it a placeholder or code): "
                                f"{line.strip()[:100]!r}")
        try:
            render(text, values)
        except KeyError as exc:
            problems.append(f"{name}: {exc.args[0]}")
    return problems


def _headings(text: str) -> list:
    return [line[3:].strip() for line in text.splitlines() if line.startswith("## ")]


def validate(tool_names=None) -> list:
    """Problems with the installed skills, as sentences. [] when all is well.

    `tool_names` is the set of tool names the registry offers; default is
    whatever discovery finds with every plugin this process can see.
    """
    problems = []
    if tool_names is None:
        from plexora.agent import registry

        registry.discover()
        tool_names = {cap.tool_name for cap in registry.all_capabilities()}
    from plexora.mcp import SERVER_TOOLS

    tool_names = set(tool_names) | set(SERVER_TOOLS)
    for entry in manifest().get("skills", []):
        name = entry["name"]
        path = SKILLS_DIR / name / "SKILL.md"
        if not path.exists():
            problems.append(f"{name}: SKILL.md is missing")
            continue
        text = path.read_text(encoding="utf-8")
        headings = _headings(text)
        missing = [h for h in REQUIRED_SECTIONS if h not in headings]
        if missing:
            problems.append(f"{name}: missing sections {missing}")
        for tool in entry.get("tools", []):
            if tool not in tool_names:
                problems.append(f"{name}: names tool {tool!r}, which does not exist")
            elif f"`{tool}`" not in text:
                problems.append(f"{name}: lists tool {tool!r} but never mentions it")
    return problems
