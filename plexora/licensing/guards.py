"""Where the licence is enforced: at the server, on the action.

Three shapes of the same check, one per way an action reaches Plexora:

* `check_capability(capability)` -- the agent registry's `_invoke`, which every
  MCP transport, the HTTP agent API, jobs at submit and nested invokes all go
  through. The decision is `GUARD.check` (the shared BioCognia gate); the
  refusal is Plexora's `AgentError("license_required")`. `check_origin` follows it:
  an entitled capability called from an external MCP client also needs `mcp`
  (external MCP access), so the in-app AI and outside agents are licensed
  separately. Free capabilities never reach either check.
* `require_entitlement(ent)` / `guard_blueprint(bp, ent)` -- a Flask route or
  a whole plugin blueprint. Answers 403 with the same payload.
* `locked_payload(...)` -- a Pro tool's `/panel` answer, which the page turns
  into the Pro modal instead of mounting anything.

The first line of `check_capability` is the Free fast path: a capability with
no entitlement returns before the licence is looked at, so nothing Free ever
reads a file, loads crypto or waits on anything here. Hiding a tool in a menu
is never the enforcement -- this is.

A refusal carries the entitlement, the plan that unlocks it, the licence state
and a sentence an agent can repeat, in `error.detail`; `code` is what an agent
branches on. A job refused here never starts; a job that started while the
licence was valid finishes, and everything any Pro feature produced stays
readable, exportable and editable after expiry -- nothing here guards a read.
"""

from __future__ import annotations

import functools

from biocognia.entitlements import is_free

from plexora.licensing import manifest

LICENSE_REQUIRED = "license_required"

#: The grant an entitled capability needs as well when an outside agent calls
#: it over MCP (`plexora mcp serve`): external MCP access, which is off by
#: default and turned on for a licence by an administrator. See
#: `manifest.OFF_BY_DEFAULT`.
MCP = "mcp"

#: The origin of a call from another application through the shared protocol
#: (spatialbridge) -- `registry.ORIGIN_BRIDGE`, spelled here so this module
#: stays importable without the agent layer. Exempt from the `mcp` grant:
#: SCIMAP Pro handing Plexora a gating run is one product calling another on
#: the user's behalf, not an outside agent. The capability's own entitlement is
#: still checked (`check_capability` runs first), so nothing Pro becomes Free.
BRIDGE = "bridge"


def _state():
    from plexora.licensing import LICENSING

    return LICENSING.current()


def _guard():
    from plexora.licensing import GUARD

    return GUARD


