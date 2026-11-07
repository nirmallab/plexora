"""tools/browser_build/budget.py: the website's size bars -- the first visit's
Python, what Cloudflare Pages takes, and the shell a first visit loads."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("web_budget", ROOT / "tools" / "browser_build" / "budget.py")
budget = importlib.util.module_from_spec(spec)
sys.modules["web_budget"] = budget
spec.loader.exec_module(budget)


def _bundle(root: Path, sizes: dict, *, base_wheels=("plexora-1-py3-none-any.whl",),
            base_packages=("numpy",), extra_files=None) -> Path:
    """A bundle whose manifest says each file's size (no bytes are written
    but the JSON the budget reads)."""
    (root / "pyodide").mkdir(parents=True)
    stages = {"base": {"pyodide": list(base_packages), "wheels": list(base_wheels), "bytes": 0},
              "qc": {"pyodide": ["opencv-python"], "wheels": [], "bytes": 0}}
    lock = {"packages": {"numpy": {"file_name": "numpy-2-cp314.whl"},
                         "opencv-python": {"file_name": "opencv-4-cp314.whl"}}}
    (root / "stages.json").write_text(json.dumps(stages), encoding="utf-8")
    (root / "pyodide" / "pyodide-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    files = {name: {"bytes": size, "sha256": "0"} for name, size in {**sizes, **(extra_files or {})}.items()}
    (root / "manifest.json").write_text(json.dumps({"plexora": "1", "first_visit_bytes": 1,
                                                    "files": files}), encoding="utf-8")
    return root


BOOT = {"stages.json": 100, "pyodide/pyodide.mjs": 100, "pyodide/pyodide.asm.mjs": 100,
        "pyodide/pyodide.asm.wasm": 10_000_000, "pyodide/python_stdlib.zip": 1_000_000,
        "pyodide/pyodide-lock.json": 100}


def test_the_first_visit_counts_what_the_worker_fetches_as_stored(tmp_path):
    bundle = _bundle(tmp_path, {**BOOT, "pyodide/pyodide.asm.wasm.br": 3_000_000,
                                "pyodide/numpy-2-cp314.whl": 9_000_000,
                                "pyodide/numpy-2-cp314.whl.br": 2_000_000,
                                "wheels/plexora-1-py3-none-any.whl": 500,
                                # A later stage and a file the page never fetches: not counted.
                                "pyodide/opencv-4-cp314.whl": 40_000_000, "pyodide/pyodide.js": 7})
    found = budget.first_visit(bundle)
    assert found["missing"] == []
    assert found["bytes"] == 3_000_000 + 1_000_000 + 2_000_000 + 500 + 4 * 100


def test_a_first_visit_over_the_bar_fails(tmp_path, capsys):
    bundle = _bundle(tmp_path, {**BOOT, "pyodide/numpy-2-cp314.whl": 45_000_000,
                                "wheels/plexora-1-py3-none-any.whl": 500})
    assert budget.main(["--bundle", str(bundle)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["failed"] == ["first_visit"]
    assert budget.main(["--bundle", str(bundle), "--max-first-visit-mb", "60"]) == 0


def test_a_file_the_manifest_does_not_list_fails_rather_than_counting_nothing(tmp_path, capsys):
    bundle = _bundle(tmp_path, {**BOOT, "pyodide/numpy-2-cp314.whl": 1})
    assert budget.main(["--bundle", str(bundle)]) == 1
    bar = json.loads(capsys.readouterr().out)["bars"][0]
    assert "wheels/plexora-1-py3-none-any.whl" in bar["error"]


def _site(root: Path, headers="/*\n  X-A: b\n", redirects="/* /index.html 200\n") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (root / "_headers").write_text(headers, encoding="utf-8")
    (root / "_redirects").write_text(redirects, encoding="utf-8")
    (root / "client").mkdir(exist_ok=True)
    (root / "client" / "big.js").write_bytes(b"x" * 3000)
    return root


def test_a_static_build_pages_takes_passes(tmp_path):
    bars = {bar["bar"]: bar for bar in budget.check_site(_site(tmp_path), 25, 20_000)}
    assert all(bar["ok"] for bar in bars.values()), bars
    assert bars["pages_largest_file"]["file"] == "client/big.js"
    assert bars["pages_dynamic_redirects"]["measured"] == 1


def test_a_file_over_pages_limit_fails(tmp_path, capsys):
    site = _site(tmp_path)
    assert budget.main(["--site", str(site), "--max-pages-file-mib", "0.001"]) == 1
    bar = json.loads(capsys.readouterr().out)["bars"][0]
    assert bar["over"] == ["client/big.js"]


def test_pages_rule_limits(tmp_path):
    headers = "".join(f"/p{i}\n  X-A: b\n" for i in range(101))
    redirects = "".join(f"/old{i} /new{i} 301\n" for i in range(5)) + "/u/:id /users/:id 301\n"
    bars = {bar["bar"]: bar for bar in budget.check_site(_site(tmp_path, headers, redirects), 25, 20_000)}
    assert not bars["pages_header_rules"]["ok"]
    assert (bars["pages_static_redirects"]["measured"], bars["pages_dynamic_redirects"]["measured"]) == (5, 1)


def test_the_shell_is_held_to_its_gzipped_bytes(tmp_path, capsys):
    record = tmp_path / "shell.json"
    record.write_text(json.dumps({"requests": 3, "bytes": 9_000_000, "gzipBytes": 3_000_000}), encoding="utf-8")
    assert budget.main(["--shell", str(record)]) == 0
    assert budget.main(["--shell", str(record), "--max-shell-mb", "2"]) == 1
    record.write_text(json.dumps({"requests": 0, "bytes": 0, "gzipBytes": 0}), encoding="utf-8")
    capsys.readouterr()
    assert budget.main(["--shell", str(record)]) == 1  # nothing counted is not a pass
    assert "no request" in json.loads(capsys.readouterr().out)["bars"][0]["error"]


def test_nothing_to_measure_is_an_error():
    with pytest.raises(SystemExit):
        budget.main([])
