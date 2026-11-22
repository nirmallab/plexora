"""Whether a project's table places its cells on their own mask labels.

Every look gating and QC send is drawn round the table's cell positions. When
those positions are in another space than the image (Xenium microns read as
CyCIF pixels, lsp35766_p193_2 on 2026-11-21), every crop lands somewhere else:
the reader is shown glass, judges it, and the session writes gates and QC
calls from it. The mask says so at once -- a cell's own label is missing from
its own crop -- but nothing asked it.

`check` reads a small seeded sample of cells the way a look does
(`crops.read_cell_crops`, so a mask on a data node is read as a look reads it)
and counts the crops that hold their own label. `refusal` turns a failing
check into the error a session start raises, naming the viewer's table
correction, which proposes the fix (`table_correction.propose`).
"""

from __future__ import annotations

import numpy as np

#: Cells read per check: enough that a misplaced table cannot pass by chance,
#: few enough to cost a second at a session's start.
SAMPLE = 32
#: Below this share of crops holding their own label, the table and the mask
#: disagree (`table_correction.MIN_MATCH`). A misplaced table scores near 0
#: (0 of 24 on lsp35766_p193_2), a placed one near 1 (300 of 300).
MIN_ON_LABEL = 0.5
#: Fewer cells read than this (most crops off the image, say) says nothing.
MIN_CHECKED = 12
#: The crop side, full-resolution pixels: a cell's own label is under its
#: centroid or a few pixels off it.
CROP_PX = 24


def check(session, project, *, sample=SAMPLE, seed=0) -> dict:
    """{status, checked, on_label, fraction, message}: status `ok`,
    `misaligned`, or `unchecked` (no mask, no table, no positions, or too few
    cells read -- nothing to compare)."""
    from plexora.agent.evidence import crops as cropmod
    from plexora.agent.presets import nuclear_channel
    from plexora.agent.render import resolve_channel
    from plexora.server.utils import source_image

    def unchecked(why):
        return {"status": "unchecked", "checked": 0, "on_label": 0, "fraction": None,
                "message": why}

    try:
        record = session.project(project)
    except Exception:
        return unchecked("the project could not be read")
    if not record.segmentation.available:
        return unchecked("no segmentation mask to compare the table with")
    if not record.has_table:
        return unchecked("no table")
    try:
        frame = session.data(project).table.geometry()
    except Exception:
        return unchecked("the table could not be read")
    roles = record.roles
    x, y = roles.get("x"), roles.get("y")
    ident = roles.get("cell_id") if roles.get("cell_id") in (frame.columns if frame is not None
                                                             else ()) else "id"
    if frame is None or not x or not y or x not in frame.columns or y not in frame.columns \
            or ident not in frame.columns:
        return unchecked("the table has no cell id or positions")
    ids = np.asarray(frame[ident].to_numpy(), dtype=float)
    xs = np.asarray(frame[x].to_numpy(), dtype=float)
    ys = np.asarray(frame[y].to_numpy(), dtype=float)
    rows = np.flatnonzero(np.isfinite(ids) & np.isfinite(xs) & np.isfinite(ys))
    if rows.size == 0:
        return unchecked("no cell in the table has a position")
    rows = np.random.default_rng(seed).permutation(rows)[:sample]
    cells = [{"cell_id": int(ids[i]), "x": float(xs[i]), "y": float(ys[i])} for i in rows]
    channels = list(record.image.real_channels)
    names = [c.get("fullname") or c.get("name") for c in channels]
    if not names:
        return unchecked("the image has no channel to read")
    name = nuclear_channel(names) or names[0]
    _index, found = resolve_channel(name, channels)
    try:
        crops, _info = cropmod.read_cell_crops(
            session, record, cells, channels={name: source_image.channel_key(found)},
            crop_px=CROP_PX, tile_px=CROP_PX, mask=True, level=0)
    except Exception as exc:
        return unchecked(f"the mask could not be read ({type(exc).__name__})")
    read = [c for c in crops.values() if c.mask_stats is not None]
    on = sum(1 for c in read if c.mask_stats.get("area_level_px"))
    if len(read) < min(MIN_CHECKED, len(cells)):
        return {**unchecked(f"only {len(read)} of {len(cells)} cells could be read"),
                "checked": len(read), "on_label": on}
    fraction = on / len(read) if read else 0.0
    if fraction >= MIN_ON_LABEL:
        return {"status": "ok", "checked": len(read), "on_label": on, "fraction": fraction,
                "message": f"{on} of {len(read)} sampled cells sit on their own mask label"}
    return {"status": "misaligned", "checked": len(read), "on_label": on, "fraction": fraction,
            "message": (f"only {on} of {len(read)} sampled cells sit on their own mask label: "
                        f"the table's {x}/{y} are not in the image's pixel space (another "
                        "unit, scale, offset or section). Every look would be drawn somewhere "
                        "else. Fix the table's placement first -- the viewer's table correction "
                        "(Edit project) proposes it -- then start again.")}


def refusal(project, found):
    """The AgentError a session start raises for a `misaligned` check."""
    from plexora.agent.errors import AgentError

    return AgentError("precondition_missing", f"{project}: {found['message']}",
                      detail={"alignment": {k: found[k] for k in
                                             ("checked", "on_label", "fraction")}})
