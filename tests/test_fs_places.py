"""The explorer's sidebar on every operating system, tested on whichever runs this.

Each OS answers "where might the data be?" differently -- drive letters,
`/Volumes`, a mount table full of things that are not places -- and the sidebar
has to be right on all three, plus every cluster. The module's seams
(`_platform`, `_read_mountinfo`, `_volumes_root`, `_mount_output`, `_kernel32`,
`_usage_many`) are what let a Mac run the Windows and Linux branches here.

The one promise that is about time rather than shape: nothing here waits long
on a mount. A network volume whose server has gone answers no free space, not
a hung sidebar.
"""

from __future__ import annotations

import os
import threading
import time
from collections import namedtuple
from pathlib import Path

import pytest

from plexora.server.utils import fs_places

Usage = namedtuple("Usage", "total used free")


@pytest.fixture
def no_usage(monkeypatch):
    monkeypatch.setattr(fs_places, "_usage_many", lambda paths: {})


@pytest.fixture
def fresh_usage(monkeypatch):
    monkeypatch.setattr(fs_places, "_usage_memo", {})
    monkeypatch.setattr(fs_places, "_usage_running", set())


# -- Windows -----------------------------------------------------------------


class _Kernel32:
    """The four kernel32 calls the Windows branch makes, answered from a table."""

    TYPES = {"C:\\": fs_places.DRIVE_FIXED, "D:\\": fs_places.DRIVE_FIXED,
             "E:\\": fs_places.DRIVE_REMOVABLE, "Q:\\": fs_places.DRIVE_NO_ROOT_DIR,
             "Z:\\": fs_places.DRIVE_REMOTE}
    LABELS = {"C:\\": ("", "NTFS"), "D:\\": ("Data", "NTFS")}

    def __init__(self):
        self.asked = []
        self.modes = []

    def GetDriveTypeW(self, root):  # noqa: N802 - the Win32 name
        return self.TYPES[root]

    def GetVolumeInformationW(self, root, name, size, serial, length, flags,  # noqa: N802
                              fstype, fstype_size):
        self.asked.append(root)
        if root not in self.LABELS:
            return 0  # an empty card reader
        assert size == fstype_size == 261
        name.value, fstype.value = self.LABELS[root]
        return 1

    def SetThreadErrorMode(self, mode, previous):  # noqa: N802
        self.modes.append(mode)
        return 1


def test_windows_drives_are_labelled_the_way_explorer_labels_them(
        monkeypatch, no_usage):
    kernel32 = _Kernel32()
    monkeypatch.setattr(fs_places, "_kernel32", lambda: kernel32)
    monkeypatch.setattr(os, "listdrives",
                        lambda: ["C:\\", "D:\\", "E:\\", "Q:\\", "Z:\\"], raising=False)

    found = fs_places.volumes("windows")

    assert [(v["label"], v["path"], v["kind"]) for v in found] == [
        ("Local Disk (C:)", "C:\\", "local"),
        ("Data (D:)", "D:\\", "local"),
        ("Network Drive (Z:)", "Z:\\", "network"),
    ]
    assert found[1]["fstype"] == "NTFS"
    # Never asked of the network drive: its server may be gone, and asking is
    # how a mapped drive hangs whatever touched it.
    assert "Z:\\" not in kernel32.asked
    # Critical-error dialogs off first, then the previous mode put back.
    assert kernel32.modes[0] == fs_places.SEM_FAILCRITICALERRORS
    assert len(kernel32.modes) == 2


def test_windows_without_kernel32_still_names_the_drives(monkeypatch, no_usage):
    monkeypatch.setattr(fs_places, "_kernel32", lambda: None)
    monkeypatch.setattr(os, "listdrives", lambda: ["C:\\"], raising=False)

    assert fs_places.volumes("windows")[0]["label"] == "Local Disk (C:)"


# -- macOS -------------------------------------------------------------------------


