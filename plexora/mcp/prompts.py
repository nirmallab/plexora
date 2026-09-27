"""MCP prompts: the workflows a user can start from the client's own menu.

A prompt is a slash command in the client ("Gate an image") that opens a
conversation already holding the right skill and the arguments -- so a client
that does not install skill files (Codex, Cursor) still gets the procedure,
and a user does not have to know which tool starts a gating session.

Which skills have a prompt, and its name, title and description, are the
skill manifest's (`prompt:`); this module only says what each one asks.
"""

from __future__ import annotations


def _skill(name):
    from plexora.ai import skills

    return skills.read_skill(name)


def _mode(mode):
    """A session mode, checked against the session's own options."""
    from typing import get_args

    from plexora.plugins.gating.capabilities_session import SessionOptions

    allowed = get_args(SessionOptions.model_fields["mode"].annotation)
    if mode not in allowed:
        raise ValueError(f"mode is one of {', '.join(allowed)}")
    return mode


def _default_mode():
    from plexora.plugins.gating.capabilities_session import SessionOptions

    return SessionOptions.model_fields["mode"].default


def _tool(capability):
    from plexora.agent import registry

    return registry.tool_name_of(capability)


def _gate_image(skill):
    def gate_image(project: str, markers: str = "", mode: str = _default_mode()) -> str:
        wanted = [m.strip() for m in markers.split(",") if m.strip()]
        return (f"Gate the image `{project}` "
                + (f"(markers: {', '.join(wanted)}) " if wanted else "(every marker) ")
                + f"in `{_mode(mode)}` mode with `{_tool('gating.session_start')}`, "
                  "following this skill exactly.\n\n" + _skill(skill))
    return gate_image


def _gate_dataset(skill):
    def gate_dataset(dataset: str, reference_image: str = "",
                     mode: str = _default_mode()) -> str:
        return (f"Gate the dataset `{dataset}` in `{_mode(mode)}` mode"
                + (f", with `{reference_image}` as the reference image" if reference_image
                   else "")
                + ", following this skill exactly.\n\n" + _skill(skill))
    return gate_dataset


def _review_gating(skill):
    def review_gating(project: str) -> str:
        return (f"Review the gates of `{project}` with me, following this skill.\n\n"
                + _skill(skill))
    return review_gating


def _diagnose_marker(skill):
    def diagnose_marker(project: str, marker: str) -> str:
        return (f"Diagnose `{marker}` in `{project}`, following this skill.\n\n"
                + _skill(skill))
    return diagnose_marker


#: What each prompting skill asks for, by skill name.
BUILDERS = {"gate-image": _gate_image, "gate-dataset": _gate_dataset,
            "review-gating": _review_gating, "diagnose-marker": _diagnose_marker}


def register(server, runtime):
    """Add every prompt the manifest names to `server`; returns their names."""
    from plexora.ai import skills

    names = []
    for entry in skills.manifest().get("skills", []):
        if not entry.get("prompt"):
            continue
        builder = BUILDERS.get(entry["name"])
        if builder is None:
            raise KeyError(f"skill {entry['name']!r} names prompt {entry['prompt']!r} but "
                           "plexora.mcp.prompts has no builder for it")
        server.prompt(name=entry["prompt"], title=entry.get("title"),
                      description=entry.get("description"))(builder(entry["name"]))
        names.append(entry["prompt"])
    return names
