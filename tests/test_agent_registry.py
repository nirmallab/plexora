"""The capability registry: discovery, collisions and the invocation pipeline."""

import sys

import pytest

from plexora.agent import registry
from plexora.agent.registry import Capability
from plexora.agent.schemas import AgentModel, ProjectInput


@pytest.fixture(autouse=True)
def clean_registry():
    registry._reset_for_tests()
    yield
    registry._reset_for_tests()


class Empty(AgentModel):
    pass


def _cap(name, owner="core", **kwargs):
    return Capability(name=name, owner=owner, purpose="p", permission="read",
                      input_model=kwargs.pop("input_model", Empty),
                      handler=kwargs.pop("handler", lambda call, inp: {"ok": 1}), **kwargs)


def test_names_collide_across_owners_but_not_on_reimport():
    registry.register(_cap("x.one"))
    registry.register(_cap("x.one"))
    with pytest.raises(ValueError):
        registry.register(_cap("x.one", owner="other"))
    with pytest.raises(ValueError):
        registry.register(_cap("x.two", tool_name="x_one"))


def test_tool_names_default_to_snake_case():
    assert _cap("gating.get_all").tool_name == "gating_get_all"


def test_unknown_permission_is_refused():
    with pytest.raises(ValueError):
        Capability(name="bad", owner="core", purpose="", permission="maybe",
                   input_model=Empty, handler=lambda c, i: None)


def test_discovery_imports_only_what_was_asked_for():
    # A fresh interpreter: this process has certainly imported gating already.
    import subprocess
    import textwrap
    from pathlib import Path

    import plexora

    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(Path(plexora.__file__).parent.parent)!r})
        from plexora.agent import registry
        names = registry.discover(["roi"])
        assert "roi.create" in names and "project.inspect" in names, names
        assert not any(n.startswith("gating.") for n in names), names
        assert "plexora.plugins.gating.capabilities" not in sys.modules
        print("ok")
    """)
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                          timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]


def test_invoke_reports_problems_instead_of_raising():
    registry.register(_cap("t.echo", input_model=ProjectInput))
    result = registry.invoke(object(), "t.echo", {"projekt": "x"})
    assert result["ok"] is False and result["error"]["code"] == "invalid_input"
    assert registry.invoke(object(), "missing", {})["error"]["code"] == "unknown_capability"


def test_invoke_runs_the_handler():
    registry.register(_cap("t.plain"))
    result = registry.invoke(object(), "t.plain", {})
    assert result == {"ok": True, "result": {"ok": 1}, "operation_id": result["operation_id"]}
    assert result["operation_id"].startswith("op_")
