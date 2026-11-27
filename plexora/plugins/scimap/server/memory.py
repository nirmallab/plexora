"""How Scimap Pro will read the working file: in memory or streamed from disk.

Prepare ends by asking `scimappro.io.init` this question, and every later
step opens the file with the same setting, so what the page says is what
happens. The answer is read from Scimap Pro's own decision
(`_data.resolveStreaming`) and the file's footprint (`peekFootprint`, metadata
only) -- the file is not loaded to answer it.

The user's Memory setting (Automatic / In memory / Streamed) is the notebook's
and is passed to `io.init` as `streamData` ("auto", False, True). Changing it
asks again; nothing reruns.
"""

from __future__ import annotations

import os
import platform

CHOICES = ("auto", "memory", "stream")

REASONS = {
    "fits-budget": "The whole file fits in the memory budget, so every step loads it once and "
                   "works in memory. Results are saved to the file when each step finishes.",
    "over-budget": "Loading the file would take {peak}, more than the {usable} a step may use, so "
                   "each step reads only the rows and columns it needs, a block at a time, and "
                   "writes its result straight into the file. Same results; steps that read every "
                   "cell take longer.",
    "unlicensed": "Streaming from disk is part of a Scimap Pro license, so every step loads the "
                  "file into memory. A file larger than the memory budget is refused with the "
                  "reason, and Prepare offers a sample.",
    "explicit-memory": "You chose In memory: every step loads the whole file, whatever its size.",
    "explicit-stream": "You chose Streamed: each step reads the file a block at a time and writes "
                       "its result straight into it, whatever its size.",
    "zarr-store": "A .zarr store is always read a block at a time.",
    "shape-unknown": "The file's size could not be read ahead, so it is loaded into memory.",
    "read-only": "The file cannot be written to, so it is read a block at a time.",
}


def _size(n) -> str:
    if n is None:
        return "unknown"
    n = float(n)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("bytes", "KB", "MB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def report(path, memory="auto") -> dict:
    """The facts the Prepare card shows. Runs where Scimap Pro is installed;
    never loads the file."""
    from pathlib import Path

    from plexora.plugins.scimap.server.adapter import MEMORY, scimappro

    sm = scimappro()
    from scimappro._data import _canStream, detectKind, peekFootprint, resolveStreaming, storeTableName
    from scimappro._exec.memory import availableMemory, memoryBudget, totalMemory

    memory = memory if memory in CHOICES else "auto"
    p = Path(path)
    kind = detectKind(p)
    table = f"tables/{storeTableName(p, None)}" if kind == "zarr" else ""
    licensed = bool(_canStream())
    want = MEMORY[memory]
    if want is True and not licensed:
        want = "auto"  # refused at run time; the card says why
    decision = resolveStreaming(want, p, kind=kind, verbose=False, tablePath=table)
    footprint = decision.layout or peekFootprint(p, table) or {}
    budget = memoryBudget(None)
    usable = int(budget * 0.75)
    stream = bool(decision)
    reason = decision.reason
    if reason == "explicit":
        reason = "explicit-stream" if stream else "explicit-memory"
    loaded = footprint.get("loadedBytes")
    peak = decision.peakBytes or loaded
    n_obs, n_vars = footprint.get("nObs"), footprint.get("nVars")
    dense = footprint.get("dense")
    dtype = footprint.get("dtype")
    data_line = None
    if n_obs:
        data_line = (f"{n_obs:,} cells × {n_vars:,} markers · "
                     f"{'dense' if dense else 'sparse'} {dtype or ''}".strip()
                     + (" · raw layer kept" if footprint.get("raw") else ""))
    try:
        workers = sm._workers.resolveWorkers(None, label="init")
    except Exception:  # noqa: BLE001
        workers = None
    total, free = totalMemory(), availableMemory()
    machine = f"{os.cpu_count()} cores · {_size(total)} RAM, {_size(free)} free"
    faster = None
    if stream and kind == "h5ad":
        faster = "a .zarr copy (io.toZarr) reads blocks faster than .h5ad"
    return {
        "mode": "stream" if stream else "memory",
        "title": "Streamed from disk" if stream else "In memory",
        "reason": reason,
        "sentence": REASONS.get(reason, "").format(peak=_size(peak), usable=_size(usable)),
        "machine": machine,
        "workers": workers,
        "budget": f"{_size(budget)} (80% of free memory)" if not sm.settings.memoryLimit
                  else f"{_size(budget)} (Scimap Pro's memory limit)",
        "dataset": data_line,
        "cost": f"about {_size(peak)}" if peak else None,
        "faster": faster,
        "licensed_streaming": licensed,
        "software": f"scimappro {_version()} · Python {platform.python_version()} · "
                    f"{platform.system().replace('Darwin', 'macOS')} {platform.mac_ver()[0] or platform.release()} "
                    f"{platform.machine()}",
        "choice": memory,
        "refused": memory == "stream" and not licensed,
        "docs": "https://pro.scimap.xyz/api/io/init/",
    }


def _version():
    try:
        from importlib.metadata import version

        return version("scimappro")
    except Exception:  # noqa: BLE001
        return "?"
