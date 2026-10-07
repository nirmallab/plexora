"""Tool-result caching (harness Layer 3): a read already answered is not run again.

Registry reads are idempotent for a given project revision and Plexora's
renders are deterministic, so `ToolResultCache.get_or_call` keys every
`read`-class capability call on

    (capability, canonical(arguments), project revision, Plexora version)

and hands back the stored answer instead of recomputing it -- across turns,
sub-agents, retries and conversations of the same project. The same bytes sent
to the model again also read from the provider's prompt cache.

What is never cached: a capability whose permission is not `read`; one whose
egress is `row_level` or `raw_pixels` (cell-level data is not copied onto disk
a second time); viewer state (`viewer_required`, `viewer.*`: what a tab shows
changes without a receipt); a job; and any failed call.

Invalidation is the revision (plexora/agent/revision.py), which every receipted
write bumps. A person's own edits in the viewer leave no receipt, so an entry
also expires after `max_age_s` (15 minutes by default).

Entries are content-addressed files, `<data_root>/.agent/ai/toolcache/<k[:2]>/
<key>.json`, images inline as base64. A hit is reported as `source: cache` to
the caller, which records it in the trace.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
from pathlib import Path

from plexora.ai.harness.wire import canonical

UNCACHED_EGRESS = ("row_level", "raw_pixels")
MAX_AGE_S = 15 * 60
MAX_ENTRY_BYTES = 24 * 1024 * 1024


def default_root() -> Path:
    from plexora import paths

    return paths.agent_root() / "ai" / "toolcache"


def plexora_version() -> str:
    try:
        from plexora import __version__

        return str(__version__)
    except Exception:                       # noqa: BLE001 -- a key part, never a failure
        import plexora

        return str(getattr(plexora, "__version__", "0"))


def cacheable(capability) -> bool:
    if capability is None or capability.permission != "read":
        return False
    if capability.egress in UNCACHED_EGRESS:
        return False
    if capability.viewer_required or capability.name.startswith("viewer.") or capability.execution == "job":
        return False
    return True


def _encode(value):
    if isinstance(value, (bytes, bytearray)):
        return {"__b64__": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(v) for v in value]
    return value


def _decode(value):
    if isinstance(value, dict):
        if set(value) == {"__b64__"}:
            return base64.b64decode(value["__b64__"])
        return {k: _decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode(v) for v in value]
    return value


class ToolResultCache:
    def __init__(self, root: Path | str | None = None, *, max_age_s: float = MAX_AGE_S,
                 version: str | None = None):
        self.root = Path(root) if root else default_root()
        self.max_age_s = max_age_s
        self.version = version or plexora_version()
        self.hits = 0
        self.misses = 0
        self._lock = threading.Lock()

    def key(self, capability: str, arguments: dict, project_revision: str) -> str:
        material = canonical({"capability": capability, "arguments": arguments or {},
                              "revision": str(project_revision), "version": self.version})
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def get(self, key: str):
        path = self._path(key)
        try:
            if self.max_age_s is not None and time.time() - path.stat().st_mtime > self.max_age_s:
                return None
            return _decode(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return None

    def put(self, key: str, outcome: dict) -> bool:
        try:
            text = json.dumps(_encode(outcome), separators=(",", ":"), ensure_ascii=False)
        except (TypeError, ValueError):
            return False
        if len(text) > MAX_ENTRY_BYTES:
            return False
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(text, encoding="utf-8")
        try:
            os.replace(tmp, path)
        except PermissionError:
            tmp.unlink(missing_ok=True)
            return False
        return True

    def get_or_call(self, capability, arguments: dict, project_revision: str, call):
        """`(outcome, source)`: the stored answer and `"cache"`, or `call()`'s and
        `"live"` (stored when it succeeded and the capability is cacheable).
        `capability` is a registry `Capability`; `call` takes no arguments and
        returns a `registry.invoke` outcome."""
        if not cacheable(capability):
            return call(), "live"
        key = self.key(capability.name, arguments, project_revision)
        stored = self.get(key)
        if stored is not None:
            with self._lock:
                self.hits += 1
            return stored, "cache"
        outcome = call()
        with self._lock:
            self.misses += 1
        if isinstance(outcome, dict) and outcome.get("ok"):
            self.put(key, outcome)
        return outcome, "live"

    def sweep(self, now: float | None = None) -> int:
        """Delete expired entries; returns how many."""
        now = now or time.time()
        removed = 0
        if not self.root.is_dir() or self.max_age_s is None:
            return 0
        for path in self.root.glob("*/*.json"):
            try:
                if now - path.stat().st_mtime > self.max_age_s:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed
