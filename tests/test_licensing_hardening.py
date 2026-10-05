"""Hardening: the promises licensing makes, tested as promises.

- A refusal happens at the server, whatever the client does or hides.
- Every way the licence service can fail leaves Paid working from the
  certificate in hand, and Free untouched.
- Expiry removes nothing: every gate, provenance row and export survives,
  editable, on Free.
- The render and data paths never look at the licence.
- The manual half of gating does not depend on the automatic (Paid) half, so
  the AI half can one day ship as a package of its own.
"""

import ast
import json
import threading
import time
from pathlib import Path

import pytest

import plexora
from plexora import licensing
from plexora.agent import AgentSession, invoke, registry
from plexora.licensing import state, store
from tests.agent_fixtures import make_synthetic_project

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def session(tmp_path):
    make_synthetic_project(tmp_path)
    registry.discover(["gating", "roi", "qc"])
    return AgentSession()


@pytest.fixture
def client():
    # A plain test client, as the rest of the suite uses: no TESTING flag,
    # which would change error handling for every test after this one.
    with plexora.app.test_client() as test_client:
        yield test_client


# -- the server decides ------------------------------------------------------------------

def test_the_mcp_protocol_itself_refuses_a_paid_tool_on_free(session):
    """Option B: every tool is listed on every transport, and the CALL refuses
    -- through the real protocol, not just the registry."""
    pytest.importorskip("mcp")
    import anyio
    from mcp import Client

    from plexora.mcp.server import build_server

    server = build_server(session)

    async def go():
        async with Client(server) as mcp_client:
            listed = await mcp_client.list_tools()
            tools = {tool.name for tool in getattr(listed, "tools", listed)}
            refused = await mcp_client.call_tool("gating_session_start", {"project": "synth"})
            allowed = await mcp_client.call_tool("set_gate", {"project": "synth", "marker": "CD8",
                                                              "low": 900})
            return tools, refused, allowed

    tools, refused, allowed = anyio.run(go)
    assert "gating_session_start" in tools, "Paid tools are listed, never hidden"
    assert refused.is_error
    text = refused.content[0].text
    assert json.loads(text[text.index("{"):])["error"]["code"] == "license_required"
    assert not allowed.is_error


def _over_mcp(server, *calls):
    """Each `(tool, arguments)` called through an in-process MCP client; the
    results as `(is_error, parsed problem or None)`."""
    import anyio
    from mcp import Client

    async def go():
        out = []
        async with Client(server) as mcp_client:
            for tool, arguments in calls:
                result = await mcp_client.call_tool(tool, arguments)
                text = result.content[0].text if result.content else ""
                problem = None
                if result.is_error and "{" in text:
                    problem = json.loads(text[text.index("{"):]).get("error")
                out.append((result.is_error, problem))
        return out

    return anyio.run(go)


def test_a_paid_tool_over_mcp_needs_mcp_access_too(session, license_issuer):
    """External MCP access is its own grant: a Paid licence without it runs
    the AI tools in Plexora but not from an outside agent."""
    pytest.importorskip("mcp")
    from plexora.mcp.server import build_server

    license_issuer.install()  # ["ai"]
    refused, allowed = _over_mcp(build_server(session),
                                 ("gating_session_start", {"project": "synth"}),
                                 ("set_gate", {"project": "synth", "marker": "CD8", "low": 900}))
    assert refused[0] and refused[1]["code"] == "license_required"
    assert refused[1]["detail"]["entitlement"] == "mcp"
    assert "external MCP access" in refused[1]["detail"]["hint"]
    assert not allowed[0], "Free tools answer over MCP on any licence"
    direct = invoke(session, "gating.session_start", {"project": "synth"})
    assert (direct.get("error") or {}).get("code") != "license_required", \
        "the in-app path never needs mcp"


def test_an_mcp_only_licence_runs_free_tools_and_refuses_ai_ones(session, license_issuer):
    pytest.importorskip("mcp")
    from plexora.mcp.server import build_server

    license_issuer.install(license_issuer.issue(entitlements=["mcp"]))
    refused, allowed = _over_mcp(build_server(session),
                                 ("gating_session_start", {"project": "synth"}),
                                 ("set_gate", {"project": "synth", "marker": "CD8", "low": 900}))
    assert refused[1]["code"] == "license_required"
    assert refused[1]["detail"]["entitlement"] == "ai:gating:session"
    assert not allowed[0]


def test_free_tools_over_mcp_never_read_the_licence(session, monkeypatch):
    pytest.importorskip("mcp")
    from plexora.mcp.server import build_server

    server = build_server(session)

    def boom(*args, **kwargs):
        raise AssertionError("a Free tool read the licence")

    monkeypatch.setattr(state, "current", boom)
    results = _over_mcp(server, ("list_projects", {}),
                        ("set_gate", {"project": "synth", "marker": "CD8", "low": 900}))
    assert not any(is_error for is_error, _ in results)


