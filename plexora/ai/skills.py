"""The runtime scientific skills: what they are, and whether they still hold.

A skill is a SKILL.md an agent reads before doing a kind of work -- when to use
it, the decision logic, which tools it may call, how to pick evidence, what it
may change and how to report. `skill_manifest.yaml` lists them with the tool
names each one relies on, and `validate()` checks those names against the live
capability registry, so a renamed tool breaks a test instead of an agent.

A skill's `## Tools` section is the manifest's list, written in when the
skill is read (`{{tools}}`), so the two cannot drift. Material a skill needs
only sometimes is a reference (`skills/<group>/<name>.md`, read as
`read_skill("<group>/<name>")`), listed in the manifest beside the skills
that point at it. A skill's old name stays readable through the manifest's
`aliases`, so a brief or an installed copy that names it keeps working.
"""

from __future__ import annotations

import re
from pathlib import Path

SKILLS_DIR = Path(__file__).parent / "skills"
MANIFEST_PATH = Path(__file__).parent / "skill_manifest.yaml"

#: A reference's name, `<group>/<name>`, served from `skills/<group>/<name>.md`.
#: Only names the manifest lists are ever read, so no caller's string becomes
#: a path.
REFERENCE_NAME = re.compile(r"^[a-z][a-z0-9-]*/[a-z][a-z0-9-]*$")

#: Where a skill's tool list goes: rendered from its manifest entry.
TOOLS_PLACEHOLDER = "{{tools}}"

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


def _entries(section: str, data=None) -> list:
    return list((manifest() if data is None else data).get(section) or [])


def aliases() -> dict:
    """{old name: the skill or reference that replaced it}."""
    return {entry["name"]: entry["to"] for entry in _entries("aliases")}


def resolve(name) -> str:
    """The skill or reference `name` reads: itself, or what replaced it."""
    name = str(name)
    return aliases().get(name, name)


def _source(name: str, data=None):
    """(manifest entry, path) of a skill or reference by its own name, or
    None. The path is built from the manifest's name, never the caller's."""
    for entry in _entries("skills", data):
        if entry["name"] == name:
            return entry, SKILLS_DIR / name / "SKILL.md"
    for entry in _entries("references", data):
        if entry["name"] == name and REFERENCE_NAME.match(name):
            return entry, SKILLS_DIR / f"{name}.md"
    return None


def readable_names(data=None) -> list:
    """Every name `read_skill` answers: skills, references, old names."""
    return ([e["name"] for e in _entries("skills", data)]
            + [e["name"] for e in _entries("references", data)]
            + [e["name"] for e in _entries("aliases", data)])


def list_skills() -> list:
    data = manifest()
    references = _entries("references", data)
    replaced = {entry["name"]: entry["to"] for entry in _entries("aliases", data)}
    out = []
    for entry in _entries("skills", data):
        name = entry["name"]
        path = SKILLS_DIR / name / "SKILL.md"
        mine = [ref for ref in references if name in ref.get("skills", ())]
        own = {name, *(ref["name"] for ref in mine)}
        row = {
            "name": name,
            "title": entry.get("title", name),
            "description": entry.get("description", ""),
            "when": entry.get("when", ""),
            "tier": entry.get("tier", "judgement"),
            "tools": list(entry.get("tools", [])),
            "available": path.exists(),
            "uri": f"plexora://skill/{name}",
        }
        if mine:
            row["references"] = [{"name": ref["name"], "title": ref.get("title", ref["name"]),
                                  "when": ref.get("when", "")} for ref in mine]
        old = {alias: to for alias, to in replaced.items() if to in own}
        if old:
            row["aliases"] = old
        out.append(row)
    return out


def prompt_entries(data=None) -> list:
    """The manifest entries that open an MCP prompt (`prompt:`), skills first,
    then old names kept as prompts (each reads what replaced it)."""
    return [entry for section in ("skills", "aliases") for entry in _entries(section, data)
            if entry.get("prompt")]


def tools_line(entry) -> str:
    """A skill's `## Tools` list, from its manifest entry."""
    return ", ".join(f"`{tool}`" for tool in entry.get("tools", ())) + "."


def raw_skill(name: str) -> str:
    """The skill or reference as written, placeholders and all; an old name
    reads what replaced it. KeyError naming what can be read."""
    found = _source(resolve(name))
    if found is None or not found[1].exists():
        raise KeyError(f"no skill named {name!r}; available: {', '.join(readable_names())}")
    return found[1].read_text(encoding="utf-8")


def _values(entry, values=None) -> dict:
    """The placeholders' values for one skill or reference: the constants,
    and its own tool list."""
    values = constants() if values is None else values
    return {**values, "tools": tools_line(entry)} if entry.get("tools") else values


