"""`plexora connect NAME --save NAME` must update a saved server, not rebuild it.

A profile made in Settings carries more than the terminal has flags for: the
preset that composed it (`extra["recipe"]`), a files-only record
(`extra["files"]`), a workstation's account details, and the owner's choice
not to run a data node on this computer (`local_node: False`). `remotes.py`
promises those ride through "a `plexora connect --save` that has never heard
of" them. Re-saving the profile to change one field from the terminal must
leave every one of them where it was, and connecting with it must not turn
the local data node back on.
"""

from __future__ import annotations

from plexora import cli, paths
from plexora.server.models import remotes


def _settings_profile():
    return remotes.Remote.from_dict("o2-files", {
        "target": "me@transfer.o2.hms.harvard.edu",
        "remote_command": "plexora",
        "local_node": False,
        "recipe": "o2-transfer",
        "files": {"via": "sftp", "root": "/n/data1/lab"},
        "workstation": {"os": "linux", "user": "me", "home": "/home/me"},
    })


def test_resaving_a_profile_from_the_terminal_keeps_what_settings_saved(
        monkeypatch, tmp_path):
    """`--save` under the profile's own name changes the typed field and
    keeps the preset, the files record, the workstation details and the
    local-node opt-out."""
    monkeypatch.setattr(paths, "data_root", lambda: tmp_path)
    remotes.save(_settings_profile())

    profile = remotes.find("o2-files")
    args = cli.build_parser("connect").parse_args(
        ["o2-files", "--srun", "-p short", "--save", "o2-files"])
    cli._save_remote("o2-files", profile.target,
                     cli.connect_kwargs(args, profile))

    saved = remotes.find("o2-files").to_dict()
    assert saved["srun"] == "-p short"
    assert saved.get("recipe") == "o2-transfer"
    assert saved.get("files") == {"via": "sftp", "root": "/n/data1/lab"}
    assert saved.get("workstation") == {
        "os": "linux", "user": "me", "home": "/home/me"}
    assert saved.get("local_node") is False


def test_connecting_with_a_profile_that_turned_the_local_node_off_keeps_it_off(
        monkeypatch, tmp_path):
    """The Settings path honours `local_node: False`; the terminal must too."""
    monkeypatch.setattr(paths, "data_root", lambda: tmp_path)
    remotes.save(_settings_profile())

    profile = remotes.find("o2-files")
    kwargs = cli.connect_kwargs(
        cli.build_parser("connect").parse_args(["o2-files"]), profile)

    assert profile.as_session_kwargs()["local_node"] is False
    assert kwargs["local_node"] is False
