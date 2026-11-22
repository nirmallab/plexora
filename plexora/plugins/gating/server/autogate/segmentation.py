"""Whether an image's cell outlines can be trusted to the cell's edge.

A look reads ownership against the judged cell's outline. When the outlines
come from another platform's segmentation carried onto this image -- the
lsp35766_p193 colon CyCIF (2026-11-22) was read through Xenium cell
boundaries warped onto it: nuclei matched to a pixel or two, cell edges did
not, and mast cells and macrophages reached past their outlines -- the
readers called real positives "a neighbour's blob beside the nucleus". So a
session says so to every look of that image (`note`), with the signal it
read:

- **the mask's file** names a spatial-transcriptomics platform the image's
  file does not (`LSP35766A_s04_xenium_cell_labels_cycif.ome.tif`);
- **the table's mask-method column** (`METHOD_COLUMNS`) names one
  (`mask_method` = `xenium_default`);
- **the looks** named `segmentation` on `LOOK_FLAG_MIN` or more of the
  image's markers (poor outlines, whatever made them).

A note informs the reading; it never moves a gate.
"""

from __future__ import annotations

import os
import re

#: Platforms whose own segmentation is carried onto another platform's image.
PLATFORMS = ("xenium", "cosmx", "merscope", "merfish", "visium", "stereoseq", "stereo-seq",
             "seqfish", "slideseq", "slide-seq")
#: Table columns that may say how the mask was made.
METHOD_COLUMNS = ("mask_method", "segmentation_method", "seg_method")
#: [cal] Markers of one image whose looks named `segmentation` before the
#: outlines are called poor (the image-pattern threshold, `replan.PARAMS`).
LOOK_FLAG_MIN = 3

#: Ownership is still read at the outline: telling readers that a stain body
#: the outline overlaps is the cell's own, and that dim stain counts, made
#: them read E-cadherin's neighbour membranes as own on lsp35766_p193_2
#: (2026-11-22) and helped only cKit; the owner had it reverted.
TEXT = ("this image's cell outlines are approximate ({why}): read which cell a stain "
        "belongs to by its nucleus; light only along an edge shared with a brighter "
        "neighbour is the neighbour's")


def _platform(text) -> str | None:
    words = re.sub(r"[^a-z0-9-]+", " ", str(text or "").lower())
    return next((p for p in PLATFORMS if p in words), None)


def static_signals(ds) -> list:
    """[{signal, value, platform}]: what the project's own record and table say
    about where its outlines came from. Never raises."""
    out = []
    try:
        record = ds.project
    except Exception:
        return out
    image = os.path.basename(str(getattr(getattr(record, "image", None), "src", "") or ""))
    own = _platform(image)
    seg = getattr(record, "segmentation", None)
    for value in (getattr(seg, "source", None), getattr(seg, "derived", None)):
        name = os.path.basename(str(value or ""))
        platform = _platform(name)
        if platform and platform != own:
            out.append({"signal": "mask_file", "value": name, "platform": platform})
            break
    dataset = getattr(record, "dataset", None)
    listed = set(getattr(getattr(dataset, "columns", None), "metadata", ()) or ()) \
        | set(getattr(dataset, "obs_columns", ()) or ())
    for column in METHOD_COLUMNS:
        if column not in listed:
            continue
        try:
            values = ds.table.columns([column])[column]
            value = next((str(v) for v in list(values[:1000]) if str(v).strip()), None)
        except Exception:
            continue
        platform = _platform(value)
        if platform and platform != own:
            out.append({"signal": column, "value": value, "platform": platform})
            break
    return out


def look_signal(units) -> dict | None:
    """The image's looks naming `segmentation` on `LOOK_FLAG_MIN` markers or more."""
    named = sorted(u["marker"] for u in units if "segmentation" in (u.get("artifact_flags") or ()))
    if len(named) < LOOK_FLAG_MIN:
        return None
    return {"signal": "looks", "value": f"{len(named)} markers' looks named segmentation",
            "markers": named}


def note(signals) -> dict | None:
    """{note, signals} for a look's evidence, or None with nothing said."""
    if not signals:
        return None
    parts = []
    for s in signals:
        if s["signal"] == "looks":
            parts.append(s["value"])
        elif s["signal"] == "mask_file":
            parts.append(f"the mask file names {s['platform']}: {s['value']}")
        else:
            parts.append(f"the table's {s['signal']} is {s['value']}")
    return {"note": TEXT.format(why="; ".join(parts)), "signals": signals}