def _recheck_server(session, monkeypatch, license_service):
    from plexora.mcp.server import build_server

    monkeypatch.delenv(store.ENV_NO_HEARTBEAT, raising=False)
    server = build_server(session, license_recheck=True)
    stop = server._plexora_runtime.license_recheck
    assert stop is not None, "a cached certificate online is rechecked"
    return server, stop


def test_a_revocation_reaches_a_running_mcp_server(session, license_issuer, license_service,
                                                   monkeypatch):
    pytest.importorskip("mcp")
    from tests.license_fixtures import PAID_TEST_GRANTS

    license_issuer.install(license_issuer.issue(entitlements=list(PAID_TEST_GRANTS)))
    license_service.script("/v1/refresh", 200, {"status": "revoked", "reason": "license_revoked",
                                                "server_time": int(time.time())})
    server, stop = _recheck_server(session, monkeypatch, license_service)
    try:
        (refused,) = _over_mcp(server, ("gating_session_start", {"project": "synth"}))
    finally:
        stop.set()
    assert refused[1]["code"] == "license_required"
    assert refused[1]["detail"]["state"] == "revoked"
    assert license_service.of("/v1/refresh")[0]["json"]["client"] == "mcp"


def test_a_grant_change_reaches_a_running_mcp_server(session, license_issuer, license_service,
                                                     monkeypatch):
    pytest.importorskip("mcp")
    from tests.license_fixtures import PAID_TEST_GRANTS

    license_issuer.install(license_issuer.issue(entitlements=list(PAID_TEST_GRANTS)))
    license_service.script("/v1/refresh", 200, {"status": "renewed",
                                                "certificate": license_issuer.issue(),
                                                "server_time": int(time.time())})
    server, stop = _recheck_server(session, monkeypatch, license_service)
    try:
        (refused,) = _over_mcp(server, ("gating_session_start", {"project": "synth"}))
    finally:
        stop.set()
    assert licensing.current().entitlements == ("ai",)
    assert refused[1]["detail"]["entitlement"] == "mcp"
    direct = invoke(session, "gating.session_start", {"project": "synth"})
    assert (direct.get("error") or {}).get("code") != "license_required"


def test_the_mcp_hint_never_claims_manual_tools_keep_working():
    from plexora.licensing import guards

    for name in ("free", "paid_active", "expired", "revoked", "invalid"):
        hint = guards.hint_for(guards.MCP, name)
        assert "keep working" not in hint and "Free tools still answer" in hint, name


def test_a_hidden_menu_item_is_not_the_enforcement(session):
    """Calling the capability directly, with no UI at all, is refused the same."""
    assert invoke(session, "gating.next", {"session_id": "whatever"})["error"]["code"] == \
        "license_required"


def test_the_client_holds_nothing_that_can_sign_a_certificate():
    from plexora.licensing import keys

    for value in keys.PUBLIC_KEYS.values():
        assert len(keys.raw(value)) == 32  # public keys; nothing longer, nothing private
    source = (REPO / "plexora" / "licensing" / "certificate.py").read_text()
    assert "Ed25519PrivateKey" not in source


def test_the_default_server_is_the_deployed_service():
    """Read from source: the suite patches `store.DEFAULT_SERVER` to "" so no
    test can reach production."""
    import re

    tree = ast.parse((REPO / "plexora" / "licensing" / "store.py").read_text())
    default = next(node.value.value for node in tree.body if isinstance(node, ast.Assign)
                   and any(getattr(t, "id", None) == "DEFAULT_SERVER" for t in node.targets))
    assert default.startswith("https://") and not default.endswith("/")
    wrangler = REPO / "licensing" / "wrangler.toml"
    if not wrangler.exists():
        pytest.skip("the licence service's source is not in this checkout")
    base = re.search(r'^PUBLIC_BASE_URL = "([^"]+)"', wrangler.read_text(), re.M).group(1)
    assert default == base


# -- every way the service can fail ------------------------------------------------------------

def _broken_service(mode, monkeypatch):
    from tests.license_fixtures import FakeLicenseService

    monkeypatch.delenv(store.ENV_OFFLINE, raising=False)
    if mode == "unreachable":
        monkeypatch.setenv(store.ENV_SERVER, "http://127.0.0.1:9")
        return None
    if mode == "dns":
        monkeypatch.setenv(store.ENV_SERVER, "http://licence-service.invalid")
        return None
    service = FakeLicenseService().start()
    monkeypatch.setenv(store.ENV_SERVER, service.url)
    if mode == "html_500":
        service.script("/v1/refresh", 500, b"<html>Cloudflare error 1101</html>", times=5)
    elif mode == "garbage_200":
        service.script("/v1/refresh", 200, b"\x00\xffnot json", times=5)
    elif mode == "wrong_shape":
        service.script("/v1/refresh", 200, ["a", "list"], times=5)
    elif mode == "signing_down":
        service.script("/v1/refresh", 503, {"error": {"code": "signing_unavailable", "message": "x"}},
                       times=5)
    return service


