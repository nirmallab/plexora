"""The runtime scientific skills: what they are, and whether they still hold.

A skill is a SKILL.md an agent reads before doing a kind of work -- when to use
it, the decision logic, which tools it may call, how to pick evidence, what it
may change and how to report. `skill_manifest.yaml` lists them with the tool
names each one relies on, and `validate()` checks those names against the live
capability registry, so a renamed tool breaks a test instead of an agent.
"""

from __future__ import annotations

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


def read_skill(name: str) -> str:
    """The SKILL.md text. KeyError naming the skills that exist."""
    names = [entry["name"] for entry in manifest().get("skills", [])]
    path = SKILLS_DIR / str(name) / "SKILL.md"
    if name not in names or not path.exists():
        raise KeyError(f"no skill named {name!r}; available: {', '.join(names)}")
    return path.read_text(encoding="utf-8")


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
    tool_names = set(tool_names) | {"validate_scope", "list_skills", "read_skill",
                                    "server_info", "list_capabilities"}
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
