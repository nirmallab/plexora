"""What kind of machine and launch this is, as coarse families.

Computed once per process on the telemetry writer thread (some of it runs a
subprocess or reads dist-info), and never anything finer than a family: `mac`
not a macOS build, `3.13` not a patch release, `slurm` not a cluster, a
power-of-two band of CPUs and memory.
"""

from __future__ import annotations

import os
import platform
import re
import sys

from plexora.telemetry import identity, schema

_VERSION = schema.CLIENT_BLOCK["plexora_version"].type
_PLUGIN_VERSION = re.compile(r"[0-9A-Za-z.+-]{1,24}")


def os_family() -> str:
    if sys.platform == "darwin":
        return "mac"
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    return "other"


def python_minor() -> str:
    minor = f"{sys.version_info.major}.{sys.version_info.minor}"
    return minor if schema.CLIENT_BLOCK["python"].type.check(minor) else "other"


def arch() -> str:
    machine = (platform.machine() or "").lower()
    if machine in ("x86_64", "amd64", "x64"):
        return "x86_64"
    if machine in ("arm64", "aarch64", "armv8"):
        return "arm64"
    return "other"


def plexora_version() -> str:
    try:
        from plexora.updates import current_version

        version = current_version()
    except Exception:
        version = None
    return version if version and _VERSION.check(version) else "0.0.0"


def install_kind() -> str:
    try:
        from plexora import updates

        desktop = os.environ.get("PLEXORA_DESKTOP", "").lower() in ("1", "true", "yes")
        kind = updates.install_kind(desktop=desktop).get("kind")
    except Exception:
        kind = None
    return kind if schema.INSTALL_KIND.check(kind) else "unknown"


def scheduler() -> str:
    try:
        from plexora.cli import scheduler_topology

        name = scheduler_topology()[0]
    except Exception:
        name = None
    if name in ("slurm", "pbs", "lsf", "sge"):
        return name
    if os.environ.get("SGE_TASK_ID") or os.environ.get("JOB_ID") and os.environ.get("SGE_ROOT"):
        return "sge"
    return "none"


def in_container() -> bool:
    if os.environ.get("PLEXORA_DOCKER", "").lower() in ("1", "true", "yes"):
        return True
    try:
        from plexora.updates import _in_container

        return bool(_in_container())
    except Exception:
        return False


def remote_env() -> bool:
    try:
        from plexora.cli import looks_remote

        return bool(looks_remote())
    except Exception:
        return False


def deployment(detected: str, sched: str, container: bool, remote: bool) -> str:
    if container:
        return "container"
    if detected == "ood":
        return "ood"
    if detected == "proxy":
        return "jupyterhub"
    if detected == "colab":
        return "colab"
    if sched not in ("none", "ssh"):
        return "hpc"
    if remote or detected == "remote":
        return "remote"
    return "local"


def machine() -> dict:
    """`{cpus, memory_gb, gpu}` as bands and a boolean."""
    facts = {}
    try:
        from plexora import _resources

        cpus = _resources.allocated_cpus()
        memory = _resources._total_memory_gb()
        facts["cpus"] = schema.band_pow2(cpus or 0)
        facts["memory_gb"] = schema.band_pow2(int(memory or 0))
        gpu = bool(_resources._gpus()) or (sys.platform == "darwin" and arch() == "arm64")
        facts["gpu_present"] = gpu
    except Exception:
        pass
    return facts


def plugins(app) -> list:
    """`[{id, version}]` for the plugins this server mounted."""
    found = []
    if app is None:
        return found
    try:
        from plexora.server import plugins as registry

        for plugin in registry.installed(app):
            version = str(getattr(plugin, "version", "") or "0")
            if not _PLUGIN_VERSION.fullmatch(version):
                version = "other"
            found.append({"id": identity.owner_label(plugin.name), "version": version})
    except Exception:
        pass
    return found[:64]


def describe(app=None, serve_mode="terminal", context=None) -> dict:
    """Everything the client block and session summary need about this run."""
    context = dict(context or {})
    detected = context.get("detected") or "none"
    if not schema.DETECTED.check(detected):
        detected = "none"
    sched = scheduler()
    container = in_container()
    remote = remote_env()
    server_is_remote = os.environ.get("PLEXORA_SERVER_IS_REMOTE", "").lower() in ("1", "true", "yes")
    mode = serve_mode if schema.SERVE_MODE.check(serve_mode) else "other"
    return {
        "plexora_version": plexora_version(),
        "python": python_minor(),
        "os": os_family(),
        "arch": arch(),
        "launch_mode": mode,
        "deployment": deployment(detected, sched, container, remote or server_is_remote),
        "scheduler": sched,
        "install_kind": install_kind(),
        "plugins": plugins(app),
        "detected": detected,
        "container": container,
        "remote_env": remote,
        "server_is_remote": server_is_remote,
        **machine(),
    }


CLIENT_KEYS = ("plexora_version", "python", "os", "arch", "launch_mode", "deployment",
               "scheduler", "install_kind", "plugins")
