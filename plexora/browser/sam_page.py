"""Magic select on the website: the model runs in the page, the rest here.

Plexora's Python runs in Pyodide on the website, where there is no ONNX
Runtime to import. The page has one, onnxruntime-web, on the GPU through
WebGPU when the browser offers an adapter and on WebAssembly threads
otherwise (client services/onnxExecutor.js, workers/onnxWorker.js). It keeps
MobileSAM's two files in the browser's own storage, fetched once and checked
against the sha256 pins in `vision/sam_weights.py`, which it is handed with
every question.

`PageExecutor` is the `sam_backend.Executor` the boot installs: everything
`vision/sam.py` would ask the files on disk and the download thread, it asks
the page instead -- one `sam` question per call through the worker's ask
channel (`ask(kind, args)`, plexora/browser/boot.py). `PageSamBackend` is the
session: only the encoder's and the decoder's run cross, as arrays, and the
pre- and post-processing (`sam.preprocess_into`, `sam.predict_batch`,
`segment.outline`) stay Python's, unchanged.

An image embedding (4 MB) crosses once: the page keeps the last few under a
key, and a decoder call names the key; when the page has let it go (or was
reloaded) it says so and the embedding is sent with the call.

Arrays cross as `{"dtype", "shape", "data"}`: numpy arrays at the top level of
an ask become that, their bytes in a typed array (the worker's `_ask`), and
the page answers with the same shape.
"""

from __future__ import annotations

import weakref

import numpy as np

from plexora.vision import sam_backend, sam_weights

KIND = "sam"

#: What a page that cannot run the model at all is reported as.
NO_PAGE = "This page cannot run magic select (it did not answer)."


def _plain(value):
    """A page's answer as Python values (a JS object arrives as a proxy)."""
    to_py = getattr(value, "to_py", None)
    return to_py() if callable(to_py) else value


def array(entry) -> np.ndarray:
    """`{"dtype", "shape", "data"}` (data: bytes, a memoryview, a typed array's
    copy) -> a numpy array of that shape."""
    data = entry["data"]
    if hasattr(data, "to_py"):
        data = data.to_py()
    dtype = np.dtype(entry.get("dtype") or "float32")
    if isinstance(data, np.ndarray):
        flat = data.reshape(-1)
    else:
        # Whatever the typed array was, its bytes.
        flat = np.frombuffer(memoryview(data).cast("B"), dtype=np.uint8)
    if flat.dtype != dtype:
        flat = flat.view(dtype) if flat.dtype == np.uint8 else flat.astype(dtype)
    return flat.reshape([int(n) for n in entry["shape"]])


def pins() -> dict:
    """The model as the page fetches and checks it."""
    return {"name": sam_weights.MODEL_NAME, "version": sam_weights.MODEL_VERSION,
            "dir": sam_weights.WEB_DIR,
            "files": [{"name": name, "role": entry["role"], "sha256": entry["sha256"],
                       "bytes": entry["bytes"]}
                      for name, entry in sam_weights.MODEL_FILES.items()]}


class PageError(RuntimeError):
    """The page could not do what was asked; its reason is the message."""


class PageExecutor:
    """`sam_backend.Executor` over the page's onnxruntime-web."""

    def __init__(self, ask):
        self._ask = ask

    def _call(self, op: str, **args):
        return _plain(self._ask(KIND, {"op": op, **args}))

    def status(self) -> dict:
        said = self._call("status", model=pins())
        if not said:
            return {"runtime": {"installed": False, "install": None}, "hint": NO_PAGE}
        return said

    def install(self) -> None:
        self._call("install", model=pins())

    def cancel(self) -> None:
        self._call("cancel")

    def remove(self) -> list:
        said = self._call("remove", model=pins()) or {}
        return list(said.get("removed") or [])

    def backend(self) -> "PageSamBackend":
        said = self._call("load", model=pins(), device=sam_backend.requested_device())
        if not said:
            raise PageError(NO_PAGE)
        if said.get("missing"):
            raise sam_weights.SamWeightsMissing(list(said["missing"]))
        if said.get("error"):
            raise PageError(str(said["error"]))
        return PageSamBackend(self._ask, said.get("device") or {})


class PageSamBackend:
    """`sam_backend.SamBackend` whose two runs are the page's."""

    #: Keys of embeddings the page holds, kept at most this many (the page
    #: keeps as many as segment.py's cache does).
    KEYS = 32

    def __init__(self, ask, device: dict):
        self._ask = ask
        self.device = {"requested": sam_backend.requested_device(), **dict(device)}
        self._keys: dict[int, tuple] = {}
        #: Diagnostics: (op, page ms) of the latest runs, newest last.
        self.runs: list[tuple[str, float]] = []

    def _run(self, op: str, **args) -> dict:
        said = _plain(self._ask(KIND, {"op": op, **args}))
        if not said:
            raise PageError(NO_PAGE)
        if said.get("error"):
            raise PageError(str(said["error"]))
        if said.get("device"):
            self.device.update(said["device"])
        if said.get("ms") is not None:
            self.runs.append((op, float(said["ms"])))
            del self.runs[:-50]
        return said

    def _key_of(self, embedding) -> str | None:
        held = self._keys.get(id(embedding))
        if held is None or held[0]() is not embedding:
            return None
        return held[1]

    def _remember(self, embedding: np.ndarray, key) -> None:
        if not key:
            return
        self._keys[id(embedding)] = (weakref.ref(embedding), str(key))
        if len(self._keys) > self.KEYS:
            for name in [k for k, (ref, _key) in self._keys.items() if ref() is None]:
                del self._keys[name]
            while len(self._keys) > self.KEYS:
                del self._keys[next(iter(self._keys))]

    def embed(self, image: np.ndarray) -> np.ndarray:
        said = self._run("embed", image=np.ascontiguousarray(image, dtype=np.float32))
        embedding = array(said["embedding"]).astype(np.float32, copy=False)
        self._remember(embedding, said.get("key"))
        return embedding

    def decode(self, embedding, coords, labels, mask_input, has_mask):
        prompts = {"coords": np.ascontiguousarray(coords, dtype=np.float32),
                   "labels": np.ascontiguousarray(labels, dtype=np.float32),
                   "mask_input": np.ascontiguousarray(mask_input, dtype=np.float32),
                   "has_mask": np.ascontiguousarray(has_mask, dtype=np.float32).reshape(1)}
        key = self._key_of(embedding)
        said = self._run("decode", key=key, **prompts) if key else None
        if said is None or said.get("missing"):
            said = self._run("decode", embedding=np.ascontiguousarray(embedding, dtype=np.float32),
                             **prompts)
            if isinstance(embedding, np.ndarray):
                self._remember(embedding, said.get("key"))
        return array(said["iou"]), array(said["low"])


def install(ask) -> PageExecutor | None:
    """Make the page's onnxruntime-web magic select's runtime (None: undo)."""
    made = PageExecutor(ask) if ask is not None else None
    sam_backend.set_executor(made)
    return made
