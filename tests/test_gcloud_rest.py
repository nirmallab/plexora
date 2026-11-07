"""Google Cloud without the CLI: `plexora.gcloud_rest` against canned answers.

Two stand-ins, never Google: a scripted `_HTTP` seam for the exact request
each verb makes and the sentence each refusal becomes (the shapes the CLI
backend's tests pin in tests/test_gcloud.py), and tests/browser/mock_google.py
-- the same stand-in the website's driver uses -- reached through the real
urllib seam, for the whole flow: sign-in token, projects, buckets, a
bucket's contents, zones, instances, a web node VM created, started,
resized, its status and its hand-over link.

The promises pinned: the token is the page's and is bound for one request
only, never stored; deleting a VM never names a bucket and needs Plexora's
label; the node's token lives in the VM's metadata and reaches the VM's
service through a root-only file, never a command line.
"""

import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from plexora import gcloud, gcloud_rest

sys.path.insert(0, str(Path(__file__).parent / "browser"))
import mock_google  # noqa: E402


CFG = {
    "project": "lab-imaging",
    "zone": "us-east1-b",
    "vm_name": "plexora-gcp",
    "machine_type": "e2-highmem-16",
    "bucket": "tonsil-data/images",
    "provisioning_model": "spot",
    "vm_source": "plexora",
}


class FakeHttp:
    """Google, scripted: `(method, path substring) -> (status, json)`, first match."""

    def __init__(self, answers=()):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, method, url, token, body=None, timeout=None):
        self.calls.append({"method": method, "url": url, "token": token, "body": body})
        for (want, part), (status, payload) in self.answers:
            if want == method and part in url:
                return status, json.dumps(payload).encode("utf-8")
        return 404, json.dumps({"error": {"code": 404, "message": "not scripted"}}).encode()


@pytest.fixture
def http(monkeypatch):
    fake = FakeHttp()
    monkeypatch.setattr(gcloud_rest, "_HTTP", fake)
    monkeypatch.setattr(gcloud_rest, "_sleep", lambda seconds: None)
    with gcloud_rest.signed_in("tok-123"):
        yield fake


@pytest.fixture
def google(monkeypatch):
    server, world = mock_google.serve(0, ip="203.0.113.10", ready_after=2)
    monkeypatch.setenv(gcloud_rest.API_ENV, f"http://127.0.0.1:{server.server_address[1]}")
    token = world.issue()
    with gcloud_rest.signed_in(token):
        yield world
    server.shutdown()


def _error(status, message, reason="", state=""):
    return (status, {"error": {"code": status, "message": message, "status": state,
                               "errors": [{"reason": reason, "message": message}]}})


# -- which backend -------------------------------------------------------------


def test_the_cli_answers_unless_the_request_carries_a_token(monkeypatch):
    """A desktop with gcloud is unchanged: no token, no REST."""
    monkeypatch.setattr(gcloud_rest, "in_browser", lambda: False)
    assert gcloud.backend() is gcloud
    with gcloud_rest.signed_in("tok"):
        assert gcloud.backend() is gcloud_rest
    assert gcloud.backend() is gcloud
    assert gcloud_rest.token() == ""


def test_the_website_always_answers_over_rest(monkeypatch):
    monkeypatch.setattr(gcloud_rest, "in_browser", lambda: True)
    assert gcloud.backend() is gcloud_rest


def test_no_token_is_a_sign_in_not_a_request(monkeypatch):
    calls = []
    monkeypatch.setattr(gcloud_rest, "_HTTP", lambda *a, **k: calls.append(a))
    with pytest.raises(gcloud.GcloudError) as raised:
        gcloud_rest.projects()
    assert raised.value.reauth is True and calls == []
    assert gcloud_rest.health() == {"installed": True, "account": None, "problem": None,
                                    "expired": False, "backend": "rest"}


def test_the_scopes_are_the_narrow_ones():
    """compute, read-only storage and the project list -- never cloud-platform."""
    assert not any(scope.endswith("/cloud-platform") for scope in gcloud_rest.SCOPES)
    assert "https://www.googleapis.com/auth/compute" in gcloud_rest.SCOPES
    assert "https://www.googleapis.com/auth/devstorage.read_only" in gcloud_rest.SCOPES
    assert ("https://www.googleapis.com/auth/cloudplatformprojects.readonly"
            in gcloud_rest.SCOPES)


