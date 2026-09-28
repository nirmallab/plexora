"""What identifies this environment to the licence server -- and what does not.

An ENVIRONMENT is a registration: a laptop, a workstation, or a whole HPC
cluster. It is identified by a random 256-bit secret, generated the first time
it registers and kept in `environment.json` in the config directory. Nothing
about the hardware goes into it. On a cluster that file is under `$HOME`, so the
login node that registered and every compute node, container and notebook that
runs afterwards present the same identity: one cluster, one environment, however
many nodes a job lands on. The server is told only `sha256(secret)` (the
BINDING) and stores a peppered hash of that.

The TRIAL FINGERPRINT is a different thing with a different job. A trial is
refused to a machine that has already had two, so it needs something that
survives reinstalling Plexora and deleting the config directory -- the OS
machine id, HMAC'd with the product name exactly as `py-machineid`'s
`hashed_id("plexora")` does, so it is the same value whether or not that
package is installed. Only the hash leaves the machine, and only when a trial
is requested or a person asks to see it.
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import os
import platform
import secrets
import socket
import subprocess
import sys
import time
import uuid

from plexora.licensing import store

APP_ID = "plexora"
KINDS = ("desktop", "cluster", "container-host")


def _b64url(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


# -- the environment secret ---------------------------------------------------


def secret(*, create: bool = False) -> str | None:
    """This environment's secret, creating it when `create` is set.

    Resolution never creates one (it only reads), so a Free user who never
    activates never has a file written on their behalf.
    """
    record = store.read_environment()
    value = record.get("secret")
    if isinstance(value, str) and len(value) >= 32:
        return value
    if not create:
        return None
    fresh = {"schema": store.SCHEMA, "secret": _b64url(secrets.token_bytes(32)),
             "created_at": int(time.time())}
    path = store.environment_path()
    if path.exists():
        # There is a file and it holds no usable secret -- damaged, or written
        # by something else. Replacing it is the only way forward.
        store.write_environment({**record, **fresh})
        return fresh["secret"]
    if not store.create_private_json(path, fresh):
        # Another process on this account (a second job on another node)
        # created it first. Its secret is this environment's secret too.
        won = store.read_environment().get("secret")
        if isinstance(won, str) and len(won) >= 32:
            return won
        store.write_environment(fresh)
    return fresh["secret"]


def binding_of(value: str) -> str:
    """What the server is told: the SHA-256 of the secret, in hex."""
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def binding(*, create: bool = False) -> str | None:
    """This environment's binding, or None when it has never registered."""
    value = secret(create=create)
    return binding_of(value) if value else None


def forget() -> bool:
    """Throw this environment's identity away (`plexora license remove --all`).

    The next registration is a new environment as far as the server can tell,
    which is why this is never done implicitly.
    """
    return store.clear_environment()


# -- the delegation key (clusters and container hosts) --------------------------


