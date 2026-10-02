"""Prefix fingerprints and the cache-hit monitor.

A prompt cache pays only while the prefix is byte-identical, and a silent
change (a timestamp, an unsorted dict, a reordered tool list) multiplies the
cost of every later call without failing anything. So every call is checked:
from a worker's second call on, the provider must report reading at least
the prefix from cache. A miss is logged with the fingerprint, counted in the
trace, and shown by `plexora ai trace --cache`.

The expected size is an estimate (characters / 3.5), and tool schemas and
prose tokenize denser than that, while providers count cached reads in
blocks (128 tokens at OpenAI-style caches). So the estimate is only a rough
first bar; once the provider has reported reading the prefix on a worker's
first call -- which reads only the system prompt another worker wrote -- that
size is the bar. A worker's later calls also read its own earlier turns (the
rolling breakpoints, wire.with_breakpoints), so their reads and every write
are prefix plus history and never set the bar. A model whose provider has
never reported caching anything for a prefix gets the verdict `uncached`:
nothing changed in the prefix, and a warning per call would say nothing new.

Some engines (vLLM / SGLang behind Chat Completions) report the chat
template's first few tokens as cached on every call, even a brand-new prompt:
a read or write of TEMPLATE_TOKENS or fewer is that, not a cached prefix. And
a best-effort cache (SayGM's TEE models hit or miss with their own load) can
miss call after call on a prefix that never changed: each prefix warns once,
and the rest are counted in the trace.
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
#: How far below the characters-based estimate a prefix's real token count may
#: be, before any provider-reported size is known for it.
ESTIMATE_SLACK = 0.7
#: Cached tokens at or under this are the chat template's opening, not a prefix.
TEMPLATE_TOKENS = 32


def fingerprint(system: list) -> str:
    return hashlib.sha256(canonical(system).encode("utf-8")).hexdigest()[:16]


def expected_tokens(system: list) -> int:
    return int(len(canonical(system)) / CHARS_PER_TOKEN)


@dataclass
class CacheMonitor:
    """Verdicts per call: `cold` (first call of a worker, may write), `hit`,
    `miss` (a warm call that did not read the prefix), `uncached` (a warm call
    on a prefix the provider has never reported caching at all)."""

    seen: dict = field(default_factory=dict)
    counts: dict = field(default_factory=lambda: {"cold": 0, "hit": 0, "miss": 0, "uncached": 0})
    usage: Usage = field(default_factory=Usage)
    #: The prefix's real cached size per prefix, from what a worker's first
    #: call read: it replaces the estimate as the bar.
    cached: dict = field(default_factory=dict)
    #: The prefixes the provider has reported reading or writing anything for.
    caching: dict = field(default_factory=dict)
    #: The prefixes a miss has already been logged for.
    warned: set = field(default_factory=set)

    def observe(self, prefix_fp: str, prefix_tokens: int, usage: Usage, *, warm: bool) -> str:
        for name in Usage.__dataclass_fields__:
            setattr(self.usage, name, getattr(self.usage, name) + getattr(usage, name))
        read = usage.cache_read if usage.cache_read > TEMPLATE_TOKENS else 0
        wrote = usage.cache_write if usage.cache_write > TEMPLATE_TOKENS else 0
        known = self.seen.get(prefix_fp, False)
        self.seen[prefix_fp] = True
        reported = self.cached.get(prefix_fp, 0)
        if reported:
            bar = HIT_SHARE * min(reported, prefix_tokens)
        else:
            bar = HIT_SHARE * ESTIMATE_SLACK * prefix_tokens
        bar = min(bar, HIT_SHARE * max(1, usage.input_total))
        if not warm and not known:
            verdict = "cold"
        elif read and read >= bar:
            verdict = "hit"
        elif not self.caching.get(prefix_fp) and not read and not wrote:
            verdict = "uncached"
            if not self.counts["uncached"]:
                log.info("prompt caching not reported on prefix %s: the provider wrote and read nothing",
                         prefix_fp)
        else:
            verdict = "miss"
            if prefix_fp not in self.warned:
                self.warned.add(prefix_fp)
                log.warning("prompt-cache miss on prefix %s: read %d of ~%d prefix tokens "
                            "(further misses on it are counted in the trace, not logged)",
                            prefix_fp, usage.cache_read, int(reported or prefix_tokens))
        if read or wrote:
            self.caching[prefix_fp] = True
        if read and not warm:
            self.cached[prefix_fp] = max(reported, read)
        self.counts[verdict] += 1
        return verdict

    def read_share(self) -> float:
        total = self.usage.input_total
        return self.usage.cache_read / total if total else 0.0
