"""The telemetry allowlist: what may be said, and that nothing else can be."""

import json

import pytest

from plexora.telemetry import redact, sample, schema


def test_every_sample_event_validates():
    body = sample.body()
    assert {e["type"] for e in body["events"]} == set(schema.EVENTS)
    for event in body["events"]:
        assert schema.validate_upload_event(event), event["type"]
    assert schema.validate_client(body["client"])
    redact.assert_clean(body)


def test_anonymous_sample_has_no_diagnostics_fields():
    for event in sample.events(schema.ANONYMOUS):
        assert schema.validate_upload_event(event, schema.ANONYMOUS), event["type"]
    diagnostic = sample.events(schema.DIAGNOSTICS)
    dataset = next(e for e in diagnostic if e["type"] == "dataset.opened")
    assert "channels" in dataset["props"]
    assert not schema.validate_upload_event(dataset, schema.ANONYMOUS)


def test_unknown_prop_rejects_the_event():
    assert not schema.validate_record("project.load", {"outcome": "ready", "name": "x"})


@pytest.mark.parametrize("value", [
    "patient_29384", "/Users/alice", "CD45", "hello world", "", "x" * 200, 5, None,
])
def test_no_free_string_passes_an_enum(value):
    assert not schema.validate_record("project.load", {"outcome": value})


@pytest.mark.parametrize("route", ["/generated/data/x", "a b", "generate_png?x=1",
                                   "ext:zzzzzzzz", "UPPER"])
def test_route_rejects_non_identifiers(route):
    assert not schema.validate_row("server.summary", "route_ms", {"route": route,
                                                                  "status": "2xx"})


def test_owner_labels():
    assert schema.OWNER.check("gating")
    assert schema.OWNER.check("ext:0a1b2c3d")
    assert not schema.OWNER.check("acme")
    assert not schema.OWNER.check("ext:0a1b2c3")


def test_hist_must_be_nine_counts():
    row = {"k": "route_ms", "d": {"route": "health", "status": "2xx"}, "n": 1,
           "h": [1, 0, 0, 0, 0, 0, 0, 0]}
    assert not schema.validate_upload_row("server.summary", row)
    row["h"] = [1, 0, 0, 0, 0, 0, 0, 0, 0]
    assert schema.validate_upload_row("server.summary", row)
    row["h"][0] = -1
    assert not schema.validate_upload_row("server.summary", row)


def test_count_rows_carry_no_histogram():
    row = {"k": "open", "d": {"tool": "roi"}, "n": 2, "h": [0] * 9}
    assert not schema.validate_upload_row("tool.summary", row)


def test_reserved_licence_fields_validate_when_present_and_are_unset_by_default():
    client = sample.client()
    assert not set(schema.RESERVED_LICENSE_FIELDS) & set(client)
    assert schema.validate_client({**client, "license_id_hash": "ab" * 16})
    assert not schema.validate_client({**client, "license_id_hash": "someone@lab.org"})


def test_vectors_are_json_and_complete():
    vectors = schema.vectors()
    text = json.dumps(vectors)
    assert set(vectors["events"]) == set(schema.EVENTS)
    assert vectors["hist_bins"] == 9
    tags = set()

    def walk(node):
        if isinstance(node, dict):
            if "t" in node:
                tags.add(node["t"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(vectors)
    assert tags <= {"enum", "pattern", "int", "bool", "hist", "list", "map", "struct"}
    assert "patient" not in text


def test_vectors_file_matches_schema():
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    result = subprocess.run([sys.executable, "scripts/sync_telemetry_vectors.py", "--check"],
                            cwd=root, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


def test_redact_catches_what_the_schema_should_have():
    names = {"alice", "patient_29384"}
    for bad in ["/Users/alice", "C:\\data", "https://x", "a@b", "?token=1", "t=abc",
                "a" * 40, "alice", "Patient_29384"]:
        with pytest.raises(redact.Dirty):
            redact.assert_clean({"x": bad}, names)
    redact.assert_clean({"install_id": "a" * 32, "family": "data"}, {"data"})
    kept, dropped = redact.clean_events([{"type": "x", "v": "ok"},
                                         {"type": "x", "v": "/etc"}], names)
    assert dropped == 1 and kept == [{"type": "x", "v": "ok"}]
