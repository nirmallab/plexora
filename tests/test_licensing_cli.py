"""`plexora license ...` from the command line: the shared BioCognia verbs, in
Plexora's words."""

import json
import time

from biocognia import store

from plexora import cli, licensing
from plexora.licensing import LICENSING
from plexora.licensing import cli as license_cli


def _run(argv):
    lines = []
    code = license_cli.run(argv, log=lines.append)
    return code, "\n".join(lines)


def test_bare_command_is_status_and_says_free():
    code, out = _run([])
    assert code == 0
    assert "Plan: Free" in out
    assert "Everything Free works with no licence" in out
    assert "plexora license activate" in out


def test_status_json_on_paid(paid_license):
    code, out = _run(["status", "--json"])
    info = json.loads(out)
    assert code == 0 and info["plan"] == "paid" and info["state"] == "paid_active"
    assert info["plan_id"] == "plexora.pro"
    assert "certificate" not in info


def test_status_shows_the_licences_end_not_the_certificates(license_issuer):
    now = int(time.time())
    licence_end, cert_end = now + 365 * 86400, now + 90 * 86400
    license_issuer.install(license_issuer.issue(exp=cert_end, lic_exp=licence_end))
    code, out = _run(["status"])
    assert code == 0
    assert f"Valid until: {license_cli._when(licence_end)}" in out
    assert license_cli._when(cert_end) not in out
    assert "Renew by" not in out
    assert "Device: Test computer (desktop)" in out


def test_status_asks_to_connect_only_when_renewal_is_overdue(license_issuer):
    now = int(time.time())
    cert_end = now + 5 * 86400
    license_issuer.install(license_issuer.issue(exp=cert_end, lic_exp=now + 365 * 86400))
    _, out = _run(["status"])
    assert f"Renew by: {license_cli._when(cert_end)}" in out


def test_status_on_a_trial_says_when_it_ends(license_issuer):
    end = int(time.time()) + 30 * 86400
    license_issuer.install(license_issuer.issue(trial=True, grace_days=0, exp=end, lic_exp=end))
    _, out = _run(["status"])
    assert "Plan: Paid (trial)" in out
    assert f"Trial ends: {license_cli._when(end)}" in out


def test_dispatch_through_main(capsys):
    assert "license" in cli.SUBCOMMANDS
    assert cli.main(["license", "status"]) == 0
    assert "Plan: Free" in capsys.readouterr().out


def test_install_a_licence_file(license_issuer, tmp_path):
    cert = license_issuer.issue(offline_until=int(time.time()) + 100 * 86400)
    path = tmp_path / "offline.bioc"
    path.write_text(f"# issued to Test Lab\n{cert}\n")
    code, out = _run(["install", str(path)])
    assert code == 0, out
    assert "Paid (offline licence)" in out
    assert LICENSING.store.read()["certificate"] == cert
    assert licensing.current().state == "offline_valid"


def test_install_a_raw_certificate(license_issuer):
    code, out = _run(["install", license_issuer.issue()])
    assert code == 0 and "Installed." in out


def test_install_refuses_a_forgery(license_issuer):
    cert = license_issuer.issue()
    code, out = _run(["install", cert[:-8] + "AAAAAAAA"])
    assert code == 1
    assert "altered" in out
    assert not LICENSING.store.path.exists()


def test_install_refuses_another_products_certificate(license_issuer):
    cert = license_issuer.issue(aud="scimappro")
    code, out = _run(["install", cert])
    assert code == 1 and "not a Plexora certificate" in out


def test_install_refuses_a_missing_file(tmp_path):
    code, out = _run(["install", str(tmp_path / "nope.bioc")])
    assert code == 1 and "Could not read" in out


def test_remove(paid_license):
    code, out = _run(["remove"])
    assert code == 0 and "removed from this device" in out
    assert not licensing.current().licensed
    assert store.environment_path().exists(), "the device identity is kept"
    code, out = _run(["remove", "--forget-environment"])
    assert "forgotten for every BioCognia product" in out
    assert not store.environment_path().exists()


def test_fingerprint_report_holds_nothing_identifying(tmp_path):
    import socket

    out_path = tmp_path / "fp.json"
    code, _ = _run(["fingerprint", "--name", "Bench scope PC", "--out", str(out_path)])
    report = json.loads(out_path.read_text())
    assert code == 0
    assert report["product"] == "plexora" and report["display_name"] == "Bench scope PC"
    assert len(report["binding"]) == 64
    text = json.dumps(report)
    assert socket.gethostname() not in text
    assert "trial_fingerprint" not in report


def test_cluster_fingerprint_carries_a_delegation_key():
    code, out = _run(["fingerprint", "--cluster"])
    report = json.loads(out)
    assert report["kind"] == "cluster" and report["delegation_pubkey"]


def test_online_commands_under_offline_say_so(paid_license):
    code, out = _run(["refresh"])
    assert code == 1 and "BIOCOGNIA_OFFLINE" in out
    code, out = _run(["activate", "BIOC-AAAA-BBBB"])
    assert code == 1 and "BIOCOGNIA_OFFLINE" in out


def test_trial_points_at_the_portal():
    code, out = _run(["trial"])
    assert code == 0
    assert "30-day trial" in out and "start?product=plexora" in out


def test_status_shows_what_the_licence_unlocks(license_issuer):
    license_issuer.install()  # the Paid default, ["ai"]
    code, out = _run(["status"])
    assert code == 0
    assert "Plexora AI: included" in out
    assert "External MCP access: not included" in out


def test_a_desktop_lease_asks_the_platform(paid_license, license_service):
    code, out = _run(["lease", "--ttl", "6h"])
    assert code == 0, out
    assert out.strip().startswith("BIOC1.")
    assert license_service.of("/v1/delegate")[0]["body"]["product"] == "plexora"


# -- connecting a device -------------------------------------------------------------------


def test_activate_with_no_code_runs_the_device_flow(license_service, monkeypatch):
    monkeypatch.setattr(LICENSING.client.__class__, "activate_poll",
                        _fast_poll(LICENSING.client.__class__.activate_poll))
    code, out = _run(["activate", "--name", "Lab Mac"])
    assert code == 0, out
    assert "Open https://account.biocognia.test/activate and enter BIOC-TEST-0001" in out
    assert "Activated." in out and "Plan: Paid" in out
    start = license_service.of("/v1/activate/start")[0]["body"]
    assert start["product"] == "plexora" and start["environment"]["display_name"] == "Lab Mac"
    assert licensing.current().licensed


def test_activate_with_a_portal_code_needs_no_browser(license_service):
    code, out = _run(["activate", "BIOC-AAAA-BBBB"])
    assert code == 0, out
    assert "Open " not in out and "Activated." in out
    assert license_service.of("/v1/activate")[0]["body"]["credential"] == "BIOC-AAAA-BBBB"


def test_refresh_asks_the_platform_now(paid_license, license_service):
    code, out = _run(["refresh"])
    assert code == 0, out
    assert "Refresh: ok" in out
    assert license_service.of("/v1/refresh")[0]["body"]["product"] == "plexora"


def test_deactivate_releases_the_device(license_service):
    _run(["activate", "BIOC-AAAA-BBBB"])
    code, out = _run(["deactivate"])
    assert code == 0, out
    assert "released from its seat" in out
    assert not LICENSING.store.path.exists()
    assert not licensing.current().licensed


def _fast_poll(real):
    """The fake platform answers pending once; do not wait out the interval."""
    def poll(self, code):
        answer = real(self, code)
        if answer.get("status") == "pending":
            answer["retry_after"] = 0.01
        return answer
    return poll
