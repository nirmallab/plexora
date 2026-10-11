"""Automatic gating's evidence on a z-stack (agent/sections.py `field_plane`).

A field of a z-stack holds cells at every depth of its column of tissue, and
the stack's default plane shows only those near it. What has to hold: the
context sheet draws each field at the plane its cells are on, and says which.
"""

import numpy as np
import pytest

from tests.test_agent_gate_panel import _deep_cells

pytestmark = pytest.mark.paid


def test_the_context_sheet_draws_a_field_at_its_cells_plane(tmp_path):
    from plexora.agent import AgentSession, registry
    from plexora.plugins.gating.server.autogate import sheet

    _deep_cells(tmp_path)                       # every cell on planes 6-8 of ten
    registry.discover(["gating"])
    session = AgentSession()
    ds = session.data("deep")
    low = float(np.percentile(np.asarray(ds.table.columns(["CD8"])["CD8"]), 50))
    out = sheet.render_context_sheet(session, ds, marker="CD8", channel="CD8", low=low,
                                     fmt="png", store=False)
    fields = out["manifest"]["fields"]
    assert fields and all(f["plane"] == 7 for f in fields)


def test_a_flat_image_has_no_field_plane(tmp_path):
    from plexora.agent import AgentSession
    from plexora.agent.sections import field_plane
    from tests.agent_fixtures import make_synthetic_project

    make_synthetic_project(tmp_path)
    data = AgentSession().data("synth")
    assert field_plane(data, {"x": 0, "y": 0, "width": 100, "height": 100}) is None
