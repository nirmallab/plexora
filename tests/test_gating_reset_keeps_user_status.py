"""Starting over keeps what the user decided about a marker.

`reset_gates` is how an agent starts automatic gating from scratch: the gates
go back to full range and the agent's own reasons are forgotten. A marker the
user excluded from automatic gating (a failed stain, an autofluorescence
channel) is the user's decision, not how a gate was made: the re-run must
still skip it. And a status the user sets while the reset runs is kept.
"""

import pytest

from plexora.agent import AgentSession, invoke, registry
from tests.autogate_fixtures import make_gating_project
from tests.test_gating_session import HARD, ok

pytestmark = pytest.mark.pro


@pytest.fixture(autouse=True)
def _gating():
    registry.discover(["gating"])


def test_reset_gates_keeps_a_marker_the_user_excluded_from_automatic_gating(tmp_path):
    from plexora.plugins.gating.server.autogate import provenance

    make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    ok(invoke(session, "set_gate", {"project": "gsynth", "marker": "CD4", "low": 150.0}))
    provenance.set_status("gsynth", "FOXP3", "excluded", principal="user")
    assert provenance.excluded("gsynth") == {"FOXP3"}

    ok(invoke(session, "reset_gates", {"project": "gsynth"}))

    # The agent's own provenance for CD4 is forgotten; the user's exclusion is not.
    assert "CD4" not in provenance.read("gsynth")
    assert provenance.status_of("gsynth", "FOXP3") == "excluded"
    assert provenance.excluded("gsynth") == {"FOXP3"}


def test_reset_gates_keeps_a_status_set_while_it_runs(tmp_path, monkeypatch):
    from plexora.plugins.gating import capabilities
    from plexora.plugins.gating.server.autogate import provenance

    make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    ok(invoke(session, "set_gate", {"project": "gsynth", "marker": "CD4", "low": 150.0}))

    real_reset = capabilities.model.reset_gates

    def reset_then_user_locks_cd8(*args, **kwargs):
        out = real_reset(*args, **kwargs)
        # The user locks another marker in the viewer while the reset is
        # between resetting the gates and rewriting provenance.
        provenance.set_status("gsynth", "CD8", "locked", principal="user")
        return out

    monkeypatch.setattr(capabilities.model, "reset_gates", reset_then_user_locks_cd8)
    reset = ok(invoke(session, "reset_gates", {"project": "gsynth", "markers": ["CD4"]}))
    assert reset["reset"] == ["CD4"]

    assert provenance.status_of("gsynth", "CD8") == "locked"