def read_skill(name: str) -> str:
    """The skill or reference an agent reads: its `{{placeholders}}` filled
    from the code's own constants (`constants`) and its `{{tools}}` from the
    manifest. An old name (`aliases`) reads what replaced it."""
    text = raw_skill(name)
    return render(text, _values(_source(resolve(name))[0]))


# -- placeholders --------------------------------------------------------------

#: `{{name.key}}` in a SKILL.md: a number the code owns, never restated.
PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][\w.]*)\s*\}\}")


def constants() -> dict:
    """What a skill's placeholders may name: the constants the server
    decides with. Plugins that are not installed contribute nothing."""
    from plexora.agent.sessions import budget

    from plexora.agent.limits import MAX_CHANNELS

    from plexora.agent import cell_exclusions

    from plexora.ai import delegation

    out = {"budget": dict(budget.UNIT_DEFAULT), "render": {"max_channels": MAX_CHANNELS},
           "TIERS": {role: spec["tier"] for role, spec in delegation.ROLES.items()},
           "qc_exclusion": {
               "field_max_percent": round(cell_exclusions.FIELD_QC_MAX_FRACTION * 100),
               "heavy_percent": round(cell_exclusions.HEAVY_EXCLUSION_FRACTION * 100)}}
    from plexora.vision import sam as segment_model

    out["SAM"] = {"max_refinements": segment_model.MAX_AGENT_REFINEMENTS,
                  "max_points": segment_model.MAX_POINTS}
    from plexora.agent.core import volume as volume_tools

    out["volume"] = {"profile_side": volume_tools.PROFILE_SIDE,
                     "max_planes": volume_tools.MAX_PLANES_PER_PROFILE}
    try:
        from plexora.plugins.qc.server import schemas as qc_schemas

        out["QC_ENGINE"] = dict(qc_schemas.ENGINE)
        from plexora.plugins.qc import capabilities_objects

        out["OBJECT_REVIEW"] = dict(capabilities_objects.OBJECT_REVIEW)
        out["qc_budget"] = dict(qc_schemas.QC_UNIT_DEFAULT)
        out["VISUAL"] = dict(qc_schemas.VISUAL)
    except ImportError:  # pragma: no cover - QC not shipped in this build
        pass
    try:
        from plexora.agent.evidence import collage
        from plexora.plugins.gating.server.autogate import schemas
    except ImportError:  # pragma: no cover - the gating plugin is always shipped
        return out
    out["ENGINE"] = dict(schemas.ENGINE)
    out["strip"] = {"cells_each_side": schemas.ENGINE["strip_cells"] // 2,
                    "markers": schemas.ENGINE["strip_batch"]}
    out["layouts"] = {name: dict(spec) for name, spec in collage.LAYOUTS.items()}
    from plexora.plugins.gating.server.autogate import scoring

    out["scoring"] = {"ceiling_sd": scoring.PARAMS["ceiling_sd"],
                      "bio_k": scoring.PARAMS["bio_k"],
                      "onset_percent": round(scoring.PARAMS["onset_lfdr"] * 100),
                      "max_elasticity": scoring.PARAMS["robust_max_elasticity"],
                      "sens_percent": round(scoring.PARAMS["sens_quantile"] * 100)}
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
_VOCABULARY_SOURCES = ("plugins/gating", "plugins/qc", "plugins/roi", "plugins/video_studio",
                       "agent", "mcp", "ai", "cli.py")
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
    # The vocabulary Plexora shares with SCIMAP Pro, defined in spatialagents:
    # the permission and egress classes, the four scope states, the error codes.
    import spatialagents.errors
    import spatialagents.policy

    for module in (spatialagents.policy, spatialagents.errors):
        _harvest(ast.parse(Path(module.__file__).read_text(encoding="utf-8")), words)
    registry.discover()
    for capability in registry.all_capabilities():
        words.update((capability.name, capability.tool_name))
    for section in ("skills", "references", "aliases"):
        for entry in _entries(section):
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
    for entry in _entries("skills") + _entries("references"):
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
            render(text, _values(entry, values))
        except KeyError as exc:
            problems.append(f"{name}: {exc.args[0]}")
    return problems


# -- the guide lint: a skill points at the reading guide, never restates it ------

#: A clause of a gating session's reading guide at least this many words long,
#: found word for word in a skill or reference, is the guide restated: the
#: session sends the guide itself (`gating_session_status`), and two copies
#: drift apart.
GUIDE_CLAUSE_WORDS = 6
_CLAUSE_BREAK = re.compile(r"[.;:!?]+(?=\s|$)|\s--\s|[()]")
_WORD = re.compile(r"[a-z0-9_']+")


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from _strings(value)


