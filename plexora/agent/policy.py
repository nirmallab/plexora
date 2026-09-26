"""What an agent is allowed to do, decided before anything runs.

Four permission classes, from "just look" to "cannot be undone", and five
egress classes for what leaves the process. The defaults are the ones a user
who connected an agent and said nothing else would expect: it may read, look at
rendered pictures and change Plexora's own state (gates, regions -- all
reversible, all receipted), but it may not write into the user's source files
or delete anything without the server having been started to allow it AND the
call saying `confirm: true`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from plexora.agent.errors import AgentError

PERMISSIONS = ("read", "reversible_write", "source_file_write", "destructive")

#: What a result carries out of Plexora, least to most.
EGRESS = ("metadata", "aggregates", "row_level", "rendered_pixels", "raw_pixels")

DEFAULT_EGRESS = frozenset({"metadata", "aggregates", "rendered_pixels"})


@dataclass(frozen=True)
class Policy:
    allow_source_writes: bool = False
    allow_destructive: bool = False
    egress: frozenset = field(default_factory=lambda: DEFAULT_EGRESS)

    @classmethod
    def from_flags(cls, *, allow_source_writes=False, allow_destructive=False,
                   egress=None) -> "Policy":
        allowed = set(DEFAULT_EGRESS)
        for item in egress or ():
            item = item.strip()
            if not item:
                continue
            if item not in EGRESS:
                raise ValueError(f"unknown egress class {item!r}; expected any of {EGRESS}")
            allowed.add(item)
        return cls(bool(allow_source_writes), bool(allow_destructive), frozenset(allowed))

    def describe(self) -> dict:
        return {"allow_source_writes": self.allow_source_writes,
                "allow_destructive": self.allow_destructive,
                "egress": sorted(self.egress, key=EGRESS.index)}


def _confirmed(inp) -> bool:
    return bool(getattr(inp, "confirm", False))


def check(capability, inp, policy: Policy) -> None:
    """Raise `permission_required` when this call may not run. Writes nothing."""
    if capability.egress not in policy.egress:
        raise AgentError(
            "permission_required",
            f"{capability.name} returns {capability.egress} data, which this server "
            f"was not started to allow",
            detail={"egress": capability.egress,
                    "hint": f"restart with --egress {capability.egress}"})
    if capability.permission == "source_file_write":
        if not policy.allow_source_writes:
            raise AgentError(
                "permission_required",
                f"{capability.name} writes into the user's source file, and this server "
                "was not started with --allow-source-writes",
                detail={"permission": "source_file_write",
                        "hint": "ask the user; they restart the server with "
                                "--allow-source-writes"})
        if not _confirmed(inp):
            raise AgentError(
                "permission_required",
                f"{capability.name} writes into the user's source file; pass "
                "confirm: true once the user has explicitly asked for it",
                detail={"permission": "source_file_write", "needs": "confirm"})
    if capability.permission == "destructive":
        if not policy.allow_destructive:
            raise AgentError(
                "permission_required",
                f"{capability.name} cannot be undone, and this server was not started "
                "with --allow-destructive",
                detail={"permission": "destructive",
                        "hint": "ask the user; they restart the server with "
                                "--allow-destructive"})
        if not _confirmed(inp):
            raise AgentError(
                "permission_required",
                f"{capability.name} cannot be undone; pass confirm: true once the user "
                "has explicitly asked for it",
                detail={"permission": "destructive", "needs": "confirm"})


def permitted(capability, policy: Policy) -> bool:
    """Whether a call COULD run under this policy, given confirmation."""
    if capability.egress not in policy.egress:
        return False
    if capability.permission == "source_file_write":
        return policy.allow_source_writes
    if capability.permission == "destructive":
        return policy.allow_destructive
    return True


_WORD = re.compile(r"[a-z0-9]+")

#: Words that mean "change something" in a request.
_WRITE_WORDS = {"set", "change", "adjust", "move", "raise", "lower", "save", "write",
                "create", "draw", "delete", "remove", "update", "apply", "gate"}

#: Words too common in any request about tissue to say which capability it
#: needs ("cell" is in half the tool names and in every biology question).
_GENERIC = {"a", "an", "the", "of", "in", "on", "for", "to", "and", "or", "it", "is",
            "this", "that", "my", "me", "i", "you", "do", "does", "can", "please", "run",
            "analysis", "analyse", "analyze", "cell", "cells", "single", "set", "get",
            "list", "mode", "data", "use", "with", "all", "each", "some", "any"}

#: Words without which a sentence never reaches a source-file write or a delete.
_RISKY_WORDS = {"save", "write", "export", "anndata", "h5ad", "file", "delete", "remove",
                "erase", "uns"}


def classify_scope(session, request, *, project=None, policy: Policy | None = None,
                   capabilities=None):
    """Which of four answers a request gets, and why.

    `request` is either a list of capability names or a sentence. A sentence is
    matched against each capability's name, tags and purpose word by word --
    deliberately simple, because the agent reading the answer is the one that
    understands language; this only has to say what Plexora can do about it.

    - `can_execute`: every capability it needs exists, is permitted, and the
      project has what they require -- including a write.
    - `can_analyze`: it needs only reads, and they can run now.
    - `can_recommend`: Plexora knows how, but something is missing -- a
      requirement the project lacks, or a permission the server was not given.
      The answer names it.
    - `outside_domain`: nothing here does that. A good answer, not a failure.
    """
    from plexora.agent import registry

    policy = policy or Policy()
    known = capabilities if capabilities is not None else registry.all_capabilities()
    if isinstance(request, (list, tuple)):
        matched = [cap for cap in known if cap.name in request or cap.tool_name in request]
        unknown = [name for name in request
                   if not any(name in (cap.name, cap.tool_name) for cap in known)]
        words = set()
    else:
        words = set(_WORD.findall(str(request).lower())) - _GENERIC
        matched = []
        for cap in known:
            vocabulary = set(_WORD.findall(" ".join(
                (cap.name, cap.tool_name, cap.owner, " ".join(cap.tags))).lower()))
            if words & vocabulary:
                matched.append(cap)
        unknown = []
        wants_write = bool(words & _WRITE_WORDS)
        if not wants_write:
            reads = [cap for cap in matched if cap.permission == "read"]
            matched = reads or matched
        # Writing the user's files or deleting something is never implied by a
        # request that did not say so.
        if not words & _RISKY_WORDS:
            matched = [cap for cap in matched
                       if cap.permission not in ("source_file_write", "destructive")]

    if not matched:
        return {"state": "outside_domain", "capabilities": [], "unknown": unknown,
                "reason": "no Plexora capability does that"}

    missing = {}
    not_permitted = []
    record = None
    if project is not None:
        record = session.project(project)
    for cap in matched:
        if not permitted(cap, policy):
            not_permitted.append(cap.tool_name)
        if record is not None and cap.requires is not None:
            if not cap.requires.applies_to(record):
                missing[cap.tool_name] = [{"key": "applies", "label":
                                           "this project's image or layers do not fit"}]
                continue
            lacking = [r.describe() for r in cap.requires.missing_from(record)]
            if lacking:
                missing[cap.tool_name] = lacking
    names = [cap.tool_name for cap in matched]
    if missing or not_permitted or unknown:
        return {"state": "can_recommend", "capabilities": names, "missing": missing,
                "not_permitted": not_permitted, "unknown": unknown,
                "reason": "Plexora can do this once what is listed is supplied"}
    if all(cap.permission == "read" for cap in matched):
        return {"state": "can_analyze", "capabilities": names,
                "reason": "every capability needed only reads, and all can run now"}
    return {"state": "can_execute", "capabilities": names,
            "reason": "every capability needed exists, is permitted and can run now"}
