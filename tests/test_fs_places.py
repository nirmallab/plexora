"""The explorer's sidebar on every operating system, tested on whichever runs this.

Each OS answers "where might the data be?" differently -- drive letters,
`/Volumes`, a mount table full of things that are not places -- and the sidebar
has to be right on all three, plus every cluster. The module's seams
(`_platform`, `_read_mountinfo`, `_volumes_root`, `_mount_output`, `_kernel32`,
`_mpr`, `_usage_many`) are what let a Mac run the Windows and Linux branches
here; the cloud folders take a home and a platform, so a `tmp_path` home is
any machine.

The one promise that is about time rather than shape: nothing here waits long
on a mount. A network volume whose server has gone answers no free space, not
a hung sidebar, and a share or a cloud folder is never listed to find it.
"""

from __future__ import annotations

import json
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
    monkeypatch.setattr(fs_places, "_mpr", lambda: None)
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


class _Mpr:
    """`WNetGetConnectionW` answered from a table of mapped drives."""

    ERROR_NOT_CONNECTED = 2250

    def __init__(self, shares):
        self.shares = shares
        self.asked = []

    def WNetGetConnectionW(self, local, buffer, size):  # noqa: N802 - the Win32 name
        self.asked.append(local)
        share = self.shares.get(local)
        if share is None:
            return self.ERROR_NOT_CONNECTED
        if len(share) + 1 > len(buffer):
            size._obj.value = len(share) + 1  # what the caller passed by reference
            return fs_places.ERROR_MORE_DATA
        buffer.value = share
        return 0


def test_a_mapped_drive_is_named_after_its_share(monkeypatch, no_usage):
    """Explorer's "projects (\\\\fileserver) (Z:)", read from this machine's own
    table of mappings -- the server is still never asked anything."""
    class Kernel32(_Kernel32):
        TYPES = {**_Kernel32.TYPES, "W:\\": fs_places.DRIVE_REMOTE,
                 "X:\\": fs_places.DRIVE_REMOTE, "Y:\\": fs_places.DRIVE_REMOTE}

    long_share = "\\\\archive\\" + "deep\\" * 60 + "run1"
    kernel32 = Kernel32()
    mpr = _Mpr({"Z:": "\\\\fileserver\\projects", "Y:": "\\\\srv\\share\\sub",
                "W:": long_share})
    monkeypatch.setattr(fs_places, "_kernel32", lambda: kernel32)
    monkeypatch.setattr(fs_places, "_mpr", lambda: mpr)
    monkeypatch.setattr(os, "listdrives",
                        lambda: ["C:\\", "W:\\", "X:\\", "Y:\\", "Z:\\"], raising=False)

    found = {volume["path"]: volume for volume in fs_places.volumes("windows")}

    assert found["Z:\\"]["label"] == "projects (\\\\fileserver) (Z:)"
    assert found["Z:\\"]["source"] == "\\\\fileserver\\projects"
    assert found["Y:\\"]["label"] == "sub (\\\\srv\\share) (Y:)"
    assert found["Y:\\"]["source"] == "\\\\srv\\share\\sub"
    # Longer than the first buffer: asked again with the size it said it needs.
    assert found["W:\\"]["label"].startswith("run1 (\\\\archive\\deep\\")
    assert found["W:\\"]["source"] == long_share
    # The mapping could not be read: Explorer's generic name, and no source.
    assert found["X:\\"]["label"] == "Network Drive (X:)"
    assert "source" not in found["X:\\"]
    assert mpr.asked == ["W:", "W:", "X:", "Y:", "Z:"]
    assert not {"W:\\", "X:\\", "Y:\\", "Z:\\"} & set(kernel32.asked)


def test_a_mapped_drive_without_mpr_keeps_the_generic_name(monkeypatch, no_usage):
    monkeypatch.setattr(fs_places, "_kernel32", lambda: _Kernel32())
    monkeypatch.setattr(fs_places, "_mpr", lambda: None)
    monkeypatch.setattr(os, "listdrives", lambda: ["Z:\\"], raising=False)

    assert fs_places.volumes("windows") == [{
        "label": "Network Drive (Z:)", "path": "Z:\\", "kind": "network",
        "fstype": "", "total": None, "free": None}]


def test_a_unc_path_with_no_share_names_nothing():
    assert fs_places._network_drive_label("\\\\fileserver", "Z:") is None
    assert fs_places._network_drive_label("\\\\fileserver\\projects\\", "Z:") == (
        "projects (\\\\fileserver) (Z:)")


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


