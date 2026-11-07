"""The website's licence, held by the page (plan D8), from Python's side.

On the website the page (client services/licenseClient.js) holds the
certificate and runs the device flow; Python reads the files it writes
(plexora/browser/licence.py `adopt`), asks it for the AI token
(`PageTokenSource`), and leaves the licence routes it answers alone. The
page's own behaviour is held by tests/js/license_client_probe.mjs and
tests/js/license_probe.mjs (tests/test_license_js.py runs both).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import plexora
from plexora import licensing
from plexora.browser import licence as page_licence
from plexora.licensing import LICENSING

REPO = Path(__file__).resolve().parents[1]
VERIFY_JS = REPO / "plexora" / "client" / "src" / "js" / "services" / "licenseVerify.js"
CLIENT_JS = REPO / "plexora" / "client" / "src" / "js" / "services" / "licenseClient.js"

#: A browser's device secret, as the page makes one (32 random bytes, base64url).
SECRET = "page-secret-0123456789abcdefghijklmnopqrstuvwx"


@pytest.fixture
def client():
    with plexora.app.test_client() as test_client:
        yield test_client


@pytest.fixture
def trusted(monkeypatch):
    """`biocognia.keys.PUBLIC_KEYS` as a copy this test may change (adopt's
    test keys), put back afterwards."""
    from biocognia import keys

    monkeypatch.setattr(keys, "PUBLIC_KEYS", dict(keys.PUBLIC_KEYS))
    return keys


def _page_certificate(issuer, *, commitment=False, **claims):
    from biocognia import environment

    binding = environment.binding_of(SECRET)
    if commitment:
        claims.update(bind=environment.commitment_of(binding), bv=environment.BIND_COMMITMENT)
    else:
        claims.setdefault("bind", binding)
    return issuer.issue(envt="browser", env="env_web0001", **claims)


def _files(cert, **record):
    return {
        "license": {"certificate": cert, "source": "activation", "credential_hint": None,
                    "installed_at": 1, "last_validated": 1, "last_attempt": 1, "hwm": 1,
                    "server": "https://api.biocognia.com",
                    "environment": {"id": "env_web0001", "name": "Chrome on Windows", "type": "browser"},
                    "revoked": None, **record},
        "environment": {"schema": 1, "secret": SECRET, "created_at": 1},
    }


@pytest.mark.parametrize("commitment", [False, True], ids=["binding", "commitment"])
def test_the_page_files_are_the_packages_own_and_unlock_pro(license_issuer, trusted, commitment):
    from biocognia import store

    from plexora.licensing import guards

    cert = _page_certificate(license_issuer, commitment=commitment, ent=["ai", "volume"])
    status = page_licence.adopt(_files(cert))
    assert status["plan"] == "pro" and status["state"] == "pro_active" and status["paid"] is True
    assert status["environment"] == {"id": "env_web0001", "type": "browser", "name": "Chrome on Windows"}
    assert cert[:40] not in json.dumps(status), "the status never carries the certificate"
    # What biocognia reads on its own, as on a desktop.
    assert store.read_environment()["secret"] == SECRET
    assert LICENSING.store.read()["certificate"] == cert
    assert licensing.peek().state == "active"
    assert licensing.allows("ai:gating:session") and licensing.allows("volume")
    guards.check("ai:gating:session")


def test_forget_leaves_no_file_and_reads_as_free(license_issuer, trusted):
    from biocognia import store

    page_licence.adopt(_files(_page_certificate(license_issuer)))
    status = page_licence.adopt({"license": None, "environment": None})
    assert status["plan"] == "free" and status["state"] == "free"
    assert not LICENSING.store.path.exists()
    assert not store.environment_path().exists()
    assert licensing.peek().state == "free"


def test_a_released_browser_keeps_its_secret(license_issuer, trusted):
    from biocognia import store

    page_licence.adopt(_files(_page_certificate(license_issuer)))
    status = page_licence.adopt({"license": None, "environment": _files("x")["environment"]})
    assert status["state"] == "free"
    assert store.read_environment()["secret"] == SECRET


def test_another_browsers_certificate_is_invalid(license_issuer, trusted):
    files = _files(_page_certificate(license_issuer))
    files["environment"]["secret"] = "another-browser-secret-0123456789abcdefghij"
    status = page_licence.adopt(files)
    assert status["plan"] == "free" and status["state"] == "invalid"
    assert status["reason"] == "environment_mismatch"


def test_only_what_the_package_writes_is_written(license_issuer, trusted):
    from biocognia import store

    files = _files(_page_certificate(license_issuer), email="ada@lab.example.org", key="BIOC-AAAA-BBBB-CCCC-DDDD")
    files["environment"]["install_id"] = "f" * 32
    page_licence.adopt(files)
    written = LICENSING.store.path.read_text(encoding="utf-8") + store.environment_path().read_text(encoding="utf-8")
    assert "ada@lab" not in written and "BIOC-AAAA" not in written and "f" * 32 not in written


def test_a_test_build_trusts_only_throwaway_kids(trusted):
    from biocognia.testing import Issuer

    keys = trusted
    production = dict(keys.PUBLIC_KEYS)
    assert set(production) == {"bc1", "bc2"}, "the shipped table, as the website's worker has it"

    class Patch:
        @staticmethod
        def setattr(target, name, value):
            pass  # the key is handed to adopt, not installed by the issuer

    throwaway = Issuer(Patch, licensing.PRODUCT, kid="bct7")
    page_licence.adopt({"keys": {"bct7": throwaway.public_b64, "bc1": throwaway.public_b64,
                                 "evil": throwaway.public_b64, "bct8": "short"}})
    assert keys.PUBLIC_KEYS["bct7"] == throwaway.public_b64
    assert keys.PUBLIC_KEYS["bc1"] == production["bc1"], "a production key is never replaced"
    assert "evil" not in keys.PUBLIC_KEYS and "bct8" not in keys.PUBLIC_KEYS
    from biocognia import environment

    cert = throwaway.issue(bind=environment.binding_of(SECRET), envt="browser")
    files = {**_files(cert), "keys": {"bct7": throwaway.public_b64}}
    assert json.loads(page_licence.adopt_json(json.dumps(files)))["plan"] == "pro"


def test_the_ai_token_is_asked_of_the_page_on_the_website(monkeypatch, license_issuer, trusted):
    from biocognia.gateway import GatewayError

    from plexora.ai.harness import gateway

    asked = []
    answers = [{"token": "BIOCAI1.page.1", "expires_at": 2000, "server_time": 200, "gateway": "https://ai.example",
                "mode": "credits", "mods": ["chat"], "balance": {}},
               {"token": "BIOCAI1.page.2", "expires_at": 2000, "server_time": 200}]

    def ask(kind, args):
        asked.append((kind, dict(args)))
        return answers.pop(0) if answers else {"error": {"code": "ai_not_entitled", "message": "No AI here."}}

    monkeypatch.setattr(page_licence, "_asker", ask)
    assert not isinstance(gateway.TokenSource(), page_licence.PageTokenSource), "the desktop asks the platform"
    monkeypatch.setenv("PLEXORA_BACKEND", "browser")
    source = gateway.TokenSource()
    assert isinstance(source, page_licence.PageTokenSource)
    assert source.get() == "BIOCAI1.page.1"
    assert source.get() == "BIOCAI1.page.1", "kept in memory until two thirds of its life"
    assert source.gateway == "https://ai.example" and source.mods == ["chat"]
    assert source.get(force=True) == "BIOCAI1.page.2"
    assert asked == [("ai_token", {"force": False}), ("ai_token", {"force": True})]
    with pytest.raises(GatewayError) as refused:
        source.get(force=True)
    assert refused.value.code == "ai_not_entitled"
    # A token given outright still wins, as on a desktop.
    assert gateway.TokenSource("BIOCAI1.fixed").get() == "BIOCAI1.fixed"
    monkeypatch.setattr(page_licence, "_asker", lambda kind, args: None)
    with pytest.raises(GatewayError) as unanswered:
        gateway.TokenSource().get()
    assert unanswered.value.code == "no_license"


def test_on_the_website_python_leaves_the_pages_routes_alone(client, monkeypatch):
    from plexora.server.routes.license_routes import PAGE_ENDPOINTS

    monkeypatch.setenv("PLEXORA_BACKEND", "browser")
    for path in ("/settings/license/connect", "/settings/license/connect/poll", "/settings/license/activate",
                 "/settings/license/install", "/settings/license/refresh", "/settings/license/deactivate",
                 "/settings/license/remove", "/settings/license/forget"):
        reply = client.post(path, json={})
        assert reply.status_code == 409 and reply.get_json()["code"] == "page_licence", path
    assert client.get("/settings/license/account").status_code == 409
    assert client.get("/license/status").status_code == 200, "the status is Python's: it reads the files"
    rules = {rule.endpoint for rule in plexora.app.url_map.iter_rules() if rule.endpoint.startswith("license.")}
    assert rules - {"license.license_status"} == set(PAGE_ENDPOINTS)


def test_the_page_answers_them_in_the_route_table():
    from plexora.browser import routes
    from plexora.server.routes.license_routes import PAGE_ENDPOINTS

    assert {endpoint: routes.classify(endpoint) for endpoint in PAGE_ENDPOINTS} \
        == dict.fromkeys(PAGE_ENDPOINTS, "js")
    assert routes.classify("license.license_status") == "python"


def test_forget_is_the_websites(client):
    reply = client.post("/settings/license/forget", json={})
    assert reply.status_code == 400 and reply.get_json()["code"] == "website_only"


def test_settings_on_the_website_offers_forget_and_no_desktop_controls(client, monkeypatch):
    desktop = client.get("/settings").get_data(as_text=True)
    assert 'id="settings_license_deactivate"' in desktop and 'id="settings_license_forget"' not in desktop
    monkeypatch.setenv("PLEXORA_BACKEND", "browser")
    page = client.get("/settings").get_data(as_text=True)
    assert 'id="settings_license_forget"' in page
    for desktop_only in ('id="settings_license_deactivate"', 'id="settings_license_remove"',
                         'data-license-mode="file"', 'id="settings_license_connect_cluster"',
                         'id="settings_license_cluster"'):
        assert desktop_only not in page, desktop_only
    assert "Connect this browser" in page


def test_the_browser_trusts_the_packages_keys():
    from biocognia import keys

    text = VERIFY_JS.read_text(encoding="utf-8")
    block = re.search(r"export const PUBLIC_KEYS = Object\.freeze\(\{(.*?)\}\);", text, re.S).group(1)
    table = dict(re.findall(r"(\w+): '([A-Za-z0-9_-]{43})'", block))
    assert table == {kid: keys.PUBLIC_KEYS[kid] for kid in keys.SLOTS}


node = shutil.which("node")


@pytest.mark.skipif(node is None, reason="node is not installed")
def test_the_page_says_what_the_package_says():
    from biocognia import codes

    script = (f"import('{CLIENT_JS.as_uri()}').then((m) => "
              "process.stdout.write(JSON.stringify(m.SENTENCES)))")
    done = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True,
                          timeout=60)
    assert done.returncode == 0, done.stderr
    page = json.loads(done.stdout)
    assert page == {code: codes.SENTENCES[code] for code in page}
    for code in ("denied", "expired", "device_limit", "cooldown_active", "forged_certificate", "rate_limited"):
        assert code in page


def test_a_test_build_says_so_and_is_never_published(tmp_path):
    import importlib.util

    from plexora.client import static_build

    spec = importlib.util.spec_from_file_location("publish", REPO / "tools" / "browser_build" / "publish.py")
    publish = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(publish)

    assert static_build.licence_config() == {}
    config = static_build.licence_config("http://127.0.0.1:9/v1/", None, ["bct=" + "A" * 43])
    assert config == {"api": "http://127.0.0.1:9/v1", "test_keys": {"bct": "A" * 43}}
    for bad in ("bc1=" + "A" * 43, "bct=short", "nokey"):
        with pytest.raises(ValueError):
            static_build.licence_config(None, None, [bad])
    (tmp_path / "client").mkdir()
    (tmp_path / "client" / "shell.json").write_text(json.dumps({"version": "0", "py": "/py/0/",
                                                                "licence": config}))
    assert "test licence key" in publish.test_build(tmp_path)
    (tmp_path / "client" / "shell.json").write_text(json.dumps({"version": "0", "py": "/py/0/",
                                                                "licence": {"api": config["api"]}}))
    assert publish.test_build(tmp_path) == ""
