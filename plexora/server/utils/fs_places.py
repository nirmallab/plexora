"""The places a machine offers the explorer's sidebar, on any operating system.

One question -- "where on this machine might somebody's data be?" -- asked of
three filesystems that answer it in three different ways:

- **Windows** has drive letters, each with a type and a label, and a mapped
  network drive that is not answering can hang anything that touches it.
- **macOS** mounts every volume under `/Volumes`, the boot disk included (as a
  link to `/`), and says what each one is in `mount`'s output.
- **Linux**, and every cluster, has a mount table full of things that are not
  places at all -- `/proc`, cgroups, snaps, container layers -- around the few
  that are: `/`, `/home`, and the shared filesystems (`/n/scratch`,
  `/n/groups`, `/gpfs`) that are the whole point of browsing a cluster.

Plus the folders a person means by name (Desktop, Documents, Downloads), the
folders a cloud client syncs to (Dropbox, OneDrive, Google Drive, iCloud
Drive, Box), the Plexora data folder, and a cluster's `$SCRATCH`/`$WORK`-style
roots.

**Nothing here may hang the explorer.** A volume's free space is the only
question that touches a mount, and it is asked on daemon threads with a short
deadline; a mount that does not answer gets no caption and is not asked again
for five minutes. Environment roots are named and never stat'ed, and neither
is a network share found in the mount table. Cloud folders are found from
local metadata and checked to exist, never listed and never asked of the
network. Every OS hook is a module-level seam (`_platform`, `_home`,
`_read_mountinfo`, `_volumes_root`, `_mount_output`, `_kernel32`, `_mpr`,
`_usage_many`) so each branch is tested on whichever machine runs the suite.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

#: The most volumes one answer lists. A cluster node's mount table can name
#: hundreds of automounted shares; the ranked first forty are the ones a
#: person browses from.
VOLUMES_LIMIT = 40

#: Seconds one volume's free-space question may take, and all of them together.
USAGE_EACH_S = 0.5
USAGE_TOTAL_S = 1.5
#: How long an answer is reused, and how long a volume that did not answer in
#: time is left alone before it is asked again.
USAGE_MEMO_S = 30.0
USAGE_SLOW_RETRY_S = 300.0

#: Seconds `mount` gets on macOS.
MOUNT_TIMEOUT_S = 2.0

#: Mount points deeper than this are somebody's bind mount, not a place.
MAX_MOUNT_DEPTH = 4

#: The environment variables a cluster names its working roots with. Offered
#: as they are written; never stat'ed, because on a cluster these are network
#: filesystems and the sidebar must draw before any of them answers.
PROJECT_ENV = ("SCRATCH", "WORK", "PROJECT", "DATA")

#: The top-level folders shared storage lives under, best first. A mount under
#: one of these outranks one that is not, after `/` and the env roots.
PRIORITY_ROOTS = ("home", "n", "scratch", "data", "work", "project", "projects",
                  "groups", "gpfs", "lustre", "nfs", "net", "mnt", "media",
                  "export", "share", "shared", "storage", "opt")

#: What a person calls the three folders every desktop has, and the
#: `user-dirs.dirs` key a Linux desktop may have moved each one to.
KNOWN_FOLDERS = (("desktop", "Desktop", "XDG_DESKTOP_DIR"),
                 ("documents", "Documents", "XDG_DOCUMENTS_DIR"),
                 ("downloads", "Downloads", "XDG_DOWNLOAD_DIR"))

#: The most cloud folders one answer lists. A person has a handful; the cap
#: is there so a CloudStorage folder full of old accounts cannot fill the
#: sidebar.
CLOUD_LIMIT = 20

#: The name before the dash in `~/Library/CloudStorage/<Provider>-<Account>`
#: (casefolded) -> the provider the sidebar draws an icon for. Any other
#: provider is `other`.
_CLOUD_STORAGE_PROVIDERS = {"dropbox": "dropbox", "onedrive": "onedrive",
                            "googledrive": "google_drive", "box": "box"}

#: Folders directly in home that a cloud client syncs to: Box Drive on
#: Windows and older macOS, Google's Backup and Sync, and Dropbox's default.
#: `(folder, label, provider)`.
_LEGACY_CLOUD = (("Box", "Box", "box"),
                 ("Google Drive", "Google Drive", "google_drive"),
                 ("Dropbox", "Dropbox", "dropbox"))


# -- the seams --------------------------------------------------------------


def _platform():
    """`windows`, `macos` or `linux`."""
    from plexora import _resources

    return _resources.platform_word()


def _home():
    return Path.home()


def _now():
    return time.monotonic()


def _read_mountinfo():
    """The Linux mount table: `/proc/self/mountinfo`, else `/proc/mounts`."""
    for name in ("/proc/self/mountinfo", "/proc/mounts"):
        try:
            with open(name, encoding="utf-8", errors="replace") as handle:
                return handle.read()
        except OSError:
            continue
    return ""


def _volumes_root():
    return Path("/Volumes")


def _mount_output():
    """`mount`'s output on macOS, or "" when it would not say in time."""
    from plexora._subprocess import popen_kwargs

    command = ["/sbin/mount"] if os.path.exists("/sbin/mount") else ["mount"]
    try:
        done = subprocess.run(command, capture_output=True, text=True,
                              timeout=MOUNT_TIMEOUT_S, check=False,
                              **popen_kwargs())
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout or ""


