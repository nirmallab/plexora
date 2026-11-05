"""Plexora in Biocognia Intelligence (the `biocognia` app): its registration is
read without importing Plexora, the tools it offers as a guest are the
shipped manifest -- each with its permission written out, none a source-file
write or a delete, each Pro tool with its entitlement -- and those tools run
over the bridge. Plexora pays for a workspace's conversation only where its AI
is licensed, labels the images it opens for an app that cannot hand them over,
and names the guest tool that imports one (`import_image`) for an app that can."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from tools import intelligence_manifest


def test_the_registration_is_read_without_importing_plexora():
    # Nor does labelling a file, naming the tool that imports it, or saying what Plexora pays for: the
    # gateway is built only when asked for.
    probe = ("import sys, json\nimport plexora_intelligence as p\n"
             "print(json.dumps([p.PRODUCT.id, p.PRODUCT.name, list(p.PRODUCT.roles), "
             "p.PRODUCT.classify('tonsil.ome.tif')['format'], p.PRODUCT.imports('tonsil.ome.tif')['tool'], "
             "list(p.PRODUCT.pay()['modules']), "
             "sorted(m for m in sys.modules\n"
             "       if m.split('.')[0] in ('plexora', 'flask', 'pydantic', 'spatialagents'))]))")
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    assert json.loads(done.stdout) == ["plexora", "Plexora", ["guest"], "ome-tiff", "import_image", ["chat"], []]


def test_plexora_registers_itself_under_the_apps_entry_point():
    from importlib.metadata import entry_points

    found = {point.name: point.value for point in entry_points(group="biocognia.products")}
    assert found.get("plexora") == "plexora_intelligence:PRODUCT"


def test_the_shipped_manifest_is_what_the_registry_says():
    assert intelligence_manifest.main(["--check"]) == 0, "run python tools/intelligence_manifest.py"


def test_every_guest_tool_names_its_permission_and_is_one_the_bridge_runs_without_an_approval():
    from plexora.agent import registry

    from plexora_intelligence import GUEST_TOOLS, PRODUCT

    manifest = PRODUCT.guest().manifest()
    assert [tool["descriptor"]["tool_name"] for tool in manifest["tools"]] == list(GUEST_TOOLS)
    registry.discover()
    for tool in manifest["tools"]:
        descriptor = tool["descriptor"]
        capability = registry.get(descriptor["tool_name"])
        assert descriptor["permission"] in ("read", "reversible_write"), descriptor["tool_name"]
        assert intelligence_manifest.refusals(capability) == []
        assert tool["description"].startswith(capability.purpose)
        # A bridge call is exempt from the external-MCP switch, so the guest's
        # description never says it needs it.
        assert "MCP" not in tool["description"], descriptor["tool_name"]


def test_the_pro_tools_carry_their_entitlement_and_say_they_are_pro():
    from plexora_intelligence import PRODUCT

    tools = {tool["descriptor"]["tool_name"]: tool for tool in PRODUCT.guest().manifest()["tools"]}
    entitled = {name: tool["descriptor"]["entitlement"] for name, tool in tools.items()
                if tool["descriptor"]["entitlement"] not in (None, "free")}
    assert entitled["ai_run_session"] == "ai" and entitled["gating_next"] == "ai:gating:session"
    assert entitled["qc_answer"] == "ai:qc:session" and entitled["render_qc_overview"] == "ai:qc:analytics"
    assert "list_projects" not in entitled and "render_region" not in entitled
    for name in entitled:
        assert "Part of Plexora Pro; on Free it answers license_required." in tools[name]["description"]
    assert "Returns an image" in tools["gating_next"]["description"]


def test_an_image_is_imported_through_a_free_guest_tool_the_app_offers():
    from plexora_intelligence import PRODUCT, RULES

    tools = {tool["descriptor"]["tool_name"]: tool for tool in PRODUCT.guest().manifest()["tools"]}
    imported = tools["import_image"]["descriptor"]
    # What the app (0.3.1 on) offers: a read or a write of Plexora's own state, on any licence.
    assert imported["permission"] == "reversible_write" and imported["entitlement"] is None
    assert imported["input_schema"]["required"] == ["image"]
    assert "Changes Plexora's own state" in tools["import_image"]["description"]
    assert "plexora.import_image" in RULES


def test_a_tool_the_bridge_could_not_run_is_refused_by_the_writer(monkeypatch):
    import plexora_intelligence

    monkeypatch.setattr(plexora_intelligence, "GUEST_TOOLS", ("write_gates_to_source", "render_region",
                                                              "gating_session_start", "no_such_tool"))
    with pytest.raises(ValueError) as refused:
        intelligence_manifest.manifest()
    said = str(refused.value)
    assert "write_gates_to_source: it is a source file write" in said
    assert "no_such_tool: no such capability" in said
    # An image and a Pro entitlement are no reason to refuse a tool any more.
    assert "render_region" not in said and "gating_session_start" not in said


def test_grants_are_the_licences_entitlements_and_nothing_on_free(monkeypatch):
    from biocognia import LicenseState

    from plexora import licensing
    from plexora_intelligence import PRODUCT

    guest = PRODUCT.guest()
    pro = LicenseState(state="active", entitlements=("ai", "volume"))
    monkeypatch.setattr(licensing, "peek", lambda: pro)
    assert guest.grants() == ("ai", "volume")
    monkeypatch.setattr(licensing, "peek", lambda: LicenseState(state="free"))
    assert guest.grants() == ()

    def unreadable():
        raise OSError("the licence file is locked")

    monkeypatch.setattr(licensing, "peek", unreadable)
    assert guest.grants() == ()


def test_plexora_pays_for_the_conversation_only_where_its_ai_is_licensed(monkeypatch):
    from spatialagents.gateway import GatewayError, RefusingGateway

    from plexora import licensing
    from plexora.ai.harness.gateway import GatewayClient
    from plexora_intelligence import PRODUCT

    allowed = {"ai:chat": False}
    monkeypatch.setattr(licensing, "allows", lambda entitlement: allowed.get(entitlement, False))
    paid = PRODUCT.pay()
    # The conversation only: the app refuses a plan's call before it is sent.
    assert paid["modules"] == ("chat",)
    factory = paid["gateway_factory"]

    refusing = factory()
    assert isinstance(refusing, RefusingGateway) and refusing.dev is False
    with pytest.raises(GatewayError) as refused:
        refusing.messages(object(), idempotency_key="k", on_delta=None)
    # The sentence the app shows is the one `ai()` gives.
    assert refused.value.code == "no_license" and str(refused.value) == PRODUCT.ai()["reason"]

    # The device is activated while the app runs: the same factory pays from its next call.
    allowed["ai:chat"] = True
    client = factory()
    assert isinstance(client, GatewayClient) and isinstance(client.dev, bool)
    allowed["ai:chat"] = False
    assert isinstance(factory(), RefusingGateway)


def test_a_conversations_task_plexora_does_not_name_goes_out_as_plexoras(monkeypatch):
    from spatialagents.gateway import GatewayError
    from spatialagents.wire import ModelRequest

    from plexora import licensing
    from plexora.ai import tasks
    from plexora_intelligence import PRODUCT

    monkeypatch.setattr(licensing, "allows", lambda entitlement: True)
    client = PRODUCT.pay()["gateway_factory"]()
    assert "chat.researcher" not in tasks.tasks()
    assert client.wire_task("chat.researcher", module="chat") == "plexora.chat.researcher"
    assert client.wire_task("chat.turn", module="chat") == "plexora.chat.turn"

    # Through `messages`, as the app calls it: the body names Plexora's task, nothing raises before the send.
    sent = []

    def gateway(method, path, body=None, headers=None, timeout=60):
        sent.append(body)
        raise GatewayError("stop here", code="no_license", status=403)

    monkeypatch.setattr(client, "open", gateway)
    request = ModelRequest(capability="text_reasoning", system=[], messages=[{"role": "user", "content": "hi"}],
                           context={"feature": "chat"}, task="chat.researcher")
    with pytest.raises(GatewayError):
        client.messages(request, idempotency_key="k", on_delta=lambda text: None)
    assert [body["task"] for body in sent] == ["plexora.chat.researcher"]


@pytest.mark.parametrize("name, kind", [
    ("tonsil.ome.tif", "ome-tiff"), ("tonsil.ome.tiff", "ome-tiff"), ("tonsil.tif", "tiff"),
    ("TONSIL.TIFF", "tiff"), ("scan.qptiff", "qptiff"), ("tonsil.ome.zarr", "ome-zarr"),
    ("tonsil.ome.zarr/", "ome-zarr"), (r"C:\data\tonsil.ome.tif", "ome-tiff"),
])
def test_an_image_plexora_opens_is_labelled_plexoras_and_never_opened_from_here(tmp_path, name, kind):
    from plexora_intelligence import PRODUCT

    for path in (name, tmp_path / name.rstrip("/").split("\\")[-1]):
        # By name: the path need not exist, and no file is read.
        assert PRODUCT.classify(path) == {"format": kind, "label": "Image · open it in Plexora", "action": None,
                                          "product": "plexora"}


# A plain `.zarr` included: by its name it may be an AnnData or SpatialData store.
@pytest.mark.parametrize("name", ["cells.csv", "tonsil_cell.csv", "tonsil.h5ad", "cells.zarr", "cells.zarr/",
                                  "notes.txt", "figure.png", "tonsil.tif.csv", "tonsil"])
def test_anything_else_is_not_plexoras_to_label(tmp_path, name):
    from plexora_intelligence import PRODUCT

    assert PRODUCT.classify(name) is None
    assert PRODUCT.classify(tmp_path / name) is None


@pytest.mark.parametrize("name, kind", [
    ("tonsil.ome.tif", "ome-tiff"), ("tonsil.ome.tiff", "ome-tiff"), ("tonsil.tif", "tiff"),
    ("TONSIL.TIFF", "tiff"), ("scan.qptiff", "qptiff"), ("tonsil.ome.zarr", "ome-zarr"),
    ("tonsil.ome.zarr/", "ome-zarr"), (r"C:\data\tonsil.ome.tif", "ome-tiff"),
])
def test_an_image_plexora_opens_is_handed_to_import_image_as_it_is_named(tmp_path, name, kind):
    from plexora_intelligence import PRODUCT

    for path in (name, tmp_path / name.rstrip("/").split("\\")[-1]):
        # By name, as `classify`: the path need not exist, and the tool is what reads it.
        assert PRODUCT.imports(path) == {"format": kind, "label": "Image · Plexora", "tool": "import_image",
                                         "arguments": {"image": str(path)}}
        # `classify` still says what an app that cannot import it shows.
        assert PRODUCT.classify(path)["action"] is None


@pytest.mark.parametrize("name", ["cells.csv", "tonsil_cell.csv", "tonsil.h5ad", "cells.zarr", "cells.zarr/",
                                  "notes.txt", "figure.png", "tonsil.tif.csv", "tonsil"])
def test_anything_else_is_not_plexoras_to_import(tmp_path, name):
    from plexora_intelligence import PRODUCT

    assert PRODUCT.imports(name) is None
    assert PRODUCT.imports(tmp_path / name) is None


def test_the_registration_says_what_plexora_is_here():
    from importlib.metadata import version

    from plexora.agent.errors import CODES
    from plexora_intelligence import PRODUCT

    assert PRODUCT.home() is None and PRODUCT.app() == [sys.executable, "-m", "plexora"]
    assert PRODUCT.page == {"formats": {"files": [".ome.tiff", ".ome.tif", ".qptiff", ".tiff", ".tif"],
                                        "folders": [".ome.zarr"]}}
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


def test_an_image_the_app_hands_over_becomes_a_project_over_the_bridge(tmp_path):
    pytest.importorskip("biocognia_intelligence.guests")
    from biocognia_intelligence.guests import GuestTools
    from spatialagents.policy import Policy
    from spatialbridge import client

    from plexora.agent import registry
    from plexora.agent.bridge_provider import PlexoraProvider
    from plexora_intelligence import PRODUCT
    from tests.agent_fixtures import blob_scene, write_image_pyramid

    registry.discover()
    image = write_image_pyramid(tmp_path / "tonsil.ome.tif", blob_scene(size=256, grid=4, radius=12)[0])
    takes = PRODUCT.imports(image)
    guest = PRODUCT.guest()
    tools = GuestTools(PRODUCT, guest, guest.manifest())
    client.register_inprocess("plexora", PlexoraProvider)
    try:
        made = tools.invoke(f"plexora.{takes['tool']}", takes["arguments"], policy=Policy())
        again = tools.invoke(f"plexora.{takes['tool']}", takes["arguments"], policy=Policy())
        missing = tools.invoke("plexora.import_image", {"image": str(tmp_path / "gone.ome.tif")}, policy=Policy())
    finally:
        client.unregister_inprocess("plexora")
        client.close_all()
    assert made["ok"] is True, made
    assert (made["result"]["project"], made["result"]["created"]) == ("tonsil", True)
    assert made["result"]["image"] == str(image) and made["operation_id"]
    assert (again["result"]["project"], again["result"]["created"]) == ("tonsil", False)
    assert missing["ok"] is False and missing["error"]["code"] == "invalid_input"
    assert "no image at" in missing["error"]["message"]


# -- what loading a tool costs a conversation ------------------------------------------------

#: The input schemas a conversation is shown, as compact JSON (what the app's
#: `load_tool` sends, less its indentation). Before 2026-11-01:
#: gating_session_start 18,595 (19,985 with pydantic's titles), render_region
#: 9,014, qc_session_start 7,086, ai_run_session 2,579; all 48 tools 84,509.
SCHEMA_CEILINGS = {"gating_session_start": 11_500, "render_region": 8_000,
                   "qc_session_start": 6_500, "ai_run_session": 2_100}
ALL_SCHEMAS_CEILING = 72_000


def _schemas():
    from plexora_intelligence import PRODUCT

    return {tool["descriptor"]["tool_name"]: tool["descriptor"]["input_schema"]
            for tool in PRODUCT.guest().manifest()["tools"]}


def test_a_guest_tools_schema_is_small_enough_to_load():
    sizes = {name: len(json.dumps(schema, ensure_ascii=False)) for name, schema in _schemas().items()}
    for name, ceiling in SCHEMA_CEILINGS.items():
        assert sizes[name] <= ceiling, (name, sizes[name])
    assert sum(sizes.values()) <= ALL_SCHEMAS_CEILING, sum(sizes.values())


def test_a_compact_schema_keeps_every_option_a_call_can_use():
    """Smaller, never fewer: every field of every guest tool's input model is
    in its schema, required where the model requires it, with its enum and
    default."""
    from plexora.agent import registry

    registry.discover()
    for name, schema in _schemas().items():
        model = registry.get(name).input_model
        assert "title" not in schema, name
        assert set(schema.get("properties") or {}) == set(model.model_fields), name
        assert sorted(schema.get("required") or ()) == sorted(
            field for field, info in model.model_fields.items() if info.is_required()), name
    start = _schemas()["gating_session_start"]["properties"]
    assert start["engine"]["enum"] == ["v1", "v3", "v4"] and start["engine"]["default"] == "v3"
    assert start["setup"]["enum"] == ["ask", "wait", "fail"]
    assert "expression_setup" in start["setup"]["description"]


def test_compact_schema_drops_only_what_says_nothing():
    from typing import Annotated, Literal

    from pydantic import BaseModel, Field

    from plexora.agent.schemas import compact_schema

    class Cat(BaseModel):
        kind: Literal["cat"] = "cat"

    class Dog(BaseModel):
        kind: Literal["dog"] = "dog"

    class Model(BaseModel):
        title: str | None = None                       # a property named title stays
        size: int | None = Field(None, ge=1, description="Optional, null by default.")
        cap: int | None = 6                            # null is not the default: kept
        must: int | None                               # required and nullable: kept
        pet: Annotated[Cat | Dog, Field(discriminator="kind")] = Cat()
        meta: dict = {"title": "kept as data"}

    schema = compact_schema(Model.model_json_schema())
    props = schema["properties"]
    assert "title" not in schema and set(props) == {"title", "size", "cap", "must", "pet", "meta"}
    assert props["title"] == {"type": "string"}
    assert props["size"] == {"minimum": 1, "type": "integer", "description": "Optional, null by default."}
    assert {"type": "null"} in props["cap"]["anyOf"] and props["cap"]["default"] == 6
    assert {"type": "null"} in props["must"]["anyOf"] and schema["required"] == ["must"]
    assert "discriminator" not in props["pet"] and len(props["pet"]["oneOf"]) == 2
    assert props["meta"]["default"] == {"title": "kept as data"}
    assert all("title" not in node for node in schema["$defs"].values())


def test_the_rules_say_one_wallet_and_a_run_quoted_as_a_ceiling():
    """A Plexora AI run and this conversation draw on the organisation's one
    AI wallet, and a run's quote is what it may cost at most: the 2026-11-01
    conversation told the person a run cost "about 875 Plexora AI credits"
    and a step-by-step session "this conversation's credits, not Plexora's";
    the run cost 478 of its 875."""
    from plexora.ai.harness import capabilities as ai_caps

    from plexora_intelligence import RULES

    assert "one AI credit wallet" in RULES and '"up to N credits"' in RULES
    assert "not this workspace's" not in RULES and "Plexora's AI credits" not in RULES
    assert "never more than the quote" in RULES and "metered" in RULES
    tools = {tool["descriptor"]["tool_name"]: tool for tool in _manifest_tools()}
    assert "a ceiling" in tools["ai_run_session"]["description"]
    assert "up to" in tools["ai_balance"]["description"]
    assert "never more than the quote" in ai_caps.CHARGING and "one AI wallet" in ai_caps.CHARGING
    for tool in tools.values():
        assert "Plexora AI credits" not in tool["description"], tool["descriptor"]["tool_name"]


def _manifest_tools():
    from plexora_intelligence import PRODUCT

    return PRODUCT.guest().manifest()["tools"]