def test_macos_volumes_come_from_volumes_and_mount(tmp_path, monkeypatch, no_usage):
    volumes = tmp_path / "Volumes"
    volumes.mkdir()
    os.symlink("/", volumes / "Work Mac")
    for name in ("USB", "share", "Stale", ".hidden",
                 "com.apple.TimeMachine.localsnapshots"):
        (volumes / name).mkdir()
    monkeypatch.setattr(fs_places, "_volumes_root", lambda: volumes)
    monkeypatch.setattr(fs_places, "_mount_output", lambda: "\n".join([
        "/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)",
        "devfs on /dev (devfs, local, nobrowse)",
        f"/dev/disk4s1 on {volumes / 'USB'} (exfat, local, nodev, nosuid, noowners)",
        f"//aj@nas.lab.org/share on {volumes / 'share'} (smbfs, nodev, nosuid, mounted by aj)",
        f"/dev/disk5s1 on {volumes / '.hidden'} (apfs, local)",
    ]))

    found = fs_places.volumes("macos")

    assert found[0] == {"label": "Work Mac", "path": "/", "kind": "local",
                        "fstype": "apfs", "total": None, "free": None}
    assert [(v["label"], v["kind"], v["fstype"]) for v in found[1:]] == [
        ("share", "network", "smbfs"), ("USB", "removable", "exfat")]
    assert found[1]["source"] == "//aj@nas.lab.org/share"
    assert "source" not in found[2]


def test_macos_without_a_boot_link_still_offers_the_disk(tmp_path, monkeypatch,
                                                         no_usage):
    monkeypatch.setattr(fs_places, "_volumes_root", lambda: tmp_path / "nowhere")
    monkeypatch.setattr(fs_places, "_mount_output", lambda: "")

    assert fs_places.volumes("macos") == [{
        "label": "Macintosh HD", "path": "/", "kind": "local", "fstype": "",
        "total": None, "free": None}]


# -- Linux and clusters ---------------------------------------------------------

MOUNTINFO = "\n".join([
    "22 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw",
    "23 22 0:21 / /proc rw,nosuid shared:5 - proc proc rw",
    "24 22 0:22 / /sys rw shared:6 - sysfs sysfs rw",
    "25 22 0:5 / /dev rw shared:2 - devtmpfs udev rw",
    "26 22 0:23 / /run rw shared:7 - tmpfs tmpfs rw",
    "27 22 7:0 / /snap/core/123 ro shared:8 - squashfs /dev/loop0 ro",
    "28 22 8:3 / /var/lib/docker rw shared:9 - ext4 /dev/sda3 rw",
    "29 22 0:50 / /n/groups rw shared:20 - autofs /etc/auto.groups rw,fd=7",
    "30 29 0:61 / /n/groups/lab rw shared:21 - nfs4 nas:/export/groups/lab rw",
    "31 22 0:62 / /n/scratch rw shared:22 - lustre 10.0.0.1@o2ib:/scratch rw",
    "32 22 8:2 / /home rw shared:23 - xfs /dev/sda2 rw",
    "33 22 8:2 /aj/data /mnt/data rw shared:23 - xfs /dev/sda2 rw",
    "34 26 8:17 / /run/media/aj/MY\\040USB rw shared:24 - vfat /dev/sdb1 rw",
    "35 22 0:70 / /data rw shared:25 - nfs4 nas:/export/data rw",
    "36 22 0:24 / /tmp rw shared:26 - tmpfs tmpfs rw",
    "37 22 8:33 / /deep/a/b/c/d rw shared:27 - ext4 /dev/sdc1 rw",
    "38 22 0:81 / /home/aj/.cache/doc rw shared:28 - fuse.portal portal rw",
    "39 22 0:82 / /mnt/bucket rw shared:29 - fuse.rclone remote: rw",
    "40 22 0:83 / /etc/hosts rw shared:30 - ext4 /dev/sda1 rw",
])