# -- who is signed in ----------------------------------------------------------------


def test_health_names_the_account_and_sends_the_token_only_to_google(http):
    http.answers = [(("GET", "/v1/userinfo"), (200, {"email": "aj@example.com"}))]
    assert gcloud_rest.health()["account"] == "aj@example.com"
    assert http.calls[0]["token"] == "tok-123"
    assert http.calls[0]["url"] == "https://openidconnect.googleapis.com/v1/userinfo"


def test_a_lapsed_token_is_expired_and_an_outage_is_a_problem(http):
    http.answers = [(("GET", "/v1/userinfo"), _error(401, "Request had invalid "
                                                     "authentication credentials.", "authError",
                                                     "UNAUTHENTICATED"))]
    body = gcloud_rest.health()
    assert body["expired"] is True and body["problem"] is None
    http.answers = [(("GET", "/v1/userinfo"), _error(503, "Backend unavailable"))]
    body = gcloud_rest.health()
    assert body["expired"] is False and "503" in body["problem"]


# -- what somebody has ---------------------------------------------------------------


def test_projects_follow_every_page(http):
    http.answers = [
        (("GET", "pageToken=p2"), (200, {"projects": [{"projectId": "b-two", "name": "B"}]})),
        (("GET", "/v1/projects"), (200, {"projects": [{"projectId": "a-one"}],
                                         "nextPageToken": "p2"})),
    ]
    assert gcloud_rest.projects() == [{"id": "a-one", "name": "a-one"},
                                      {"id": "b-two", "name": "B"}]
    first = urlsplit(http.calls[0]["url"])
    assert first.netloc == "cloudresourcemanager.googleapis.com"
    assert parse_qs(first.query)["filter"] == ["lifecycleState:ACTIVE"]


def test_buckets_carry_the_region_they_imply(http):
    http.answers = [(("GET", "/storage/v1/b?"), (200, {"items": [
        {"name": "b-two", "location": "US-EAST1", "locationType": "region"},
        {"name": "a-one", "location": "US", "locationType": "multi-region"}]}))]
    found = gcloud_rest.buckets("my-project")
    assert [one["name"] for one in found] == ["a-one", "b-two"]
    assert found[1]["region"] == "us-east1" and found[1]["exact"] is True
    assert found[0]["region"] == "us-central1" and found[0]["exact"] is False


def test_a_bucket_that_is_not_there_says_so(http):
    http.answers = [(("GET", "/storage/v1/b/nope"), _error(404, "Not Found"))]
    with pytest.raises(gcloud.GcloudError) as raised:
        gcloud_rest.bucket("p", "nope-bucket")
    assert "There is no bucket called gs://nope-bucket" in raised.value.message


def test_a_public_bucket_is_readable_though_its_metadata_is_not(http):
    http.answers = [
        (("GET", "/o?maxResults=1"), (200, {"items": [{"name": "x"}]})),
        (("GET", "/storage/v1/b/idc-open-data"), _error(403, "caller does not have "
                                                        "storage.buckets.get access",
                                                        "forbidden")),
    ]
    found = gcloud_rest.bucket("p", "gs://idc-open-data/fc07/")
    assert found["name"] == "idc-open-data" and found["public"] is True


def test_a_bucket_nobody_may_read_names_the_role_to_ask_for(http):
    http.answers = [(("GET", "/storage/v1/b/private-bucket"), _error(403, "denied",
                                                                     "forbidden"))]
    with pytest.raises(gcloud.GcloudError) as raised:
        gcloud_rest.bucket("p", "private-bucket")
    assert "Storage Object Viewer" in raised.value.message


def test_a_bucket_name_is_checked_before_anything_is_asked(http):
    with pytest.raises(gcloud.GcloudError):
        gcloud_rest.bucket("p", "Not A Bucket")
    assert http.calls == []


# -- zones and VMs ---------------------------------------------------------------------


def test_a_project_without_compute_is_said_so(http):
    http.answers = [(("GET", "/zones"), _error(403, "Compute Engine API has not been used "
                                                    "in project 1 before or it is disabled.",
                                               "accessNotConfigured", "PERMISSION_DENIED"))]
    with pytest.raises(gcloud.GcloudError) as raised:
        gcloud_rest.pick_zone("p", "us-east1")
    assert "Compute Engine API is not enabled" in raised.value.message


