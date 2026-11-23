"""Whether each marker's stain sits in the compartment the marker graph names.

A nuclear stain that does not fill the nuclei on an image (lsp35766_p193
CDX2, 2026-11-22: as bright on the ring around a nucleus as inside it, where
Ki67, TCF1 and Rb were clearly brighter inside; LSP11385 TCF1, with its
non-specific binding) is read by a look as
"stain beside the nucleus" -- a neighbour's, or nothing -- and its gate
follows that misreading. This says so once, from pixels, before any look.

Design rules (the owner's, 2026-11-22):

- **Full-resolution pixels only** (level 0). At a coarser level the
  cytoplasm is a pixel or less and every stain looks nuclear; an image whose
  pixels are coarser than `MAX_PIXEL_UM` is skipped with the reason.
- **No segmentation mask.** Most masks are nuclei dilated by a few pixels,
  so a mask says nothing about where a nucleus ends. Nuclei come from the
  marker's own imaging cycle's DNA -- the nearest nuclear channel at or before
  the marker's (`presets.nuclear_channels`) -- thresholded per crop (Otsu).
- **Per cell**: around cells bright for the marker (the top `TOP_SHARE` by
  the table's value, `N_CELLS` of them, seeded), the marker's mean over the
  nucleus's core (eroded by `CORE_ERODE_UM`) against a thin ring just outside
  it (`RING_IN_UM` to `RING_OUT_UM`, other nuclei left out), each less the
  crop's background (its 5th percentile): `log2(core / ring)`.
- **Relative to the image** (`call`): a nuclear-compartment marker is flagged
  only when its median score is at or below the upper quartile of the image's
  non-nuclear markers' and at least `MIN_REFERENCES` nuclear markers score
  above every non-nuclear one; otherwise no call. No absolute bar is set on
  one slide.
- Autofluorescence and nuclear-stain channels take no part; cells QC leaves
  out (`leave_out`) are never sampled.

A finding about a marker, never a cell call.
"""

from __future__ import annotations

import numpy as np

#: Bumped when the same image would be scored or called differently.
VERSION = "1"

#: [cal] Cells scored per marker: at forty, a marker near the line (FOXP3,
#: KLF2 on lsp35766_p193) flipped between seeds; at eighty the call held on
#: every seed (CDX2 alone, the same references).
N_CELLS = 80
TOP_SHARE = 0.02
SEED = 0
#: Crop half-side: a nucleus and its ring with room to spare.
HALF_UM = 7.8
CORE_ERODE_UM = 0.5
RING_IN_UM = 0.6
RING_OUT_UM = 1.6
#: Coarser pixels than this leave the ring under two pixels wide: skipped.
MAX_PIXEL_UM = 0.5
MIN_PIXELS = 10
#: A marker is scored from at least this many cells, or not at all.
MIN_CELLS = 10
MIN_REFERENCES = 2

FLAG = "not_in_expected_compartment"


def cell_score(dna, stain, pixel_um) -> float | None:
    """`log2(core / ring)` of `stain` around the nucleus nearest the crop's
    centre in `dna` (both 2-D, the same crop); None when no nucleus, core or
    ring can be found."""
    from scipy import ndimage as ndi
    from skimage.filters import threshold_otsu

    dna = np.asarray(dna, dtype=np.float64)
    stain = np.asarray(stain, dtype=np.float64)
    if dna.shape != stain.shape or dna.size == 0 or dna.max() <= dna.min():
        return None
    nuclei = dna > threshold_otsu(dna)
    labels, count = ndi.label(nuclei)
    if not count:
        return None
    cy, cx = dna.shape[0] // 2, dna.shape[1] // 2
    own = labels[cy, cx]
    if not own:
        yy, xx = np.indices(labels.shape)
        far = np.where(labels > 0, (yy - cy) ** 2 + (xx - cx) ** 2, np.inf)
        own = labels.flat[int(np.argmin(far))]
    nucleus = labels == own
    erode = max(1, int(round(CORE_ERODE_UM / pixel_um)))
    ring_in = max(1, int(round(RING_IN_UM / pixel_um)))
    ring_out = max(ring_in + 1, int(round(RING_OUT_UM / pixel_um)))
    core = ndi.binary_erosion(nucleus, iterations=erode)
    ring = (ndi.binary_dilation(nucleus, iterations=ring_out)
            & ~ndi.binary_dilation(nucleus, iterations=ring_in) & ~nuclei)
    if core.sum() < MIN_PIXELS or ring.sum() < MIN_PIXELS:
        return None
    background = np.percentile(stain, 5)
    inside = stain[core].mean() - background
    outside = stain[ring].mean() - background
    return float(np.log2(max(inside, 1.0) / max(outside, 1.0)))


