"""The capability registry as spatialagents' tool backend.

`RegistryBackend` is what Plexora's conversations run their tools through
(`spatialagents.backend.ToolBackend`): the catalog is the registry filtered by
the conversation's policy, a definition is the capability's input model and
its MCP description, and a call is `registry.invoke` -- so validation, the
licence check, receipts and the audit log are the same as on every other
surface.

- **Catalog.** Every capability the policy could let through (egress allowed;
  writes only when the policy allows writes; source-file writes and deletes
  included, because they pause for the user's approval rather than being
  refused), minus the `ai.*` chat capabilities themselves and, with no viewer
  to drive, viewer capabilities.
- **Permission** is the capability's own class; Plexora has no tool whose
  class depends on its arguments.
- **Approved calls** run with `confirm: true` when the capability takes one:
  the person's approval is the confirmation a source-file write or a delete
  asks for.
- **Outcomes.** A write's receipt says what the harness needs: its
  operation_id, whether it can be undone (an Undo chip), and the project
  revision it moved (`revision_guard`: several writes a model sends together
  each carry the revision it read, and the runner carries them past the
  turn's own earlier writes).
- **Caching** (`toolcache.cacheable`): reads only, never row-level or raw
  pixels, never viewer state or a job; keyed on the project's revision
  (`plexora/agent/revision.py`).
- Results are cleaned and bounded as the MCP server bounds them
  (`plexora.mcp.serialize`, `MAX_TOOL_CHARS`), and images travel as
  `plexora.mcp.tools.split_images` lifts them out.
"""

from __future__ import annotations

from spatialagents.backend import ToolBackend

from plexora.agent.limits import MAX_TOOL_CHARS


def confirmed(capability, arguments: dict) -> dict:
    """The arguments with `confirm: true` when the capability takes one."""
    fields = getattr(capability.input_model, "model_fields", {})
    if "confirm" in fields:
        return {**arguments, "confirm": True}
    return dict(arguments)


def _receipt_outcome(outcome: dict, permission: str) -> dict:
    """A registry outcome as spatialagents reads one: the operation, whether it
    can be undone, and the revision a write moved, from its receipt."""
    result = outcome.get("result")
    receipt = result.get("receipt") if isinstance(result, dict) else None
    out = dict(outcome)
    if permission != "read":
        out["operation_id"] = (receipt or {}).get("operation_id") or outcome.get("operation_id")
        out["undo"] = bool(receipt and receipt.get("reversible", True) and receipt.get("changed", True)
                           and receipt.get("undo_hint"))
    if isinstance(receipt, dict):
        project, before, after = (receipt.get("project"), receipt.get("revision_before"),
                                  receipt.get("revision_after"))
        if isinstance(project, str) and isinstance(before, str) and isinstance(after, str) and before != after:
            out["advanced"] = {"scope": project, "before": before, "after": after}
    return out


class RegistryBackend(ToolBackend):
    """Plexora's capabilities, for one session (and its viewer link and notifier)."""

    max_result_chars = MAX_TOOL_CHARS
    revision_guard = ("project", "expected_revision")

    def __init__(self, session, *, link=None, notify=None, audit=None):
        self.session = session
        self.link = link
        self.notify = notify
        self.audit = audit

    @staticmethod
    def _capability(tool: str):
        """The capability an agent calls by `tool` (the registry answers to a
        tool name as well as to a capability name)."""
        from plexora.agent import registry

        return registry.get(tool)

    # -- what a conversation is given --------------------------------------------------

    def catalog(self, policy, *, viewer: bool = False) -> list[dict]:
        from plexora.agent import registry

        entries = []
        for cap in registry.all_capabilities():
            if cap.name.startswith("ai."):
                continue
            if cap.egress not in policy.egress:
                continue
            if cap.permission != "read" and not policy.allow_writes:
                continue
            if cap.viewer_required and not viewer:
                continue
            entries.append({"tool": cap.tool_name, "capability": cap.name, "permission": cap.permission,
                            "purpose": cap.purpose})
        return entries

    def definition(self, tool: str) -> dict:
        from plexora.mcp.tools import description_for

        cap = self._capability(tool)
        schema = cap.input_model.model_json_schema() if cap.input_model else {}
        return {"name": cap.tool_name, "description": description_for(cap), "input_schema": schema}

    def permission(self, tool: str, arguments: dict) -> str:
        return self._capability(tool).permission

    def purpose(self, tool: str) -> str:
        return self._capability(tool).purpose

    # -- running a call ----------------------------------------------------------------

    def invoke(self, tool: str, arguments: dict, *, policy, approved: bool = False) -> dict:
        from plexora.agent import registry

        cap = self._capability(tool)
        if approved:
            arguments = confirmed(cap, arguments)
        outcome = registry.invoke(self.session, cap.name, arguments, policy=policy, audit=self.audit,
                                  link=self.link, notify=self.notify)
        if not outcome.get("ok"):
            return outcome
        return _receipt_outcome(outcome, cap.permission)

    def cacheable(self, tool: str, arguments: dict) -> bool:
        from plexora.ai.harness.toolcache import cacheable

        return cacheable(self._capability(tool))

    def revision_token(self, tool: str, arguments: dict) -> str:
        from plexora.agent import revision

        project = arguments.get("project") if isinstance(arguments.get("project"), str) else None
        return revision.token(project)

    # -- the local tools that describe Plexora -------------------------------------------

    def skills(self) -> list[dict]:
        from plexora.ai import skills

        return [s for s in skills.list_skills() if s["available"]]

    def read_skill(self, name: str) -> str:
        from plexora.ai import skills

        return skills.read_skill(name)

    def server_info(self, policy, *, tools: int, viewer: bool) -> dict:
        """The MCP server's `server_info` facts that mean something inside the
        app (skill dataset-triage calls it first); the MCP transport's own
        (transport, auth, attached server) do not apply to a conversation."""
        from plexora import paths
        from plexora.agent import registry
        from plexora.agent.schemas import SCHEMA_VERSION, plexora_version

        try:
            data_root = str(paths.data_root())
        except Exception as exc:                  # noqa: BLE001 -- a conflicted account
            data_root = f"unavailable: {exc}"
        try:
            from plexora import licensing

            state = licensing.peek()
            license_info = {"plan": licensing.plan_of(state), "state": licensing.state_name(state),
                            "entitlements": list(state.entitlements)}
        except Exception:                         # noqa: BLE001 -- licensing never breaks this
            license_info = {"plan": "free", "state": "free", "entitlements": []}
        return {
            "plexora_version": plexora_version(),
            "schema_version": SCHEMA_VERSION,
            "data_root": data_root,
            "policy": policy.describe(),
            "capability_owners": sorted({cap.owner for cap in registry.all_capabilities()}),
            "n_tools": tools,
            "viewer": viewer,
            "skills": [s["name"] for s in self.skills()],
            "license": license_info,
        }

    # -- results -------------------------------------------------------------------------

    def serialize(self, value):
        from plexora.mcp import serialize

        return serialize._clean(value)

    def split_images(self, result) -> tuple:
        from plexora.mcp.tools import split_images

        return split_images(result)