@pytest.mark.parametrize("mode", ["unreachable", "dns", "html_500", "garbage_200", "wrong_shape",
                                  "signing_down"])
def test_a_failing_service_changes_nothing(mode, license_issuer, monkeypatch):
    license_issuer.install(last_validated=time.time() - 30 * 86400)
    service = _broken_service(mode, monkeypatch)
    try:
        current = licensing.current()
        assert current.paid
        outcome = state.heartbeat(current)
        assert outcome in ("failed", "ignored")
        assert licensing.current().paid, f"{mode}: Paid dropped because the service failed"
        assert licensing.allows(None)
    finally:
        if service is not None:
            service.stop()


def test_a_revocation_is_the_only_answer_that_turns_paid_off(license_issuer, license_service):
    license_issuer.install(last_validated=time.time() - 30 * 86400)
    license_service.script("/v1/refresh", 200, {"status": "revoked", "server_time": int(time.time())})
    state.heartbeat(licensing.current())
    assert licensing.current().state == "revoked"


# -- expiry removes nothing -----------------------------------------------------------------------

def test_expiry_removes_nothing(session, license_issuer, tmp_path):
    now = int(time.time())
    license_issuer.install(license_issuer.issue(expires_at=now + 3600, grace_days=0))
    # Work done while Paid: a manual gate, and a Paid capability's output.
    assert invoke(session, "set_gate", {"project": "synth", "marker": "CD8", "low": 900})["ok"]
    context = invoke(session, "set_panel_context", {"project": "synth", "tissue": "tonsil"})
    before = {p: p.stat().st_mtime for p in tmp_path.rglob("*") if p.is_file()
              and ".plexora-license" not in p.parts}

    # The licence runs out.
    state._clock = lambda: now + 7200
    try:
        licensing.reset_for_tests()
        assert licensing.current().state == "expired"
        after = {p for p in tmp_path.rglob("*") if p.is_file() and ".plexora-license" not in p.parts}
        assert set(before) <= after, "expiry deleted something"
        # Everything made is still readable, exportable and editable, on Free.
        gate = invoke(session, "get_gate", {"project": "synth", "marker": "CD8"})
        assert gate["ok"] and gate["result"]["gate"]["low"] == 900
        assert invoke(session, "get_gate_provenance", {"project": "synth"})["ok"]
        exported = invoke(session, "export_gates", {"project": "synth"})
        assert exported["ok"], exported
        assert invoke(session, "set_gate", {"project": "synth", "marker": "CD8", "low": 950})["ok"]
        # The Paid capability itself is what stops, and it says why.
        refused = invoke(session, "get_panel_context", {"project": "synth"})
        assert refused["error"]["detail"]["state"] == "expired"
        del context
    finally:
        state._clock = time.time


def test_reading_qc_results_survives_expiry(session, license_issuer, tmp_path):
    """QC made while Paid stays readable, changeable and exportable on Free;
    only the session tools stop."""
    now = int(time.time())
    license_issuer.install(license_issuer.issue(expires_at=now + 3600, grace_days=0))
    square = {"type": "Polygon", "coordinates": [[[10, 10], [200, 10], [200, 200], [10, 200],
                                                  [10, 10]]]}
    assert invoke(session, "create_roi", {"project": "synth", "category": "QC: Tissue fold",
                                          "geometry": square})["ok"]
    assert invoke(session, "refresh_qc", {"project": "synth"})["ok"]
    state._clock = lambda: now + 7200
    try:
        licensing.reset_for_tests()
        assert licensing.current().state == "expired"
        found = invoke(session, "get_qc_results", {"project": "synth"})
        assert found["ok"] and found["result"]["regions"], found
        assert invoke(session, "set_qc_strictness", {"project": "synth",
                                                     "preset": "strict"})["ok"]
        assert invoke(session, "export_qc", {"project": "synth"})["ok"]
        refused = invoke(session, "qc_session_start", {"project": "synth"})
        assert refused["error"]["code"] == "license_required"
        assert refused["error"]["detail"]["state"] == "expired"
    finally:
        state._clock = time.time


# -- the render and data paths never look at the licence -------------------------------------------

