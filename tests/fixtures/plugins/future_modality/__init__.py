"""A plugin for a modality Plexora has never heard of: holography.

The conformance check for "future-proof": a third-party distribution declares a
new layer modality and its own agent capabilities, and core -- the registry,
the scene, the render path, the MCP server -- handles it without a line of
change. Installed in tests through a fake entry point.
"""

from plexora.api.plugin import Plugin, Requires


def _capabilities():
    import time

    from pydantic import Field

    from plexora.agent.errors import AgentError
    from plexora.agent.receipts import make_receipt
    from plexora.agent.registry import Capability
    from plexora.agent.schemas import ProjectInput

    class ReconstructInput(ProjectInput):
        steps: int = Field(5, ge=1, le=1000)
        step_s: float = Field(0.01, ge=0, le=1)
        fail_at: int | None = None

    def reconstruct(call, inp):
        """Long work as a job: progress per step, stoppable between steps,
        receipted once at the end."""
        for step in range(inp.steps):
            call.check_cancelled()
            if inp.fail_at is not None and step == inp.fail_at:
                raise AgentError("internal_error", f"reconstruction diverged at step {step}")
            call.progress(done=step + 1, total=inp.steps, message=f"step {step + 1}")
            time.sleep(inp.step_s)
        receipt = make_receipt(call, changed=True, before=None,
                               after={"steps": inp.steps}, persistent_state="none")
        return {"receipt": receipt.model_dump(mode="json"), "phase_map": "done",
                "steps": inp.steps}

    def summarize(call, inp):
        from plexora import api

        layers = api.layers(call.session.project(inp.project), modality="holography")
        return {"holograms": [{"id": layer.id, "label": layer.label,
                               "extra": dict(layer.extra)} for layer in layers]}

    def show(call, inp):  # pragma: no cover - never reached without a viewer
        return {"shown": True}

    requires = Requires(layers=("holography",))
    return [
        Capability(name="holography.summarize", owner="future_modality",
                   purpose="Summarize a sample's holograms.", permission="read",
                   input_model=ProjectInput, handler=summarize, requires=requires,
                   tags=("hologram", "holography")),
        Capability(name="holography.show", owner="future_modality",
                   purpose="Show a hologram in the open viewer.", permission="read",
                   input_model=ProjectInput, handler=show, requires=requires,
                   viewer_required=True, tags=("hologram", "show")),
        Capability(name="holography.reconstruct", owner="future_modality",
                   purpose="Reconstruct a hologram's phase map (long; runs as a job).",
                   permission="reversible_write", input_model=ReconstructInput,
                   handler=reconstruct, requires=requires, execution="job",
                   tags=("hologram", "reconstruct")),
    ]


PLUGIN = Plugin(
    name="future_modality",
    label="Holography",
    version="1",
    requires=Requires(layers=("holography",)),
    capabilities_factory=_capabilities,
)