def test_a_vm_that_does_not_exist_is_none_not_an_error(http):
    http.answers = [(("GET", "/instances/plexora-gcp"), _error(404, "was not found"))]
    assert gcloud_rest.instance("p", "us-east1-b", "plexora-gcp") is None


def test_a_vm_plexora_did_not_make_is_never_deleted(http):
    http.answers = [(("GET", "/instances/plexora-gcp"), (200, {
        "status": "RUNNING", "labels": {"team": "x"}}))]
    with pytest.raises(gcloud.GcloudError) as raised:
        gcloud_rest.delete_instance(CFG)
    assert "not created by Plexora" in raised.value.message
    assert [call["method"] for call in http.calls] == ["GET"]


def test_deleting_a_vm_names_a_machine_and_never_a_bucket(http):
    http.answers = [
        (("POST", "/operations/"), (200, {"status": "DONE"})),
        (("DELETE", "/instances/plexora-gcp"), (200, {"name": "op", "status": "RUNNING",
                                                      "zone": "zones/us-east1-b"})),
        (("GET", "/instances/plexora-gcp"), (200, {"status": "TERMINATED",
                                                   "labels": {"created-by": "plexora"}})),
    ]
    gcloud_rest.delete_instance(CFG)
    for call in http.calls:
        assert "tonsil-data" not in call["url"] and "/storage/" not in call["url"]
    assert any(call["method"] == "DELETE" for call in http.calls)


def test_a_full_zone_on_spot_offers_the_standard_button(http):
    http.answers = [
        (("GET", "/global/firewalls"), (200, {"items": [
            {"name": gcloud.DENY_RULE},
            {"name": gcloud_rest.NODE_RULE, "allowed": [{"IPProtocol": "tcp",
                                                         "ports": ["8443"]}]}]})),
        (("GET", "/instances/plexora-gcp"), _error(404, "was not found")),
        (("POST", "/zones/us-east1-b/instances"), _error(
            503, "The zone 'us-east1-b' does not have enough resources available",
            "ZONE_RESOURCE_POOL_EXHAUSTED")),
    ]
    with pytest.raises(gcloud.GcloudError) as raised:
        gcloud_rest.ensure_node(CFG)
    assert raised.value.recovery == gcloud.RECOVERY_STANDARD


def test_a_vm_somebody_already_runs_is_not_turned_into_a_node(http):
    with pytest.raises(gcloud.GcloudError) as raised:
        gcloud_rest.ensure_node(dict(CFG, vm_source="existing"))
    assert "desktop app" in raised.value.message and http.calls == []


def test_the_startup_script_keeps_the_token_off_every_command_line():
    script = gcloud_rest.node_startup_script(8443)
    assert "node serve --host 0.0.0.0 --port $PORT --app --tls self-signed" in script
    assert "--token" not in script
    assert "PLEXORA_NODE_TOKEN=%s" in script and "chmod 600 /etc/plexora-node.env" in script
    assert "User=plexora" in script
    assert "__NODE_PORT__" not in script
    # Ready means /app/ answered 200 with the certificate in place; a node
    # whose install has no web app build says so instead of handing over a 404.
    assert 'say state ready' in script and 'say state failed-noapp' in script


# -- the whole flow, through urllib, against the stand-in --------------------------------


def test_the_form_s_questions_against_the_stand_in(google):
    assert gcloud_rest.health()["account"] == "researcher@example.org"
    assert [one["id"] for one in gcloud_rest.projects()] == ["lab-imaging", "teaching-2026"]
    found = gcloud_rest.buckets("lab-imaging")
    assert [one["name"] for one in found] == ["atlas-public", "tonsil-data"]
    assert gcloud_rest.bucket("lab-imaging", "tonsil-data")["region"] == "us-east1"
    top = gcloud_rest.objects("lab-imaging", "tonsil-data")
    assert top["folders"] == ["images/"] and [f["name"] for f in top["files"]] == ["README.txt"]
    inner = gcloud_rest.objects("lab-imaging", "tonsil-data/images")
    assert inner["prefix"] == "images/" and inner["folders"] == ["images/masks/"]
    assert {f["name"] for f in inner["files"]} == {"images/tonsil.ome.tif", "images/cells.csv"}
    assert gcloud_rest.zones("lab-imaging", "us-east1") == ["us-east1-b", "us-east1-c"]
    assert gcloud_rest.pick_zone("lab-imaging", "us-east1") == "us-east1-b"
    assert gcloud_rest.instances("lab-imaging") == []


