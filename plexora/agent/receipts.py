"""Building the receipt a mutation hands back, and logging it."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from plexora.agent.audit import now_iso
from plexora.agent.schemas import Receipt, versions


def operation_id() -> str:
    """`op_<utc timestamp>_<8 hex>`: sortable, and unique without a counter."""
    return f"op_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}_{uuid.uuid4().hex[:8]}"


def make_receipt(call, *, changed, before=None, after=None, revision_before=None,
                 revision_after=None, persistent_state="none", source_file_modified=False,
                 source_path=None, reversible=None, undo_hint=None, extra=None) -> Receipt:
    """The receipt for the call in hand, appended to the audit log as `ok`.

    `call` is the `Call` the capability was invoked with (plexora/agent/registry.py);
    it carries the operation id, the capability and the audit log.
    """
    capability = call.capability
    viewer_notified = False
    # Only persistent state is worth telling other tabs about: a viewer
    # command changed one tab, which already knows.
    if changed and capability.persistent and call.notify is not None:
        try:
            viewer_notified = bool(call.notify(call.project_name, capability.owner,
                                               capability.name, {"after": after}))
        except Exception:
            viewer_notified = False
    receipt = Receipt(
        operation_id=call.operation_id,
        project=call.project_name or "",
        capability=capability.name,
        capability_version=capability.version,
        changed=bool(changed),
        before=before,
        after=after,
        revision_before=revision_before,
        revision_after=revision_after,
        persistent_state=persistent_state,
        source_file_modified=bool(source_file_modified),
        source_path=source_path,
        reversible=capability.reversible if reversible is None else bool(reversible),
        undo_hint=undo_hint,
        viewer_notified=viewer_notified,
        timestamp=now_iso(),
        audit_path=str(call.audit.path),
        versions=versions(capability.version),
    )
    call.audit.append({"status": "ok", "operation_id": call.operation_id,
                       "capability": capability.name, "project": call.project_name,
                       "arguments": call.arguments, "receipt": receipt.model_dump(mode="json"),
                       **(extra or {})})
    call.receipted = True
    return receipt
