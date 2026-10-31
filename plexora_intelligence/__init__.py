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
on whether Plexora is running. Its Pro tools (AI gating and QC) carry their
entitlement, and the app offers them only where `grants()` -- this device's
Plexora licence, read without a network call -- covers it. Plexora's own AI
-- its chat panel, `plexora ai`, its MCP server -- is untouched by any of this.
"""

from __future__ import annotations

import sys

#: The Plexora tools a conversation in another product's workspace may call:
#: Plexora's own analyses -- what a project is, gating judged against the
#: marker's distribution, image quality control, regions, the images that show
#: them -- the job and undo tools they need, and on Pro its AI gating and QC.
#: None writes a source file or deletes (the bridge carries no approval); what
#: the home's own hand-offs drive (the viewer, gates and regions coming back)
#: is the home's. `tools/intelligence_manifest.py` checks all of it.
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
    # What the pixels show, as images.
    "render_region", "render_cell_gallery", "explain_cell",
    # A long check runs as a job; a write is taken back by its operation_id.
    "job_get", "job_wait", "undo_operation",
    # Pro: Plexora's own AI does a whole gating or QC run, on Plexora's credits.
    "ai_run_session", "ai_run_status", "ai_run_control", "ai_balance",
    # Pro: gating and QC step by step, the conversation deciding from each packet's images.
    "gating_session_start", "gating_next", "gating_answer", "gating_session_status", "gating_session_finish",
    "gating_report", "render_gate_validation",
    "qc_session_start", "qc_next", "qc_answer", "qc_session_status", "qc_session_finish", "qc_report",
    "render_qc_overview",
)

#: What the conversation is told about Plexora (the prefix, beside the catalogue).
RULES = ("Plexora is the image viewer and its analyses of the pixels. Its tools take a `project` (an image "
         "with its mask and cell table): plexora.list_projects names those it knows, and a dataset it does "
         "not know yet is handed to it by this workspace's own hand-off first. Gating: "
         "plexora.suggest_auto_gate proposes a gate from the marker's distribution, plexora.set_gate stores "
         "it, plexora.get_gated_summary counts what a gate calls positive. Quality control: "
         "plexora.run_basic_qc on an image nobody has checked, then plexora.get_qc_results. "
         "On Plexora Pro there are two ways to gate or check quality with AI. plexora.ai_run_session hands "
         "the whole run to Plexora's own AI, which spends Plexora's AI credits, not this workspace's: say "
         "so before starting one (plexora.ai_balance tells what is left), follow it with "
         "plexora.ai_run_status, and a run waiting_for_user is a question to put to the person. The "
         "session tools go step by step instead: plexora.gating_session_start, then plexora.gating_next "
         "returns a packet with images to look at, and plexora.gating_answer records your decision on it "
         "(plexora.qc_* the same for quality control); these spend this conversation's credits.")

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

    def grants(self) -> tuple:
        """The entitlements this device's Plexora licence grants, read without a
        network call; () on Free or when the licence cannot be read."""
        try:
            from plexora import licensing

            state = licensing.peek()
            return tuple(state.entitlements) if state.licensed else ()
        except Exception:  # noqa: BLE001 -- a licence that cannot be read grants nothing
            return ()


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