def test_tiles_and_data_reads_never_look_at_the_licence(client, tmp_path, monkeypatch):
    make_synthetic_project(tmp_path)
    assert client.get("/synth").status_code == 200   # the page's one peek happens here
    calls = []
    real = state.current

    def counted(*args, **kwargs):
        calls.append(threading.current_thread().name)
        return real(*args, **kwargs)

    monkeypatch.setattr(state, "current", counted)
    for path in ("/generated/data/synth/CD3_files/0/0_0.png",
                 "/generated/data/synth/CD8_files/0/0_0.png",
                 "/generated/data/synth/CD3_files/1/0_0.png"):
        assert client.get(path).status_code < 500, path
    assert calls == [], f"the data path asked about the licence: {calls}"


def test_a_broken_licensing_module_breaks_no_page(client, tmp_path, monkeypatch):
    make_synthetic_project(tmp_path)

    def broken(*args, **kwargs):
        raise RuntimeError("licensing is broken")

    monkeypatch.setattr(state, "current", broken)
    for path in ("/", "/synth", "/settings", "/license/status",
                 "/generated/data/synth/CD3_files/0/0_0.png"):
        assert client.get(path).status_code == 200, path


def test_importing_plexora_resolves_no_licence(tmp_path):
    """Startup reads no licence file: resolution waits for a Paid feature."""
    import os
    import subprocess
    import sys

    licence_dir = tmp_path / "lic"
    licence_dir.mkdir()
    (licence_dir / "license.json").write_text("{this is not json")
    code = ("import plexora, plexora.licensing.state as s, sys; "
            "print(s._state is None, 'cryptography' in sys.modules)")
    env = {**os.environ, "PLEXORA_LICENSE_DIR": str(licence_dir), "PLEXORA_LICENSE_OFFLINE": "1",
           "PLEXORA_DATA_PATH": str(tmp_path / "data"), "PLEXORA_TELEMETRY": "off"}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                         cwd=REPO, timeout=120)
    assert out.stdout.strip().splitlines()[-1] == "True False", out.stderr[-2000:]


# -- the boundary for a future plexora-ai package ------------------------------------------------------

def _imports(path: Path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def test_gating_cores_shared_maths_imports_nothing_from_autogate():
    for rel in ("plexora/plugins/gating/server/mixture.py",
                "plexora/plugins/gating/server/provenance.py"):
        assert not any("autogate" in name for name in _imports(REPO / rel)), rel


#: Every place gating's manual half, or core, still reaches into the automatic
#: (Paid) package. Pinned so the list can only shrink on purpose; each entry is
#: a step in docs/LICENSING.md's "splitting out plexora-ai".
KNOWN_COUPLINGS = {
    "plexora/plugins/gating/capabilities.py",      # capabilities() aggregates the AI capabilities
    "plexora/plugins/gating/server/routes.py",     # agent-session control and events routes
    "plexora/plugins/gating/server/tableops.py",   # registers autogate's node operations
    "plexora/mcp/resources_gating.py",             # gating session resources
    "plexora/ai/skills.py",                        # skills read autogate's schemas
    "plexora/ai/bench.py",                         # the autogate benchmark
    # Plexora's own harness drives autogate sessions like any agent: their
    # answer models, reading guide and events.
    "plexora/ai/harness/decision.py",
    "plexora/ai/harness/prefix.py",
    "plexora/ai/harness/schema.py",
    "plexora/ai/harness/capabilities.py",
    "plexora/ai/harness/route_bench.py",          # the routing bench gates synthetic scenes
}


def test_the_remaining_couplings_are_the_documented_ones():
    found = set()
    for path in (REPO / "plexora").rglob("*.py"):
        rel = path.relative_to(REPO).as_posix()
        if "/autogate/" in rel or "/tests/" in rel or rel.endswith(("capabilities_session.py",
                                                                    "capabilities_autogate.py")):
            continue
        if any("gating.server.autogate" in name for name in _imports(path)):
            found.add(rel)
    assert found == KNOWN_COUPLINGS


# -- telemetry observes, never enforces ---------------------------------------------------------

def test_telemetry_carries_the_tier_and_never_the_licence(paid_license):
    from plexora.telemetry import schema
    from plexora.telemetry.client import telemetry

    block = telemetry.client_block()
    assert block["license_tier"] == "paid"
    assert schema.validate_client(block)
    assert not set(schema.RESERVED_LICENSE_FIELDS) & set(block)
    text = json.dumps(block)
    assert "lic_" not in text and "acc_" not in text and "crt_" not in text


def test_telemetry_says_free_by_default():
    from plexora.telemetry.client import telemetry

    assert telemetry.client_block()["license_tier"] == "free"


def test_turning_telemetry_off_changes_no_licence(paid_license, monkeypatch):
    monkeypatch.setenv("PLEXORA_TELEMETRY", "off")
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    licensing.reset_for_tests()
    assert licensing.allows("ai:gating")


def test_licensing_never_imports_telemetry():
    for path in (REPO / "plexora" / "licensing").rglob("*.py"):
        assert not any("telemetry" in name for name in _imports(path)), path