def _kernel32():
    """Windows' kernel32 through ctypes, or None anywhere else."""
    try:
        import ctypes

        return ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError):
        return None


def _mpr():
    """Windows' mpr.dll (the network-provider router) through ctypes, or None
    anywhere else."""
    try:
        import ctypes

        return ctypes.WinDLL("mpr", use_last_error=True)
    except (AttributeError, OSError):
        return None


# -- the whole answer -------------------------------------------------------


def describe():
    """`{platform, hostname, home, known, volumes, cloud, project}` for this machine.

    What `/fs_places` (and a node's `/node/v1/fs_places`) answers before the
    remembered places are added. Each part is guarded on its own: a mount
    table that cannot be read still leaves Home, the known folders, the cloud
    folders and the project roots.
    """
    platform = _platform()
    home = _home()
    return {
        "platform": platform,
        "hostname": _hostname(),
        "home": str(home),
        "known": _guarded(lambda: known_folders(home, platform)),
        "volumes": _guarded(lambda: volumes(platform)),
        "cloud": _guarded(lambda: cloud_folders(home, platform)),
        "project": _guarded(project_places),
    }


def _guarded(part):
    try:
        return part()
    except Exception:  # noqa: BLE001 - one missing section, never a failure
        return []


def _hostname():
    """This machine's short name: `login01`, not `login01.o2.rc.hms...`."""
    try:
        name = socket.gethostname() or ""
    except OSError:
        return ""
    if re.fullmatch(r"[\d.]+", name) or ":" in name:
        return name
    return name.split(".", 1)[0]


def _is_dir(path):
    try:
        return Path(path).is_dir()
    except OSError:
        return False


# -- folders a person means by name -------------------------------------------


def known_folders(home=None, platform=None):
    """`[{id, label, path}]` for Desktop, Documents and Downloads, where present.

    A Linux desktop may have moved them (`~/.config/user-dirs.dirs`, often to
    localised names), and Windows' OneDrive backup moves them under OneDrive --
    both are honoured before the plain `~/Desktop`.
    """
    home = Path(home if home is not None else _home())
    platform = platform or _platform()
    moved = _user_dirs(home) if platform == "linux" else {}
    clouds = _onedrive_roots(home) if platform == "windows" else []
    found = []
    for folder_id, label, key in KNOWN_FOLDERS:
        candidates = [moved[key]] if key in moved else []
        candidates += [root / label for root in clouds]
        candidates.append(home / label)
        for candidate in candidates:
            if _is_dir(candidate):
                found.append({"id": folder_id, "label": label,
                              "path": str(candidate)})
                break
    return found


def _user_dirs(home):
    """`{XDG_*_DIR: Path}` from `user-dirs.dirs`, absolute and not home itself.

    A key set to `$HOME` is how that file says "this folder is disabled".
    """
    config = os.environ.get("XDG_CONFIG_HOME") or str(home / ".config")
    try:
        text = (Path(config) / "user-dirs.dirs").read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return {}
    moved = {}
    for line in text.splitlines():
        match = re.match(r'\s*(XDG_[A-Z]+_DIR)\s*=\s*"?([^"]*)"?\s*$', line)
        if not match:
            continue
        value = match.group(2).replace("${HOME}", str(home)).replace("$HOME", str(home))
        path = Path(value) if os.path.isabs(value) else home / value
        if path != home:
            moved[match.group(1)] = path
    return moved


def _onedrive_roots(home):
    """The OneDrive folders a Windows account has, newest naming first."""
    roots = []
    for key in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        value = os.environ.get(key)
        if value and os.path.isabs(value):
            roots.append(Path(value))
    roots.append(home / "OneDrive")
    return list(dict.fromkeys(roots))


