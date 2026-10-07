"""Plexora's tool-result cache: spatialagents', under Plexora's data root, keyed
on Plexora's version, holding what the registry may cache.

Registry reads are idempotent for a given project revision and Plexora's
renders are deterministic, so a `read`-class capability call is answered from
`spatialagents.toolcache.ToolResultCache` when it was answered before for

    (capability, canonical(arguments), project revision, Plexora version)

What is never cached (`cacheable`): a capability whose permission is not
`read`; one whose egress is `row_level` or `raw_pixels` (cell-level data is not
copied onto disk a second time); viewer state (`viewer_required`, `viewer.*`:
what a tab shows changes without a receipt); a job; and any failed call.

Invalidation is the revision (plexora/agent/revision.py), which every receipted
write bumps. A person's own edits in the viewer leave no receipt, so an entry
also expires after 15 minutes. Entries live under
`<data_root>/.agent/ai/toolcache/`.
"""

from __future__ import annotations

from pathlib import Path

from spatialagents import toolcache as _toolcache
from spatialagents.toolcache import MAX_AGE_S, MAX_ENTRY_BYTES

__all__ = ["MAX_AGE_S", "MAX_ENTRY_BYTES", "UNCACHED_EGRESS", "ToolResultCache", "cacheable", "default_root",
           "plexora_version"]

UNCACHED_EGRESS = ("row_level", "raw_pixels")


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


class ToolResultCache(_toolcache.ToolResultCache):
    def __init__(self, root: Path | str | None = None, *, max_age_s: float | None = MAX_AGE_S,
                 version: str | None = None):
        super().__init__(Path(root) if root else default_root(), version=version or plexora_version(),
                         max_age_s=max_age_s)
