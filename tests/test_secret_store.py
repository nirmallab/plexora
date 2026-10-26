"""A credential file is never readable by anyone else, not even for a moment."""

import json
import os
import stat
import sys

import pytest

from plexora.server.models import secret_store

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")


@posix_only
def test_the_file_is_owner_only_under_a_permissive_umask(tmp_path, monkeypatch):
    seen = []
    real_replace = os.replace

    def watching_replace(src, dst):
        # The temp file, the instant before it becomes the registry.
        seen.append(stat.S_IMODE(os.stat(src).st_mode))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", watching_replace)
    old = os.umask(0o022)
    try:
        secret_store.write_private_json(tmp_path / "nodes.json", {"token": "t"})
    finally:
        os.umask(old)

    assert seen == [0o600]
    assert stat.S_IMODE((tmp_path / "nodes.json").stat().st_mode) == 0o600
    assert json.loads((tmp_path / "nodes.json").read_text()) == {"token": "t"}


@posix_only
def test_a_planted_temp_name_is_not_followed(tmp_path):
    """The old temp name was `<name>.<pid>.tmp`; a symlink planted there is
    neither written through nor in the way."""
    target = tmp_path / "elsewhere"
    target.write_text("untouched")
    (tmp_path / f"nodes.json.{os.getpid()}.tmp").symlink_to(target)

    secret_store.write_private_json(tmp_path / "nodes.json", {"a": 1})

    assert target.read_text() == "untouched"
    assert json.loads((tmp_path / "nodes.json").read_text()) == {"a": 1}