def call(markers) -> dict:
    """The image's call from `markers` = {marker: {compartment, score, ...}}:
    {status: called | no_call, flags: {marker: score}, references, why}."""
    nuclear = {m: r["score"] for m, r in markers.items() if r.get("compartment") == "nuclear"}
    other = {m: r["score"] for m, r in markers.items() if r.get("compartment") != "nuclear"}
    if not nuclear:
        return {"status": "no_call", "flags": {}, "references": [],
                "why": "no nuclear-compartment marker was scored"}
    if not other:
        return {"status": "no_call", "flags": {}, "references": [],
                "why": "no membrane or cytoplasmic marker to compare with"}
    top_other = max(other.values())
    upper = float(np.percentile(list(other.values()), 75))
    references = sorted((m for m, s in nuclear.items() if s > top_other),
                        key=lambda m: -nuclear[m])
    if len(references) < MIN_REFERENCES:
        return {"status": "no_call", "flags": {}, "references": references,
                "why": f"{len(references)} nuclear markers sit in the nuclei more than every "
                       f"membrane or cytoplasmic one; {MIN_REFERENCES} are needed to call one off"}
    flags = {m: round(float(s), 3) for m, s in nuclear.items() if s <= upper}
    return {"status": "called", "flags": flags, "references": references,
            "why": f"nuclear references {', '.join(references)}; non-nuclear upper quartile "
                   f"{upper:.2f}, highest {top_other:.2f}"}


def own_dna(channel, names, nuclear) -> str | None:
    """The nuclear channel of `channel`'s imaging cycle: the nearest one at or
    before it in `names` (acquisition order), else the first; None without one."""
    if not nuclear:
        return None
    at = names.index(channel) if channel in names else len(names)
    before = [n for n in nuclear if names.index(n) <= at]
    return before[-1] if before else nuclear[0]


def plan(names, markers, compartment_of) -> list:
    """[(marker, channel, dna channel, compartment)] of the markers to score:
    a channel on the image, a compartment in the graph, not autofluorescence
    or a nuclear stain."""
    from plexora.agent import presets
    from plexora.ai import vocabulary

    nuclear = presets.nuclear_channels(names)
    out = []
    for marker in markers:
        channel = vocabulary.match_name(marker, names)
        if channel is None or channel in nuclear:
            continue
        if vocabulary.AUTOFLUORESCENCE_PATTERN.search(str(channel)) \
                or vocabulary.AUTOFLUORESCENCE_PATTERN.search(str(marker)):
            continue
        compartment = compartment_of(marker)
        if compartment is None:
            continue
        dna = own_dna(channel, names, nuclear)
        if dna is None:
            continue
        out.append((marker, channel, dna, compartment))
    return out


def compartment_of(marker) -> str | None:
    from plexora.ai import vocabulary

    entry = vocabulary.entry(marker) or {}
    if entry.get("role") == "context" or vocabulary.canonical(marker) == vocabulary.NUCLEAR:
        return None
    return entry.get("compartment")