# -- cloud folders ----------------------------------------------------------------


def cloud_folders(home=None, platform=None):
    """`[{label, path, provider, account?}]`: the folders a cloud client syncs to.

    Dropbox, OneDrive, Google Drive, iCloud Drive and Box each keep a local
    folder that mirrors the cloud. They are found from what is on this disk --
    Dropbox's own `info.json`, one listing of `~/Library/CloudStorage` (where
    macOS keeps every sync client's folder), one of home for the links OneDrive
    names its folder with -- and each candidate is only checked to be a folder.
    None is ever listed: listing a folder that is online-only asks the client
    to fetch it, and nothing here touches the network.

    The sources are asked in order -- `info.json`, CloudStorage, iCloud,
    OneDrive's own, the legacy folders in home -- each guarded on its own, so
    a malformed `info.json` drops Dropbox's answer and nothing else. One folder
    reached two ways (a `~/Dropbox` link to the folder `info.json` names) is
    listed once, under the label of the first source that found it.
    `provider` picks the icon; `account`, the e-mail or organisation a folder's
    name carries, is there for a tooltip.
    """
    home = Path(home if home is not None else _home())
    platform = platform or _platform()
    found, seen = [], set()
    for source in (_dropbox_info_folders, _cloud_storage_folders,
                   _icloud_folders, _onedrive_folders, _legacy_cloud_folders):
        try:
            rows = list(_existing(source(home, platform)))
        except Exception:  # noqa: BLE001 - that source is missing, not the rest
            continue
        for identity, row in rows:
            if identity not in seen:
                seen.add(identity)
                found.append(row)
    found.sort(key=lambda row: row["label"].casefold())
    return found[:CLOUD_LIMIT]


def _existing(candidates):
    """`(identity, row)` for each `(label, path, provider, account)` that is a
    folder. The identity is the real path, so a link and its folder are one."""
    for label, path, provider, account in candidates:
        if not _is_dir(path):
            continue
        row = {"label": label, "path": str(path), "provider": provider}
        if account:
            row["account"] = account
        yield _identity(path), row


def _identity(path):
    return os.path.normcase(os.path.realpath(str(path)))


def _plain(path):
    return os.path.normcase(os.path.normpath(str(path)))


def _words(name):
    """`ContosoLabs` -> `Contoso Labs`: CloudStorage folder names drop the
    spaces the provider's own name for the account has."""
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)


