"""Named starting points for a render, for the questions agents ask most.

A preset only fills what the caller left unset -- an explicit channel list, a
region or an output size always wins -- and the manifest records the preset
AND the fields it filled, so a reader can tell a choice from a default.
"""

from __future__ import annotations

import re

from plexora.agent.render_spec import CellsSpec, ChannelSpec, MarkerHighlight, OutputSpec

#: Colours with a job: the marker being judged, and the nuclear context.
TARGET_COLOR = "#ffd60a"
NUCLEAR_COLOR = "#9a9a9a"
CONTEXT_COLOR = "#22e6e6"

_NUCLEAR = re.compile(r"^(dna|dapi|hoechst|nuclear|nuclei|h3342|ir19[13]|iridium)", re.I)

PRESETS = {
    "marker_validation": {
        "field_um": 150.0, "field_px": 512, "output": 768, "segmentation": "outlines",
        "about": "the target marker in yellow over nuclear grey, cell outlines, and the "
                 "stored gate's positive cells highlighted",
    },
    "segmentation_qc": {
        "field_um": 200.0, "field_px": 600, "output": 768, "segmentation": "outlines",
        "about": "nuclear channel with cell outlines, to judge whether the mask fits",
    },
    "roi_context": {
        "output": 1024, "segmentation": "none",
        "about": "the region's bounding box with margin, first channels, no outlines",
    },
    "cell_identity": {
        "field_um": 60.0, "field_px": 200, "output": 512, "segmentation": "outlines",
        "about": "a close field with cell ids written beside the cells",
    },
    "spatial_context": {
        "output": 1024, "segmentation": "none",
        "about": "a wide field of the tissue, for where-is-it questions",
    },
}


def nuclear_channel(names):
    """The first channel that is plainly a nuclear stain, or None."""
    for name in names:
        if _NUCLEAR.match(str(name)):
            return name
    return None


def apply(spec, channel_names, *, has_mask):
    """(spec with the preset's defaults filled, [fields it filled])."""
    name = spec.preset
    filled = []
    updates = {}
    marker = spec.marker or (spec.cells.highlight.marker
                             if spec.cells and isinstance(spec.cells.highlight,
                                                          MarkerHighlight) else None)
    nuclear = nuclear_channel(channel_names)

    if spec.channels is None:
        channels = []
        if name in ("marker_validation", "cell_identity") or (marker and name is None):
            if nuclear and nuclear != marker:
                channels.append(ChannelSpec(name=nuclear, color=NUCLEAR_COLOR))
            if marker and marker in channel_names:
                channels.append(ChannelSpec(name=marker, color=TARGET_COLOR))
        elif name == "segmentation_qc":
            channels.append(ChannelSpec(name=nuclear or channel_names[0], color="#ffffff"))
        if not channels:
            palette = ("#4f86ff", "#35d07f", "#ff4f7b")
            ordered = ([nuclear] if nuclear else []) + [c for c in channel_names if c != nuclear]
            channels = [ChannelSpec(name=c, color=palette[i])
                        for i, c in enumerate(ordered[:3])]
        updates["channels"] = channels
        filled.append("channels")

    if spec.segmentation is None:
        default = PRESETS.get(name, {}).get("segmentation", "outlines")
        updates["segmentation"] = default if has_mask else "none"
        filled.append("segmentation")

    if marker and (spec.cells is None or spec.cells.highlight is None):
        base = spec.cells or CellsSpec()
        updates["cells"] = base.model_copy(update={"highlight": MarkerHighlight(marker=marker)})
        filled.append("cells.highlight")
    if name == "cell_identity":
        base = updates.get("cells") or spec.cells or CellsSpec()
        if not base.label_ids:
            updates["cells"] = base.model_copy(update={"label_ids": True})
            filled.append("cells.label_ids")

    if spec.output is None and name in PRESETS:
        side = PRESETS[name]["output"]
        updates["output"] = OutputSpec(width=side)
        filled.append("output")

    return spec.model_copy(update=updates), filled


def field_size(preset_name):
    """(microns, pixels) a preset's field is, for callers that frame one."""
    entry = PRESETS.get(preset_name or "", {})
    return entry.get("field_um"), entry.get("field_px")