def test_a_node_vm_is_made_reached_and_handed_over(google):
    done = gcloud_rest.ensure_node(CFG)
    assert done["action"] == "created"
    # The rules first, the machine second: the deny rule must exist before
    # anything Plexora made has an address.
    assert gcloud.DENY_RULE in google.firewalls
    rule = google.firewalls[gcloud_rest.NODE_RULE]
    assert rule["allowed"] == [{"IPProtocol": "tcp", "ports": ["8443"]}]
    assert rule["targetTags"] == [gcloud_rest.NODE_TAG]
    made = google.instances["plexora-gcp"]
    meta = {item["key"]: item["value"] for item in made["metadata"]["items"]}
    assert len(meta[gcloud_rest.META_TOKEN]) == 32
    assert meta[gcloud_rest.META_BUCKET] == "tonsil-data/images"
    assert meta["enable-guest-attributes"] == "TRUE"
    assert "--tls self-signed" in meta["startup-script"]
    assert made["labels"] == {"created-by": "plexora"}
    assert made["tags"]["items"] == ["plexora", "plexora-node"]
    assert made["scheduling"]["provisioningModel"] == "SPOT"
    assert made["scheduling"]["instanceTerminationAction"] == "STOP"
    assert made["machineType"].endswith("/machineTypes/e2-highmem-16")
    assert gcloud_rest.instances("lab-imaging") == [{
        "name": "plexora-gcp", "zone": "us-east1-b", "status": "RUNNING",
        "machine_type": "e2-highmem-16"}]

    # Booting, then installing, then ready -- read from the VM's guest
    # attributes through the Compute API, never from the node itself.
    states = [gcloud_rest.node_status(CFG)["state"] for _ in range(3)]
    assert states == ["booting", "installing", "ready"]
    status = gcloud_rest.node_status(CFG)
    assert status["ready"] is True and status["fingerprint"] == mock_google.FINGERPRINT
    assert status["app_url"] == "https://203.0.113.10:8443/app/"
    link = urlsplit(status["pair_url"])
    assert (link.scheme, link.netloc, link.path) == ("https", "203.0.113.10:8443",
                                                     "/app/connect")
    fragment = parse_qs(link.fragment)
    assert fragment["t"] == [meta[gcloud_rest.META_TOKEN]]
    assert unquote(fragment["e"][0]) == "https://203.0.113.10:8443"

    # Stop, resize while stopped, start again: the same machine.
    gcloud_rest.stop_instance(CFG, block=True)
    with pytest.raises(gcloud.GcloudError):
        gcloud_rest.ensure_node(dict(CFG, external_ip=False))
    gcloud_rest.set_machine_type(CFG, "e2-highmem-8")
    assert made["machineType"].endswith("e2-highmem-8")
    assert gcloud_rest.ensure_node(CFG)["action"] == "started"
    assert google.instances["plexora-gcp"]["status"] == "RUNNING"
    with pytest.raises(gcloud.GcloudError) as raised:
        gcloud_rest.set_machine_type(CFG, "e2-highmem-4")
    assert "Stop" in raised.value.message
    gcloud_rest.delete_instance(CFG)
    assert "plexora-gcp" not in google.instances


def test_a_vm_the_desktop_made_becomes_a_node_while_stopped(google):
    google.instances["plexora-gcp"] = {
        "name": "plexora-gcp", "status": "RUNNING", "labels": {"created-by": "plexora"},
        "zone": "https://x/zones/us-east1-b", "machineType": "x/e2-highmem-16",
        "metadata": {"fingerprint": "m0", "items": [{"key": "enable-oslogin",
                                                     "value": "TRUE"}]},
        "tags": {"fingerprint": "t0", "items": ["plexora"]},
        "networkInterfaces": [{"accessConfigs": [{"natIP": "203.0.113.10"}]}]}
    with pytest.raises(gcloud.GcloudError) as raised:
        gcloud_rest.ensure_node(CFG)
    assert "Stop it here" in raised.value.message
    google.instances["plexora-gcp"]["status"] = "TERMINATED"
    assert gcloud_rest.ensure_node(CFG)["action"] == "converted"
    made = google.instances["plexora-gcp"]
    meta = {item["key"]: item["value"] for item in made["metadata"]["items"]}
    assert meta["enable-oslogin"] == "TRUE" and len(meta[gcloud_rest.META_TOKEN]) == 32
    assert made["tags"]["items"] == ["plexora", "plexora-node"]
    assert made["status"] == "RUNNING"


