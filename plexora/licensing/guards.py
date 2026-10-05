"""Where the licence is enforced: at the server, on the action.

Three shapes of the same check, one per way an action reaches Plexora:

* `check_capability(capability)` -- the agent registry's `_invoke`, which every
  MCP transport, the HTTP agent API, jobs at submit and nested invokes all go
  through. Raises `AgentError("license_required")`. `check_origin` follows it:
  an entitled capability called from an external MCP client also needs `mcp`
  (external MCP access), so the in-app AI and outside agents are licensed
  separately. Free capabilities never reach either check.
* `require_entitlement(ent)` / `guard_blueprint(bp, ent)` -- a Flask route or
  a whole plugin blueprint. Answers 403 with the same payload.
* `locked_payload(...)` -- a Paid tool's `/panel` answer, which the page turns
  into the Paid modal instead of mounting anything.

The first line of `check_capability` is the Free fast path: a capability with
no entitlement returns before the licence is looked at, so nothing Free ever
reads a file, loads crypto or waits on anything here. Hiding a tool in a menu
is never the enforcement -- this is.

A refusal carries the entitlement, the plan that unlocks it, the licence state
and a sentence an agent can repeat, in `error.detail`; `code` is what an agent
branches on. A job refused here never starts; a job that started while the
licence was valid finishes, and everything any Paid feature produced stays
readable, exportable and editable after expiry -- nothing here guards a read.
"""

from __future__ import annotations

import functools

from plexora.licensing import manifest
from plexora.licensing.entitlements import is_free

LICENSE_REQUIRED = "license_required"

#: The add-on an entitled capability needs as well when an outside agent calls
#: it over MCP (`plexora mcp serve`). See `manifest.ADD_ONS`.
MCP = "mcp"


def _state():
    from plexora.licensing import state

    return state.current()


def refusal_detail(entitlement: str, *, what: str | None = None) -> dict:
    """What every refusal says, wherever it is raised."""
    current = _state()
    label = manifest.label(entitlement)
    detail = {
        "entitlement": entitlement,
        "plan_required": manifest.plan_for(entitlement),
        "label": label,
        "summary": manifest.summary(entitlement),
        "state": current.state,
        "plan": current.plan,
        "hint": _hint(current.state, label, entitlement),
    }
    if what:
        detail["capability"] = what
    return detail


def hint_for(entitlement: str, state: str) -> str:
    """The sentence a refusal of `entitlement` carries, for a licence in `state`."""
    return _hint(state, manifest.label(entitlement), entitlement)


def _hint(state: str, label: str, entitlement: str | None = None) -> str:
    if entitlement == MCP:
        # Never "manual tools keep working": the tool being refused may be one.
        if state == "expired":
            return ("This Paid tool needs a licence with external MCP access, and this one has "
                    "expired. Free tools still answer over MCP; renewing brings the rest back.")
        if state in ("revoked", "invalid"):
            return ("This Paid tool needs a licence with external MCP access, and the one on this "
                    "machine is not active. The user can check Settings > License. Free tools "
                    "still answer over MCP.")
        return ("This Paid tool needs a licence that includes external MCP access, and this one "
                "does not. Tell the user an administrator can add it to their licence or seat; "
                "it takes effect within minutes, with no change to this client's configuration. "
                "Free tools still answer over MCP, and Plexora's own AI is unaffected.")
    if state == "expired":
        return (f"{label} needs a Paid licence, and this one has expired. Everything the "
                f"user made with it is still here and still editable; renewing brings "
                f"{label} back. Manual tools keep working on Free.")
    if state in ("revoked", "invalid"):
        return (f"{label} needs a Paid licence, and the one on this machine is not active. "
                f"The user can check Settings > License. Manual tools keep working on Free.")
    return (f"{label} is part of Plexora Paid. Tell the user it is a Paid feature and that "
            f"they can start a trial or enter a licence in Settings > License. The manual "
            f"tools (gating, ROIs, the viewer) keep working on Free.")


