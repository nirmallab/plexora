"""Plexora in Biocognia Intelligence (the `biocognia` app): its registration is
read without importing Plexora, the tools it offers as a guest are the
shipped manifest -- each with its permission written out, none a call the
bridge could not run for everyone -- and those tools run over the bridge."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from tools import intelligence_manifest


def test_the_registration_is_read_without_importing_plexora():
    probe = ("import sys, json\nimport plexora_intelligence as p\n"
             "print(json.dumps([p.PRODUCT.id, p.PRODUCT.name, list(p.PRODUCT.roles), "
             "sorted(m for m in sys.modules if m.split('.')[0] in ('plexora', 'flask', 'pydantic'))]))")
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    assert json.loads(done.stdout) == ["plexora", "Plexora", ["guest"], []]


def test_plexora_registers_itself_under_the_apps_entry_point():
    from importlib.metadata import entry_points

    found = {point.name: point.value for point in entry_points(group="biocognia.products")}
    assert found.get("plexora") == "plexora_intelligence:PRODUCT"


def test_the_shipped_manifest_is_what_the_registry_says():
    assert intelligence_manifest.main(["--check"]) == 0, "run python tools/intelligence_manifest.py"


def test_every_guest_tool_names_its_permission_and_is_one_the_bridge_runs_for_everyone():
    from plexora.agent import registry

    from plexora_intelligence import GUEST_TOOLS, PRODUCT

    manifest = PRODUCT.guest().manifest()
    assert [tool["descriptor"]["tool_name"] for tool in manifest["tools"]] == list(GUEST_TOOLS)
    registry.discover()
    for tool in manifest["tools"]:
        descriptor = tool["descriptor"]
        assert descriptor["permission"] in ("read", "reversible_write"), descriptor["tool_name"]
        assert intelligence_manifest.refusals(registry.get(descriptor["tool_name"])) == []
        assert tool["description"].startswith(registry.get(descriptor["tool_name"]).purpose)


def test_a_tool_the_bridge_could_not_run_is_refused_by_the_writer(monkeypatch):
    import plexora_intelligence

    monkeypatch.setattr(plexora_intelligence, "GUEST_TOOLS", ("write_gates_to_source", "render_region",
                                                              "gating_session_start", "no_such_tool"))
    with pytest.raises(ValueError) as refused:
        intelligence_manifest.manifest()
    said = str(refused.value)
    assert "write_gates_to_source: it is a source file write" in said
    assert "render_region: it returns an image" in said
    assert "gating_session_start: it needs the ai:gating:session entitlement" in said
    assert "no_such_tool: no such capability" in said


def test_the_registration_says_what_plexora_is_here():
    from importlib.metadata import version

    from plexora.agent.errors import CODES
    from plexora_intelligence import PRODUCT

    assert PRODUCT.home() is None and PRODUCT.app() == [sys.executable, "-m", "plexora"]
    assert set(PRODUCT.ai()) == {"ok", "reason"}
    guest = PRODUCT.guest()
    assert guest.provider == "plexora" and guest.manifest()["version"] == version("plexora")
    # Every code it maps is one of Plexora's, and every target one the runtime knows.
    from spatialagents.errors import known

    assert set(guest.codes) <= set(CODES)
    assert all((target["code"] if isinstance(target, dict) else target) in known() for target in guest.codes.values())


def test_it_holds_to_the_apps_contract():
    registry = pytest.importorskip("biocognia_intelligence.registry")
    from biocognia_intelligence.guests import check_manifest

    from plexora_intelligence import PRODUCT

    assert registry.check_registration(PRODUCT) == []
    assert check_manifest(PRODUCT.guest().manifest(), "plexora") == []


def test_a_guest_tool_runs_in_plexora_as_a_bridge_call(tmp_path):
    pytest.importorskip("biocognia_intelligence.guests")
    from biocognia_intelligence.guests import GuestTools
    from spatialagents.policy import Policy
    from spatialbridge import client

    from plexora.agent import registry
    from plexora.agent.bridge_provider import PlexoraProvider
    from plexora_intelligence import PRODUCT

    # A running Plexora, or `plexora mcp serve`, has discovered its capabilities.
    registry.discover()
    guest = PRODUCT.guest()
    tools = GuestTools(PRODUCT, guest, guest.manifest())
    client.register_inprocess("plexora", PlexoraProvider)
    try:
        listed = tools.invoke("plexora.list_projects", {}, policy=Policy())
        unknown = tools.invoke("plexora.inspect_project", {"project": "no-such-project"}, policy=Policy())
    finally:
        client.unregister_inprocess("plexora")
        client.close_all()
    assert listed["ok"] is True and listed["result"]["projects"] == [], listed
    # Plexora's own word for it, in the runtime's, with what to do next.
    error = unknown["error"]
    assert (error["code"], error["detail"]["plexora_code"]) == ("invalid_input", "unknown_project")
    assert error["detail"]["hint"] == "plexora.list_projects names the projects Plexora knows"