def test_a_lapsed_sign_in_is_a_401_the_form_can_act_on(google):
    google.tokens.clear()
    with pytest.raises(gcloud.GcloudError) as raised:
        gcloud_rest.projects()
    assert raised.value.reauth is True


# -- the routes, with the token in the request ----------------------------------------


@pytest.fixture
def client():
    import plexora

    return plexora.app.test_client()


def _saved(name="gcp", **overrides):
    from plexora.server.models import remotes as remote_store

    record = gcloud.profile(**dict(CFG, **overrides))
    return remote_store.save(remote_store.Remote(
        name=name, target=record["vm_name"], local_node=False,
        remote_command="~/plexora-venv", data_dir="~/plexora-data",
        extra={"gcloud": record}))


def test_routes_answer_over_rest_with_the_page_s_token_and_forget_it(client, monkeypatch):
    server, world = mock_google.serve(0)
    monkeypatch.setenv(gcloud_rest.API_ENV, f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setattr(gcloud_rest, "in_browser", lambda: False)
    token = world.issue()
    signed = {gcloud_rest.TOKEN_HEADER: token}
    try:
        body = client.get("/settings/gcloud/status", headers=signed).get_json()
        assert body["backend"] == "rest" and body["account"] == "researcher@example.org"
        assert gcloud_rest.token() == ""
        projects = client.get("/settings/gcloud/projects", headers=signed).get_json()
        assert [one["id"] for one in projects["projects"]] == ["lab-imaging", "teaching-2026"]
        listing = client.get("/settings/gcloud/objects?project=lab-imaging&bucket=tonsil-data"
                             "&prefix=images/", headers=signed).get_json()
        assert listing["folders"] == ["images/masks/"]
        zones = client.get("/settings/gcloud/zones?project=lab-imaging&region=us-east1",
                           headers=signed).get_json()
        assert zones["pick"] == "us-east1-b"

        _saved()
        started = client.post("/settings/remotes/gcp/vm/node", headers=signed)
        assert started.status_code == 202 and started.get_json()["action"] == "created"
        for _ in range(4):
            status = client.get("/settings/remotes/gcp/vm", headers=signed).get_json()
        assert status["status"] == "RUNNING" and status["made_by_plexora"] is True
        assert status["node"]["ready"] is True
        assert status["node"]["pair_url"].startswith("https://203.0.113.10:8443/app/connect#")

        client.post("/settings/remotes/gcp/vm/stop", headers=signed)
        resized = client.post("/settings/remotes/gcp/vm/resize", headers=signed,
                              json={"machine_type": "e2-standard-8"})
        assert resized.status_code == 200, resized.get_json()
        from plexora.server.models import remotes as remote_store

        saved = remote_store.find("gcp").gcloud
        assert saved["machine_type"] == "e2-standard-8"
        # No credential anywhere in what Plexora keeps.
        stored = json.dumps(remote_store.find("gcp").extra)
        node_token = {item["key"]: item["value"] for item in
                      world.instances["plexora-gcp"]["metadata"]["items"]}[
                          gcloud_rest.META_TOKEN]
        assert token not in stored and node_token not in stored

        world.tokens.clear()
        refused = client.get("/settings/gcloud/projects", headers=signed)
        assert refused.status_code == 400 and refused.get_json()["reauth"] is True
    finally:
        server.shutdown()


def test_without_a_token_the_desktop_keeps_its_cli(client, monkeypatch):
    monkeypatch.setattr(gcloud_rest, "in_browser", lambda: False)
    monkeypatch.setattr(gcloud, "_which", lambda name: None)
    monkeypatch.setattr(gcloud, "_adopt_install", lambda: False)
    body = client.get("/settings/gcloud/status").get_json()
    assert body == {"installed": False, "account": None, "problem": None,
                    "expired": False, "backend": "cli"}
    _saved()
    refused = client.post("/settings/remotes/gcp/vm/node")
    assert refused.status_code == 400 and "SSH" in refused.get_json()["error"]
