"""Prefix fingerprints and the cache-hit monitor: spatialagents', logging where
Plexora's harness logs.

A prompt cache pays only while the prefix is byte-identical, so every call is
checked (`spatialagents.cache_plan`): from a worker's second call on, the
provider must report reading at least the prefix from cache, and a miss is
logged with the fingerprint, counted in the trace, and shown by `plexora ai
trace --cache`. Plexora's monitor logs to `plexora.ai.harness`, beside the
decision loop's own lines, so Plexora's log configuration -- and its tests --
see a miss wherever it happens.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from spatialagents import cache_plan as _cache_plan
from spatialagents.cache_plan import (CHARS_PER_TOKEN, ESTIMATE_SLACK, HIT_SHARE, TEMPLATE_TOKENS, expected_tokens,
                                     fingerprint)

__all__ = ["CHARS_PER_TOKEN", "ESTIMATE_SLACK", "HIT_SHARE", "TEMPLATE_TOKENS", "CacheMonitor", "expected_tokens",
           "fingerprint", "log"]

log = logging.getLogger("plexora.ai.harness")


@dataclass
class CacheMonitor(_cache_plan.CacheMonitor):
    log: logging.Logger = field(default=log, repr=False, compare=False)