def measure(read_pair, names, markers, values_of, xs, ys, pixel_um, *, leave_out=None,
            seed=SEED) -> dict:
    """The record for one image. `read_pair(cells, channel, dna, side)` gives
    {cell_id: (dna crop, stain crop)} at full resolution for `cells` =
    [{cell_id, x, y}]; `values_of(marker)` the table's values (row-aligned);
    `leave_out` rows QC leaves out."""
    record = {"version": VERSION, "pixel_um": pixel_um, "markers": {}}
    if not pixel_um:
        return {**record, "status": "skipped",
                "why": "no pixel size: the ring around a nucleus cannot be set in microns"}
    if float(pixel_um) > MAX_PIXEL_UM:
        return {**record, "status": "skipped",
                "why": f"pixels of {float(pixel_um):.2f} um leave the ring around a nucleus "
                       "under two pixels wide"}
    if xs is None or ys is None:
        return {**record, "status": "skipped", "why": "no cell positions"}
    xs = np.asarray(xs, dtype=np.float64)
    ys = np.asarray(ys, dtype=np.float64)
    usable = np.isfinite(xs) & np.isfinite(ys)
    if leave_out is not None and len(leave_out):
        usable[np.asarray(leave_out, dtype=np.int64)] = False
    side = 2 * int(round(HALF_UM / float(pixel_um)))
    todo = plan(names, markers, compartment_of)
    if not todo:
        return {**record, "status": "skipped",
                "why": "no marker with a channel and a compartment"}
    for marker, channel, dna, compartment in todo:
        values = values_of(marker)
        if values is None:
            continue
        values = np.asarray(values, dtype=np.float64)
        ok = usable & np.isfinite(values)
        if ok.sum() < MIN_CELLS:
            continue
        rows = np.flatnonzero(ok & (values >= np.quantile(values[ok], 1.0 - TOP_SHARE)))
        rng = np.random.default_rng([int(seed), sum(map(ord, str(marker)))])
        pick = rng.choice(rows, min(N_CELLS, rows.shape[0]), replace=False)
        cells = [{"cell_id": int(r), "x": float(xs[r]), "y": float(ys[r])} for r in pick]
        crops = read_pair(cells, channel, dna, side)
        scores = [s for s in (cell_score(d, m, float(pixel_um)) for d, m in crops.values())
                  if s is not None]
        if len(scores) < MIN_CELLS:
            continue
        record["markers"][marker] = {
            "compartment": compartment, "channel": channel, "dna": dna,
            "score": round(float(np.median(scores)), 3), "n": len(scores),
            "iqr": [round(float(np.percentile(scores, 25)), 3),
                    round(float(np.percentile(scores, 75)), 3)]}
    record.update(call(record["markers"]))
    for marker, entry in record["markers"].items():
        entry["call"] = FLAG if marker in record["flags"] else (
            "as_expected" if record["status"] == "called" else None)
    return record


def for_dataset(session, ds, values_of, *, leave_out=None) -> dict:
    """`measure` on a project's image and table, crops read through
    `crops.read_cell_crops` at level 0. Never raises: a failure is the
    record's `status: skipped` with why."""
    try:
        from plexora.agent.evidence import crops as cropmod
        from plexora.server.utils import pixel_scale, source_image

        record = ds.project
        if record.image.is_blank:
            return {"version": VERSION, "status": "skipped", "why": "no image", "markers": {}}
        channels = list(record.image.real_channels)
        names = [c.get("fullname") or c.get("name") for c in channels]
        keys = {n: source_image.channel_key(c) for n, c in zip(names, channels)}
        found = pixel_scale.pixel_size(record)
        pixel_um = float(found["value"]) if found else None
        frame = ds.table.geometry()
        schema = ds.schema
        xs = ys = None
        if frame is not None and schema is not None and schema.x in frame.columns \
                and schema.y in frame.columns:
            xs = frame[schema.x].to_numpy().astype(np.float64)
            ys = frame[schema.y].to_numpy().astype(np.float64)

        def read_pair(cells, channel, dna, side):
            got, _info = cropmod.read_cell_crops(
                session, record, cells, channels={dna: keys[dna], channel: keys[channel]},
                crop_px=side, tile_px=side, mask=False, level=0)
            return {cid: (crop.planes[dna], crop.planes[channel]) for cid, crop in got.items()
                    if dna in crop.planes and channel in crop.planes}

        return measure(read_pair, names, list(ds.table.markers), values_of, xs, ys, pixel_um,
                       leave_out=leave_out)
    except Exception as exc:  # noqa: BLE001 -- a finding, never a reason to fail QC or a look
        return {"version": VERSION, "status": "skipped", "markers": {},
                "why": f"could not be measured: {type(exc).__name__}: {exc}"[:240]}


def note(record, marker) -> str | None:
    """The plain words a look of `marker` carries when the image's record
    flags it, else None."""
    entry = ((record or {}).get("markers") or {}).get(marker) or {}
    if entry.get("call") != FLAG:
        return None
    references = ", ".join((record.get("references") or [])[:3])
    return (f"on this image {marker}'s stain does not sit in the nuclei as a nuclear marker's "
            f"should: around its brightest cells it is as bright just outside the nucleus as "
            f"inside it, where {references} are much brighter inside. Judge whether the stain "
            f"belongs to the cell wherever it sits in that cell; do not call a cell unstained "
            f"only because its nucleus is not filled")
