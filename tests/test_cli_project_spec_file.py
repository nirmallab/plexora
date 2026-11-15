"""`plexora project create/set --from SPEC.json` (audit lead ep-cli-7).

`create` used to overwrite the file's `"exist_ok": true` with the untyped
`--exist-ok` flag (store_true, so False), and `set` passed the file's `name`
as a keyword next to the positional project name: a TypeError, not a refusal.
"""
import json
import types

from tests.test_cli import _stub_datasets, cli


def _fake(calls):
    def project_from_spec(spec, node=None):
        calls.append(("create", dict(spec)))
        return "P"

    def configure_project(name, **answers):
        calls.append(("set", name, dict(answers)))
        return {"name": name}

    return types.SimpleNamespace(
        DatasetCreateError=RuntimeError,
        project_from_spec=project_from_spec,
        configure_project=configure_project,
        project_manifest=lambda name: {"name": name},
        pending_conversions=lambda names: [],
    )


def test_create_from_file_keeps_its_exist_ok_when_the_flag_is_not_typed(
        tmp_path, monkeypatch, capsys):
    calls = []
    _stub_datasets(monkeypatch, _fake(calls))
    spec = tmp_path / "p.json"
    spec.write_text(json.dumps({"exist_ok": True, "data": "cells.csv"}), encoding="utf-8")

    args = cli.build_parser("project").parse_args(
        ["create", "slide.ome.tif", "--from", str(spec), "--json"])
    assert cli._run_project(args) == 0
    assert calls[0][1]["exist_ok"] is True


def test_create_flag_still_turns_exist_ok_on(tmp_path, monkeypatch, capsys):
    calls = []
    _stub_datasets(monkeypatch, _fake(calls))
    args = cli.build_parser("project").parse_args(
        ["create", "slide.ome.tif", "--exist-ok", "--json"])
    assert cli._run_project(args) == 0
    assert calls[0][1]["exist_ok"] is True


def test_set_from_a_file_that_names_the_project(tmp_path, monkeypatch, capsys):
    calls = []
    _stub_datasets(monkeypatch, _fake(calls))
    spec = tmp_path / "p.json"
    spec.write_text(json.dumps({"name": "P", "image": "slide.ome.tif",
                                "cell_id": "CellID"}), encoding="utf-8")

    args = cli.build_parser("project").parse_args(
        ["set", "P", "--from", str(spec), "--json"])
    assert cli._run_project(args) == 0
    assert calls == [("set", "P", {"cell_id": "CellID"})]
