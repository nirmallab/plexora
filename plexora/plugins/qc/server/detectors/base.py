"""The detector interface: maps in, candidate regions out.

A detector reads the scan's maps (`DetectorContext`) and proposes candidate
regions -- a mask on the map grid, the class it suggests, which channels,
how strong -- never a verdict. Whether a candidate is an artifact, and what it
means for the cells, is the agent's call and the strictness preset's. So a
detector never sees strictness, and a better one (a trained model, say)
drops in beside the classical ones without anything else changing:
`available()` says whether it can run here, `run()` returns candidates.
Third-party detectors register under the `plexora.qc_detectors` entry point.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np


@dataclass(frozen=True)
class DetectorRequires:
    maps: tuple = ()
    nuclear: bool = False
    cycles: bool = False
    pixel_size: bool = False
    external: str | None = None


@dataclass
class Candidate:
    detector: str
    detector_version: str
    class_hint: str
    scope_hint: str
    channels: tuple
    mask: np.ndarray
    score: float
    severity: float
    cycles: tuple = ()
    metrics: dict = field(default_factory=dict)
    primary_metric: str = ""
    evidence_channels: tuple = ()
    alternatives: list = field(default_factory=list)
    merged_from: list = field(default_factory=list)
    id: str | None = None
    #: The detector's own measure of how strong it is, unbounded (a robust z,
    #: a depth past the cut) -- `score` / `severity` saturate at 1.0, so on a
    #: large image nearly every candidate ties there. Ranks candidates within
    #: a scan (`candidates.build`); None for a detector that gives none.
    strength: float | None = None

    def mask_hash(self) -> str:
        packed = np.packbits(self.mask.astype(bool), axis=None)
        return hashlib.sha1(packed.tobytes() + str(self.mask.shape).encode()).hexdigest()

    def make_id(self, project) -> str:
        blob = "|".join([str(project), ",".join(self.channels), self.detector,
                         self.detector_version, self.mask_hash()])
        self.id = "cand_" + hashlib.sha1(blob.encode("utf-8")).hexdigest()[:10]
        return self.id


class DetectorContext:
    """What a detector may read: the scan and a few helpers."""

    def __init__(self, scan, *, project, params=None):
        self.scan = scan
        self.project = project
        self.params = params or {}
        self.pixel_um = scan.meta.get("pixel_um")
        self.cycles = scan.meta.get("cycles") or {}
        self.nuclear = scan.meta.get("nuclear")

    @property
    def channels(self):
        return [c["name"] for c in self.scan.channels]

    def channel_meta(self, name):
        return self.scan.channel(name)

    def markers(self):
        """Channels that are not nuclear stains and not flagged empty."""
        from plexora.plugins.qc.server.cycles import is_nuclear

        return [c["name"] for c in self.scan.channels
                if not is_nuclear(c["name"]) and "empty_channel" not in c["flags"]
                and "near_zero_plane" not in c["flags"]]

    def usable(self):
        return [c["name"] for c in self.scan.channels
                if "empty_channel" not in c["flags"] and "near_zero_plane" not in c["flags"]]

    def map(self, channel, metric):
        return self.scan.map(channel, metric)

    def tissue(self, core=False):
        return self.scan.tissue(core=core)

    def tissue_fraction(self):
        return self.scan.shared("tissue_fraction")

    def robust_z(self, values, *, core=True, floor=1e-9):
        from plexora.plugins.qc.server.scan import robust_z

        return robust_z(values, self.tissue(core=core), floor=floor)

    def cells_for_um2(self, um2, fallback=2):
        cell_um = self.scan.grid.get("cell_um")
        if not cell_um:
            return fallback
        return max(1, int(np.ceil(um2 / (cell_um * cell_um))))

    def cycle_of(self, channel):
        return (self.cycles.get("of_channel") or {}).get(channel)


class QCDetector(Protocol):
    name: str
    version: str
    classes_hint: tuple
    requires: DetectorRequires

    def available(self, context) -> tuple: ...

    def run(self, context) -> list: ...


def severity_from_z(z, z_cut, z_max):
    return float(np.clip((abs(z) - z_cut) / max(1e-9, z_max - z_cut), 0.0, 1.0))
