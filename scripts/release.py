#!/usr/bin/env python3
"""Build, version and package Plexora: the wheel and the desktop app.

One script for every release step, stdlib only, so it runs from any Python
3.10+ before anything is installed. It shells out to `uv`, `npm` and the
Tauri CLI; `doctor` says exactly what is missing and how to install it.

    python scripts/release.py doctor
    python scripts/release.py bump patch --commit --tag
    python scripts/release.py all --dev          # wheel + runtime + installer + validate
    python scripts/release.py ci --bump patch    # tag, let GitHub build all three OSes

Desktop installers cannot be cross-built (NSIS, DMG and AppImage each need
their own OS), so `all` builds for the machine it runs on and `ci` drives the
GitHub Actions matrix for the rest.

The version lives in one place, `pyproject.toml`; `propagate` copies it into
the Tauri config, Cargo.toml and the client's package.json, and `propagate
--check` refuses to build when they disagree.

Signing is off until its environment variables exist (see SIGNING below and
docs in DEPLOYMENT.md). Nothing here changes shape when they appear.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
DESKTOP = ROOT / "desktop"
TAURI_DIR = DESKTOP / "src-tauri"
TAURI_CONF = TAURI_DIR / "tauri.conf.json"
CARGO_TOML = TAURI_DIR / "Cargo.toml"
CLIENT = ROOT / "plexora" / "client"
CLIENT_PACKAGE = CLIENT / "package.json"
VENDOR_BUNDLE = CLIENT / "dist" / "vendor_bundle.js"

PRODUCT = "Plexora"
BINARY = "plexora-desktop"

# -- the embedded interpreter ----------------------------------------------

#: python-build-standalone release and interpreter, pinned together. Bumping
#: either means updating PBS_SHA256 from that release's SHA256SUMS.
PBS_TAG = "20260924"
PBS_PYTHON = "3.13.15"
PBS_FLAVOR = "install_only_stripped"
PBS_URL = ("https://github.com/astral-sh/python-build-standalone/releases/"
           "download/{tag}/{name}")
PBS_SHA256 = {
    "aarch64-apple-darwin":
        "064afb7c2fc0bbf511d886288adf98696af5105e36c138cdf2c199c0146fcf68",
    "x86_64-apple-darwin":
        "327814efd865a0b6a99c149b12a261e9d0ad409183515c745d41bda2d07282e9",
    "x86_64-pc-windows-msvc":
        "e42fa944748a50e9ff481cbb817ef8a6e3da6fbcf0cf6f29b554e1acb8c7384d",
    "x86_64-unknown-linux-gnu":
        "d0b640eed27fbdd6f5f2bd33444aee53df2c8863f8b2a96f4094717411e3de9c",
}

#: Extras the app ships with. Not `jupyter` (a desktop app is not a notebook
#: server) and not `dev`.
RUNTIME_EXTRAS = ("spatial", "wsi", "remote")

#: Imported from the relocated runtime before it is trusted: every heavy
#: binary dependency, so a wheel with a hard-coded path fails here rather than
#: on somebody's laptop.
RUNTIME_IMPORTS = (
    "plexora", "plexora.cli", "numpy", "scipy", "polars", "cv2", "sklearn",
    "shapely", "spatialdata", "zarr", "tifffile", "imagecodecs", "openslide",
    "pydicom", "wsidicom", "pyarrow", "gcsfs", "adlfs", "waitress", "flask",
)

#: Friendly platform word per target triple, used in artifact names.
TARGETS = {
    "x86_64-pc-windows-msvc": ("windows", "x64"),
    "aarch64-apple-darwin": ("macos", "arm64"),
    "x86_64-apple-darwin": ("macos", "x64"),
    "x86_64-unknown-linux-gnu": ("linux", "x64"),
    # A Windows shell linked with llvm-mingw instead of MSVC, for a machine
    # without the Visual Studio C++ workload (it needs admin rights to add).
    # Same installer and the same (MSVC-built) Python runtime; CI uses MSVC.
    "x86_64-pc-windows-gnullvm": ("windows", "x64"),
}

#: The interpreter each shell target ships. The same unless the shell's own
#: toolchain differs from the one Python was built with.
RUNTIME_TRIPLE = {"x86_64-pc-windows-gnullvm": "x86_64-pc-windows-msvc"}

#: Which Tauri bundles each OS produces.
BUNDLES = {"windows": ["nsis"], "macos": ["app", "dmg"], "linux": ["appimage", "deb"]}

#: `--smoke-test` exit codes from the shell (desktop/src-tauri/src/smoke.rs).
SMOKE_CODES = {0: "ok", 3: "runtime not found", 4: "no ready line in time",
               5: "health check failed", 6: "server did not exit when asked"}

# -- signing ----------------------------------------------------------------
#
# Every signing input is an environment variable, so turning signing on is
# adding secrets to the repository -- no file here changes.
#
#   APPLE_SIGNING_IDENTITY        Developer ID Application; absent = ad-hoc
#   APPLE_CERTIFICATE(+_PASSWORD) .p12 for CI keychains (read by Tauri)
#   APPLE_ID, APPLE_PASSWORD, APPLE_TEAM_ID       notarization (Tauri)
#   APPLE_API_ISSUER, APPLE_API_KEY, APPLE_API_KEY_PATH   (alternative)
#   PLEXORA_WIN_SIGN_COMMAND      e.g. "signtool sign /fd sha256 ... %1"
#   PLEXORA_WIN_CERT_THUMBPRINT, PLEXORA_WIN_TIMESTAMP_URL, PLEXORA_WIN_DIGEST

ENTITLEMENTS = TAURI_DIR / "entitlements.plist"


class StepError(RuntimeError):
    """A step failed in a way with something to do about it."""

    def __init__(self, message, hint=""):
        super().__init__(message)
        self.hint = hint


# -- context ---------------------------------------------------------------


def host_triple() -> str:
    machine = platform.machine().lower()
    arm = machine in ("arm64", "aarch64")
    if sys.platform == "win32":
        return "x86_64-pc-windows-msvc"
    if sys.platform == "darwin":
        return "aarch64-apple-darwin" if arm else "x86_64-apple-darwin"
    if sys.platform.startswith("linux"):
        if arm:
            raise StepError("Linux on ARM is not a release target yet.")
        return "x86_64-unknown-linux-gnu"
    raise StepError(f"No desktop target for {sys.platform}.")


def _in_synced_folder(path: Path) -> bool:
    text = str(path).lower()
    return any(word in text for word in ("dropbox", "onedrive", "icloud", "google drive"))


def default_build_dir() -> Path:
    """Where the heavy build products go.

    Outside the repository when the repository is in a synced folder: the
    runtime is 50,000 files and a gigabyte, the Rust target directory more,
    and a sync client re-uploading them on every build is slow at best and
    breaks hard links (uv's default) at worst.
    """
    if os.environ.get("PLEXORA_BUILD_DIR"):
        return Path(os.environ["PLEXORA_BUILD_DIR"]).expanduser()
    if not _in_synced_folder(ROOT):
        return ROOT / "build"
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "plexora-release"


@dataclass
class Ctx:
    dev: bool = True
    target: str = ""
    build_dir: Path = field(default_factory=default_build_dir)
    runtime_dir: Path | None = None
    release_dir: Path | None = None
    clean: bool = True
    skip_tests: bool = False
    skip_client: bool = False
    skip_wheel: bool = False
    force: bool = False
    yes: bool = False
    verbose: bool = False
    dry_run: bool = False

    def __post_init__(self):
        self.target = self.target or host_triple()
        if self.target not in TARGETS:
            raise StepError(f"Unknown target {self.target!r}; expected one of "
                            f"{', '.join(TARGETS)}.")
        if self.runtime_dir is None:
            env = os.environ.get("PLEXORA_RUNTIME_DIR")
            self.runtime_dir = (Path(env).expanduser() if env
                                else self.build_dir / "runtime" / self.runtime_triple)
        if self.release_dir is None:
            self.release_dir = (ROOT / "release" if not _in_synced_folder(ROOT)
                                else self.build_dir / "release")

    @property
    def runtime_triple(self):
        return RUNTIME_TRIPLE.get(self.target, self.target)

    @property
    def os_word(self):
        return TARGETS[self.target][0]

    @property
    def arch_word(self):
        return TARGETS[self.target][1]

    @property
    def cargo_target_dir(self) -> Path:
        return Path(os.environ.get("CARGO_TARGET_DIR") or self.build_dir / "target")

    @property
    def dist_dir(self) -> Path:
        return self.build_dir / "dist"

    @property
    def runtime_python(self) -> Path:
        if self.os_word == "windows":
            return self.runtime_dir / "python.exe"
        return self.runtime_dir / "bin" / "python3"


def say(message=""):
    print(message, flush=True)


def step(title):
    say(f"\n== {title}")


def run(cmd, *, cwd=ROOT, env=None, check=True, capture=False, ctx=None,
        timeout=None):
    """Run one command, echoing it. Returns CompletedProcess."""
    cmd = [str(part) for part in cmd]
    shown = " ".join(f'"{part}"' if " " in part else part for part in cmd)
    say(f"$ {shown}")
    if ctx is not None and ctx.dry_run:
        return subprocess.CompletedProcess(cmd, 0, "", "")
    merged = dict(os.environ)
    if env:
        merged.update(env)
    try:
        done = subprocess.run(cmd, cwd=cwd, env=merged, text=True,
                              capture_output=capture, timeout=timeout,
                              encoding="utf-8" if capture else None,
                              errors="replace" if capture else None)
    except FileNotFoundError:
        raise StepError(f"{cmd[0]} is not installed or not on PATH.",
                        "Run `python scripts/release.py doctor`.") from None
    if check and done.returncode != 0:
        detail = ""
        if capture:
            detail = "\n" + ((done.stdout or "") + (done.stderr or ""))[-4000:]
        raise StepError(f"{cmd[0]} exited with {done.returncode}.{detail}")
    return done


def which(name) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    cargo_bin = Path.home() / ".cargo" / "bin" / (name + (".exe" if os.name == "nt" else ""))
    return str(cargo_bin) if cargo_bin.exists() else None


def uv_env():
    # A cloud-filter driver (Dropbox, OneDrive) refuses uv's hard links with
    # os error 396; copying always works.
    return {"UV_LINK_MODE": "copy", "UV_NO_PROGRESS": "1"}


# -- version -----------------------------------------------------------------

_VERSION_LINE = re.compile(r'^(version\s*=\s*")([^"]+)(")', re.MULTILINE)
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def read_version() -> str:
    text = PYPROJECT.read_text(encoding="utf-8")
    project = text.split("[project]", 1)[1]
    match = _VERSION_LINE.search(project)
    if not match:
        raise StepError("No version in pyproject.toml's [project] table.")
    return match.group(2)


def git_sha(short=7) -> str:
    try:
        done = subprocess.run(["git", "rev-parse", f"--short={short}", "HEAD"],
                              cwd=ROOT, capture_output=True, text=True)
        return done.stdout.strip() or "nogit"
    except FileNotFoundError:
        return "nogit"


def artifact_version(ctx: Ctx) -> str:
    """The version in file names: `0.1.0`, or `0.1.0-dev+g1a2b3c4`."""
    version = read_version()
    return f"{version}-dev+g{git_sha()}" if ctx.dev else version


def next_version(current: str, spec: str) -> str:
    if _SEMVER.match(spec):
        return spec
    if not _SEMVER.match(current):
        raise StepError(f"Current version {current!r} is not X.Y.Z.")
    major, minor, patch = (int(part) for part in current.split("."))
    if spec == "major":
        return f"{major + 1}.0.0"
    if spec == "minor":
        return f"{major}.{minor + 1}.0"
    if spec == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise StepError(f"Bump must be major, minor, patch or X.Y.Z, not {spec!r}.")


def _write_text_lf(path: Path, text: str):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _set_pyproject_version(version):
    text = PYPROJECT.read_text(encoding="utf-8")
    head, project = text.split("[project]", 1)
    project = _VERSION_LINE.sub(lambda m: f"{m.group(1)}{version}{m.group(3)}",
                                project, count=1)
    _write_text_lf(PYPROJECT, head + "[project]" + project)


def _propagation_targets():
    """`(path, reader, writer)` for every file that carries the version."""
    def json_reader(path):
        return json.loads(path.read_text(encoding="utf-8")).get("version")

    def json_writer(path, version):
        text = path.read_text(encoding="utf-8")
        new = re.sub(r'("version"\s*:\s*")[^"]*(")', rf"\g<1>{version}\g<2>",
                     text, count=1)
        _write_text_lf(path, new)

    def cargo_reader(path):
        package = path.read_text(encoding="utf-8").split("[package]", 1)[1]
        match = _VERSION_LINE.search(package.split("\n[", 1)[0])
        return match.group(2) if match else None

    def cargo_writer(path, version):
        text = path.read_text(encoding="utf-8")
        head, package = text.split("[package]", 1)
        package = _VERSION_LINE.sub(lambda m: f"{m.group(1)}{version}{m.group(3)}",
                                    package, count=1)
        _write_text_lf(path, head + "[package]" + package)

    return [
        (TAURI_CONF, json_reader, json_writer),
        (CARGO_TOML, cargo_reader, cargo_writer),
        (CLIENT_PACKAGE, json_reader, json_writer),
        (DESKTOP / "package.json", json_reader, json_writer),
    ]


def propagate(check=False, ctx=None) -> list[str]:
    """Copy pyproject's version everywhere; with check, only report."""
    version = read_version()
    stale = []
    for path, reader, writer in _propagation_targets():
        if not path.exists():
            continue
        current = reader(path)
        if current == version:
            continue
        stale.append(f"{path.relative_to(ROOT)}: {current} (want {version})")
        if not check and not (ctx and ctx.dry_run):
            writer(path, version)
    if not check and stale and CARGO_TOML.exists() and which("cargo"):
        lock = TAURI_DIR / "Cargo.lock"
        if lock.exists():
            run([which("cargo"), "update", "-w", "--offline"], cwd=TAURI_DIR,
                check=False, ctx=ctx)
    return stale


def cmd_propagate(ctx, args):
    stale = propagate(check=args.check, ctx=ctx)
    if args.check:
        if stale:
            raise StepError("Version out of step:\n  " + "\n  ".join(stale),
                            "Run `python scripts/release.py propagate`.")
        say(f"Every file says {read_version()}.")
    else:
        say("\n".join(f"updated {line}" for line in stale) or
            f"Already {read_version()} everywhere.")


def _git_dirty_files():
    """Tracked files with changes. Untracked files never reach a release commit
    or a tag, so a stray scratch file is not a reason to refuse one."""
    done = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                          cwd=ROOT, capture_output=True, text=True)
    return [line[3:] for line in done.stdout.splitlines() if line.strip()]


