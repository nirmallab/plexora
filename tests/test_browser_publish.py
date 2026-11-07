"""tools/browser_build/publish.py: which bytes go to the assets bucket under
which key, and which prefixes it refuses. No network: wrangler is a stub."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("publish", ROOT / "tools" / "browser_build" / "publish.py")
publish = importlib.util.module_from_spec(spec)
sys.modules["publish"] = publish
spec.loader.exec_module(publish)


@pytest.fixture
def bundle(tmp_path):
    (tmp_path / "pyodide").mkdir()
    (tmp_path / "wheels").mkdir()
    for name, body in {"pyodide/pyodide.asm.wasm": b"wasm", "pyodide/pyodide.asm.wasm.br": b"w",
                       "pyodide/python_stdlib.zip": b"zip", "wheels/plexora-1-py3-none-any.whl": b"whl",
                       "wheels/plexora-1-py3-none-any.whl.br": b"br", "stages.json": b"{}",
                       "manifest.json": b"{}"}.items():
        (tmp_path / name).write_bytes(body)
    return tmp_path


def test_a_compressed_file_is_stored_as_its_brotli_bytes_under_its_own_name(bundle):
    found = {key: (source.name, encoding) for key, source, encoding in publish.objects(bundle)}
    assert found == {
        "manifest.json": ("manifest.json", None),
        "pyodide/pyodide.asm.wasm": ("pyodide.asm.wasm.br", "br"),
        "pyodide/python_stdlib.zip": ("python_stdlib.zip", None),
        "stages.json": ("stages.json", None),
        "wheels/plexora-1-py3-none-any.whl": ("plexora-1-py3-none-any.whl.br", "br"),
    }


def _op(call):
    """The wrangler subcommand of a call (`r2 object put`), whatever runs it."""
    at = call.index("r2")
    return call[at:at + 3]


def _key(call):
    return call[call.index("r2") + 3]


class _Wrangler:
    def __init__(self, held=b"", failing=(), broken=False):
        self.calls = []
        self.held = held
        self.failing = set(failing)
        self.broken = broken

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        if _op(command) == ["r2", "object", "get"]:
            if self.broken:
                return type("Done", (), {"returncode": 1, "stdout": "",
                                         "stderr": "Authentication error [code: 10000]"})()
            return type("Done", (), {"returncode": 0 if self.held else 1, "stdout": self.held.decode(),
                                     "stderr": "" if self.held else "X The specified key does not exist."})()
        failed = _op(command) == ["r2", "object", "put"] and _key(command).split("/", 3)[-1] in self.failing
        return type("Done", (), {"returncode": 1 if failed else 0, "stdout": "", "stderr": ""})()


def _args(bundle, prefix, dev=False):
    return type("Args", (), {"bundle": str(bundle), "prefix": prefix, "bucket": "b", "dev": dev,
                             "jobs": 2, "wrangler": "wrangler.js"})()


def test_every_object_is_immutable_and_typed(bundle, monkeypatch):
    stub = _Wrangler()
    monkeypatch.setattr(publish, "run", stub)
    assert publish.assets(_args(bundle, "py/9.9.9/")) == 0
    puts = {_key(call): call for call in stub.calls if _op(call) == ["r2", "object", "put"]}
    wasm = puts["b/py/9.9.9/pyodide/pyodide.asm.wasm"]
    assert wasm[wasm.index("--content-encoding") + 1] == "br"
    assert wasm[wasm.index("--content-type") + 1] == "application/wasm"
    assert wasm[wasm.index("--file") + 1].endswith("pyodide.asm.wasm.br")
    assert all(call[call.index("--cache-control") + 1] == publish.IMMUTABLE for call in puts.values())
    assert "--content-encoding" not in puts["b/py/9.9.9/stages.json"]


def test_a_released_prefix_is_never_rewritten(bundle, monkeypatch):
    stub = _Wrangler(held=json.dumps({"plexora": "9.9.9"}).encode())
    monkeypatch.setattr(publish, "run", stub)
    with pytest.raises(SystemExit, match="never rewritten"):
        publish.assets(_args(bundle, "py/9.9.9/"))
    assert not [call for call in stub.calls if _op(call) == ["r2", "object", "put"]]


def test_anything_else_must_say_it_is_development(bundle, monkeypatch):
    monkeypatch.setattr(publish, "run", _Wrangler())
    with pytest.raises(SystemExit, match="--dev"):
        publish.assets(_args(bundle, "py/dev/abc/"))
    assert publish.assets(_args(bundle, "py/dev/abc/", dev=True)) == 0


def test_only_a_development_prefix_is_pruned(monkeypatch):
    stub = _Wrangler(held=json.dumps({"files": {"manifest.json": {}, "a.whl": {}, "a.whl.br": {},
                                                "stages.json": {}}}).encode())
    monkeypatch.setattr(publish, "run", stub)
    args = type("Args", (), {"prefix": "py/9.9.9/", "bucket": "b", "jobs": 2, "wrangler": "wrangler.js"})()
    with pytest.raises(SystemExit, match="only a development prefix"):
        publish.prune(args)
    args.prefix = "py/dev/abc/"
    assert publish.prune(args) == 0
    deleted = [_key(call) for call in stub.calls if _op(call) == ["r2", "object", "delete"]]
    assert sorted(deleted[:-1]) == ["b/py/dev/abc/a.whl", "b/py/dev/abc/stages.json"]
    assert deleted[-1] == "b/py/dev/abc/manifest.json"  # last, so a stopped prune can run again


def test_the_manifest_goes_last_and_only_once_everything_else_is_in(bundle, monkeypatch):
    stub = _Wrangler()
    monkeypatch.setattr(publish, "run", stub)
    assert publish.assets(_args(bundle, "py/9.9.9/")) == 0
    puts = [_key(call) for call in stub.calls if _op(call) == ["r2", "object", "put"]]
    assert puts[-1] == "b/py/9.9.9/manifest.json" and len(puts) == 5

    stub = _Wrangler(failing={"stages.json"})
    monkeypatch.setattr(publish, "run", stub)
    assert publish.assets(_args(bundle, "py/9.9.9/")) == 1
    puts = [_key(call) for call in stub.calls if _op(call) == ["r2", "object", "put"]]
    assert "b/py/9.9.9/manifest.json" not in puts  # a run again finds no bundle there


def test_a_release_run_again_finds_its_own_bundle_and_does_nothing(bundle, monkeypatch):
    stub = _Wrangler(held=(bundle / "manifest.json").read_bytes())
    monkeypatch.setattr(publish, "run", stub)
    assert publish.assets(_args(bundle, "py/9.9.9/")) == 0
    assert not [call for call in stub.calls if _op(call) == ["r2", "object", "put"]]


def test_a_bucket_that_cannot_be_asked_is_not_taken_for_an_empty_one(bundle, monkeypatch):
    stub = _Wrangler(broken=True)
    monkeypatch.setattr(publish, "run", stub)
    with pytest.raises(SystemExit, match="could not tell"):
        publish.assets(_args(bundle, "py/9.9.9/"))
    assert not [call for call in stub.calls if _op(call) == ["r2", "object", "put"]]


@pytest.fixture
def which(monkeypatch):
    found = {"npx": "/usr/bin/npx", "node": "/usr/bin/node", "wrangler": "/usr/local/bin/wrangler"}
    monkeypatch.setattr(publish.shutil, "which", lambda name: found.get(name))
    monkeypatch.delenv("PLEXORA_WRANGLER", raising=False)


def test_npx_runs_one_exact_version_of_wrangler(which):
    assert publish.wrangler("npx --yes wrangler@3.114.17") == ["/usr/bin/npx", "--yes", "wrangler@3.114.17"]
    for spec in ("npx wrangler", "npx --yes wrangler@latest", "npx wrangler@3", "npx wrangler@^3.114.17",
                 "npx --yes"):
        with pytest.raises(SystemExit, match="exact version"):
            publish.wrangler(spec)


def test_a_script_runs_with_node_and_an_executable_as_it_is(which, monkeypatch):
    assert publish.wrangler("/opt/w/bin/wrangler.js") == ["/usr/bin/node", "/opt/w/bin/wrangler.js"]
    assert publish.wrangler(None) == ["/usr/local/bin/wrangler"]
    monkeypatch.setenv("PLEXORA_WRANGLER", "npx --yes wrangler@3.114.17")
    assert publish.wrangler(None)[0] == "/usr/bin/npx"
    with pytest.raises(SystemExit, match="not found"):
        publish.wrangler("bunx wrangler@3.114.17")


def test_an_upload_through_npx_puts_every_object(bundle, monkeypatch, which):
    stub = _Wrangler()
    monkeypatch.setattr(publish, "run", stub)
    args = _args(bundle, "py/dev/abc/", dev=True)
    args.wrangler = "npx --yes wrangler@3.114.17"
    assert publish.assets(args) == 0
    puts = [call for call in stub.calls if _op(call) == ["r2", "object", "put"]]
    assert len(puts) == 5
    assert all(call[:3] == ["/usr/bin/npx", "--yes", "wrangler@3.114.17"] for call in puts)