def test_a_cluster_mount_table_is_cut_down_to_places(monkeypatch, no_usage):
    monkeypatch.setattr(fs_places, "_read_mountinfo", lambda: MOUNTINFO)
    monkeypatch.setenv("SCRATCH", "/n/scratch/users/a/aj")
    for name in ("WORK", "PROJECT", "DATA"):
        monkeypatch.delenv(name, raising=False)

    found = fs_places.volumes("linux")

    assert [v["path"] for v in found] == [
        # The root first; then the mount that holds $SCRATCH; then by the
        # top-level folders shared storage lives under.
        "/", "/n/scratch", "/home", "/n/groups", "/data", "/mnt/bucket",
        "/run/media/aj/MY USB",
    ]
    by_path = {v["path"]: v for v in found}
    assert by_path["/"]["label"] == "Root"
    assert by_path["/n/scratch"]["kind"] == "network"
    assert by_path["/n/scratch"]["source"] == "10.0.0.1@o2ib:/scratch"
    assert by_path["/n/groups"]["kind"] == "network"  # autofs
    assert by_path["/data"]["fstype"] == "nfs4"
    assert by_path["/mnt/bucket"]["kind"] == "remote"
    usb = by_path["/run/media/aj/MY USB"]
    assert usb["label"] == "MY USB" and usb["kind"] == "removable"
    # /mnt/data is /home bound somewhere else: one filesystem, shown once, at
    # its shortest mount point. /n/groups/lab is under the automount root.
    assert "/mnt/data" not in by_path and "/n/groups/lab" not in by_path


def test_the_mount_table_is_cut_at_the_volume_limit(monkeypatch, no_usage):
    monkeypatch.setattr(fs_places, "_read_mountinfo", lambda: MOUNTINFO)
    monkeypatch.setattr(fs_places, "VOLUMES_LIMIT", 3)

    found = fs_places.volumes("linux")

    assert len(found) == 3 and found[0]["path"] == "/"


def test_proc_mounts_is_read_when_mountinfo_is_not_there(monkeypatch, no_usage):
    monkeypatch.setattr(fs_places, "_read_mountinfo", lambda: "\n".join([
        "/dev/sda1 / ext4 rw 0 0",
        "proc /proc proc rw 0 0",
        "nas:/export/groups /n/groups nfs rw 0 0",
    ]))

    assert [v["path"] for v in fs_places.volumes("linux")] == ["/", "/n/groups"]


def test_a_container_root_is_still_offered(monkeypatch, no_usage):
    monkeypatch.setattr(fs_places, "_read_mountinfo", lambda: (
        "1 0 0:40 / / rw - overlay overlay rw,lowerdir=/x\n"
        "2 1 0:41 / /proc rw - proc proc rw"))

    assert fs_places.volumes("linux") == [{
        "label": "Root", "path": "/", "kind": "local", "fstype": "overlay",
        "total": None, "free": None}]


# -- free space ---------------------------------------------------------------------


def test_free_space_never_waits_long_on_a_mount_that_hangs(monkeypatch, fresh_usage):
    """A network mount whose server has gone blocks `disk_usage` in the
    kernel. The answer arrives anyway, without a caption for that volume, and
    the volume is not asked again for five minutes."""
    release = threading.Event()
    asked = []

    def _usage(path):
        asked.append(path)
        if path == "/slow":
            release.wait(5)
        return Usage(1000, 600, 400)

    monkeypatch.setattr(fs_places.shutil, "disk_usage", _usage)
    try:
        began = time.monotonic()
        answer = fs_places._usage_many(["/fast", "/slow"])
        assert time.monotonic() - began < 2.0
        assert answer == {"/fast": (1000, 400), "/slow": None}
        assert fs_places._usage_memo["/slow"][0] - fs_places._now() > 250

        # Asked again: both from memory, nothing measured twice.
        assert fs_places._usage_many(["/fast", "/slow"]) == answer
        assert sorted(asked) == ["/fast", "/slow"]

        # Thirty seconds on, the fast one is measured again and the slow one
        # is still left alone.
        later = fs_places._now() + 31
        monkeypatch.setattr(fs_places, "_now", lambda: later)
        fs_places._usage_many(["/fast", "/slow"])
        assert sorted(asked) == ["/fast", "/fast", "/slow"]
    finally:
        release.set()


def test_a_volume_that_cannot_say_has_no_caption(monkeypatch, fresh_usage):
    def _usage(path):
        raise PermissionError(path)

    monkeypatch.setattr(fs_places.shutil, "disk_usage", _usage)

    assert fs_places._usage_many(["/locked"]) == {"/locked": None}


