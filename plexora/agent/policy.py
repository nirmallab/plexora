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

import dataclasses
import re
from dataclasses import dataclass, field

from plexora.agent.errors import AgentError

PERMISSIONS = ("read", "reversible_write", "source_file_write", "destructive")

#: What a result carries out of Plexora, least to most.
EGRESS = ("metadata", "aggregates", "row_level", "rendered_pixels", "raw_pixels")

DEFAULT_EGRESS = frozenset({"metadata", "aggregates", "rendered_pixels"})

#: What `validate_scope` answers, most capable first.
SCOPE_STATES = ("can_execute", "can_analyze", "can_recommend", "outside_domain")
CAN_EXECUTE, CAN_ANALYZE, CAN_RECOMMEND, OUTSIDE_DOMAIN = SCOPE_STATES


@dataclass(frozen=True)
class Policy:
    allow_source_writes: bool = False
    allow_destructive: bool = False
    egress: frozenset = field(default_factory=lambda: DEFAULT_EGRESS)
    #: False: reads only -- a `read`-scoped token's policy.
    allow_writes: bool = True
    #: Who is calling, when the transport knows (a token's id); in audit lines.
    principal: str | None = None

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
                "allow_writes": self.allow_writes,
                "egress": sorted(self.egress, key=EGRESS.index),
                "principal": self.principal}

    def narrowed_by_scope(self, scope: str, principal: str | None = None) -> "Policy":
        """This policy as a token of `scope` may use it: narrower, never wider.

        `read` allows no writes at all; `write` allows Plexora's own reversible
        state but no source-file writes or deletes; `admin` leaves the server's
        policy as it is. An unknown scope is treated as `read`.
        """
        if scope == "admin":
            return dataclasses.replace(self, principal=principal)
        if scope == "write":
            return dataclasses.replace(self, allow_source_writes=False,
                                       allow_destructive=False, principal=principal)
        return dataclasses.replace(self, allow_writes=False, allow_source_writes=False,
                                   allow_destructive=False, principal=principal)


def _confirmed(inp) -> bool:
    return bool(getattr(inp, "confirm", False))


def _exact_reversal(capability, arguments, undo_of) -> bool:
    """Whether this call is exactly the undo hint of a receipted operation."""
    if not undo_of:
        return False
    hint = undo_of.get("undo_hint") or {}
    return (hint.get("tool") in (capability.tool_name, capability.name)
            and dict(hint.get("arguments") or {}) == dict(arguments or {}))


def check(capability, inp, policy: Policy, *, undo_of=None, arguments=None) -> None:
    """Raise `permission_required` when this call may not run. Writes nothing.

    One relaxation, and only for `destructive`: a call that is exactly the
    undo hint of an operation this log receipted (`undo_operation` replaying
    it, revision checked) needs its `confirm` but not `--allow-destructive` --
    deleting the region the agent itself just drew is putting things back,
    not destroying the user's work. A source-file write is never relaxed.
    """
    if capability.egress not in policy.egress:
        raise AgentError(
            "permission_required",
            f"{capability.name} returns {capability.egress} data, which this server "
            f"was not started to allow",
            detail={"egress": capability.egress,
                    "hint": f"restart with --egress {capability.egress}"})
    if capability.permission != "read" and not policy.allow_writes:
        raise AgentError(
            "permission_required",
            f"{capability.name} changes state, and this connection may only read",
            detail={"permission": capability.permission, "scope": "read",
                    "hint": "ask the user for a token with --scope write"})
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
        if not policy.allow_destructive and not _exact_reversal(capability, arguments,
                                                                undo_of):
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
    if capability.permission != "read" and not policy.allow_writes:
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

#: English function words, ignored when a request is matched against a
#: capability's purpose (prose, unlike names and tags, is full of them).
_PROSE = {"are", "was", "were", "be", "been", "has", "have", "had", "what", "which", "who",
          "how", "why", "when", "where", "there", "these", "those", "they", "them", "their",
          "its", "from", "by", "at", "as", "but", "not", "no", "yes", "so", "if", "than",
          "then", "into", "out", "up", "down", "about", "over", "under", "one", "two",
          "many", "much", "more", "most", "less", "only", "also", "just", "very", "too",
          "own", "same", "other", "such", "we", "our", "your", "us", "he", "she", "his",
          "her", "will", "would", "should", "could", "may", "might", "must", "shall",
          "let", "show", "tell", "give", "look", "see", "find", "make", "need", "want",
          "like", "now", "here", "true", "false", "optionally", "every", "without"}


