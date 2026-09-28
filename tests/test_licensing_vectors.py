"""The cross-language contract: every certificate in
licensing/vectors/plexora-license-vectors.json (signed by the Python tool, and
re-signed byte-for-byte by the Worker's vitest suite) verifies here exactly as
marked -- and the vector key is trusted by nothing outside this test."""

import json
from pathlib import Path

import pytest

from plexora.licensing import certificate, delegation, keys, manifest
from plexora.licensing.errors import LicenseError

VECTORS = json.loads((Path(__file__).resolve().parents[1] / "licensing" / "vectors"
                      / "plexora-license-vectors.json").read_text(encoding="utf-8"))
TABLE = {VECTORS["kid"]: VECTORS["public_key"]}


@pytest.mark.parametrize("case", VECTORS["certificates"], ids=lambda c: c["name"])
def test_certificate_vectors(case):
    assert certificate.canonical_json(case["payload"]).decode("utf-8") == case["canonical"]
    if case["valid"]:
        payload = certificate.verify(case["certificate"], keys=TABLE, now=case["payload"]["issued_at"])
        assert payload == case["payload"]
    else:
        with pytest.raises(LicenseError) as info:
            certificate.verify(case["certificate"], keys=TABLE, now=case["payload"]["issued_at"])
        assert info.value.code == case["reason"]


@pytest.mark.parametrize("case", VECTORS["jobs"], ids=lambda c: c["name"])
def test_job_vectors(case, monkeypatch):
    assert certificate.canonical_json(case["payload"]).decode("utf-8") == case["canonical"]
    monkeypatch.setattr(keys, "PUBLIC_KEYS", TABLE)
    payload = delegation.verify(case["certificate"], now=case["payload"]["issued_at"])
    assert payload["entitlements"] == case["effective_entitlements"]
    assert payload["expires_at"] == case["effective_expires_at"]


def test_vector_key_is_not_a_shipped_key():
    assert VECTORS["kid"] not in keys.PUBLIC_KEYS
    assert VECTORS["public_key"] not in keys.PUBLIC_KEYS.values()


def test_paid_default_matches_the_manifest():
    assert VECTORS["paid_entitlements"] == list(manifest.PLAN_ENTITLEMENTS["paid"])


def test_server_error_codes_are_mirrored():
    """client.SERVER_CODES and licensing/src/http.ts ERROR_CODES agree."""
    import re

    from plexora.licensing import client

    ts = (Path(__file__).resolve().parents[1] / "licensing" / "src" / "http.ts").read_text()
    block = ts.split("export const ERROR_CODES = [", 1)[1].split("] as const", 1)[0]
    server = set(re.findall(r"'([a-z_]+)'", block))
    missing = set(client.SERVER_CODES) - server
    assert not missing, f"client knows codes the server never sends: {missing}"
    client_side = {"unauthorized", "forbidden", "seat_limit", "conflict", "not_found"}
    assert server - set(client.SERVER_CODES) <= client_side