def cmd_bump(ctx, args):
    current = read_version()
    version = next_version(current, args.spec)
    if version == current:
        raise StepError(f"Already {current}.")
    before = set(_git_dirty_files())
    step(f"Bump {current} -> {version}")
    if ctx.dry_run:
        say("(dry run)")
        return version
    _set_pyproject_version(version)
    propagate(ctx=ctx)
    run(["uv", "lock"], env=uv_env())
    expected = {"pyproject.toml", "uv.lock",
                str(TAURI_CONF.relative_to(ROOT)).replace("\\", "/"),
                str(CARGO_TOML.relative_to(ROOT)).replace("\\", "/"),
                str((TAURI_DIR / "Cargo.lock").relative_to(ROOT)).replace("\\", "/"),
                str(CLIENT_PACKAGE.relative_to(ROOT)).replace("\\", "/"),
                "desktop/package.json"}
    changed = set(_git_dirty_files()) - before
    surprise = sorted(path for path in changed if path not in expected)
    if surprise:
        raise StepError("The bump touched files it should not have:\n  "
                        + "\n  ".join(surprise))
    say(f"Now {version}.")
    if args.commit or args.tag:
        files = sorted(path for path in expected if (ROOT / path).exists())
        run(["git", "add", *files])
        run(["git", "commit", "-m", f"Release {version}"])
    if args.tag:
        run(["git", "tag", "-a", f"v{version}", "-m", f"Plexora {version}"])
    return version


