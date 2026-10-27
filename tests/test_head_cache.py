"""The first rows of a remote table, kept where only this account can reach them."""

import os
import stat
import sys

import pytest

from plexora.server.utils import head_cache

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")


@pytest.fixture
def tmp_tempdir(tmp_path, monkeypatch):
    monkeypatch.setattr(head_cache.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(head_cache, "_fallback", None)
    return tmp_path


@posix_only
def test_the_directory_is_this_accounts_alone(tmp_tempdir):
    local = head_cache.place("sftp-heads", "abc", "cells.csv")
    head_cache.write(local, b"a,b\n")
    assert local.read_bytes() == b"a,b\n"
    assert stat.S_IMODE(os.stat(head_cache.root()).st_mode) == 0o700


@posix_only
def test_a_directory_others_can_write_is_not_used(tmp_tempdir):
    planted = tmp_tempdir / f"plexora-{os.geteuid()}"
    planted.mkdir(mode=0o777)
    os.chmod(planted, 0o777)
    assert head_cache.root() != planted


@posix_only
def test_a_symlink_in_place_of_the_directory_is_not_followed(tmp_tempdir):
    elsewhere = tmp_tempdir / "elsewhere"
    elsewhere.mkdir()
    (tmp_tempdir / f"plexora-{os.geteuid()}").symlink_to(elsewhere)
    assert head_cache.root() != tmp_tempdir / f"plexora-{os.geteuid()}"


@pytest.mark.parametrize("name", ["../../etc/passwd", "..", "", "a\\..\\b.csv"])
def test_a_remote_name_cannot_leave_its_directory(tmp_tempdir, name):
    local = head_cache.place("node-heads", "k", name)
    assert local.parent == head_cache.root() / "node-heads" / "k"
