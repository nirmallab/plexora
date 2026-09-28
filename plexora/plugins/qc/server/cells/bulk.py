"""The cell modules' deterministic half, inside the session's bulk pass."""

from __future__ import annotations


def run(call, session_id):
    from plexora.plugins.qc.server.engine import engine_for

    with engine_for(call, session_id) as engine:
        for unit in engine.units_of("cells"):
            if unit["state"] == "pending":
                engine.close(unit, "skipped_not_applicable", "cell QC is not available yet")