def test_macos_network_shares_mounted_elsewhere_are_offered_unstat_ed(
        tmp_path, monkeypatch, no_usage):
    """A share mounted into a folder in home is in the mount table and nowhere
    else. It is offered from the table alone -- none of these paths exist here,
    and a dead server's mount point would hang whatever stat'ed it."""
    volumes = tmp_path / "Volumes"
    (volumes / "share").mkdir(parents=True)
    monkeypatch.setattr(fs_places, "_volumes_root", lambda: volumes)
    monkeypatch.setattr(fs_places, "_mount_output", lambda: "\n".join([
        "/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)",
        f"//jane@nas.lab.org/share on {volumes / 'share'} (smbfs, nodev, nosuid, mounted by jane)",
        "//jane@nas.lab.org/lab on /Users/jane/mnt/lab data (smbfs, nodev, nosuid, mounted by jane)",
        "jane@login.example.org:/n/groups on /Users/jane/cluster (macfuse, nodev, nosuid, synchronous, mounted by jane)",
        "nas:/export/runs on /net/nas/export/runs (nfs, nodev, nosuid, automounted)",
        "map -hosts on /net (autofs, nosuid, automounted, nobrowse)",
        "map auto_home on /System/Volumes/Data/home (autofs, automounted, nobrowse)",
        "//jane@nas.lab.org/sys on /System/Volumes/Data/sys (smbfs, nodev, nosuid)",
        "//jane@nas.lab.org/old on /Volumes/old (smbfs, nodev, nosuid)",
        "/dev/disk3s5 on /System/Volumes/Data (apfs, local, journaled, nobrowse)",
    ]))

    found = fs_places.volumes("macos")

    assert [volume["path"] for volume in found] == [
        "/", str(volumes / "share"), "/Users/jane/cluster",
        "/Users/jane/mnt/lab data", "/net/nas/export/runs"]
    assert found[3] == {"label": "lab data", "path": "/Users/jane/mnt/lab data",
                        "kind": "network", "fstype": "smbfs",
                        "source": "//jane@nas.lab.org/lab", "total": None,
                        "free": None}
    assert found[2]["fstype"] == "macfuse" and found[2]["kind"] == "network"
    assert found[4]["source"] == "nas:/export/runs"
    assert not Path("/Users/jane/cluster").exists()


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


# -- cloud folders ------------------------------------------------------------------


@pytest.fixture
def no_cloud_env(monkeypatch):
    """None of the Windows variables a cloud folder is found by, whatever the
    machine running the suite has."""
    for name in ("APPDATA", "LOCALAPPDATA", "OneDrive", "OneDriveCommercial",
                 "OneDriveConsumer"):
        monkeypatch.delenv(name, raising=False)


def _info_json(folder, **accounts):
    """Dropbox's `info.json`, as the client writes it, in `folder`."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "info.json").write_text(json.dumps({
        account_type: {"path": str(path), "host": 1,
                       "is_team": account_type == "business"}
        for account_type, path in accounts.items()}))


def _cloud_storage(home, *names):
    storage = home / "Library" / "CloudStorage"
    for name in names:
        (storage / name).mkdir(parents=True)
    return storage


def test_dropbox_folders_come_from_its_own_info_json(tmp_path, no_cloud_env):
    """A team space names the member's folder inside the team's; the place a
    person means is the team's folder, which holds the shared folders too."""
    personal = tmp_path / "Dropbox (Personal)"
    team = tmp_path / "Acme Dropbox"
    for folder in (personal, team / "Jane Doe"):
        folder.mkdir(parents=True)
    _info_json(tmp_path / ".dropbox", personal=personal,
               business=team / "Jane Doe")

    assert fs_places.cloud_folders(tmp_path, "macos") == [
        {"label": "Acme Dropbox", "path": str(team), "provider": "dropbox"},
        {"label": "Dropbox (Personal)", "path": str(personal),
         "provider": "dropbox"},
    ]


def test_a_dropbox_folder_named_otherwise_is_just_dropbox(tmp_path):
    home = tmp_path / "dropbox-fan"
    # Moved and renamed: no name to use.
    assert fs_places._dropbox_place(tmp_path / "Sync" / "Jane", home) == (
        "Dropbox", tmp_path / "Sync" / "Jane")
    # Home itself is never offered as a team's folder, whatever it is called.
    assert fs_places._dropbox_place(home / "Files", home) == ("Dropbox", home / "Files")


@pytest.mark.parametrize("text", [
    "{not json",
    "[]",
    '{"personal": "nope", "business": {"path": "relative/Dropbox"}}',
])
def test_a_malformed_info_json_drops_only_dropbox(tmp_path, no_cloud_env, text):
    (tmp_path / ".dropbox").mkdir()
    (tmp_path / ".dropbox" / "info.json").write_text(text)
    (tmp_path / "relative" / "Dropbox").mkdir(parents=True)
    icloud = tmp_path / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
    icloud.mkdir(parents=True)

    assert fs_places.cloud_folders(tmp_path, "macos") == [
        {"label": "iCloud Drive", "path": str(icloud), "provider": "icloud"}]


