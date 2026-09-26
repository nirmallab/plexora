"""The two rules the agent layer rests on, checked against the code itself.

1. Nothing in plexora/agent or plexora/mcp imports `data_model`, or calls the
   handle methods that fall through to it (`ImageHandle.stats`,
   `quantization_window`, `SegHandle.centroid_*`).
2. An agent reading a project does not load it into the viewer's slot --
   checked in a fresh interpreter, so nothing another test did can hide it.
"""

import ast
import subprocess
import sys
import textwrap
from pathlib import Path

import plexora

PACKAGE = Path(plexora.__file__).parent
BANNED_CALLS = {"stats", "quantization_window", "centroid_manifest", "centroid_tiles"}


def _agent_sources():
    for folder in ("agent", "mcp", "ai"):
        root = PACKAGE / folder
        if root.exists():
            yield from sorted(root.rglob("*.py"))
    for plugin in ("gating", "roi"):
        path = PACKAGE / "plugins" / plugin / "capabilities.py"
        if path.exists():
            yield path


def test_no_agent_module_imports_data_model():
    offenders = []
    for path in _agent_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                names = {alias.name for alias in node.names}
                if node.module.endswith("data_model") or "data_model" in names:
                    offenders.append(f"{path}:{node.lineno}")
            elif isinstance(node, ast.Import):
                if any(alias.name.endswith("data_model") for alias in node.names):
                    offenders.append(f"{path}:{node.lineno}")
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr in BANNED_CALLS
                  and not (isinstance(node.func.value, ast.Name)
                           and node.func.value.id in ("source_image", "np", "numpy"))):
                offenders.append(f"{path}:{node.lineno} .{node.func.attr}()")
    assert not offenders, offenders


def test_agent_reads_never_load_the_viewers_project(tmp_path):
    repo = PACKAGE.parent
    script = textwrap.dedent(f"""
        import os, sys
        os.environ["PLEXORA_DATA_PATH"] = {str(tmp_path)!r}
        sys.path.insert(0, {str(repo)!r})
        from tests.agent_fixtures import make_synthetic_project
        make_synthetic_project({str(tmp_path)!r})
        from plexora.agent import AgentSession, invoke, registry
        registry.discover(["gating", "roi"])
        session = AgentSession()
        session.data("synth").table.describe()
        for name, args in [
            ("inspect_project", {{"project": "synth"}}),
            ("get_gate", {{"project": "synth", "marker": "CD8"}}),
            ("suggest_auto_gate", {{"project": "synth", "marker": "CD8"}}),
            ("set_gate", {{"project": "synth", "marker": "CD8", "low": 900}}),
            ("list_channels", {{"project": "synth", "with_stats": True}}),
        ]:
            result = invoke(session, name, args)
            assert result["ok"], (name, result)
        from plexora.server.models import data_model
        assert data_model._loaded_source is None, data_model._loaded_source
        print("clean")
    """)
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                          timeout=180)
    assert done.returncode == 0, done.stderr[-2000:]
    assert "clean" in done.stdout
