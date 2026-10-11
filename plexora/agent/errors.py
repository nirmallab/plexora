"""The one shape a failure reaches an agent in.

An agent cannot read a traceback the way a person reads a dialog, and it should
not have to: every failure here is a `code` it can branch on, a `message` it can
repeat to the user, a `detail` with whatever makes the failure actionable (the
requirements a project is missing, the revision somebody else wrote, the node
that is asleep), and whether trying again could help.

`AgentError` itself is spatialagents' (`spatialagents.errors`), the one class
every BioCognia product's agents and harness raise, so a failure raised in the
shared runtime and one raised by a capability are caught alike. Its closed
list of codes is the shared one plus each product's: Plexora's are registered
here, at import, so every module that raises one has imported this first.
`as_agent_error` -- what an exception means in Plexora -- stays Plexora's.
"""

from __future__ import annotations

from spatialagents.errors import AgentError, register

__all__ = ["CODES", "AgentError", "as_agent_error"]

#: Every code a capability may fail with. A closed list, so an agent's
#: instructions (plexora/ai/skills) can name them and mean something.
CODES = (
    "unknown_project",
    "unknown_capability",
    "invalid_input",
    "precondition_missing",
    "resource_unavailable",
    "resource_not_local",
    "viewer_not_available",
    "viewer_not_responding",
    "ambiguous_view",
    "permission_required",
    # A Paid capability on a licence that does not unlock it. `detail` names
    # the entitlement, the plan that unlocks it and the licence state
    # (plexora/licensing/guards.py). Not retryable: a licence is not something
    # trying again fixes.
    "license_required",
    "conflict",
    "capability_unavailable",
    "unsupported_modality",
    # A volume: the capability's 2-D code cannot run on the picture it was
    # given (a maximum projection handed to a tool that measures one plane).
    # `detail` lists what it does accept and how to ask for it
    # (plexora/api/representation.py). Not retryable as asked.
    "representation_unsupported",
    # A volume: the conclusion asked for cannot be drawn from that picture at
    # all -- a z position, a colocalization or a 3-D object out of a
    # projection. `detail.inference` names which; the message says why.
    "inference_forbidden",
    "too_large",
    "internal_error",
)

register(*CODES)


def as_agent_error(exc: BaseException) -> AgentError:
    """Any exception, as the AgentError it most honestly is.

    The mapping is from what the exception MEANS in Plexora, not from its
    type alone: a node that is unreachable is worth retrying, a resource that is
    on another machine is not, and anything unrecognised is an internal error
    rather than being passed off as the caller's mistake.
    """
    if isinstance(exc, AgentError):
        return exc
    if type(exc).__name__ == "BridgeError" and \
            type(exc).__module__.startswith("spatialbridge"):
        # The protocol library's own failure (a peer unreachable, a stale
        # workspace), raised by a bridge capability. Recognised by name so this
        # module never imports the optional package.
        from plexora.agent.bridge_wire import agent_error

        return agent_error(exc)

    from pydantic import ValidationError

    from plexora.server.providers.base import (RemoteUnreachable, ResourceNotLocal,
                                               ResourceUnavailable)

    if isinstance(exc, ValidationError):
        return AgentError("invalid_input", "the arguments did not validate",
                          detail=[{"loc": list(e.get("loc", ())), "msg": e.get("msg")}
                                  for e in exc.errors()])
    if isinstance(exc, RemoteUnreachable):
        return AgentError("resource_unavailable", str(exc) or "a data node is unreachable",
                          detail={"node": getattr(exc, "node", None)}, retryable=True)
    if isinstance(exc, ResourceUnavailable):
        return AgentError("resource_unavailable", str(exc) or "a resource is unavailable",
                          detail={"node": getattr(exc, "node", None),
                                  "resource": getattr(exc, "resource", None)},
                          retryable=True)
    if isinstance(exc, ResourceNotLocal):
        return AgentError("resource_not_local", str(exc))

    try:
        from plexora.server.providers.operations import UnknownOperation
    except ImportError:  # pragma: no cover
        UnknownOperation = ()
    if UnknownOperation and isinstance(exc, UnknownOperation):
        return AgentError("capability_unavailable", str(exc))

    try:
        from plexora.server.utils.source_image import RenderError
    except ImportError:  # pragma: no cover
        RenderError = ()
    if RenderError and isinstance(exc, RenderError):
        return AgentError("invalid_input", str(exc))

    foreign = _raised_outside(exc)
    if foreign is not None:
        # A library refusing what Plexora handed it (Pillow drawing an
        # inverted rectangle, say) is Plexora's bug, not the caller's input:
        # said as `invalid_input` it reads as "your answer was wrong".
        return AgentError("internal_error", f"{type(exc).__name__}: {exc}", detail=foreign)
    if isinstance(exc, KeyError):
        message = exc.args[0] if exc.args else "not found"
        return AgentError("invalid_input", str(message))
    if isinstance(exc, (ValueError, LookupError)):
        return AgentError("invalid_input", str(exc))
    return AgentError("internal_error", f"{type(exc).__name__}: {exc}")


def _raised_outside(exc):
    """{raised_in, called_from} when a ValueError/LookupError was raised in a
    library rather than in Plexora's own code; else None."""
    import os

    if not isinstance(exc, (ValueError, LookupError)) or exc.__traceback__ is None:
        return None
    import traceback

    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return None
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ours = [f for f in frames if os.path.abspath(f.filename).startswith(root)]
    last = frames[-1]
    if os.path.abspath(last.filename).startswith(root) or not ours:
        return None
    caller = ours[-1]
    return {"raised_in": f"{os.path.basename(last.filename)}:{last.lineno}",
            "called_from": f"{os.path.relpath(caller.filename, os.path.dirname(root))}:"
                           f"{caller.lineno}"}