def _dropbox_info_folders(home, platform):
    """Dropbox's personal and team folders, where its own `info.json` says.

    Dropbox lets a person move its folder anywhere and names it after the
    account (`Dropbox (Personal)`, a team's `Acme Dropbox`); `info.json` is the
    one place that says where, without asking Dropbox. A file that is missing,
    unreadable or not what Dropbox writes is skipped.
    """
    for info in _dropbox_info_files(home, platform):
        try:
            data = json.loads(info.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        for account_type in ("personal", "business"):
            account = data.get(account_type)
            value = account.get("path") if isinstance(account, dict) else None
            if isinstance(value, str) and os.path.isabs(value):
                label, folder = _dropbox_place(Path(value), home)
                yield label, folder, "dropbox", None


def _dropbox_info_files(home, platform):
    """Where Dropbox keeps `info.json`: `%APPDATA%`, then `%LOCALAPPDATA%`, on
    Windows; `~/.dropbox` everywhere else."""
    if platform != "windows":
        return [home / ".dropbox" / "info.json"]
    roots = (os.environ.get(name) for name in ("APPDATA", "LOCALAPPDATA"))
    return [Path(root) / "Dropbox" / "info.json"
            for root in roots if root and os.path.isabs(root)]


def _dropbox_place(path, home):
    """`(label, folder)` for a folder `info.json` names.

    A folder with Dropbox in its name is its own label. A team space names the
    member's folder inside the team's -- `~/Acme Dropbox/Jane Doe` -- and the
    place a person means is the team's folder around it, which holds the team's
    shared folders too. Anything else is just "Dropbox".
    """
    if "dropbox" in path.name.casefold():
        return path.name, path
    parent = path.parent
    if "dropbox" in parent.name.casefold() and _plain(parent) != _plain(home):
        return parent.name, parent
    return "Dropbox", path


def _cloud_storage_folders(home, platform):
    """macOS's sync folders: `~/Library/CloudStorage/<Provider>-<Account>`.

    OneDrive, Google Drive, Box and Dropbox all keep their folder here on
    current macOS, named without spaces (`OneDrive-ContosoLabs`). OneDrive also
    links it into home under the name it shows (`~/OneDrive - Contoso Labs`),
    and that name wins when there is one; otherwise the folder's own name is
    spelled out. One listing of CloudStorage itself -- the folders in it are
    never opened.
    """
    if platform != "macos":
        return
    root = home / "Library" / "CloudStorage"
    try:
        with os.scandir(root) as scan:
            names = sorted(entry.name for entry in scan
                           if not entry.name.startswith("."))
    except OSError:
        return
    links = _home_links(home, root) if names else {}
    for name in names:
        brand, _, account = name.partition("-")
        provider = _CLOUD_STORAGE_PROVIDERS.get(brand.casefold(), "other")
        folder = root / name
        label = (links.get(_identity(folder))
                 or _cloud_storage_label(brand, provider, account))
        yield label, folder, provider, account or None


def _cloud_storage_label(brand, provider, account):
    """What a CloudStorage folder is called when no link in home names it."""
    if provider == "onedrive":
        return f"OneDrive - {_words(account)}" if account else "OneDrive"
    if provider == "google_drive":
        return "Google Drive"
    if provider == "box":
        return "Box"
    if provider == "dropbox":
        return f"Dropbox ({_words(account)})" if account else "Dropbox"
    return f"{_words(brand)} ({account})" if account else _words(brand)


def _home_links(home, root):
    """`{identity: name}` for the links directly in home that point into `root`.

    One `scandir` of home. A link is read before it is followed, and only one
    whose target is inside `root` is resolved: `realpath` visits every folder
    on the way to a target, and a link to a share whose server has gone would
    hang it.
    """
    inside = {_plain(root), _identity(root)}
    try:
        with os.scandir(home) as scan:
            entries = sorted(scan, key=lambda entry: entry.name)
    except OSError:
        return {}
    links = {}
    for entry in entries:
        try:
            if not entry.is_symlink():
                continue
            target = _plain(os.path.join(home, os.readlink(entry.path)))
        except OSError:
            continue
        if any(target == base or target.startswith(base.rstrip(os.sep) + os.sep)
               for base in inside):
            links.setdefault(_identity(entry.path), entry.name)
    return links


def _icloud_folders(home, platform):
    """iCloud Drive: its fixed folder on macOS, `~/iCloudDrive` on Windows."""
    folder = {"macos": home / "Library" / "Mobile Documents" / "com~apple~CloudDocs",
              "windows": home / "iCloudDrive"}.get(platform)
    if folder is not None:
        yield "iCloud Drive", folder, "icloud", None


def _onedrive_folders(home, platform):
    """OneDrive on Windows, one folder per signed-in account, labelled with its
    own name (`OneDrive - Contoso`); the `~/OneDrive` a Linux client syncs to.
    macOS's is in CloudStorage."""
    if platform == "windows":
        roots = _onedrive_roots(home)
    elif platform == "linux":
        roots = [home / "OneDrive"]
    else:
        return
    for root in roots:
        name = root.name or "OneDrive"
        _, dash, account = name.partition(" - ")
        yield name, root, "onedrive", account if dash else None


def _legacy_cloud_folders(home, platform):
    """`~/Box`, `~/Google Drive` and `~/Dropbox`, wherever they still are."""
    for folder, label, provider in _LEGACY_CLOUD:
        yield label, home / folder, provider, None


# -- the Plexora data folder and a cluster's working roots ---------------------


def project_places():
    """`[{label, path}]`: the data folder, shared roots, then `$SCRATCH` & co.

    The env roots are offered as written, absolute ones only, and never
    stat'ed: on a cluster each is a network filesystem, and one that is slow
    to answer must not hold up the sidebar it would only add a line to.
    """
    from plexora import paths

    found, seen = [], set()

    def add(label, path):
        key = os.path.normcase(os.path.normpath(str(path)))
        if key in seen:
            return
        seen.add(key)
        found.append({"label": label, "path": str(path)})

    try:
        add("Plexora data", paths.data_root())
    except Exception:  # noqa: BLE001 - an unwritable root is still a machine
        pass
    try:
        for resolution in paths.shared_root_resolutions():
            add(f"Shared: {resolution.path.name or resolution.path}",
                resolution.path)
    except Exception:  # noqa: BLE001
        pass
    for name in PROJECT_ENV:
        value = (os.environ.get(name) or "").strip()
        if value and os.path.isabs(value):
            add(f"${name}", value)
    return found


# -- volumes --------------------------------------------------------------------


def volumes(platform=None):
    """`[{label, path, kind, fstype, total, free, source?}]`, best first.

    `kind` is `local`, `removable`, `network` or `remote` (a cloud bucket
    mounted through FUSE). `total` and `free` are bytes, or None for a volume
    that did not say in time.
    """
    platform = platform or _platform()
    reader = {"windows": _windows_volumes,
              "macos": _mac_volumes}.get(platform, _linux_volumes)
    try:
        found = reader()[:VOLUMES_LIMIT]
    except Exception:  # noqa: BLE001 - no volumes is still a sidebar
        found = []
    usage = _usage_many([volume["path"] for volume in found])
    for volume in found:
        measured = usage.get(volume["path"])
        volume["total"], volume["free"] = measured if measured else (None, None)
    return found


# Windows ------------------------------------------------------------------------

DRIVE_UNKNOWN = 0
DRIVE_NO_ROOT_DIR = 1
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_REMOTE = 4
DRIVE_CDROM = 5
DRIVE_RAMDISK = 6
SEM_FAILCRITICALERRORS = 0x0001
ERROR_MORE_DATA = 234
ERROR_CONNECTION_UNAVAIL = 1201

#: `GetDriveTypeW`'s answer -> (kind, the label Explorer uses for a volume
#: with none of its own).
_DRIVE_KINDS = {
    DRIVE_FIXED: ("local", "Local Disk"),
    DRIVE_RAMDISK: ("local", "Local Disk"),
    DRIVE_REMOVABLE: ("removable", "USB Drive"),
    DRIVE_CDROM: ("removable", "CD Drive"),
    DRIVE_REMOTE: ("network", "Network Drive"),
}

#: `MAX_PATH + 1`, the buffer size `GetVolumeInformationW` documents.
_VOLUME_BUFFER = 261


def _windows_volumes():
    r"""Every drive letter, labelled the way Explorer labels it: "Data (D:)".

    A network drive's volume label is not asked for: `GetVolumeInformationW`
    goes to the server, and a mapped drive whose server is gone takes tens of
    seconds to say so. It is named the way Explorer names it instead, from the
    share it is mapped to -- "projects (\\fileserver) (Z:)" -- which this
    machine knows without asking anyone. A removable drive that will not
    answer is an empty card reader or a CD drive with no disc, which Explorer
    hides too. Critical-error dialogs ("There is no disk in the drive") are
    switched off around all of it -- on a server with nobody at the screen, one
    would hang the request.
    """
    import ctypes

    kernel32 = _kernel32()
    mpr = _mpr()
    set_mode = getattr(kernel32, "SetThreadErrorMode", None) if kernel32 else None
    previous = ctypes.c_uint(0)
    if set_mode is not None:
        set_mode(SEM_FAILCRITICALERRORS, ctypes.byref(previous))
    try:
        found = []
        for drive in _windows_drives(kernel32):
            letter = str(drive)[:2].upper()
            root = f"{letter}\\"
            code = kernel32.GetDriveTypeW(root) if kernel32 else DRIVE_FIXED
            if code == DRIVE_NO_ROOT_DIR:
                continue
            kind, default = _DRIVE_KINDS.get(code, ("local", "Local Disk"))
            label, fstype = "", ""
            if kernel32 is not None and code != DRIVE_REMOTE:
                info = _volume_information(kernel32, root)
                if info is None and kind == "removable":
                    continue
                if info is not None:
                    label, fstype = info
            volume = {"label": f"{label or default} ({letter})",
                      "path": root, "kind": kind, "fstype": fstype}
            if code == DRIVE_REMOTE:
                share = _mapped_share(mpr, letter)
                named = _network_drive_label(share, letter) if share else None
                if named:
                    volume["label"], volume["source"] = named, share
            found.append(volume)
        return found
    finally:
        if set_mode is not None:
            set_mode(previous.value, None)


def _mapped_share(mpr, letter):
    r"""The UNC path a mapped drive stands for (`\\fileserver\projects`), or None.

    `WNetGetConnectionW` answers from this logon session's own table of
    mappings and sends nothing to the server, so a drive whose server has gone
    cannot hold it up. A persistent mapping that is not connected yet is named
    too, when the call fills in its share.
    """
    if mpr is None:
        return None
    import ctypes

    size = ctypes.c_ulong(_VOLUME_BUFFER)
    for _ in range(2):
        buffer = ctypes.create_unicode_buffer(size.value)
        try:
            code = mpr.WNetGetConnectionW(letter, buffer, ctypes.byref(size))
        except (AttributeError, OSError):
            return None
        if code == ERROR_MORE_DATA:
            continue
        if code in (0, ERROR_CONNECTION_UNAVAIL) and buffer.value.startswith("\\\\"):
            return buffer.value
        return None
    return None


def _network_drive_label(share, letter):
    r"""Explorer's name for a mapped drive: "projects (\\fileserver) (Z:)".

    The last folder of the UNC path, then the rest of it, then the letter, so
    `\\srv\share\sub` is "sub (\\srv\share) (Z:)". None for a path that names
    no share.
    """
    head, _, name = share.rstrip("\\").rpartition("\\")
    if not name or not head.strip("\\"):
        return None
    return f"{name} ({head}) ({letter})"


def _windows_drives(kernel32):
    """The drive roots, from `os.listdrives` (3.12+) or the logical bitmask."""
    if hasattr(os, "listdrives"):
        try:
            return list(os.listdrives())
        except OSError:
            return []
    if kernel32 is None:
        return []
    mask = kernel32.GetLogicalDrives()
    return [f"{chr(65 + index)}:\\" for index in range(26) if mask & (1 << index)]


def _volume_information(kernel32, root):
    """`(label, fstype)` for one drive, or None when it will not say."""
    import ctypes

    name = ctypes.create_unicode_buffer(_VOLUME_BUFFER)
    fstype = ctypes.create_unicode_buffer(_VOLUME_BUFFER)
    try:
        ok = kernel32.GetVolumeInformationW(root, name, _VOLUME_BUFFER, None,
                                            None, None, fstype, _VOLUME_BUFFER)
    except OSError:
        return None
    if not ok:
        return None
    return name.value, fstype.value


# macOS --------------------------------------------------------------------------

_MAC_MOUNT = re.compile(r"^(?P<source>.+?) on (?P<point>/.*?) \((?P<options>[^()]*)\)\s*$")
#: Network filesystems in `mount`'s output. macFUSE (`macfuse`, and
#: `osxfuse`/`osxfusefs` before it) is what sshfs mounts through. `autofs` is
#: not one: it is the trigger an automount hangs from, and the share mounted
#: under it is listed in its own right.
_MAC_NETWORK = {"smbfs", "nfs", "afpfs", "webdav", "cifs", "fuse-t",
                "macfuse", "osxfuse", "osxfusefs"}
_MAC_REMOVABLE = {"exfat", "msdos", "ntfs", "udf", "cd9660"}


def _mac_mounts(text):
    """`{mount point: (source, fstype)}` from `mount`'s output."""
    table = {}
    for line in (text or "").splitlines():
        match = _MAC_MOUNT.match(line.strip())
        if match:
            fstype = match.group("options").split(",", 1)[0].strip()
            table[match.group("point")] = (match.group("source"), fstype)
    return table


def _mac_kind(source, fstype):
    if fstype in _MAC_NETWORK or source.startswith("//"):
        return "network"
    if fstype in _MAC_REMOVABLE:
        return "removable"
    return "local"


def _mac_volumes():
    """The boot disk as `/`, every volume under `/Volumes` by name, then the
    network shares mounted anywhere else.

    The boot disk appears in `/Volumes` as a link to `/` named whatever the
    user called it; it is offered once, at `/`, under that name. A folder in
    `/Volumes` that `mount` does not list is what an unclean eject leaves
    behind, and is skipped -- unless `mount` said nothing at all, when every
    folder is given the benefit of the doubt.

    A share mounted outside `/Volumes` -- `mount_smbfs` into a folder in home,
    sshfs through macFUSE, NFS under an automount -- comes from the mount table
    alone, `/System`'s own mounts left out. It is never stat'ed: the table
    already says it is there, and a share whose server has gone would hang the
    call that touched it.
    """
    root = _volumes_root()
    table = _mac_mounts(_mount_output())
    boot, found = None, []
    try:
        with os.scandir(root) as scan:
            entries = sorted(scan, key=lambda entry: entry.name.lower())
    except OSError:
        entries = []
    for entry in entries:
        name = entry.name
        if name.startswith(".") or name.startswith("com.apple."):
            continue
        try:
            if entry.is_symlink():
                if boot is None and os.path.realpath(entry.path) == "/":
                    boot = name
                continue
            if not entry.is_dir():
                continue
        except OSError:
            continue
        if table and entry.path not in table:
            continue
        source, fstype = table.get(entry.path, ("", ""))
        volume = {"label": name, "path": entry.path,
                  "kind": _mac_kind(source, fstype), "fstype": fstype}
        if volume["kind"] == "network" and source:
            volume["source"] = source
        found.append(volume)
    elsewhere = sorted(
        ({"label": os.path.basename(point.rstrip("/")) or point, "path": point,
          "kind": "network", "fstype": fstype, "source": source}
         for point, (source, fstype) in table.items()
         if fstype in _MAC_NETWORK and _outside_volumes(point, root)),
        key=lambda volume: (volume["label"].lower(), volume["path"]))
    return [{"label": boot or "Macintosh HD", "path": "/", "kind": "local",
             "fstype": table.get("/", ("", ""))[1]}, *found, *elsewhere]


def _outside_volumes(point, root):
    """Not `/`, not in `/Volumes` (listed already) and not under `/System`."""
    if point == "/":
        return False
    return not any(point == base or point.startswith(base + "/")
                   for base in (str(root).rstrip("/"), "/Volumes", "/System"))


# Linux and clusters --------------------------------------------------------------

#: Filesystems that hold somebody's files. Everything else in a mount table --
#: proc, sysfs, cgroup, tmpfs, overlay, squashfs -- is the system's.
_LINUX_FSTYPES = {
    "xfs", "btrfs", "zfs", "f2fs", "vfat", "exfat", "fuseblk", "iso9660", "udf",
    "lustre", "gpfs", "beegfs", "cifs", "smb3", "smbfs", "ceph", "glusterfs",
    "panfs", "wekafs", "ocfs2", "autofs",
}
_LINUX_FSTYPE_PREFIXES = ("ext", "ntfs", "nfs")
#: `fuse.*` filesystems that are the desktop's plumbing, not a place.
_FUSE_PLUMBING = ("portal", "gvfsd", "snapfuse", "lxcfs", "xdg-document-portal")
#: Shared filesystems, reached over a network.
_NETWORK_FSTYPES = {"lustre", "gpfs", "beegfs", "cifs", "smb3", "smbfs", "ceph",
                    "glusterfs", "panfs", "wekafs", "ocfs2", "autofs",
                    "fuse.sshfs", "fuse.glusterfs", "fuse.ceph"}
#: Cloud buckets mounted through FUSE.
_REMOTE_FUSE = ("rclone", "s3fs", "gcsfuse", "goofys", "blobfuse", "mountpoint-s3")
#: Mount points under these are the system's own.
_EXCLUDED_PREFIXES = ("/proc", "/sys", "/dev", "/run", "/boot", "/snap", "/etc",
                      "/var/lib/docker", "/var/lib/snapd", "/var/snap",
                      "/var/lib/containers")
_RUN_MEDIA = "/run/media"


def _unescape(text):
    """A mount table's octal escapes (`\\040` is a space) decoded."""
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), text)