def test_a_source_that_fails_drops_only_itself(tmp_path, monkeypatch, no_cloud_env):
    (tmp_path / "Box").mkdir()

    def _broken(home, platform):
        yield "Dropbox", home, "dropbox", None
        raise RuntimeError("half an answer")

    monkeypatch.setattr(fs_places, "_dropbox_info_folders", _broken)

    # Not even the row it gave before failing.
    assert [row["label"] for row in fs_places.cloud_folders(tmp_path, "linux")] == [
        "Box"]


def test_onedrive_in_cloud_storage_takes_the_name_its_home_link_has(
        tmp_path, monkeypatch, no_cloud_env):
    """OneDrive links its folder into home under the name it shows; that name
    wins. A folder with no link is spelled out from its own name."""
    storage = _cloud_storage(tmp_path, "OneDrive-MGB", "OneDrive-ContosoLabs",
                             "OneDrive")
    os.symlink(storage / "OneDrive-MGB", tmp_path / "OneDrive - Mass General Brigham")
    # Any other link in home is read and never followed: its target could be a
    # share whose server has gone.
    os.symlink("/Volumes/gone/lab", tmp_path / "lab")
    resolved = []
    realpath = os.path.realpath

    def _spy(path, **kwargs):
        resolved.append(str(path))
        return realpath(path, **kwargs)

    monkeypatch.setattr(fs_places.os.path, "realpath", _spy)

    found = fs_places.cloud_folders(tmp_path, "macos")

    assert found == [
        {"label": "OneDrive", "path": str(storage / "OneDrive"),
         "provider": "onedrive"},
        {"label": "OneDrive - Contoso Labs",
         "path": str(storage / "OneDrive-ContosoLabs"), "provider": "onedrive",
         "account": "ContosoLabs"},
        {"label": "OneDrive - Mass General Brigham",
         "path": str(storage / "OneDrive-MGB"), "provider": "onedrive",
         "account": "MGB"},
    ]
    assert str(tmp_path / "OneDrive - Mass General Brigham") in resolved
    assert str(tmp_path / "lab") not in resolved


def test_every_cloud_storage_provider_has_a_name(tmp_path, no_cloud_env):
    storage = _cloud_storage(tmp_path, "GoogleDrive-a@b.org", "Box-Box",
                             "Dropbox-AcmeTeam", "ProtonDrive-a@b.org",
                             "Tresorit", ".Trash")
    (storage / ".DS_Store").write_text("")

    found = fs_places.cloud_folders(tmp_path, "macos")

    assert [(row["label"], row["provider"], row.get("account")) for row in found] == [
        ("Box", "box", "Box"),
        ("Dropbox (Acme Team)", "dropbox", "AcmeTeam"),
        ("Google Drive", "google_drive", "a@b.org"),
        ("Proton Drive (a@b.org)", "other", "a@b.org"),
        ("Tresorit", "other", None),
    ]
    assert found[2]["path"] == str(storage / "GoogleDrive-a@b.org")


def test_icloud_drive_on_macos_and_windows(tmp_path, no_cloud_env):
    mac = tmp_path / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
    mac.mkdir(parents=True)
    (tmp_path / "iCloudDrive").mkdir()

    icloud = {"label": "iCloud Drive", "provider": "icloud"}
    assert fs_places.cloud_folders(tmp_path, "macos") == [{**icloud, "path": str(mac)}]
    assert fs_places.cloud_folders(tmp_path, "windows") == [
        {**icloud, "path": str(tmp_path / "iCloudDrive")}]
    assert fs_places.cloud_folders(tmp_path, "linux") == []


@pytest.mark.parametrize("platform", ["macos", "linux"])
def test_a_link_to_a_folder_already_found_is_listed_once(tmp_path, no_cloud_env,
                                                        platform):
    """`~/Dropbox` linked to the folder `info.json` names is one folder, under
    the name `info.json` gave it."""
    personal = tmp_path / "Dropbox (Personal)"
    personal.mkdir()
    _info_json(tmp_path / ".dropbox", personal=personal)
    os.symlink(personal, tmp_path / "Dropbox")

    assert fs_places.cloud_folders(tmp_path, platform) == [
        {"label": "Dropbox (Personal)", "path": str(personal),
         "provider": "dropbox"}]


