"""Prefix fingerprints and the cache-hit monitor.

A prompt cache pays only while the prefix is byte-identical, and a silent
change (a timestamp, an unsorted dict, a reordered tool list) multiplies the
cost of every later call without failing anything. So every call is checked:
from a worker's second call on, the provider must report reading at least
the prefix from cache. A miss is logged with the fingerprint, counted in the
trace, and shown by `plexora ai trace --cache`.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field

from plexora.ai.harness.wire import Usage, canonical

log = logging.getLogger("plexora.ai.harness")

CHARS_PER_TOKEN = 3.5
#: Share of the expected prefix a warm call must read from cache.
HIT_SHARE = 0.9


def fingerprint(system: list) -> str:
    return hashlib.sha256(canonical(system).encode("utf-8")).hexdigest()[:16]


def expected_tokens(system: list) -> int:
    return int(len(canonical(system)) / CHARS_PER_TOKEN)


@dataclass
class CacheMonitor:
    """Verdicts per call: `cold` (first call of a worker, may write), `hit`,
    `miss` (a warm call that did not read the prefix)."""

    seen: dict = field(default_factory=dict)
    counts: dict = field(default_factory=lambda: {"cold": 0, "hit": 0, "miss": 0})
    usage: Usage = field(default_factory=Usage)

    def observe(self, prefix_fp: str, prefix_tokens: int, usage: Usage, *, warm: bool) -> str:
        for name in Usage.__dataclass_fields__:
            setattr(self.usage, name, getattr(self.usage, name) + getattr(usage, name))
        known = self.seen.get(prefix_fp, False)
        self.seen[prefix_fp] = True
        if not warm and not known:
            verdict = "cold"
        elif usage.cache_read >= HIT_SHARE * min(prefix_tokens, max(1, usage.input_total)):
            verdict = "hit"
        else:
            verdict = "miss"
            log.warning("prompt-cache miss on prefix %s: read %d of ~%d prefix tokens",
                        prefix_fp, usage.cache_read, prefix_tokens)
        self.counts[verdict] += 1
        return verdict

    def read_share(self) -> float:
        total = self.usage.input_total
        return self.usage.cache_read / total if total else 0.0