# -- doctor ------------------------------------------------------------------


def _tool_version(cmd):
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return (done.stdout or done.stderr).strip().splitlines()[0]
    except Exception:
        return None


def cmd_doctor(ctx, args):
    """What is installed, what is missing, and the line that installs it."""
    ok = True
    rows = []

    def check(name, cmd, install, required=True):
        nonlocal ok
        resolved = which(cmd[0])
        found = _tool_version([resolved, *cmd[1:]]) if resolved else None
        if not found and required:
            ok = False
        rows.append((name, found or "MISSING", "" if found else install))

    check("python", [sys.executable, "--version"], "")
    check("git", ["git", "--version"], "https://git-scm.com/downloads")
    check("uv", ["uv", "--version"],
          "curl -LsSf https://astral.sh/uv/install.sh | sh   (Windows: winget install astral-sh.uv)")
    check("node", ["node", "--version"], "https://nodejs.org (LTS)")
    check("npm", ["npm", "--version"], "comes with node")
    check("cargo", [which("cargo") or "cargo", "--version"],
          "https://rustup.rs  (rustup-init -y --profile minimal)")
    check("rustc", [which("rustc") or "rustc", "--version"], "comes with rustup")
    check("gh", ["gh", "--version"], "https://cli.github.com (only for `ci`)",
          required=False)
    if ctx.os_word == "macos":
        check("codesign", ["codesign", "--help"], "xcode-select --install")
        check("hdiutil", ["hdiutil", "help"], "part of macOS")
    if ctx.os_word == "linux":
        check("patchelf", ["patchelf", "--version"], "apt install patchelf")
        pkg = _tool_version(["pkg-config", "--modversion", "webkit2gtk-4.1"])
        rows.append(("webkit2gtk-4.1", pkg or "MISSING", "" if pkg else
                     "sudo apt-get install -y libwebkit2gtk-4.1-dev "
                     "libappindicator3-dev librsvg2-dev patchelf xdg-utils libfuse2"))
        ok = ok and bool(pkg)
    if ctx.os_word == "windows":
        webview = _webview2_version()
        rows.append(("WebView2", webview or "MISSING", "" if webview else
                     "https://developer.microsoft.com/microsoft-edge/webview2/"))
        link = _msvc_linker()
        rows.append(("MSVC linker", link or "MISSING", "" if link else
                     "Visual Studio Build Tools with 'Desktop development with C++'"))
        ok = ok and bool(link)

    tauri_cli = DESKTOP / "node_modules" / "@tauri-apps" / "cli"
    rows.append(("tauri-cli", "installed" if tauri_cli.exists() else "not yet",
                 "" if tauri_cli.exists() else "npm ci --prefix desktop (release.py does it)"))

    width = max(len(row[0]) for row in rows)
    for name, found, install in rows:
        say(f"  {name:<{width}}  {found}")
        if install:
            say(f"  {'':<{width}}    -> {install}")

    say("")
    say(f"  version       {read_version()}")
    say(f"  target        {ctx.target}")
    say(f"  build dir     {ctx.build_dir}")
    say(f"  runtime dir   {ctx.runtime_dir}")
    say(f"  release dir   {ctx.release_dir}")
    stale = propagate(check=True)
    if stale:
        say("  version drift " + "; ".join(stale))
    if _in_synced_folder(ROOT):
        say("  note          the repository is in a synced folder; build products "
            "are kept outside it (PLEXORA_BUILD_DIR overrides).")
    signing = _signing_state(ctx)
    say(f"  signing       {signing}")
    if args.dropbox_ignore:
        _dropbox_ignore([ROOT / "release", ROOT / "build",
                         TAURI_DIR / "target", TAURI_DIR / "gen", DESKTOP / "node_modules"])
    if not ok and not args.ci:
        raise StepError("Some required tools are missing (above).")
    if not ok:
        raise StepError("Toolchain incomplete.")


def _webview2_version():
    if os.name != "nt":
        return None
    try:
        import winreg
    except ImportError:
        return None
    key = r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for path in (key, key.replace("WOW6432Node\\", "")):
            try:
                with winreg.OpenKey(hive, path) as handle:
                    return winreg.QueryValueEx(handle, "pv")[0]
            except OSError:
                continue
    return None