def delegation_keypair(*, create: bool = False) -> tuple[bytes, str] | None:
    """`(private seed, public b64url)` for minting job certificates, or None.

    Generated at cluster registration; the public half goes to the server,
    which embeds it in this environment's certificate. The private half never
    leaves environment.json.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (Encoding, NoEncryption,
                                                              PrivateFormat, PublicFormat)

    record = store.read_environment()
    seed_text = record.get("delegation_seed")
    if isinstance(seed_text, str) and seed_text:
        import base64

        seed = base64.urlsafe_b64decode(seed_text + "=" * (-len(seed_text) % 4))
        private = Ed25519PrivateKey.from_private_bytes(seed)
    elif create:
        private = Ed25519PrivateKey.generate()
        seed = private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        store.write_environment({**record, "delegation_seed": _b64url(seed)})
    else:
        return None
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return seed, _b64url(public)


# -- what kind of place this is -------------------------------------------------


def scheduler_hint() -> str:
    """The batch scheduler this process can see, or `none`."""
    env = os.environ
    if env.get("SLURM_JOB_ID") or env.get("SLURM_CLUSTER_NAME") or env.get("SLURM_CONF"):
        return "slurm"
    if env.get("PBS_JOBID") or env.get("PBS_SERVER"):
        return "pbs"
    if env.get("LSB_JOBID") or env.get("LSF_ENVDIR"):
        return "lsf"
    if env.get("SGE_ROOT"):
        return "sge"
    from shutil import which

    if which("sbatch"):
        return "slurm"
    if which("qsub"):
        return "pbs"
    if which("bsub"):
        return "lsf"
    return "none"


def in_container() -> bool:
    env = os.environ
    return bool(env.get("SINGULARITY_CONTAINER") or env.get("APPTAINER_CONTAINER")
                or os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"))


def suggested_kind() -> str:
    """What `plexora license environment register` should default to."""
    if scheduler_hint() != "none":
        return "cluster"
    return "desktop"


def os_label() -> str:
    if sys.platform == "darwin":
        return "macOS"
    if sys.platform.startswith("win"):
        return "Windows"
    if sys.platform.startswith("linux"):
        return "Linux"
    return platform.system() or "Unknown"


def suggested_name(kind: str) -> str:
    """A friendly default name. Never a hostname: the name is shown in the
    portal to whoever manages the licence, and a person can always give a
    better one with `--name`."""
    if kind == "cluster":
        cluster = (os.environ.get("SLURM_CLUSTER_NAME") or "").strip()
        return f"{cluster} cluster" if cluster else "HPC cluster"
    if kind == "container-host":
        return f"{os_label()} container host"
    return f"{os_label()} computer"


def platform_label() -> str:
    machine = (platform.machine() or "").lower()
    arch = {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}.get(machine, machine)
    return f"{os_label().lower()}-{arch or 'unknown'}"


# -- the trial fingerprint ------------------------------------------------------


def _machine_id() -> str | None:
    """The OS's own machine identifier, or None where there is none."""
    try:
        import machineid  # type: ignore[import-not-found]

        value = machineid.id()
        if value:
            return str(value).strip()
    except Exception:  # noqa: BLE001 - any failure means "read it ourselves"
        pass
    if sys.platform.startswith("linux"):
        for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                with open(path, encoding="ascii") as handle:
                    value = handle.read().strip()
                if value:
                    return value
            except OSError:
                continue
        return None
    if sys.platform == "darwin":
        from plexora._subprocess import popen_kwargs

        try:
            out = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                                 capture_output=True, text=True, timeout=5,
                                 **popen_kwargs()).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        for line in out.splitlines():
            if "IOPlatformUUID" in line:
                return line.split("=", 1)[-1].strip().strip('"') or None
        return None
    if sys.platform.startswith("win"):
        try:
            import winreg  # type: ignore[import-not-found]

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SOFTWARE\Microsoft\Cryptography") as key:
                value, _ = winreg.QueryValueEx(key, "MachineGuid")
                return str(value).strip() or None
        except Exception:  # noqa: BLE001
            return None
    return None


@functools.lru_cache(maxsize=1)
def machine_basis() -> str:
    """The trial fingerprint: HMAC-SHA256(key=b"plexora", msg=machine id).

    Containers and some HPC images have no machine id, so the fallback hashes
    the pieces usually stable within one host. Neither form is reversible to a
    serial number or a MAC address.
    """
    ident = _machine_id()
    if ident:
        return hmac.new(APP_ID.encode(), ident.encode(), hashlib.sha256).hexdigest()
    parts = [socket.gethostname(), f"{uuid.getnode():012x}", platform.machine(),
             platform.system()]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def plexora_version() -> str:
    try:
        from importlib.metadata import version

        return version("plexora")
    except Exception:  # noqa: BLE001 - a source checkout with no metadata
        return "0.0.0"


def fingerprint_report(*, name: str | None = None, kind: str | None = None,
                       create: bool = True) -> dict:
    """What the portal's offline-licence form needs, and nothing more.

    Everything here is a hash, a coarse family, or a name the person chose. A
    person can read the file before uploading it and see that for themselves.
    Creates the environment secret when asked for one, because an offline
    licence is bound to it.
    """
    chosen = kind if kind in KINDS else suggested_kind()
    delegation = delegation_keypair(create=create) if chosen != "desktop" else None
    return {
        "schema": 1,
        "product": "plexora",
        "kind": chosen,
        "display_name": (name or "").strip() or suggested_name(chosen),
        "binding": binding(create=create),
        "delegation_pubkey": delegation[1] if delegation else None,
        "platform": platform_label(),
        "scheduler_hint": scheduler_hint(),
        "plexora_version": plexora_version(),
        "created_at": int(time.time()),
    }