def _vocabulary(cap) -> set:
    return set(_WORD.findall(" ".join(
        (cap.name, cap.tool_name, cap.owner, " ".join(cap.tags))).lower()))


def _purpose_words(cap) -> set:
    return {word for word in _WORD.findall(cap.purpose.lower())
            if len(word) > 2 and word not in _PROSE and word not in _GENERIC}


def _narrow(matched, words):
    """Reads unless the request says to change something; never a source-file
    write or a delete unless it says so."""
    if not words & _WRITE_WORDS:
        reads = [cap for cap in matched if cap.permission == "read"]
        matched = reads or matched
    # Writing the user's files or deleting something is never implied by a
    # request that did not say so.
    if not words & _RISKY_WORDS:
        matched = [cap for cap in matched
                   if cap.permission not in ("source_file_write", "destructive")]
    return matched


def _project_markers(session, project):
    if project is None:
        return []
    try:
        record = session.project(project)
    except AgentError:
        return []
    columns = getattr(record, "columns", None)
    return list(columns.markers) if columns is not None and columns.classified else []


def classify_scope(session, request, *, project=None, policy: Policy | None = None,
                   capabilities=None):
    """Which of four answers a request gets, and why.

    `request` is either a list of capability names or a sentence. A sentence is
    matched in tiers, each tried only when the one before found nothing --
    deliberately simple, because the agent reading the answer is the one that
    understands language; this only has to say what Plexora can do about it:

    1. the words of each capability's name, tool name, owner and tags;
    2. the content words of each capability's purpose;
    3. the biological task the words are about (`plexora.agent.tasks`): a task
       Plexora serves answers `can_recommend` with what to establish first, a
       task it does not answers `outside_domain` with the reason.

    - `can_execute`: every capability it needs exists, is permitted, and the
      project has what they require -- including a write.
    - `can_analyze`: it needs only reads, and they can run now.
    - `can_recommend`: Plexora knows how, but something is missing -- a
      requirement the project lacks, a permission the server was not given, or
      (for a task-phrased request) a choice the agent must make first. The
      answer names it.
    - `outside_domain`: nothing here does that. A good answer, not a failure.

    Every sentence answer says which tier matched (`matched_by`).
    """
    from plexora.agent import registry
    from plexora.agent import tasks

    policy = policy or Policy()
    known = capabilities if capabilities is not None else registry.all_capabilities()
    matched_by = "name"
    task = None
    establish = []
    if isinstance(request, (list, tuple)):
        matched = [cap for cap in known if cap.name in request or cap.tool_name in request]
        unknown = [name for name in request
                   if not any(name in (cap.name, cap.tool_name) for cap in known)]
        words = set()
    else:
        unknown = []
        all_words = set(_WORD.findall(str(request).lower()))
        words = all_words - _GENERIC
        matched = [cap for cap in known if words & _vocabulary(cap)]
        matched_by = "tags"
        if not matched:
            content = words - _PROSE
            matched = [cap for cap in known if content & _purpose_words(cap)]
            matched_by = "purpose"
        if not matched:
            task = tasks.task_for(all_words)
            matched_by = "task"
            if task is not None and not task.tags:
                return {"state": OUTSIDE_DOMAIN, "capabilities": [], "unknown": [],
                        "matched_by": "task", "task": task.name, "reason": task.reason}
            if task is not None:
                matched = [cap for cap in known if set(cap.tags) & set(task.tags)]
                named = tasks.marker_terms(all_words, _project_markers(session, project))
                establish = [item for item in task.establish
                             if not (item["key"] == "marker" and named)]
        matched = _narrow(matched, words)

    if not matched:
        return {"state": OUTSIDE_DOMAIN, "capabilities": [], "unknown": unknown,
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
    how = {} if isinstance(request, (list, tuple)) else {"matched_by": matched_by}
    if task is not None:
        how["task"] = task.name
    if establish:
        missing["_task"] = list(establish)
    if missing or not_permitted or unknown:
        return {"state": CAN_RECOMMEND, "capabilities": names, "missing": missing,
                "not_permitted": not_permitted, "unknown": unknown, **how,
                "reason": "Plexora can do this once what is listed is supplied"}
    if all(cap.permission == "read" for cap in matched):
        return {"state": CAN_ANALYZE, "capabilities": names, **how,
                "reason": "every capability needed only reads, and all can run now"}
    return {"state": CAN_EXECUTE, "capabilities": names, **how,
            "reason": "every capability needed exists, is permitted and can run now"}
