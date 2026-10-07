"""Tool profiles: which capabilities an MCP connection is offered.

Every tool's name, description and input schema is sent to the agent on
every turn, cached or not. All 132 capabilities are ~300k characters; on the
2026-10-03 benchmark that was a 129k-token prefix for a Claude Code
coordinator that called a dozen of them to gate one image ($0.5-1.5 a run).
A profile names the tools one kind of work needs, and `plexora mcp serve
--profile gating` offers only those. `full` (the default) is everything.

The server's own tools (`server_info`, `list_capabilities`, `validate_scope`,
`list_skills`, `read_skill`) are always offered: an agent can always find out
what it is missing. A name a profile lists that no capability has is a bug
(a rename); `unknown()` finds them and the test suite checks.

A profile may also name OWNERS (`OWNER_PROFILES`): every capability a plugin
registers, whatever its tools are called -- the `analysis` profile is all of
an analysis plugin's tools (SCIMAP Pro's, `scimappro_*`), which come from that
plugin's own catalogue and change with it. A profile naming a plugin's tools
by name when the plugin is not installed is not a rename, it is an absent
plugin: `unknown()` ignores names under an owner with no capability here.
"""

from __future__ import annotations

DEFAULT = "full"

#: What every focused profile starts from: finding and reading a project, jobs, undo.
_COMMON = (
    "list_projects", "inspect_project", "list_datasets", "get_dataset", "list_markers", "list_channels",
    "inspect_expression_sources", "set_expression_source", "set_pixel_size", "get_resource_status",
    "job_get", "job_wait", "job_list", "job_cancel", "undo_operation", "session_report",
)

PROFILES: dict[str, tuple | None] = {
    "full": None,
    # Gate an image or a dataset: the session loop, the gate readers and the
    # pictures that check a gate (skills gate-image, gate-packets, gate-dataset,
    # review-gating, diagnose-marker).
    "gating": _COMMON + (
        "gating_session_start", "gating_next", "gating_answer", "gating_session_status",
        "gating_session_bulk", "gating_session_finish", "gating_report", "gating_qc",
        "get_panel_context", "set_panel_context", "get_marker_hierarchy",
        "get_gate", "get_all_gates", "get_gate_distribution", "get_gate_provenance", "get_gated_summary",
        "get_marker_distribution", "profile_marker", "bivariate_evidence", "score_gate_candidates",
        "suggest_auto_gate", "compare_gates_across_images",
        "set_gate", "adjust_gate", "set_gate_status", "reset_gates", "restore_gates",
        "apply_gate_to_dataset", "export_gates", "write_gates_to_source",
        "render_region", "render_gate_validation", "render_gating_collage", "render_cell_gallery",
        "sample_gating_cells", "sample_gate_validation_regions", "explain_cell", "get_qc_exclusions",
    ),
    # QC an image: the session loop, the checks, regions and the QC pictures
    # (skills qc-image, qc-packets, qc-checks, review-qc).
    "qc": _COMMON + (
        "qc_session_start", "qc_next", "qc_answer", "qc_session_status", "qc_session_bulk",
        "qc_session_finish", "qc_report", "refresh_qc", "get_qc_results", "list_qc_results",
        "activate_qc_result", "get_qc_exclusions", "approve_qc_roi", "refine_qc_roi", "segment_qc_roi",
        "render_artifact_overview", "inspect_artifact_channels", "dismiss_qc_finding",
        "reset_qc", "restore_qc", "export_qc", "write_qc_to_source", "set_qc_strictness", "set_qc_cycles",
        "profile_image_qc", "render_qc_overview", "sample_qc_examples", "detect_nuclear_channels",
        "run_artifact_check", "get_artifact_check", "set_artifact_check", "clear_artifact_check",
        "write_artifact_regions", "run_blur_check", "get_blur_check", "set_blur_check", "clear_blur_check",
        "write_blur_regions", "run_segmentation_qc", "get_segmentation_qc", "clear_segmentation_qc",
        "write_segmentation_flags", "compute_registration_mismatch", "step_registration_comparison",
        "get_registration_check", "set_registration_check", "write_registration_regions",
        "write_qc_background_roi", "run_dna_retention", "get_dna_retention", "clear_dna_retention",
        "list_rois", "get_roi", "create_roi", "update_roi", "delete_roi", "count_cells_in_roi",
        "render_region", "render_cell_gallery", "explain_cell",
    ),
}


#: The bridge tools an analysis profile needs to reach Plexora's side of a
#: hand-off (plexora/agent/core/bridge.py), and the binding/selection tools a
#: result is shown through.
_BRIDGE = (
    "bridge_info", "bridge_capabilities", "bridge_route", "bridge_status", "bridge_collect",
    "bridge_handoff", "bridge_invoke", "workspace_get", "find_project_for_table",
    "bind_project", "viewer_set_color_by", "viewer_highlight_cells", "set_selection",
    "get_selection", "list_selections",
)

#: Profiles that offer every capability of these owners, beside their names.
OWNER_PROFILES: dict[str, tuple] = {
    "analysis": ("scimappro",),
}

PROFILES["analysis"] = _COMMON + _BRIDGE
#: The analysis plugin through three generic tools instead of one per
#: function: search, describe, run. For a coordinator whose prompt cannot carry
#: a hundred function schemas.
PROFILES["analysis-lite"] = _COMMON + _BRIDGE + (
    "scimappro_search_functions", "scimappro_describe_function", "scimappro_run",
)

#: Which owner a profile name belongs to, by its tools' prefix, when that owner
#: may be absent: a name under one is ignored by `unknown()` while no tool of
#: that owner is registered.
OPTIONAL_OWNERS = ("scimappro",)


def names() -> tuple:
    return tuple(PROFILES)


def check(profile: str | None) -> str:
    """The profile's name, or SystemExit naming the ones there are."""
    profile = profile or DEFAULT
    if profile not in PROFILES:
        raise SystemExit(f"--profile {profile}: there is no such profile; use one of {', '.join(PROFILES)}.")
    return profile


def allows(profile: str | None, tool: str, owner: str | None = None) -> bool:
    """Whether a capability's tool is offered under `profile`: listed by name,
    or owned by an owner the profile names."""
    name = check(profile)
    tools = PROFILES[name]
    if tools is None or tool in tools:
        return True
    return owner is not None and owner in OWNER_PROFILES.get(name, ())


def _owner_of(name: str):
    return next((owner for owner in OPTIONAL_OWNERS if name.startswith(f"{owner}_")), None)


def unknown(profile: str, tools) -> list:
    """The names `profile` lists that are not among `tools` (the registry's).

    A name under an optional owner (`scimappro_*`) counts only when that owner
    has a tool here at all: absent, its plugin is not installed, and that is
    not a bug in the profile."""
    listed = PROFILES[check(profile)] or ()
    have = set(tools)
    present = {owner for owner in OPTIONAL_OWNERS
               if any(t.startswith(f"{owner}_") for t in have)}
    return sorted(name for name in set(listed) if name not in have
                  and (_owner_of(name) is None or _owner_of(name) in present))
