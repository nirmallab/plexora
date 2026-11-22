"""A session over a table whose cells are not on their own mask labels is
refused before anything is written (gating) or says so (QC).

lsp35766_p193_2 (2026-11-21): the table's x/y were Xenium microns read as
CyCIF pixels. Every look was drawn on glass and readers judged it; the mask
knew at once -- no crop held its own cell's label."""

import pytest

from plexora.agent import AgentSession, registry
from tests.autogate_fixtures import make_gating_project

pytestmark = pytest.mark.pro


@pytest.fixture(autouse=True)
def _plugins():
    registry.discover(["gating", "qc", "roi"])


def _shift(session, project, dx, dy, monkeypatch):
    """Read the table with every cell moved by (dx, dy) pixels."""
    import polars as pl

    table = session.data(project).table
    real = type(table).geometry
    roles = session.project(project).roles
    x, y = roles.get("x"), roles.get("y")

    def moved(self):
        frame = real(self)
        return frame.with_columns((pl.col(x) + dx).alias(x), (pl.col(y) + dy).alias(y))

    monkeypatch.setattr(type(table), "geometry", moved)


def test_a_table_on_its_mask_passes(tmp_path):
    from plexora.agent.evidence import alignment

    make_gating_project(tmp_path)
    found = alignment.check(AgentSession(), "gsynth")
    assert found["status"] == "ok" and found["fraction"] >= 0.9


def test_a_table_off_its_mask_is_misaligned_and_gating_refuses(tmp_path, monkeypatch):
    from plexora.agent.errors import AgentError
    from plexora.agent.evidence import alignment

    make_gating_project(tmp_path)
    session = AgentSession()
    _shift(session, "gsynth", 23.0, 31.0, monkeypatch)
    found = alignment.check(session, "gsynth")
    assert found["status"] == "misaligned" and found["fraction"] < alignment.MIN_ON_LABEL
    assert "table correction" in found["message"]

    out = registry.invoke(session, "gating_session_start",
                          {"scope": "project", "project": "gsynth", "mode": "propose"})
    assert not out["ok"] and out["error"]["code"] == "precondition_missing"
    assert "mask label" in out["error"]["message"]


def test_no_mask_is_unchecked_and_passes(tmp_path):
    from plexora.agent.evidence import alignment

    make_gating_project(tmp_path, mask=False)
    assert alignment.check(AgentSession(), "gsynth")["status"] == "unchecked"
