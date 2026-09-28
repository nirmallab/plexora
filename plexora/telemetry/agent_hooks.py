"""Which capabilities agents call, how they end, and how long they take --
never with what arguments.

`registry.invoke` is the one path every capability call takes (MCP over
stdio or HTTP, the tests, a capability calling another), so it is timed here
and nowhere else. The audit log carries full arguments for the user's own
record; nothing here reads it or them. A call records its capability's
registered name (a plugin's own as `ext:<8 hex>`), its owner, permission
class and execution kind, the transport it arrived on, whether it was nested
inside another call, and its outcome as an agent error code.
"""

from __future__ import annotations

import threading
from time import perf_counter

from plexora.telemetry import identity, schema
from plexora.telemetry.client import telemetry

_local = threading.local()
#: The previous top-level capability per agent session, for transitions.
#: Keyed by `id(session)`; bounded, and holds names only.
_previous: dict = {}
_previous_lock = threading.Lock()
_TRANSPORTS = {"stdio": "stdio", "http": "web", "streamable-http": "web", "web": "web"}


def names(capability) -> tuple[str, str]:
    """`(name, owner)` as telemetry may say them."""
    owner = identity.owner_label(capability.owner)
    if owner.startswith("ext:"):
        return identity.owner_label(capability.name), owner
    name = capability.name if schema.CAPABILITY.check(capability.name) \
        else identity.owner_label(capability.name)
    return name, owner


def timed_invoke(invoke, session, name, arguments, **kwargs):
    """Call `invoke(session, name, arguments, **kwargs)` and record it."""
    if not telemetry.enabled:
        return invoke(session, name, arguments, **kwargs)
    depth = getattr(_local, "depth", 0)
    _local.depth = depth + 1
    started = perf_counter()
    answer = None
    try:
        answer = invoke(session, name, arguments, **kwargs)
        return answer
    finally:
        _local.depth = depth
        try:
            _record(session, name, answer, (perf_counter() - started) * 1000.0, depth > 0)
        except Exception:
            telemetry._fail()


def _record(session, name, answer, ms, nested):
    from plexora.agent import registry

    try:
        capability = registry.get(name)
    except Exception:
        telemetry.count("capability.summary", "n", name="ext:00000000", owner="ext:00000000",
                        permission="other", execution="immediate",
                        transport="nested" if nested else _transport(),
                        outcome="unknown_capability")
        return
    cap_name, owner = names(capability)
    if isinstance(answer, dict) and answer.get("ok"):
        outcome = "ok"
    else:
        code = ((answer or {}).get("error") or {}).get("code") if isinstance(answer, dict) \
            else None
        outcome = code if code in schema.AGENT_OUTCOMES else "other"
    if outcome == "internal_error":
        with telemetry._lock:
            telemetry._errors["agent"] += 1
    permission = capability.permission if capability.permission in \
        ("read", "reversible_write", "source_file_write", "destructive") else "other"
    telemetry.count("capability.summary", "n", name=cap_name, owner=owner,
                    permission=permission, execution=capability.execution,
                    transport="nested" if nested else _transport(), outcome=outcome)
    telemetry.observe("capability.summary", "ms", ms, name=cap_name, owner=owner)
    if nested:
        return
    key = id(session)
    with _previous_lock:
        previous = _previous.get(key)
        _previous[key] = cap_name
        if len(_previous) > 256:
            _previous.pop(next(iter(_previous)))
    if previous is not None:
        telemetry.count("capability.transition", "n", **{"from": previous, "to": cap_name})


def _transport() -> str:
    source = telemetry.call_source.get()
    return _TRANSPORTS.get(source, "other")


def job_finished(capability, status, ms):
    """One agent job's final status and duration (`JobStore._run`)."""
    if not telemetry.enabled:
        return
    try:
        if status not in ("done", "failed", "cancelled"):
            return
        cap_name, owner = names(capability)
        telemetry.observe("capability.summary", "job_ms", ms, name=cap_name, owner=owner,
                          status=status)
    except Exception:
        telemetry._fail()


def _reset_for_tests():
    with _previous_lock:
        _previous.clear()