# -- known folders and project roots -----------------------------------------------


def test_known_folders_are_the_ones_that_exist(tmp_path):
    (tmp_path / "Desktop").mkdir()
    (tmp_path / "Downloads").mkdir()

    found = fs_places.known_folders(tmp_path, "macos")

    assert found == [
        {"id": "desktop", "label": "Desktop", "path": str(tmp_path / "Desktop")},
        {"id": "downloads", "label": "Downloads", "path": str(tmp_path / "Downloads")},
    ]


def test_a_linux_desktop_s_moved_folders_are_honoured(tmp_path, monkeypatch):
    config = tmp_path / ".config"
    config.mkdir()
    (config / "user-dirs.dirs").write_text(
        '# written by xdg-user-dirs-update\n'
        'XDG_DESKTOP_DIR="$HOME/Schreibtisch"\n'
        'XDG_DOCUMENTS_DIR="$HOME/"\n')
    (tmp_path / "Schreibtisch").mkdir()
    (tmp_path / "Desktop").mkdir()
    (tmp_path / "Documents").mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))

    found = {entry["id"]: entry["path"]
             for entry in fs_places.known_folders(tmp_path, "linux")}

    assert found["desktop"] == str(tmp_path / "Schreibtisch")
    # "$HOME/" is how the file says the folder is switched off.
    assert found["documents"] == str(tmp_path / "Documents")


def test_onedrive_backup_wins_on_windows(tmp_path, monkeypatch):
    cloud = tmp_path / "OneDrive - Lab"
    (cloud / "Desktop").mkdir(parents=True)
    (tmp_path / "Desktop").mkdir()
    monkeypatch.setenv("OneDriveCommercial", str(cloud))
    monkeypatch.delenv("OneDrive", raising=False)

    found = fs_places.known_folders(tmp_path, "windows")

    assert found[0]["path"] == str(cloud / "Desktop")


def test_project_roots_are_named_and_never_stat_ed(tmp_path, monkeypatch):
    """`$SCRATCH` on a cluster is a network filesystem, and one slow to answer
    must not hold up a sidebar it only adds a line to: offered as written."""
    from plexora import paths

    shared = tmp_path / "lab-share"
    monkeypatch.setattr(paths, "shared_root_resolutions",
                        lambda: [paths.Resolution(shared, "test")])
    monkeypatch.setenv("SCRATCH", "/n/scratch/users/a/aj")
    monkeypatch.setenv("WORK", "relative/path")
    monkeypatch.setenv("PROJECT", "/n/scratch/users/a/aj")
    monkeypatch.delenv("DATA", raising=False)

    found = fs_places.project_places()

    assert found == [
        {"label": "Plexora data", "path": str(paths.data_root())},
        {"label": "Shared: lab-share", "path": str(shared)},
        {"label": "$SCRATCH", "path": "/n/scratch/users/a/aj"},
    ]
    assert not Path("/n/scratch/users/a/aj").exists()


# -- the whole answer -----------------------------------------------------------------


def test_describe_answers_every_key_even_when_a_part_fails(tmp_path, monkeypatch,
                                                           no_usage):
    monkeypatch.setattr(fs_places, "_platform", lambda: "linux")
    monkeypatch.setattr(fs_places, "_home", lambda: tmp_path)
    monkeypatch.setattr(fs_places, "_hostname", lambda: "login01")

    def _broken():
        raise RuntimeError("no mount table")

    monkeypatch.setattr(fs_places, "_linux_volumes", _broken)

    found = fs_places.describe()

    assert found["platform"] == "linux" and found["hostname"] == "login01"
    assert found["home"] == str(tmp_path)
    assert found["volumes"] == [] and found["known"] == []
    assert found["project"][0]["label"] == "Plexora data"


def test_the_hostname_is_the_short_one(monkeypatch):
    monkeypatch.setattr(fs_places.socket, "gethostname",
                        lambda: "login01.o2.rc.hms.harvard.edu")
    assert fs_places._hostname() == "login01"
    monkeypatch.setattr(fs_places.socket, "gethostname", lambda: "10.0.0.7")
    assert fs_places._hostname() == "10.0.0.7"
