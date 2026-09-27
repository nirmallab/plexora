"""Automatic gating's state as MCP resources, under `plexora://gating/`.

Each one is a capability call underneath, like every other resource, so the
resource and the tool that report a session can never disagree.
"""

from __future__ import annotations

from plexora.mcp import serialize
from plexora.mcp.resources import JSON, _answer


def register(server, runtime):
    def resource(uri, name, description):
        def decorate(fn):
            server.resource(uri, name=name, description=description, mime_type=JSON)(fn)
            return fn
        return decorate

    @resource("plexora://gating/sessions", "gating-sessions",
              "Recent automatic-gating sessions and their states.")
    def sessions() -> str:
        return _answer(runtime, "gating.session_status", {})

    @resource("plexora://gating/session/{session_id}", "gating-session",
              "One gating session: every unit's state, confidence and gate; what it spent; "
              "its open questions.")
    def session(session_id: str) -> str:
        return _answer(runtime, "gating.session_status", {"session_id": session_id})

    @resource("plexora://gating/session/{session_id}/packet", "gating-packet",
              "The session's outstanding decision packet, without its images.")
    def packet(session_id: str) -> str:
        from plexora.plugins.gating.server.autogate import engine

        try:
            record = engine.store().load(session_id)
            outstanding = record.get("outstanding_packet")
            if not outstanding:
                return serialize.bound({"outstanding": None, "state": record.get("state")})
            found, _images = engine.store().read_packet(session_id, outstanding)
            return serialize.bound(found)
        except Exception as exc:
            from plexora.agent.errors import as_agent_error

            return serialize.bound({"error": as_agent_error(exc).to_problem()})

    @resource("plexora://project/{name}/gates/provenance", "gate-provenance",
              "Where each of a project's gates came from: method, status, confidence.")
    def provenance(name: str) -> str:
        return _answer(runtime, "gating.provenance", {"project": name})

    @resource("plexora://project/{name}/panel", "panel-context",
              "The project's panel biology as Plexora resolved it, and the gating order.")
    def panel(name: str) -> str:
        return _answer(runtime, "gating.get_panel_context", {"project": name})
