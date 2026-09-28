"""`plexora license ...` from the command line."""

import json

from plexora import cli, licensing
from plexora.licensing import cli as license_cli
from plexora.licensing import store


def _run(argv):
    lines = []
    code = license_cli.run(argv, log=lines.append)
    return code, "\n".join(lines)


def test_bare_command_is_status_and_says_free():
    code, out = _run([])
    assert code == 0
    assert "Plan: Free" in out
    assert "Everything Free works with no licence" in out


def test_status_json_on_paid(paid_license):
    code, out = _run(["status", "--json"])
    info = json.loads(out)
    assert code == 0 and info["plan"] == "paid" and info["state"] == "paid_active"
    assert "certificate" not in info


def test_status_shows_the_licences_end_not_the_certificates(license_issuer):
    import time

    now = int(time.time())
    licence_end, cert_end = now + 365 * 86400, now + 90 * 86400
    license_issuer.install(license_issuer.issue(expires_at=cert_end, license_expires_at=licence_end))
    code, out = _run(["status"])
    assert code == 0
    assert f"Valid until: {license_cli._when(licence_end)}" in out
    assert license_cli._when(cert_end) not in out
    assert "Renew by" not in out


def test_status_asks_to_connect_only_when_renewal_is_overdue(license_issuer):
    import time

    now = int(time.time())
    cert_end = now + 5 * 86400
    license_issuer.install(license_issuer.issue(expires_at=cert_end,
                                                license_expires_at=now + 365 * 86400))
    _, out = _run(["status"])
    assert f"Renew by: {license_cli._when(cert_end)}" in out


def test_status_on_a_trial_says_when_it_ends(license_issuer):
    import time

    end = int(time.time()) + 30 * 86400
    license_issuer.install(license_issuer.issue(trial=True, grace_days=0, expires_at=end,
                                                license_expires_at=end))
    _, out = _run(["status"])
    assert f"Trial ends: {license_cli._when(end)}" in out


def test_dispatch_through_main(capsys):
    assert "license" in cli.SUBCOMMANDS
    assert cli.main(["license", "status"]) == 0
    assert "Plan: Free" in capsys.readouterr().out


def test_install_a_licence_file(license_issuer, tmp_path):
    import time

    cert = license_issuer.issue(offline_until=int(time.time()) + 100 * 86400)
    path = tmp_path / "offline.plexora"
    path.write_text(f"# issued to Test Lab\n{cert}\n")
    code, out = _run(["install", str(path)])
    assert code == 0, out
    assert "Paid (offline licence)" in out
    assert store.read_license()["certificate"] == cert
    assert licensing.current().state == "offline_valid"


def test_install_a_raw_certificate(license_issuer):
    code, out = _run(["install", license_issuer.issue()])
    assert code == 0 and "Installed." in out


def test_install_refuses_a_forgery(license_issuer):
    cert = license_issuer.issue()
    code, out = _run(["install", cert[:-8] + "AAAAAAAA"])
    assert code == 1
    assert "altered" in out
    assert not store.license_path().exists()


def test_install_refuses_a_missing_file(tmp_path):
    code, out = _run(["install", str(tmp_path / "nope.plexora")])
    assert code == 1 and "Could not read" in out


def test_remove(paid_license):
    code, out = _run(["remove"])
    assert code == 0 and "Plexora is on Free here" in out
    assert not licensing.current().paid
    assert store.environment_path().exists(), "the environment identity is kept"
    code, out = _run(["remove", "--forget-environment"])
    assert "no local licence" in out
    assert not store.environment_path().exists()


def test_fingerprint_report_holds_nothing_identifying(tmp_path):
    import socket

    out_path = tmp_path / "fp.json"
    code, _ = _run(["fingerprint", "--name", "Bench scope PC", "--out", str(out_path)])
    report = json.loads(out_path.read_text())
    assert code == 0
    assert report["display_name"] == "Bench scope PC"
    assert len(report["binding"]) == 64
    text = json.dumps(report)
    assert socket.gethostname() not in text
    assert "trial_fingerprint" not in report
    assert set(report) == {"schema", "product", "kind", "display_name", "binding",
                           "delegation_pubkey", "platform", "scheduler_hint",
                           "plexora_version", "created_at"}


def test_cluster_fingerprint_carries_a_delegation_key():
    code, out = _run(["fingerprint", "--cluster"])
    report = json.loads(out)
    assert report["kind"] == "cluster" and report["delegation_pubkey"]


def test_online_commands_under_offline_say_so(paid_license):
    code, out = _run(["refresh"])
    assert code == 1 and "PLEXORA_LICENSE_OFFLINE" in out
    code, out = _run(["activate", "PLEX-AAAA-BBBB-CCCC-DDDD"])
    assert code == 1 and "PLEXORA_LICENSE_OFFLINE" in out


def test_trial_without_a_service():
    code, out = _run(["trial"])
    assert code == 1 and "no licence service is configured" in out


def test_environment_show():
    code, out = _run(["environment", "show"])
    assert code == 0 and "Registered here: no" in out