def capability_error(capability, entitlement: str):
    from plexora.agent.errors import AgentError

    detail = refusal_detail(entitlement, what=getattr(capability, "name", None))
    if entitlement == MCP:
        return AgentError(LICENSE_REQUIRED,
                          f"{detail.get('capability') or 'This tool'} is a Paid tool, and calling it "
                          f"over MCP needs a licence with external MCP access (entitlement 'mcp'); "
                          f"this machine is on {detail['plan'].title()} ({detail['state']}).",
                          detail=detail)
    return AgentError(LICENSE_REQUIRED,
                      f"{detail['label']} needs a Paid Plexora licence "
                      f"(entitlement {entitlement!r}); this machine is on "
                      f"{detail['plan'].title()} ({detail['state']}).",
                      detail=detail)


def check_capability(capability) -> None:
    """Refuse an entitled capability the licence does not unlock."""
    entitlement = getattr(capability, "entitlement", None)
    if entitlement is None or entitlement == "free":
        return
    if not _state().allows(entitlement):
        raise capability_error(capability, entitlement)


def check_origin(capability, origin) -> None:
    """Refuse an entitled capability an external MCP client calls without `mcp`.

    Runs after `check_capability`, so the capability's own grant is already
    known to be there. Returns at once for a Free capability or any origin other
    than MCP; the in-app harness and the HTTP agent API never set one."""
    if origin != MCP:
        return
    entitlement = getattr(capability, "entitlement", None)
    if entitlement is None or entitlement == "free":
        return
    if not _state().allows(MCP):
        raise capability_error(capability, MCP)


def check(entitlement, *, what: str | None = None) -> None:
    """`check_capability` for code that is not a capability (an MCP resource)."""
    if is_free(entitlement):
        return
    if not _state().allows(entitlement):
        from plexora.agent.errors import AgentError

        detail = refusal_detail(entitlement, what=what)
        message = (f"{what or 'This'} is Paid, and reading it over MCP needs a licence with "
                   f"external MCP access." if entitlement == MCP
                   else f"{detail['label']} needs a Paid Plexora licence.")
        raise AgentError(LICENSE_REQUIRED, message, detail=detail)


def http_refusal(entitlement: str, *, what: str | None = None):
    """The 403 a guarded route answers with: the app's usual `success/error`
    shape, plus the structured licence block."""
    from flask import jsonify

    detail = refusal_detail(entitlement, what=what)
    return jsonify(success=False,
                   error=f"{detail['label']} needs a Paid Plexora licence.",
                   license={"code": LICENSE_REQUIRED, **detail}), 403


def require_entitlement(entitlement: str):
    """Decorate a Flask view so it answers 403 unless `entitlement` is unlocked."""
    def wrap(view):
        if is_free(entitlement):
            return view

        @functools.wraps(view)
        def guarded(*args, **kwargs):
            if not _state().allows(entitlement):
                return http_refusal(entitlement, what=view.__name__)
            return view(*args, **kwargs)

        return guarded

    return wrap


#: Never guarded: a plugin's JavaScript and CSS are code, not data or
#: actions, and a locked panel never asks for them anyway.
EXEMPT_ENDPOINTS = frozenset({"static"})


def guard_blueprint(blueprint, entitlement: str, *, endpoints=None) -> None:
    """Guard every route of `blueprint` (or just `endpoints`, bare view names)
    behind `entitlement`. For a Paid plugin, or the Paid endpoints of a mixed
    one -- whose manual routes stay unguarded."""
    if is_free(entitlement):
        return
    wanted = None if endpoints is None else set(endpoints)

    @blueprint.before_request
    def _license_guard():
        from flask import request

        endpoint = (request.endpoint or "").rpartition(".")[2]
        if endpoint in EXEMPT_ENDPOINTS:
            return None
        if wanted is not None and endpoint not in wanted:
            return None
        if _state().allows(entitlement):
            return None
        return http_refusal(entitlement, what=endpoint or None)


def locked_payload(entitlement: str, *, tool: str, label: str | None = None) -> dict:
    """A Paid tool's `/panel` answer on a licence that does not unlock it."""
    detail = refusal_detail(entitlement, what=tool)
    return {"locked": {"entitlement": entitlement, "plan_required": detail["plan_required"],
                       "tool": tool, "label": label or detail["label"],
                       "summary": detail["summary"], "state": detail["state"]}}
