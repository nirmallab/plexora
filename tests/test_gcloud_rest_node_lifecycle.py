"""The website's Google Cloud node VM: what happens after the request returns.

Three things the scripted tests in test_gcloud_rest.py did not reach:

- **A create that fails after it was accepted.** The website does not wait on
  `instances.insert` (a request held for minutes is a frozen page), and
  Compute Engine reports a full zone or a spent quota on the OPERATION, not
  on the insert call. Nothing read it, so the page's next status read found
  no VM and said "The VM is gone", and the Standard-VM button for a full
  Spot zone was never offered. `node_status` now reads the pending insert.
- **A VM the website made, connected from the desktop.** The desktop waits
  for `gcloud.STARTUP_MARK` before handing its ssh session over and counts an
  ssh session as busy; the node's startup script wrote no mark (a ten-minute
  wait on every connect) and its idle timer counted only the node's port (the
  VM shut down under a desktop session).
- **Delete on the website** returns once Google has accepted it, as Stop does,
  instead of holding the page's one Python thread on the operation.
"""

import json

import pytest

from plexora import gcloud, gcloud_rest


CFG = {
    "project": "lab-imaging",
    "zone": "us-east1-b",
    "vm_name": "plexora-gcp",
    "machine_type": "e2-highmem-16",
    "bucket": "tonsil-data/images",
    "provisioning_model": "spot",
    "vm_source": "plexora",
    "external_ip": True,
}


class FakeHttp:
    """`(method, path substring) -> (status, json)`, first match."""

    def __init__(self, answers=()):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, method, url, token, body=None, timeout=None):
        self.calls.append({"method": method, "url": url})
        for (want, part), (status, payload) in self.answers:
            if want == method and part in url:
                return status, json.dumps(payload).encode("utf-8")
        return 404, json.dumps({"error": {"code": 404, "message": "not scripted"}}).encode()


@pytest.fixture
def http(monkeypatch):
    fake = FakeHttp()
    monkeypatch.setattr(gcloud_rest, "_HTTP", fake)
    monkeypatch.setattr(gcloud_rest, "_sleep", lambda seconds: None)
    monkeypatch.setattr(gcloud, "wants_external_ip", lambda cfg: True)
    gcloud_rest._INSERTING.clear()
    with gcloud_rest.signed_in("tok-123"):
        yield fake
    gcloud_rest._INSERTING.clear()


FIREWALLS = (("GET", "/global/firewalls"), (200, {"items": [
    {"name": gcloud.DENY_RULE},
    {"name": gcloud_rest.NODE_RULE, "allowed": [{"IPProtocol": "tcp", "ports": ["8443"]}]}]}))
NO_VM = (("GET", "/instances/plexora-gcp"), (404, {"error": {"code": 404, "message": "not found"}}))
INSERTED = (("POST", "/zones/us-east1-b/instances"),
            (200, {"name": "op-insert", "status": "PENDING", "zone": "zones/us-east1-b"}))


def _created(http):
    http.answers = [FIREWALLS, NO_VM, INSERTED]
    assert gcloud_rest.ensure_node(CFG)["action"] == "created"


def test_a_spot_zone_that_turns_the_create_down_later_offers_a_standard_vm(http):
    _created(http)
    http.answers = [NO_VM, (("GET", "/operations/op-insert"), (200, {
        "name": "op-insert", "status": "DONE", "error": {"errors": [{
            "code": "ZONE_RESOURCE_POOL_EXHAUSTED",
            "message": "The zone 'us-east1-b' does not have enough resources available"}]}}))]
    with pytest.raises(gcloud.GcloudError) as raised:
        gcloud_rest.node_status(CFG)
    assert raised.value.recovery == gcloud.RECOVERY_STANDARD
    # Said once: the next read is an ordinary missing VM.
    assert gcloud_rest.node_status(CFG)["status"] == "missing"


def test_a_quota_refusal_on_the_operation_is_said_in_its_own_words(http):
    _created(http)
    http.answers = [NO_VM, (("GET", "/operations/op-insert"), (200, {
        "name": "op-insert", "status": "DONE", "error": {"errors": [{
            "code": "QUOTA_EXCEEDED", "message": "Quota 'CPUS' exceeded."}]}}))]
    with pytest.raises(gcloud.GcloudError) as raised:
        gcloud_rest.node_status(CFG)
    assert "quota" in raised.value.message.lower()


def test_a_create_still_running_is_not_a_vm_that_is_gone(http):
    _created(http)
    http.answers = [NO_VM, (("GET", "/operations/op-insert"),
                            (200, {"name": "op-insert", "status": "RUNNING"}))]
    assert gcloud_rest.node_status(CFG)["status"] == "PROVISIONING"


def test_a_missing_vm_with_no_create_pending_is_still_missing(http):
    http.answers = [NO_VM]
    assert gcloud_rest.node_status(CFG)["status"] == "missing"
    assert not any("/operations/" in call["url"] for call in http.calls)


def test_the_node_startup_script_tells_the_desktop_it_has_finished_installing():
    script = gcloud_rest.node_startup_script(8443)
    assert gcloud.STARTUP_MARK in script
    # After gcsfuse is known to be there: the desktop's mark means exactly that.
    assert script.index(gcloud.STARTUP_MARK) > script.index("command -v gcsfuse >/dev/null 2>&1 || {")


def test_the_node_idle_timer_counts_a_desktop_ssh_session_as_busy():
    script = gcloud_rest.node_startup_script(8443)
    idle = script[script.index("cat > /usr/local/sbin/plexora-idle-shutdown"):
                  script.index("IDLESCRIPT\n  chmod")]
    assert "who " in idle and "sshd: .*@" in idle


def test_deleting_off_the_website_still_waits(http, monkeypatch):
    monkeypatch.setattr(gcloud_rest, "in_browser", lambda: False)
    http.answers = [
        (("POST", "/operations/op-del/wait"), (200, {"status": "DONE"})),
        (("DELETE", "/instances/plexora-gcp"), (200, {"name": "op-del", "status": "RUNNING",
                                                      "zone": "zones/us-east1-b"})),
        (("GET", "/instances/plexora-gcp"), (200, {"status": "RUNNING",
                                                   "labels": {"created-by": "plexora"}})),
    ]
    assert gcloud_rest.delete_instance(CFG) is True
    assert any("/operations/op-del/wait" in call["url"] for call in http.calls)


def test_deleting_on_the_website_does_not_wait_on_the_operation(http, monkeypatch):
    monkeypatch.setattr(gcloud_rest, "in_browser", lambda: True)
    http.answers = [
        (("DELETE", "/instances/plexora-gcp"), (200, {"name": "op-del", "status": "RUNNING",
                                                      "zone": "zones/us-east1-b"})),
        (("GET", "/instances/plexora-gcp"), (200, {"status": "RUNNING",
                                                   "labels": {"created-by": "plexora"}})),
    ]
    assert gcloud_rest.delete_instance(CFG) == "deleting"
    assert not any("/operations/" in call["url"] for call in http.calls)