def _msvc_linker():
    vswhere = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) \
        / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if not vswhere.exists():
        return None
    done = subprocess.run([str(vswhere), "-latest", "-products", "*", "-requires",
                           "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                           "-property", "displayName"], capture_output=True, text=True)
    return done.stdout.strip() or None


def _dropbox_ignore(paths):
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            stream = f"{path}:com.dropbox.ignored"
            try:
                with open(stream, "w") as handle:
                    handle.write("1")
                say(f"  dropbox-ignored {path}")
            except OSError as exc:
                say(f"  could not mark {path}: {exc}")
        elif sys.platform == "darwin":
            run(["xattr", "-w", "com.dropbox.ignored", "1", path], check=False)
        else:
            run(["attr", "-s", "com.dropbox.ignored", "-V", "1", path], check=False)


def _signing_state(ctx):
    if ctx.os_word == "macos":
        identity = os.environ.get("APPLE_SIGNING_IDENTITY")
        notarize = bool(os.environ.get("APPLE_ID") or os.environ.get("APPLE_API_KEY"))
        return (f"Developer ID {identity!r}" + (" + notarization" if notarize else "")
                if identity else "ad-hoc (unsigned for distribution)")
    if ctx.os_word == "windows":
        if os.environ.get("PLEXORA_WIN_SIGN_COMMAND"):
            return "Authenticode via PLEXORA_WIN_SIGN_COMMAND"
        if os.environ.get("PLEXORA_WIN_CERT_THUMBPRINT"):
            return "Authenticode via certificate thumbprint"
        return "unsigned"
    return "not applicable"


# -- client and wheel --------------------------------------------------------


def verify_client_bundle():
    if not VENDOR_BUNDLE.exists():
        raise StepError(f"{VENDOR_BUNDLE.relative_to(ROOT)} is missing.",
                        "cd plexora/client && npm ci && npm run build")
    size = VENDOR_BUNDLE.stat().st_size
    if size > 5 * 1024 * 1024:
        raise StepError(f"vendor_bundle.js is {size / 1e6:.1f} MB -- a development "
                        f"build, not a production one.",
                        "cd plexora/client && npm run build   (never `npm run start`)")
    head = VENDOR_BUNDLE.read_text(encoding="utf-8", errors="replace")[:200000]
    if "eval(" in head and "webpackBootstrap" in head:
        raise StepError("vendor_bundle.js is an eval-mode development build.",
                        "cd plexora/client && npm run build")


def cmd_client(ctx, args):
    step("Client bundle")
    if args.verify_only or ctx.skip_client:
        verify_client_bundle()
        say("Committed bundle is a production build.")
        return
    npm = which("npm")
    if not (CLIENT / "node_modules").exists():
        run([npm, "ci"], cwd=CLIENT, ctx=ctx)
    run([npm, "run", "build"], cwd=CLIENT, ctx=ctx)
    verify_client_bundle()
    changed = [path for path in _git_dirty_files() if path.startswith("plexora/client/dist")]
    if changed:
        say("Rebuilt bundle differs from the committed one; commit it:\n  "
            + "\n  ".join(changed))


def verify_wheel(path: Path):
    """The wheel holds the server, the client and the plugins -- and not
    node_modules. Namespace discovery (pyproject.toml) is what fails silently
    otherwise."""
    with zipfile.ZipFile(path) as wheel:
        names = wheel.namelist()
    required = [
        "plexora/cli.py",
        "plexora/_lifetime.py",
        "plexora/server/routes/desktop_routes.py",
        "plexora/server/routes/page_routes.py",
        "plexora/client/dist/vendor_bundle.js",
        "plexora/client/src/js/services/desktopBridge.js",
        "plexora/client/templates/base.html",
    ]
    missing = [name for name in required if name not in names]
    if not any("/static/" in name and name.startswith("plexora/plugins/") for name in names):
        missing.append("plexora/plugins/*/static/")
    entry = next((name for name in names if name.endswith("entry_points.txt")), None)
    if entry is None:
        missing.append("*.dist-info/entry_points.txt")
    else:
        with zipfile.ZipFile(path) as wheel:
            if "[plexora.plugins]" not in wheel.read(entry).decode():
                missing.append("[plexora.plugins] entry points")
    leaked = [name for name in names if "/node_modules/" in name]
    if missing or leaked:
        raise StepError("The wheel is incomplete:\n  "
                        + "\n  ".join(missing + [f"leaked: {n}" for n in leaked[:5]]))


def find_wheel(ctx) -> Path:
    version = read_version()
    wheels = sorted(ctx.dist_dir.glob(f"plexora-{version}-*.whl"))
    if not wheels:
        raise StepError(f"No plexora {version} wheel in {ctx.dist_dir}.",
                        "Run `python scripts/release.py wheel`.")
    return wheels[-1]


def cmd_wheel(ctx, args=None):
    step("Wheel and sdist")
    if ctx.dist_dir.exists() and ctx.clean:
        shutil.rmtree(ctx.dist_dir)
    ctx.dist_dir.mkdir(parents=True, exist_ok=True)
    run(["uv", "build", "--out-dir", ctx.dist_dir], env=uv_env(), ctx=ctx)
    # setuptools stages the sdist in the repository and deletes it after; a
    # sync client holding one of its files open leaves it behind.
    for leftover in (ROOT / f"plexora-{read_version()}", ROOT / "build" / "lib"):
        if leftover.is_dir():
            _rmtree(leftover)
    if ctx.dry_run:
        return
    wheel = find_wheel(ctx)
    verify_wheel(wheel)
    say(f"{wheel.name}: {wheel.stat().st_size / 1e6:.1f} MB, contents verified.")


def cmd_tests(ctx, args=None):
    step("Tests")
    run([sys.executable, "-m", "pytest", "-q", "-p", "no:randomly"], ctx=ctx)


# -- runtime -------------------------------------------------------------------


def pbs_asset_name(target) -> str:
    return f"cpython-{PBS_PYTHON}+{PBS_TAG}-{target}-{PBS_FLAVOR}.tar.gz"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_runtime(ctx) -> Path:
    """The pinned interpreter tarball, downloaded once and verified always."""
    name = pbs_asset_name(ctx.runtime_triple)
    cache = Path(os.environ.get("PLEXORA_PBS_CACHE") or
                 Path.home() / ".cache" / "plexora-release" / "pbs")
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / name
    expected = PBS_SHA256[ctx.runtime_triple]
    if path.exists() and _sha256(path) == expected:
        say(f"cached {path}")
        return path
    url = PBS_URL.format(tag=PBS_TAG, name=name.replace("+", "%2B"))
    say(f"downloading {url}")
    if ctx.dry_run:
        return path
    partial = path.with_suffix(".part")
    with urllib.request.urlopen(url, timeout=120) as response, open(partial, "wb") as out:
        shutil.copyfileobj(response, out, 1 << 20)
    actual = _sha256(partial)
    if actual != expected:
        partial.unlink()
        raise StepError(f"{name}: sha256 {actual} is not the pinned {expected}.")
    partial.replace(path)
    return path


def _rmtree(path: Path):
    def onerror(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    if path.exists():
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=onerror)
        else:
            shutil.rmtree(path, onerror=onerror)


def unpack_runtime(ctx, tarball: Path):
    if ctx.runtime_dir.exists():
        _rmtree(ctx.runtime_dir)
    staging = ctx.runtime_dir.parent / (ctx.runtime_dir.name + ".unpack")
    _rmtree(staging)
    staging.mkdir(parents=True)
    with tarfile.open(tarball) as archive:
        if sys.version_info >= (3, 12):
            archive.extractall(staging, filter="tar")
        else:
            archive.extractall(staging)
    (staging / "python").rename(ctx.runtime_dir)
    staging.rmdir()


def export_lock(ctx) -> Path:
    out = ctx.build_dir / "requirements-desktop.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["uv", "export", "--locked", "--no-dev", "--no-editable",
           "--no-emit-project", "--no-header", "-o", out]
    for extra in RUNTIME_EXTRAS:
        cmd += ["--extra", extra]
    run(cmd, env=uv_env(), ctx=ctx)
    return out


def install_into_runtime(ctx, requirements: Path, wheel: Path):
    python = ctx.runtime_python
    common = ["--python", python, "--link-mode", "copy"]
    run(["uv", "pip", "sync", *common, "--break-system-packages", requirements],
        env=uv_env(), ctx=ctx)
    run(["uv", "pip", "install", *common, "--break-system-packages", "--no-deps",
         wheel], env=uv_env(), ctx=ctx)


#: Removed from the stdlib: never imported by a server, and big.
STDLIB_PRUNE = ("test", "idlelib", "ensurepip", "turtledemo", "lib2to3",
                "pydoc_data", "tkinter/test", "unittest/test")


def _site_packages(ctx) -> Path:
    if ctx.os_word == "windows":
        return ctx.runtime_dir / "Lib" / "site-packages"
    matches = sorted((ctx.runtime_dir / "lib").glob("python3.*/site-packages"))
    if not matches:
        raise StepError("No site-packages in the runtime.")
    return matches[0]


def _stdlib(ctx) -> Path:
    if ctx.os_word == "windows":
        return ctx.runtime_dir / "Lib"
    return _site_packages(ctx).parent


def prune_runtime(ctx, profile="safe"):
    """Delete what a running server never touches. Returns bytes saved."""
    before = _tree_size(ctx.runtime_dir)
    stdlib = _stdlib(ctx)
    for name in STDLIB_PRUNE:
        _rmtree(stdlib / name)
    site = _site_packages(ctx)
    # Plural `tests` only, and never numpy.testing or a singular `test` --
    # both are imported at runtime by the packages that ship them.
    for path in sorted(site.glob("**/tests"), key=lambda p: -len(p.parts)):
        if path.is_dir() and path.parent.name != "testing":
            _rmtree(path)
    for pattern in ("**/*.pyi",) if profile == "aggressive" else ():
        for path in site.glob(pattern):
            path.unlink()
    for name in ("include", "share/man"):
        _rmtree(ctx.runtime_dir / name)
    if ctx.os_word == "windows":
        for name in ("libs", "tcl/tk8.6/demos"):
            _rmtree(ctx.runtime_dir / name)
    else:
        for path in (ctx.runtime_dir / "lib").glob("tk8.*/demos"):
            _rmtree(path)
    after = _tree_size(ctx.runtime_dir)
    say(f"pruned {(before - after) / 1e6:.0f} MB; runtime is {after / 1e6:.0f} MB")


def strip_launchers(ctx):
    """Console-script launchers embed the absolute path of the interpreter
    they were made for, which is a path on the build machine. The app runs
    `python -m`, so they are dead weight that would only mislead."""
    if ctx.os_word == "windows":
        scripts = ctx.runtime_dir / "Scripts"
        if scripts.exists():
            _rmtree(scripts)
    else:
        keep = re.compile(r"^python3(\.\d+)?$|^python$")
        for path in (ctx.runtime_dir / "bin").iterdir():
            if not keep.match(path.name):
                if path.is_dir():
                    _rmtree(path)
                else:
                    path.unlink()
    site = _site_packages(ctx)
    for direct in site.glob("plexora-*.dist-info/direct_url.json"):
        direct.unlink()
    for record in site.glob("*.dist-info/RECORD"):
        text = record.read_text(encoding="utf-8", errors="replace")
        if "../../Scripts/" in text or "../../../bin/" in text:
            lines = [line for line in text.splitlines()
                     if not (line.startswith("../../Scripts/") or line.startswith("../../../bin/"))]
            _write_text_lf(record, "\n".join(lines) + "\n")


def compile_runtime(ctx):
    """Byte-compile with unchecked hashes: the .pyc stay valid when an
    installer copies files with new timestamps, and the app runs with -B, so
    nothing ever writes a .pyc into an install directory."""
    done = run([ctx.runtime_python, "-B", "-m", "compileall", "-q", "-f", "-j", "0",
                "--invalidation-mode", "unchecked-hash", _stdlib(ctx)],
               ctx=ctx, check=False, capture=True)
    # A handful of stdlib modules are loaded by the compiler itself, and
    # Windows will not replace a .pyc that is open; those keep the timestamp
    # .pyc they shipped with, which is still valid. Anything else is an error.
    output = (done.stdout or "") + (done.stderr or "")
    # compileall prints "*** Error compiling '<path>'..." and the exception
    # on the next line.
    failures = re.findall(r"Error compiling '([^']+)'\.\.\.\s*\n(\w+)", output)
    locked = [path for path, kind in failures if kind == "PermissionError"]
    others = [f"{path}: {kind}" for path, kind in failures if kind != "PermissionError"]
    if locked:
        say(f"  {len(locked)} stdlib modules in use by the compiler kept their shipped .pyc")
    if others:
        raise StepError("Byte-compiling the runtime failed:\n  " + "\n  ".join(others[:10]))


def _tree_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def _import_probe(python: Path, env=None, cwd=None):
    program = (
        "import importlib, sys, json\n"
        f"names = {list(RUNTIME_IMPORTS)!r}\n"
        "failed = {}\n"
        "for n in names:\n"
        "    try: importlib.import_module(n)\n"
        "    except Exception as e: failed[n] = repr(e)\n"
        "import tkinter\n"
        "tkinter.Tcl().eval('info patchlevel')\n"
        "print(json.dumps({'prefix': sys.prefix, 'failed': failed}))\n")
    done = subprocess.run([str(python), "-I", "-B", "-c", program], capture_output=True,
                          text=True, env=env, cwd=cwd, timeout=600)
    if done.returncode != 0:
        raise StepError("The runtime could not import its own packages:\n"
                        + (done.stderr or done.stdout)[-3000:])
    return json.loads(done.stdout.strip().splitlines()[-1])


def _absolute_path_leaks(ctx, needle: str):
    leaks = []
    site = _site_packages(ctx)
    for pattern in ("*.pth", "*.dist-info/RECORD", "*.dist-info/direct_url.json"):
        for path in site.glob(pattern):
            if needle.lower() in path.read_text(encoding="utf-8", errors="replace").lower():
                leaks.append(str(path.relative_to(ctx.runtime_dir)))
    for cfg in ctx.runtime_dir.glob("**/pyvenv.cfg"):
        leaks.append(str(cfg.relative_to(ctx.runtime_dir)))
    return leaks


def check_relocatable(ctx):
    """Move the runtime, prove it still works there, move it back."""
    leaks = _absolute_path_leaks(ctx, str(ctx.runtime_dir))
    if leaks:
        raise StepError("Build-machine paths inside the runtime:\n  " + "\n  ".join(leaks))
    moved = ctx.runtime_dir.parent / (ctx.runtime_dir.name + "-moved test")
    _rmtree(moved)
    ctx.runtime_dir.rename(moved)
    try:
        python = moved / ctx.runtime_python.relative_to(ctx.runtime_dir)
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("PYTHON", "CONDA", "VIRTUAL_ENV"))}
        result = _import_probe(python, env=env, cwd=tempfile.gettempdir())
    finally:
        moved.rename(ctx.runtime_dir)
    if Path(result["prefix"]).resolve() != moved.resolve():
        raise StepError(f"The moved runtime reported prefix {result['prefix']}.")
    if result["failed"]:
        raise StepError("Imports failed in the moved runtime:\n  " + "\n  ".join(
            f"{name}: {error}" for name, error in result["failed"].items()))
    if ctx.os_word == "linux" and which("readelf"):
        _check_rpaths(ctx)
    say(f"relocatable: {len(RUNTIME_IMPORTS)} packages and Tcl/Tk import from a moved copy")


