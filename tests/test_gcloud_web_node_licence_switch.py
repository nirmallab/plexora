"""The website's Google Cloud connect honours the remote-servers switch
(audit nr-remotes-8).

Every other connect asks `remote_access.refusal()` -- the desktop's
`/settings/remotes/<name>/connect`, and the relay for the website's SSH -- but
the website connects a Google Cloud profile through `/vm/node`, which reaches
neither, so a "licensed" website switch did not hold there.
"""
import pytest

from plexora import gcloud, gcloud_rest

CFG = {
    "project": "lab-imaging", "zone": "us-east1-b", "vm_name": "plexora-gcp",
    "machine_type": "e2-highmem-16", "bucket": "tonsil-data/images",
    "provisioning_model": "spot", "vm_source": "plexora",
}


@pytest.fixture()
def website(monkeypatch):
    from plexora import licensing
    from plexora.server.models import remote_access
    from plexora.server.models import remotes as remote_store
    from plexora.server.routes import gcloud_routes

    switch = {"website": "free", "local": "free"}
    monkeypatch.setattr(remote_access, "_fetch", lambda: dict(switch))
    monkeypatch.setattr(licensing, "peek",
                        lambda: type("S", (), {"licensed": False, "state": "free"})())
    monkeypatch.setenv("PLEXORA_BACKEND", "browser")
    monkeypatch.setattr(gcloud_routes, "_api", lambda: gcloud_rest)
    calls = []
    monkeypatch.setattr(gcloud_rest, "ensure_node",
                        lambda record: calls.append(record) or {"action": "reused"})
    monkeypatch.setattr(gcloud_rest, "node_status", lambda record: {"ready": False})
    record = gcloud.profile(**CFG)
    remote_store.save(remote_store.Remote(
        name="gcp", target=record["vm_name"], local_node=False, extra={"gcloud": record}))
    remote_access.reset_for_tests()
    yield switch, calls
    remote_access.reset_for_tests()


def test_a_licensed_website_switch_refuses_an_unlicensed_vm_node(website):
    import plexora

    switch, calls = website
    switch["website"] = "licensed"
    answer = plexora.app.test_client().post("/settings/remotes/gcp/vm/node")
    assert answer.status_code == 403
    assert answer.get_json()["license"]["code"] == "license_required"
    assert calls == []


def test_a_free_switch_lets_it_through(website):
    import plexora

    _switch, calls = website
    answer = plexora.app.test_client().post("/settings/remotes/gcp/vm/node")
    assert answer.status_code == 202, answer.get_json()
    assert len(calls) == 1
