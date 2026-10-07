"""What each Plexora capability is FOR, in the words both applications share.

Plexora keeps its own capability names; SCIMAP Pro keeps its function names.
What the two agree on is a short role vocabulary (spatialbridge `schema.ROLES`:
`image.inspect`, `gate.auto`, `qc.review`, `spatial.neighborhood`, ...), and
routing reads roles and needs, never package names -- "gate the markers
against the image" goes to whichever reachable provider declares `gate.auto`
on a capability that needs the image.

This module is the map from Plexora's tools to those roles, plus what each
needs and produces, and `describe(capability)` turns a registered capability
into the protocol's `CapabilityDescriptor` dict. A capability not named here
still travels, with no role: it can be called by name, it is just never
chosen by routing.

Pure data and one function. Imports nothing from the protocol package, so the
HTTP route can list capabilities whether or not it is installed.
"""

from __future__ import annotations

PROVIDER = "plexora"

#: The protocol's role vocabulary (spatialbridge `schema.ROLES`), spelled here
#: so this module needs no import of it. A test checks the two agree.
ROLES = (
    "image.inspect", "image.capture", "roi.edit", "roi.magic_select",
    "gate.manual", "gate.auto", "gate.apply", "qc.auto", "qc.review",
    "cells.phenotype", "cells.cluster", "cells.embed",
    "spatial.neighborhood", "spatial.interaction", "spatial.distance",
    "spatial.region", "stats.compare", "stats.survival", "report.figure",
    "dataset.export", "selection.edit",
)

#: tool name -> roles. Only what the tool is genuinely for: a role here is a
#: promise routing will act on.
TOOL_ROLES = {
    # Seeing the tissue, in the viewer or rendered headless.
    "viewer_open_project": ("image.inspect",),
    "viewer_navigate": ("image.inspect",),
    "viewer_set_channels": ("image.inspect",),
    "viewer_set_contrast": ("image.inspect",),
    "viewer_set_color_by": ("image.inspect",),
    "viewer_highlight_cells": ("image.inspect",),
    "viewer_show_shapes": ("image.inspect", "spatial.region"),
    "viewer_open_tool": ("image.inspect",),
    "viewer_get_state": ("image.inspect",),
    "render_region": ("image.inspect", "image.capture"),
    "render_cell_gallery": ("image.inspect",),
    "explain_cell": ("image.inspect",),
    "viewer_capture": ("image.capture",),
    # Regions.
    "list_rois": ("roi.edit",),
    "get_roi": ("roi.edit",),
    "create_roi": ("roi.edit", "spatial.region"),
    "update_roi": ("roi.edit",),
    "delete_roi": ("roi.edit",),
    "count_cells_in_roi": ("spatial.region",),
    "segment_qc_roi": ("roi.magic_select",),
    # Gates.
    "get_gate": ("gate.manual",),
    "set_gate": ("gate.manual",),
    "adjust_gate": ("gate.manual",),
    "viewer_preview_gate": ("gate.manual",),
    "render_gate_validation": ("gate.manual",),
    "suggest_auto_gate": ("gate.auto",),
    "gating_session_start": ("gate.auto",),
    "apply_gate_to_dataset": ("gate.apply",),
    "export_gates": ("gate.apply", "dataset.export"),
    "get_all_gates": ("gate.apply",),
    # Quality control.
    "qc_session_start": ("qc.auto",),
    "run_blur_check": ("qc.auto",),
    "run_artifact_check": ("qc.auto",),
    "run_segmentation_qc": ("qc.auto",),
    "get_qc_results": ("qc.review",),
    "get_qc_exclusions": ("qc.review",),
    "approve_qc_roi": ("qc.review",),
    "render_qc_overview": ("qc.review",),
    "export_qc": ("qc.review", "dataset.export"),
    # Selections.
    "set_selection": ("selection.edit",),
    "get_selection": ("selection.edit",),
    "list_selections": ("selection.edit",),
    "delete_selection": ("selection.edit",),
    "viewer_get_selection": ("selection.edit",),
}

#: tool name -> what it hands back, in the protocol's `PRODUCES` words.
TOOL_PRODUCES = {
    "gating_session_start": ("gates",), "suggest_auto_gate": ("gates",),
    "set_gate": ("gates",), "adjust_gate": ("gates",), "apply_gate_to_dataset": ("gates",),
    "export_gates": ("gates",), "get_all_gates": ("gates",),
    "create_roi": ("regions",), "update_roi": ("regions",), "segment_qc_roi": ("regions",),
    "list_rois": ("regions",),
    "qc_session_start": ("qc", "regions"), "run_blur_check": ("qc",),
    "run_artifact_check": ("qc",), "run_segmentation_qc": ("qc",),
    "get_qc_exclusions": ("qc",), "export_qc": ("qc", "regions"),
    "set_selection": ("selection",), "get_selection": ("selection",),
    "viewer_get_selection": ("selection",),
    "render_region": ("figure",), "viewer_capture": ("figure",),
    "render_cell_gallery": ("figure",), "render_gate_validation": ("figure",),
    "render_qc_overview": ("figure",),
}

#: The roles where a person looks at the result -- a human is a need.
_HUMAN = ("gate.manual", "qc.review", "roi.edit", "roi.magic_select", "selection.edit")


def roles_for(capability) -> list:
    return [role for role in TOOL_ROLES.get(capability.tool_name, ()) if role in ROLES]


def needs_for(capability) -> list:
    """The protocol's `needs`: what must exist before this can run.

    Every Plexora capability that takes a project needs its image (a project IS
    an image); a plugin's `Requires` adds the table and the mask; a viewer
    command needs an open tab; a role a person judges needs a person.
    """
    needs = []
    if capability.takes_project or capability.viewer_required:
        needs.append("image")
    requires = capability.requires
    if requires is not None:
        if getattr(requires, "table", False):
            needs.append("table")
        if getattr(requires, "segmentation", False):
            needs.append("mask")
    if capability.viewer_required:
        needs.append("viewer")
    if any(role in _HUMAN for role in roles_for(capability)):
        needs.append("human")
    return list(dict.fromkeys(needs))


def produces_for(capability) -> list:
    produced = list(TOOL_PRODUCES.get(capability.tool_name, ()))
    if not produced and capability.visual_output:
        produced.append("figure")
    if not produced and capability.viewer_required and capability.permission != "read":
        produced.append("view")
    return produced


def describe(capability) -> dict:
    """A registered capability as the protocol's `CapabilityDescriptor` dict.

    The descriptor id is `plexora:<capability name>`; the tool name is what a
    peer calls (`POST /agent/v1/capabilities/<tool_name>`, or the MCP tool).
    """
    entitlement = capability.entitlement if capability.entitlement not in (None, "free") \
        else None
    return {
        "id": f"{PROVIDER}:{capability.name}",
        "provider": PROVIDER,
        "tool_name": capability.tool_name,
        "roles": roles_for(capability),
        "purpose": capability.purpose,
        "needs": needs_for(capability),
        "produces": produces_for(capability),
        "permission": capability.permission,
        "execution": capability.execution,
        "viewer_required": bool(capability.viewer_required),
        "entitlement": entitlement,
        "input_schema": capability.input_model.model_json_schema()
        if capability.input_model else None,
        "version": capability.version,
        "tags": [str(tag) for tag in capability.tags],
    }
