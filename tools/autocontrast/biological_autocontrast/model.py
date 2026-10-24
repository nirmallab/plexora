"""ONNX inference (onnxruntime, CPU) and the abstain rules around it.

No torch here: prediction runs in the plain plexora environment. The rules
before the model are about the data (nothing to show), the ones after it about
the model (not sure enough).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from biological_autocontrast.config import MODEL_LAYOUT, Config

INPUTS = ("patches", "patch_cat", "patch_mask", "hist", "quant", "dom", "dom_mask")
OUTPUTS = ("min_norm", "max_norm", "confidence", "abstain")


class ContrastModel:
    def __init__(self, path):
        import onnxruntime as ort

        self.path = Path(path)
        options = ort.SessionOptions()
        options.intra_op_num_threads = 0
        self.session = ort.InferenceSession(str(self.path), options,
                                            providers=["CPUExecutionProvider"])
        self.checkpoint_id = hashlib.sha256(self.path.read_bytes()).hexdigest()[:12]
        meta = self.session.get_modelmeta().custom_metadata_map or {}
        self.meta = {k: v for k, v in meta.items()}
        layout = self.meta.get("model_layout")
        if layout and layout != MODEL_LAYOUT:
            raise ValueError(f"{self.path.name} was exported for {layout}; this code reads "
                             f"{MODEL_LAYOUT}")
        self.model_version = self.meta.get("model_version", "unknown")
        self.n_patches = int(self.meta.get("n_patches", 24))
        self.patch_size = int(self.meta.get("patch_size", 128))
        sidecar = self.path.with_name("metrics.json")
        self.metrics = json.loads(sidecar.read_text()) if sidecar.exists() else {}

    @property
    def valid(self) -> bool:
        return bool(self.metrics.get("valid", False))

    def predict(self, batch: dict) -> dict:
        feeds = {name: batch[name] for name in INPUTS}
        values = self.session.run(list(OUTPUTS), feeds)
        return {name: np.asarray(v, dtype=np.float64).reshape(-1) for name, v in zip(OUTPUTS, values)}


def data_abstain(features, ctx, cfg: Config):
    """(reason or None, flags list): the rules that need no model."""
    flags = []
    dom = features.domain
    if dom.finite_fraction < cfg.finite_warn:
        flags.append(f"only {dom.finite_fraction:.1%} of sampled pixels are finite")
    if dom.saturated_fraction > cfg.saturated_flag:
        flags.append(f"{dom.saturated_fraction:.1%} of sampled pixels are saturated")
    if dom.finite_fraction == 0:
        return "no_finite_pixels", flags
    if ctx.tissue is not None and ctx.tissue_fraction < cfg.min_tissue_fraction:
        return "no_tissue", flags
    top = features.raw_max_sampled
    if top is None or (top - dom.offset) / dom.span < cfg.dark_norm_max:
        return "dark_channel", flags
    return None, flags
