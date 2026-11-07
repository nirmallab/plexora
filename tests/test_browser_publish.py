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


class _Wrangler:
    def __init__(self, held=b""):
        self.calls = []
        self.held = held

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        if command[2:5] == ["r2", "object", "get"]:
            return type("Done", (), {"returncode": 0 if self.held else 1, "stdout": self.held.decode(),
                                     "stderr": ""})()
        return type("Done", (), {"returncode": 0, "stdout": "", "stderr": ""})()


def _args(bundle, prefix, dev=False):
    return type("Args", (), {"bundle": str(bundle), "prefix": prefix, "bucket": "b", "dev": dev,
                             "jobs": 2, "wrangler": "wrangler.js"})()


def test_every_object_is_immutable_and_typed(bundle, monkeypatch):
    stub = _Wrangler()
    monkeypatch.setattr(publish, "run", stub)
    assert publish.assets(_args(bundle, "py/9.9.9/")) == 0
    puts = {call[5]: call for call in stub.calls if call[2:5] == ["r2", "object", "put"]}
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
    assert not [call for call in stub.calls if call[2:5] == ["r2", "object", "put"]]


def test_anything_else_must_say_it_is_development(bundle, monkeypatch):
    monkeypatch.setattr(publish, "run", _Wrangler())
    with pytest.raises(SystemExit, match="--dev"):
        publish.assets(_args(bundle, "py/dev/abc/"))
    assert publish.assets(_args(bundle, "py/dev/abc/", dev=True)) == 0
