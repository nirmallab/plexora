"""A plugin for a modality Plexora has never heard of: holography.

The conformance check for "future-proof": a third-party distribution declares a
new layer modality and its own agent capabilities, and core -- the registry,
the scene, the render path, the MCP server -- handles it without a line of
change. Installed in tests through a fake entry point.
"""

from plexora.api.plugin import Plugin, Requires


def _capabilities():
    from plexora.agent.registry import Capability
    from plexora.agent.schemas import ProjectInput

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
    ]


PLUGIN = Plugin(
    name="future_modality",
    label="Holography",
    version="1",
    requires=Requires(layers=("holography",)),
    capabilities_factory=_capabilities,
)