def _parse_mount_line(line):
    """`(device, root, point, fstype, source)` for one line, or None.

    Either format: `mountinfo` (with `major:minor` and the ` - ` separator) or
    `/proc/mounts` (no device; dedupe falls back to the source).
    """
    fields = line.split()
    if " - " in line and len(fields) >= 5 and re.fullmatch(r"\d+:\d+", fields[2]):
        left, _, right = line.partition(" - ")
        before, after = left.split(), right.split()
        if len(before) < 5 or not after:
            return None
        return (before[2], _unescape(before[3]), _unescape(before[4]), after[0],
                _unescape(after[1]) if len(after) > 1 else "")
    if len(fields) >= 3:
        return "", "", _unescape(fields[1]), fields[2], _unescape(fields[0])
    return None


def _wanted_fstype(fstype):
    if fstype in _LINUX_FSTYPES or fstype.startswith(_LINUX_FSTYPE_PREFIXES):
        return True
    if fstype.startswith("fuse."):
        return not fstype[5:].startswith(_FUSE_PLUMBING)
    return False


def _excluded(point):
    if point == _RUN_MEDIA or point.startswith(_RUN_MEDIA + "/"):
        return False
    return any(point == prefix or point.startswith(prefix + "/")
               for prefix in _EXCLUDED_PREFIXES)


