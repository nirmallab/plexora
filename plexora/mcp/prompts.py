"""MCP prompts: the workflows a user can start from the client's own menu.

A prompt is a slash command in the client ("Gate an image") that opens a
conversation already holding the right skill and the arguments -- so a client
that does not install skill files (Codex, Cursor) still gets the procedure,
and a user does not have to know which tool starts a gating session.
"""

from __future__ import annotations


def _skill(name):
    from plexora.ai import skills

    return skills.read_skill(name)


def register(server, runtime):
    """Add every prompt to `server`."""

    @server.prompt(name="gate_image", title="Gate an image",
                   description="Gate every marker of one image automatically: a gating "
                               "session, deterministic where the numbers suffice.")
    def gate_image(project: str, markers: str = "", mode: str = "apply") -> str:
        wanted = [m.strip() for m in markers.split(",") if m.strip()]
        return (f"Gate the image `{project}` "
                + (f"(markers: {', '.join(wanted)}) " if wanted else "(every marker) ")
                + f"in `{mode}` mode, following this skill exactly.\n\n"
                + _skill("gate-image"))

    @server.prompt(name="gate_dataset", title="Gate a dataset",
                   description="Gate every image of a dataset from a reference image, with "
                               "drift classes and a strategy per marker.")
    def gate_dataset(dataset: str, reference_image: str = "", mode: str = "apply") -> str:
        return (f"Gate the dataset `{dataset}` in `{mode}` mode"
                + (f", with `{reference_image}` as the reference image" if reference_image
                   else "")
                + ", following this skill exactly.\n\n" + _skill("gate-dataset"))

    @server.prompt(name="review_gating", title="Review gates",
                   description="Go over a project's gates: provenance, consistency, and the "
                               "user's approve / lock / exclude / undo decisions.")
    def review_gating(project: str) -> str:
        return (f"Review the gates of `{project}` with me, following this skill.\n\n"
                + _skill("review-gating"))

    @server.prompt(name="diagnose_marker", title="Diagnose a marker",
                   description="Why one marker is hard to gate, and what would fix it.")
    def diagnose_marker(project: str, marker: str) -> str:
        return (f"Diagnose `{marker}` in `{project}`, following this skill.\n\n"
                + _skill("diagnose-marker"))

    return ["gate_image", "gate_dataset", "review_gating", "diagnose_marker"]
