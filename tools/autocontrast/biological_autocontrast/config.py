"""Versions, defaults and where outputs go.

Every derived artefact (a proposal, a cached example, a label's domain record)
carries the versions it was made with, and every cache key includes them, so a
change to how a domain is detected or patches are sampled can never be served
from an older cache.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

VERSION = "0.1.0"
DOMAIN_VERSION = "domain-1"
SAMPLING_VERSION = "sampling-1"
FEATURES_VERSION = "features-1"
LABELS_VERSION = "labels-1"
#: The ONNX input/output contract. Bump when an input is added or reshaped.
MODEL_LAYOUT = "layout-1"

#: Display gamma. Plexora's renderer is linear everywhere (frag.glsl
#: `range_clamp`, source_image `blend_plane`), so the model never predicts one;
#: the field is carried so the record matches the spec's contract.
GAMMA = 1.0

ENV_HOME = "AUTOCONTRAST_HOME"


def home() -> Path:
    """Where runs, caches and label indexes live. Never the repo, never a synced folder."""
    return Path(os.environ.get(ENV_HOME) or Path.home() / "plexora-autocontrast")


@dataclass
class Config:
    # sampling
    n_patches: int = 24
    patch_size: int = 128
    quotas: dict = field(default_factory=lambda: {
        "high": 6, "intermediate": 6, "background": 4, "texture": 4, "random": 4})
    #: Patches are read at the level whose pixel size is nearest this.
    target_um_per_px: float = 1.0
    #: Patches never closer than this many footprints (centre to centre, Chebyshev).
    min_distance: float = 1.0
    #: The overview is the coarsest level with at least this many pixels.
    overview_min_pixels: int = 1_000_000
    overview_max_pixels: int = 16_000_000
    #: Above this a level-0 plane read is refused.
    max_plane_pixels: int = 120_000_000
    # abstain rules (all in canonical [0, 1] units unless named otherwise)
    dark_norm_max: float = 0.01
    min_tissue_fraction: float = 0.01
    saturated_flag: float = 0.05
    finite_warn: float = 0.99
    min_confidence: float = 0.0
    # heuristic used when no model is given
    heuristic: str = "percentile"

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    def digest(self) -> str:
        blob = json.dumps(self.to_dict(), sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:12]

    @classmethod
    def load(cls, path=None, **overrides) -> "Config":
        values = {}
        if path:
            text = Path(path).read_text(encoding="utf-8")
            if str(path).endswith((".yaml", ".yml")):
                import yaml

                values = yaml.safe_load(text) or {}
            else:
                values = json.loads(text)
        values.update({k: v for k, v in overrides.items() if v is not None})
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = sorted(set(values) - known)
        if unknown:
            raise ValueError(f"unknown config keys: {', '.join(unknown)}")
        return cls(**values)


def versions() -> dict:
    return {"domain_version": DOMAIN_VERSION, "sampling_version": SAMPLING_VERSION,
            "features_version": FEATURES_VERSION, "model_layout": MODEL_LAYOUT,
            "package_version": VERSION}