def _depth(point):
    return len([part for part in point.split("/") if part])


def _linux_kind(point, fstype):
    if fstype.startswith("fuse.") and fstype[5:].startswith(_REMOTE_FUSE):
        return "remote"
    if fstype in _NETWORK_FSTYPES or fstype.startswith("nfs"):
        return "network"
    if (point == "/media" or point.startswith("/media/") or point == _RUN_MEDIA
            or point.startswith(_RUN_MEDIA + "/") or fstype in ("iso9660", "udf")):
        return "removable"
    return "local"


def _linux_volumes():
    """The mount table, cut down to the places a person browses, ranked.

    Bind mounts of one filesystem collapse to the shortest mount point
    (same `major:minor`); a network filesystem is told apart by its root too,
    since two exports of one server can share a superblock. A mount inside an
    automounted root is left out: the root is the place, and what has been
    automounted under it is only what was touched lately. `/` is always
    offered -- a container's root is an overlay, and still where its files are.
    """
    hints = [value for value in (os.environ.get(name) for name in PROJECT_ENV)
             if value and os.path.isabs(value)]
    best, root_fstype, automounts = {}, "", []
    for line in _read_mountinfo().splitlines():
        parsed = _parse_mount_line(line)
        if parsed is None:
            continue
        device, fs_root, point, fstype, source = parsed
        if point == "/":
            root_fstype = root_fstype or fstype
        if not _wanted_fstype(fstype) or _excluded(point):
            continue
        if _depth(point) > MAX_MOUNT_DEPTH:
            continue
        if fstype == "autofs":
            automounts.append(point)
        kind = _linux_kind(point, fstype)
        if device:
            key = (device, fs_root) if kind in ("network", "remote") else (device,)
        else:
            key = (source,) if source not in ("", "none") else (point,)
        volume = {"label": os.path.basename(point.rstrip("/")) or "Root",
                  "path": point, "kind": kind, "fstype": fstype}
        if source and source != "none":
            volume["source"] = source
        held = best.get(key)
        if held is None or (_depth(point), len(point)) < (
                _depth(held["path"]), len(held["path"])):
            best[key] = volume

    found = [volume for volume in best.values()
             if not any(volume["path"] != auto and
                        volume["path"].startswith(auto.rstrip("/") + "/")
                        for auto in automounts)]
    if not any(volume["path"] == "/" for volume in found):
        found.append({"label": "Root", "path": "/", "kind": "local",
                      "fstype": root_fstype})
    found.sort(key=lambda volume: _rank(volume["path"], hints))
    return found


