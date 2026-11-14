"""A file name the system index is asked about is data, never code
(server/utils/relink.py `_index`).

On Windows `_index` asks the Windows Search index through PowerShell. The
name it looks for comes from a .plexora's manifest (a ref's `identity`,
handed to `relink.resolve` by `bundles._find_refs` on import and kept in the
project's `sources` for `relink.ensure` at every open), so whoever made the
file chooses it. A name such as `x$(Set-Content ...).tif` must be looked up
as that name, not run."""

import os
import subprocess
import types

from plexora.server.models import bundles, sources
from plexora.server.utils import relink


def _payload(marker):
    # Forward slashes and no quotes: `_index` doubles a `'`, and the test's
    # folder (under pytest's basetemp) holds no space.
    return f"x$(Set-Content -Path {marker.as_posix()} -Value pwned).tif"


def test_a_name_with_a_subexpression_does_not_run_on_windows(tmp_path):
    """The real index query: a name holding `$( ... )` writes no file."""
    marker = tmp_path / "audit_marker.txt"
    relink._index(_payload(marker))
    assert not marker.exists(), "the file name was run as PowerShell code"


def test_a_bundle_ref_name_does_not_run_on_import(tmp_path, monkeypatch):
    """The import's search (`_find_refs` with search=True) for a ref the
    bundle names but this computer does not have: its name is looked up, not
    run."""
    marker = tmp_path / "audit_marker_import.txt"
    monkeypatch.setattr(bundles, "FIND_SECONDS", 0.0)
    monkeypatch.setattr(bundles, "_search_roots", lambda: [])
    monkeypatch.setattr(relink, "_user_folders", lambda: [])
    refs = {"p1": [{"kind": "file", "token": "{{ref1}}", "hints": {},
                    "identity": {"name": _payload(marker), "size": 12345, "mtime_ms": 1}}]}
    found = bundles._find_refs(refs, None, {}, sources.app_key(), search=True)
    assert found == {"p1": {}}
    assert not marker.exists(), "a name from the .plexora was run as PowerShell code"


def test_the_name_is_not_spliced_into_the_powershell_script(monkeypatch):
    """Any platform: with Windows' branch taken, the name never appears in
    the text PowerShell parses as code (it reaches the query out of band, or
    escaped so `$`, backtick and `"` lose their meaning)."""
    seen = []

    def run(argv, **kwargs):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(relink, "os", types.SimpleNamespace(name="nt", environ=os.environ))
    monkeypatch.setattr(relink.sys, "platform", "win32")
    monkeypatch.setattr(relink.subprocess, "run", run)
    name = 'panel$(Start-Process calc)`"$HOME.tif'
    assert relink._index(name) == []
    assert len(seen) == 1
    script = seen[0][seen[0].index("-Command") + 1]
    assert name not in script