def refusal_detail(entitlement: str, *, what: str | None = None) -> dict:
    """What every refusal says, wherever it is raised."""
    from plexora.licensing import plan_of, state_name

    current = _state()
    label = manifest.label(entitlement)
    state = state_name(current)
    detail = {
        "entitlement": entitlement,
        "plan_required": manifest.plan_for(entitlement),
        "label": label,
        "summary": manifest.summary(entitlement),
        "state": state,
        "plan": plan_of(current),
        # Outside the Pro default (off by default, like `mcp`, or a
        # third-party plugin): neither Pro nor its trial unlocks it, so no
        # surface may call it Pro, offer a trial or name a price for it.
        "off_by_default": manifest.off_by_default(entitlement),
        "hint": _hint(state, label, entitlement),
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
            return ("This Pro tool needs a licence with external MCP access, and this one has "
                    "expired. Free tools still answer over MCP; renewing brings the rest back.")
        if state in ("revoked", "invalid"):
            return ("This Pro tool needs a licence with external MCP access, and the one on this "
                    "machine is not active. The user can check Settings > Account. Free tools "
                    "still answer over MCP.")
        return ("This Pro tool needs a licence with external MCP access turned on, and this one "
                "does not have it. External MCP access is off by default and is not part of "
                "Plexora Pro or its trial: tell the user an administrator can turn it on for "
                "their licence; it takes effect within minutes, with no change to this client's "
                "configuration. Free tools still answer over MCP, and Plexora's own AI is "
                "unaffected.")
    off = entitlement is not None and manifest.off_by_default(entitlement)
    needs = f"a licence that includes {label}" if off else "a Plexora Pro licence"
    if state == "expired":
        return (f"{label} needs {needs}, and this one has expired. Everything the "
                f"user made with it is still here and still editable; renewing brings "
                f"{label} back. Manual tools keep working on Free.")
    if state in ("revoked", "invalid"):
        return (f"{label} needs {needs}, and the one on this machine is not active. "
                f"The user can check Settings > Account. Manual tools keep working on Free.")
    # Off by default: never "part of Pro", never "start a trial", never a
    # price -- none of them unlocks it.
    if off and manifest.third_party(entitlement):
        return (f"{label} is a plugin from its maker, not part of Plexora Pro or its trial. "
                f"Tell the user its maker licenses it, so they should ask its maker for a "
                f"licence that includes it, and that the manual tools (gating, ROIs, the "
                f"viewer) keep working on Free.")
    if off:
        return (f"{label} is off by default and is not part of Plexora Pro or its trial. Tell "
                f"the user an administrator can turn it on for their licence, and that the "
                f"manual tools (gating, ROIs, the viewer) keep working on Free.")
    from plexora.links import PRICING_URL

    return (f"{label} is part of Plexora Pro. Tell the user it is a Pro feature and that "
            f"they can buy it at {PRICING_URL}, start a trial, or connect this device in "
            f"Settings > Account. The manual tools (gating, ROIs, the viewer) keep working on Free.")


def _needs(detail: dict) -> str:
    """The refusal's first sentence, without its full stop."""
    if detail.get("off_by_default"):
        if manifest.third_party(detail.get("entitlement")):
            return (f"{detail['label']} is a plugin from its maker, not part of Plexora Pro, "
                    f"and this licence does not include it")
        return (f"{detail['label']} is off by default, not part of Plexora Pro, and this "
                f"licence does not have it turned on")
    return f"{detail['label']} needs a Plexora Pro licence"


def capability_error(capability, entitlement: str):
    from plexora.agent.errors import AgentError

    detail = refusal_detail(entitlement, what=getattr(capability, "name", None))
    if entitlement == MCP:
        return AgentError(LICENSE_REQUIRED,
                          f"{detail.get('capability') or 'This tool'} is a Pro tool, and calling it "
                          f"over MCP needs a licence with external MCP access turned on "
                          f"(entitlement 'mcp'), which is off by default; this machine is on "
                          f"{detail['plan'].title()} ({detail['state']}).",
                          detail=detail)
    return AgentError(LICENSE_REQUIRED,
                      f"{_needs(detail)} "
                      f"(entitlement {entitlement!r}); this machine is on "
                      f"{detail['plan'].title()} ({detail['state']}).",
                      detail=detail)


def check_capability(capability) -> None:
    """Refuse an entitled capability the licence does not unlock."""
    entitlement = getattr(capability, "entitlement", None)
    if entitlement is None or entitlement == "free":
        return
    from biocognia import EntitlementRequired

    try:
        _guard().check(entitlement)
    except EntitlementRequired:
        raise capability_error(capability, entitlement) from None


def check_origin(capability, origin) -> None:
    """Refuse an entitled capability an external MCP client calls without `mcp`.

    Runs after `check_capability`, so the capability's own grant is already
    known to be there. Returns at once for a Free capability or any origin other
    than MCP; the in-app harness and the HTTP agent API never set one, and the
    bridge origin (`BRIDGE`) is exempt by decision, not by omission."""
    if origin == BRIDGE or origin != MCP:
        return
    entitlement = getattr(capability, "entitlement", None)
    if entitlement is None or entitlement == "free":
        return
    if not _guard().allows(MCP):
        raise capability_error(capability, MCP)


def check(entitlement, *, what: str | None = None) -> None:
    """`check_capability` for code that is not a capability (an MCP resource)."""
    if is_free(entitlement):
        return
    if not _guard().allows(entitlement):
        from plexora.agent.errors import AgentError

        detail = refusal_detail(entitlement, what=what)
        message = (f"{what or 'This'} is part of Plexora Pro, and reading it over MCP needs a "
                   f"licence with external MCP access turned on, which is off by default."
                   if entitlement == MCP
                   else f"{_needs(detail)}.")
        raise AgentError(LICENSE_REQUIRED, message, detail=detail)


def http_refusal(entitlement: str, *, what: str | None = None):
    """The 403 a guarded route answers with: the app's usual `success/error`
    shape, plus the structured licence block."""
    from flask import jsonify

    detail = refusal_detail(entitlement, what=what)
    return jsonify(success=False,
                   error=f"{_needs(detail)}.",
                   license={"code": LICENSE_REQUIRED, **detail}), 403


def require_entitlement(entitlement: str):
    """Decorate a Flask view so it answers 403 unless `entitlement` is unlocked."""
    def wrap(view):
        if is_free(entitlement):
            return view

        @functools.wraps(view)
        def guarded(*args, **kwargs):
            if not _guard().allows(entitlement):
                return http_refusal(entitlement, what=view.__name__)
            return view(*args, **kwargs)

        return guarded

    return wrap


#: Never guarded: a plugin's JavaScript and CSS are code, not data or
#: actions, and a locked panel never asks for them anyway.
EXEMPT_ENDPOINTS = frozenset({"static"})


def guard_blueprint(blueprint, entitlement: str, *, endpoints=None) -> None:
    """Guard every route of `blueprint` (or just `endpoints`, bare view names)
    behind `entitlement`. For a Pro plugin, or the Pro endpoints of a mixed
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
        if _guard().allows(entitlement):
            return None
        return http_refusal(entitlement, what=endpoint or None)


def locked_payload(entitlement: str, *, tool: str, label: str | None = None) -> dict:
    """A Pro tool's `/panel` answer on a licence that does not unlock it."""
    detail = refusal_detail(entitlement, what=tool)
    return {"locked": {"entitlement": entitlement, "plan_required": detail["plan_required"],
                       "tool": tool, "label": label or detail["label"],
                       "summary": detail["summary"], "state": detail["state"],
                       "off_by_default": detail["off_by_default"]}}
