"""Plexora in Biocognia Intelligence: its registration under the
`biocognia.products` entry point (the contract is
`biocognia_intelligence/registry.py`).

Standard library only, so `biocognia list` names Plexora without importing
it; each method imports what it needs. Plexora holds no workspace of its own
(`home()` is None and its one role is guest), and takes part in the app's
workspaces three ways.

- Its tools. In a workspace's conversation the app may call the tools listed
  in `GUEST_TOOLS` as `plexora.<tool>`. Each call reaches Plexora over
  spatialbridge -- the copy already running, else one started for it
  (`plexora mcp serve --origin bridge`) -- and Plexora checks it as it checks
  any bridge call: its licence, its policy, and never a source-file write or
  a delete.
- Its licence pays (`pay()`). A workspace's conversation may be paid for by
  Plexora: each model call goes through Plexora's own gateway client, on this
  device's Plexora licence, as Plexora's task (`plexora.chat.turn`). Plexora
  pays for the conversation only -- its `chat` module -- so the app refuses a
  plan's call before sending it rather than letting the gateway answer 403.
  Without Plexora's AI on this device every call is refused with the
  sentence `ai()` gives, and the licence is asked again at the next call.
- Its images. An app that can hand a file to a product (0.3.1 on) asks
  `imports(path)`: for an image Plexora opens, by name alone, it names the
  guest tool that takes it -- `import_image`, which answers with the project
  that shows that file, made from it where there is none, the image read
  where it lies -- and its arguments. `classify(path)` still labels the image
  as Plexora's, with no action, for an app that cannot.

What a conversation is offered is `guest.json`, written from the capability
registry by `tools/intelligence_manifest.py`, so the catalogue never depends
on whether Plexora is running. Its Pro tools (AI gating and QC) carry their
entitlement, and the app offers them only where `grants()` -- this device's
Plexora licence, read without a network call -- covers it. Plexora's own chat
panel, `plexora ai` and its MCP server work as they did; paying for the app's
conversation is a second use of the same licence and gateway.
"""

from __future__ import annotations

import os
import re
import sys

#: The Plexora tools a conversation in another product's workspace may call:
#: Plexora's own analyses -- what a project is, gating judged against the
#: marker's distribution, image quality control, regions, the images that show
#: them -- the job and undo tools they need, and on Pro its AI gating and QC.
#: None writes a source file or deletes (the bridge carries no approval); what
#: the home's own hand-offs drive (the viewer, gates and regions coming back)
#: is the home's. `tools/intelligence_manifest.py` checks all of it.
GUEST_TOOLS = (
    # What a project is, and the one an image file the person names becomes.
    "list_projects", "inspect_project", "list_channels", "list_markers", "get_marker_distribution",
    "detect_nuclear_channels", "count_cells_in_roi", "import_image",
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
         "not know yet is handed to it by this workspace's own hand-off first. An image file the person "
         "names becomes one through plexora.import_image, which answers with the project that already "
         "shows that file when there is one. Gating: "
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

#: Why Plexora's AI is off on this device: `ai()`'s reason, and what a call
#: Plexora would pay for is refused with.
NO_AI = "Plexora's AI needs Plexora Pro; `plexora license status` says what this device has"

#: What Plexora's licence pays for in a workspace: the conversation (its chat
#: module), never SCIMAP Pro's plans.
PAID_MODULES = ("chat",)

#: The images Plexora opens, by the end of their name, longest first so
#: `.ome.tif` is not read as `.tif`: the format each is. Not a plain `.zarr`:
#: by its name it may as well be an AnnData or SpatialData store (SCIMAP Pro's).
IMAGES = ((".ome.tiff", "ome-tiff"), (".ome.tif", "ome-tiff"), (".ome.zarr", "ome-zarr"),
          (".qptiff", "qptiff"), (".tiff", "tiff"), (".tif", "tiff"))

#: What the app shows beside an image Plexora opens.
IMAGE_LABEL = "Image · open it in Plexora"

#: What the app shows beside an image it may hand to Plexora (`imports`).
IMPORT_LABEL = "Image · Plexora"

#: The guest tool an image is handed to Plexora through.
IMPORT_TOOL = "import_image"


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
        return {"ok": False, "reason": NO_AI}

    def pay(self) -> dict:
        """What the app needs for Plexora to pay for a workspace's
        conversation: a factory for the gateway client each model call goes
        through, and the modules it serves (the chat only)."""
        return {"gateway_factory": self._gateway, "modules": PAID_MODULES}

    def _gateway(self):
        """Plexora's gateway client when this device's licence allows its AI,
        else one that refuses every call with `ai()`'s reason. Decided at each
        call, so a device activated while the app runs pays from the next one.

        The app hands it requests carrying SCIMAP Pro's task names too
        (`chat.researcher`); `tasks.wire_id` sends a task Plexora's registry
        does not name as `plexora.<module>.<task>`, which the gateway serves
        at Plexora's default."""
        state = self.ai()
        if not state["ok"]:
            from spatialagents.gateway import RefusingGateway

            return RefusingGateway(state["reason"])
        from plexora.ai.harness.gateway import GatewayClient

        return GatewayClient()

    def classify(self, path) -> dict | None:
        """An image Plexora opens, by its name alone (no file is read):
        `{format, label, action, product}`, with no action in this release;
        None for anything else, a cell table included."""
        name = re.split(r"[\\/]", os.fspath(path).rstrip("\\/"))[-1].lower()
        for suffix, kind in IMAGES:
            if name.endswith(suffix):
                return {"format": kind, "label": IMAGE_LABEL, "action": None, "product": self.id}
        return None

    @property
    def page(self) -> dict:
        """What the app's home screen lists among what it opens: the images
        Plexora takes (`imports`), files and the OME-Zarr folder."""
        return {"formats": {"files": [suffix for suffix, _ in IMAGES if not suffix.endswith(".zarr")],
                            "folders": [suffix for suffix, _ in IMAGES if suffix.endswith(".zarr")]}}

    def imports(self, path) -> dict | None:
        """How the app hands an image Plexora opens to Plexora: `{format,
        label, tool, arguments}`, the guest tool (`import_image`) and what to
        call it with; None for anything `classify` does not name an image.
        By name alone, as `classify`: the tool is what reads the file."""
        found = self.classify(path)
        if found is None:
            return None
        return {"format": found["format"], "label": IMPORT_LABEL, "tool": IMPORT_TOOL,
                "arguments": {"image": os.fspath(path)}}

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
