"""The two inference calls magic select makes, behind a small protocol.

`SamBackend` is what `plexora.vision.sam` talks to: one `embed` per view
(the expensive image encoder) and any number of cheap `decode` calls against
that embedding. `OnnxSamBackend` runs the exported MobileSAM files on ONNX
Runtime; `FakeSamBackend` is a deterministic stand-in for tests that needs
neither the runtime nor the weights.

**Accelerators.** The encoder is a fixed-shape 1x3x1024x1024 graph, exactly
what a GPU compiles well: on an Apple M-series CoreML runs it ~4.7x faster
than four CPU threads (0.08 s against 0.37 s, measured 2026-10-04, max
difference 1e-6). The decoder takes a variable number of prompts, which
CoreML handles badly (it partitions the graph and lands slower than CPU), so
on a Mac only the encoder goes to the GPU. CUDA, ROCm and DirectML take
dynamic shapes well and run both graphs. Every provider choice falls back to
CPU when the session cannot be built or its first run fails, and the reason
is kept for `status()`. `PLEXORA_SEGMENT_DEVICE=cpu` forces CPU; `gpu`
refuses to start without one.

CoreML compiles the encoder on first load (~9 s); its compiled form is
cached under `<model dir>/cache/coreml` so later starts take ~2 s, and the
session is built on a background thread at server start, never inside the
first click when it can be helped.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Protocol

import numpy as np

ENV_DEVICE = "PLEXORA_SEGMENT_DEVICE"
DEVICE_CHOICES = ("auto", "cpu", "gpu")

EMBED_SHAPE = (1, 256, 64, 64)
LOW_RES = 256
INPUT_SIDE = 1024

#: Providers tried for the encoder, in order, when available. TensorRT is left
#: out on purpose: its engine build takes minutes, longer than anyone waits
#: for a click.
_ENCODER_GPU = ("CUDAExecutionProvider", "ROCMExecutionProvider",
                "MIGraphXExecutionProvider", "DmlExecutionProvider",
                "CoreMLExecutionProvider")
#: Providers trusted with the decoder's dynamic prompt axis.
_DECODER_GPU = ("CUDAExecutionProvider", "ROCMExecutionProvider",
                "MIGraphXExecutionProvider", "DmlExecutionProvider")


class SamRuntimeMissing(ImportError):
    """ONNX Runtime is not importable. `INSTALL` is the line to show somebody.

    It is a core dependency, so this only fires on a platform with no wheel or
    a broken environment.
    """

    INSTALL = "pip install --upgrade plexora"

    def __init__(self, cause=None):
        super().__init__(
            "Magic select needs the onnxruntime package, which is not "
            f"installed in this environment. Reinstall with: {self.INSTALL}")
        self.cause = cause


class SamBackend(Protocol):
    """embed: 1x3x1024x1024 float32 (normalised, padded) -> 1x256x64x64.

    decode: one embedding, B prompt sets of N points each ->
    (iou B x 4, low-resolution logits B x 4 x 256 x 256). Output 0 is the
    single-mask answer; 1..3 are the multimask candidates.
    """

    device: dict

    def embed(self, image: np.ndarray) -> np.ndarray: ...

    def decode(self, embedding: np.ndarray, coords: np.ndarray, labels: np.ndarray,
               mask_input: np.ndarray, has_mask: np.ndarray): ...


def requested_device() -> str:
    raw = (os.environ.get(ENV_DEVICE) or "auto").strip().lower()
    return raw if raw in DEVICE_CHOICES else "auto"


def import_runtime():
    try:
        import onnxruntime  # noqa: F401
    except Exception as exc:  # ImportError, or a broken native library
        raise SamRuntimeMissing(exc) from exc
    return onnxruntime


def _threads() -> int:
    try:
        from plexora import _resources

        cpus = _resources.allocated_cpus()
    except Exception:
        cpus = os.cpu_count() or 1
    return max(1, min(4, int(cpus)))


def _provider_entry(name: str, cache_dir: Path | None):
    if name == "CoreMLExecutionProvider":
        options = {"ModelFormat": "MLProgram", "MLComputeUnits": "ALL"}
        if cache_dir is not None:
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                options["ModelCacheDirectory"] = str(cache_dir)
            except OSError:
                pass
        return (name, options)
    if name == "CUDAExecutionProvider":
        # The default arena grows to fit the largest request and keeps it;
        # next-power-of-two growth would hold ~2x what the encoder needs.
        return (name, {"arena_extend_strategy": "kSameAsRequested"})
    return name


class OnnxSamBackend:
    """MobileSAM on ONNX Runtime, on a GPU when one is usable."""

    def __init__(self, encoder_path, decoder_path, *, threads=None, device=None,
                 cache_dir=None):
        ort = import_runtime()
        self._ort = ort
        self._threads = threads or _threads()
        self._device = device or requested_device()
        self._cache = Path(cache_dir) if cache_dir else None
        self._lock = threading.Lock()
        self._paths = {"encoder": str(encoder_path), "decoder": str(decoder_path)}
        available = set(ort.get_available_providers())
        want_gpu = self._device != "cpu"
        enc_gpu = [p for p in _ENCODER_GPU if p in available] if want_gpu else []
        dec_gpu = [p for p in _DECODER_GPU if p in available] if want_gpu else []
        if self._device == "gpu" and not enc_gpu:
            raise RuntimeError(
                f"{ENV_DEVICE}=gpu but this onnxruntime build offers no GPU "
                f"provider (has: {', '.join(sorted(available))})")
        self.device = {"requested": self._device, "fallback": None}
        self._encoder, self.device["encoder"] = self._build(
            str(encoder_path), enc_gpu, "encoder")
        self._decoder, self.device["decoder"] = self._build(
            str(decoder_path), dec_gpu, "decoder")
        self.device["gpu"] = (self.device["encoder"] != "CPUExecutionProvider"
                              or self.device["decoder"] != "CPUExecutionProvider")

    def _options(self, provider: str):
        ort = self._ort
        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        options.intra_op_num_threads = self._threads
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.log_severity_level = 3
        # A spinning pool burns a core between clicks for nothing.
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        if provider == "DmlExecutionProvider":
            options.enable_mem_pattern = False
        return options

    def _build(self, path: str, gpu: list, role: str):
        ort = self._ort
        cache = self._cache / "coreml" if self._cache is not None else None
        for provider in gpu:
            try:
                session = ort.InferenceSession(
                    path, self._options(provider),
                    providers=[_provider_entry(provider, cache), "CPUExecutionProvider"])
                if provider in session.get_providers():
                    return session, provider
            except Exception as exc:
                self.device["fallback"] = f"{role}: {provider} failed ({exc})"[:300]
        session = ort.InferenceSession(
            path, self._options("CPUExecutionProvider"),
            providers=["CPUExecutionProvider"])
        return session, "CPUExecutionProvider"

    def _rebuild_cpu(self, role: str, exc: Exception):
        """A GPU session that built but failed to run: CPU from now on."""
        cpu = self._ort.InferenceSession(
            self._paths[role], self._options("CPUExecutionProvider"),
            providers=["CPUExecutionProvider"])
        self.device["fallback"] = f"{role}: {self.device[role]} failed at run ({exc})"[:300]
        self.device[role] = "CPUExecutionProvider"
        self.device["gpu"] = (self.device["encoder"] != "CPUExecutionProvider"
                              or self.device["decoder"] != "CPUExecutionProvider")
        if role == "encoder":
            self._encoder = cpu
        else:
            self._decoder = cpu
        return cpu

    def embed(self, image: np.ndarray) -> np.ndarray:
        feed = {"image": np.ascontiguousarray(image, dtype=np.float32)}
        try:
            return self._encoder.run(None, feed)[0]
        except Exception as exc:
            if self.device["encoder"] == "CPUExecutionProvider":
                raise
            with self._lock:
                session = self._rebuild_cpu("encoder", exc)
            return session.run(None, feed)[0]

    def decode(self, embedding, coords, labels, mask_input, has_mask):
        feed = {
            "image_embeddings": np.ascontiguousarray(embedding, dtype=np.float32),
            "point_coords": np.ascontiguousarray(coords, dtype=np.float32),
            "point_labels": np.ascontiguousarray(labels, dtype=np.float32),
            "mask_input": np.ascontiguousarray(mask_input, dtype=np.float32),
            "has_mask_input": np.ascontiguousarray(has_mask, dtype=np.float32).reshape(1),
        }
        try:
            iou, low = self._decoder.run(None, feed)
        except Exception as exc:
            if self.device["decoder"] == "CPUExecutionProvider":
                raise
            with self._lock:
                session = self._rebuild_cpu("decoder", exc)
            iou, low = session.run(None, feed)
        return iou, low


class FakeSamBackend:
    """A deterministic stand-in: segments bright objects, needs nothing.

    `embed` packs the input's 256x256 luminance into the embedding (so tests
    can prove an embedding was reused rather than recomputed); `decode`
    answers each prompt set with the bright connected region under the first
    positive point, minus discs around negatives, or the ellipse inscribed in
    a box prompt. Every call is recorded in `calls`.
    """

    def __init__(self, threshold=None, on_embed=None):
        self.threshold = threshold
        self.on_embed = on_embed
        self.calls: list[tuple] = []
        self.device = {"requested": "cpu", "encoder": "fake", "decoder": "fake",
                       "gpu": False, "fallback": None}

    def embed(self, image):
        if self.on_embed is not None:
            self.on_embed()
        self.calls.append(("embed", image.shape))
        lum = image[0].mean(axis=0)  # 1024x1024, normalised units
        small = lum.reshape(LOW_RES, 4, LOW_RES, 4).mean(axis=(1, 3))
        out = np.zeros(EMBED_SHAPE, np.float32)
        out.reshape(-1)[: LOW_RES * LOW_RES] = small.reshape(-1)
        return out

    def decode(self, embedding, coords, labels, mask_input, has_mask):
        import cv2

        self.calls.append(("decode", coords.shape))
        lum = embedding.reshape(-1)[: LOW_RES * LOW_RES].reshape(LOW_RES, LOW_RES)
        content = lum != 0
        level = self.threshold
        if level is None:
            level = float(lum[content].mean()) if content.any() else 0.0
        bright = (lum > level).astype(np.uint8)
        _, components = cv2.connectedComponents(bright, connectivity=8)
        batch = coords.shape[0]
        out = np.full((batch, 4, LOW_RES, LOW_RES), -8.0, np.float32)
        yy, xx = np.mgrid[0:LOW_RES, 0:LOW_RES]
        for b in range(batch):
            pts = coords[b] / 4.0
            lab = labels[b]
            mask = np.zeros((LOW_RES, LOW_RES), bool)
            corners = pts[(lab == 2) | (lab == 3)]
            if len(corners) == 2:
                (x0, y0), (x1, y1) = corners
                cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
                rx, ry = max(abs(x1 - x0) / 2, 1), max(abs(y1 - y0) / 2, 1)
                mask |= ((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2 <= 1
            for (x, y), label in zip(pts, lab):
                if label != 1:
                    continue
                ix = int(np.clip(round(x), 0, LOW_RES - 1))
                iy = int(np.clip(round(y), 0, LOW_RES - 1))
                if bright[iy, ix]:
                    mask |= components == components[iy, ix]
                else:
                    mask |= (xx - x) ** 2 + (yy - y) ** 2 <= 12 ** 2
            for (x, y), label in zip(pts, lab):
                if label == 0:
                    mask &= (xx - x) ** 2 + (yy - y) ** 2 > 6 ** 2
            logits = np.where(mask, 8.0, -8.0).astype(np.float32)
            out[b, :] = logits
        iou = np.tile(np.array([[0.9, 0.7, 0.8, 0.6]], np.float32), (batch, 1))
        return iou, out
