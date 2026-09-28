"""Documentation examples run.

* Every command in tools/docs/cli_examples.yaml parses with the real parser.
* Every ```python runnable block -- in a documented docstring or in any
  hand-written page under website/content/docs -- executes against the tiny
  files in tests/doc_fixtures.py, in a throwaway working directory, with the
  viewer server and the browser patched to refuse.

Examples must not wait on background work they did not ask to wait for:
`import_sample(...)` in a runnable block must pass `wait=True`. After each
example the runner still waits for every project's mask conversion and layer
builds, so nothing it started outlives the test's data root.
"""

import re
import shlex
import time
from pathlib import Path

import pytest
import yaml

from tests.doc_fixtures import build_referenced
from tools.docs import cli_api, config, manifest

RUNNABLE = re.compile(r"^([ \t]*)```python runnable[^\n]*\n(.*?)^\1```", re.M | re.S)


def _cli_examples():
    data = yaml.safe_load(config.CLI_EXAMPLES.read_text(encoding="utf-8")) or {}
    for key, entry in (data.get("commands") or {}).items():
        for example in (entry or {}).get("examples") or []:
            for line in example["command"].strip().splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    yield pytest.param(key, line, id=f"{key}: {line[:50]}")


@pytest.mark.parametrize("key,command", list(_cli_examples()))
def test_cli_example_parses(key, command):
    cli = cli_api.load_cli()
    argv = shlex.split(command)
    assert argv[0] == "plexora", f"examples start with `plexora`: {command}"
    sub, rest = cli.split_command(argv[1:])
    expected = None if key == "plexora" else key.split()[0]
    assert sub == expected, f"example under `{key}` runs `plexora {sub or ''}`"
    parser = cli_api.build(cli, sub)
    try:
        parser.parse_args(rest)
    except SystemExit as exc:  # argparse reports and exits
        pytest.fail(f"`{command}` does not parse (exit {exc.code})")


def _runnable_blocks():
    import textwrap

    from tools.docs.python_api import build_model

    blocks = []
    m = manifest.load()
    try:
        symbols = build_model()["symbols"]
    except Exception:  # pragma: no cover - reported by the coverage test
        symbols = {}
    for qualname in sorted(m.pages):
        record = symbols.get(qualname) or {}
        docs = [record.get("doc") or ""] + [x.get("doc") or "" for x in record.get("members") or []]
        for doc in docs:
            for index, match in enumerate(RUNNABLE.finditer(doc)):
                blocks.append(pytest.param(textwrap.dedent(match.group(2)), id=f"{qualname}#{index}"))
    for path in sorted(config.CONTENT.rglob("*.mdx")):
        text = path.read_text(encoding="utf-8")
        if "generated: true" in text.split("---", 2)[1] if text.startswith("---") else False:
            continue
        for index, match in enumerate(RUNNABLE.finditer(text)):
            rel = path.relative_to(config.CONTENT).as_posix()
            blocks.append(pytest.param(textwrap.dedent(match.group(2)), id=f"{rel}#{index}"))
    return blocks


BLOCKS = _runnable_blocks()


def test_import_sample_examples_wait():
    for param in BLOCKS:
        code = param.values[0]
        for call in re.findall(r"import_sample\((.*?)\)\s*$", code, re.M | re.S):
            assert "wait=True" in call, f"{param.id}: import_sample(...) must pass wait=True"


def _settle(timeout=60.0):
    """Wait until no project has a mask conversion or layer build pending.

    The segmentation conversion runs on its own thread and conftest joins it
    by name at teardown; polling here as well keeps a slow conversion from
    turning into a teardown error that points at the wrong place.
    """
    import plexora
    from plexora.server.models import layer_jobs

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pending = False
        for name in plexora.get_config_names():
            try:
                pending = pending or bool(layer_jobs.status(name).get("pending"))
            except Exception:
                continue
        if not pending:
            return
        time.sleep(0.1)


@pytest.mark.parametrize("code", BLOCKS)
def test_runnable_example(code, tmp_path, monkeypatch):
    import webbrowser

    from plexora.jupyter import PlexoraViewer

    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    build_referenced(code, work)

    def refuse(*_a, **_k):
        raise AssertionError("documentation examples must not start a server or open a browser")

    monkeypatch.setattr(PlexoraViewer, "start", refuse)
    monkeypatch.setattr(webbrowser, "open", refuse)
    try:
        exec(compile(code, "<docs example>", "exec"), {"__name__": "__docs_example__"})
    finally:
        _settle()
