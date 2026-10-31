"""Plexora in Biocognia Intelligence: its registration under the
`biocognia.products` entry point (the contract is
`biocognia_intelligence/registry.py`).

Standard library only, so `biocognia list` names Plexora without importing
it; each method imports what it needs. Plexora joins as a guest: no workspace
belongs to it yet, and in a workspace another product holds (SCIMAP Pro's),
the conversation may call the tools listed in `GUEST_TOOLS` as
`plexora.<tool>`. Each call reaches Plexora over spatialbridge -- the copy
already running, else one started for it (`plexora mcp serve --origin
bridge`) -- and Plexora checks it as it checks any bridge call: its licence,
its policy, and never a source-file write or a delete.

What a conversation is offered is `guest.json`, written from the capability
registry by `tools/intelligence_manifest.py`, so the catalogue never depends
on whether Plexora is running. Plexora's own AI -- its chat panel, `plexora
ai`, its MCP server -- is untouched by any of this.
"""

from __future__ import annotations

import sys

#: The Plexora tools a conversation in another product's workspace may call:
#: Plexora's own analyses -- what a project is, gating judged against the
#: marker's distribution, image quality control, regions -- and the job and
#: undo tools they need. None returns an image (an image does not cross the
#: bridge yet), needs a licence beyond Free, writes a source file or deletes;
#: what the home's own hand-offs drive (the viewer, gates and regions coming
#: back) is the home's. `tools/intelligence_manifest.py` checks all of it.
GUEST_TOOLS = (
    # What a project is.
    "list_projects", "inspect_project", "list_channels", "list_markers", "get_marker_distribution",
    "detect_nuclear_channels", "count_cells_in_roi",
    # Gates, judged against the marker's distribution.
    "get_gate", "suggest_auto_gate", "get_gate_distribution", "get_gated_summary", "get_gate_provenance",
    "set_gate", "adjust_gate", "reset_gates",
    # Image quality control.
    "run_basic_qc", "run_blur_check", "run_artifact_check", "run_segmentation_qc", "get_qc_results",
    "get_segmentation_qc",
    # Regions.
    "create_roi", "update_roi",
    # A long check runs as a job; a write is taken back by its operation_id.
    "job_get", "undo_operation",
)

#: What the conversation is told about Plexora (the prefix, beside the catalogue).
RULES = ("Plexora is the image viewer and its analyses of the pixels. Its tools take a `project` (an image "
         "with its mask and cell table): plexora.list_projects names those it knows, and a dataset it does "
         "not know yet is handed to it by this workspace's own hand-off first. Gating: "
         "plexora.suggest_auto_gate proposes a gate from the marker's distribution, plexora.set_gate stores "
         "it, plexora.get_gated_summary counts what a gate calls positive. Quality control: "
         "plexora.run_basic_qc on an image nobody has checked, then plexora.get_qc_results.")

#: How a person makes Plexora reachable.
HINT = "start Plexora (`biocognia open plexora`, or `plexora` in its environment), then try again"

#: Plexora's error codes the runtime has no word for, in the runtime's.
CODES = {
    "unknown_project": {"code": "invalid_input", "hint": "plexora.list_projects names the projects Plexora knows"},
    "viewer_not_available": {"code": "precondition_missing",
                             "hint": "open the project in Plexora (`biocognia open plexora`), then try again"},
    "viewer_not_responding": "resource_unavailable",
    "resource_not_local": "precondition_missing",
    "ambiguous_view": "invalid_input",
}


class _Guest:
    """Plexora's tools, as a guest in another product's workspace."""

    provider = "plexora"
    rules = RULES
    hint = HINT
    codes = CODES

    def manifest(self) -> dict:
        """`guest.json`, with the version of the Plexora installed."""
        import json
        from importlib.metadata import PackageNotFoundError, version
        from importlib.resources import files

        found = json.loads(files(__name__).joinpath("guest.json").read_text(encoding="utf-8"))
        try:
            found["version"] = version("plexora")
        except PackageNotFoundError:
            pass
        return found


class _Plexora:
    contract = 1
    id = "plexora"
    name = "Plexora"
    brand = "plexora"
    distribution = "plexora"
    roles = ("guest",)
    tagline = "Multiplexed tissue images: view, gate against the staining, check the quality, draw regions."

    def ai(self) -> dict:
        """Whether Plexora's own AI is unlocked on this device, and why not."""
        from plexora import licensing

        if licensing.allows("ai:chat"):
            return {"ok": True, "reason": None}
        return {"ok": False, "reason": "Plexora's AI needs Plexora Pro; `plexora license status` says what this "
                                       "device has"}

    def licensing(self):
        from plexora.licensing import LICENSING

        return LICENSING

    def app(self) -> list:
        """Plexora's own application, as a bare `plexora` opens it."""
        return [sys.executable, "-m", "plexora"]

    def home(self):
        """None: no workspace belongs to Plexora yet."""
        return None

    def guest(self):
        return _Guest()


PRODUCT = _Plexora()