def _check_rpaths(ctx):
    bad = []
    for path in _site_packages(ctx).rglob("*.so*"):
        done = subprocess.run(["readelf", "-d", str(path)], capture_output=True, text=True)
        for line in done.stdout.splitlines():
            if ("RPATH" in line or "RUNPATH" in line) and str(ctx.build_dir) in line:
                bad.append(str(path))
    if bad:
        raise StepError("Absolute build rpaths:\n  " + "\n  ".join(bad[:10]))


def smoke_boot(ctx, python: Path | None = None, timeout=180):
    """Start `python -m plexora --desktop` exactly as the shell will."""
    python = python or ctx.runtime_python
    done = subprocess.run([str(python), "-I", "-B", "-m", "plexora", "--version"],
                          capture_output=True, text=True, timeout=300)
    say(f"  {done.stdout.strip() or done.stderr.strip()}")
    if done.returncode != 0 or read_version() not in done.stdout:
        raise StepError(f"The runtime's plexora is not {read_version()}:\n"
                        + done.stdout + done.stderr)
    with tempfile.TemporaryDirectory(prefix="plexora-smoke-") as data:
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("PYTHON", "CONDA", "VIRTUAL_ENV", "PLEXORA_"))}
        env["PLEXORA_DATA_PATH"] = data
        started = time.time()
        process = subprocess.Popen(
            [str(python), "-I", "-B", "-u", "-X", "utf8", "-m", "plexora",
             "--desktop", "--port", "0"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, cwd=data, text=True, encoding="utf-8")
        try:
            import threading

            box = {}
            reader = threading.Thread(target=lambda: box.update(line=process.stdout.readline()),
                                      daemon=True)
            reader.start()
            reader.join(timeout)
            if not box.get("line"):
                process.kill()
                raise StepError("No ready line from the runtime within "
                                f"{timeout}s:\n" + process.stderr.read()[-3000:])
            ready = json.loads(box["line"])
            boot = time.time() - started
            with urllib.request.urlopen(f"{ready['origin']}/health?token={ready['token']}",
                                        timeout=15) as response:
                if response.status != 204:
                    raise StepError(f"/health answered {response.status}.")
            with urllib.request.urlopen(f"{ready['origin']}/?token={ready['token']}",
                                        timeout=60) as response:
                page = response.read().decode("utf-8", errors="replace")
                if "desktopBridge.js" not in page:
                    raise StepError("The home page does not load desktopBridge.js.")
            process.stdin.close()
            code = process.wait(20)
            if code != 0:
                raise StepError(f"The server exited {code} when stdin closed.")
            say(f"  booted in {boot:.1f}s on {ready['origin']}, served the home page, "
                f"exited cleanly")
        finally:
            if process.poll() is None:
                process.kill()


def cmd_runtime(ctx, args):
    step(f"Runtime for {ctx.target} -> {ctx.runtime_dir}")
    wheel = find_wheel(ctx)
    stamp = ctx.build_dir / "stamps" / f"runtime-{ctx.runtime_triple}.json"
    inputs = {"pbs": pbs_asset_name(ctx.runtime_triple), "wheel": _sha256(wheel),
              "lock": _sha256(ROOT / "uv.lock"), "extras": list(RUNTIME_EXTRAS),
              "prune": args.prune_profile}
    if (not ctx.force and stamp.exists() and ctx.runtime_python.exists()
            and json.loads(stamp.read_text()) == inputs):
        say("unchanged since the last build (--force rebuilds)")
        return
    tarball = fetch_runtime(ctx)
    if ctx.dry_run:
        return
    unpack_runtime(ctx, tarball)
    requirements = export_lock(ctx)
    install_into_runtime(ctx, requirements, wheel)
    prune_runtime(ctx, args.prune_profile)
    strip_launchers(ctx)
    compile_runtime(ctx)
    check_relocatable(ctx)
    if not args.no_smoke:
        smoke_boot(ctx)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(json.dumps(inputs))


# -- macOS signing of the runtime ---------------------------------------------

_MACHO_MAGIC = {b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe",
                b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}


def iter_mach_o(root: Path):
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            with open(path, "rb") as handle:
                if handle.read(4) in _MACHO_MAGIC:
                    yield path
        except OSError:
            continue


def sign_mach_o(path: Path, identity: str):
    cmd = ["codesign", "--force", "--sign", identity, "--options", "runtime",
           "--entitlements", str(ENTITLEMENTS)]
    if identity != "-":
        cmd.append("--timestamp")
    subprocess.run(cmd + [str(path)], check=True, capture_output=True)


def sign_runtime(ctx):
    """Sign every Mach-O inside the runtime before Tauri seals the app.

    Tauri signs Contents/MacOS and Frameworks only; notarization checks every
    binary in the archive. Deepest first, so a library is signed before
    anything that loads it. Ad-hoc when no identity is configured -- Apple
    Silicon refuses to run unsigned code at all.
    """
    identity = os.environ.get("APPLE_SIGNING_IDENTITY") or "-"
    files = sorted(iter_mach_o(ctx.runtime_dir), key=lambda p: -len(p.parts))
    say(f"signing {len(files)} Mach-O files with {identity!r}")
    for path in files:
        sign_mach_o(path, identity)


def verify_runtime_signatures(ctx):
    bad = []
    for path in iter_mach_o(ctx.runtime_dir):
        done = subprocess.run(["codesign", "--verify", "--strict", str(path)],
                              capture_output=True, text=True)
        if done.returncode != 0:
            bad.append(str(path))
    if bad:
        raise StepError("Unsigned or broken signatures:\n  " + "\n  ".join(bad[:10]))


# -- the shell ---------------------------------------------------------------


def webview2_loader() -> Path:
    """The x64 WebView2Loader.dll shipped inside the webview2-com-sys crate,
    at the version Cargo.lock pins."""
    lock = (TAURI_DIR / "Cargo.lock").read_text(encoding="utf-8")
    match = re.search(r'name = "webview2-com-sys"\nversion = "([^"]+)"', lock)
    if not match:
        raise StepError("webview2-com-sys is not in desktop/src-tauri/Cargo.lock.")
    registry = Path(os.environ.get("CARGO_HOME", Path.home() / ".cargo")) / "registry" / "src"
    found = sorted(registry.glob(f"*/webview2-com-sys-{match.group(1)}/x64/WebView2Loader.dll"))
    if not found:
        raise StepError(f"WebView2Loader.dll for webview2-com-sys {match.group(1)} is not in "
                        f"{registry}.", "Run `cargo fetch` in desktop/src-tauri.")
    return found[-1]


def tauri_config_overlay(ctx, sign: bool) -> Path:
    """A `--config` overlay with only what this build adds to tauri.conf.json:
    the runtime as a resource, and whatever signing the environment asks for."""
    resources = {str(ctx.runtime_dir).replace("\\", "/") + "/": "runtime/"}
    if ctx.target.endswith("-gnullvm"):
        # llvm-mingw links the shell against its own unwinder, which Windows
        # does not have; it goes beside the executable.
        toolchain = llvm_mingw_dir(ctx)
        unwind = toolchain / "x86_64-w64-mingw32" / "bin" / "libunwind.dll" if toolchain else None
        if unwind is None or not unwind.exists():
            raise StepError("libunwind.dll not found in the llvm-mingw toolchain.")
        resources[str(unwind).replace("\\", "/")] = "libunwind.dll"
        # MSVC builds link the WebView2 loader statically; GNU-ABI builds load
        # it as a DLL, and the bundler does not ship it for them.
        resources[str(webview2_loader()).replace("\\", "/")] = "WebView2Loader.dll"
    overlay = {"bundle": {"resources": resources}}
    bundle = overlay["bundle"]
    if ctx.os_word == "macos":
        bundle["macOS"] = {"signingIdentity": os.environ.get("APPLE_SIGNING_IDENTITY") or "-",
                           "entitlements": str(ENTITLEMENTS)}
    if ctx.os_word == "windows":
        windows = {}
        if os.environ.get("PLEXORA_WIN_SIGN_COMMAND"):
            windows["signCommand"] = os.environ["PLEXORA_WIN_SIGN_COMMAND"]
        if os.environ.get("PLEXORA_WIN_CERT_THUMBPRINT"):
            windows["certificateThumbprint"] = os.environ["PLEXORA_WIN_CERT_THUMBPRINT"]
            windows["digestAlgorithm"] = os.environ.get("PLEXORA_WIN_DIGEST", "sha256")
            windows["timestampUrl"] = os.environ.get(
                "PLEXORA_WIN_TIMESTAMP_URL", "http://timestamp.digicert.com")
        if windows:
            bundle["windows"] = windows
    if sign:
        _assert_signing_inputs(ctx)
    path = ctx.build_dir / "tauri.overlay.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(overlay, indent=2), encoding="utf-8")
    return path


def _assert_signing_inputs(ctx):
    if ctx.os_word == "macos" and not os.environ.get("APPLE_SIGNING_IDENTITY"):
        raise StepError("--sign asked for, but APPLE_SIGNING_IDENTITY is not set.")
    if ctx.os_word == "windows" and not (os.environ.get("PLEXORA_WIN_SIGN_COMMAND")
                                         or os.environ.get("PLEXORA_WIN_CERT_THUMBPRINT")):
        raise StepError("--sign asked for, but neither PLEXORA_WIN_SIGN_COMMAND nor "
                        "PLEXORA_WIN_CERT_THUMBPRINT is set.")


def _ensure_tauri_cli(ctx):
    cli = DESKTOP / "node_modules" / ".bin" / ("tauri.cmd" if os.name == "nt" else "tauri")
    if not cli.exists():
        npm = which("npm")
        lock = DESKTOP / "package-lock.json"
        run([npm, "ci" if lock.exists() else "install"], cwd=DESKTOP, ctx=ctx)
    return cli


def llvm_mingw_dir(ctx) -> Path | None:
    """The llvm-mingw toolchain for the `gnullvm` target, if one is unpacked:
    PLEXORA_LLVM_MINGW, or the newest under <build dir>/toolchain."""
    if os.environ.get("PLEXORA_LLVM_MINGW"):
        return Path(os.environ["PLEXORA_LLVM_MINGW"])
    found = sorted((ctx.build_dir / "toolchain").glob("llvm-mingw-*-ucrt-x86_64"))
    return found[-1] if found else None


def _cargo_env(ctx):
    env = {"CARGO_TARGET_DIR": str(ctx.cargo_target_dir)}
    path = [os.environ.get("PATH", "")]
    cargo_bin = Path.home() / ".cargo" / "bin"
    if cargo_bin.exists():
        path.insert(0, str(cargo_bin))
    if ctx.target.endswith("-gnullvm"):
        toolchain = llvm_mingw_dir(ctx)
        if toolchain is None:
            raise StepError("The gnullvm target needs llvm-mingw.",
                            "Unpack github.com/mstorsjo/llvm-mingw's ucrt-x86_64 zip under "
                            f"{ctx.build_dir / 'toolchain'} or set PLEXORA_LLVM_MINGW.")
        bin_dir = toolchain / "bin"
        path.insert(0, str(bin_dir))
        # Build scripts and proc macros run on the HOST, so the host toolchain
        # has to link with llvm-mingw too, or they go looking for link.exe.
        env["RUSTUP_TOOLCHAIN"] = os.environ.get(
            "RUSTUP_TOOLCHAIN", "stable-x86_64-pc-windows-gnullvm")
        env["CARGO_TARGET_X86_64_PC_WINDOWS_GNULLVM_LINKER"] = str(bin_dir / "x86_64-w64-mingw32-clang.exe")
        env["CC_x86_64_pc_windows_gnullvm"] = str(bin_dir / "x86_64-w64-mingw32-clang.exe")
        env["AR_x86_64_pc_windows_gnullvm"] = str(bin_dir / "llvm-ar.exe")
        env["RC_x86_64_pc_windows_gnullvm"] = str(bin_dir / "x86_64-w64-mingw32-windres.exe")
    env["PATH"] = os.pathsep.join(path)
    return env


def tauri_build(ctx, sign=False, debug=False):
    cli = _ensure_tauri_cli(ctx)
    overlay = tauri_config_overlay(ctx, sign)
    cmd = [cli, "build", "--ci", "--target", ctx.target,
           "--bundles", ",".join(BUNDLES[ctx.os_word]), "--config", overlay]
    if debug:
        cmd.append("--debug")
    run(cmd, cwd=DESKTOP, env=_cargo_env(ctx), ctx=ctx)


def bundle_dir(ctx, debug=False) -> Path:
    return ctx.cargo_target_dir / ctx.target / ("debug" if debug else "release") / "bundle"


def cmd_bundle(ctx, args):
    step(f"Desktop bundle for {ctx.target}")
    stale = propagate(check=True)
    if stale:
        raise StepError("Version out of step:\n  " + "\n  ".join(stale),
                        "Run `python scripts/release.py propagate`.")
    if not ctx.runtime_python.exists():
        raise StepError(f"No runtime at {ctx.runtime_dir}.",
                        "Run `python scripts/release.py runtime`.")
    if ctx.os_word == "macos" and not ctx.dry_run:
        sign_runtime(ctx)
        verify_runtime_signatures(ctx)
    tauri_build(ctx, sign=args.sign, debug=args.debug)


def expected_artifacts(ctx, debug=False):
    """`[(built file, release name)]` for this target."""
    version = read_version()
    name_version = artifact_version(ctx)
    base = bundle_dir(ctx, debug)
    os_word, arch = ctx.os_word, ctx.arch_word
    stem = f"{PRODUCT}-{name_version}-{os_word}-{arch}"
    if os_word == "windows":
        return [(base / "nsis" / f"{PRODUCT}_{version}_x64-setup.exe", f"{stem}-setup.exe")]
    if os_word == "macos":
        tauri_arch = "aarch64" if arch == "arm64" else "x64"
        return [(base / "dmg" / f"{PRODUCT}_{version}_{tauri_arch}.dmg", f"{stem}.dmg")]
    return [(base / "appimage" / f"{PRODUCT}_{version}_amd64.AppImage", f"{stem}.AppImage"),
            (base / "deb" / f"{PRODUCT}_{version}_amd64.deb", f"{stem}.deb")]


def cmd_collect(ctx, args=None):
    step("Collect")
    out = ctx.release_dir / artifact_version(ctx)
    out.mkdir(parents=True, exist_ok=True)
    collected = []
    for built, name in expected_artifacts(ctx, getattr(args, "debug", False)):
        if not built.exists():
            raise StepError(f"Expected {built} and it is not there.")
        target = out / name
        shutil.copy2(built, target)
        collected.append(target)
    if ctx.dist_dir.exists():
        for path in ctx.dist_dir.glob(f"plexora-{read_version()}*"):
            shutil.copy2(path, out / path.name)
            collected.append(out / path.name)
    write_checksums(out)
    for path in collected:
        say(f"  {path.name}  {path.stat().st_size / 1e6:.0f} MB")
    say(f"-> {out}")
    return out


def write_checksums(directory: Path):
    files = sorted(path for path in directory.iterdir()
                   if path.is_file() and path.name not in ("SHA256SUMS.txt", "SIZES.txt"))
    (directory / "SHA256SUMS.txt").write_text(
        "".join(f"{_sha256(path)}  {path.name}\n" for path in files), encoding="utf-8")
    (directory / "SIZES.txt").write_text(
        "".join(f"{path.stat().st_size:>14,}  {path.name}\n" for path in files),
        encoding="utf-8")


def cmd_checksums(ctx, args):
    directory = Path(args.directory) if args.directory else ctx.release_dir / artifact_version(ctx)
    write_checksums(directory)
    say((directory / "SHA256SUMS.txt").read_text())


# -- validate ------------------------------------------------------------------


def _smoke(executable: Path, env=None, cwd=None) -> None:
    say(f"$ {executable} --smoke-test")
    started = time.time()
    done = subprocess.run([str(executable), "--smoke-test"], capture_output=True,
                          text=True, timeout=300, env=env, cwd=cwd)
    output = (done.stdout + done.stderr).strip()
    if done.returncode != 0:
        raise StepError(f"--smoke-test failed: {SMOKE_CODES.get(done.returncode, done.returncode)}"
                        f"\n{output[-3000:]}")
    say(f"  {output.splitlines()[-1] if output else 'ok'}  ({time.time() - started:.1f}s)")


def validate_launch(ctx, artifact: Path | None, bundle_only=False, debug=False):
    if bundle_only or artifact is None:
        exe_dir = ctx.cargo_target_dir / ctx.target / ("debug" if debug else "release")
        exe = exe_dir / (BINARY + (".exe" if ctx.os_word == "windows" else ""))
        if ctx.os_word == "macos":
            exe = bundle_dir(ctx, debug) / "macos" / f"{PRODUCT}.app" / "Contents" / "MacOS" / BINARY
        # Tauri copies resources beside the binary only inside a bundle; for
        # the bare executable, point it at the staged runtime.
        env = dict(os.environ, PLEXORA_DESKTOP_RUNTIME=str(ctx.runtime_dir))
        return _smoke(exe, env=env)

    artifact = Path(artifact)
    name = artifact.name.lower()
    with tempfile.TemporaryDirectory(prefix="plexora-validate-") as temp:
        temp = Path(temp)
        if name.endswith("-setup.exe"):
            if not ctx.yes:
                raise StepError("Validating the installer installs Plexora (per user) "
                                "into a temporary folder and uninstalls it again.",
                                "Pass --yes to allow it.")
            target = temp / "Plexora"
            run([artifact, "/S", f"/D={target}"], timeout=900)
            exe = target / f"{BINARY}.exe"
            try:
                if not exe.exists():
                    raise StepError(f"The installer did not create {exe}.")
                _smoke(exe)
            finally:
                uninstaller = target / "uninstall.exe"
                if uninstaller.exists():
                    run([uninstaller, "/S"], check=False, timeout=600)
        elif name.endswith(".dmg"):
            mount = temp / "mnt"
            mount.mkdir()
            run(["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", mount, artifact])
            try:
                _smoke(mount / f"{PRODUCT}.app" / "Contents" / "MacOS" / BINARY)
            finally:
                run(["hdiutil", "detach", mount], check=False)
        elif name.endswith(".appimage"):
            artifact.chmod(artifact.stat().st_mode | stat.S_IEXEC)
            env = dict(os.environ, APPIMAGE_EXTRACT_AND_RUN="1")
            _smoke(artifact, env=env, cwd=temp)
        elif name.endswith(".deb"):
            run(["dpkg-deb", "-x", artifact, temp])
            _smoke(temp / "usr" / "bin" / BINARY)
        else:
            raise StepError(f"Do not know how to validate {artifact.name}.")


def cmd_validate(ctx, args):
    step("Validate")
    if args.bundle_dir:
        return validate_launch(ctx, None, bundle_only=True, debug=args.debug)
    artifacts = [Path(a) for a in args.artifacts] or [
        ctx.release_dir / artifact_version(ctx) / name
        for _built, name in expected_artifacts(ctx)]
    for artifact in artifacts:
        say(f"-- {artifact.name}")
        validate_launch(ctx, artifact)


# -- all / ci / clean ----------------------------------------------------------


def cmd_all(ctx, args):
    started = time.time()
    if not ctx.skip_tests:
        cmd_tests(ctx)
    cmd_client(ctx, argparse.Namespace(verify_only=ctx.skip_client))
    stale = propagate(check=True)
    if stale:
        raise StepError("Version out of step:\n  " + "\n  ".join(stale),
                        "Run `python scripts/release.py propagate`.")
    if not ctx.skip_wheel:
        cmd_wheel(ctx)
    cmd_runtime(ctx, argparse.Namespace(prune_profile="safe", no_smoke=False))
    cmd_bundle(ctx, argparse.Namespace(sign=args.sign, debug=False))
    out = cmd_collect(ctx)
    if not args.no_validate:
        cmd_validate(ctx, argparse.Namespace(bundle_dir=False, debug=False, artifacts=[]))
    say(f"\nDone in {(time.time() - started) / 60:.1f} min -> {out}")


def cmd_ci(ctx, args):
    """Bump (optionally), tag, push, watch the GitHub build, download it."""
    if _git_dirty_files():
        raise StepError("The working tree is not clean.", "Commit or stash first.")
    if args.bump:
        version = cmd_bump(ctx, argparse.Namespace(spec=args.bump, commit=True, tag=True))
    else:
        version = read_version()
        tags = subprocess.run(["git", "tag", "--list", f"v{version}"], cwd=ROOT,
                              capture_output=True, text=True).stdout.split()
        if not tags:
            run(["git", "tag", "-a", f"v{version}", "-m", f"Plexora {version}"])
    remote = args.remote
    run(["git", "push", remote, "HEAD"])
    run(["git", "push", remote, f"v{version}"])
    say("waiting for the Release workflow to start...")
    run_id = None
    for _ in range(60):
        done = run(["gh", "run", "list", "--workflow", "release.yml", "--branch",
                    f"v{version}", "--limit", "1", "--json", "databaseId"],
                   capture=True, check=False)
        found = json.loads(done.stdout or "[]")
        if found:
            run_id = str(found[0]["databaseId"])
            break
        time.sleep(10)
    if not run_id:
        raise StepError("The Release workflow did not start.", "Check GitHub Actions.")
    run(["gh", "run", "watch", run_id, "--exit-status"])
    out = ctx.release_dir / version
    out.mkdir(parents=True, exist_ok=True)
    run(["gh", "release", "download", f"v{version}", "--dir", out, "--clobber"])
    say(f"-> {out}")


def cmd_clean(ctx, args):
    targets = [ctx.dist_dir, ctx.build_dir / "stamps", ctx.build_dir / "tauri.overlay.json",
               ROOT / "dist", ROOT / "build" / "lib"]
    if args.all:
        targets += [ctx.build_dir, ctx.release_dir, TAURI_DIR / "target", TAURI_DIR / "gen"]
    for path in targets:
        if path.exists():
            say(f"removing {path}")
            if not ctx.dry_run:
                _rmtree(path) if path.is_dir() else path.unlink()


# -- argv ------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(prog="release.py", description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dev", dest="dev", action="store_true", default=True,
                      help="Dev build: file names carry -dev+g<sha> (default).")
    mode.add_argument("--release", dest="dev", action="store_false",
                      help="Release build: plain version in file names.")
    parser.add_argument("--target", default="", help="Rust target triple (default: this machine).")
    parser.add_argument("--build-dir", type=Path, default=None)
    parser.add_argument("--runtime-dir", type=Path, default=None)
    parser.add_argument("--release-dir", type=Path, default=None)
    parser.add_argument("--no-clean", action="store_true")
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--skip-client", action="store_true",
                        help="Verify the committed client bundle rather than rebuild it.")
    parser.add_argument("--skip-wheel", action="store_true",
                        help="Use the wheel already in the dist directory.")
    parser.add_argument("--force", action="store_true", help="Ignore up-to-date stamps.")
    parser.add_argument("--yes", action="store_true",
                        help="Allow steps that install software (installer validation).")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    subs = parser.add_subparsers(dest="command", required=True)

    doctor = subs.add_parser("doctor", help="Check the toolchain.")
    doctor.add_argument("--ci", action="store_true")
    doctor.add_argument("--dropbox-ignore", action="store_true",
                        help="Mark build directories as ignored by Dropbox.")

    bump = subs.add_parser("bump", help="Change the version everywhere.")
    bump.add_argument("spec", help="major | minor | patch | X.Y.Z")
    bump.add_argument("--commit", action="store_true")
    bump.add_argument("--tag", action="store_true")

    prop = subs.add_parser("propagate", help="Copy pyproject's version to every file.")
    prop.add_argument("--check", action="store_true")

    client = subs.add_parser("client", help="Rebuild (or verify) the client bundle.")
    client.add_argument("--verify-only", action="store_true")

    subs.add_parser("wheel", help="Build the wheel and sdist.")
    subs.add_parser("test", help="Run the test suite.")

    runtime = subs.add_parser("runtime", help="Build the embedded Python runtime.")
    runtime.add_argument("--prune-profile", choices=("safe", "aggressive"), default="safe")
    runtime.add_argument("--no-smoke", action="store_true")

    bundle = subs.add_parser("bundle", help="Build the desktop installer(s).")
    bundle.add_argument("--sign", action="store_true",
                        help="Require signing credentials rather than falling back.")
    bundle.add_argument("--debug", action="store_true", help="Debug shell (devtools).")

    collect = subs.add_parser("collect", help="Rename artifacts into the release directory.")
    collect.add_argument("--debug", action="store_true")

    validate = subs.add_parser("validate", help="Launch built artifacts with --smoke-test.")
    validate.add_argument("artifacts", nargs="*")
    validate.add_argument("--bundle-dir", action="store_true",
                          help="Smoke-test the built executable without installing.")
    validate.add_argument("--debug", action="store_true")

    checksums = subs.add_parser("checksums", help="Write SHA256SUMS.txt and SIZES.txt.")
    checksums.add_argument("directory", nargs="?")

    everything = subs.add_parser("all", help="tests, client, wheel, runtime, bundle, collect, validate")
    everything.add_argument("--sign", action="store_true")
    everything.add_argument("--no-validate", action="store_true")

    ci = subs.add_parser("ci", help="Tag and let GitHub Actions build every OS.")
    ci.add_argument("--bump", default=None)
    ci.add_argument("--remote", default="plexora")

    clean = subs.add_parser("clean", help="Remove build products.")
    clean.add_argument("--all", action="store_true")
    return parser


COMMANDS = {
    "doctor": cmd_doctor, "bump": cmd_bump, "propagate": cmd_propagate,
    "client": cmd_client, "wheel": cmd_wheel, "test": cmd_tests,
    "runtime": cmd_runtime, "bundle": cmd_bundle, "collect": cmd_collect,
    "validate": cmd_validate, "checksums": cmd_checksums, "all": cmd_all,
    "ci": cmd_ci, "clean": cmd_clean,
}


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        ctx = Ctx(dev=args.dev, target=args.target,
                  build_dir=args.build_dir or default_build_dir(),
                  runtime_dir=args.runtime_dir, release_dir=args.release_dir,
                  clean=not args.no_clean, skip_tests=args.skip_tests,
                  skip_client=args.skip_client, skip_wheel=args.skip_wheel,
                  force=args.force, yes=args.yes, verbose=args.verbose,
                  dry_run=args.dry_run)
        COMMANDS[args.command](ctx, args)
    except StepError as exc:
        print(f"\nrelease.py: {exc}", file=sys.stderr)
        if exc.hint:
            print(f"  hint: {exc.hint}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