def guide_clauses() -> set:
    """The clauses of every engine's reading guide, as lower-case word runs:
    `packets.READING_GUIDE`, and what `packets.reading_guide` adds to it (the
    compartment readings, the answer schemas). Read only, never changed."""
    try:
        from plexora.plugins.gating.server.autogate import packets, schemas
    except ImportError:  # pragma: no cover - the gating plugin is always shipped
        return set()
    texts = list(_strings(packets.READING_GUIDE))
    for engine in schemas.ENGINES:
        texts.extend(_strings(packets.reading_guide(engine)))
    out = set()
    for text in texts:
        for part in _CLAUSE_BREAK.split(text):
            words = _WORD.findall(part.lower())
            if len(words) >= GUIDE_CLAUSE_WORDS:
                out.add(" ".join(words))
    return out


def guide_repeats(clauses=None) -> list:
    """Problems, as sentences: each skill or reference that repeats a clause of
    the reading guide word for word (`GUIDE_CLAUSE_WORDS`). [] when none does."""
    clauses = guide_clauses() if clauses is None else clauses
    problems = []
    for entry in _entries("skills") + _entries("references"):
        words = " " + " ".join(_WORD.findall(raw_skill(entry["name"]).lower())) + " "
        hits = sorted(clause for clause in clauses if f" {clause} " in words)
        if hits:
            more = f" (and {len(hits) - 1} more)" if len(hits) > 1 else ""
            problems.append(f"{entry['name']}: restates the gating reading guide, which the "
                            f"session sends itself: {hits[0]!r}{more}")
    return problems


def _headings(text: str) -> list:
    return [line[3:].strip() for line in text.splitlines() if line.startswith("## ")]


def _section(text: str, heading: str):
    """A `## heading` section's body, stripped; None when there is none."""
    match = re.search(rf"^## {re.escape(heading)}[ \t]*\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    return match.group(1).strip() if match else None


def _tools_section(name, entry, text, tool_names) -> list:
    """The `## Tools` section is the manifest's list: it opens with
    `{{tools}}`, and a note after it names no tool the manifest leaves out."""
    body = _section(text, "Tools")
    if body is None:
        return []
    problems = []
    if body.split("\n\n", 1)[0].strip() != TOOLS_PLACEHOLDER:
        problems.append(f"{name}: its Tools section must open with {TOOLS_PLACEHOLDER} (the "
                        "manifest's list, rendered), not a list of its own")
    listed = set(entry.get("tools", ()))
    extra = sorted({token for span in _CODE_SPAN.findall(body)
                    for token in _IDENT.findall(span)
                    if token in tool_names and token not in listed})
    if extra:
        problems.append(f"{name}: its Tools section names tools its manifest entry does not "
                        f"list: {extra}")
    return problems


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
    data = manifest()
    names = [e["name"] for e in _entries("skills", data)]
    for entry in _entries("skills", data):
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
        problems += [f"{name}: names tool {tool!r}, which does not exist"
                     for tool in entry.get("tools", []) if tool not in tool_names]
        if _section(text, "Tools") is None:
            continue    # reported as a missing section
        problems += _tools_section(name, entry, text, tool_names)
    listed = set()
    for entry in _entries("references", data):
        name = entry["name"]
        listed.add(name)
        if not REFERENCE_NAME.match(name):
            problems.append(f"{name}: a reference is named <group>/<name>, in lower case")
            continue
        path = SKILLS_DIR / f"{name}.md"
        if not path.exists():
            problems.append(f"{name}: {path.name} is missing")
            continue
        owners = entry.get("skills") or []
        if not owners or any(owner not in names for owner in owners):
            problems.append(f"{name}: names skills {owners}; a reference belongs to skills "
                            "that exist")
        problems += [f"{name}: names tool {tool!r}, which does not exist"
                     for tool in entry.get("tools", []) if tool not in tool_names]
        problems += _tools_section(name, entry, path.read_text(encoding="utf-8"), tool_names)
    unlisted = sorted(f"{p.parent.name}/{p.stem}" for p in SKILLS_DIR.glob("*/*.md")
                      if p.name != "SKILL.md" and f"{p.parent.name}/{p.stem}" not in listed)
    if unlisted:
        problems.append(f"references on disk the manifest does not list: {unlisted}")
    for entry in _entries("aliases", data):
        if entry["name"] in names or entry["name"] in listed:
            problems.append(f"{entry['name']}: an old name may not also be a skill or reference")
        if entry.get("to") not in set(names) | listed:
            problems.append(f"{entry['name']}: resolves to {entry.get('to')!r}, which is not a "
                            "skill or reference")
    prompts = [entry["prompt"] for entry in prompt_entries(data)]
    if len(prompts) != len(set(prompts)):
        problems.append(f"a prompt is named twice: {sorted(prompts)}")
    return problems + guide_repeats()
