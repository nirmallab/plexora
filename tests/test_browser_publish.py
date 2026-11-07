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


def _current(stub, monkeypatch, capsys, set_=None):
    monkeypatch.setattr(publish, "run", stub)
    args = type("Args", (), {"bucket": "b", "wrangler": "wrangler.js", "set": set_})()
    code = publish.current(args)
    return code, capsys.readouterr().out.strip()


def test_the_preview_records_its_prefix_and_reads_it_back(monkeypatch, capsys):
    stub = _Wrangler()
    assert _current(stub, monkeypatch, capsys) == (0, "")  # none recorded yet
    assert _current(stub, monkeypatch, capsys, set_="py/dev/abc123")[0] == 0
    put = [call for call in stub.calls if _op(call) == ["r2", "object", "put"]][-1]
    assert _key(put) == "b/py/dev/current.json"
    assert put[put.index("--cache-control") + 1] == "no-store"
    stub = _Wrangler(held=json.dumps({"prefix": "py/dev/abc123/"}).encode())
    assert _current(stub, monkeypatch, capsys) == (0, "py/dev/abc123/")


def test_the_preview_never_names_a_release_prefix(monkeypatch, capsys):
    with pytest.raises(SystemExit, match="development prefix"):
        _current(_Wrangler(), monkeypatch, capsys, set_="py/9.9.9/")
    stub = _Wrangler(held=json.dumps({"prefix": "py/9.9.9/"}).encode())
    with pytest.raises(SystemExit, match="never pruned"):
        _current(stub, monkeypatch, capsys)
    with pytest.raises(SystemExit, match="could not read"):
        _current(_Wrangler(broken=True), monkeypatch, capsys)


def _models(tmp_path, monkeypatch, public):
    """Two stand-in model files pinned as sam_weights pins the real ones, and
    the bucket's public address answered by `public(url, method)`."""
    files = {}
    for name, body in {"enc.onnx": b"encoder", "dec.onnx": b"decoder"}.items():
        (tmp_path / name).write_bytes(body)
        files[name] = {"sha256": publish.hashlib.sha256(body).hexdigest(), "bytes": len(body)}
    monkeypatch.setattr(publish, "_model_pins", lambda: ("mobilesam/tag/", files))
    monkeypatch.setattr(publish, "_public", public)
    stub = _Wrangler()
    monkeypatch.setattr(publish, "run", stub)
    args = type("Args", (), {"dir": str(tmp_path), "bucket": "b", "base": "https://assets.example",
                             "wrangler": "wrangler.js"})()
    return args, stub


def test_the_model_files_go_up_once_and_are_read_back(tmp_path, monkeypatch, capsys):
    stored = {}

    def public(url, method="GET"):
        name = url.rsplit("/", 1)[1]
        body = stored.get(name)
        if body is None:
            return 404, {}, b""
        return 200, {"Content-Length": str(len(body)), "Access-Control-Allow-Origin": "*"}, \
            body if method == "GET" else b""

    args, stub = _models(tmp_path, monkeypatch, public)
    stored["dec.onnx"] = b"decoder"  # already there: left alone
    real = stub.__call__

    def put(command, **kwargs):
        if _op(command) == ["r2", "object", "put"]:
            stored[_key(command).rsplit("/", 1)[1]] = Path(command[command.index("--file") + 1]).read_bytes()
        return real(command, **kwargs)

    monkeypatch.setattr(publish, "run", put)
    assert publish.models(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["files"] == {"enc.onnx": "uploaded", "dec.onnx": "already there"}
    assert [_key(call) for call in stub.calls] == ["b/models/mobilesam/tag/enc.onnx"]
    call = stub.calls[0]
    assert call[call.index("--cache-control") + 1] == publish.IMMUTABLE


def test_a_model_file_that_is_not_the_pinned_one_never_goes_up(tmp_path, monkeypatch):
    args, stub = _models(tmp_path, monkeypatch, lambda url, method="GET": (404, {}, b""))
    (tmp_path / "enc.onnx").write_bytes(b"something else")
    with pytest.raises(SystemExit, match="not the pinned"):
        publish.models(args)
    assert not stub.calls


def test_a_different_file_already_in_the_bucket_is_never_replaced(tmp_path, monkeypatch):
    args, stub = _models(tmp_path, monkeypatch,
                         lambda url, method="GET": (200, {"Content-Length": "3"}, b"old"))
    with pytest.raises(SystemExit, match="look before replacing"):
        publish.models(args)
    assert not stub.calls


def test_the_flags_change_only_what_is_asked():
    current = {"viewer": "on", "models": {"mobilesam": {"enabled": True}}}
    paused = publish.changed_flags(current, viewer="desktop-only", message="Back at noon.")
    assert paused == {"viewer": "desktop-only", "message": "Back at noon.",
                      "models": {"mobilesam": {"enabled": True}}}
    assert current == {"viewer": "on", "models": {"mobilesam": {"enabled": True}}}  # not changed in place
    assert publish.changed_flags(paused, message="", min_site_version="0.0.37") == {
        "viewer": "desktop-only", "min_site_version": "0.0.37", "models": {"mobilesam": {"enabled": True}}}
    assert publish.changed_flags({}, models=["mobilesam=off"]) == {"models": {"mobilesam": {"enabled": False}}}
    for wrong in ({"viewer": "maybe"}, {"min_site_version": "1.2"}, {"models": ["mobilesam=no"]}):
        with pytest.raises(SystemExit):
            publish.changed_flags({}, **wrong)


def _flag_args(**kwargs):
    base = {"viewer": None, "min_site_version": None, "message": None, "model": [], "show": False,
            "bucket": "b", "base": "https://assets.example", "wrangler": "wrangler.js"}
    return type("Args", (), {**base, **kwargs})()


def test_the_kill_switch_is_written_for_five_minutes_and_read_back(monkeypatch, capsys):
    stored = {}

    def public(url, method="GET"):
        assert url == "https://assets.example/flags.json"
        if "body" not in stored:
            return 404, {}, b""
        return 200, {"Cache-Control": publish.FLAGS_CACHE}, stored["body"]

    stub = _Wrangler()
    real = stub.__call__

    def put(command, **kwargs):
        if _op(command) == ["r2", "object", "put"]:
            stored["body"] = Path(command[command.index("--file") + 1]).read_bytes()
        return real(command, **kwargs)

    monkeypatch.setattr(publish, "_public", public)
    monkeypatch.setattr(publish, "run", put)
    assert publish.flags(_flag_args(viewer="desktop-only")) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["flags"] == {"viewer": "desktop-only"} and report["read_back"] is True
    call = stub.calls[0]
    assert _key(call) == "b/flags.json"
    assert call[call.index("--cache-control") + 1] == "public, max-age=300"
    assert publish.flags(_flag_args(viewer="desktop-only")) == 0  # nothing to change: nothing written
    assert len(stub.calls) == 1


def test_flags_that_cannot_be_read_are_never_overwritten(monkeypatch):
    monkeypatch.setattr(publish, "_public", lambda url, method="GET": (500, {}, b""))
    monkeypatch.setattr(publish, "run", _Wrangler())
    with pytest.raises(SystemExit, match="could not read"):
        publish.flags(_flag_args(viewer="on"))