def test_folders_that_are_not_there_are_not_offered(tmp_path, no_cloud_env):
    _info_json(tmp_path / ".dropbox", personal=tmp_path / "Dropbox (Personal)",
               business=tmp_path / "Gone Dropbox" / "Jane Doe")
    storage = _cloud_storage(tmp_path)
    storage.mkdir(parents=True)
    (storage / "notes.txt").write_text("")
    (tmp_path / "Google Drive").write_text("not a folder")

    assert fs_places.cloud_folders(tmp_path, "macos") == []
    assert fs_places.cloud_folders(tmp_path / "nobody", "linux") == []


def test_cloud_folders_are_sorted_by_label_and_capped(tmp_path, monkeypatch,
                                                      no_cloud_env):
    _cloud_storage(tmp_path, "OneDrive-Beta", "Dropbox-zeta",
                   "GoogleDrive-x@y.org", "Box")
    (tmp_path / "Library" / "Mobile Documents" / "com~apple~CloudDocs").mkdir(
        parents=True)

    labels = [row["label"] for row in fs_places.cloud_folders(tmp_path, "macos")]
    # Case-blind: "iCloud Drive" between Google and OneDrive, not after them.
    assert labels == ["Box", "Dropbox (zeta)", "Google Drive", "iCloud Drive",
                      "OneDrive - Beta"]

    monkeypatch.setattr(fs_places, "CLOUD_LIMIT", 3)
    assert [row["label"] for row in fs_places.cloud_folders(tmp_path, "macos")] == [
        "Box", "Dropbox (zeta)", "Google Drive"]


def test_windows_cloud_folders_come_from_the_environment(tmp_path, monkeypatch,
                                                         no_cloud_env):
    """Dropbox's `info.json` under `%APPDATA%`, OneDrive's one folder per
    account from its own variables, then the folders in home."""
    roaming = tmp_path / "AppData" / "Roaming"
    personal = tmp_path / "Dropbox"
    work = tmp_path / "OneDrive - Contoso"
    for folder in (personal, work, tmp_path / "OneDrive", tmp_path / "Box"):
        folder.mkdir(parents=True)
    _info_json(roaming / "Dropbox", personal=personal)
    monkeypatch.setenv("APPDATA", str(roaming))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    monkeypatch.setenv("OneDrive", str(work))
    monkeypatch.setenv("OneDriveCommercial", str(work))
    monkeypatch.setenv("OneDriveConsumer", "relative-OneDrive")

    assert fs_places.cloud_folders(tmp_path, "windows") == [
        {"label": "Box", "path": str(tmp_path / "Box"), "provider": "box"},
        {"label": "Dropbox", "path": str(personal), "provider": "dropbox"},
        {"label": "OneDrive", "path": str(tmp_path / "OneDrive"),
         "provider": "onedrive"},
        {"label": "OneDrive - Contoso", "path": str(work), "provider": "onedrive",
         "account": "Contoso"},
    ]


def test_linux_has_the_folders_its_clients_sync_to(tmp_path, no_cloud_env):
    for name in ("OneDrive", "Dropbox"):
        (tmp_path / name).mkdir()

    assert fs_places.cloud_folders(tmp_path, "linux") == [
        {"label": "Dropbox", "path": str(tmp_path / "Dropbox"), "provider": "dropbox"},
        {"label": "OneDrive", "path": str(tmp_path / "OneDrive"),
         "provider": "onedrive"},
    ]


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
    assert found["cloud"] == []
    assert found["project"][0]["label"] == "Plexora data"


def test_describe_answers_a_cloud_list_even_when_finding_them_fails(
        tmp_path, monkeypatch, no_usage):
    monkeypatch.setattr(fs_places, "_platform", lambda: "macos")
    monkeypatch.setattr(fs_places, "_home", lambda: tmp_path)
    monkeypatch.setattr(fs_places, "_volumes_root", lambda: tmp_path / "Volumes")
    monkeypatch.setattr(fs_places, "_mount_output", lambda: "")
    (tmp_path / "Desktop").mkdir()

    def _broken(home=None, platform=None):
        raise RuntimeError("CloudStorage unreadable")

    monkeypatch.setattr(fs_places, "cloud_folders", _broken)

    found = fs_places.describe()

    assert found["cloud"] == []
    assert found["known"][0]["label"] == "Desktop"
    assert found["volumes"][0]["path"] == "/"


def test_the_hostname_is_the_short_one(monkeypatch):
    monkeypatch.setattr(fs_places.socket, "gethostname",
                        lambda: "login01.o2.rc.hms.harvard.edu")
    assert fs_places._hostname() == "login01"
    monkeypatch.setattr(fs_places.socket, "gethostname", lambda: "10.0.0.7")
    assert fs_places._hostname() == "10.0.0.7"