def _rank(point, hints):
    """`/` first, then mounts holding an env root, then `PRIORITY_ROOTS`."""
    if point == "/":
        return (0, 0, 0, point)
    prefix = point.rstrip("/") + "/"
    hinted = any(hint == point or hint.startswith(prefix) for hint in hints)
    first = point.strip("/").split("/", 1)[0]
    priority = (PRIORITY_ROOTS.index(first) if first in PRIORITY_ROOTS
                else len(PRIORITY_ROOTS))
    return (1 if hinted else 2, priority, _depth(point), point)


# Free space --------------------------------------------------------------------

_usage_lock = threading.Lock()
#: path -> (expires at, (total, free) or None)
_usage_memo = {}
#: Paths with a measuring thread still running: never two at once for one.
_usage_running = set()


def _usage_many(paths):
    """`{path: (total, free) or None}`, never waiting long for any of them.

    One daemon thread per path, because `shutil.disk_usage` on a network mount
    whose server has gone blocks in the kernel and cannot be interrupted. Each
    is waited on for at most `USAGE_EACH_S`, all of them for `USAGE_TOTAL_S`;
    one that is still running then answers None, is not asked again for
    `USAGE_SLOW_RETRY_S`, and its thread is left to finish (or not) on its own
    -- and while it runs, no second thread is started for the same path.
    """
    began = _now()
    answers, waiting = {}, []
    with _usage_lock:
        for path in dict.fromkeys(paths):
            memo = _usage_memo.get(path)
            if memo is not None and memo[0] > began:
                answers[path] = memo[1]
                continue
            if path in _usage_running:
                answers[path] = None
                continue
            _usage_running.add(path)
            box = {}
            waiting.append((path, box, threading.Thread(
                target=_measure, args=(path, box), daemon=True,
                name="plexora-disk-usage")))
    for _, _, thread in waiting:
        thread.start()
    for path, box, thread in waiting:
        thread.join(max(0.0, min(USAGE_EACH_S, began + USAGE_TOTAL_S - _now())))
        if "value" in box:
            answers[path] = box["value"]
            continue
        answers[path] = None
        with _usage_lock:
            if path in _usage_running:
                _usage_memo[path] = (_now() + USAGE_SLOW_RETRY_S, None)
    return answers


def _measure(path, box):
    """One `disk_usage`, recorded for `_usage_many` whenever it finishes."""
    try:
        usage = shutil.disk_usage(path)
        value = (int(usage.total), int(usage.free))
    except Exception:  # noqa: BLE001 - no caption, never a failure
        value = None
    with _usage_lock:
        _usage_running.discard(path)
        _usage_memo[path] = (_now() + USAGE_MEMO_S, value)
    box["value"] = value
